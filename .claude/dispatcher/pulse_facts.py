"""Пульс v2 (TK-105, В-50): факты карточки проекта для страницы и Telegram — цель и шаг, кто что делает, вопрос владельцу, причина
«застряло» по-человечески, сделано сегодня, очередь, итоги дня. Всё из тикетов, целей, `state.json`; нет источника — поля нет."""
from __future__ import annotations

import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
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


def ask_of(tickets, owner_items: list, now: datetime) -> dict | None:
    """Первый вопрос владельцу: тикет, вопрос (последняя запись `[итог: ask-owner]`, иначе заголовок тикета), сколько ждёт."""
    for tid, _ in owner_items:
        tkt = next((t for t in tickets if t.id == tid), None)
        if not tkt:
            continue
        asked = next(((e, m) for e in reversed(tkt.log) if (m := ASK_MARK.search(e.text.strip()))), None)
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
    """Сделано сегодня (принято Судьёй), очередь, итоги дня: принято, возвратов, в работе, в очереди."""
    running = set(state.get("active_runs") or {})
    done = sorted(((at, t) for t in tickets if (at := _accepted_at(t, now)) and at.date() == now.date()), key=lambda r: r[0], reverse=True)
    queue = sorted((t for t in tickets if t.status in T.OPEN_STATUSES and t.id not in running), key=lambda t: t.id)
    return {"done_today": [{"time": f"{at:%H:%M}", "ticket": t.id, "title": _title(t)} for at, t in done[:LIST_MAX]],
            "queue": [{"ticket": t.id, "title": _title(t)} for t in queue[:LIST_MAX]],
            "day": {"accepted": len(done), "returns": sum(_returned_today(t, now) for t in tickets), "working": len(running),
                    "queued": len(queue)}}


def facts(state: dict, tickets, active: list, owner_items: list, now: datetime) -> dict:
    """Поля версии 2 для `pulse.build`; тикеты не прочитались (None) — пусто, ничего не выдумываем."""
    if tickets is None:
        return {}
    job = job_of(state, tickets, now)
    fresh = sorted((t for t in tickets if t.status in T.OPEN_STATUSES), key=lambda t: str(t.header.get("updated") or ""), reverse=True)
    focus = ([job["ticket"]] if job else []) + [t for t, _ in owner_items] + [t.id for t in fresh]
    out = dict(day_of(tickets, state, now))
    for key, value in (("goal", goal_card(tickets, focus)), ("job", job), ("ask", ask_of(tickets, owner_items, now)),
                       ("fix", fix_of(active, now))):
        if value:
            out[key] = value
    return out
