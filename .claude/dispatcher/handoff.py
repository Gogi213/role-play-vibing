"""Ход у открытого тикета: передача CEO (`--next ceo`), инвариант «у открытого тикета есть ход», цикл ожиданий.

Вынесено из dispatch.py (TK-011). Идущие запуски (`RUNNING`) передаёт вызывающий — обёртка в dispatch.py."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import alarms as AL  # noqa: E402
import config as CFG  # noqa: E402
import project as P  # noqa: E402
import signals as SIG  # noqa: E402
import ticket as T  # noqa: E402
from signals import MAX_REVIEW_RETURNS, WAIT_NOTICE_EVERY, _review_returns  # noqa: E402


IDLE_GRACE_S = 600.0


INVARIANT_GRACE_S = 600.0  # TK-076 п.6: открытый тикет без хода дольше этого — владельца будим сами


def _waits_without_condition(tkt: T.Ticket, now) -> bool:
    if tkt.status != "waiting" or tkt.owner not in P.ROLES or tkt.next_role:
        return False
    if (tkt.header.get("wait_for") or "").strip():
        return False
    try:
        return (now - T.parse_dt(tkt.header.get("updated", ""))).total_seconds() > IDLE_GRACE_S
    except Exception:
        return False


def _drop_ceo_handoff(state: dict, tid: str) -> None:
    (state.get("ceo_handoffs") or {}).pop(tid, None)
    (state.get("ceo_handoff_reminded") or {}).pop(tid, None)


def expire_ceo_handoff(tkt: T.Ticket, state: dict, running) -> None:
    """Метка передачи CEO кончается на тике, как только тикет вышел из ожидания (waiting/in_review): CEO вернул его
    шапкой, done/stopped/blocked. Запуск любой роли и запись CEO снимают её в launch_run / _ceo_handoff_pending."""
    at = (state.get("ceo_handoffs") or {}).get(tkt.id)
    if not at or tkt.id in running or tkt.status in ("waiting", "in_review"):
        return
    # `--next ceo` и смена статуса роли — две команды: тик между ними видит старый статус при свежей метке. Статус
    # сменили после метки (updated новее) — это ход CEO/роли; иначе метку не трогаем, роль ещё допишет `waiting`.
    try:
        if tkt.status in ("todo", "in_progress") and T.parse_dt(tkt.header.get("updated", "")) <= T.parse_dt(at):
            return
    except (ValueError, TypeError):
        pass
    _drop_ceo_handoff(state, tkt.id)


def _ceo_handoff_pending(tkt: T.Ticket, state: dict, now) -> bool:
    """Передача CEO (`--next ceo`) — ход, пока CEO не ответил: метка живёт до первой записи CEO после неё (или смены
    статуса на не-ожидание). Без ответа дольше INVARIANT_GRACE_S — одна повторная строка CEO, владельца не будим."""
    handoffs = state.get("ceo_handoffs") or {}
    at = handoffs.get(tkt.id)
    if not at:
        return False
    try:
        at_dt = T.parse_dt(at)
    except (ValueError, TypeError):
        handoffs.pop(tkt.id, None)
        return False
    if tkt.status not in ("waiting", "in_review") or any(
            T.author_is(e.author, "ceo") and e.ts >= at_dt.replace(microsecond=0) for e in tkt.log):
        handoffs.pop(tkt.id, None)
        return False
    if (now - at_dt).total_seconds() > INVARIANT_GRACE_S:
        reminded = state.setdefault("ceo_handoff_reminded", {})
        if reminded.get(tkt.id) != at:
            reminded[tkt.id] = at
            SIG.append_ceo_inbox(tkt.id, "ждёт-ceo", f"передача CEO без ответа дольше {int(INVARIANT_GRACE_S // 60)} мин "
                             f"(владельца не будим, ход за CEO)", now)
    return True


def _no_move_reason(tkt: T.Ticket, now, state: "dict | None" = None) -> "str | None":
    """TK-076 п.6: у открытого тикета есть ход — роль запущена / next / годный wait_for / передача CEO. Здесь —
    тикет, которого decide() не будит, а хода нет дольше INVARIANT_GRACE_S (от `updated`): причина или None."""
    if tkt.next_role or tkt.status not in ("waiting", "in_review"):
        return None
    try:
        last = T.parse_dt(tkt.header.get("updated", ""))
        # `tickets.py comment` без --next `updated` не двигает: запись CEO и смена статуса — две команды, грейс считаем и от записи
        stamps = [e.ts for e in tkt.log[-1:] if e.ts <= now.replace(microsecond=0)]
        if (now - max([last] + stamps)).total_seconds() <= INVARIANT_GRACE_S:
            return None
    except Exception:
        return None
    if state is not None and _ceo_handoff_pending(tkt, state, now):
        return None
    if tkt.status == "waiting":
        spec = (tkt.header.get("wait_for") or "").strip()
        if not spec:
            return "waiting без wait_for и без next"
        if T.parse_wait_for(spec) is None:
            return f"wait_for не понят ({spec[:80]})"
        return None
    if tkt.reviewer in P.ROLES and _review_returns(state or {}, tkt.id) < MAX_REVIEW_RETURNS:  # предел — вопрос CEO
        return "in_review: последняя запись — ревьюера, статус не сменён, next не задан"
    return None


def enforce_move_invariant(path: Path, tkt: T.Ticket, state: dict, now) -> "T.Ticket":
    """Нарушение инварианта: будим владельца (status → in_progress, запись диспетчера) + строка CEO (раз на эпизод)."""
    reason = _no_move_reason(tkt, now, state)
    if not reason:
        return tkt
    AL.STALLED.append(AL._stall_title(tkt))
    key = f"{tkt.id}|{tkt.header.get('updated', '')}"
    sig = state.setdefault("invariant_signaled", {})
    if sig.get(tkt.id) != key:
        sig[tkt.id] = key
        SIG.append_ceo_inbox(tkt.id, "нет-хода", f"{reason} дольше {int(INVARIANT_GRACE_S // 60)} мин — "
                         + (f"владелец {tkt.owner} разбужен" if tkt.owner in P.ROLES else "владельца-роли нет, нужен CEO"), now)
    if tkt.owner not in P.ROLES:
        return tkt
    T.write_header_updates(path, {"status": "in_progress"}, now=now)
    T.append_log(path, "dispatcher", f"инвариант «у открытого тикета есть ход»: {reason} — владелец разбужен "
                 f"(status: in_progress). Следующий ход обязателен: работа / `--next <роль>` / "
                 f"`tickets.py wait <ID> <форма>` / вопрос владельцу.", now=now)
    return T.read_ticket(path)


def notify_wait_cycle(tkt: T.Ticket, state: dict, now) -> None:
    """TK-076 п.7: уже существующий цикл ожиданий ticket:<ID> → строка CEO (раз в сутки, от тикета с наименьшим ID)."""
    if tkt.status != "waiting":
        return
    parsed = T.parse_wait_for(tkt.header.get("wait_for", ""))
    if not parsed or parsed[0] != "ticket":
        return
    cycle = T.wait_cycle(CFG.TICKETS_DIR, tkt.id, parsed[1])
    if not cycle or tkt.id != min(cycle[:-1]):
        return
    AL.STALLED.append(AL._stall_title(tkt))
    notified = state.setdefault("ceo_wait_cycle_notified", {})
    prev = notified.get(tkt.id)
    if prev:
        try:
            if now - T.parse_dt(prev) < WAIT_NOTICE_EVERY:
                return
        except ValueError:
            pass
    SIG.append_ceo_inbox(tkt.id, "цикл-ожиданий", " -> ".join(cycle) + " — каждый ждёт следующего, никто не пойдёт; разорвать", now)
    notified[tkt.id] = T.now_iso(now)
