#!/usr/bin/env python3
"""Машины табло: по ssh (BatchMode) читает /proc и ход задач, отдаёт `machines` и `tags` в формате VIEW2.md.

    RPV_MACHINES=pc2=user@host,srv=host2     # id=адрес (алиас из ~/.ssh/config или user@host); пусто — машин нет
    RPV_MACHINES=old=!user@host              # «!» — машина выключена вами: не опрашивается, state=off
    RPV_DECK_KEY=<файл ключа>                # ssh -i (необязательно)
    RPV_DECK_KNOWN_HOSTS=<файл>              # ssh -o UserKnownHostsFile (необязательно)
    RPV_PROGRESS_DIR=/var/lib/progress       # на машине: *.json свежее 30 мин, {"done": N, "total": M, "step": "…", "unit": "…"}

ЦП, диск и память считаются по дельтам между двумя опросами: первый опрос после запуска даёт `cpu`/`disk_mb_s` = null.
Недоступная машина — state=down (ошибка ssh цикл не роняет). Только стандартная библиотека.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys

SSH_TIMEOUT_S = 8
COLORS = ("teal", "blue", "purple", "gray")
REMOTE = r'''d="$1"
echo "up $(cut -d' ' -f1 /proc/uptime)"
awk '/^cpu /{t=0;for(i=2;i<=9;i++)t+=$i;print "cpu",t,$5+$6}' /proc/stat
awk '$3~/^(sd[a-z]+|vd[a-z]+|xvd[a-z]+|nvme[0-9]+n[0-9]+)$/{s+=$6+$10}END{print "sect",s+0}' /proc/diskstats
awk '/^MemTotal/{t=$2}/^MemAvailable/{a=$2}END{print "mem",t,a}' /proc/meminfo
if [ -n "$d" ]; then find "$d" -maxdepth 1 -name '*.json' -mmin -30 2>/dev/null | sort | while IFS= read -r f; do
  printf 'job %s %s\n' "$(basename "$f" .json)" "$(tr -d '\n\r' < "$f" | head -c 2000)"; done; fi
'''
_PREV: dict = {}  # id → (up, cpu_total, cpu_idle, sectors) прошлого опроса


def parse_spec(spec: str | None) -> list[tuple[str, str, bool]]:
    """`id=адрес,id2=!адрес2` → [(id, адрес, выключена)]; строки без `=` и пустые адреса отбрасываются."""
    out = []
    for part in (spec or "").split(","):
        mid, sep, target = part.strip().partition("=")
        mid, target = mid.strip(), target.strip()
        off = target.startswith("!")
        target = target.lstrip("!").strip()
        if sep and mid and target and mid not in [m[0] for m in out]:
            out.append((mid, target, off))
    return out


def parse_output(text: str) -> dict | None:
    """Вывод REMOTE → {up, cpu:(всего, простой), sect, mem:(всего, доступно), jobs:{имя: json}}; не хватает строк — None."""
    got: dict = {"jobs": {}}
    try:
        for line in text.splitlines():
            k, _, rest = line.partition(" ")
            if k == "up":
                got["up"] = float(rest)
            elif k == "cpu":
                got["cpu"] = tuple(float(x) for x in rest.split()[:2])
            elif k == "sect":
                got["sect"] = float(rest)
            elif k == "mem":
                got["mem"] = tuple(float(x) for x in rest.split()[:2])
            elif k == "job":
                name, _, body = rest.partition(" ")
                try:
                    doc = json.loads(body)
                except ValueError:
                    continue
                if name and isinstance(doc, dict):
                    got["jobs"][name] = doc
    except ValueError:
        return None
    return got if all(k in got for k in ("up", "cpu", "sect", "mem")) else None


def _run(cmd: list, timeout: float = 6) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def pc_load() -> dict:
    """{'cpu': %, 'mem': %} этого ПК (None, если не узнать): Linux /proc, macOS sysctl/vm_stat, Windows ctypes."""
    cpus = os.cpu_count() or 1
    cpu = mem = None
    if hasattr(os, "getloadavg"):
        try:
            cpu = min(100, round(os.getloadavg()[0] / cpus * 100))
        except OSError:
            pass
    if sys.platform.startswith("linux"):
        try:
            m = dict(re.findall(r"(\w+):\s+(\d+)", open("/proc/meminfo").read()))
            mem = round(100 - int(m["MemAvailable"]) / int(m["MemTotal"]) * 100)
        except (OSError, KeyError):
            pass
    elif sys.platform == "darwin":
        total = int((_run(["sysctl", "-n", "hw.memsize"]) or "0").strip() or 0)
        vm = _run(["vm_stat"])
        page = int((re.search(r"page size of (\d+)", vm) or [0, 4096])[1])
        free = sum(int(x) for _, x in re.findall(r"Pages (free|inactive|speculative):\s+(\d+)", vm))
        if total:
            mem = round(100 - free * page / total * 100)
    elif sys.platform == "win32":
        try:
            import ctypes

            class MS(ctypes.Structure):
                _fields_ = [("l", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [(n, ctypes.c_ulonglong) for n in "abcdefg"]
            ms = MS()
            ms.l = ctypes.sizeof(MS)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms))
            mem = int(ms.load)
            if cpu is None:
                class FT(ctypes.Structure):
                    _fields_ = [("lo", ctypes.c_ulong), ("hi", ctypes.c_ulong)]
                i, k, u = FT(), FT(), FT()
                t = []
                for _ in range(2):
                    ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(i), ctypes.byref(k), ctypes.byref(u))
                    t.append(tuple((x.hi << 32) | x.lo for x in (i, k, u)))
                    if len(t) == 1:
                        import time
                        time.sleep(0.2)
                di, dk, du = (t[1][n] - t[0][n] for n in range(3))
                tot = dk + du
                cpu = round(100 * (1 - di / tot)) if tot > 0 else None
        except Exception:
            pass
    return {"cpu": cpu, "mem": mem}


def pc_view() -> dict:
    ld = pc_load()
    txt = "этот ПК"
    return {"id": "pc", "state": "ok", "orphans": 0, "cpu": ld["cpu"], "mem": ld["mem"], "disk_mb_s": None,
            "load": txt, "now": {"state": "idle", "text": txt}}


def _now(jobs: dict) -> dict:
    """«Что сейчас» по файлам хода: не дошедшие до total — run, иначе idle."""
    run = []
    for name, p in jobs.items():
        try:
            done, total = float(p["done"]), float(p["total"])
        except (KeyError, TypeError, ValueError):
            continue
        if done < total:
            run.append(f"{p.get('step') or name} {done:g}/{total:g} {p.get('unit') or ''}".strip())
    return {"state": "run", "text": "; ".join(run)} if run else {"state": "idle", "text": "простаивает"}


def machine_view(mid: str, sample: dict | None, prev: tuple | None = None, off: bool = False) -> dict:
    """Одна машина в формате VIEW2.md. `prev` — (up, cpu_всего, cpu_простой, сектора) прошлого опроса или None."""
    base = {"id": mid, "state": "ok", "orphans": 0, "cpu": None, "mem": None, "disk_mb_s": None}
    if off or sample is None:
        now = {"state": "off", "text": "выключена вами"} if off else {"state": "bad", "text": "нет связи"}
        return {**base, "state": "off" if off else "down", "load": now["text"], "now": now}
    total, avail = sample["mem"]
    base["mem"] = round(100 * (1 - avail / total)) if total else None
    if prev and sample["up"] > prev[0] and sample["cpu"][0] >= prev[1] and sample["sect"] >= prev[3]:
        dtot, didle, dt = sample["cpu"][0] - prev[1], sample["cpu"][1] - prev[2], sample["up"] - prev[0]
        base["cpu"] = round(100 * (1 - didle / dtot)) if dtot > 0 else None
        base["disk_mb_s"] = round((sample["sect"] - prev[3]) * 512 / dt / 1e6, 1)
    now = _now(sample["jobs"])
    return {**base, "load": now["text"], "now": now}


def ssh_sample(target: str, progress_dir: str, run=subprocess.run) -> dict | None:
    """Один опрос машины; любая ошибка ssh/разбора — None."""
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5"]
    if os.environ.get("RPV_DECK_KEY"):
        cmd += ["-i", os.environ["RPV_DECK_KEY"]]
    if os.environ.get("RPV_DECK_KNOWN_HOSTS"):
        cmd += ["-o", "UserKnownHostsFile=" + os.environ["RPV_DECK_KNOWN_HOSTS"]]
    cmd += [target, "sh", "-s", "--", progress_dir or ""]
    try:
        r = run(cmd, input=REMOTE, capture_output=True, text=True, encoding="utf-8", timeout=SSH_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_output(r.stdout) if r.returncode == 0 else None


def collect(sampler=ssh_sample) -> tuple[list, dict]:
    """(machines, tags): «этот ПК» (RPV_PC=0 — без него) + RPV_MACHINES по ssh. `sampler(адрес, каталог хода)` подменяется в тестах."""
    machines, tags = [], {}
    if os.environ.get("RPV_PC", "1") != "0":
        machines.append(pc_view())
        tags["pc"] = {"tag": "ПК", "name": "этот ПК", "color": "gray"}
    progress_dir = os.environ.get("RPV_PROGRESS_DIR", "")
    for i, (mid, target, off) in enumerate(parse_spec(os.environ.get("RPV_MACHINES"))):
        sample = None if off else sampler(target, progress_dir)
        machines.append(machine_view(mid, sample, _PREV.get(mid), off))
        if sample:
            _PREV[mid] = (sample["up"], sample["cpu"][0], sample["cpu"][1], sample["sect"])
        tags[mid] = {"tag": mid.upper()[:4], "name": mid, "color": COLORS[i % len(COLORS)]}
    return machines, tags


def merge(view2: dict, machines: list, tags: dict) -> dict:
    """Влить результат `collect` в view2: у машины из плана — метрики и связь (нагрузка из плана остаётся), новые — в конец;
    метки машин из плана не трогаются."""
    by = {m["id"]: m for m in view2.setdefault("machines", [])}
    for m in machines:
        cur = by.get(m["id"])
        if cur is None:
            view2["machines"].append(m)
            continue
        cur.update(state=m["state"], cpu=m["cpu"], mem=m["mem"], disk_mb_s=m["disk_mb_s"])
        if m["state"] != "ok" or (cur.get("now") or {}).get("state") == "idle":
            cur["now"] = m["now"]
    for mid, t in tags.items():
        view2.setdefault("tags", {}).setdefault(mid, t)
    return view2


if __name__ == "__main__":
    print(json.dumps(collect(), ensure_ascii=False, indent=1))
