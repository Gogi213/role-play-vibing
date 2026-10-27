"""Остановка роли и подхват запусков: заявки `tickets.py stop`, опрос идущих запусков, подхват после перезапуска.

Вынесено из dispatch.py (TK-011). Зависимости от запусков и процессов (RUNNING, _finish_run, _kill_proc…) передаются
параметрами — подставляют обёртки в dispatch.py, поэтому подмена имён на dispatch в тестах работает как раньше."""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import project_paths as CFG  # noqa: E402
import project as P  # noqa: E402
import ticket as T  # noqa: E402


def poll_running(state: dict, now, running, proc_alive, kill_proc, finish_run, run_timeout) -> None:
    for tid in list(running):
        info = running[tid]
        if proc_alive(info):
            if (now - info["started"]).total_seconds() > run_timeout:
                kill_proc(info)
                del running[tid]
                finish_run(tid, info, state, now, timed_out=True)
            continue
        del running[tid]
        finish_run(tid, info, state, now, timed_out=False)


def _find_pid_by_session(session_id) -> "int | None":
    """Роль, запущенная перед убийством диспетчера, но не успевшая попасть в зеркало с pid: ищем процесс по
    `--session-id`/`--resume <id>` в командной строке (id известен до запуска)."""
    if not session_id or not re.fullmatch(r"[A-Za-z0-9_-]+", str(session_id)):
        return None
    sid = str(session_id).lower()
    pairs = [(p, pp) for p, pp, cmd in P.scan_processes()
             if p != os.getpid() and sid in cmd.lower() and "get-ciminstance" not in cmd.lower()]  # сам сканер — не роль
    found = {p for p, _ in pairs}
    roots = [p for p, pp in pairs if pp not in found]  # обёртка (cmd/node/sh) выше дочернего — берём корень дерева
    return min(roots or found) if found else None


def recover_active_runs(state: dict, now, running, finish_run, proc_start, pid_alive) -> None:
    """После перезапуска диспетчера — подхватить зеркало state.json["active_runs"]: живой pid не
    запускаем повторно (просто продолжаем отслеживать по pid), уже закончившийся — обрабатываем как
    обычное завершение прогона (лог/ретрай/blocked), раз диспетчер это пропустил, пока не работал.
    Зеркало от диспетчера до v2 снимка `log_keys_at_launch` не содержит — тогда «новая запись» ищется
    по времени старта (см. _role_logged)."""
    for tid, saved in list(state.get("active_runs", {}).items()):
        if tid in running:
            continue  # уже отслеживаем в этом процессе (это не перезапуск)
        info = {
            "role": saved.get("role"), "popen": None, "pid": saved.get("pid"), "pstart": saved.get("pstart"),
            "started": T.parse_dt(saved["started"]), "attempt": saved.get("attempt", 0),
            "run_file": Path(saved["run_file"]), "err_file": Path(saved.get("err_file") or ""),
            "out_fh": None, "err_fh": None, "reason": saved.get("reason", "recovered"),
            "status_at_launch": saved.get("status_at_launch"),
            "executor": saved.get("executor", ""), "log_keys_at_launch": saved.get("log_keys_at_launch"),
            "effort": saved.get("effort", ""),
        }
        by_session = False
        if not saved.get("pid"):  # зеркало-намерение: диспетчер убит до записи pid
            by_session = True
            found = _find_pid_by_session(saved.get("session_id"))
            if not found:
                state.get("active_runs", {}).pop(tid, None)
                try:
                    has_output = info["run_file"].stat().st_size > 0
                except OSError:
                    has_output = False
                if has_output:  # роль отработала, пока диспетчер лежал: итог прогона не терять
                    print(f"[dispatch] {T.now_iso(now)} подхват {tid}: процесса нет, вывод есть — разбираю как завершённый",
                          file=sys.stderr, flush=True)
                    finish_run(tid, info, state, now, timed_out=False)
                else:
                    print(f"[dispatch] {T.now_iso(now)} подхват {tid}: запуск не состоялся (процесса с session_id нет, "
                          "вывода нет) — зеркало снято", file=sys.stderr, flush=True)
                continue
            saved["pid"] = info["pid"] = found
            saved["pstart"] = info["pstart"] = proc_start(found)  # метки старта не было — берём текущую: «процесс есть»
            print(f"[dispatch] {T.now_iso(now)} подхват {tid}: pid {found} найден по session_id (зеркало было без pid)",
                  file=sys.stderr, flush=True)
        want, got = saved.get("pstart"), proc_start(saved.get("pid"))
        alive = pid_alive(saved.get("pid"), expect_name="" if by_session else None, start=want)
        how = (f"старт записан {want}, сейчас {got}" if want
               else "старт не записан (запуск прежней версии) — по имени образа")
        print(f"[dispatch] {T.now_iso(now)} подхват {tid}: pid {saved.get('pid')}, {how}: "
              f"{'жив — слежу' if alive else 'не найден или занят другим процессом — разбираю как завершённый'}",
              file=sys.stderr, flush=True)
        if alive:
            running[tid] = info
        else:
            state.get("active_runs", {}).pop(tid, None)
            finish_run(tid, info, state, now, timed_out=False)


