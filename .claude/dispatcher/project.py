"""Корень проекта и переменные окружения диспетчера. Только stdlib.

Диспетчер, сторож и tickets.py запускаются прямо из папки плагина и работают с проектом, а не с
каталогом, где лежит сам файл. Корень проекта: `--project <путь>` в аргументах, иначе `RPV_PROJECT`, иначе
`CLAUDE_PROJECT_DIR`, иначе ближайший каталог вверх от текущего, где есть `.claude/roles` (после `/rpv-init`);
не нашли — `ProjectNotFound` (скрипты печатают подсказку и выходят, ничего не создавая). Состояние
(state.json, логи, pid, ceo-inbox, ceo-wake.log) пишется в `<проект>/.claude/dispatcher/`, тикеты читаются
из `<проект>/.claude/tickets/`.

Переменные: `RPV_<имя>`; при отсутствии берётся прежнее `ALPHA_<имя>` (совместимость).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ENV_PREFIXES = ("RPV_", "ALPHA_")  # новое имя первым, прежнее — запасное

NOT_FOUND_HINT = ("проект не найден: от текущего каталога вверх нет каталога с `.claude/roles`. "
                  "Укажите `--project <путь>` (или RPV_PROJECT) либо выполните `/rpv-init` в корне проекта.")


class ProjectNotFound(Exception):
    """Корень проекта не задан и не найден поиском вверх от текущего каталога."""


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


def find_project_root(start=None) -> Path | None:
    """Ближайший каталог вверх от `start` (по умолчанию — текущий), где есть `.claude/roles`; нет — None."""
    here = Path(start).expanduser().resolve() if start else Path.cwd().resolve()
    for d in (here, *here.parents):
        if (d / ".claude" / "roles").is_dir():
            return d
    return None


def resolve_project(argv=None) -> Path:
    """Корень проекта: --project, RPV_PROJECT, CLAUDE_PROJECT_DIR, иначе поиск вверх от текущего каталога
    (не расположение файла). Нигде не нашли — `ProjectNotFound`; каталоги не создаются."""
    argv = sys.argv[1:] if argv is None else argv
    chosen = project_arg(argv) or os.environ.get("RPV_PROJECT") or os.environ.get("CLAUDE_PROJECT_DIR")
    if chosen:
        return Path(chosen).expanduser().resolve()
    found = find_project_root()
    if found is None:
        raise ProjectNotFound(NOT_FOUND_HINT)
    return found


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
