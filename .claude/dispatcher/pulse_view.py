"""Пульс (В-38), шаг 2: чтение `pulse.json` проектов и сводка для страницы и закреплённого сообщения Telegram. Только stdlib.

Каждый проект пишет свой `pulse.json` (шаг 1, docs/pulse.md); здесь он читается из файла или по URL, приводится к одному виду
(`Pulse`) и сводится в фразу-итог. Страница `.claude/board/pulse_server.py` и `pulse_tg.py` берут отсюда одно и то же.
"""
from __future__ import annotations

import json
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import project as P  # noqa: E402

STALE_S = 120  # pulse.json старше — диспетчер не отвечает (порог dispatcher_dead шага 1)
FETCH_TIMEOUT_S = 5.0
CELLS = 96  # 15-минутных клеток в сутках
OK_PHRASE = "Всё идёт. От вас ничего."
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
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def projects_of(root) -> dict:
    """Ключ rpv.json `pulse_projects` — {имя: путь|URL}; нет или не объект — {}."""
    value = P.setting("pulse_projects", {}, root=root)
    return {str(k): str(v) for k, v in value.items() if k and v} if isinstance(value, dict) else {}


def _cell(item) -> tuple:
    cls = item.get("cls") if isinstance(item, dict) else item
    runs = item.get("runs", 0) if isinstance(item, dict) else 0
    return (cls if cls in CLASSES else "none", runs if isinstance(runs, int) and runs > 0 else 0)


def _state(raw: dict, phrase: str) -> str:
    if raw.get("state") in STATES:
        return raw["state"]
    return "owner" if phrase.startswith("Нужны вы") else "stuck" if phrase.startswith("Застряло") else "ok"


def _slot(moment: datetime) -> int:
    return min(CELLS - 1, (moment.hour * 60 + moment.minute) // 15)


def normalise(name: str, raw, now: datetime) -> dict:
    """Один вид проекта: name, state, phrase, now, pause, cells [(класс, запусков)] ×96, idx (текущая клетка), age_s.
    Файла нет или он старше STALE_S — «застряло: диспетчер не отвечает», клетки после последней записи — stuck."""
    if raw is None:
        return {"name": name, "state": "stuck", "phrase": f"Застряло: {name} — пульс не читается.", "now": "", "pause": "",
                "cells": [("none", 0)] * CELLS, "idx": _slot(now), "age_s": None}
    cells = [_cell(c) for c in (raw.get("cells") or [])][:CELLS]
    cells += [("none", 0)] * (CELLS - len(cells))
    phrase = str(raw.get("phrase") or OK_PHRASE)
    stamp = P.parse_time(raw.get("ts"), strict=False) if raw.get("ts") else None
    view = {"name": name, "state": _state(raw, phrase), "phrase": phrase, "now": str(raw.get("now") or ""),
            "pause": str(raw.get("pause") or ""), "cells": cells, "idx": _slot(now), "age_s": None}
    if stamp is None:
        return view
    stamp = stamp.astimezone(now.tzinfo) if stamp.tzinfo else stamp
    age = max(0, int((now - stamp).total_seconds())) if stamp.tzinfo else 0
    view["age_s"], view["idx"] = age, _slot(stamp)
    if age > STALE_S:
        view.update(state="stuck", phrase=f"Застряло: {name} — диспетчер не отвечает {max(1, age // 60)} мин.")
        for i in range(view["idx"] + 1, _slot(now) + 1):
            cells[i] = ("stuck", 0)
        view["idx"] = _slot(now)
    return view


def load_all(root, now: datetime | None = None, fetch=urllib.request.urlopen) -> list:
    now = now or datetime.now().astimezone()
    return [normalise(name, read(spec, fetch), now) for name, spec in projects_of(root).items()]


def summary(views: list) -> str:
    """Фраза-итог: застряло важнее «нужны вы», иначе «Всё идёт»; несколько — первая и счёт остальных."""
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
