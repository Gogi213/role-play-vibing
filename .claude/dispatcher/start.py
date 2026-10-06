"""Запуск диспетчера и сторожа проекта в фоне (команда `/rpv-start`). Только stdlib, Windows и POSIX.

    python <плагин>/.claude/dispatcher/start.py [--project <проект>]

Проект — как у остальных скриптов (`--project`, RPV_PROJECT, CLAUDE_PROJECT_DIR, иначе поиск вверх с
`.claude/roles`); нет `.claude/roles` — отказ с подсказкой `/rpv-init`, ничего не создаётся. Для `dispatch.py`
и `watch.py` из этой же папки плагина: если по pid-замку (`dispatch.pid`, `watch.pid` в `<проект>/.claude/dispatcher/`)
процесс уже жив — он останавливается и запускается заново (перезапуск после обновления плагина; запущенные
диспетчером роли переживают перезапуск — диспетчер подхватывает их по pid из state.json; под systemd юнит гасит и роли).

Служба запускается ОТВЯЗАННОЙ от сессии, из которой вызвали `start.py`, — иначе она умирает вместе с ней:
- Windows — WMI `Win32_Process.Create` (PowerShell `Invoke-CimMethod`): процесс создаёт служба WMI, он вне job-объекта
  приложения (`Popen` с CREATE_BREAKAWAY_FROM_JOB остаётся в job молча); вывод — `cmd /c "… >> лог 2>&1"`, pid — из ответа
  CIM (это `cmd`; сама служба — его потомок, её pid — в `<имя>.pid`);
- Linux с systemd — `systemd-run --user --unit rpv-<служба>-<хеш пути проекта> --collect` (вывод — `bash -c 'exec … >> лог
  2>&1'`); pid — MainPID юнита, остановка — `systemctl --user stop`;
- иначе (нет systemd, WMI не ответил) — `Popen`: Windows — без окна и вне группы процессов, POSIX — `start_new_session`.
Печатает pid, «отвязан: да/нет» (Windows — IsProcessInJob; Linux — cgroup процесса не совпадает с cgroup этой сессии) и путь
лога `<проект>/.claude/dispatcher/<имя>.run.log` (вывод дописывается).
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shlex
import shutil
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
BOARD_SERVICE = "board_push"       # третья служба — отправка на веб-табло; стартует, только если задан RPV_BOARD
BOARD_ARGS = ("--loop", "5")


def services() -> tuple:
    return SERVICES + ((BOARD_SERVICE,) if P.env("BOARD") else ())
LOCK_IMAGE = "py"                  # подстрока имени образа процесса, как в dispatch.acquire_instance_lock
STOP_TIMEOUT_S = 10.0
SETTLE_S = 1.5                     # столько ждём после запуска: упал сразу (замок, ошибка) — скажем об этом

CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

# Службу, которую запускает не этот процесс (WMI, systemd), окружение вызывающего не наследует: переносим настройки
# диспетчера (RPV_*, ALPHA_*) и пути claude/проекта; секреты (ANTHROPIC_*, токены) — нет: командная строка видна другим.
FORWARD_ENV_PREFIXES = ("RPV_", "ALPHA_")
FORWARD_ENV_NAMES = ("CLAUDE_BIN", "CLAUDE_PROJECT_DIR", "CLAUDE_CONFIG_DIR")

# PowerShell: Win32_Process.Create с невидимым окном; команда и каталог — через окружение (RPV_START_CMD/RPV_START_CWD),
# чтобы не экранировать их в тексте скрипта; печатает pid из ответа CIM, ненулевой ReturnValue — код выхода 2.
_CIM_CREATE = (
    "$si = New-CimInstance -Namespace root/cimv2 -ClassName Win32_ProcessStartup -ClientOnly "
    "-Property @{ShowWindow=[uint16]0}; "
    "$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments "
    "@{CommandLine=$env:RPV_START_CMD; CurrentDirectory=$env:RPV_START_CWD; ProcessStartupInformation=$si}; "
    "if ($r.ReturnValue -ne 0) { [Console]::Error.WriteLine('ReturnValue=' + $r.ReturnValue); exit 2 }; $r.ProcessId")


def read_pid(pid_file: Path) -> int:
    try:
        return int(pid_file.read_text(encoding="utf-8").strip() or 0)
    except (OSError, ValueError):
        return 0


def _cmdline(pid: int) -> str | None:
    """Командная строка процесса или None (узнать нельзя). Linux — /proc, macOS — ps, Windows — PowerShell/CIM."""
    if os.name == "nt":
        for attempt in range(3):  # холодный PowerShell на нагруженном раннере: таймаут/пустой ответ — повтор, не «узнать нельзя»
            try:
                out = subprocess.run(
                    ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                     f"(Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}').CommandLine"],
                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
                if out.stdout.strip():
                    return out.stdout.strip()
            except Exception:
                pass
            time.sleep(0.5)
        return None
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            return fh.read().replace(b"\0", b" ").decode("utf-8", "replace").strip() or None
    except OSError:
        pass
    return D._ps_field(pid, "args") or None   # macOS/BSD: /proc нет


def is_ours(pid: int, script_name: str) -> bool:
    """Живой процесс из pid-файла — наш скрипт, а не чужой python с переиспользованным pid. Командную строку
    узнать не удалось — считаем нашим (как и сам pid-замок диспетчера)."""
    if not (pid and D._pid_alive(pid, LOCK_IMAGE)):
        return False
    line = _cmdline(pid)
    return True if line is None else script_name.lower() in line.lower()


# --- systemd (Linux) ----------------------------------------------------------------------------------------------

def _systemctl(*args: str, timeout: float = 20.0) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout)


def systemd_user_available() -> bool:
    """Linux, есть `systemd-run` и `systemctl --user` отвечает (менеджер пользовательских юнитов запущен)."""
    if not sys.platform.startswith("linux") or not shutil.which("systemd-run") or not shutil.which("systemctl"):
        return False
    try:
        return _systemctl("show", "-p", "Version").returncode == 0
    except Exception:
        return False


def _launcher() -> str:
    """Чем запускать службы: "wmi" (Windows), "systemd" (Linux с пользовательским systemd), иначе "popen"."""
    if os.name == "nt":
        return "wmi"
    return "systemd" if systemd_user_available() else "popen"


def unit_name(service: str, project: Path) -> str:
    """Имя юнита службы проекта: `rpv-<dispatch|watch>-<8 знаков хеша пути проекта>` — у каждого проекта свои."""
    return f"rpv-{service}-{hashlib.sha1(str(project).encode('utf-8')).hexdigest()[:8]}"


def _main_pid(unit: str) -> int:
    for line in _systemctl("show", "-p", "MainPID", f"{unit}.service").stdout.splitlines():
        if line.startswith("MainPID="):
            try:
                return int(line.split("=", 1)[1])
            except ValueError:
                return 0
    return 0


def _systemd_run(unit: str, cmd: list, project, log, env: dict) -> int:
    """Transient-юнит пользователя: `exec` в `bash -c` — MainPID юнита и есть python; вывод — в лог. Возвращает MainPID
    (0 — юнит уже завершился); `systemd-run` отказал — OSError."""
    shell = "exec " + " ".join(shlex.quote(str(a)) for a in cmd) + f" >> {shlex.quote(str(log))} 2>&1"
    args = ["systemd-run", "--user", "--unit", unit, "--collect", "-p", f"WorkingDirectory={project}"]
    args += [f"--setenv={k}={v}" for k, v in env.items()]
    args += ["bash", "-c", shell]
    done = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    if done.returncode != 0:
        raise OSError(f"systemd-run: {done.stderr.strip() or done.returncode}")
    return _main_pid(unit)


def stop_unit(unit: str) -> int:
    """Останавливает наш юнит, если он есть и не остановлен. Возвращает его MainPID (0 — юнита не было или pid неизвестен)."""
    try:
        out = _systemctl("show", "-p", "ActiveState", "-p", "MainPID", f"{unit}.service").stdout
        props = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
        if props.get("ActiveState") not in ("active", "activating", "deactivating", "reloading"):
            return 0
        _systemctl("stop", f"{unit}.service")
        return int(props.get("MainPID") or 0)
    except Exception:
        return 0


# --- Windows: WMI -------------------------------------------------------------------------------------------------

def _forward_env() -> dict:
    """Окружение службы, запускаемой не этим процессом: UTF-8 для Python, настройки диспетчера, пути; PATH — на POSIX
    (менеджер юнитов даёт свой; на Windows WMI берёт PATH из реестра)."""
    env = {"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    for k, v in os.environ.items():
        if (k.startswith(FORWARD_ENV_PREFIXES) or k in FORWARD_ENV_NAMES) and v and not any(c in v for c in '"%\r\n'):
            env[k] = v
    if os.name != "nt" and os.environ.get("PATH"):
        env["PATH"] = os.environ["PATH"]
    return env


def _cim_cmdline(cmd: list, log, env: dict) -> str:
    """Командная строка для Win32_Process.Create: `cmd /d /s /c "set … && "python" … >> "лог" 2>&1"` — вывод в лог, окружение
    через set (WMI окружение вызывающего не передаёт); /s снимает внешние кавычки, внутренние остаются."""
    sets = " && ".join(f'set "{k}={v}"' for k, v in env.items())
    prog = " ".join(f'"{a}"' for a in cmd)
    return f'cmd.exe /d /s /c "{sets} && {prog} >> "{log}" 2>&1"'


def _cim_create(cmdline: str, cwd: str) -> int:
    """Win32_Process.Create через PowerShell/CIM: процесс создаёт служба WMI — он вне job-объекта этой сессии. Возвращает
    pid из ответа CIM; не вышло (нет PowerShell, ReturnValue ≠ 0, нет pid) — OSError."""
    env = dict(os.environ, RPV_START_CMD=cmdline, RPV_START_CWD=cwd)
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", _CIM_CREATE],
                             capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=60)
    except Exception as e:
        raise OSError(f"powershell: {e}") from e
    lines = out.stdout.strip().splitlines()
    if out.returncode != 0 or not lines or not lines[-1].strip().isdigit():
        raise OSError(f"Win32_Process.Create: код {out.returncode}, {out.stderr.strip()[:200]}")
    return int(lines[-1])


def _in_job(pid: int):
    """Windows: True — процесс в job-объекте (умрёт вместе с приложением), False — вне его, None — проверить нельзя."""
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        k32.OpenProcess.restype = ctypes.c_void_p
        k32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        k32.IsProcessInJob.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, int(pid))
        if not handle:
            return None
        try:
            res = ctypes.c_int(0)
            return bool(res.value) if k32.IsProcessInJob(handle, None, ctypes.byref(res)) else None
        finally:
            k32.CloseHandle(handle)
    except Exception:
        return None


def _cgroup(pid: int) -> str | None:
    try:
        with open(f"/proc/{pid}/cgroup", encoding="utf-8") as fh:
            return fh.read().strip() or None
    except OSError:
        return None


def _detached_linux(pid: int) -> bool | None:
    """cgroup процесса не совпадает с cgroup этой сессии (юнит systemd) — выход из сессии его не заденет."""
    mine, theirs = _cgroup(os.getpid()), _cgroup(pid)
    return None if mine is None or theirs is None else mine != theirs


def is_detached(pid) -> bool | None:
    """Отвязан ли процесс от сессии, из которой запустили: Windows — вне job-объекта; Linux — свой cgroup. None — не проверить."""
    if not pid:
        return None
    if os.name == "nt":
        job = _in_job(pid)
        return None if job is None else not job
    return _detached_linux(pid)


def _yes_no(detached) -> str:
    return "да" if detached else "нет" if detached is False else "нет (проверить не удалось)"


# --- запуск и остановка -------------------------------------------------------------------------------------------

class Started:
    """Запущенная служба. pid: Popen — pid процесса; WMI — pid `cmd` из ответа CIM; systemd — MainPID юнита (0 — юнит уже
    завершился). how — "popen" | "wmi" | "systemd"."""

    def __init__(self, pid: int, how: str, popen: subprocess.Popen | None = None, unit: str | None = None):
        self.pid, self.how, self.popen, self.unit = pid, how, popen, unit

    def poll(self):
        """None — работает; иначе код завершения ("?" — после запуска через WMI/systemd он неизвестен)."""
        if self.popen is not None:
            return self.popen.poll()
        return None if self.pid and D._pid_alive(self.pid, "cmd" if self.how == "wmi" else LOCK_IMAGE) else "?"


def stop_running(pid_file: Path, script_name: str, timeout: float = STOP_TIMEOUT_S, unit: str | None = None) -> int:
    """Останавливает процесс из pid-замка, если он жив и наш (и наш юнит systemd, если он есть); осиротевший pid-файл
    удаляет. Возвращает pid остановленного процесса или 0."""
    stopped = stop_unit(unit) if unit else 0
    pid = read_pid(pid_file)
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


def _popen_detached(cmd: list, project: Path, log: Path) -> Started:
    """Запасной путь: `Popen`, отвязанный от родителя как умеет ОС; вывод — в `log` (дописывается)."""
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    with open(log, "ab") as out:
        common = dict(cwd=str(project), stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, env=env,
                      close_fds=True)
        if os.name == "nt":
            base = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
            try:   # выйти из job-объекта родителя, чтобы процесс пережил закрытие сессии; не разрешено — без выхода
                p = subprocess.Popen(cmd, creationflags=base | CREATE_BREAKAWAY_FROM_JOB, **common)
            except OSError:
                p = subprocess.Popen(cmd, creationflags=base, **common)
        else:
            p = subprocess.Popen(cmd, start_new_session=True, **common)
    return Started(p.pid, "popen", popen=p)


def _python() -> str:
    """Интерпретатор службы: pythonw (его зовёт Планировщик присмотра) без консоли — дети службы получили бы окна."""
    exe = Path(sys.executable)
    return str(exe.with_name("python.exe")) if exe.name.lower() == "pythonw.exe" else sys.executable


def spawn(script: Path, project: Path, log: Path, extra: tuple = ()) -> Started:
    """Фоновый процесс `python -u script --project <проект>`, отвязанный от этой сессии (Windows — WMI, Linux — юнит
    systemd, иначе Popen; не получилось — запасной путь Popen); вывод — в `log` (дописывается)."""
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "ab") as out:
        out.write(f"\n=== {datetime.now().astimezone().isoformat(timespec='seconds')} старт {script.name} ===\n"
                  .encode("utf-8"))
    cmd = [_python(), "-u", str(script), "--project", str(project), *extra]
    how = _launcher()
    try:
        if how == "wmi":
            return Started(_cim_create(_cim_cmdline(cmd, log, _forward_env()), str(project)), "wmi")
        if how == "systemd":
            unit = unit_name(script.stem, project)
            return Started(_systemd_run(unit, cmd, project, log, _forward_env()), "systemd", unit=unit)
    except OSError:
        pass   # WMI/systemd не ответили — запасной путь ниже
    return _popen_detached(cmd, project, log)


def start(project: Path, code_dir: Path = CODE_DIR, settle: float = SETTLE_S) -> list[dict]:
    """Останавливает уже запущенные диспетчер и сторож проекта и запускает заново. Результат — по строке на службу:
    {name, pid, log, restarted (pid остановленного или 0), ok, rc, how, detached (True/False/None — не проверить)}."""
    state_dir = project / ".claude" / "dispatcher"
    state_dir.mkdir(parents=True, exist_ok=True)
    use_systemd = _launcher() == "systemd"
    procs = []
    for name in services():
        restarted = stop_running(state_dir / f"{name}.pid", f"{name}.py",
                                 unit=unit_name(name, project) if use_systemd else None)
        log = state_dir / f"{name}.run.log"
        procs.append((name, spawn(code_dir / f"{name}.py", project, log, BOARD_ARGS if name == BOARD_SERVICE else ()),
                      log, restarted))
    time.sleep(settle)
    rows = []
    for name, p, log, restarted in procs:
        rc = p.poll()
        rows.append({"name": name, "pid": p.pid, "log": log, "restarted": restarted, "ok": rc is None, "rc": rc,
                     "how": p.how, "detached": is_detached(p.pid) if rc is None else None})
    return rows


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
            print(f"[start] {r['name']}: запущен, pid {r['pid']}{again}; отвязан: {_yes_no(r['detached'])}; лог {r['log']}")
        else:
            bad += 1
            print(f"[start] {r['name']}: завершился сразу (код {r['rc']}) — смотрите лог {r['log']}", file=sys.stderr)
    return 1 if bad else 0


if __name__ == "__main__":
    P.utf8_stdio()
    sys.exit(main())
