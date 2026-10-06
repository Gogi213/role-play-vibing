"""Счётчик простоя (TK-076 п.4): минуты в сутки, когда «есть готовая работа, никто не работает» и «роль ждёт без условия».
Считает диспетчер каждый тик; SLO — RPV_IDLE_SLO_MIN (по умолчанию 10 мин/сутки), нарушение — тревога раз в сутки.
Состояние — в state.json под ключами `downtime` (по дням) и `dt_*` (флаги прошлого тика)."""
from __future__ import annotations

from datetime import datetime

KEEP_DAYS = 14


def account(state: dict, now: datetime, *, ready_unserved: bool, work_present: bool, waiting_nocond: bool,
            poll_s: float, grace_s: float = 60.0, slo_min: float = 10.0):
    """Учесть интервал с прошлого тика по ЕГО флагам. Возвращает (день, запись дня, нарушено_впервые)."""
    prev = state.get("dt_last")
    prev_dt = datetime.fromisoformat(prev) if prev else None
    day = now.date().isoformat()
    rec = state.setdefault("downtime", {}).setdefault(day, {"idle_s": 0.0, "wait_s": 0.0, "stall_s": 0.0, "alerted": False})
    if prev_dt is not None:
        dt = max(0.0, (now - prev_dt).total_seconds())
        flags = state.get("dt_flags") or {}
        if flags.get("ready_unserved"):
            rec["idle_s"] += dt
        elif flags.get("work_present"):
            stall = max(0.0, dt - poll_s - grace_s)  # диспетчер молчал при работе в очереди/в запуске
            rec["stall_s"] += stall
        if flags.get("waiting_nocond"):
            rec["wait_s"] += dt
    state["dt_last"] = now.isoformat(timespec="seconds")
    state["dt_flags"] = {"ready_unserved": ready_unserved, "work_present": work_present, "waiting_nocond": waiting_nocond}
    for old in sorted(state["downtime"])[:-KEEP_DAYS]:
        del state["downtime"][old]
    breached = total_min(rec) > slo_min and not rec["alerted"]
    if breached:
        rec["alerted"] = True
    return day, rec, breached


def total_min(rec: dict) -> float:
    return (rec.get("idle_s", 0) + rec.get("wait_s", 0) + rec.get("stall_s", 0)) / 60.0
