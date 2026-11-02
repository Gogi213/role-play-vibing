"""Пульс (В-38), шаг 2: чтение `pulse.json` проектов и сводка для страницы и закреплённого сообщения Telegram. Только stdlib.

Каждый проект пишет свой `pulse.json` (шаг 1, docs/pulse.md); здесь он читается из файла или по URL, приводится к одному виду
(`Pulse`) и сводится в фразу-итог. Страница `.claude/board/pulse_server.py` и `pulse_tg.py` берут отсюда одно и то же.
"""
from __future__ import annotations

import http.client
import json
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
OK_PHRASE = "Всё идёт. От вас ничего."
NO_PROJECTS = "Проектов нет: ключ pulse_projects в rpv.json пуст или rpv.json не читается."
STATES = ("ok", "owner", "stuck")
CLASSES = ("work", "none", "owner", "stuck")


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


def _slot(moment: datetime) -> int:
    return min(CELLS - 1, (moment.hour * 60 + moment.minute) // 15)


def _unreadable(name: str, now: datetime) -> dict:
    return {"name": name, "state": "stuck", "phrase": f"Застряло: {name} — пульс не читается.", "now": "", "pause": "",
            "cells": [("none", 0)] * CELLS, "idx": _slot(now), "age_s": None, "zone": None}


def normalise(name: str, raw, now: datetime) -> dict:
    """Один вид проекта: name, state, phrase, now, pause, cells [(класс, запусков)] ×96, idx (текущая клетка), age_s.
    Формат — вывод `pulse.build` (state, phrase, now, pause, cells, generated). Нет файла, `state` не из списка, `cells` не список или
    `generated` не время — «застряло: пульс не читается»; `generated` старше STALE_S — «застряло: диспетчер не отвечает», клетки после
    записи — stuck, прежние «сейчас» и пауза убраны. Сутки и клетка — по поясу `generated` (поясу проекта), не сервера."""
    stamp = P.parse_time(raw.get("generated"), strict=False) if isinstance(raw, dict) and isinstance(raw.get("generated"), str) else None
    if stamp is None or stamp.tzinfo is None or raw.get("state") not in STATES or not isinstance(raw.get("cells"), list):
        return _unreadable(name, now)
    here = now.astimezone(stamp.tzinfo)
    age = max(0, int((now - stamp).total_seconds()))
    view = {"name": name, "state": raw["state"], "phrase": str(raw.get("phrase") or OK_PHRASE), "now": str(raw.get("now") or ""),
            "pause": str(raw.get("pause") or ""), "cells": _cells_of(raw), "idx": _slot(stamp), "age_s": age, "zone": stamp.tzinfo}
    if age > STALE_S:
        same_day = stamp.date() == here.date()
        first = view["idx"] + 1 if same_day else 0
        if not same_day:
            view["cells"] = [("none", 0)] * CELLS
        view.update(state="stuck", phrase=f"Застряло: {name} — диспетчер не отвечает {max(1, age // 60)} мин.", now="", pause="",
                    idx=_slot(here))
        for i in range(first, view["idx"] + 1):
            view["cells"][i] = ("stuck", 0)
    return view


def load_all(root, now: datetime | None = None, fetch=urllib.request.urlopen) -> list:
    now = now or datetime.now().astimezone()
    return [normalise(name, read(spec, fetch), now) for name, spec in projects_of(root).items()]


def summary(views: list) -> str:
    """Фраза-итог: застряло важнее «нужны вы», иначе «Всё идёт»; несколько — первая и счёт остальных. Проектов нет — не «Всё идёт»."""
    if not views:
        return NO_PROJECTS
    bad = [v for v in views if v["state"] == "stuck"] + [v for v in views if v["state"] == "owner"]
    if not bad:
        return OK_PHRASE
    extra = f" (и ещё {len(bad) - 1})" if len(bad) > 1 else ""
    return bad[0]["phrase"] + extra


def tg_line(view: dict) -> str:
    """Строка проекта для Telegram: «имя — идёт» / «имя — нужны вы: …» / «имя — застряло: …»."""
    if view["state"] == "ok":
        return f"{view['name']} — идёт"
    tail = view["phrase"].split(" — ", 1)[-1] if " — " in view["phrase"] else view["phrase"].split(": ", 1)[-1]
    return f"{view['name']} — {'нужны вы' if view['state'] == 'owner' else 'застряло'}: {tail.rstrip('.')}"


def tg_text(views: list) -> str:
    return "\n".join([summary(views)] + [tg_line(v) for v in views])
