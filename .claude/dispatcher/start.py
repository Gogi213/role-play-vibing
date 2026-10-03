"""Запуск диспетчера и сторожа проекта в фоне (команда `/rpv-start`). Только stdlib, Windows и POSIX.

    python <плагин>/.claude/dispatcher/start.py [--project <проект>]

Проект — как у остальных скриптов (`--project`, RPV_PROJECT, CLAUDE_PROJECT_DIR, иначе поиск вверх с
`.claude/roles`); нет `.claude/roles` — отказ с подсказкой `/rpv-init`, ничего не создаётся. Для `dispatch.py`
и `watch.py` из этой же папки плагина: если по pid-замку (`dispatch.pid`, `watch.pid` в `<проект>/.claude/dispatcher/`)
процесс уже жив — он останавливается и запускается заново (перезапуск после обновления плагина; запущенные
диспетчером роли переживают перезапуск — диспетчер подхватывает их по pid из state.json). Процесс запускается
отвязанным от этой сессии: Windows — без окна консоли и вне группы процессов, иначе `start_new_session`; вывод
дописывается в `<проект>/.claude/dispatcher/<имя>.run.log`. Печатает pid и путь лога.
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dispatch as D  # noqa: E402 — _pid_alive/_pid_kill; импорт ничего не создаёт
import project as P  # noqa: E402

CODE_DIR = Path(__file__).resolve().parent
SERVICES = ("dispatch", "watch")   # порядок запуска: сначала диспетчер, потом сторож, который за ним следит
LOCK_IMAGE = "py"                  # подстрока имени образа процесса, как в dispatch.acquire_instance_lock
STOP_TIMEOUT_S = 10.0
SETTLE_S = 1.5                     # столько ждём после запуска: упал сразу (замок, ошибка) — скажем об этом

CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000
CREATE_BREAKAWAY_FROM_JOB = 0x01000000


def read_pid(pid_file: Path) -> int:
    try:
        return int(pid_file.read_text(encoding="utf-8").strip() or 0)
    except (OSError, ValueError):
        return 0


def _cmdline(pid: int) -> str | None:
    """Командная строка процесса или None (узнать нельзя). Linux — /proc, Windows — PowerShell/CIM."""
    if os.name == "nt":
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 f"(Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}').CommandLine"],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15)
            return out.stdout.strip() or None
        except Exception:
            return None
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            return fh.read().replace(b"\0", b" ").decode("utf-8", "replace").strip() or None
    except OSError:
        return None


def is_ours(pid: int, script_name: str) -> bool:
    """Живой процесс из pid-файла — наш скрипт, а не чужой python с переиспользованным pid. Командную строку
    узнать не удалось — считаем нашим (как и сам pid-замок диспетчера)."""
    if not (pid and D._pid_alive(pid, LOCK_IMAGE)):
        return False
    line = _cmdline(pid)
    return True if line is None else script_name.lower() in line.lower()


def stop_running(pid_file: Path, script_name: str, timeout: float = STOP_TIMEOUT_S) -> int:
    """Останавливает процесс из pid-замка, если он жив и наш; осиротевший pid-файл удаляет. Возвращает pid
    остановленного процесса или 0."""
    pid = read_pid(pid_file)
    stopped = 0
    if is_ours(pid, script_name):
        D._pid_kill(pid)
        deadline = time.time() + timeout
        while D._pid_alive(pid, LOCK_IMAGE) and time.time() < deadline:
            time.sleep(0.1)
        if D._pid_alive(pid, LOCK_IMAGE) and os.name != "nt":
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
            time.sleep(0.3)
        stopped = pid
    try:
        pid_file.unlink()
    except FileNotFoundError:
        pass
    return stopped


def spawn(script: Path, project: Path, log: Path) -> subprocess.Popen:
    """Фоновый процесс `python -u script --project <проект>`, отвязанный от родителя; вывод — в `log` (дописывается)."""
    log.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    cmd = [sys.executable, "-u", str(script), "--project", str(project)]
    with open(log, "ab") as out:
        out.write(f"\n=== {datetime.now().astimezone().isoformat(timespec='seconds')} старт {script.name} ===\n"
                  .encode("utf-8"))
        out.flush()
        common = dict(cwd=str(project), stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, env=env,
                      close_fds=True)
        if os.name == "nt":
            base = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
            try:   # выйти из job-объекта родителя, чтобы процесс пережил закрытие сессии; не разрешено — без выхода
                return subprocess.Popen(cmd, creationflags=base | CREATE_BREAKAWAY_FROM_JOB, **common)
            except OSError:
                return subprocess.Popen(cmd, creationflags=base, **common)
        return subprocess.Popen(cmd, start_new_session=True, **common)


def start(project: Path, code_dir: Path = CODE_DIR, settle: float = SETTLE_S) -> list[dict]:
    """Останавливает уже запущенные диспетчер и сторож проекта и запускает заново. Результат — по строке на службу:
    {name, pid, log, restarted (pid остановленного или 0), ok, rc}."""
    state_dir = project / ".claude" / "dispatcher"
    state_dir.mkdir(parents=True, exist_ok=True)
    procs = []
    for name in SERVICES:
        restarted = stop_running(state_dir / f"{name}.pid", f"{name}.py")
        log = state_dir / f"{name}.run.log"
        procs.append((name, spawn(code_dir / f"{name}.py", project, log), log, restarted))
    time.sleep(settle)
    return [{"name": name, "pid": p.pid, "log": log, "restarted": restarted, "ok": p.poll() is None,
             "rc": p.poll()} for name, p, log, restarted in procs]


def main(argv=None, code_dir: Path = CODE_DIR, settle: float = SETTLE_S) -> int:
    argv = sys.argv[1:] if argv is None else argv
    ap = argparse.ArgumentParser(prog="start.py", description="Запуск (перезапуск) диспетчера и сторожа проекта в фоне")
    ap.add_argument("--project", default=None,
                    help="корень проекта (иначе RPV_PROJECT, CLAUDE_PROJECT_DIR, ближайший каталог вверх с .claude/roles)")
    args = ap.parse_args(argv)
    try:
        project = P.resolve_project(["--project", args.project] if args.project else [])
    except P.ProjectNotFound as e:
        print(f"[start] ошибка: {e}", file=sys.stderr)
        return 2
    if not (project / ".claude" / "roles").is_dir():
        print(f"[start] ошибка: в {project} нет `.claude/roles` — сначала выполните `/rpv-init` в корне проекта.",
              file=sys.stderr)
        return 2
    print(f"[start] проект: {project}")
    bad = 0
    for r in start(project, code_dir, settle):
        again = f"; перезапуск, остановлен прежний pid {r['restarted']}" if r["restarted"] else ""
        if r["ok"]:
            print(f"[start] {r['name']}: запущен, pid {r['pid']}{again}; лог {r['log']}")
        else:
            bad += 1
            print(f"[start] {r['name']}: завершился сразу (код {r['rc']}) — смотрите лог {r['log']}", file=sys.stderr)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
