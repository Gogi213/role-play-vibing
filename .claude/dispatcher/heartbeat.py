"""Сердцебиение службы (state.json / watch-heartbeat.json / ci-heartbeat.json): порог «устарело» и возраст — одни на
supervise, doctor и хук подсказок (TK-010; раньше 600 с у сторожа служб и 240 с у хука — два ответа на один вопрос).
Модуль без импортов диспетчера: его читает и хук сессии."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

STALE_S = 600.0


def age_of(stamp, now: float) -> float:
    """Возраст отметки времени (ISO, `Z` или со смещением) в секундах на момент `now` (epoch); непонятная — ValueError."""
    return now - datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()


def read_age(path: Path, field: str, now: float) -> float | None:
    """Возраст сердцебиения в секундах; None — файла/поля нет или он нечитаем."""
    try:
        return age_of(json.loads(Path(path).read_text(encoding="utf-8"))[field], now)
    except (OSError, ValueError, KeyError, TypeError):
        return None
