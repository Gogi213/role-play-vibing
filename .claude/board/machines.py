#!/usr/bin/env python3
"""Машины для табло: «этот ПК» (Linux/macOS/Windows, только stdlib) и серверы из RPV_MACHINES по ssh.

RPV_MACHINES = `id=user@host,id2=host2` (алиасы ssh); ключ — RPV_DECK_KEY / RPV_DECK_KNOWN_HOSTS.
Каталог файлов хода на серверах — RPV_PROGRESS_DIR. Без RPV_MACHINES — только «этот ПК».
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys

REMOTE = ("cat /proc/loadavg; grep -E '^(MemTotal|MemAvailable)' /proc/meminfo; nproc; "
          "grep -m1 '^cpu ' /proc/stat")


def _run(cmd, timeout=6) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def pc_load() -> dict:
    """{'cpu': %, 'mem': %} этого ПК; None, если не удалось (без /proc: sysctl/vm_stat на macOS, ctypes на Windows)."""
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
        free = sum(int(x) for k, x in re.findall(r"Pages (free|inactive|speculative):\s+(\d+)", vm))
        if total:
            mem = round(100 - free * page / total * 100)
    elif sys.platform == "win32":
        try:
            import ctypes

            class MS(ctypes.Structure):
                _fields_ = [("l", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [(n, ctypes.c_ulonglong) for n in "abcdefg"]
            s = MS()
            s.l = ctypes.sizeof(MS)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(s))
            mem = int(s.load)
        except Exception:
            pass
        if cpu is None:
            out = _run(["wmic", "cpu", "get", "loadpercentage"])
            nums = re.findall(r"\d+", out)
            cpu = int(nums[0]) if nums else None
    return {"cpu": cpu, "mem": mem}


def parse_remote(out: str) -> dict:
    """Разбор вывода REMOTE: load1/ядра → cpu %, MemAvailable/MemTotal → mem %."""
    lines = out.strip().splitlines()
    cpu = mem = None
    try:
        load1 = float(lines[0].split()[0])
        cores = int(next(l for l in lines if l.strip().isdigit()))
        cpu = min(100, round(load1 / cores * 100))
        m = {k: int(v) for k, v in re.findall(r"(MemTotal|MemAvailable):\s+(\d+)", out)}
        mem = round(100 - m["MemAvailable"] / m["MemTotal"] * 100)
    except (IndexError, ValueError, StopIteration, KeyError, ZeroDivisionError):
        pass
    return {"cpu": cpu, "mem": mem}


def _ssh(target: str) -> list:
    cmd = [shutil.which("ssh") or "ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6"]
    if os.environ.get("RPV_DECK_KNOWN_HOSTS"):
        cmd += ["-o", "UserKnownHostsFile=" + os.environ["RPV_DECK_KNOWN_HOSTS"]]
    if os.environ.get("RPV_DECK_KEY"):
        cmd += ["-i", os.environ["RPV_DECK_KEY"]]
    return cmd + [target, REMOTE]


def collect(spec: str | None = None) -> tuple[list, dict]:
    """(machines, tags) в формате VIEW2.md. Ошибка ssh — state down, цикл не падает."""
    spec = os.environ.get("RPV_MACHINES", "") if spec is None else spec
    pc = pc_load()
    machines = [{"id": "pc", "state": "ok", "load": "", "now": {"state": "idle", "text": ""}, "orphans": 0,
                 "cpu": pc["cpu"], "mem": pc["mem"], "disk_mb_s": None}]
    tags = {"pc": {"tag": "ПК", "name": "этот ПК", "color": "purple"}}
    colors = ("teal", "blue", "gray")
    for i, item in enumerate(x for x in spec.split(",") if "=" in x):
        mid, target = (s.strip() for s in item.split("=", 1))
        r = parse_remote(_run(_ssh(target), timeout=12))
        ok = r["cpu"] is not None
        machines.append({"id": mid, "state": "ok" if ok else "down", "load": "",
                         "now": {"state": "idle" if ok else "bad", "text": "" if ok else "нет связи"},
                         "orphans": 0, "cpu": r["cpu"], "mem": r["mem"], "disk_mb_s": None})
        tags[mid] = {"tag": mid.upper()[:4], "name": mid, "color": colors[i % 3]}
    return machines, tags


if __name__ == "__main__":
    import json
    print(json.dumps(collect(), ensure_ascii=False))
