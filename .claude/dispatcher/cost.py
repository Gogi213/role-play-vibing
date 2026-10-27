"""Учёт стоимости запусков: суточная и по тикету сумма, окно «за час», разница modelUsage и стоимость именно этого
запуска при кумулятивном `--resume` — вынесено из dispatch.py (TK-011, ось 4). Состояние — словарь `state` от вызывающего;
модуль ни к dispatch, ни к путям проекта не обращается."""
from __future__ import annotations

from datetime import timedelta

import ticket as T


def _today(now) -> str:
    return now.strftime("%Y-%m-%d")


def _add_cost(state: dict, now, cost) -> None:
    if not cost:
        return
    daily = state.setdefault("daily_cost", {})
    day = _today(now)
    daily[day] = round(daily.get(day, 0.0) + float(cost), 6)


def ticket_cost_spent(state: dict, tid: str) -> float:
    return state.get("ticket_cost", {}).get(tid, 0.0)


def add_ticket_cost(state: dict, tid: str, cost) -> None:
    if not cost:
        return
    costs = state.setdefault("ticket_cost", {})
    costs[tid] = round(costs.get(tid, 0.0) + float(cost), 6)


def _record_cost_event(state: dict, now, cost) -> None:
    """Учёт: скользящее окно по ВСЕМ ролям — история (время, сумма), обрезаем с запасом; показатель «за час»
    в `tickets.py status`, никого не блокирует."""
    if not cost:
        return
    hist = state.setdefault("cost_history", [])
    hist.append([T.now_iso(now), float(cost)])
    cutoff = now - timedelta(hours=2)
    state["cost_history"] = [e for e in hist if T.parse_dt(e[0]) > cutoff]


def _rolling_hour_cost(state: dict, now) -> float:
    cutoff = now - timedelta(hours=1)
    return sum(c for t, c in state.get("cost_history", []) if T.parse_dt(t) > cutoff)


def _model_usage_number(v) -> float:
    """costUSD (или похожее поле) из одной записи modelUsage; неизвестная форма — 1.0 (сам факт есть)."""
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, dict):
        for k in ("costUSD", "cost_usd", "cost", "totalCostUsd", "total_cost_usd"):
            if k in v:
                try:
                    return float(v[k])
                except (TypeError, ValueError):
                    return 0.0
        return 1.0
    return 0.0


def _model_usage_diff(current: dict, previous: dict) -> dict:
    """Модели, реально задействованные В ЭТОМ прогоне — не за всю историю сессии (судья TK-002 п.1в):
    --resume отдаёт modelUsage кумулятивно, и историческая примесь (например Fable из самого первого,
    домодельного вызова сессии) иначе вечно всплывает как «не-opus», хотя в этом прогоне её не было —
    поймано 27.09 на живой сессии судьи (T-38/TK-002, claude-fable-5-1 не рос ни разу после в1.2)."""
    current = current or {}
    previous = previous or {}
    diff = {}
    for model, val in current.items():
        cur_n = _model_usage_number(val)
        prev_n = _model_usage_number(previous.get(model)) if model in previous else 0.0
        if model not in previous or (cur_n - prev_n) > 0:
            diff[model] = val
    return diff


def resolve_run_cost(state: dict, result: dict):
    """(стоимость ИМЕННО этого запуска, разница modelUsage, нужна_ли_пометка «как есть» в runs.log).
    Условия судьи TK-002 п.1: --resume отдаёт total_cost_usd/modelUsage КУМУЛЯТИВНО по всей истории
    session_id, не по этому запуску (живой пример 27.09: сессия судьи сходила с $6,80 → $8,29 → $8,74
    кумулятивных; реальные траты запусков — $1,49 и $0,45 — записывались как $8,29 и $8,74, отсюда
    ложная часовая пауза $19,39/ч при реальных ≈ $4,30/ч). (а) разница — по session_id, не по
    хранилищу роли/задачи (переживает ротацию иначе — новый id начинает с нуля сам по себе, так как
    для него нет прошлого итога). (б) нет прошлого итога ИЛИ разница < 0 → берём итог как есть,
    помечаем. Вызывающий должен получить `result` только когда `total_cost_usd` присутствует —
    отсутствие JSON целиком (таймаут/убит) обрабатывается отдельно, см. _finish_run."""
    raw_cost = float(result.get("total_cost_usd"))
    raw_usage = result.get("modelUsage") or {}
    session_id = result.get("session_id")
    if not session_id:
        return raw_cost, raw_usage, True
    seen_costs = state.setdefault("session_cost_seen", {})
    seen_usage = state.setdefault("session_model_usage_seen", {})
    prev_cost = seen_costs.get(session_id)
    prev_usage = seen_usage.get(session_id, {})
    if prev_cost is None or (raw_cost - prev_cost) < 0:
        cost, note = raw_cost, True
    else:
        cost, note = raw_cost - prev_cost, False
    usage_diff = _model_usage_diff(raw_usage, prev_usage)
    seen_costs[session_id] = raw_cost
    seen_usage[session_id] = raw_usage
    return cost, usage_diff, note
