"""Корень проекта и переменные окружения диспетчера. Только stdlib.

Диспетчер, сторож и tickets.py запускаются прямо из папки плагина и работают с проектом, а не с
каталогом, где лежит сам файл. Корень проекта: `--project <путь>` в аргументах, иначе `RPV_PROJECT`, иначе
`CLAUDE_PROJECT_DIR`, иначе ближайший каталог вверх от текущего, где есть `.claude/roles` (после `/rpv-init`);
не нашли — `ProjectNotFound` (скрипты печатают подсказку и выходят, ничего не создавая). Состояние
(state.json, логи, pid, ceo-inbox, ceo-wake.log) пишется в `<проект>/.claude/dispatcher/`, тикеты читаются
из `<проект>/.claude/tickets/`.

Окружение — только то, что принадлежит машине (`RPV_PROJECT`, токен и чат Telegram, `RPV_PLUGIN_DIR`); настройки проекта —
ключи файла `<проект>/.claude/rpv.json` (`setting`), остальное — константы в коде (CP8, `docs/configuration.md`).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


NOT_FOUND_HINT = ("проект не найден: от текущего каталога вверх нет каталога с `.claude/roles`. "
                  "Укажите `--project <путь>` (или RPV_PROJECT) либо выполните `/rpv-init` в корне проекта.")


class ProjectNotFound(Exception):
    """Корень проекта не задан и не найден поиском вверх от текущего каталога."""


def utf8_stdio() -> None:
    """stdout/stderr — utf-8: на Windows с не-кириллической локалью (cp1252) кириллица иначе роняет печать в трубу."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


def env(name: str, default=None):
    """Значение `RPV_<name>`, иначе `default` (пустая строка — как «не задано»)."""
    return os.environ.get("RPV_" + name) or default


SETTINGS_NAME = "rpv.json"
_ROOT: Path | None = None
_CACHE: dict = {}


def set_root(root) -> None:
    """Корень проекта для `setting` — ставит `dispatch.configure_project`."""
    global _ROOT
    _ROOT = Path(root) if root else None


def settings(root=None) -> dict:
    """Настройки проекта `<проект>/.claude/rpv.json` (объект JSON); проект — `root`, иначе заданный `set_root`. Нет
    файла — {}; не читается или не объект — {} и строка в stderr один раз на версию файла (страж удаления читает его сам
    и при порче отказывает — fail-closed). Перечитывается по времени изменения: правка CEO действует без перезапуска."""
    root = Path(root) if root else _ROOT
    if root is None:
        return {}
    path = root / ".claude" / SETTINGS_NAME
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        return {}
    hit = _CACHE.get(str(path))
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError("корень — не объект JSON")
    except (OSError, ValueError) as e:
        print(f"[rpv] {path}: не прочитан ({type(e).__name__}: {e}) — настройки проекта по умолчанию", file=sys.stderr)
        data = {}
    _CACHE[str(path)] = (mtime, data)
    return data


def setting(key: str, default=None, root=None):
    """Ключ `.claude/rpv.json`; нет ключа, `null` или пустая строка — `default`."""
    value = (settings(root) if root else settings()).get(key)
    return default if value is None or value == "" else value


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
