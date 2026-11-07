"""Отметка «прочитано» очереди CEO (`ceo-inbox.md`): одна на хук подсказки и на `tickets.py inbox` (TK-010).

Отметка — число уже показанных строк и (TK-112) время показа: `220 2026-11-07T19:00:00+04:00`; прежний файл из одного числа читается.
Двигает её только показ строк CEO (`tickets.py inbox`); хук лишь считает непрочитанные.
Модуль без импортов диспетчера: его читает и хук сессии."""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

SEEN_NAME = ".ceo-inbox-seen"
_ISO = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")  # разбор времени — у читателя (`project.parse_time`): модуль без импортов диспетчера
FAIL_KIND = "ceo-не-разбужен"  # вид строки очереди, когда побудка CEO не дошла (пишет ceo_wake._fail)
ABSENT_WHY = "сессия CEO не найдена в ListAgents"  # причина: сессии CEO нет вовсе — не простой, будить некого


def read(inbox: Path, seen_f: Path) -> tuple[list[str], int]:
    """(строки очереди, сколько уже показано). Нет файла — пусто; отметка больше длины (файл усечён) — с начала."""
    try:
        lines = Path(inbox).read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    seen = _read_seen(seen_f)[0]
    return lines, (0 if seen > len(lines) else seen)


def _read_seen(seen_f: Path) -> tuple[int, str]:
    """(сколько показано, время показа ISO или ""). Нет файла, мусор в числе — (0, ""); мусор во времени — время пусто."""
    try:
        parts = Path(seen_f).read_text(encoding="utf-8").split()
    except OSError:
        return 0, ""
    try:
        seen = int(parts[0]) if parts else 0
    except ValueError:
        return 0, ""
    at = parts[1] if len(parts) > 1 else ""
    return seen, (at if _ISO.match(at) else "")


def read_at(seen_f: Path) -> str:
    """Когда CEO последний раз прочитал очередь (ISO); неизвестно (файла нет, старый формат) — пустая строка."""
    return _read_seen(seen_f)[1]


def mark(seen_f: Path, shown: int, now=None) -> None:
    at = (now or datetime.now().astimezone()).isoformat(timespec="seconds")
    Path(seen_f).write_text(f"{shown} {at}", encoding="utf-8")
