"""Сторож «у цели нет следующего шага» (TK-086, В-33, В-24): у открытой цели (G-NNN) принят шаг, а открытого тикета на ту же
цель нет дольше `goal_idle_min` минут — CEO будят, даже если рядом крутится посторонний тикет (alpha 02.11: критический путь
цели стоял 6,7 ч, потому что in_progress был у побочного). Вид `watch-goal-idle` будит CEO один раз на событие
(цель + принятый тикет), по таймеру не повторяется. Тикет без цели и цель без принятых шагов сигнала не дают.
Находку собирает `watch.check_goal_idle`; здесь только проверка. Шаг — тикет, чьё описание ссылается на цель
(`Ссылка на задачу владельца: G-NNN`), закрытый приёмкой Судьи.
Второй вид события — хвост без цели: тикет принят с `--tail ceo:"…"`, а за `goal_idle_min` минут не заведён тикет, в чьём
названии или описании есть «Хвост TK-N» (`new --title "Хвост TK-N: …"`; `--ref` у `new` — только номер реестра), и CEO не
снял хвост записью `ceo` в логе принятого тикета «хвост снят» или «хвост закрыт» (`comment TK-N --author ceo --text "хвост снят: …"`)."""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import project as P  # noqa: E402
import project_paths as CFG  # noqa: E402
import registry  # noqa: E402
import report as RP  # noqa: E402 — GOAL_REF и last_verdict: та же мера «принято», что в отчёте
import ticket as T  # noqa: E402

GOAL_IDLE_MIN = 10  # умолчание, как у `unreleased.UNRELEASED_IDLE_MIN`; точное число — за Судьёй (В-4/В-10), меняется ключом `goal_idle_min`
TAIL_MARK = "хвост CEO:"  # пометка приёмки в логе: `tickets._tail_ack` пишет « хвост CEO: <что>»
TAIL_FIELDS = re.compile(r"\s(?:вживую|критик|проверки договора): ")  # поля приёмки после текста хвоста (`tickets.cmd_accept`)
TAIL_GOAL = "хвост"  # вместо номера цели в ключе события и в находке
TAIL_CLEARED = re.compile(r"хвост\s+(?:снят|закрыт)", re.IGNORECASE)  # другие слова о хвосте (нет, остаётся) его не снимают
SEEN_KEY = "goal_idle_seen"  # watch-state.json: события, о которых CEO уже сказали


def threshold_min() -> int:
    v = P.setting("goal_idle_min", GOAL_IDLE_MIN, root=CFG.PROJECT_ROOT)  # корень явно: общий `P._ROOT` в полном прогоне другой (CI красный на 98169f5)
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 1 else GOAL_IDLE_MIN


def _read_all():
    """Все тикеты проекта; None — какой-то не читается (неизвестно, что в нём: молчим, как `unreleased.queue_empty`)."""
    out = []
    for path in T.list_tickets(CFG.TICKETS_DIR):
        try:
            out.append(T.read_ticket(path))
        except Exception:
            return None
    return out


def _accepted(tkt):
    """Запись приёмки Судьи по сданному тикету (закрыт, не тикет Судьи) или None."""
    verdict = RP.last_verdict(tkt) if tkt.status == "done" and tkt.owner != "judge" else None
    return verdict[1] if verdict and verdict[0] == "accept" else None


def _goal_state(tickets) -> tuple:
    """({цель: (время принятия, тикет)}) по последнему принятому шагу и {цели, у которых есть открытый тикет}."""
    goals = registry.goals(CFG.PROJECT_ROOT)
    open_goals = {g for g, p in goals.items() if registry.goal_header(p).get("status") == "open"}
    accepted, covered = {}, set()
    for tkt in tickets:
        m = RP.GOAL_REF.search(tkt.description)
        gid = m.group(1) if m else ""
        if gid not in open_goals:
            continue
        if tkt.status in T.OPEN_STATUSES:
            covered.add(gid)
        entry = _accepted(tkt)
        if entry and (gid not in accepted or entry.ts > accepted[gid][0]):
            accepted[gid] = (entry.ts, tkt.id)
    return accepted, covered


