"""Прогон на выносливость (TK-076 п.2): настоящий диспетчер в подпроцессе + фейковые роли + сбои.

    python endurance.py [--rounds N | --hours H] [--idle-max S] [--seed K] [--keep]

Раунд: 7 тикетов, роли ведут себя по плану (ok / 429 / молчит / без статуса / waiting без условия / долгая), посреди
раунда диспетчер убивается жёстко (во время долгой роли и в паузе лимита) и поднимается заново. Критерий раунда:
все тикеты done, 0 blocked, у каждого done есть строка в ceo-inbox (0 потерянных сигналов), самый долгий простой
(«есть готовая работа, никто не работает», вне паузы лимита) ≤ --idle-max. Код возврата 0 — критерий выполнен."""
from __future__ import annotations

import shutil
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
import urllib.request
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import ticket as T  # noqa: E402

FAKE_ROLE = r'''
import json, os, re, subprocess, sys, time
from datetime import datetime, timedelta
from pathlib import Path
base = Path(os.environ["EN_BASE"]); tid = os.environ["RPV_TICKET"]; role = os.environ["RPV_ROLE"]
alive = base / "alive" / f"{os.getpid()}"; alive.parent.mkdir(exist_ok=True); alive.write_text(tid)
(base / "roles").mkdir(exist_ok=True)
plan_f = base / "plan" / f"{tid}.json"
plan = json.loads(plan_f.read_text()) if plan_f.exists() else ["ok"]
act = plan.pop(0) if plan else "ok"
plan_f.write_text(json.dumps(plan))
with open(base / "roles" / f"{tid}.txt", "a", encoding="utf-8") as fh:
    fh.write(f"{role} {act}" + chr(10))
tk = os.environ["EN_TK"]
def cli(*a):
    subprocess.run([sys.executable, str(tk), "--project", os.environ["RPV_PROJECT"], *a], check=True, capture_output=True)
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
    if act == "ok" or act.startswith("after-"):
        entry("done")
    elif act in ("429", "429now"):  # 429now: метка = текущая минута (resetsAt уже прошёл к разбору)
        at = datetime.now().astimezone() + (timedelta(seconds=60) if act == "429" else timedelta(0))  # «resets H:MMam»: следующая минута, диспетчер добавит ещё минуту
        label = at.strftime("%I:%M%p").lstrip("0").lower()
        print(json.dumps({"is_error": True, "api_error_status": 429,
                          "result": f"You've hit your session limit · resets {label}"}))
        sys.exit(0)
    elif act.startswith("handoff:"):  # comment --next <роль>, потом правка шапки — как живой случай 16:04
        target = act.split(":")[1]
        cli("comment", tid, "--author", role, "--text", f"передаю {target}", "--next", target)
        time.sleep(2.5)  # диспетчер за это время делает тики при живой роли
        text = path.read_text(encoding="utf-8")
        text = re.sub(r"(?m)^status:.*$", "status: waiting", text, count=1)
        text = re.sub(r"(?m)^wait_for:.*$", "wait_for:", text, count=1)
        path.write_text(text, encoding="utf-8")
    elif act.startswith("waitfile:"):  # настоящее ожидание: файл появится позже (создаёт «мир» прогона)
        flag = base / "flags" / tid
        flag.parent.mkdir(exist_ok=True)
        flag.write_text(act.split(":")[1])
        cli("wait", tid, f"file:{base / 'ready' / tid}")
    elif act == "silent":
        pass
    elif act in ("nostatus", "noverdict"):  # noverdict: ревьюер пишет запись, статус in_review не меняет, next не ставит
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
D.PID_EXPECT_NAME = ""  # фейковая роль — python, а не «claude»: иначе подхват после kill диспетчера считает живую роль мёртвой и снимает метку передачи CEO
D._popen = lambda cmd, **kw: subprocess.Popen([sys.executable, sys.argv[2]] + list(cmd[1:]), **kw)
sys.exit(D.main(["--project", sys.argv[3]]))
'''

CEO_REPLY_S = 12.0  # дольше RPV_DISPATCH_INVARIANT_GRACE_S прогона: передача CEO — ход, владельца будить нельзя

PLANS = [  # (владелец, план ролей на запуски подряд)
    ("engineer", ["ok"]),
    ("researcher", ["429", "ok"]),
    ("engineer", ["silent", "ok"]),
    ("researcher", ["nostatus", "ok"]),
    ("engineer", ["waitnocond", "ok"]),
    ("researcher", ["slow:5", "ok"]),
    ("engineer", ["slow:5", "ok"]),
    ("engineer", ["handoff:judge", "after-handoff", "ok"]),
    ("researcher", ["handoff:ceo", "after-ceo"]),
    ("engineer", ["waitfile:6", "after-wait"]),
    ("researcher", ["429now", "ok"]),
    ("engineer", ["ok", "noverdict", "ok"], "judge"),  # п.6: ревьюер без вердикта и без next — владелец разбужен сам
]


