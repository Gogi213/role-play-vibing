#!/usr/bin/env python3
"""Сводка команды на веб-табло: тикеты проекта → view2 → POST на RPV_BOARD_URL с ключом RPV_BOARD_KEY (без ssh).

    python board_push.py            # один раз
    python board_push.py --loop 5   # каждые 5 с
    python board_push.py --dry      # напечатать сводку, не слать

RPV_BOARD_URL — адрес приёма вида https://host/<токен>/ingest; ключ выдаёт «+» на табло и в логах не печатается.
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
import ticket  # noqa: E402

STATE = {"in_progress": "run", "in_review": "review", "waiting": "wait", "blocked": "bad", "needs_owner": "wait",
         "stopped": "bad", "done": "done", "todo": "todo", "backlog": "todo"}
TZ = timezone(timedelta(hours=4))


def build_view2(tickets_dir, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    procs, counters = [], {"done": 0, "run": 0, "review": 0, "repair": 0, "wait": 0, "todo": 0, "bad": 0}
    for p in ticket.list_tickets(tickets_dir):
        try:
            t = ticket.read_ticket(p)
        except Exception:
            continue
        st = STATE.get(t.status, "todo")
        counters[st] += 1
        procs.append({"id": t.id, "n": len(procs) + 1, "title": t.header.get("title", ""), "summary": None, "wave": 1,
                      "depends": [], "flow": [], "for": None, "state": st, "step_now": 1, "steps_total": 1,
                      "eta_min": None, "wait_min": None,
                      "steps": [{"n": 1, "title": t.header.get("title", ""), "who": t.owner, "on": "pc", "for": "",
                                 "state": st, "after": [], "wave": 1, "pct": None, "detail": None, "eta_min": None,
                                 "started": None, "finished": None, "question": None}]})
    open_ = [x for x in procs if x["state"] != "done"]
    open_.sort(key=lambda x: x["state"] == "todo")
    done = [x for x in procs if x["state"] == "done"]
    ordered = open_ + done
    wait = counters["wait"]
    return {"time": datetime.fromtimestamp(now, TZ).strftime("%H:%M"), "built_ts": now, "tick_s": 5,
            "headline": {"state": "bad" if counters["bad"] else "wait" if wait else "ok",
                         "text": f"ждёт вас: {wait}" if wait else "идёт"},
            "counters": counters, "progress": None, "tags": {}, "machines": [],
            "waves": [{"n": 1, "label": "ТИКЕТЫ", "state": "run" if open_ else "done", "procs": [x["id"] for x in ordered]}],
            "processes": ordered, "questions": []}


def push(url: str, key: str, view2: dict, timeout: float = 10.0) -> int:
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
    url, key = project.env("BOARD_URL"), project.env("BOARD_KEY")
    if not url or not key:
        print("задайте RPV_BOARD_URL и RPV_BOARD_KEY (ключ выдаёт «+» на табло)", file=sys.stderr)
        return 2
    loop = float(argv[argv.index("--loop") + 1]) if "--loop" in argv else 0
    while True:
        try:
            push(url, key, build_view2(tdir))
        except Exception as e:  # сеть/табло недоступны — не падать в цикле, ключ в текст не попадает
            print("табло: " + type(e).__name__, file=sys.stderr)
            if not loop:
                return 1
        if not loop:
            return 0
        time.sleep(loop)


if __name__ == "__main__":
    sys.exit(main())
