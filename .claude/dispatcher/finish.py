"""Что делать с тикетом после запуска роли: запись/нет записи, холостой ход, петля статуса, повтор или blocked,
ошибка API. Вынесено из dispatch.py (TK-011). Запуск, проверку `wait_for`, каталог транскриптов и пределы передаёт
вызывающий (`Env`) — обёртки в dispatch.py подставляют текущие значения (подмена в тестах)."""
from __future__ import annotations

import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import project_paths as CFG  # noqa: E402
import project as P  # noqa: E402
import signals as SIG  # noqa: E402
import ticket as T  # noqa: E402
import transcript  # noqa: E402
from limits import API_AUTH_PAUSE, API_PAUSE, MAX_API_ERRORS, _auth_error  # noqa: E402
from stop import _reset_same_status  # noqa: E402


@dataclass
class Env:
    """Что finish_role_part берёт у dispatch.py: запуск роли, проверка wait_for, транскрипты и пределы подряд."""
    launch_run: object
    check_wait_for: object
    projects_dir: Path
    max_idle_runs: int
    max_same_status_runs: int


def role_logged(tkt: T.Ticket, role: str, info: dict) -> bool:
    """v2 (02.10): роль оставила НОВУЮ запись лога — заголовок `### <ts> <роль>`, которого не было на старте
    запуска (`log_keys_at_launch`). Не «лог вырос»: запись CEO/dispatcher/другой роли не считается, правка
    текста тоже; от компакции лога (смещений) не зависит. Запуск из старого state.json без снимка ключей
    (диспетчер перезапущен посреди запуска) — запасной путь: запись этой роли с временем не раньше старта."""
    keys = T.role_entry_keys(tkt, role)
    before = info.get("log_keys_at_launch")
    if before is not None:
        return bool(Counter(keys) - Counter(before))  # Counter: две одинаковые записи в одну секунду — две
    started = info.get("started")
    if started is None:
        return bool(keys)
    return any(e.ts >= started for e in tkt.log if T.author_is(e.author, role))


def block_ticket(path: Path, tid: str, role: str, why: str, state: dict, now, log_text: str) -> None:
    T.write_header_updates(path, {"status": "blocked"}, now=now)
    # роль без "@" намеренно: @упоминания никого не будят (v2), а запись dispatcher не должна выглядеть
    # просьбой к роли
    T.append_log(path, "dispatcher", log_text, now=now)
    SIG.append_ceo_inbox(tid, "blocked", f"{role}: {why}", now)
    # одна строка CEO: маркер статуса, чтобы notify_status_for_ceo следующим тиком не написал вторую; счётчики
    # тормозов цикла сброшены — CEO сам решит, что дальше
    state.setdefault("ceo_status_notified", {})[tid] = f"blocked@{T.now_iso(now)}"
    state.setdefault("idle_runs", {}).pop(f"{tid}::{role}", None)
    _reset_same_status(state, f"{tid}::{role}")


def _same_status_loop(tkt: T.Ticket, logged: bool, status_changed: bool, check_wait_for) -> bool:
    """Запуск оставил запись, а статус тот же: in_progress, либо waiting при УЖЕ выполненном wait_for (иначе ждать
    — штатно). Подряд MAX_SAME_STATUS_RUNS таких запусков — петля (аудит 03.10)."""
    if not logged or status_changed:
        return False
    if tkt.status == "in_progress":
        return True
    if tkt.status == "waiting":
        return check_wait_for(tkt.header.get("wait_for", ""))
    return False


def _remember_session(state: dict, tid: str, role: str, info: dict, timed_out: bool, result: dict, projects_dir) -> None:
    store = state.setdefault("ticket_sessions", {}).setdefault(f"{tid}::{role}", {})
    if result.get("session_id"):
        store["session_id"] = result["session_id"]
    elif (timed_out and info.get("session_id") and not store.get("session_id")
          and transcript.find(projects_dir, info["session_id"]) is not None):
        store["session_id"] = info["session_id"]  # убитая новая сессия: транскрипт есть — повтор продолжит её, не с нуля


