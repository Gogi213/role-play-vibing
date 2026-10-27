"""Процессы: жив ли pid, снять процесс (с деревом), замок единственного экземпляра службы.

Вынесено из `dispatch.py` (TK-011, раунд ревью 1): раньше `start`, `watch`, `ci_watch`, `errors` тянули эти имена из
диспетчера и замыкали циклы импортов. Только stdlib и `project`, `hide`; `dispatch` отсюда ничего не импортирует.
"""
from __future__ import annotations

import os
import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hide  # noqa: E402
import project as P  # noqa: E402

PID_EXPECT_NAME = "claude"  # _pid_alive: подстрока имени образа процесса; тесты подменяют на "python"


def _proc_start(pid):
    """Метка времени старта процесса (строка) — вместе с pid однозначно называет процесс, переиспользованный pid даёт
    другую метку. Windows — GetProcessTimes (ctypes), Linux — поле 22 `/proc/<pid>/stat`, иначе `ps -o lstart=`.
    None — процесса нет или метку узнать нельзя."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.OpenProcess.restype = wintypes.HANDLE
            h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return None
            try:
                c, e, k, u = (wintypes.FILETIME() for _ in range(4))
                if not k32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e), ctypes.byref(k), ctypes.byref(u)):
                    return None
                return str((c.dwHighDateTime << 32) | c.dwLowDateTime)
            finally:
                k32.CloseHandle(h)
        except Exception:
            return None
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as fh:
            return "t" + fh.read().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        pass
    got = _ps_field(pid, "lstart")
    return "s" + " ".join(got.split()) if got else None


def _pid_alive(pid, expect_name: str = None, start: str = None) -> bool:
    """Жив ли pid. `start` — метка старта, записанная при запуске (`_proc_start`): сверка pid + старт вместо имени
    образа (роль под npm-установкой — `node`, не `claude`); не совпала — pid занят другим процессом, мёртв; метку узнать
    нельзя — только «процесс есть». Дальше — прежнее: """
    if start:
        now_start = _proc_start(pid)
        if now_start is not None:
            return now_start == start and _pid_alive(pid, "")
        return _pid_state(pid, "") is not False  # метку узнать нельзя (сбой ps) — процесс есть, имя образа не судья
    return _pid_state(pid, expect_name) is not False  # «неизвестно» (таймаут tasklist) = жив: В-209 Д-1


PID_CHECK_TRIES = 3          # tasklist на нагруженной машине отвечает позже 5 с — повтор, не «мёртв»
PID_CHECK_TIMEOUT_S = 10


def _pid_state(pid, expect_name: str = None):
    """True — жив и похож на наш `claude`; False — процесса нет / чужой образ; None — узнать не удалось (таймаут,
    сбой tasklist после повторов). Подстрочный поиск pid ловил чужие совпадения (123 ⊂ 1234) — сравнение PID-колонки
    через `/FO CSV` + имя образа."""
    expect_name = PID_EXPECT_NAME if expect_name is None else expect_name
    if not pid:
        return False
    if os.name == "nt":
        for attempt in range(PID_CHECK_TRIES):
            try:
                out = hide.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                               capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=PID_CHECK_TIMEOUT_S)
            except Exception:
                continue
            if out.returncode:
                continue
            for line in (out.stdout or "").splitlines():
                fields = [f.strip().strip('"') for f in line.split(",")]
                if len(fields) >= 2 and fields[1] == str(pid):
                    return (expect_name or "").lower() in fields[0].lower()
            return False
        return None
    return _pid_alive_posix(pid, expect_name)


def _ps_field(pid, field: str):
    """Поле процесса из `ps -o <field>= -p <pid>` (macOS/BSD, где нет /proc; stdlib). Строка (может быть пустой),
    None — ps нет или процесса нет."""
    try:
        out = hide.run(["ps", "-o", f"{field}=", "-p", str(int(pid))], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=5,
                             env=dict(os.environ, LC_ALL="C", TZ="UTC0"))  # lstart зависит от локали и пояса
    except Exception:
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def _proc_state(pid):
    """Буква состояния процесса: Linux — `/proc/<pid>/status` (R, S, D, T, Z, X…), иначе (macOS) — `ps -o stat=`;
    None — узнать нельзя."""
    try:
        with open(f"/proc/{pid}/status", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith("State:"):
                    return line.split(":", 1)[1].strip()[:1] or None
        return None
    except OSError:
        pass
    return (_ps_field(pid, "stat") or "")[:1] or None


def _proc_comm(pid):
    """Имя образа процесса: `/proc/<pid>/comm` или `ps -o comm=` (macOS: путь к исполняемому — берём имя файла).
    None — узнать нельзя."""
    try:
        with open(f"/proc/{pid}/comm", encoding="utf-8", errors="replace") as fh:
            return fh.read().strip()
    except OSError:
        pass
    comm = _ps_field(pid, "comm")
    return os.path.basename(comm) if comm else None


def _pid_alive_posix(pid, expect_name: str = None) -> bool:
    """Живость pid на Linux/macOS: `kill -0` отвечает и на зомби (завершился, но родитель не вызвал wait) — такой
    процесс мёртв, иначе подхват «живого» прогона после перезапуска ждёт вечно (состояние Z или X). Имя образа
    сверяется всегда, когда его можно узнать (без /proc — через ps): чужой процесс с переиспользованным pid — не наш."""
    try:
        os.kill(pid, 0)
    except PermissionError:
        pass  # процесс есть, но чужого пользователя — имя проверим ниже
    except Exception:
        return False
    if _proc_state(pid) in ("Z", "X"):
        return False
    if not expect_name:
        return True
    comm = _proc_comm(pid)
    return True if comm is None else expect_name.lower() in comm.lower()  # имя узнать нельзя — не валим живость


def _kill(pid, tree: bool = False, force: bool = False) -> None:
    """Единственный способ снять процесс. tree — со всеми потомками: Windows `taskkill /T`, POSIX — SIGKILL группе роли
    (запуск со `start_new_session=True`, группа = pid; не лидер своей группы — только он сам: killpg по такой группе
    снял бы и сам диспетчер). force — SIGKILL вместо SIGTERM (Windows: taskkill всегда `/F`)."""
    if not pid:
        return
    try:
        if os.name == "nt":
            hide.run(["taskkill", *(["/T"] if tree else []), "/F", "/PID", str(pid)], capture_output=True, timeout=15)
        elif tree and os.getpgid(pid) == pid:
            os.killpg(pid, getattr(signal, "SIGKILL", 9))
        else:
            os.kill(pid, getattr(signal, "SIGKILL", 9) if tree or force else 15)
    except Exception:
        pass


def acquire_instance_lock(pid_file, expect_name: str = "py"):
    """Замок единственного экземпляра. Проверка-и-запись идут под короткой мьютекс-файлом (`<pid_file>.mx`, O_EXCL):
    без него второй претендент видел файл замка уже созданным, но ещё пустым (pid не записан), считал его битым,
    удалял и брал себе — оба диспетчера «владели» замком (TK-093, гонка в test_watch_round). Файл есть и в нём живой
    чужой процесс — (False, сообщение); процесса нет (упал, зомби) или файл битый — замок забирается; свой pid
    (его записал запускатель) — ок. Pid пишется целиком через временный файл и `os.replace`."""
    pid_file = Path(pid_file)
    me = os.getpid()
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    mx = pid_file.with_name(pid_file.name + ".mx")
    if not P.take_lock_file(mx, wait_s=40, stale_s=30, poll_s=0.01):   # мьютекс старше 30 с держит умерший процесс (секция — миллисекунды)
        return False, f"не удалось взять замок {pid_file}: занят мьютекс {mx.name}"
    try:
        other = P.read_pid(pid_file)
        if other == me:
            return True, ""
        if other and _pid_alive(other, expect_name):
            return False, f"уже запущен (pid {other}, файл {pid_file.name}) — второй экземпляр не нужен, выхожу"
        try:
            P.atomic_write_text(pid_file, str(me))   # осиротевший/битый замок забираем, свой пишем целиком
        except OSError as e:
            return False, f"не удалось взять замок {pid_file}: {e}"
        return True, ""
    finally:
        P.drop_lock_file(mx)


def release_instance_lock(pid_file) -> None:
    """Снять замок, если он наш (чужой pid не трогаем)."""
    if P.read_pid(pid_file) == os.getpid():
        P.drop_lock_file(pid_file)


def _proc_alive(info: dict) -> bool:
    if info.get("popen") is not None:
        return info["popen"].poll() is None
    return _pid_alive(info.get("pid"), start=info.get("pstart"))


def _kill_proc(info: dict) -> None:
    if info.get("popen") is not None:
        try:
            info["popen"].kill()
            info["popen"].wait(timeout=10)
        except Exception:
            pass
    else:
        _kill(info.get("pid"))
