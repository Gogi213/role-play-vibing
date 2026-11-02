"""Сторож «у цели нет следующего шага» (TK-086, В-33, В-24): у открытой цели (G-NNN) принят шаг, а открытого тикета на ту же
цель нет дольше `goal_idle_min` минут — CEO будят, даже если рядом крутится посторонний тикет (alpha 02.11: критический путь
цели стоял 6,7 ч, потому что in_progress был у побочного). Вид `watch-goal-idle` будит CEO один раз на событие
(цель + принятый тикет), по таймеру не повторяется. Тикет без цели и цель без принятых шагов сигнала не дают.
Находку собирает `watch.check_goal_idle`; здесь только проверка. Шаг — тикет, чьё описание ссылается на цель
(`Ссылка на задачу владельца: G-NNN`), закрытый приёмкой Судьи."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import project as P  # noqa: E402
import project_paths as CFG  # noqa: E402
import registry  # noqa: E402
import report as RP  # noqa: E402 — GOAL_REF и last_verdict: та же мера «принято», что в отчёте
import ticket as T  # noqa: E402

GOAL_IDLE_MIN = 10  # умолчание, как у `unreleased.UNRELEASED_IDLE_MIN`; точное число — за Судьёй (В-4/В-10), меняется ключом `goal_idle_min`
SEEN_KEY = "goal_idle_seen"  # watch-state.json: события, о которых CEO уже сказали


def threshold_min() -> int:
    v = P.setting("goal_idle_min", GOAL_IDLE_MIN, root=CFG.PROJECT_ROOT)  # корень явно: общий `P._ROOT` в полном прогоне другой (CI красный на 98169f5)
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 1 else GOAL_IDLE_MIN


def _goal_state() -> tuple:
    """({цель: (время принятия, тикет)}) по последнему принятому шагу и {цели, у которых есть открытый тикет}; None —
    тикет не читается (неизвестно, что в нём: молчим, как `unreleased.queue_empty`)."""
    goals = registry.goals(CFG.PROJECT_ROOT)
    open_goals = {g for g, p in goals.items() if registry.goal_header(p).get("status") == "open"}
    accepted, covered = {}, set()
    for path in T.list_tickets(CFG.TICKETS_DIR):
        try:
            tkt = T.read_ticket(path)
        except Exception:
            return None
        m = RP.GOAL_REF.search(tkt.description)
        gid = m.group(1) if m else ""
        if gid not in open_goals:
            continue
        if tkt.status in T.OPEN_STATUSES:
            covered.add(gid)
        verdict = RP.last_verdict(tkt) if tkt.status == "done" and tkt.owner != "judge" else None
        if verdict and verdict[0] == "accept" and (gid not in accepted or verdict[1].ts > accepted[gid][0]):
            accepted[gid] = (verdict[1].ts, tkt.id)
    return accepted, covered


def goal_idle_events(ws: dict, now) -> list:
    """Как `_new_events`, но сбой чтения не роняет цикл сторожа."""
    try:
        return _new_events(ws, now)
    except Exception as e:
        print(f"[watch] goal_idle: {type(e).__name__}: {e}", file=sys.stderr)
        return []


def _new_events(ws: dict, now) -> list:
    """[(цель, тикет, текст)] новых событий: шаг цели принят ≥ `goal_idle_min` мин назад, открытого тикета цели нет.
    Событие, о котором уже сказали (метка в ws), не повторяется; ушло (следующий тикет заведён) — метка забывается."""
    state = _goal_state()
    if state is None:
        return []
    accepted, covered = state
    seen = ws.setdefault(SEEN_KEY, {})
    due = {}
    for gid, (ts, tid) in accepted.items():
        age = (now - ts).total_seconds() / 60
        if gid not in covered and age >= threshold_min():
            due[f"{gid}@{tid}"] = (gid, tid, age)
    for key in [k for k in seen if k not in due]:
        seen.pop(key)
    out = []
    for key, (gid, tid, age) in due.items():
        if key not in seen:
            seen[key] = T.now_iso(now)
            out.append((gid, tid, f"цель {gid}: принят {tid} {age:.0f} мин назад, следующего шага нет — завести тикет или закрыть цель"))
    return out