def _refuse_empty_wait(path: Path, tkt: T.Ticket, role: str, handed_to_ceo: bool, now) -> T.Ticket:
    """TK-070 п.3: ожидание без условия пробуждения — отказ (иначе тикет молчит до эскалации); вернуть в работу."""
    if not (role == tkt.owner and tkt.status == "waiting" and not (tkt.header.get("wait_for") or "").strip()
            and not tkt.next_role and not handed_to_ceo):
        return tkt
    T.write_header_updates(path, {"status": "in_progress"}, now=now)
    T.append_log(path, "dispatcher", "status: waiting без wait_for и без next — отказ: ожидание без условия "
                 "пробуждения не принимается; тикет возвращён в in_progress. Задай условие "
                 "`tickets.py wait <ID> <форма>` (формы — README диспетчера) или передай `--next <роль>`.", now=now)
    return T.read_ticket(path)


def _count_review_return(state: dict, tid: str, tkt: T.Ticket, role: str, info: dict, logged: bool) -> None:
    """Запуск ревьюера на ревью: вернул владельцу (todo/in_progress либо `--next` другой роли) — счётчик +1, иначе
    (принял, заблокировал, передал CEO) ревью состоялось — серия кончилась."""
    if not (role == tkt.reviewer and info.get("status_at_launch") == "in_review" and logged):
        return
    returns = state.setdefault("review_returns", {})
    if tkt.status in ("todo", "in_progress") or (tkt.next_role in P.ROLES and tkt.next_role != role):
        returns[tid] = returns.get(tid, 0) + 1
    else:
        returns.pop(tid, None)


def _blocked_by_loop(path: Path, tid: str, role: str, tkt: T.Ticket, state: dict, now, logged: bool,
                     status_changed: bool, env: Env) -> bool:
    """Холостой ход (нет записи, статус не сменён) и тормоз цикла (запись есть, статус не меняется) — по числу подряд;
    предел — blocked + строка CEO, True. Первый холостой — обычный повтор в `_retry_or_block`."""
    key = f"{tid}::{role}"
    idle_runs = state.setdefault("idle_runs", {})
    same_runs = state.setdefault("same_status_runs", {})
    if (not logged) and (not status_changed):
        idle_runs[key] = n = idle_runs.get(key, 0) + 1
        if n >= env.max_idle_runs:
            block_ticket(path, tid, role, f"холостой ход ×{n}", state, now,
                         f"Запусков роли {role} подряд без записи и без смены статуса: {n} — холостой ход, "
                         "задача заблокирована, нужен CEO.")
            return True
    else:
        idle_runs.pop(key, None)
    if _same_status_loop(tkt, logged, status_changed, env.check_wait_for):
        same_runs[key] = n = same_runs.get(key, 0) + 1
        if n >= env.max_same_status_runs:
            block_ticket(path, tid, role, f"{n} запусков подряд: запись есть, статус {tkt.status} не меняется",
                         state, now,
                         f"Роль {role}: {n} запусков подряд оставляют запись, а статус остаётся {tkt.status} — "
                         "петля, задача заблокирована, нужен CEO.")
            return True
    elif status_changed or tkt.status not in ("in_progress", "waiting"):
        _reset_same_status(state, key)
    return False


def _retry_or_block(path: Path, tid: str, role: str, info: dict, state: dict, now, timed_out: bool, logged: bool,
                    stuck_todo: bool, env: Env) -> None:
    """(д) запуск завершился без пригодного результата — один повтор, затем blocked."""
    sess = state.setdefault("sessions", {}).setdefault(f"{tid}::{role}", {})
    if info.get("attempt", 0) < 1:
        if not logged:
            note = ("Предыдущий запуск не оставил новую запись в «## Лог» — обязательно допиши итог "
                    "командой tickets.py result (done|pr|accept|return|blocked|ask-owner|wait, --why); status и next поставит маршрут." if not timed_out else
                    "Предыдущий запуск не уложился в таймаут — сократи шаг и обязательно запиши итог.")
        else:
            note = ("Запись в «## Лог» есть, но status остался todo — обязательно смени статус (например "
                    "in_progress/waiting/done), иначе задача возьмётся в работу заново.")
        env.launch_run(path, role, state, now, reason="retry", attempt=info.get("attempt", 0) + 1, extra_note=note)
        sess["retries"] = sess.get("retries", 0) + 1
        return
    why = ("статус остался todo дважды подряд" if stuck_todo else
           "дважды не уложился в таймаут" if timed_out else
           "дважды не оставил запись в «## Лог»")
    block_ticket(path, tid, role, why, state, now, f"Запуск роли {role} — {why} — задача заблокирована, нужен CEO.")
    sess["retries"] = 0


