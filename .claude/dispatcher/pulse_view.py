"""Пульс (В-38), шаг 2; v2 — TK-105 (В-50): чтение `pulse.json` проектов и сводка (шапка, счётчики, итоги дня) для страницы и
закреплённого сообщения Telegram. Только stdlib.

Каждый проект пишет свой `pulse.json` (шаг 1, docs/pulse.md); здесь он читается из файла или по URL, приводится к одному виду
(`Pulse`) и сводится в фразу-итог. Страница `.claude/board/pulse_server.py` и `pulse_tg.py` берут отсюда одно и то же.
"""
from __future__ import annotations

import http.client
import json
import re
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import heartbeat as HB  # noqa: E402
import project as P  # noqa: E402

STALE_S = HB.STALE_S  # pulse.json старше — диспетчер не отвечает; тот же порог, что в pulse.read (шаг 1)
FETCH_TIMEOUT_S = 5.0
CELLS = 96  # 15-минутных клеток в сутках
OK_PHRASE = "Всё идёт. От вас ничего."  # запасная фраза проекта без phrase в файле
OK_TITLE, OK_SUB = "Всё идёт", "От вас ничего не нужно"
OWNER_TITLE = "Нужен ваш ответ"
DAY_KEYS = ("accepted", "returns", "working", "queued")
NO_PROJECTS = "Проектов нет: ключ pulse_projects в rpv.json пуст или rpv.json не читается."
STATES = ("ok", "owner", "stuck")
FILTERS = ("all", "ok", "owner", "stuck")  # переключатель страницы: Все | Идёт | Нужен ответ | Застряло
CLASSES = ("work", "none", "owner", "stuck")
PLAN_MARKS = ("done", "run", "ask", "stuck", "next")  # ✓ принят · ◐ идёт · ? ждёт слова владельца · ! застряло · ○ впереди


def plural(n: int, one: str, few: str, many: str) -> str:
    return one if n % 10 == 1 and n % 100 != 11 else few if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14) else many