# --- остановка роли посреди запуска: `tickets.py stop` (CEO, 03.10) ----------------------------------------------

def write_stop_request(tid: str, next_role: str, text: str, now=None) -> Path:
    """`tickets.py stop`: заявка `stop/<ID>.json` — время, роль для `--next` (или пусто) и текст новой постановки. Диспетчер
    разбирает её на ближайшем тике (process_stop_requests); новая заявка по тому же тикету заменяет прежнюю."""
    CFG.STOP_DIR.mkdir(parents=True, exist_ok=True)
    path = CFG.STOP_DIR / f"{tid}.json"
    P.write_json(path, {"at": T.now_iso(now), "next": next_role or "", "text": text})
    return path


def _stop_requests() -> list:
    out = []
    for path in sorted(CFG.STOP_DIR.glob("*.json")) if CFG.STOP_DIR.exists() else []:
        try:
            req = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(req, dict):
            out.append((path, req))
    return out


def _reset_same_status(state: dict, key: str) -> None:
    state.setdefault("same_status_runs", {}).pop(key, None)


def _reset_loop_state(state: dict, tid: str) -> None:
    """Остановка CEO — не провал и новая постановка: счётчики холостых и «запись есть, статус тот же» запусков, повторов,
    ротации (сессия и контекст ЭТОГО тикета у всех ролей) и часовой лимит запусков по тикету — с нуля."""
    for role in P.ROLES:
        key = f"{tid}::{role}"
        state.setdefault("idle_runs", {}).pop(key, None)
        _reset_same_status(state, key)
        if key in state.get("sessions", {}):
            state["sessions"][key]["retries"] = 0
        store = state.get("ticket_sessions", {}).get(key)
        if store:
            store.pop("session_id", None)
    state.get("launch_history", {}).pop(tid, None)
    state.get("launch_role", {}).pop(tid, None)


def _apply_stop(path: Path, tid: str, req: dict, info, state: dict, now) -> None:
    """Запись тикета после остановки: след диспетчера → запись CEO с постановкой (последняя в логе — её читает следующий
    запуск) → статус (`--next`: todo + next, роль стартует в этом же тике; иначе stopped — не будит, пока CEO не переведёт
    в todo). Был снят запуск — следующий запуск тикета начнёт новую сессию (пометка `stopped_runs`, см. launch_run)."""
    hhmm = now.strftime("%H:%M")
    if info is not None:
        T.append_log(path, "dispatcher", f"остановлен CEO в {hhmm}: запуск роли {info['role']} (pid {info.get('pid')}) снят "
                                          "вместе с дочерними процессами; следующий запуск — новая сессия.", now=now)
    else:
        T.append_log(path, "dispatcher", f"остановка CEO в {hhmm}: запущенной роли не было — статус применён.", now=now)
    try:
        at = T.parse_dt(req["at"])
    except Exception:
        at = now
    T.append_log(path, "ceo", str(req.get("text") or "").strip() or "(постановка без текста)", now=at)
    nxt = req.get("next")
    if nxt in P.ROLES:
        T.write_header_updates(path, {"status": "todo", "next": nxt}, now=now)
    else:
        T.write_header_updates(path, {"status": "stopped", "next": ""}, now=now)
    _reset_loop_state(state, tid)
    if info is not None:
        state.setdefault("stopped_runs", {})[tid] = T.now_iso(now)


def process_stop_requests(state: dict, now, running, stop_run, finish_run, append_ceo_inbox) -> None:
    """Заявки `tickets.py stop`: запущенную роль тикета снять деревом процессов, проверить смерть, записать след и статус.
    Не умерла — одна на заявку строка CEO `stop-failed`, заявка и запуск остаются до следующего тика. Тикета нет или он
    не читается — заявку не трогаем (битый тикет заметит основной цикл; чужой корень заявку не съест)."""
    for req_path, req in _stop_requests():
        tid = req_path.stem
        path = CFG.TICKETS_DIR / f"{tid}.md"
        try:
            T.read_ticket(path)
        except Exception:
            continue
        info = running.get(tid)
        if info is not None:
            if not stop_run(info):
                notified = state.setdefault("ceo_stop_failed_notified", {})
                if notified.get(tid) != req.get("at"):
                    append_ceo_inbox(tid, "stop-failed", f"{info['role']}: pid {info.get('pid')} не умер после снятия "
                                     "деревом процессов — заявка остаётся, снимите вручную", now)
                    notified[tid] = req.get("at")
                continue
            running.pop(tid, None)
            finish_run(tid, info, state, now, timed_out=False, stopped=True)
        _apply_stop(path, tid, req, info, state, now)
        try:
            req_path.unlink()
        except OSError:
            pass


# --- тик / цикл -------------------------------------------------------------------------------
