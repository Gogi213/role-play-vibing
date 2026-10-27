"""Лимит сессии (429) и ошибки API: распознавание ответа и время сброса — вынесено из dispatch.py (TK-011, ось 4).
Паузы и счётчики подряд идущих ошибок ведёт dispatch (`_handle_api_error`, `unblock_limit_victims`)."""
from __future__ import annotations

import re
from datetime import datetime, timedelta

import ticket as T

LIMIT_LABEL_SLACK = timedelta(minutes=2)


def _limit_hit(result: dict) -> bool:
    text = str((result or {}).get("result") or "")
    return (result or {}).get("api_error_status") == 429 or "hit your session limit" in text


API_PAUSE = timedelta(minutes=5)  # ошибка API (обрыв сети, 5xx): запуски на паузе, по образцу лимита сессии


API_AUTH_PAUSE = timedelta(minutes=30)  # вход истёк: повтор бессмыслен, пока CEO не войдёт заново


MAX_API_ERRORS = 3  # подряд ошибок API у (задача, роль) → blocked с настоящей причиной


def _api_error(result: dict) -> bool:
    """Запуск оборвала ошибка API (обрыв ECONNRESET, 5xx, вход истёк), кроме 429 — это лимит (`_limit_hit`)."""
    r = result or {}
    if _limit_hit(r):
        return False
    return r.get("terminal_reason") == "api_error" or (bool(r.get("is_error")) and r.get("api_error_status") is not None)


def _auth_error(result: dict) -> bool:
    r = result or {}
    return r.get("api_error_status") == 401 or "Failed to authenticate" in str(r.get("result") or "")


def _limit_reset_at(result: dict, now, ref=None) -> datetime:
    """Ближайшее «resets 5am (TZ)» / «resets 3:30pm»; не разобрали — через час (проверим снова, пауза продлится).
    ref — момент ответа (конец запуска; по умолчанию now): метка раньше него — завтра, но метка в пределах
    LIMIT_LABEL_SLACK до ответа (минутное округление) — уже прошла: вернём момент <= now, паузы не будет."""
    m = re.search(r"resets\s+(\d{1,2})(?::(\d{2}))?\s*([ap]m)(?:\s*\(([^)]+)\))?", str((result or {}).get("result") or ""), re.I)
    if not m:
        return now + timedelta(hours=1)
    hour, minute = int(m.group(1)) % 12, int(m.group(2) or 0)
    if m.group(3).lower() == "pm":
        hour += 12
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(m.group(4)) if m.group(4) else now.tzinfo
    except Exception:
        tz = now.tzinfo
    local = (ref or now).astimezone(tz)
    at = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if at <= local - LIMIT_LABEL_SLACK:
        at += timedelta(days=1)
    return at + timedelta(minutes=1)


def _limit_paused(state: dict, now) -> bool:
    until = state.get("limit_pause_until")
    if not until:
        return False
    try:
        return now < T.parse_dt(until)
    except ValueError:
        return False
