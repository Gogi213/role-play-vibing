"""Пульс, шаг 1 (TK-095, В-38): раз в тик диспетчер пишет `.claude/dispatcher/pulse.json` — клетки по 15 минут за сутки,
фраза-итог, «что сейчас», самая длинная пауза. Поля, коды причин и пороги — docs/pulse.md.
Класс клетки считается на момент клетки: каждый тик дописывает в `state["pulse_cells"]` состояние текущей клетки (худшее за клетку),
задним числом по тикетам ничего не пересчитывается. Причина «застряло» — код + порог; жёлтое («owner») — только слово владельца.
Модуль не запускает ролей и не ходит в сеть: GitHub уже спрашивали сторожа и CI-наблюдатель, читаем их файлы."""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import project as P  # noqa: E402
import project_paths as CFG  # noqa: E402
import runs as RUNS  # noqa: E402
import ticket as T  # noqa: E402

CELL_MIN = 15
CELLS = 96
FILE_NAME = "pulse.json"
CODES = ("dispatcher_dead", "unserved", "step_uncovered", "loop", "merged_unreleased", "ci_red")  # порядок = важность в фразе
# пороги по умолчанию, минуты (ключи `.claude/rpv.json`; менять — Судья, В-4/В-10); step_uncovered берёт `goal_idle_min` (TK-086)
LIMITS = {"pulse_dead_min": 2, "pulse_unserved_min": 20, "pulse_ci_red_min": 30, "pulse_loop_returns": 2, "pulse_loop_errors": 2,
          "pulse_loop_window_min": 60}
OWNER_WAIT = re.compile(r"слов\w*\s+владельц|владельц\w*\s+(?:ответ|слов)|owner", re.IGNORECASE)  # wait_for с условием «слово владельца»
PAUSE_MIN_CELLS = 2  # пауза короче двух клеток (30 мин) в строку не попадает
RANK = {"none": 0, "owner": 1, "stuck": 2, "work": 3}  # худшее за клетку; «работал» перекрывает остальное


def limit(key: str) -> int:
    v = P.setting(key, LIMITS[key], root=CFG.PROJECT_ROOT)
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else LIMITS[key]


def cell_index(now: datetime) -> int:
    return (now.hour * 60 + now.minute) // CELL_MIN


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _runs(now: datetime) -> list:
    """[(время, тикет, роль, ключи)] запусков ролей из runs.log (on_met — не запуск модели) за последние сутки."""
    try:
        lines = (CFG.DISPATCHER_DIR / "runs.log").read_text(encoding="utf-8").splitlines()[-4000:]
    except OSError:
        return []
    out = []
    for line in lines:
        parts = RUNS.split(line)
        if not parts or parts[2] == RUNS.ON_MET:
            continue
        try:
            out.append((T.parse_dt(parts[0]).astimezone(now.tzinfo), parts[1], parts[2], parts[3]))
        except ValueError:
            continue
    return out


def _tickets():
    """Все тикеты проекта; None — какой-то не читается (неизвестно, что в нём: причин не выдумываем)."""
    out = []
    for path in T.list_tickets(CFG.TICKETS_DIR):
        try:
            out.append(T.read_ticket(path))
        except Exception:
            return None
    return out


def _owner_items(tickets) -> list:
    """[(тикет, действие)] — только слово владельца: needs_owner и wait с условием «слово владельца»."""
    out = []
    for t in tickets:
        if t.status == "needs_owner":
            out.append((t.id, "ответить на вопрос"))
        elif t.status == "waiting" and OWNER_WAIT.search(t.header.get("wait_for") or ""):
            out.append((t.id, "дать слово"))
    return out


