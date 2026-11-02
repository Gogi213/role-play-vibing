#!/usr/bin/env python3
"""План шагов цели (TK-087, В-35): шаги ведёт диспетчер, а не CEO. Только stdlib.

План — раздел «## План шагов» цели владельца (`registry.py goal --step "<роль> | <заголовок> | <готово, когда>"`), CEO показал его
владельцу вместе с целью. Диспетчер каждый тик смотрит открытые цели с планом: первый шаг без тикета заводит, когда все
предыдущие приняты (тикет шага — статус `done`), тем же путём, что `tickets.py new` (ссылка на цель, Судья на тикете). В описании
тикета шага — строка «Шаг N из M цели G-NNN.»: по ней шаг узнаётся. Все шаги приняты, а цель не принята владельцем — одна строка
CEO `следующий-не-определён` (на цель один раз). Тикет шага не `done` — ждём, ничего не заводим. Все тикеты шага `stopped` —
план встал: одна строка CEO `шаг-остановлен` на тикет. Хоть один тикет проекта не читается — проход ничего не заводит (шаг мог
стоять в нём, и дубль был бы лишним).
"""
from __future__ import annotations

import re
from pathlib import Path

import registry as R
import routes
import signals as SIG
import ticket as T

MARK = re.compile(r"^Шаг (\d+) из (\d+) цели (G-\d{3,})\.", re.M)
NOTIFIED = "goal_steps_notified"
STOPPED = "goal_steps_stopped"


def _marked(tickets_dir: Path) -> dict | None:
    """{G-NNN: {номер шага: [тикеты]}} по строке «Шаг N из M цели G-NNN.» в описании; None — какой-то тикет не прочитался."""
    found: dict = {}
    for path in T.list_tickets(tickets_dir):
        try:
            tkt = T.read_ticket(path)
        except Exception:
            return None
        if m := MARK.search(tkt.description or ""):
            found.setdefault(m.group(3), {}).setdefault(int(m.group(1)), []).append(tkt)
    return found


def _step_ticket(tickets_dir: Path, gid: str, step: dict, total: int, prev: str, now) -> Path:
    prev_line = f"\nПредыдущий шаг плана принят: {prev}." if prev else ""
    desc = (f"Ссылка на задачу владельца: {gid}\n\nШаг {step['n']} из {total} цели {gid}.\n"
            f"Готово, когда: {step['done']}{prev_line}")
    return T.create_ticket(tickets_dir, owner=step["role"], title=step["title"], now=now, description=desc,
                           reviewer="" if step["role"] == "judge" else routes.default_reviewer(""))


def advance(root: Path, tickets_dir: Path, state: dict, now) -> list[str]:
    """Один проход по открытым целям с планом. → что сделано: «TK-N: шаг n цели G-NNN» / «G-NNN: следующий не определён»."""
    done: list[str] = []
    marked = None
    for gid, p in R.goals(root).items():
        steps = R.goal_steps(root, gid) if R.goal_header(p).get("status") == "open" else []
        if not steps:
            continue
        marked = _marked(tickets_dir) if marked is None else marked
        if marked is None:
            return done
        by_step, prev, notified = marked.get(gid, {}), "", state.setdefault(NOTIFIED, [])
        for step in steps:
            tickets = by_step.get(step["n"], [])
            if not tickets:
                path = _step_ticket(tickets_dir, gid, step, len(steps), prev, now)
                done.append(f"{path.stem}: шаг {step['n']} цели {gid}")
                break
            accepted = [t for t in tickets if t.status == "done"]
            if not accepted:
                if stopped := [t.id for t in tickets if t.status == "stopped"]:
                    if len(stopped) == len(tickets) and stopped[-1] not in (seen := state.setdefault(STOPPED, [])):
                        seen.append(stopped[-1])
                        SIG.append_ceo_inbox(stopped[-1], "шаг-остановлен",
                                             f"цель {gid}: шаг {step['n']} остановлен, план стоит — реши с владельцем, как продолжить", now)
                        done.append(f"{stopped[-1]}: шаг {step['n']} цели {gid} остановлен")
                break  # шаг в работе — следующий ждёт приёмки
            prev = accepted[-1].id
        else:
            if gid not in notified:
                notified.append(gid)
                SIG.append_ceo_inbox(prev, "следующий-не-определён",
                                     f"цель {gid}: все шаги плана приняты, следующий не определён — предложи владельцу шаги или принять цель", now)
                done.append(f"{gid}: следующий не определён")
    return done
