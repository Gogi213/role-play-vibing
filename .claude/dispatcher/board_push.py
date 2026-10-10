#!/usr/bin/env python3
"""Сводка команды на веб-табло: тикеты проекта → view2 → POST на табло по RPV_BOARD (без ssh).

    python board_push.py            # один раз
    python board_push.py --loop 5   # каждые 5 с
    python board_push.py --dry      # напечатать сводку, не слать

RPV_BOARD — одна строка подключения из окна «+» (https://host/<токен>/#<ключ>); часть после «#» — ключ, он уходит только
в заголовке и в логах не печатается. Без RPV_BOARD сводка только пишется в `<проект>/.claude/pulse/status.json`
(его читает `.claude/board/mcp_server.py`).

Необязательно (каталог `.claude/board/`): RPV_MACHINES — загрузка машин по ssh (`machines.py`), RPV_PLAIN=0 — выключить человеческие
строки процессов от Haiku (`plainify.py`). Сбой любой из них кадр не роняет.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import project  # noqa: E402
import pulsedata as PD  # noqa: E402
import ticket  # noqa: E402
import view2 as V2  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "board"))  # machines.py, plainify.py — по желанию


def _pstate(steps: list, st: str) -> str:
    """Состояние процесса: «ждёт» и «проблема» перебивают; иначе — по шагам; все готовы — готово."""
    states = [s["state"] for s in steps]
    for k in ("bad", "wait", "repair", "review", "run"):
        if k in states:
            return k
    return "done" if states and all(x == "done" for x in states) else st if st in ("done", "bad") else "todo"


def build_view2(tickets_dir, now: float | None = None, plans_dir=None, wait: bool = False) -> dict:
    """view2 как у alpha: тикеты + планы шагов + вопросы владельцу (V2.make); плюс машины (RPV_MACHINES) и строки (Haiku, RPV_PLAIN=0 — без них)."""
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
    return _extras(V2.make(tickets, plans, allq, now), pulse, wait)


def _extras(view2: dict, pulse: Path, wait: bool = False) -> dict:
    """Машины по ssh (RPV_MACHINES) и человеческие строки (по умолчанию, RPV_PLAIN=0 — без них); ошибка любой из них кадр не роняет."""
    try:  # «этот ПК» всегда (RPV_PC=0 — без него) + RPV_MACHINES по ssh
        import machines
        machines.merge(view2, *machines.collect())
    except Exception as e:
        print("машины: " + type(e).__name__, file=sys.stderr)
    if project.env("PLAIN") != "0":  # строки Haiku по умолчанию; RPV_PLAIN=0 — выключить
        try:
            import plainify
            plainify.apply(view2, pulse / "plain-auto.json", wait=wait)
        except Exception as e:
            print("строки: " + type(e).__name__, file=sys.stderr)
    return view2


def write_status(pulse: Path, view2: dict) -> None:
    """Кадр для TUI и MCP: `<pulse>/status.json` = {"view2": …, "built_at": …}, замена файла целиком (читатель не видит половину)."""
    pulse.mkdir(parents=True, exist_ok=True)
    ticket.atomic_write_text(pulse / "status.json", json.dumps({"view2": view2, "built_at": view2["time"]}, ensure_ascii=False))


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
        url = key = ""
        print("RPV_BOARD не задан — кадр только пишется в .claude/pulse/status.json (строка подключения — в окне «+» на табло)",
              file=sys.stderr)
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
    pulse = Path(tdir).parent / "pulse"
    while True:
        try:
            v2 = build_view2(tdir, wait=not loop)  # разовый запуск ждёт строки модели, в цикле они придут на следующих кадрах
            write_status(pulse, v2)
            if url:
                push(url, key, v2)
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
