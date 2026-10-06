"""Прогон на выносливость (TK-076 п.2): настоящий диспетчер в подпроцессе + фейковые роли + сбои.

    python endurance.py [--rounds N | --hours H] [--idle-max S] [--seed K] [--keep]

Раунд: 7 тикетов, роли ведут себя по плану (ok / 429 / молчит / без статуса / waiting без условия / долгая), посреди
раунда диспетчер убивается жёстко (во время долгой роли и в паузе лимита) и поднимается заново. Критерий раунда:
все тикеты done, 0 blocked, у каждого done есть строка в ceo-inbox (0 потерянных сигналов), самый долгий простой
(«есть готовая работа, никто не работает», вне паузы лимита) ≤ --idle-max. Код возврата 0 — критерий выполнен."""
from __future__ import annotations

import argparse
import json
import os
import random
import socket
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import ticket as T  # noqa: E402

FAKE_ROLE = r'''
import json, os, re, sys, time
from pathlib import Path
base = Path(os.environ["EN_BASE"]); tid = os.environ["RPV_TICKET"]; role = os.environ["RPV_ROLE"]
alive = base / "alive" / f"{os.getpid()}"; alive.parent.mkdir(exist_ok=True); alive.write_text(tid)
plan_f = base / "plan" / f"{tid}.json"
plan = json.loads(plan_f.read_text()) if plan_f.exists() else ["ok"]
act = plan.pop(0) if plan else "ok"
plan_f.write_text(json.dumps(plan))
path = Path(os.environ["RPV_PROJECT"]) / ".claude" / "tickets" / f"{tid}.md"
def entry(status):
    text = path.read_text(encoding="utf-8")
    if status:
        text = re.sub(r"(?m)^status:.*$", f"status: {status}", text, count=1)
    if status == "waiting":
        text = re.sub(r"(?m)^wait_for:.*$", "wait_for:", text, count=1)
    text = text.rstrip("\n") + f"\n\n### 2099-01-01T00:00:00+04:00 {role}\nШаг ({act}).\n"
    path.write_text(text, encoding="utf-8")
try:
    if act.startswith("slow"):
        time.sleep(float(act.split(":")[1])); act = "ok"
    if act == "ok":
        entry("done")
    elif act == "429":
        print(json.dumps({"is_error": True, "api_error_status": 429, "result": "You've hit your session limit"}))
        sys.exit(0)
    elif act == "silent":
        pass
    elif act == "nostatus":
        entry(None)
    elif act == "waitnocond":
        entry("waiting")
    print(json.dumps({"session_id": f"s-{tid}", "total_cost_usd": 0.0, "usage": {"input_tokens": 5}}))
finally:
    try: alive.unlink()
    except OSError: pass
'''

RUNNER = r'''
import subprocess, sys
sys.path.insert(0, sys.argv[1])
import dispatch as D
D._popen = lambda cmd, **kw: subprocess.Popen([sys.executable, sys.argv[2]] + list(cmd[1:]), **kw)
sys.exit(D.main(["--project", sys.argv[3]]))
'''

PLANS = [  # (владелец, план ролей на запуски подряд)
    ("engineer", ["ok"]),
    ("researcher", ["429", "ok"]),
    ("engineer", ["silent", "ok"]),
    ("researcher", ["nostatus", "ok"]),
    ("engineer", ["waitnocond", "ok"]),
    ("researcher", ["slow:5", "ok"]),
    ("engineer", ["slow:5", "ok"]),
]


