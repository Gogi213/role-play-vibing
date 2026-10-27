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
import re
import sys
import threading
import time
from datetime import datetime
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
    """Корень проекта для `setting` — ставит `project_paths.configure_project`."""
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


SEMVER = re.compile(r"\d+\.\d+\.\d+")  # версия плагина (release, report)
ROLES = ("researcher", "engineer", "judge")  # роли, которых запускает диспетчер; единственный список (ceo — человек/CEO-сессия)


def now_iso(now: datetime | None = None) -> str:
    """Время с поясом машины, до секунды: единственный формат отметок времени в журналах и состоянии."""
    return (now or datetime.now().astimezone()).isoformat(timespec="seconds")


def parse_time(text, naive_tz=None, *, strict: bool = False):
    """ISO-время → datetime с поясом (`Z` = UTC). Без пояса: `naive_tz`, а `None` — местное время машины.
    Не разобрать — None; `strict=True` — ValueError."""
    try:
        dt = datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except ValueError:
        if strict:
            raise
        return None
    if dt.tzinfo is not None:
        return dt
    return dt.astimezone() if naive_tz is None else dt.replace(tzinfo=naive_tz)


PROC_SCAN_TIMEOUT_S = 30.0   # один предел на все платформы; прежние 10/20/30/60 с у трёх сканеров разошлись без причины
_CIM_ROWS = ("Get-CimInstance Win32_Process | ForEach-Object "
             "{ \"$($_.ProcessId)`t$($_.ParentProcessId)`t$($_.CommandLine)\" }")


def _toolhelp_rows() -> list:
    """Windows без командных строк: снимок Toolhelp32 (замер: 0,03 с против 0,84 с у CIM)."""
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
        return []
    out, pe = [], PE()
    pe.dwSize = ctypes.sizeof(PE)
    try:
        ok = k.Process32FirstW(ctypes.c_void_p(snap), ctypes.byref(pe))
        while ok:
            out.append((int(pe.th32ProcessID), int(pe.th32ParentProcessID), ""))
            ok = k.Process32NextW(ctypes.c_void_p(snap), ctypes.byref(pe))
    finally:
        k.CloseHandle(ctypes.c_void_p(snap))
    return out


def _proc_fs_rows(cmdline: bool) -> list:
    out = []
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            with open(f"/proc/{d}/stat", encoding="utf-8", errors="replace") as f:
                ppid = int(f.read().rsplit(")", 1)[1].split()[1])
            cmd = ""
            if cmdline:
                with open(f"/proc/{d}/cmdline", "rb") as f:
                    cmd = f.read().replace(b"\0", b" ").decode("utf-8", "replace").strip()
        except (OSError, IndexError, ValueError):
            continue
        out.append((int(d), ppid, cmd))
    return out


def scan_processes(cmdline: bool = True, timeout: float = PROC_SCAN_TIMEOUT_S) -> list:
    """[(pid, ppid, командная строка)] всех процессов — единственный сканер таблицы процессов, только stdlib. Windows:
    CIM (с командной строкой) или Toolhelp32 (cmdline=False, строки пустые); Linux — /proc; иначе `ps`. Сбой — []."""
    import hide
    import subprocess
    try:
        if os.name == "nt":
            if not cmdline:
                return _toolhelp_rows()
            text = hide.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", _CIM_ROWS], capture_output=True,
                            text=True, encoding="utf-8", errors="replace", timeout=timeout).stdout
            rows = [(ln.split("\t", 2) + [""])[:3] for ln in text.splitlines() if "\t" in ln]
        else:
            rows = _proc_fs_rows(cmdline) if os.path.isdir("/proc") else []
            if rows:
                return rows
            text = hide.run(["ps", "-ww", "-eo", "pid=,ppid=,args="], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=timeout).stdout
            rows = [(ln.split(None, 2) + [""])[:3] for ln in text.splitlines() if ln.strip()]
        return [(int(p), int(pp), c) for p, pp, c in rows if p.isdigit() and pp.isdigit()]
    except (OSError, subprocess.SubprocessError, ValueError):
        return []


def _parent_map() -> dict:
    """{pid: ppid} всех процессов (без командных строк)."""
    return {pid: ppid for pid, ppid, _ in scan_processes(cmdline=False)}


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


FRONTMATTER = re.compile(r"^---\r?\n(.*?)\r?\n---\r?\n?", re.S)


def frontmatter(text: str):
    """Единственный разбор шапки `---` ... `---` (тикет, ошибка, цель, роль): (плоская шапка {ключ: значение}, смещение
    конца шапки) или None, если шапки нет. Пустые строки в шапке пропускаются, перевод строки — LF или CRLF."""
    m = FRONTMATTER.match(text)
    if not m:
        return None
    head = {}
    for line in m.group(1).splitlines():
        if line.strip():
            key, _, value = line.partition(":")
            head[key.strip()] = value.strip()
    return head, m.end()


def _atomic_write(path, write, retries: int) -> None:
    """Запись через временный файл (`write(tmp)`) + os.replace. На Windows replace падает с PermissionError, пока
    другой процесс (диспетчер, роль) держит файл открытым на чтение, — несколько коротких повторов. Сбой записи
    оставляет прежний файл целым и не оставляет `.tmp`. Каталог создаётся."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        write(tmp)
        for i in range(retries):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if i == retries - 1:
                    raise
                time.sleep(0.05 * (i + 1))
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def atomic_write_text(path, text: str, retries: int = 8) -> None:
    """Текст (UTF-8, LF) целиком или прежний файл — `_atomic_write`."""
    _atomic_write(path, lambda tmp: tmp.write_text(text, encoding="utf-8", newline="\n"), retries)


def atomic_write_bytes(path, data: bytes, retries: int = 8) -> None:
    """Байты как есть (уставы с CRLF) целиком или прежний файл — `_atomic_write`."""
    _atomic_write(path, lambda tmp: tmp.write_bytes(data), retries)


def write_json(path, data, sort_keys: bool = False) -> None:
    """Единственная запись JSON состояния: `indent=2`, кириллица как есть, перевод строки в конце, атомарно."""
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2, sort_keys=sort_keys) + "\n")


def read_json(path, default=None):
    """Единственное чтение JSON: нет файла / битый JSON — `default`. Вид значения (объект, список) проверяет вызывающий."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def read_log(path) -> list:
    """Журнал (`config set`, `charter sync`): список записей-объектов; нет файла, битый JSON, не список — `[]`."""
    data = read_json(path, [])
    return [e for e in data if isinstance(e, dict)] if isinstance(data, list) else []


def read_pid(pid_file) -> int:
    """Pid из файла замка; нет файла, пусто, не число — 0."""
    try:
        return int(Path(pid_file).read_text(encoding="utf-8").strip() or 0)
    except (OSError, ValueError):
        return 0


def take_lock_file(path, wait_s: float, stale_s: float, poll_s: float) -> bool:
    """Замок-файл: создаётся `O_EXCL`; старше `stale_s` секунд — бесхозный (держал умерший процесс), снимается.
    Не отдали за `wait_s` — False (вызывающий решает: отказ или работа без замка). Снимает `drop_lock_file`."""
    path = Path(path)
    deadline = time.time() + wait_s
    while True:
        try:
            os.close(os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            return True
        except FileExistsError:
            try:
                if time.time() - path.stat().st_mtime > stale_s:
                    path.unlink()
                    continue
            except OSError:
                pass
            if time.time() >= deadline:
                return False
            time.sleep(poll_s)


def drop_lock_file(path) -> None:
    try:
        Path(path).unlink()
    except OSError:
        pass