def finish_role_part(tid: str, info: dict, state: dict, now, timed_out: bool, result: dict, env: Env) -> None:
    """Что делать с тикетом после запуска (деньги/модель уже учтены в _finish_run)."""
    role = info["role"]
    sess = state.setdefault("sessions", {}).setdefault(f"{tid}::{role}", {})  # retries — всегда на (задачу, роль)
    _remember_session(state, tid, role, info, timed_out, result, env.projects_dir)
    path = CFG.TICKETS_DIR / f"{tid}.md"
    if not path.exists():
        CFG.save_state(state)
        return
    tkt = _refuse_empty_wait(path, T.read_ticket(path), role, SIG._ceo_handoff_during_run(state, tid, info), now)
    # аудит 03.10: запуск ЛЮБОЙ роли (владелец, ревьюер, адресат `--next`) без новой записи — провал: один повтор,
    # затем blocked (раньше чужой холостой запуск молчал, и тикет `in_review` висел вечно).
    logged = role_logged(tkt, role, info)  # судья 27.09, п.7: таймаут сам по себе — не провал, если запись успела
    stuck_todo = role == tkt.owner and logged and tkt.status == "todo"  # отчиталась, но не увела статус с todo
    status_changed = tkt.status != info.get("status_at_launch")
    _count_review_return(state, tid, tkt, role, info, logged)
    if _blocked_by_loop(path, tid, role, tkt, state, now, logged, status_changed, env):
        sess["retries"] = 0
    elif logged and not stuck_todo:
        sess["retries"] = 0
    else:
        _retry_or_block(path, tid, role, info, state, now, timed_out, logged, stuck_todo, env)
    CFG.save_state(state)


def handle_api_error(tid: str, info: dict, state: dict, now, result: dict) -> None:
    """Ошибка API — не холостой ход и не провал роли: пауза запусков, счётчик подряд; N подряд — blocked с причиной;
    вход истёк — сразу сигнал CEO (повтор не поможет)."""
    role = info["role"]
    key = f"{tid}::{role}"
    text = SIG._first_line(str(result.get("result") or ""), 160) or f"api_error_status={result.get('api_error_status')}"
    counts = state.setdefault("api_errors", {})
    counts[key] = n = counts.get(key, 0) + 1
    until = now + (API_AUTH_PAUSE if _auth_error(result) else API_PAUSE)
    cur = state.get("limit_pause_until")
    if not cur or T.parse_dt(cur) < until:
        state["limit_pause_until"] = T.now_iso(until)
    state.setdefault("idle_runs", {}).pop(key, None)
    _reset_same_status(state, key)
    state.setdefault("sessions", {}).setdefault(key, {})["retries"] = 0
    print(f"[dispatch] {T.now_iso(now)} ошибка API на {tid} ({role}) x{n}: {text}; запуски на паузе до {T.now_iso(until)}",
          file=sys.stderr, flush=True)
    path = CFG.TICKETS_DIR / f"{tid}.md"
    if _auth_error(result):
        SIG.append_ceo_inbox(tid, "auth", f"{role}: вход Claude истёк — {text}. Войти заново (`claude /login`)", now)
    elif n >= MAX_API_ERRORS and path.exists():
        counts.pop(key, None)
        block_ticket(path, tid, role, f"ошибка API x{n}: {text}", state, now,
                      f"Запусков роли {role} подряд оборвала ошибка API: {n} — {text}. Задача заблокирована, нужен CEO.")
    CFG.save_state(state)
