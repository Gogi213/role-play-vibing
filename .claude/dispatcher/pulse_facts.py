"""Пульс v2 (TK-105, В-50): факты карточки проекта для страницы и Telegram — цель и шаг, кто что делает, вопрос владельцу, причина
«застряло» по-человечески, сделано сегодня, очередь, итоги дня; план цели по шагам и «нет движения N мин» (TK-108, В-53); таблица «Статус» раскрытой карточки (TK-117, В-57). Всё из
тикетов, целей, `state.json`; нет источника — поля нет."""
from __future__ import annotations

import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import project as P  # noqa: E402
import project_paths as CFG  # noqa: E402
from pulse_view import ago  # noqa: E402 — «3 дня», «2 ч 10 мин»: одна мера у писателя и читателя
import registry  # noqa: E402
import report as RP  # noqa: E402 — GOAL_REF, last_verdict, _plain, _goal_title: та же мера «принято», что в отчёте
import ticket as T  # noqa: E402

STEP_MARK = re.compile(r"Шаг (\d+) из (\d+) цели (G-\d{3,})\.")  # строка тикета шага (goal_steps.MARK без якоря строки)
ROLE_RU = {"engineer": "Инженер", "judge": "Судья", "researcher": "Исследователь", "ceo": "CEO"}
VERB = {"judge": "проверяет"}  # остальные делают
FIX = {"dispatcher_dead": "перезапускает CEO", "unserved": "разбирает CEO", "step_uncovered": "заводит тикет CEO", "loop": "разбирает CEO",
       "merged_unreleased": "решает CEO", "ci_red": "чинит Инженер"}
ASK_MARK = re.compile(r"\[итог: ask-owner\]\s*(.*?)(?:\s+повод:.*)?$", re.S)
LIST_MAX = 12
SHORT, LEFT_SHORT, LEFT_MAX, DONE_SHOWN = 120, 60, 4, 3  # знаков «Что» · знаков шага в «Осталось» · шагов в «Осталось» · принятых строк в таблице
SERVICE = re.compile(r"^(?:(?:Хвост|Правка|Возврат|Доработка)\s+)?TK-\d+\s*[:—–-]\s*")  # служебный префикс заголовка: «Хвост TK-112: …»
WAIT_TEXT = {"ticket": "ждёт {1}", "merged": "ждёт слияния", "ci": "ждёт CI", "ci-run": "ждёт CI", "job": "ждёт задание на {1}"}  # по первой части parse_wait_for; file, host и at — в `_waits_on` (имя файла, юнит, местное время)
MOVE_WARN_MIN, MOVE_BAD_MIN = 15, 30  # «стоит N мин» жёлтым / красным, минуты (ключи `.claude/rpv.json`, docs/pulse.md)


def _title(tkt) -> str:
    return RP._plain(str(tkt.header.get("title") or "")) or tkt.id


def _goal_id(tkt) -> str:
    m = RP.GOAL_REF.search(tkt.description)
    return m.group(1) if m else ""


def _accepted_at(tkt, now: datetime):
    verdict = RP.last_verdict(tkt) if tkt.status == "done" and tkt.owner != "judge" else None
    return verdict[1].ts.astimezone(now.tzinfo) if verdict and verdict[0] == "accept" else None


def goal_card(tickets, focus: list) -> dict | None:
    """Цель владельца для карточки: открытая цель первого из `focus` (id тикетов по важности), у которого она есть. Шаг — наибольший
    номер в строках «Шаг N из M цели G-NNN.» тикетов этой цели (остановленные не в счёт); таких строк нет — без шага и полоски."""
    by_id = {t.id: t for t in tickets}
    goals = registry.goals(CFG.PROJECT_ROOT)
    for tid in focus:
        gid = _goal_id(by_id[tid]) if tid in by_id else ""
        path = goals.get(gid)
        if not path or registry.goal_header(path).get("status") != "open":
            continue
        card = {"id": gid, "title": RP._goal_title(path)}
        marks = [STEP_MARK.search(t.description) for t in tickets if t.status != "stopped"]
        marks = [(int(m.group(1)), int(m.group(2))) for m in marks if m and m.group(3) == gid]
        if marks:
            card["step"], card["steps"] = max(marks)  # номер и «из M» — из одной строки (наибольший номер)
        return card
    return None


