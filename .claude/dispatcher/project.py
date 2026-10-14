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


ROLES = ("researcher", "engineer", "judge")


def _parent_map() -> dict:
    """{pid: ppid} всех процессов, только stdlib: Windows — снимок Toolhelp32, Linux — /proc, иначе `ps`. Сбой — {}."""
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes as w

            class PE(ctypes.Structure):
                _fields_ = [("dwSize", w.DWORD), ("cntUsage", w.DWORD), ("th32ProcessID", w.DWORD),
                            ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", w.DWORD), ("cntThreads", w.DWORD),
                            ("th32ParentProcessID", w.DWORD), ("pcPriClassBase", ctypes.c_long), ("dwFlags", w.DWORD),
                            ("szExeFile", ctypes.c_wchar * 260)]
            k = ctypes.windll.kernel32
            k.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
            snap = k.CreateToolhelp32Snapshot(0x2, 0)
            if not snap or snap == ctypes.c_void_p(-1).value:
                return {}
            out, pe = {}, PE()
            pe.dwSize = ctypes.sizeof(PE)
            try:
                ok = k.Process32FirstW(ctypes.c_void_p(snap), ctypes.byref(pe))
                while ok:
                    out[int(pe.th32ProcessID)] = int(pe.th32ParentProcessID)
                    ok = k.Process32NextW(ctypes.c_void_p(snap), ctypes.byref(pe))
            finally:
                k.CloseHandle(ctypes.c_void_p(snap))
            return out
        except Exception:
            return {}
    out = {}
    if os.path.isdir("/proc"):
        for d in os.listdir("/proc"):
            if d.isdigit():
                try:
                    with open(f"/proc/{d}/stat", encoding="utf-8", errors="replace") as f:
                        out[int(d)] = int(f.read().rsplit(")", 1)[1].split()[1])
                except (OSError, IndexError, ValueError):
                    continue
        if out:
            return out
    try:
        import subprocess
        r = subprocess.run(["ps", "-A", "-o", "pid=,ppid="], capture_output=True, text=True, timeout=10)
        for ln in r.stdout.splitlines():
            f = ln.split()
            if len(f) == 2 and f[0].isdigit() and f[1].isdigit():
                out[int(f[0])] = int(f[1])
    except (OSError, ValueError):
        pass
    return out


def ancestor_pids(pid: int | None = None, parents: dict | None = None) -> list:
    """Предки процесса (родитель, его родитель, …) по карте {pid: ppid}; цикл и глубина ограничены."""
    parents = _parent_map() if parents is None else parents
    out, cur = [], os.getpid() if pid is None else pid
    for _ in range(64):
        cur = parents.get(cur)
        if not cur or cur in out:
            break
        out.append(cur)
    return out


def caller_role(root=None, ancestors=None) -> str:
    """Роль вызывающего процесса — сначала по записи диспетчера: процесс внутри запуска роли (предок — pid из
    `state.json` → `active_runs` проекта; файл закрыт ролям стражем) — это роль того запуска, что бы ни стояло в
    окружении: ни стёртое, ни подменённое `RPV_ROLE` (`RPV_ROLE=judge …`) её не меняет — притвориться CEO или другой ролью
    запуск роли не может (слово владельца 14.10). Вне запусков — `RPV_ROLE` (тесты, ручной запуск); нет — '' (CEO, владелец)."""
    env = (os.environ.get("RPV_ROLE") or "").strip().lower()
    root = Path(root) if root else _ROOT
    if root is None:
        try:
            root = resolve_project()
        except ProjectNotFound:
            return env
    try:
        runs = json.loads((Path(root) / ".claude" / "dispatcher" / "state.json").read_text(encoding="utf-8"))
        runs = runs.get("active_runs") or {}
    except (OSError, ValueError, AttributeError):
        return env
    by_pid = {int(r["pid"]): str(r.get("role") or "") for r in runs.values() if isinstance(r, dict) and r.get("pid")}
    if by_pid:
        for pid in (ancestor_pids() if ancestors is None else ancestors):
            if pid in by_pid:
                return by_pid[pid].strip().lower() or "engineer"
    return env


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
