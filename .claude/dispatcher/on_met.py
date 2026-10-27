"""on_met: продолжение по коду после wait_for (TK-056 п.2; семантика — запись Судьи 05.10 23:34).

Вынесено из `dispatch.py` (TK-011, раунд ревью 1): пути проекта приходят параметрами (`root`, `runs_log`), диспетчера
модуль не импортирует.
"""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hide  # noqa: E402
import procs  # noqa: E402
import ticket as T  # noqa: E402

ON_MET_TIMEOUT_S = 120.0
ON_MET_MAX_CHAIN = 3          # подряд on_met без запуска роли на тикет; 4-й раз — будим владельца
ON_MET_INTERPRETERS = ("python", "python3", "bash")
ON_MET_DIRS = ("tools", ".claude")


def _git_tracked(root, rel: str) -> bool:
    return hide.git(root, "ls-files", "--error-unmatch", "--", rel, timeout=20).returncode == 0


def _on_met_argv(spec: str, root):
    """(argv, None) | (None, причина отказа): argv[0] — python/bash, argv[1] — отслеживаемый git скрипт под tools/ или .claude/."""
    try:
        argv = shlex.split(spec, posix=True)
    except ValueError as e:
        return None, f"разбор команды: {e}"
    if len(argv) < 2 or argv[0] not in ON_MET_INTERPRETERS:
        return None, f"argv[0] должен быть из {ON_MET_INTERPRETERS}, дальше — скрипт"
    rel = argv[1]
    parts = rel.split("/")
    if "\\" in rel or rel.startswith("/") or ".." in parts or parts[0] not in ON_MET_DIRS:
        return None, f"скрипт `{rel}` вне tools/ и .claude/ (пути — с прямыми слешами)"
    try:
        tracked = _git_tracked(root, rel)
    except Exception as e:
        return None, f"git ls-files: {type(e).__name__}: {e}"
    if not tracked:
        return None, f"скрипт `{rel}` не отслеживается git"
    return argv, None


def _tail(b, limit: int = 1024) -> str:
    text = b.decode("utf-8", "replace") if isinstance(b, (bytes, bytearray)) else (b or "")
    return text.strip()[-limit:]


def _log_on_met_run(runs_log, tid: str, now, status: str, dur: float) -> None:
    runs_log.parent.mkdir(parents=True, exist_ok=True)
    with open(runs_log, "a", encoding="utf-8") as fh:
        fh.write(f"{T.now_iso(now)} {tid} on_met reason=on_met attempt=0 session=- cost_usd=- resolved_cost=0.0000 "
                 f"cost_note=asis ticket_spent=0.0000 in_tok=- out_tok=- cr_tok=- cw_tok=- tok_src=none ctx_last=0 "
                 f"ctx_sum=0 dur_s={dur:.1f} status={status}\n")


def run_on_met(path: Path, tkt: T.Ticket, state: dict, now, root, runs_log) -> bool:
    """waiting + wait_for выполнен + on_met задан → команда вместо пробуждения LLM. True — тикет обработан этим тиком
    (кандидата в запуск роли не делаем); False — on_met отклонён/сброшен, решает обычное `wait_for-met`."""
    tid = tkt.id
    spec = (tkt.header.get("on_met") or "").strip()
    old_wait = (tkt.header.get("wait_for") or "").strip()
    T.write_header_updates(path, {"on_met": ""}, now=now, stamp_updated=False)  # до запуска: падение не даёт повтора
    chain = state.setdefault("on_met_chain", {})
    if chain.get(tid, 0) >= ON_MET_MAX_CHAIN:
        T.append_log(path, "dispatcher", f"on_met не запущен: {ON_MET_MAX_CHAIN} подряд без запуска роли — будим владельца. "
                     f"Команда: {spec}", now=now)
        chain.pop(tid, None)
        return False
    argv, why = _on_met_argv(spec, root)
    if argv is None:
        T.append_log(path, "dispatcher", f"on_met отклонён ({why}): {spec} — будим владельца", now=now)
        return False
    chain[tid] = chain.get(tid, 0) + 1
    T.append_log(path, "dispatcher", f"запущен on_met: {argv}", now=now)
    env = dict(os.environ, RPV_TICKET=tid)
    t0 = time.time()
    out = err = b""
    code, status = None, "ok"
    try:
        proc = subprocess.Popen(argv, cwd=str(root), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                **hide.hidden())
        try:
            out, err = proc.communicate(timeout=ON_MET_TIMEOUT_S)
            code = proc.returncode
        except subprocess.TimeoutExpired:
            procs._kill(proc.pid, tree=True)
            status = "timeout"
            try:
                out, err = proc.communicate(timeout=10)
            except Exception:
                pass
    except Exception as e:
        status, err = "fail", f"{type(e).__name__}: {e}".encode()
    dur = time.time() - t0
    if status == "ok" and code != 0:
        status = "fail"
    _log_on_met_run(runs_log, tid, now, status, dur)
    head = f"on_met {argv}: код {code}, {dur:.1f} с"
    if status != "ok":
        tmo = f", таймаут {int(ON_MET_TIMEOUT_S)} с, дерево убито" if status == "timeout" else ""
        T.append_log(path, "dispatcher", f"{head}{tmo} — будим владельца.\nstdout: {_tail(out)}\nstderr: {_tail(err)}", now=now)
        chain.pop(tid, None)
        return False
    T.append_log(path, "dispatcher", f"{head}\nstdout: {_tail(out)}", now=now)
    after = T.read_ticket(path)
    new_wait = (after.header.get("wait_for") or "").strip()
    if after.status in ("in_review", "done") or after.next_role == "judge":
        T.write_header_updates(path, {"status": "in_progress", "next": ""}, now=now)
        T.append_log(path, "dispatcher", "on_met не закрывает тикет и не зовёт Судью: возврат в in_progress, будим владельца", now=now)
        chain.pop(tid, None)
        return True
    if after.status == "waiting" and new_wait and new_wait != old_wait:
        return True  # цепочка: следующий этап ждёт своё условие, LLM не нужен
    if after.status == "waiting":
        T.write_header_updates(path, {"status": "in_progress", "wait_for": ""}, now=now)
    chain.pop(tid, None)
    return True
