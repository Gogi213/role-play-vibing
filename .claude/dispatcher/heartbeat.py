"""Сердцебиение службы (state.json / watch-heartbeat.json / ci-heartbeat.json): порог «устарело» и возраст — одни на
supervise, doctor и хук подсказок (TK-010; раньше 600 с у сторожа служб и 240 с у хука — два ответа на один вопрос).
Из диспетчера импортирует только project (стандартная библиотека): модуль читает и хук сессии."""
from __future__ import annotations

import json
from pathlib import Path

import project as P

STALE_S = 600.0


def age_of(stamp, now: float) -> float:
    """Возраст отметки времени (ISO, `Z` или со смещением) в секундах на момент `now` (epoch); непонятная — ValueError."""
    return now - P.parse_time(stamp, strict=True).timestamp()


def read_age(path: Path, field: str, now: float) -> float | None:
    """Возраст сердцебиения в секундах; None — файла/поля нет или он нечитаем."""
    try:
        return age_of(json.loads(Path(path).read_text(encoding="utf-8"))[field], now)
    except (OSError, ValueError, KeyError, TypeError):
        return None
