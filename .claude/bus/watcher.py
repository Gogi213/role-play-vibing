#!/usr/bin/env python3
"""Сторож машины (TK-045): сам видит остановку юнитов tk*/rpv-* и ход заданий (~/rpv/progress/*.json) и шлёт события
на шину — ни юниты, ни progress-файлы трогать не нужно. Остановка: машина.<host>.юнит.остановлен (успех) / .упал;
задание: задача.<TK>.задание.старт|ход|готово, при падении юнита с файлом хода — задача.<TK>.задание.упало."""
import argparse
import glob
import json
import os
import subprocess
import time

import busclient

RUNNING = ("active", "activating", "reloading")


def systemctl(*args):
    return subprocess.run(["systemctl", *args], capture_output=True, text=True, timeout=20).stdout


def snapshot(patterns):
    out = systemctl("list-units", *patterns, "--all", "--type=service", "--plain", "--no-legend")
    cur = {}
    for ln in out.splitlines():
        p = ln.split()
        if len(p) >= 4:
            cur[p[0]] = p[2]
    return cur


def show(unit):
    d = {}
    for ln in systemctl("show", unit, "-p", "Result", "-p", "ExecMainStatus", "-p", "InvocationID").splitlines():
        k, _, v = ln.partition("=")
        d[k] = v
    return d


def read_progress(pattern):
    res = {}
    for p in glob.glob(pattern):
        try:
            with open(p, encoding="utf-8") as f:
                o = json.load(f)
            res[os.path.basename(p)[:-5]] = o
        except (OSError, ValueError):
            pass
    return res


def is_done(o):
    try:
        return float(o["total"]) > 0 and float(o["done"]) >= float(o["total"])
    except (KeyError, TypeError, ValueError):
        return False


class Watcher:
    def __init__(self, host, patterns, exclude, progress_glob, post=busclient.post, snap=snapshot, showf=show,
                 prog=read_progress, watch_file=os.path.expanduser("~/rpv/progress/watch.list"), exists=os.path.exists):
        self.watch_file, self.exists, self.files_seen = watch_file, exists, set()
        self.host, self.patterns, self.exclude = host, patterns, exclude
        self.pg, self.post, self.snap, self.showf, self.prog = progress_glob, post, snap, showf, prog
        self.running = {}   # unit -> InvocationID
        self.failed_seen = set()
        self.jobs = {}      # job -> (step, done)
        self.first = True

    def _units(self):
        return {u: s for u, s in self.snap(self.patterns).items() if not any(u.startswith(x) for x in self.exclude)}

    def tick(self, progress):
        events = []
        cur = self._units()
        for u, st in cur.items():
            if st in RUNNING and u not in self.running:
                self.running[u] = self.showf(u).get("InvocationID", "")
            if st == "failed" and (u, "f") not in self.failed_seen:
                self.failed_seen.add((u, "f"))
                if not self.first and u not in self.running:
                    events += self._stopped(u, self.showf(u))
        for u in list(self.running):
            if cur.get(u) in RUNNING:
                continue
            info = self.showf(u) if u in cur else {"Result": "success", "ExecMainStatus": "0"}
            events += self._stopped(u, info, self.running.pop(u))
            self.failed_seen.add((u, "f"))
        events += self._progress(progress)
        self.first = False
        return events

    def _stopped(self, unit, info, inv=None):
        inv = inv or info.get("InvocationID", "")
        ok = info.get("Result", "success") == "success"
        base = unit[:-8] if unit.endswith(".service") else unit
        payload = {"unit": unit, "host": self.host, "result": info.get("Result", "success"),
                   "exit": info.get("ExecMainStatus", "0")}
        ev = [(f"машина.{self.host}.юнит.{'остановлен' if ok else 'упал'}", payload,
               f"unit:{self.host}:{unit}:{inv}")]
        tk = (self.prog(self.pg).get(base) or {}).get("ticket")
        if tk and not ok:
            ev.append((f"задача.{tk}.задание.упало", payload, f"unit-task:{self.host}:{unit}:{inv}"))
        return ev

    def _progress(self, progress):
        ev = []
        for job, o in progress.items():
            tk, step, done = o.get("ticket"), o.get("step", ""), is_done(o)
            if not tk:
                continue
            old = self.jobs.get(job)
            self.jobs[job] = (step, done)
            if self.first:
                continue
            pl = {"job": job, "host": self.host, "step": step, "done": o.get("done"), "total": o.get("total")}
            stamp = o.get("updated", "")
            if done and not (old and old[1]):
                ev.append((f"задача.{tk}.задание.готово", pl, f"prog:{self.host}:{job}:done:{stamp}"))
            elif old is None:
                ev.append((f"задача.{tk}.задание.старт", pl, f"prog:{self.host}:{job}:start:{stamp}"))
            elif old[0] != step and not done:
                ev.append((f"задача.{tk}.задание.ход", pl, f"prog:{self.host}:{job}:step:{step}:{stamp}"))
        return ev

    def file_events(self):
        """Пути из watch_file (по строке; их дописывает диспетчер) — появление файла = событие; проверка os.path.exists
        по списку, без обхода каталогов."""
        ev = []
        try:
            with open(self.watch_file, encoding="utf-8") as f:
                paths = [ln.strip() for ln in f if os.path.isabs(ln.strip())]
        except OSError:
            return ev
        for p in paths:
            if p in self.files_seen or not self.exists(p):
                continue
            self.files_seen.add(p)
            try:
                stamp = int(os.stat(p).st_mtime)
            except OSError:
                stamp = 0
            ev.append((f"машина.{self.host}.файл.появился", {"path": p, "host": self.host},
                       f"file:{self.host}:{p}:{stamp}"))
        return ev

    def run_once(self):
        events = self.tick(self.prog(self.pg)) + self.file_events()
        for addr, payload, eid in events:
            self.post(addr, payload, eid, 5)
        return events


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True)
    ap.add_argument("--patterns", nargs="+", default=["rpv-*"])
    ap.add_argument("--exclude", nargs="*", default=["rpv-bus"])
    ap.add_argument("--progress-glob", default=os.path.expanduser("~/rpv/progress/*.json"))
    ap.add_argument("--interval", type=float, default=3.0)
    a = ap.parse_args()
    w = Watcher(a.host, a.patterns, a.exclude, a.progress_glob)
    while True:
        try:
            w.run_once()
        except Exception as e:
            print(f"[watcher] {type(e).__name__}: {e}", flush=True)
        time.sleep(a.interval)


if __name__ == "__main__":
    main()
