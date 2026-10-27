"""Отметка «прочитано» очереди CEO (`ceo-inbox.md`): одна на хук подсказки и на `tickets.py inbox` (TK-010).

Отметка — число уже показанных строк. Двигает её только показ строк CEO (`tickets.py inbox`); хук лишь считает непрочитанные.
Модуль без импортов диспетчера: его читает и хук сессии."""
from __future__ import annotations

from pathlib import Path

SEEN_NAME = ".ceo-inbox-seen"


def read(inbox: Path, seen_f: Path) -> tuple[list[str], int]:
    """(строки очереди, сколько уже показано). Нет файла — пусто; отметка больше длины (файл усечён) — с начала."""
    try:
        lines = Path(inbox).read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    try:
        seen = int(Path(seen_f).read_text(encoding="utf-8").strip() or 0)
    except (OSError, ValueError):
        seen = 0
    return lines, (0 if seen > len(lines) else seen)


def mark(seen_f: Path, shown: int) -> None:
    Path(seen_f).write_text(str(shown), encoding="utf-8")
