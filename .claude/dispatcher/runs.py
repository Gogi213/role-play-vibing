"""Строка runs.log (формат пишет dispatch): одно место разбора для overseer, report, usage и watch (TK-010).

`<время> <тикет> <роль> ключ=значение ...` — не короче 4 слов; `on_met` вместо роли — команда проекта, не запуск модели.
Цена запуска — `resolved_cost` (начислено именно этому запуску), иначе `cost_usd` (итог сессии: при `--resume` считал бы дважды);
не число — 0.0. Модуль без импортов диспетчера."""
from __future__ import annotations

import re

KV = re.compile(r"(\w+)=(\S+)")
ON_MET = "on_met"


def split(line: str):
    """(время, тикет, роль, {ключ: значение}) или None, если слов меньше четырёх."""
    parts = line.split()
    if len(parts) < 4:
        return None
    return parts[0], parts[1], parts[2], dict(KV.findall(line))


def num(text):
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def costs(kv: dict) -> tuple:
    """(цена запуска, cost_usd как записан): разошедшиеся 0/битое `resolved_cost` → `cost_usd`, нет и его → 0.0."""
    raw = num(kv.get("cost_usd")) or 0.0
    cost = num(kv.get("resolved_cost"))
    return (raw if cost is None else cost), raw