def run_entry(tid: str, role: str, tickets) -> dict:
    """Запуск роли для карточки и подсказки клетки: кто, глагол, тикет, заголовок (пусто, если тикета нет)."""
    tkt = next((t for t in tickets or [] if t.id == tid), None)
    role = role.strip().lower()
    return {"who": ROLE_RU.get(role, ""), "verb": VERB.get(role, "делает"), "ticket": tid, "title": _title(tkt) if tkt else ""}


def job_of(state: dict, tickets, now: datetime) -> dict | None:
    """Самый давний идущий запуск роли из `state["active_runs"]`: роль, глагол, тикет, заголовок, сколько идёт."""
    runs = sorted((r.get("started") or "", tid, r.get("role") or "") for tid, r in (state.get("active_runs") or {}).items())
    if not runs:
        return None
    started, tid, role = runs[0]
    out = {"role": role.strip().lower(), **run_entry(tid, role, tickets)}
    try:
        out["since"] = ago((now - T.parse_dt(started)).total_seconds())
    except ValueError:
        pass
    return out


def _asked(tkt):
    """(запись, разбор) последней записи `[итог: ask-owner]` тикета или None."""
    return next(((e, m) for e in reversed(tkt.log) if (m := ASK_MARK.search(e.text.strip()))), None)


def ask_of(tickets, owner_items: list, now: datetime) -> dict | None:
    """Первый вопрос владельцу: тикет, вопрос (последняя запись `[итог: ask-owner]`, иначе заголовок тикета), сколько ждёт."""
    for tid, _ in owner_items:
        tkt = next((t for t in tickets if t.id == tid), None)
        if not tkt:
            continue
        asked = _asked(tkt)
        out = {"ticket": tid, "question": (asked[1].group(1).strip() if asked else "") or _title(tkt)}
        if asked:
            out["since"] = ago((now - asked[0].ts).total_seconds())
        return out
    return None


def fix_of(active: list, now: datetime) -> dict | None:
    """Первая причина «застряло» человеческими словами и кто чинит; `active` — [(код, подробность, с какого времени)]."""
    if not active:
        return None
    code, detail, since = active[0]
    try:
        long = ago((now - T.parse_dt(since)).total_seconds())
    except ValueError:
        long = ""
    goal = re.search(r"G-\d{3,}", detail)
    text = {"dispatcher_dead": f"диспетчер молчит {long}".strip(),
            "step_uncovered": (f"у цели {goal.group(0)} нет следующего шага" + (f" уже {long}" if long else "")) if goal else detail}.get(code, detail)
    return {"text": text, "who": FIX.get(code, "разбирает CEO")}


def _returned_today(tkt, now: datetime) -> int:
    n = 0
    for e in tkt.log:
        m = RP.VERDICT.match(e.text.strip()) if T.author_is(e.author, "judge") else None
        n += bool(m and m.group(1) == "return" and e.ts.astimezone(now.tzinfo).date() == now.date())
    return n


def day_of(tickets, state: dict, now: datetime) -> dict:
    """Очередь, итоги дня: принято, возвратов, в работе, в очереди."""
    running = set(state.get("active_runs") or {})
    accepted = sum(1 for t in tickets if (at := _accepted_at(t, now)) and at.date() == now.date())
    queue = sorted((t for t in tickets if t.status in T.OPEN_STATUSES and t.id not in running), key=lambda t: t.id)
    return {"queue": [{"ticket": t.id, "title": _title(t)} for t in queue[:LIST_MAX]],
            "day": {"accepted": accepted, "returns": sum(_returned_today(t, now) for t in tickets), "working": len(running),
                    "queued": len(queue)}}


def move_limits() -> tuple:
    """(жёлтое от, красное от) минут без движения: ключи rpv.json; не целое ≥ 1 — умолчание. Ключи записаны буквально (docs/configuration.md)."""
    root = CFG.PROJECT_ROOT
    got = (P.setting("pulse_move_warn_min", MOVE_WARN_MIN, root=root), P.setting("pulse_move_bad_min", MOVE_BAD_MIN, root=root))
    return tuple(v if isinstance(v, int) and not isinstance(v, bool) and v >= 1 else d for v, d in zip(got, (MOVE_WARN_MIN, MOVE_BAD_MIN)))


