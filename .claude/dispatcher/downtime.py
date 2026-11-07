"""Счётчик простоя (TK-076 п.4): минуты в сутки, когда «есть готовая работа, никто не работает» и «роль ждёт без условия».
Считает диспетчер каждый тик; SLO — 10 мин/сутки (`dispatch.IDLE_SLO_MIN`), нарушение — тревога раз в сутки.
Состояние — в state.json под ключами `downtime` (по дням) и `dt_*` (флаги прошлого тика).
TK-112: ещё два класса — «влито, не выпущено» (`unreleased_s`, отметка сторожа в watch-state.json) и «CEO не разбужен»
(`unwoken_s`, непрочитанная строка очереди CEO); оба берутся из данных сторожа и побудки (`watch_flags`), предикатов здесь нет.
Часы, когда сессии CEO нет вовсе, в эти два класса не входят.
TK-121 (В-58): третий — «принят, не влит» (`unmerged_s`): принятый Судьёй PR не влит из-за красного CI; отметку `accepted_red_at`
в merge-state.json ставит `merge_rule` каждый проход (раз в ~60 с), от CEO она не зависит."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import ceo_inbox as CI
import merge_rule as MR
import project as P
import unreleased as UNR

KEEP_DAYS = 14
UNRELEASED_FRESH_S = 600.0  # отметка сторожа старше этого — сторож молчит или GitHub не отвечает: не считаем
UNMERGED_FRESH_S = 600.0  # отметка merge_rule старше этого — ci_watch стоит: не считаем
UNWOKEN_MAX_H = 12.0  # непрочитанная строка «не разбужен» старше — CEO очередь не читает, считаем его отсутствующим


def account(state: dict, now: datetime, *, ready_unserved: bool, work_present: bool, waiting_nocond: bool,
            poll_s: float, grace_s: float = 60.0, slo_min: float = 10.0, throttled: bool = False,
            limit_paused: bool = False, unreleased: bool = False, ceo_unwoken: bool = False, unmerged: bool = False):
    """Учесть интервал с прошлого тика по ЕГО флагам. Возвращает (день, запись дня, нарушено_впервые)."""
    prev = state.get("dt_last")
    prev_dt = P.parse_time(prev, strict=True) if prev else None
    day = now.date().isoformat()
    rec = state.setdefault("downtime", {}).setdefault(day, {"idle_s": 0.0, "wait_s": 0.0, "stall_s": 0.0, "throttle_s": 0.0, "limit_s": 0.0,
                                                                 "unreleased_s": 0.0, "unwoken_s": 0.0, "unmerged_s": 0.0, "alerted": False})
    if prev_dt is not None:
        dt = max(0.0, (now - prev_dt).total_seconds())
        flags = state.get("dt_flags") or {}
        if flags.get("ready_unserved"):
            rec["idle_s"] += dt
        elif flags.get("throttled"):
            rec["throttle_s"] = rec.get("throttle_s", 0.0) + dt  # тормоз MAX_RUNS_PER_TICKET_HOUR: наш простой, признак петли
        elif flags.get("limit_paused"):
            rec["limit_s"] = rec.get("limit_s", 0.0) + dt  # пауза лимита сессии — внешняя, вне SLO
        elif flags.get("work_present"):
            stall = max(0.0, dt - poll_s - grace_s)  # диспетчер молчал при работе в очереди/в запуске
            rec["stall_s"] += stall
        if flags.get("waiting_nocond"):
            rec["wait_s"] += dt
        if flags.get("unreleased"):  # очередь пуста, влитое не выпущено; «не разбужен» в ту же минуту не задваивается
            rec["unreleased_s"] = rec.get("unreleased_s", 0.0) + dt
        elif flags.get("ceo_unwoken"):
            rec["unwoken_s"] = rec.get("unwoken_s", 0.0) + dt
        if flags.get("unmerged"):  # принят, не влит: красный CI держит PR
            rec["unmerged_s"] = rec.get("unmerged_s", 0.0) + dt
    state["dt_last"] = now.isoformat(timespec="seconds")
    state["dt_flags"] = {"ready_unserved": ready_unserved, "work_present": work_present, "waiting_nocond": waiting_nocond,
                         "throttled": throttled, "limit_paused": limit_paused, "unreleased": unreleased,
                         "ceo_unwoken": ceo_unwoken, "unmerged": unmerged}
    for old in sorted(state["downtime"])[:-KEEP_DAYS]:
        del state["downtime"][old]
    breached = total_min(rec) > slo_min and not rec["alerted"]
    if breached:
        rec["alerted"] = True
    return day, rec, breached


def total_min(rec: dict) -> float:
    """Минуты в SLO — сумма корзин (минута может попасть в две: idle и wait); пауза лимита (limit_s) вне SLO."""
    return (rec.get("idle_s", 0) + rec.get("wait_s", 0) + rec.get("stall_s", 0) + rec.get("throttle_s", 0)
            + rec.get("unreleased_s", 0) + rec.get("unwoken_s", 0) + rec.get("unmerged_s", 0)) / 60.0


def _json(path: Path) -> dict:
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def _fail_lines(inbox_lines: list) -> list:
    """[(время, причина)] строк `[ceo-не-разбужен]` очереди CEO по порядку; строка без разбираемого времени пропущена."""
    marker = f"[{CI.FAIL_KIND}] "
    rows = []
    for ln in inbox_lines:
        head, sep, rest = ln.partition(marker)
        at = P.parse_time(head.lstrip("- ").split(" ")[0]) if sep else None
        if at is not None:
            rows.append((at, rest.split("; событие:", 1)[0].strip()))
    return rows


def unmerged_fresh(dispatcher_dir: Path, now: datetime) -> bool:
    """`merge_rule` видел принятый PR с красным CI не раньше UNMERGED_FRESH_S секунд назад (отметка в merge-state.json)."""
    at = P.parse_time(_json(Path(dispatcher_dir) / "merge-state.json").get(MR.RED_MARK))
    return at is not None and 0 <= (now - at).total_seconds() <= UNMERGED_FRESH_S


def watch_flags(dispatcher_dir: Path, now: datetime) -> dict:
    """Флаги трёх классов по файлам сторожа, очереди CEO и вливания. Нет CEO — первые два False (третий от CEO не зависит).
    Ошибка чтения — False, тик не падает."""
    d = Path(dispatcher_dir)
    unmerged = unmerged_fresh(d, now)
    session = _json(d / "ceo-session.json")
    if not (session.get("id") or session.get("name")):
        return {"unreleased": False, "ceo_unwoken": False, "unmerged": unmerged}
    lines, seen = CI.read(d / "ceo-inbox.md", d / CI.SEEN_NAME)
    present = max([t for t in (P.parse_time(session.get("at")), P.parse_time(CI.read_at(d / CI.SEEN_NAME))) if t],
                  default=None)  # `/ceo set` и показ очереди — CEO есть
    fails = _fail_lines(lines)
    absent = [at for at, why in fails if why == CI.ABSENT_WHY]
    if absent and (present is None or max(absent) > present):
        return {"unreleased": False, "ceo_unwoken": False, "unmerged": unmerged}
    unread = [at for at, why in _fail_lines(lines[seen:]) if why != CI.ABSENT_WHY]
    unwoken = bool(unread) and (now - min(unread)).total_seconds() <= UNWOKEN_MAX_H * 3600
    return {"unreleased": UNR.open_fresh(_json(d / "watch-state.json"), now, UNRELEASED_FRESH_S), "ceo_unwoken": unwoken, "unmerged": unmerged}