class Harness:
    def __init__(self, base: Path, idle_max: float, seed: int):
        self.base, self.idle_max, self.rng = base, idle_max, random.Random(seed)
        self.proj = base / "proj"
        self.disp = self.proj / ".claude" / "dispatcher"
        self.tdir = self.proj / ".claude" / "tickets"
        self.proc = None
        self.idle_episodes: list[float] = []
        self.ceo_seen: set = set()
        self.ceo_at: dict = {}
        self.stop = threading.Event()
        (base / "plan").mkdir(parents=True, exist_ok=True)
        (base / "alive").mkdir(exist_ok=True)
        self.fake = base / "fake_role.py"
        self.fake.write_text(FAKE_ROLE, encoding="utf-8")
        self.runner = base / "runner.py"
        self.runner.write_text(RUNNER, encoding="utf-8")
        self.disp.mkdir(parents=True, exist_ok=True)
        self.tdir.mkdir(parents=True, exist_ok=True)
        self.env = dict(os.environ, EN_BASE=str(base), EN_TK=str(HERE / "tickets.py"), PYTHONIOENCODING="utf-8", RPV_BUS_DISABLE="1",
                        RPV_DISPATCH_INTERVAL="1", RPV_DISPATCH_MIN_GAP_S="1", RPV_DISPATCH_MAX_PARALLEL="4",
                        RPV_DISPATCH_ROLE_PARALLEL="engineer:3,researcher:3", RPV_DISPATCH_TIMEOUT="60",
                        RPV_DISPATCH_MAX_RUNS_PER_TICKET_HOUR="1000", RPV_DISPATCH_INVARIANT_GRACE_S="8", RPV_DISPATCH_MAX_SAME_STATUS_RUNS="1000")
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
        """Сигналы CEO: файл (запасной путь / без шины) + очередь `ceo` шины, прочитанная без ack."""
        f = self.disp / "ceo-inbox.md"
        text = f.read_text(encoding="utf-8") if f.exists() else ""
        if "RPV_BUS_URL" in self.env and "RPV_BUS_DISABLE" not in self.env:
            try:
                req = urllib.request.Request(self.env["RPV_BUS_URL"] + "/q/ceo?after=0&wait=0",
                                             headers={"Authorization": "Bearer " + self.env["RPV_BUS_TOKEN"]})
                with urllib.request.urlopen(req, timeout=3) as r:
                    for e in json.loads(r.read())["events"]:
                        pl = e.get("payload") or {}
                        tid = e["addr"].split(".")[1] if e["addr"].startswith("задача.") else e["addr"]
                        text += f"- {tid} [{pl.get('kind', '')}] [{e['addr']}] {pl.get('note', '')}\n"
            except Exception:
                pass
        return text

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

    def _snap_state(self, tag):
        src = self.disp / "state.json"
        if src.exists():
            self._snaps = getattr(self, "_snaps", 0) + (tag == "before")
            shutil.copyfile(src, self.disp / f"state.{tag}-kill-{self._snaps}.json")

    def kill_dispatcher(self):
        if self.proc and self.proc.poll() is None:
            self._snap_state("before")
            self.proc.kill()
            self.proc.wait(timeout=10)
            self._snap_state("after")
        (self.disp / "dispatch.pid").unlink(missing_ok=True)

    # --- наблюдатель простоя: считает независимо от диспетчера ---
    def observe(self):
        idle_since = None
        while not self.stop.is_set():
            try:
                self.world()
                runnable = any(self.is_ready(T.read_ticket(p)) for p in T.list_tickets(self.tdir))
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

    def is_ready(self, t) -> bool:
        """Есть готовая работа: todo/in_progress либо waiting с уже выполненным file:-условием."""
        if t.status in ("todo", "in_progress"):
            return True
        wf = str(t.header.get("wait_for") or "").strip()
        return t.status == "waiting" and wf.startswith("file:") and Path(wf[5:]).exists()

    def world(self):
        """Внешний мир: файл ожидания появляется через N с после просьбы роли; CEO возвращает тикет, переданный ему."""
        for flag in (self.base / "flags").glob("*"):
            ready = self.base / "ready" / flag.name
            if not ready.exists() and time.time() - flag.stat().st_mtime >= float(flag.read_text() or 5):
                ready.parent.mkdir(exist_ok=True)
                ready.write_text("go")
        inbox = self.inbox()
        for p in T.list_tickets(self.tdir):
            if f"{p.stem} [next-ceo]" in inbox and p.stem not in self.ceo_seen:
                self.ceo_seen.add(p.stem)
                self.ceo_at[p.stem] = time.time()
        for tid, at in list(self.ceo_at.items()):  # CEO отвечает позже грейса инварианта (8 с): 12 с, запись + возврат в работу
            if time.time() - at < CEO_REPLY_S:
                continue
            try:
                waiting = T.read_ticket(self.tdir / f"{tid}.md").status == "waiting"
            except ValueError:  # диспетчер пишет тикет в этот момент — на следующем обходе
                continue
            if waiting:
                T.append_log(self.tdir / f"{tid}.md", "ceo", "принято, продолжай")
                T.write_header_updates(self.tdir / f"{tid}.md", {"status": "todo"})
                del self.ceo_at[tid]

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
        out = {}
        for p in T.list_tickets(self.tdir):
            try:
                out[p.stem] = T.read_ticket(p).status
            except (OSError, ValueError):  # Windows: диспетчер/роль пишет тикет в этот момент — нет статуса «done» на этом обходе
                out[p.stem] = "?"
        return out

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
    def round(self, n: int, timeout: float = 300.0, mode: str = "base") -> dict:
        ids = []
        for owner, plan, *rev in PLANS:
            p = T.create_ticket(self.tdir, owner=owner, title=f"раунд {n}", prefix="TK-", reviewer=rev[0] if rev else None)
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
        # 1) пауза лимита (429): диспетчер убит и поднят посреди паузы; state не правим — после resetsAt он обязан
        # сам вернуться к работе (простой после resetsAt считает наблюдатель)
        if self.wait(lambda: bool(self.state().get("limit_pause_until")), 40, "пауза 429"):
            self.kill_dispatcher()
            time.sleep(1)
            self.start_dispatcher()
            faults.append("429: рестарт диспетчера посреди паузы, возобновление по resetsAt")
        # 2) жёсткое убийство диспетчера, пока идёт долгая роль (она — сирота, диспетчер подхватывает по state.json)
        if self.wait(lambda: any((self.base / "alive").iterdir()), 150, "роль запущена"):
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
        self.wait(lambda: all(f"{t} [done]" in self.inbox() for t in ids), 30, "сигналы done в ceo-inbox")  # диспетчер дописывает на следующем тике
        self.stop.set()
        obs.join(5)
        self.kill_dispatcher()
        self.kill_watcher()
        inbox = self.inbox()  # очередь шины читается до её остановки
        self.kill_bus()
        st = self.statuses()
        lost = [t for t in ids if f"{t} [done]" not in inbox]
        lost += [e for e in bus_ev if f"[{e}]" not in inbox]
        roles = {t: (self.base / "roles" / f"{t}.txt").read_text(encoding="utf-8").splitlines()
                 if (self.base / "roles" / f"{t}.txt").exists() else [] for t in ids}
        ho_judge, ho_ceo, wf = ids[7], ids[8], ids[9]
        if "judge after-handoff" not in roles[ho_judge]:
            lost.append(f"{ho_judge}: --next judge не запустил judge")
        if f"{ho_ceo} [next-ceo]" not in inbox:
            lost.append(f"{ho_ceo}: --next ceo не дошёл до ceo-inbox")
        if any(r.endswith("handoff:ceo") for r in roles[ho_ceo][1:]) or len(roles[ho_ceo]) < 2:
            lost.append(f"{ho_ceo}: после передачи CEO нет ровно одного возобновления ({roles[ho_ceo]})")
        if f"{ho_ceo} [нет-хода]" in inbox:
            lost.append(f"{ho_ceo}: владелец разбужен инвариантом, пока CEO не ответил ({roles[ho_ceo]})")
        if not any(r.endswith("after-wait") for r in roles[wf]):
            lost.append(f"{wf}: владелец не разбужен по wait_for file")
        refused = [t for t in ids if "waiting без wait_for и без next" in (self.tdir / f"{t}.md").read_text(encoding="utf-8")
                   and t in (ho_judge, ho_ceo)]
        lost += [f"{t}: после --next диспетчер отказал «waiting без условия»" for t in refused]
        nv = ids[11]
        if roles[nv] != ["engineer ok", "judge noverdict", "engineer ok", "judge ok"]:
            lost.append(f"{nv}: ревьюер без вердикта — владелец не разбужен инвариантом ({roles[nv]})")
        if f"{nv} [нет-хода]" not in inbox:
            lost.append(f"{nv}: нарушение инварианта не дошло до ceo-inbox")
        if lost:  # диагностика красного раунда в логе CI: тикет, записи ролей, строки ceo-inbox, метки передачи CEO
            for t in ids:
                if any(t in x for x in lost):
                    print(f"[endurance] разбор {t}: роли {roles.get(t)}; ceo_handoffs {self.state().get('ceo_handoffs')}; "
                          f"inbox {[l for l in inbox.splitlines() if t in l]}", file=sys.stderr)
                    print((self.tdir / f"{t}.md").read_text(encoding="utf-8")[-1800:], file=sys.stderr)
            dl = self.disp / "endurance-dispatch.log"
            if dl.exists():
                tail = dl.read_text(encoding="utf-8", errors="replace").splitlines()[-40:]
                print("[endurance] хвост лога диспетчера:\n" + "\n".join(tail), file=sys.stderr)
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
