#!/usr/bin/env python3
"""Шина событий команды (TK-045, В-180): sqlite-журнал, маршруты, очереди с ack, удержание по блокерам. Только stdlib."""
import argparse
import fnmatch
import hmac
import json
import os
import sqlite3
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

SERVICE_PREFIX = "служба."
SNAPSHOT_ADDR = "служба.блокеры.снимок"
STALE_ADDR = "служба.не_обработано"
MAX_WAIT = 55
MAX_BODY = 1 << 20

SCHEMA = """
CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
  addr TEXT NOT NULL, ts REAL NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS deliveries(seq INTEGER NOT NULL, recipient TEXT NOT NULL, state TEXT NOT NULL,
  held_for TEXT, created REAL NOT NULL, first_sent REAL, acked_at REAL, stale INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(seq, recipient));
CREATE INDEX IF NOT EXISTS deliveries_q ON deliveries(recipient, state, seq);
CREATE TABLE IF NOT EXISTS blocked(tk TEXT PRIMARY KEY, reason TEXT, ts REAL);
"""


def tk_of(addr: str):
    parts = addr.split(".")
    return parts[1] if len(parts) > 2 and parts[0] == "задача" else None


class Bus:
    def __init__(self, db_path, routes_path, stale_after=600, clock=time.time):
        self.clock = clock
        self.routes_path = routes_path
        self.stale_after = stale_after
        self.cond = threading.Condition()
        self.db = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self._rules, self._mtime = [], None

    def _routes(self):
        m = os.stat(self.routes_path).st_mtime_ns
        if m != self._mtime:
            with open(self.routes_path, encoding="utf-8") as f:
                self._rules = json.load(f)["rules"]
            self._mtime = m
        return self._rules

    def _route(self, addr):
        to, hold = [], set()
        for r in self._routes():
            if fnmatch.fnmatchcase(addr, r["match"]):
                to += [x for x in r.get("to", []) if x not in to]
                hold.update(r.get("hold", []))
        return to, hold

    def _release(self, tk):
        self.db.execute("UPDATE deliveries SET state='pending' WHERE state='held' AND held_for=?", (tk,))

    def _control(self, addr, payload, now):
        tk = tk_of(addr)
        if tk and addr.endswith(".блокер.поставлен"):
            self.db.execute("INSERT OR REPLACE INTO blocked VALUES(?,?,?)", (tk, str(payload.get("reason", "")), now))
        elif tk and addr.endswith(".блокер.снят"):
            self.db.execute("DELETE FROM blocked WHERE tk=?", (tk,))
            self._release(tk)
        elif addr == SNAPSHOT_ADDR:
            new = payload.get("blocked", {})
            for (old,) in self.db.execute("SELECT tk FROM blocked").fetchall():
                if old not in new:
                    self.db.execute("DELETE FROM blocked WHERE tk=?", (old,))
                    self._release(old)
            for t, reason in new.items():
                self.db.execute("INSERT OR REPLACE INTO blocked VALUES(?,?,?)", (t, str(reason), now))

    def post(self, addr, payload=None, eid=None):
        eid = eid or uuid.uuid4().hex
        payload = payload or {}
        with self.cond:
            row = self.db.execute("SELECT seq FROM events WHERE id=?", (eid,)).fetchone()
            if row:
                return {"seq": row[0], "dup": True}
            now = self.clock()
            self.db.execute("BEGIN IMMEDIATE")
            try:
                seq = self.db.execute("INSERT INTO events(id,addr,ts,payload) VALUES(?,?,?,?)",
                                      (eid, addr, now, json.dumps(payload, ensure_ascii=False))).lastrowid
                self._control(addr, payload, now)
                tk = tk_of(addr)
                to, hold = self._route(addr)
                blocked = bool(tk and self.db.execute("SELECT 1 FROM blocked WHERE tk=?", (tk,)).fetchone())
                for rcp in to:
                    held = blocked and rcp in hold
                    self.db.execute("INSERT INTO deliveries(seq,recipient,state,held_for,created) VALUES(?,?,?,?,?)",
                                    (seq, rcp, "held" if held else "pending", tk if held else None, now))
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
            self.cond.notify_all()
        return {"seq": seq, "dup": False}

    def fetch(self, rcp, after=0, wait=0):
        end = time.monotonic() + wait
        with self.cond:
            while True:
                rows = self.db.execute(
                    "SELECT e.seq,e.id,e.addr,e.ts,e.payload FROM deliveries d JOIN events e ON e.seq=d.seq "
                    "WHERE d.recipient=? AND d.state='pending' AND d.seq>? ORDER BY d.seq LIMIT 100",
                    (rcp, after)).fetchall()
                left = end - time.monotonic()
                if rows or left <= 0:
                    break
                self.cond.wait(left)
            now = self.clock()
            for r in rows:
                self.db.execute("UPDATE deliveries SET first_sent=COALESCE(first_sent,?) WHERE recipient=? AND seq=?",
                                (now, rcp, r[0]))
        return [{"seq": s, "id": i, "addr": a, "ts": t, "payload": json.loads(p)} for s, i, a, t, p in rows]

    def ack(self, rcp, seqs):
        with self.cond:
            n = 0
            for s in seqs:
                n += self.db.execute("UPDATE deliveries SET state='acked',acked_at=? "
                                     "WHERE recipient=? AND seq=? AND state='pending'", (self.clock(), rcp, s)).rowcount
            return n

    def _old_pending(self):
        return [r for r in self.db.execute(
            "SELECT d.seq,d.recipient,e.addr,d.created,d.stale FROM deliveries d JOIN events e ON e.seq=d.seq "
            "WHERE d.state='pending' AND d.created<? ORDER BY d.seq", (self.clock() - self.stale_after,)).fetchall()
            if not r[2].startswith(SERVICE_PREFIX)]

    def stale(self):
        with self.cond:
            return [{"seq": s, "recipient": r, "addr": a, "age_s": int(self.clock() - c)}
                    for s, r, a, c, _ in self._old_pending()]

    def scan_stale(self):
        with self.cond:
            fresh = [r for r in self._old_pending() if not r[4]]
            for s, r, *_ in fresh:
                self.db.execute("UPDATE deliveries SET stale=1 WHERE seq=? AND recipient=?", (s, r))
        for s, r, a, c, _ in fresh:
            self.post(STALE_ADDR, {"recipient": r, "seq": s, "addr": a, "age_s": int(self.clock() - c)})
        return len(fresh)

    def health(self):
        with self.cond:
            last = self.db.execute("SELECT COALESCE(MAX(seq),0) FROM events").fetchone()[0]
            q = {f"{r}.{st}": n for r, st, n in self.db.execute(
                "SELECT recipient,state,COUNT(*) FROM deliveries WHERE state!='acked' GROUP BY 1,2")}
            b = {t: r for t, r in self.db.execute("SELECT tk,reason FROM blocked")}
        return {"ok": True, "last_seq": last, "queues": q, "blocked": b}