def _tail_handled(tkt, entry, tickets) -> bool:
    """Хвост принятого тикета занят: есть тикет с «Хвост TK-N» в названии или описании (форма из устава Судьи; простое
    упоминание номера не в счёт) либо `ceo` после приёмки записал «хвост снят» / «хвост закрыт»."""
    named = re.compile(r"(?<!\w)хвост\s+" + re.escape(tkt.id) + r"(?!\w)", re.IGNORECASE)
    if any(o.id != tkt.id and named.search(f"{o.header.get('title') or ''} {o.description}") for o in tickets):
        return True
    return any(T.author_is(e.author, "ceo") and e.ts > entry.ts and TAIL_CLEARED.search(e.text) for e in tkt.log)


def _tail_text(entry_text: str) -> str:
    """Текст хвоста из записи приёмки: от «хвост CEO:» до следующего поля приёмки, иначе до « — » перед выводом Судьи."""
    rest = entry_text.split(TAIL_MARK, 1)[1]
    field = TAIL_FIELDS.search(rest)
    return (rest[:field.start()] if field else rest.split(" — ", 1)[0]).strip()


def _tail_state(tickets) -> dict:
    """{тикет: (время принятия, что сделать CEO)} — принят с `--tail ceo:` и хвост ещё никто не занял."""
    out = {}
    for tkt in tickets:
        entry = _accepted(tkt)
        if entry and TAIL_MARK in entry.text and not _tail_handled(tkt, entry, tickets):
            out[tkt.id] = (entry.ts, _tail_text(entry.text))
    return out


def goal_idle_events(ws: dict, now) -> list:
    """Как `_new_events`, но сбой чтения не роняет цикл сторожа."""
    try:
        return _new_events(ws, now)
    except Exception as e:
        print(f"[watch] goal_idle: {type(e).__name__}: {e}", file=sys.stderr)
        return []


def _due(tickets, now) -> dict:
    """{ключ события: (метка, тикет, текст)} — всё, о чём CEO пора сказать сейчас."""
    accepted, covered = _goal_state(tickets)
    limit, due = threshold_min(), {}
    for gid, (ts, tid) in accepted.items():
        age = (now - ts).total_seconds() / 60
        if gid not in covered and age >= limit:
            due[f"{gid}@{tid}"] = (gid, tid, f"цель {gid}: принят {tid} {age:.0f} мин назад, следующего шага нет — завести тикет или закрыть цель")
    for tid, (ts, what) in _tail_state(tickets).items():
        age = (now - ts).total_seconds() / 60
        if age >= limit:
            due[f"{TAIL_GOAL}@{tid}"] = (TAIL_GOAL, tid, f"{tid}: тикета на хвост нет {age:.0f} мин — хвост CEO «{what}» — завести тикет "
                                                         f"«Хвост {tid}: …» или снять хвост записью ceo «хвост снят» в {tid}")
    return due


def _new_events(ws: dict, now) -> list:
    """[(цель или «хвост», тикет, текст)] новых событий: шаг цели принят ≥ `goal_idle_min` мин назад, открытого тикета цели нет;
    или принятый с `--tail ceo:` тикет без тикета на хвост. Событие, о котором уже сказали (метка в ws), не повторяется;
    ушло (следующий тикет заведён, хвост занят) — метка забывается."""
    tickets = _read_all()
    if tickets is None:
        return []
    seen = ws.setdefault(SEEN_KEY, {})
    due = _due(tickets, now)
    for key in [k for k in seen if k not in due]:
        seen.pop(key)
    out = []
    for key, event in due.items():
        if key not in seen:
            seen[key] = T.now_iso(now)
            out.append(event)
    return out
