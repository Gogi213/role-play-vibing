#!/usr/bin/env python3
"""Клиент шины (TK-045). post() не бросает: при недоступности шины событие уходит в spool и дошлётся со следующим
успешным вызовом (id события — ключ дедупа на шине). CLI: busclient.py send <адрес> [--payload JSON] [--id ID] [--strict]."""
import argparse
import json
import os
import sys
import tempfile
import urllib.request
import uuid

DEFAULT_URL = ""  # шина выключена, пока не задан RPV_BUS_URL (README, «Шина событий»)


def _env(name, default=""):
    return os.environ.get("RPV_BUS_" + name) or default


def config():
    url = _env("URL") or DEFAULT_URL
    token = _env("TOKEN", "")
    for p in (_env("TOKEN_FILE"), os.path.expanduser("~/.rpv-bus-token")):
        if token:
            break
        if not p:
            continue
        try:
            with open(p, encoding="utf-8") as f:
                token = f.read().strip()
        except OSError:
            pass
    return url, token


def spool_path():
    return _env("SPOOL") or os.path.join(tempfile.gettempdir(), "rpv-bus-spool.jsonl")


def request(path, body=None, timeout=5):
    url, token = config()
    req = urllib.request.Request(url + path, data=None if body is None else json.dumps(body).encode("utf-8"),
                                 headers={"Authorization": "Bearer " + token})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _spool(ev):
    with open(spool_path(), "a", encoding="utf-8") as f:
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")


def flush_spool(timeout=5):
    sp = spool_path()
    tmp = f"{sp}.{os.getpid()}"
    try:
        os.replace(sp, tmp)
    except OSError:
        return
    with open(tmp, encoding="utf-8") as f:
        lines = [ln for ln in f.read().splitlines() if ln.strip()]
    os.remove(tmp)
    for i, ln in enumerate(lines):
        try:
            request("/event", json.loads(ln), timeout)
        except Exception:
            for rest in lines[i:]:
                with open(sp, "a", encoding="utf-8") as f:
                    f.write(rest + "\n")
            return


def post(addr, payload=None, eid=None, timeout=5, spool=True):
    """Результат шины {'seq','dup'} или None (событие в spool; spool=False — вызывающий сам идёт запасным путём). Никогда не бросает."""
    if _env("DISABLE"):
        return None
    ev = {"addr": addr, "payload": payload or {}, "id": eid or uuid.uuid4().hex}
    try:
        res = request("/event", ev, timeout)
    except Exception as e:
        print(f"[bus] {addr}: шина недоступна ({type(e).__name__})" + (", в spool" if spool else ""), file=sys.stderr)
        if spool:
            try:
                _spool(ev)
            except OSError:
                pass
        return None
    flush_spool(timeout)
    return res


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("send")
    s.add_argument("addr")
    s.add_argument("--payload", default="{}")
    s.add_argument("--id")
    s.add_argument("--strict", action="store_true")
    a = ap.parse_args()
    res = post(a.addr, json.loads(a.payload), a.id)
    if res is None and a.strict:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
