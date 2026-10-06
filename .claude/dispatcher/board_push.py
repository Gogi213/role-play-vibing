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
import view2 as V2  # noqa: E402

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
    """view2 как у alpha: тикеты + планы шагов + вопросы владельцу (V2.make)."""
    now = time.time() if now is None else now
    tickets_dir = Path(tickets_dir)
    pulse = tickets_dir.parent / "pulse"
    plans_dir = Path(plans_dir) if plans_dir else pulse / "plans"
    tickets = {}
    for p in ticket.list_tickets(tickets_dir):
        try:
            t = ticket.read_ticket(p)
        except Exception:
            continue
        tickets[t.id] = t
    plans = {}
    for p in sorted(plans_dir.glob("*.json")):
        d = PD.read_json(p)
        if isinstance(d, dict) and isinstance(d.get("steps"), list):
            plans[p.stem] = d
    allq = [q for q in (PD.read_json(p) for p in sorted((pulse / "questions").glob("q-*.json"))) if isinstance(q, dict)]
    v = V2.make(tickets, plans, allq, now)
    try:
        import plainify
        plainify.apply(v.get("processes") or v.get("procs") or [], pulse)
    except Exception as e:
        print("plainify: " + type(e).__name__, file=sys.stderr)
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "board"))
        import machines as M
        ms, tags = M.collect()
        have = {m["id"]: m for m in v.get("machines") or []}
        for m in ms:  # загрузка из machines.py поверх строк view2 (там cpu/mem пустые)
            if m["id"] in have:
                have[m["id"]].update({k: m[k] for k in ("cpu", "mem", "disk_mb_s") if m.get(k) is not None})
            else:
                v.setdefault("machines", []).append(m)
        v["tags"] = {**tags, **(v.get("tags") or {})}
    except Exception as e:  # ssh/платформа не должны ронять сводку
        print("машины: " + type(e).__name__, file=sys.stderr)
    _write_status(pulse, v)
    return v


def _write_status(pulse: Path, v: dict) -> None:
    """status.json для TUI (board.py) и MCP — атомарно."""
    try:
        pulse.mkdir(parents=True, exist_ok=True)
        tmp = pulse / "status.json.tmp"
        tmp.write_text(json.dumps({"view2": v, "built_at": v.get("time"), "built_ts": time.time()}, ensure_ascii=False), encoding="utf-8")
        tmp.replace(pulse / "status.json")
    except OSError:
        pass


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