def _found(state: dict, tickets, now: datetime, runs: list) -> dict:
    """{ключ: (код, подробность)} — что стоит прямо сейчас, до порогов времени."""
    found = {}
    flags = state.get("dt_flags") or {}
    for flag, sub in (("limit_paused", "лимит сессии"), ("throttled", "тормоз запусков тикета"), ("ready_unserved", "роль не взята")):
        if flags.get(flag):
            found["unserved"] = ("unserved", f"тикет в очереди, слот свободен, запуска нет ({sub})")
            break
    try:
        import goal_idle  # TK-086: тот же сигнал, что будит CEO; дублировать нельзя
        for key, (_, tid, text) in goal_idle._due(tickets, now).items():
            found[f"step_uncovered:{key}"] = ("step_uncovered", text if tid in text else f"{tid}: {text}")
    except Exception as e:
        print(f"[pulse] step_uncovered: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
    open_ids = {t.id for t in tickets if t.status in T.OPEN_STATUSES}
    for tid in open_ids:
        if int((state.get("review_returns") or {}).get(tid, 0)) >= limit("pulse_loop_returns"):
            found[f"loop:{tid}"] = ("loop", f"{tid}: возвращён Судьёй {(state.get('review_returns') or {}).get(tid)} раз")
    window = now - timedelta(minutes=limit("pulse_loop_window_min"))
    errors = {}
    for ts, tid, _, kv in runs:
        if ts >= window and tid in open_ids and kv.get("status", "ok") != "ok":
            errors[tid] = errors.get(tid, 0) + 1
    for tid, n in errors.items():
        if n >= limit("pulse_loop_errors"):
            found[f"loop:{tid}"] = ("loop", f"{tid}: запуск падает и перезапускается ({n} за {limit('pulse_loop_window_min')} мин)")
    if "unreleased:main" in (_read_json(CFG.DISPATCHER_DIR / "watch-state.json").get("notified") or {}):
        found["merged_unreleased"] = ("merged_unreleased", "влито в main, не выпущено, очередь пуста")
    repo = P.setting("ci_repo", "", root=CFG.PROJECT_ROOT)
    ci, merge = _read_json(CFG.DISPATCHER_DIR / "ci-state.json"), _read_json(CFG.DISPATCHER_DIR / "merge-state.json")
    for t in tickets:
        pr = str(t.header.get("pr") or "").strip()
        if not (repo and pr and t.status in T.OPEN_STATUSES):
            continue
        entry, key = ci.get(f"{repo}#{pr}") or {}, f"{repo}#{pr}"
        if entry.get("state") in ("failure", "error") or entry.get("red"):
            found[f"ci_red:{t.id}"] = ("ci_red", f"{t.id}: CI красный на PR #{pr}")
        elif (merge.get(key) or {}).get("conflict"):
            found[f"ci_red:{t.id}"] = ("ci_red", f"{t.id}: PR #{pr} в конфликте с main")
    return found


def _active(state: dict, now: datetime, found: dict) -> list:
    """[(код, подробность, с какого времени)] — найденное, что держится дольше своего порога; отметки «с какого» — в state.json."""
    since = state.setdefault("pulse_since", {})
    for key in [k for k in since if k not in found]:
        since.pop(key)
    waits = {"unserved": limit("pulse_unserved_min"), "ci_red": limit("pulse_ci_red_min"), "step_uncovered": 0, "loop": 0,
             "merged_unreleased": 0}
    out = []
    for key, (code, detail) in found.items():
        began = T.parse_dt(since[key]) if since.get(key) else now
        since.setdefault(key, T.now_iso(now))
        if (now - began).total_seconds() / 60 >= waits[code]:
            out.append((code, detail, since[key]))
    return sorted(out, key=lambda r: CODES.index(r[0]))


def dispatcher_dead_min(state: dict, now: datetime) -> float:
    """Сколько минут нет тика; 0 — нет метки (первый запуск)."""
    last = P.parse_time(state["last_tick"], strict=True) if state.get("last_tick") else None
    return max((now - last).total_seconds() / 60, 0.0) if last else 0.0


def _record_cell(state: dict, now: datetime, cls: str) -> None:
    """Худшее состояние текущей клетки; пропущенные клетки между тиками при молчавшем диспетчере — stuck/dispatcher_dead."""
    day = now.date().isoformat()
    book = state.get("pulse_cells")
    if not isinstance(book, dict) or book.get("day") != day:
        book = state["pulse_cells"] = {"day": day, "cells": {}}
    cells, idx = book["cells"], cell_index(now)
    gap = dispatcher_dead_min(state, now)
    if gap > limit("pulse_dead_min") and state.get("last_tick"):
        prev = P.parse_time(state["last_tick"], strict=True)
        first = cell_index(prev) + 1 if prev.date() == now.date() else 0
        for i in range(first, idx):
            cells.setdefault(str(i), "stuck:dispatcher_dead")
    if cls.split(":")[0] and RANK[cls.split(":")[0]] >= RANK[(cells.get(str(idx)) or "none").split(":")[0]]:
        cells[str(idx)] = cls


def _cells(state: dict, runs: list, now: datetime) -> list:
    counts = [0] * CELLS
    for ts, *_ in runs:
        if ts.date() == now.date():
            counts[cell_index(ts)] += 1
    book = (state.get("pulse_cells") or {}).get("cells") or {}
    out = []
    for i in range(CELLS):
        rec = str(book.get(str(i)) or "")
        cls, _, why = rec.partition(":")
        cls = "work" if counts[i] or cls == "work" else (cls if cls in RANK else "none")
        cell = {"i": i, "n": counts[i], "c": cls}
        if cls == "stuck" and why:
            cell["why"] = why
        out.append(cell)
    return out


def _pause(cells: list, upto: int) -> str:
    """Самая длинная череда клеток без запусков до текущей (не включая её): «<с>–<по> · <минут> · <причина>» или пусто."""
    best, run = (0, 0), None
    for c in cells[:upto]:
        if c["c"] != "work":
            run = (run[0], c["i"] + 1) if run else (c["i"], c["i"] + 1)
            if run[1] - run[0] > best[1] - best[0]:
                best = run
        else:
            run = None
    if best[1] - best[0] < PAUSE_MIN_CELLS:
        return ""
    kinds = [c.get("why") or {"owner": "ждали слова владельца", "none": "делать было нечего"}.get(c["c"], "") for c in cells[best[0]:best[1]]]
    why = max(set(kinds), key=kinds.count)
    hhmm = lambda k: f"{k * CELL_MIN // 60:02d}:{k * CELL_MIN % 60:02d}"  # noqa: E731
    return f"{hhmm(best[0])}–{hhmm(best[1])} · {(best[1] - best[0]) * CELL_MIN} мин · {why}".strip(" ·")


def build(state: dict, now: datetime, tickets=None, runs=None) -> dict:
    """Содержимое pulse.json; побочный эффект — отметки `pulse_since`/`pulse_cells` в state."""
    tickets = _tickets() if tickets is None else tickets
    runs = _runs(now) if runs is None else runs
    project = Path(CFG.PROJECT_ROOT).name
    dead = dispatcher_dead_min(state, now) > limit("pulse_dead_min")
    active = [] if tickets is None else _active(state, now, _found(state, tickets, now, runs))
    if dead:
        active.insert(0, ("dispatcher_dead", f"диспетчер молчал {dispatcher_dead_min(state, now):.0f} мин", state["last_tick"]))
    owner = [] if tickets is None else _owner_items(tickets)
    working = sorted((r.get("started") or "", r.get("role") or "", tid) for tid, r in (state.get("active_runs") or {}).items())
    waiting = [t for t in (tickets or []) if t.status in T.OPEN_STATUSES]
    if working:
        cls = "work"
    elif active:
        cls = "stuck:" + active[0][0]
    elif owner:
        cls = "owner"
    else:
        cls = "none"
    if tickets is not None or working:  # проект не прочитан — клетку не красим «нечего делать»
        _record_cell(state, now, cls)
    if active:
        kind, phrase = "stuck", f"Застряло: {project} — {active[0][1]}" + (f" (и ещё {len(active) - 1})" if len(active) > 1 else "") + "."
    elif owner:
        kind, phrase = "owner", f"Нужны вы: {project} — {owner[0][1]} по {owner[0][0]}."
    else:
        kind, phrase = "ok", "Всё идёт. От вас ничего."
    cells = _cells(state, runs, now)
    return {"version": 1, "project": project, "day": now.date().isoformat(), "generated": T.now_iso(now), "state": kind, "phrase": phrase,
            "now": (f"{working[0][1]} · {working[0][2]}" if working else
                    (f"запусков нет, в очереди {len(waiting)}" if waiting else "очередь пуста")),
            "pause": _pause(cells, cell_index(now)),
            "reasons": [{"code": c, "detail": d, "since": s} for c, d, s in active], "owner": [{"ticket": t, "action": a} for t, a in owner],
            "cells": cells}


def update(state: dict, now: datetime) -> dict:
    """Тик диспетчера: построить и записать целиком (временный файл + замена: читатель не увидит половину)."""
    data = build(state, now)
    path = CFG.DISPATCHER_DIR / FILE_NAME
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)
    return data


def read(now: datetime) -> dict:
    """Для читателя (табло, шаг 2): файл как есть, но если диспетчер не обновлял его дольше `pulse_dead_min` — «Застряло»."""
    data = _read_json(CFG.DISPATCHER_DIR / FILE_NAME)
    try:
        age = (now - T.parse_dt(data["generated"])).total_seconds() / 60
    except (KeyError, ValueError):
        return {}
    if age > limit("pulse_dead_min"):
        data = dict(data, state="stuck", phrase=f"Застряло: {data.get('project', '')} — диспетчер не отвечает {age:.0f} мин.")
    return data
