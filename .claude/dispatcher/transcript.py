"""Токены хода из транскрипта сессии claude: одно определение «контекста хода» для диспетчера и хука (TK-010).

Контекст хода — вход + чтение кэша + запись кэша (вывод в него не входит, как в `ctx_last`/`ctx_sum` runs.log);
реплики подагентов (`isSidechain`) — не контекст самой сессии, пропускаются. Модуль без импортов диспетчера: его читает и хук."""
from __future__ import annotations

import json
import re
from pathlib import Path

CONTEXT_KEYS = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
TAIL_BYTES = 600_000
_SESSION_ID = re.compile(r"[A-Za-z0-9_-]+")


def context_of(usage) -> int:
    usage = usage or {}
    return sum(int(usage.get(k) or 0) for k in CONTEXT_KEYS)


def find(projects_dir, session_id) -> Path | None:
    """Файл транскрипта сессии в `projects/*/<session_id>.jsonl`; нет или id странный — None."""
    if not session_id or not _SESSION_ID.fullmatch(str(session_id)):
        return None
    return next(Path(projects_dir).glob(f"*/{session_id}.jsonl"), None)


def last_turn_tokens(path) -> int:
    """Контекст последнего хода модели по хвосту транскрипта; 0 — файла или хода с usage нет."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            start = max(0, fh.tell() - TAIL_BYTES)
            fh.seek(start)
            lines = fh.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return 0
    if start:
        lines = lines[1:]  # первая строка хвоста может быть оборвана
    for line in reversed(lines):
        if '"usage"' not in line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict) or row.get("isSidechain"):
            continue
        message = row.get("message")
        try:
            n = context_of(message.get("usage") if isinstance(message, dict) else None)
        except (TypeError, ValueError, AttributeError):
            continue
        if n:
            return n
    return 0