class Harness:
    def __init__(self, base: Path, idle_max: float, seed: int):
        self.base, self.idle_max, self.rng = base, idle_max, random.Random(seed)
        self.proj = base / "proj"
        self.disp = self.proj / ".claude" / "dispatcher"
        self.tdir = self.proj / ".claude" / "tickets"
        self.proc = None
        self.idle_episodes: list[float] = []
        self.stop = threading.Event()
        (base / "plan").mkdir(parents=True, exist_ok=True)
        (base / "alive").mkdir(exist_ok=True)
        self.fake = base / "fake_role.py"
        self.fake.write_text(FAKE_ROLE, encoding="utf-8")
        self.runner = base / "runner.py"
        self.runner.write_text(RUNNER, encoding="utf-8")
        self.disp.mkdir(parents=True, exist_ok=True)
        self.tdir.mkdir(parents=True, exist_ok=True)
        self.env = dict(os.environ, EN_BASE=str(base), PYTHONIOENCODING="utf-8", RPV_BUS_DISABLE="1",
                        RPV_DISPATCH_INTERVAL="1", RPV_DISPATCH_MIN_GAP_S="1", RPV_DISPATCH_MAX_PARALLEL="4",
                        RPV_DISPATCH_ROLE_PARALLEL="engineer:3,researcher:3", RPV_DISPATCH_TIMEOUT="60",
                        RPV_DISPATCH_MAX_RUNS_PER_TICKET_HOUR="1000", RPV_DISPATCH_MAX_SAME_STATUS_RUNS="1000")
        self.log_fh = open(self.disp / "endurance-dispatch.log", "a", encoding="utf-8")

    # --- шина (настоящий bus.py на свободном порту) ---
    def start_bus(self):
        if not hasattr(self, "bus_port"):
            with socket.socket() as sk:
                sk.bind(("127.0.0.1", 0))
                self.bus_port = sk.getsockname()[1]
            (self.base / "bus-token").write_text("endurance", encoding="utf-8")
            self.env.pop("RPV_BUS_DISABLE", None)
            self.env.update(RPV_BUS_URL=f"http://127.0.0.1:{self.bus_port}", RPV_BUS_TOKEN="endurance",
                            RPV_BUS_SPOOL=str(self.base / "spool.jsonl"), RPV_BUS_SNAPSHOT_S="3")
            for k in ("RPV_BUS_URL", "RPV_BUS_TOKEN", "RPV_BUS_SPOOL"):
                os.environ[k] = self.env[k]
            os.environ.pop("RPV_BUS_DISABLE", None)
        self.bus = subprocess.Popen(
            [sys.executable, str(HERE.parent / "bus" / "bus.py"), "--db", str(self.base / "bus.db"),
             "--routes", str(HERE.parent / "bus" / "routes.json"), "--token-file", str(self.base / "bus-token"),
             "--host", "127.0.0.1", "--port", str(self.bus_port)], stdout=self.log_fh, stderr=subprocess.STDOUT)
        end = time.time() + 15
        while time.time() < end:
            try:
                with socket.create_connection(("127.0.0.1", self.bus_port), timeout=1):
                    return
            except OSError:
                time.sleep(0.2)

    def kill_bus(self):
        if getattr(self, "bus", None) and self.bus.poll() is None:
            self.bus.kill()
            self.bus.wait(timeout=10)

    def inbox(self) -> str:
        f = self.disp / "ceo-inbox.md"
        return f.read_text(encoding="utf-8") if f.exists() else ""

    # --- диспетчер ---
    def start_dispatcher(self):
        self.proc = subprocess.Popen([sys.executable, str(self.runner), str(HERE), str(self.fake), str(self.proj),
                                      "dispatch.py"],  # имя в командной строке — присмотр узнаёт «наш» процесс
                                     env=self.env, stdout=self.log_fh, stderr=subprocess.STDOUT)

    def start_watcher(self):
        self.watch = subprocess.Popen([sys.executable, "-u", str(HERE / "watch.py"), "--project", str(self.proj)],
                                      env=self.env, stdout=self.log_fh, stderr=subprocess.STDOUT)

    def kill_watcher(self):
        if getattr(self, "watch", None) and self.watch.poll() is None:
            self.watch.kill()
            self.watch.wait(timeout=10)

    def watcher_fresh(self, max_age: float = 15.0) -> bool:
        try:
            beat = json.loads((self.disp / "watch-heartbeat.json").read_text(encoding="utf-8"))["ts"]
            return time.time() - datetime.fromisoformat(beat).timestamp() < max_age
        except Exception:
            return False

    def supervise_pass(self) -> dict:
        """Один проход настоящего supervise.run_once (его по таймеру ОС зовёт Планировщик/launchd/systemd);
        запуск служб подменён: диспетчер — через обвязку прогона, сторож — настоящий watch.py."""
        import supervise

        class Started:
            def __init__(self, pid):
                self.pid, self.how = pid, "endurance"

        def spawn(script, project, log, extra=()):
            if script.name == "watch.py":
                self.start_watcher()
                return Started(self.watch.pid)
            self.start_dispatcher()
            return Started(self.proc.pid)

        return supervise.run_once(self.proj, spawn=spawn, stop=supervise.S.stop_running)

    def kill_dispatcher(self):
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=10)
        (self.disp / "dispatch.pid").unlink(missing_ok=True)

    # --- наблюдатель простоя: считает независимо от диспетчера ---
    def observe(self):
        idle_since = None
        while not self.stop.is_set():
            try:
                runnable = any(T.read_ticket(p).status in ("todo", "in_progress") for p in T.list_tickets(self.tdir))
                busy = any((self.base / "alive").iterdir())
                st = json.loads((self.disp / "state.json").read_text(encoding="utf-8")) if (self.disp / "state.json").exists() else {}
                until = st.get("limit_pause_until")
                paused = bool(until and datetime.fromisoformat(until) > datetime.now().astimezone())
            except Exception:
                time.sleep(0.2)
                continue
            if runnable and not busy and not paused:
                idle_since = idle_since or time.time()
            elif idle_since:
                self.idle_episodes.append(time.time() - idle_since)
                idle_since = None
            time.sleep(0.25)
        if idle_since:
            self.idle_episodes.append(time.time() - idle_since)

    def state(self) -> dict:
        try:
            return json.loads((self.disp / "state.json").read_text(encoding="utf-8"))
        except Exception:
            return {}

    def wait(self, cond, timeout: float, what: str):
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return True
            time.sleep(0.2)
        print(f"[endurance] таймаут: {what}", file=sys.stderr)
        return False

    def statuses(self) -> dict:
        return {p.stem: T.read_ticket(p).status for p in T.list_tickets(self.tdir)}

    def bus_faults(self, n: int) -> list:
        """Шина падает посреди раунда: диспетчер обязан заметить (bus-down), работать по таймеру, события, брошенные
        в простое, уйти из spool после возврата; ложное событие юнита не должно ничего блокировать."""
        sys.path.insert(0, str(HERE.parent / "bus"))
        import busclient
        sent = []
        self.wait(lambda: "[bus-up]" in self.inbox() or self.state().get("bus_url"), 10, "диспетчер видит шину")
        self.kill_bus()
        ok = self.wait(lambda: "[bus-down]" in self.inbox(), 20, "bus-down в ceo-inbox")
        addr = f"задача.TK-900{n}.к_ceo"
        busclient.post(addr, {"note": "в простое шины"}, f"en-spool-{n}-{time.time()}")  # уходит в spool
        sent.append(addr)
        time.sleep(self.rng.uniform(1, 3))
        self.start_bus()
        up = self.wait(lambda: "[bus-up]" in self.inbox(), 30, "bus-up в ceo-inbox")
        if not up:
            self.log_fh.flush()
            tail = (self.disp / "endurance-dispatch.log").read_text(encoding="utf-8", errors="replace").splitlines()
            print("[endurance] хвост лога диспетчера и шины:\n" + "\n".join(tail[-25:]), file=sys.stderr)
        busclient.post("машина.ghost.юнит.упал", {"unit": "нет-такого"}, f"en-ghost-{n}-{time.time()}")  # ложное событие
        sent.append("машина.ghost.юнит.упал")
        return sent if ok and up else sent + ["bus-down/up не замечены"]

    def reboot(self):
        """Имитация перезагрузки: диспетчер и все живые роли убиты без предупреждения, pid-файл и «alive» не чищены."""
        self.kill_dispatcher()
        for f in list((self.base / "alive").iterdir()):
            try:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/F", "/PID", f.name], capture_output=True)
                else:
                    os.kill(int(f.name), 9)
            except Exception:
                pass
        time.sleep(1)
        for f in list((self.base / "alive").iterdir()):
            f.unlink(missing_ok=True)
        self.start_dispatcher()

    # --- раунд ---
    def round(self, n: int, timeout: float = 120.0, mode: str = "base") -> dict:
        ids = []
        for owner, plan in PLANS:
            p = T.create_ticket(self.tdir, owner=owner, title=f"раунд {n}", prefix="TK-")
            (self.base / "plan" / f"{p.stem}.json").write_text(json.dumps(plan))
            ids.append(p.stem)
        self.idle_episodes.clear()
        self.stop.clear()
        obs = threading.Thread(target=self.observe, daemon=True)
        obs.start()
        if mode == "bus":
            self.start_bus()
        self.start_dispatcher()
        if mode == "watch":
            self.start_watcher()
        faults = []
        bus_ev = []
        watch_bad = False
        # 1) пауза лимита (429): убить диспетчер, «время прошло» — снять паузу в state.json, поднять
        if self.wait(lambda: bool(self.state().get("limit_pause_until")), 40, "пауза 429"):
            self.kill_dispatcher()
            st = self.state()
            st.pop("limit_pause_until", None)
            (self.disp / "state.json").write_text(json.dumps(st), encoding="utf-8")
            time.sleep(1)
            self.start_dispatcher()
            faults.append("429 + рестарт после сброса лимита")
        # 2) жёсткое убийство диспетчера, пока идёт долгая роль (она — сирота, диспетчер подхватывает по state.json)
        if self.wait(lambda: any((self.base / "alive").iterdir()), 30, "роль запущена"):
            time.sleep(self.rng.uniform(0.5, 2.0))
            self.kill_dispatcher()
            time.sleep(2)  # «присмотр» поднимает не мгновенно
            self.start_dispatcher()
            faults.append("kill dispatcher при живой роли")
        if mode == "bus":
            bus_ev = self.bus_faults(n)
            faults.append("шина: падение, spool, возврат, ложное событие юнита")
        if mode == "watch" and self.wait(self.watcher_fresh, 20, "сторож жив"):
            self.kill_watcher()
            self.kill_dispatcher()
            act = self.supervise_pass()
            back = self.wait(lambda: self.watcher_fresh(8) and self.proc.poll() is None, 30, "присмотр поднял сторож")
            faults.append(f"kill сторожа и диспетчера, присмотр: {act}")
            if not back or act.get("watch") != "start" or act.get("dispatch") != "start":
                faults.append("присмотр не поднял службы")
                watch_bad = True
        if mode == "reboot" and self.wait(lambda: any((self.base / "alive").iterdir()), 30, "роль запущена"):
            self.reboot()
            faults.append("перезагрузка: убиты диспетчер и роли, чистый перезапуск")
        done = self.wait(lambda: all(s == "done" for s in self.statuses().values()), timeout, "все тикеты done")
        time.sleep(2)  # дать диспетчеру дописать сигналы done
        self.stop.set()
        obs.join(5)
        self.kill_dispatcher()
        self.kill_watcher()
        self.kill_bus()
        st = self.statuses()
        inbox = self.inbox()
        lost = [t for t in ids if f"{t} [done]" not in inbox]
        lost += [e for e in bus_ev if f"[{e}]" not in inbox]
        blocked = [t for t, s in st.items() if s == "blocked"]
        res = {"round": n, "mode": mode, "done": done, "blocked": blocked, "lost_signals": lost, "faults": faults,
               "max_idle_s": round(max(self.idle_episodes, default=0.0), 1)}
        res["ok"] = bool(done and not blocked and not lost and not watch_bad and res["max_idle_s"] <= self.idle_max)
        return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--rounds", type=int, default=1)
    ap.add_argument("--hours", type=float, default=0.0)
    ap.add_argument("--idle-max", type=float, default=20.0)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--modes", default="base,bus,reboot,watch", help="сбои раундов по кругу: base | bus | reboot | watch")
    a = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    bad = 0
    deadline = time.time() + a.hours * 3600 if a.hours else None
    tmp = tempfile.mkdtemp(prefix="rpv-endurance-")
    h = Harness(Path(tmp), a.idle_max, a.seed)
    n = 0
    modes = a.modes.split(",")
    try:
        while True:
            n += 1
            r = h.round(n, mode=modes[(n - 1) % len(modes)])
            print(json.dumps(r, ensure_ascii=False), flush=True)
            bad += 0 if r["ok"] else 1
            if deadline is None and n >= a.rounds or deadline and time.time() >= deadline:
                break
    finally:
        h.kill_dispatcher()
        if not a.keep and not bad:
            import shutil
            h.log_fh.close()
            shutil.rmtree(tmp, ignore_errors=True)
        else:
            print(f"[endurance] след оставлен: {tmp}", file=sys.stderr)
    print(f"[endurance] раундов {n}, с нарушением {bad}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
