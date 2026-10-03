"""Корень проекта и переменные окружения диспетчера. Только stdlib.

Диспетчер, сторож и tickets.py запускаются прямо из папки плагина и работают с проектом, а не с
каталогом, где лежит сам файл. Корень проекта: `--project <путь>` в аргументах, иначе `RPV_PROJECT`, иначе
`CLAUDE_PROJECT_DIR`, иначе текущий каталог. Состояние (state.json, логи, pid, ceo-inbox, ceo-wake.log)
пишется в `<проект>/.claude/dispatcher/`, тикеты читаются из `<проект>/.claude/tickets/`.

Переменные: `RPV_<имя>`; при отсутствии берётся прежнее `ALPHA_<имя>` (совместимость).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ENV_PREFIXES = ("RPV_", "ALPHA_")  # новое имя первым, прежнее — запасное


def env(name: str, default=None):
    """Значение `RPV_<name>`, иначе `ALPHA_<name>`, иначе `default` (пустая строка — как «не задано»)."""
    for prefix in ENV_PREFIXES:
        value = os.environ.get(prefix + name)
        if value:
            return value
    return default


def project_arg(argv) -> str | None:
    """Значение `--project <путь>` / `--project=<путь>` из аргументов (None — флага нет)."""
    argv = list(argv)
    for i, arg in enumerate(argv):
        if arg == "--project":
            return argv[i + 1] if i + 1 < len(argv) else None
        if arg.startswith("--project="):
            return arg.split("=", 1)[1]
    return None


def resolve_project(argv=None) -> Path:
    """Корень проекта: --project, RPV_PROJECT, CLAUDE_PROJECT_DIR, текущий каталог (не расположение файла)."""
    argv = sys.argv[1:] if argv is None else argv
    chosen = project_arg(argv) or os.environ.get("RPV_PROJECT") or os.environ.get("CLAUDE_PROJECT_DIR")
    return Path(chosen).expanduser().resolve() if chosen else Path.cwd().resolve()


def strip_project_arg(argv) -> list:
    """Аргументы без `--project <путь>` (для разборщиков, которым флаг не нужен)."""
    out, skip = [], False
    for arg in argv:
        if skip:
            skip = False
        elif arg == "--project":
            skip = True
        elif not arg.startswith("--project="):
            out.append(arg)
    return out