def ago(seconds: float) -> str:
    """«меньше минуты» · «23 мин» · «2 ч 10 мин» · «3 дня»; отрицательное — как 0."""
    minutes = max(0, int(seconds // 60))
    if minutes < 1:
        return "меньше минуты"
    if minutes < 60:
        return f"{minutes} мин"
    if minutes < 1440:
        return f"{minutes // 60} ч {minutes % 60} мин" if minutes % 60 else f"{minutes // 60} ч"
    days = minutes // 1440
    return f"{days} {plural(days, 'день', 'дня', 'дней')}"


def read(spec: str, fetch=urllib.request.urlopen):
    """Содержимое pulse.json по пути или URL; не прочиталось или не объект — None."""
    try:
        if str(spec).startswith(("http://", "https://")):
            with fetch(str(spec), timeout=FETCH_TIMEOUT_S) as r:
                data = json.loads(r.read().decode("utf-8-sig"))
        else:
            data = json.loads(Path(spec).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, http.client.HTTPException):  # InvalidURL (порт не число), IncompleteRead — не OSError
        return None
    return data if isinstance(data, dict) else None


def projects_of(root) -> dict:
    """Ключ rpv.json `pulse_projects` — {имя: путь|URL}; нет или не объект — {}."""
    value = P.setting("pulse_projects", {}, root=root)
    return {str(k): str(v) for k, v in value.items() if k and v} if isinstance(value, dict) else {}


def _cells_of(raw: dict) -> list:
    """96 клеток (класс, запусков) из `cells` шага 1: `{i, n, c, why?}`; нет клетки или класс неизвестен — ('none', 0)."""
    out = [("none", 0)] * CELLS
    for item in raw["cells"]:
        i = item.get("i") if isinstance(item, dict) else None
        if isinstance(i, int) and not isinstance(i, bool) and 0 <= i < CELLS:
            n = item.get("n")
            out[i] = (item.get("c") if item.get("c") in CLASSES else "none", n if isinstance(n, int) and n > 0 else 0)
    return out


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def _count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _run(item):
    """Запуск в клетке: {who, verb, ticket, title}; не объект или без тикета — None."""
    if not isinstance(item, dict) or not _text(item.get("ticket")):
        return None
    return {k: _text(item.get(k)) for k in ("who", "verb", "ticket", "title")}


def _cell_extras(raw: dict) -> tuple:
    """(запуски клеток, причины клеток) ×96 из `cells[].runs` / `cells[].why`; версия 1 запусков не пишет — пусто."""
    runs, why = [[] for _ in range(CELLS)], [""] * CELLS
    for item in raw["cells"]:
        i = item.get("i") if isinstance(item, dict) else None
        if isinstance(i, int) and not isinstance(i, bool) and 0 <= i < CELLS:
            listed = item.get("runs") if isinstance(item.get("runs"), list) else []
            runs[i] = [r for r in map(_run, listed) if r]
            why[i] = _text(item.get("why"))
    return runs, why


def _entries(value, *keys) -> list:
    """Список записей {ключи…} из v2-поля; запись без `ticket` или `title` — не запись."""
    items = value if isinstance(value, list) else []
    return [{k: _text(i.get(k)) for k in keys} for i in items if isinstance(i, dict) and _text(i.get("ticket")) and _text(i.get("title"))]


def _goal(value):
    """Цель {id, title, step?, steps?}; шаг — только пара 1 ≤ N ≤ M, иначе без шага и полоски."""
    title = _text(value.get("title")) if isinstance(value, dict) else ""
    if not title:
        return None
    out = {"id": _text(value.get("id")), "title": title}
    step, steps = _count(value.get("step")), _count(value.get("steps"))
    if step and steps and step <= steps:
        out.update(step=step, steps=steps)
    return out


def _plan(value, stale: bool = False) -> list:
    """План цели [{mark, n, title, ticket, who, at, since, idle, level, waiting}]; строка без названия или с чужой отметкой — не строка.
    Файл устарел — времена («идёт», «движение», «ждёт») убраны: они о прошлом."""
    out = []
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict) or item.get("mark") not in PLAN_MARKS or not _text(item.get("title")):
            continue
        row = {k: _text(item.get(k)) for k in ("title", "ticket", "who", "at", "since", "idle", "waiting")}
        row.update(mark=item["mark"], n=_count(item.get("n")) or 0, level=item.get("level") if item.get("level") in ("warn", "bad") else "")
        if stale:
            row.update(since="", idle="", waiting="", level="")
        out.append(row)
    return out


def _job(value):
    ticket = _text(value.get("ticket")) if isinstance(value, dict) else ""
    return {k: _text(value.get(k)) for k in ("who", "verb", "ticket", "title", "since")} if ticket else None


def _tail(phrase: str) -> str:
    """Подробность из фразы v1: после « — » (или «: »), без «(и ещё N)» и точки на конце."""
    tail = phrase.split(" — ", 1)[-1] if " — " in phrase else phrase.split(": ", 1)[-1]
    return re.sub(r"\s*\(и ещё \d+\)\.?$", "", tail).rstrip(".").strip()


def _ask(value, raw: dict, phrase: str) -> dict:
    """Вопрос владельцу {ticket, question, link}: v2 `ask`, иначе первая запись `owner` (v1: тикет и действие), иначе хвост фразы."""
    if isinstance(value, dict) and _text(value.get("question")):
        return {"ticket": _text(value.get("ticket")), "question": _text(value["question"]), "link": _text(value.get("link"))}
    owners = raw.get("owner") if isinstance(raw.get("owner"), list) else []
    first = next((o for o in owners if isinstance(o, dict) and _text(o.get("ticket"))), None)
    if first:
        return {"ticket": _text(first["ticket"]), "question": _text(first.get("action")) or "нужен ваш ответ", "link": ""}
    return {"ticket": "", "question": _tail(phrase) or "нужен ваш ответ", "link": ""}


def _fix(value, phrase: str) -> dict:
    """Причина «застряло» {text, who}: v2 `fix`, иначе хвост фразы (v1) и «разбирает CEO»."""
    if isinstance(value, dict) and _text(value.get("text")):
        return {"text": _text(value["text"]), "who": _text(value.get("who")) or "разбирает CEO"}
    return {"text": _tail(phrase) or "причина не названа", "who": "разбирает CEO"}


def _day(value) -> dict:
    return {k: n for k in DAY_KEYS if isinstance(value, dict) and (n := _count(value.get(k))) is not None}


def _slot(moment: datetime) -> int:
    return min(CELLS - 1, (moment.hour * 60 + moment.minute) // 15)


def _unreadable(name: str, now: datetime) -> dict:
    return {"name": name, "state": "stuck", "phrase": f"Застряло: {name} — пульс не читается.", "now": "", "pause": "",
            "cells": [("none", 0)] * CELLS, "runs": [[] for _ in range(CELLS)], "why": [""] * CELLS, "idx": _slot(now), "age_s": None,
            "zone": None, "goal": None, "job": None, "ask": None, "fix": {"text": "пульс не читается", "who": "разбирает CEO"},
            "done_today": [], "queue": [], "day": {}, "plan": []}


def normalise(name: str, raw, now: datetime) -> dict:
    """Один вид проекта: name, state, phrase, now, pause, cells [(класс, запусков)] ×96, idx (текущая клетка), age_s.
    Формат — вывод `pulse.build` (state, phrase, now, pause, cells, generated; версия 2 — ещё goal, job, ask, fix, done_today, queue,
    day, plan, cells[].runs; версия 1 их не имеет — карточка без них). Нет файла, `state` не из списка, `cells` не список или
    `generated` не время — «застряло: пульс не читается»; `generated` старше STALE_S — «застряло: диспетчер не отвечает», клетки после
    записи — stuck, прежние «сейчас» и пауза убраны. Сутки и клетка — по поясу `generated` (поясу проекта), не сервера."""
    stamp = P.parse_time(raw.get("generated"), strict=False) if isinstance(raw, dict) and isinstance(raw.get("generated"), str) else None
    if stamp is None or stamp.tzinfo is None or raw.get("state") not in STATES or not isinstance(raw.get("cells"), list):
        return _unreadable(name, now)
    here = now.astimezone(stamp.tzinfo)
    age = max(0, int((now - stamp).total_seconds()))
    phrase = str(raw.get("phrase") or OK_PHRASE)
    runs, why = _cell_extras(raw)
    view = {"name": name, "state": raw["state"], "phrase": phrase, "now": str(raw.get("now") or ""),
            "pause": str(raw.get("pause") or ""), "cells": _cells_of(raw), "runs": runs, "why": why, "idx": _slot(stamp), "age_s": age,
            "zone": stamp.tzinfo, "goal": _goal(raw.get("goal")), "job": _job(raw.get("job")), "ask": _ask(raw.get("ask"), raw, phrase),
            "fix": _fix(raw.get("fix"), phrase), "done_today": _entries(raw.get("done_today"), "time", "ticket", "title"),
            "queue": _entries(raw.get("queue"), "ticket", "title"), "day": _day(raw.get("day")),
            "plan": _plan(raw.get("plan"), age > STALE_S)}
    if age > STALE_S:
        same_day = stamp.date() == here.date()
        first = view["idx"] + 1 if same_day else 0
        if not same_day:
            view.update(cells=[("none", 0)] * CELLS, runs=[[] for _ in range(CELLS)], why=[""] * CELLS)
        silent = f"диспетчер не отвечает {ago(age)}"
        view.update(state="stuck", phrase=f"Застряло: {name} — {silent}.", now="", pause="", idx=_slot(here), job=None,
                    fix={"text": silent, "who": "перезапускает CEO"})
        for i in range(first, view["idx"] + 1):
            view["cells"][i] = ("stuck", 0)
    return view


def load_all(root, now: datetime | None = None, fetch=urllib.request.urlopen) -> list:
    now = now or datetime.now().astimezone()
    return [normalise(name, read(spec, fetch), now) for name, spec in projects_of(root).items()]


def ask_text(view: dict) -> str:
    ask = view["ask"] or {}
    return f"{ask['ticket']}: {ask['question']}" if ask.get("ticket") else ask.get("question", "")


def header(views: list) -> dict:
    """Шапка страницы и Telegram: {kind, title, sub}. Застряло важнее «нужен ответ», иначе «Всё идёт»; про остальные проекты — число в
    переключателе (`counts`), в шапке «(и ещё N)» нет. Проектов нет — не «Всё идёт»."""
    if not views:
        return {"kind": "stuck", "title": NO_PROJECTS, "sub": ""}
    stuck = next((v for v in views if v["state"] == "stuck"), None)
    if stuck:
        return {"kind": "stuck", "title": f"Застряло: {stuck['name']}", "sub": f"{stuck['fix']['text']} — {stuck['fix']['who']}"}
    owner = next((v for v in views if v["state"] == "owner"), None)
    if owner:
        return {"kind": "owner", "title": OWNER_TITLE, "sub": f"{owner['name']} · {ask_text(owner)}"}
    return {"kind": "ok", "title": OK_TITLE, "sub": OK_SUB}


def counts(views: list) -> dict:
    """Числа переключателя: {all, ok, owner, stuck} — проекты по состоянию."""
    return {"all": len(views), **{s: sum(v["state"] == s for v in views) for s in STATES}}


def totals(views: list) -> dict:
    """Итоги дня по всем проектам: принято, возвратов, в работе, в очереди; проект без `day` (версия 1) не прибавляет."""
    return {k: sum(v["day"].get(k, 0) for v in views) for k in DAY_KEYS}


def tg_line(view: dict) -> str:
    """Строка проекта для Telegram: «имя — идёт» / «имя — нужны вы: TK-N: вопрос» / «имя — застряло: причина»."""
    if view["state"] == "ok":
        return f"{view['name']} — идёт"
    if view["state"] == "owner":
        return f"{view['name']} — нужны вы: {ask_text(view)}"
    return f"{view['name']} — застряло: {view['fix']['text']}"


def tg_text(views: list) -> str:
    """Те же две строки шапки, что на странице, и строка на проект."""
    head = header(views)
    return "\n".join([head["title"]] + ([head["sub"]] if head["sub"] else []) + [tg_line(v) for v in views])
