#!/usr/bin/env python3
"""Сводка команды на веб-табло: тикеты проекта → view2 → POST на табло по RPV_BOARD (без ssh).

    python board_push.py            # один раз
    python board_push.py --loop 5   # каждые 5 с
    python board_push.py --dry      # напечатать сводку, не слать

RPV_BOARD — одна строка подключения из окна «+» (https://host/<токен>/#<ключ>); часть после «#» — ключ, он уходит только
в заголовке и в логах не печатается.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import project  # noqa: E402
import pulsedata as PD  # noqa: E402
import ticket  # noqa: E402

STATE = {"in_progress": "run", "in_review": "review", "waiting": "wait", "blocked": "bad", "needs_owner": "wait",
         "stopped": "bad", "done": "done", "todo": "todo", "backlog": "todo"}
TZ = timezone(timedelta(hours=4))


TAGS = {"pc": {"tag": "ПК", "name": "этот ПК", "color": "purple"}, "vps": {"tag": "VPS", "name": "сервер", "color": "teal"},
        "calc": {"tag": "СЧЁТ", "name": "сервер счёта", "color": "blue"},
        "col": {"tag": "КОЛ", "name": "сборщик", "color": "gray"}, "you": {"tag": "ВЫ", "name": "вы", "color": "amber"}}
MORDER = ("pc", "vps", "calc", "col")
ACTIVE = ("run", "review", "repair")


def _plan_steps(plan, st: str, who: str, title: str) -> list:
    """Шаги тикета: из плана (`plan.py`, пишут роли), плана нет — один шаг из состояния тикета. `after`/`wave` — как у alpha:
    после предыдущего шага по умолчанию, волна = 1 + максимум волн `after`."""
    raw = plan.get("steps") if isinstance(plan, dict) else None
    if not raw:
        return [_step(1, title, who, "pc", "", st)]
    steps = []
    for i, x in enumerate(raw, 1):
        if not isinstance(x, dict):
            continue
        sst = x.get("state") if x.get("state") in PD.STEP_STATES else "todo"
        steps.append(_step(len(steps) + 1, x.get("title") or "шаг", x.get("who") or who, x.get("on") if x.get("on") in PD.VALID_ON else "pc",
                           x.get("for") or "", sst, detail=x.get("detail"), started=PD.hhmm(x.get("started_at")),
                           finished=PD.hhmm(x.get("finished_at")), after=x.get("after")))
    for i, s in enumerate(steps, 1):
        a = s["after"]
        if a is None:
            a = [i - 1] if i > 1 else []
        a = sorted({v for v in a if isinstance(v, int) and not isinstance(v, bool) and 1 <= v < i})
        s["after"] = a
        s["wave"] = 1 + max((steps[v - 1]["wave"] for v in a), default=0)
    return steps


def _step(n, title, who, on, forr, state, **kw) -> dict:
    d = {"n": n, "title": title, "who": who, "on": on, "for": forr, "state": state, "after": [], "wave": 1, "pct": None,
         "detail": None, "eta_min": None, "started": None, "finished": None, "question": None}
    d.update(kw)
    return d


def _pstate(steps: list, st: str) -> str:
    """Состояние процесса: «ждёт» и «проблема» перебивают; иначе — по шагам; все готовы — готово."""
    states = [s["state"] for s in steps]
    for k in ("bad", "wait", "repair", "review", "run"):
        if k in states:
            return k
    return "done" if states and all(x == "done" for x in states) else st if st in ("done", "bad") else "todo"


def build_view2(tickets_dir, now: float | None = None, plans_dir=None) -> dict:
    now = time.time() if now is None else now
    tickets_dir = Path(tickets_dir)
    plans_dir = Path(plans_dir) if plans_dir else tickets_dir.parent / "pulse" / "plans"
    procs, counters = [], {"done": 0, "run": 0, "review": 0, "repair": 0, "wait": 0, "todo": 0, "bad": 0}
    used = set()
    for p in ticket.list_tickets(tickets_dir):
        try:
            t = ticket.read_ticket(p)
        except Exception:
            continue
        st = STATE.get(t.status, "todo")
        plan = PD.read_json(plans_dir / f"{t.id}.json")
        title = t.header.get("title", "")
        steps = _plan_steps(plan, st, t.owner, title)
        ps = _pstate(steps, st) if st not in ("done", "bad") else st
        counters[ps] += 1
        used.update(s["on"] for s in steps)
        now_i = next((s["n"] for s in steps if s["state"] in ACTIVE + ("wait", "bad")), None) or             next((s["n"] for s in steps if s["state"] == "todo"), len(steps))
        flow = [x for x in (plan or {}).get("flow", []) if isinstance(x, dict)] if isinstance(plan, dict) else []
        forp = (plan or {}).get("for") if isinstance(plan, dict) else None
        procs.append({"id": t.id, "n": len(procs) + 1, "title": (plan or {}).get("title") or title if isinstance(plan, dict) else title,
                      "summary": title if isinstance(plan, dict) else None, "wave": 1, "depends": [], "flow": flow,
                      "for": forp or None, "state": ps, "step_now": now_i, "steps_total": len(steps),
                      "eta_min": None, "wait_min": None, "steps": steps})
    open_ = [x for x in procs if x["state"] != "done"]
    open_.sort(key=lambda x: x["state"] == "todo")
    done = [x for x in procs if x["state"] == "done"]
    ordered = open_ + done
    wait = counters["wait"]
    machines = [{"id": m, **TAGS[m], "state": "ok", "jobs": []} for m in MORDER if m in used]
    return {"time": datetime.fromtimestamp(now, TZ).strftime("%H:%M"), "built_ts": now, "tick_s": 5,
            "headline": {"state": "bad" if counters["bad"] else "wait" if wait else "ok",
                         "text": f"ждёт вас: {wait}" if wait else "идёт"},
            "counters": counters, "progress": None, "tags": {m: TAGS[m] for m in used if m in TAGS}, "machines": machines,
            "waves": [{"n": 1, "label": "ТИКЕТЫ", "state": "run" if open_ else "done", "procs": [x["id"] for x in ordered]}],
            "processes": ordered, "questions": []}


def push(url: str, key: str, view2: dict, timeout: float = 10.0) -> int:
    if not url.rstrip("/").endswith("/ingest"):
        url = url.rstrip("/") + "/ingest"
    body = json.dumps({"view2": view2, "built_at": view2["time"]}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"X-Board-Key": key, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    root = project.resolve_project(argv)
    argv = project.strip_project_arg(argv)
    tdir = root / ".claude" / "tickets"
    if "--dry" in argv:
        print(json.dumps(build_view2(tdir), ensure_ascii=False, indent=1))
        return 0
    url, _, key = (project.env("BOARD") or "").partition("#")
    if not url or not key:
        print("задайте RPV_BOARD — строку подключения из окна «+» на табло", file=sys.stderr)
        return 2
    loop = float(argv[argv.index("--loop") + 1]) if "--loop" in argv else 0
    pid_file = root / ".claude" / "dispatcher" / "board_push.pid"
    if loop:  # служба: один экземпляр на проект, живёт, пока жив диспетчер
        import dispatch as D
        ok, msg = D.acquire_instance_lock(pid_file)
        if not ok:
            print(msg, file=sys.stderr)
            return 0
    started = time.time()
    try:
        return _loop(tdir, url, key, loop, root / ".claude" / "dispatcher" / "dispatch.pid", started)
    finally:
        if loop:
            import dispatch as D
            D.release_instance_lock(pid_file)


def _dispatcher_gone(dpid_file: Path, started: float, grace: float = 120.0) -> bool:
    """Диспетчера нет (pid-файла нет или процесс мёртв) дольше `grace` с после старта — табло гасится вместе с ним."""
    if time.time() - started < grace:
        return False
    import dispatch as D
    try:
        pid = int(dpid_file.read_text(encoding="utf-8").strip() or 0)
    except (OSError, ValueError):
        return True
    return not D._pid_alive(pid, "py")


def _loop(tdir, url, key, loop, dpid_file, started) -> int:
    while True:
        try:
            push(url, key, build_view2(tdir))
        except Exception as e:  # сеть/табло недоступны — не падать в цикле, ключ в текст не попадает
            print("табло: " + type(e).__name__, file=sys.stderr)
            if not loop:
                return 1
        if not loop:
            return 0
        if _dispatcher_gone(dpid_file, started):
            return 0
        time.sleep(loop)


if __name__ == "__main__":
    sys.exit(main())
