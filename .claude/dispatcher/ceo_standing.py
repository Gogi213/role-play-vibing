"""Что стоит сейчас (TK-120, В-58): для хука CEO — список «TK-N — что — стоит N мин» вместо числа строк ceo-inbox.

Стоит: открытые `needs_owner` и `blocked`; принятые Судьёй, но не влитые (`done` с `pr:`, а PR в ci-state.json не закрыт);
«влито, не выпущено» (отметка сторожа `unreleased:<ветка>` в watch-state.json). Возраст — от `updated` тикета, у невыпущенного — от
`pulse_since` в state.json, иначе от отметки сторожа. Только читает; модуль берёт хук сессии CEO (`role_memory.ceo_standing_alert`)."""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

import project as P
import ticket as T

TITLE = 60      # знаков названия тикета в строке
MAX_LINES = 8   # строк в подсказке; остальное — числом
KINDS = {"needs_owner": "ждёт ответа владельца", "blocked": "заблокирован"}


def _json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _minutes(since, now: datetime) -> int:
    try:
        return max(0, int((now - T.parse_dt(str(since))).total_seconds() // 60))
    except (ValueError, TypeError):
        return 0


def _short(title: str) -> str:
    title = " ".join(str(title).split())
    return title if len(title) <= TITLE else title[:TITLE - 1].rstrip() + "…"


def standing(root, now: datetime | None = None) -> list:
    """Строки «TK-N — что — стоит N мин»; пусто — ничего не стоит. Нечитаемый тикет пропускается."""
    root, now = Path(root), now or datetime.now().astimezone()
    disp = root / ".claude" / "dispatcher"
    repo = str(P.setting("ci_repo", "", root=root) or "")
    ci = _json(disp / "ci-state.json") if repo else {}
    rows = []
    for path in T.list_tickets(root / ".claude" / "tickets"):
        try:
            tkt = T.read_ticket(path)
        except Exception:  # noqa: BLE001 — один битый тикет не гасит подсказку
            continue
        what = KINDS.get(tkt.status, "")
        if not what and tkt.status == "done":
            prs = re.findall(r"\d+", str(tkt.header.get("pr") or ""))
            if any(ci.get(f"{repo}#{n}") and not ci[f"{repo}#{n}"].get("closed") for n in prs):
                what = "принят, не влит"
        if what:
            rows.append((_minutes(tkt.header.get("updated"), now), f"{tkt.id} — {_short(tkt.header.get('title', ''))}: {what}"))
    watch = _json(disp / "watch-state.json").get("notified") or {}
    marker = next((k for k in watch if k.startswith("unreleased:")), "")
    if marker:
        entry = watch[marker] if isinstance(watch[marker], dict) else {"ts": watch[marker]}
        since = (_json(disp / "state.json").get("pulse_since") or {}).get("merged_unreleased") or entry.get("ts")
        rows.append((_minutes(since, now), f"{marker.split(':', 1)[1]} — влито, не выпущено"))
    return [f"{text} — стоит {m} мин" for m, text in sorted(rows, key=lambda r: -r[0])]


def render(lines: list) -> str:
    """Текст подсказки: пусто — одна строка «всё идёт»."""
    if not lines:
        return "[диспетчер] всё идёт"
    shown = lines[:MAX_LINES] + ([f"… и ещё {len(lines) - MAX_LINES}"] if len(lines) > MAX_LINES else [])
    return "[диспетчер] стоит сейчас:\n" + "\n".join(shown)