def _last_move(tkt, runs: list, state: dict):
    """Позже из последней записи лога тикета, последнего запуска роли (runs.log) и начала идущего запуска; нет ничего — None."""
    stamps = [tkt.log[-1].ts] if tkt.log else []
    stamps += [ts for ts, tid, _, _ in runs if tid == tkt.id]
    try:
        stamps.append(T.parse_dt(((state.get("active_runs") or {}).get(tkt.id) or {}).get("started") or ""))
    except ValueError:
        pass
    return max(stamps) if stamps else None


def _idle(tkt, runs: list, state: dict, now: datetime) -> tuple:
    """(минут без движения, уровень «warn»/«bad»/«»); движения не найдено — (None, "")."""
    last = _last_move(tkt, runs, state)
    if last is None:
        return None, ""
    minutes = max(0, int((now - last).total_seconds() // 60))
    warn, bad = move_limits()
    return minutes, "bad" if minutes >= bad else "warn" if minutes >= warn else ""


def _accepted_text(at, now: datetime) -> str:
    """«19:03» (сегодня) · «вчера» · «07.11»; времени приёмки нет — пусто."""
    if at is None:
        return ""
    days = (now.date() - at.date()).days
    return f"{at:%H:%M}" if days == 0 else "вчера" if days == 1 else f"{at:%d.%m}"


def _plan_row(n: int, title: str, tickets: list, state: dict, owner_ids: set, stuck_text: str, runs: list, now: datetime) -> dict:
    """Строка плана: mark — done (принят) · run (идёт) · ask (ждёт слова владельца) · stuck (застряло) · next (впереди)."""
    row = {"mark": "next", "n": n, "title": title}
    accepted = [t for t in tickets if t.status == "done"]
    live = [t for t in tickets if t.status != "stopped"]
    if accepted:
        tkt = accepted[-1]
        row.update(mark="done", ticket=tkt.id)
        if at := _accepted_text(_accepted_at(tkt, now), now):
            row["at"] = at
        return row
    if not tickets:
        return row
    tkt = (live or tickets)[-1]
    running = (state.get("active_runs") or {}).get(tkt.id) or {}
    row.update(ticket=tkt.id, who=ROLE_RU.get(str(running.get("role") or tkt.owner).strip().lower(), ""))
    if tkt.id in owner_ids:
        asked = _asked(tkt)
        at = asked[0].ts if asked else (tkt.log[-1].ts if tkt.log else None)
        row["mark"] = "ask"
        if at:
            row["waiting"] = ago((now - at).total_seconds())
        return row
    row["mark"] = "stuck" if not live or re.search(rf"\b{re.escape(tkt.id)}\b", stuck_text) else "run"
    minutes, level = _idle(tkt, runs, state, now)
    if minutes is not None:
        row.update(idle=ago(minutes * 60), level=level)
    return row


def plan_of(gid: str, tickets, state: dict, active: list, owner_items: list, runs: list, now: datetime) -> list:
    """План цели владельца по шагам; у цели нет плана (`goal --step`) — открытые тикеты цели тем же списком (без номеров шагов)."""
    owner_ids, stuck_text = {t for t, _ in owner_items}, " ".join(d for _, d, _ in active)
    steps = registry.goal_steps(CFG.PROJECT_ROOT, gid)
    if steps:
        marked: dict = {}
        for t in sorted(tickets, key=lambda t: t.id):
            if (m := STEP_MARK.search(t.description)) and m.group(3) == gid:
                marked.setdefault(int(m.group(1)), []).append(t)
        return [_plan_row(s["n"], s["title"], marked.get(s["n"], []), state, owner_ids, stuck_text, runs, now) for s in steps]
    own = sorted((t for t in tickets if t.status in T.OPEN_STATUSES and _goal_id(t) == gid), key=lambda t: t.id)
    return [_plan_row(0, _title(t), [t], state, owner_ids, stuck_text, runs, now) for t in own[:LIST_MAX]]


def plan_step(plan: list) -> tuple:
    """(N, M) для «шаг N из M» и полоски — из того же списка: N — первый ещё не принятый шаг (все приняты — последний); без номеров — (0, 0)."""
    numbered = [r for r in plan if r["n"]]
    if not numbered:
        return 0, 0
    return next((r["n"] for r in numbered if r["mark"] != "done"), numbered[-1]["n"]), len(numbered)


def short(title: str, size: int = SHORT) -> str:
    """Заголовок для таблицы: без служебного «Хвост TK-N:», с большой буквы, не длиннее `size` знаков (обрезка по слову, «…»)."""
    text = SERVICE.sub("", " ".join(title.split()))
    text = text[:1].upper() + text[1:]
    if len(text) <= size:
        return text
    cut = text[:size - 1]
    if " " in cut[size // 2:]:
        cut = cut[:cut.rindex(" ")]
    return cut.rstrip(" ,;:(—-") + "…"


def _of_goal(tkt, gid: str) -> bool:
    mark = STEP_MARK.search(tkt.description)
    return _goal_id(tkt) == gid or bool(mark and mark.group(3) == gid)


def _waits_on(tkt) -> str:
    """Чего ждёт `waiting` тикет, по форме `wait_for`: «ждёт TK-N» · «ждёт слияния» · «ждёт CI» · «ждёт файл имя» · «ждёт процесс юнит на хост» ·
    «ждёт итог расчёта на хост» · «ждёт задание на хост» · «ждёт до дд.мм чч:мм»; пусто или форма не понята — «ждёт, не сказано чего»."""
    parsed = T.parse_wait_for(str(tkt.header.get("wait_for") or ""))
    if not parsed:
        return "ждёт, не сказано чего"
    if parsed[0] == "file":
        return f"ждёт файл {parsed[1].replace(chr(92), '/').rsplit('/', 1)[-1]}"
    if parsed[0] == "host":
        return f"ждёт процесс {parsed[3]} на {parsed[1]}" if parsed[2] == "unit" else f"ждёт итог расчёта на {parsed[1]}"
    if parsed[0] == "at":
        return f"ждёт до {parsed[1].astimezone():%d.%m %H:%M}"
    return WAIT_TEXT[parsed[0]].format(*parsed)


def _live_phrase(tkt, role: str) -> str:
    """Кто сейчас держит живой тикет, до пяти слов: «у Судьи» · «в очереди» · «Инженер правит после возврата» · «Инженер делает»."""
    role = (role or str(tkt.owner)).strip().lower()
    if role == "judge" or tkt.status == "in_review":
        return "у Судьи"
    if tkt.status == "todo" and role == tkt.owner:
        return "в очереди"
    verdict = RP.last_verdict(tkt)
    if role == "engineer" and verdict and verdict[0] == "return":
        return "Инженер правит после возврата"
    name = ROLE_RU.get(role)
    return f"{name} делает" if name else "в работе"


def _blockers(plan: list) -> dict:
    """{тикет шага: тикет ближайшего предыдущего не принятого шага} — «ждёт TK-N» по порядку плана (номер шага `n`); шаг без тикета пропущен."""
    out, prev = {}, ""
    for row in sorted((r for r in plan if r["n"] and r["mark"] != "done" and r.get("ticket")), key=lambda r: r["n"]):
        if prev:
            out[row["ticket"]] = prev
        prev = row["ticket"]
    return out


def _where(tkt, state: dict, owner_ids: set, stuck_text: str, runs: list, now: datetime, after: str = "") -> tuple:
    """(отметка, «Где сейчас», уровень, минут без движения): принят чч:мм · ждёт вашего ответа · ждёт CEO (blocked) · ждёт TK-N (wait_for, а
    в очереди — предыдущий шаг плана `after`) · стоит N мин (жёлтое/красное от порогов TK-108; очередь и ожидание не «стоят») · кто держит.
    Отметка — из PLAN_MARKS страницы: done / ask / next / stuck / run."""
    if tkt.status == "done":
        at = _accepted_text(_accepted_at(tkt, now), now)
        return "done", f"принят {at}" if at else "принят", "", None
    if tkt.id in owner_ids or tkt.status == "needs_owner":
        return "ask", "ждёт вашего ответа", "", None
    named = bool(re.search(rf"\b{re.escape(tkt.id)}\b", stuck_text))
    minutes, level = _idle(tkt, runs, state, now)
    if tkt.status == "blocked":
        return "stuck", "ждёт CEO", level, minutes
    if tkt.status == "waiting" and not named:
        return "next", _waits_on(tkt), "", None
    if named or (level and tkt.status != "todo"):
        return "stuck" if named else "run", f"стоит {ago(minutes * 60)}" if minutes is not None else "застряло", level or "bad", minutes
    running = (state.get("active_runs") or {}).get(tkt.id) or {}
    phrase = _live_phrase(tkt, str(running.get("role") or ""))
    if phrase == "в очереди":
        return "next", f"ждёт {after}" if after else phrase, "", minutes
    return "run", phrase, "", minutes


def _hold(ask: dict | None, fix: dict | None, rows: list) -> str:
    """Одна главная причина: вопрос владельцу › застряло › самый долгий без движения (только от порога жёлтого)."""
    if ask and ask.get("ticket"):
        return f"ваш ответ по {ask['ticket']}"
    if fix:
        return fix["text"]
    idle = [(m, r) for r, m in rows if r.get("level") and m is not None]
    longest = max(idle, key=lambda x: x[0])[1] if idle else None
    return f"{longest['ticket']} {longest['where']}" if longest else ""


def _left(plan: list, open_rows: list) -> str:
    """«Осталось»: шаги плана, ещё не принятые (коротко), а плана нет — номера открытых тикетов; не больше LEFT_MAX, остальные — «и ещё N»."""
    items = ([short(r["title"], LEFT_SHORT) for r in plan if r["mark"] != "done"] if any(r["n"] for r in plan)
             else [r["ticket"] for r in open_rows])
    return ", ".join(items[:LEFT_MAX]) + (f" и ещё {len(items) - LEFT_MAX}" if len(items) > LEFT_MAX else "")


def status_of(gid: str, tickets, state: dict, active: list, owner_items: list, runs: list, now: datetime, plan: list,
              ask: dict | None, fix: dict | None) -> dict | None:
    """Блок «Статус» раскрытой карточки (TK-117, В-57): таблица Задача | Что | Где сейчас по тикетам цели `gid` (нет цели — по открытым
    тикетам проекта), «Осталось» и «Держит». Принятых больше DONE_SHOWN — последние DONE_SHOWN, остальные счётчиком `more`. Пусто — None."""
    own = sorted((t for t in tickets if t.status != "stopped" and (_of_goal(t, gid) if gid else t.status in T.OPEN_STATUSES)), key=lambda t: t.id)
    done, live = [t for t in own if t.status == "done"], [t for t in own if t.status != "done"]
    owner_ids, stuck_text = {t for t, _ in owner_items}, " ".join(d for _, d, _ in active)
    rows, blockers = [], _blockers(plan)
    for tkt in done[-DONE_SHOWN:] + live[:LIST_MAX]:
        mark, where, level, minutes = _where(tkt, state, owner_ids, stuck_text, runs, now, blockers.get(tkt.id, ""))
        row = {"ticket": tkt.id, "title": short(str(tkt.header.get("title") or tkt.id)), "mark": mark, "where": where}
        rows.append(({**row, "level": level} if level else row, minutes))
    out = {"rows": [r for r, _ in rows], "more": max(0, len(done) - DONE_SHOWN), "left": _left(plan, [{"ticket": t.id} for t in live]),
           "hold": _hold(ask, fix, rows)}
    return out if out["rows"] or out["left"] else None


def facts(state: dict, tickets, active: list, owner_items: list, now: datetime, runs: list | None = None) -> dict:
    """Поля версии 2 для `pulse.build`; тикеты не прочитались (None) — пусто, ничего не выдумываем."""
    if tickets is None:
        return {}
    job = job_of(state, tickets, now)
    fresh = sorted((t for t in tickets if t.status in T.OPEN_STATUSES), key=lambda t: str(t.header.get("updated") or ""), reverse=True)
    focus = ([job["ticket"]] if job else []) + [t for t, _ in owner_items] + [t.id for t in fresh]
    out, goal = dict(day_of(tickets, state, now)), goal_card(tickets, focus)
    ask, fix = ask_of(tickets, owner_items, now), fix_of(active, now)
    plan = plan_of(goal["id"], tickets, state, active, owner_items, runs or [], now) if goal else []
    if plan:
        out["plan"] = plan
        step, steps = plan_step(plan)  # номер и «из M» — из списка плана, а не из отдельной выборки тикетов
        if steps:
            goal.update(step=step, steps=steps)
    if status := status_of(goal["id"] if goal else "", tickets, state, active, owner_items, runs or [], now, plan, ask, fix):
        out["status"] = status
    for key, value in (("goal", goal), ("job", job), ("ask", ask), ("fix", fix)):
        if value:
            out[key] = value
    return out