def make_handler(bus, token):
    tok = ("Bearer " + token).encode()

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _send(self, code, obj):
            body = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authed(self):
            if hmac.compare_digest(self.headers.get("Authorization", "").encode(), tok):
                return True
            self._send(401, {"error": "token"})
            return False

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                raise ValueError("body too large")
            return json.loads(self.rfile.read(n) or b"{}")

        def do_GET(self):
            u = urlparse(self.path)
            parts = [p for p in u.path.split("/") if p]
            if parts == ["health"]:
                return self._send(200, {"ok": True})
            if not self._authed():
                return
            qs = {k: v[0] for k, v in parse_qs(u.query).items()}
            try:
                if parts == ["stats"]:
                    return self._send(200, bus.health())
                if parts == ["stale"]:
                    return self._send(200, {"stale": bus.stale()})
                if len(parts) == 2 and parts[0] == "q":
                    ev = bus.fetch(parts[1], int(qs.get("after", 0)), min(float(qs.get("wait", 0)), MAX_WAIT))
                    return self._send(200, {"events": ev})
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            self._send(404, {"error": "no route"})

        def do_POST(self):
            if not self._authed():
                return
            path = urlparse(self.path).path
            try:
                b = self._body()
                if path == "/event":
                    return self._send(200, bus.post(b["addr"], b.get("payload"), b.get("id")))
                if path == "/ack":
                    return self._send(200, {"acked": bus.ack(b["recipient"], b["seqs"])})
            except (ValueError, KeyError, TypeError) as e:
                return self._send(400, {"error": str(e)})
            self._send(404, {"error": "no route"})

    return H


def serve(bus, token, host, port):
    srv = ThreadingHTTPServer((host, port), make_handler(bus, token))
    srv.daemon_threads = True

    def scanner():
        while True:
            time.sleep(30)
            bus.scan_stale()

    threading.Thread(target=scanner, daemon=True).start()
    return srv


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="/var/lib/rpv-bus/bus.db")
    ap.add_argument("--routes", default=os.path.join(here, "routes.json"))
    ap.add_argument("--token-file", default="/etc/rpv-bus/token")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--stale-after", type=int, default=600)
    a = ap.parse_args()
    with open(a.token_file, encoding="utf-8") as f:
        token = f.read().strip()
    serve(Bus(a.db, a.routes, a.stale_after), token, a.host, a.port).serve_forever()


if __name__ == "__main__":
    main()
