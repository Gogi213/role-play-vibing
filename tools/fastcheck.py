#!/usr/bin/env python3
"""Быстрый прогон тестов плагина (TK-095): pytest -n auto на этом ПК и, если задан --calc, на сервере счёта через
планировщик (alsched submit, правило 06.10: голый запуск на сервере запрещён). Итог — одна строка ok/fail по машинам.

  python tools/fastcheck.py                      # только эта машина
  python tools/fastcheck.py --calc root@89.163.242.211 --ssh-key <ключ> --known-hosts <файл> [--cores 8 --mem 8]

Тесты лежат под .claude; рабочее дерево на сервер — tar по ssh в /tmp/rpv-fastcheck-<pid>."""
import argparse
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = ["-m", "pytest", ".claude", "-q", "-p", "no:cacheprovider"]
# endurance — по часам ~3 мин на раунд: отдельным процессом рядом с основным, иначе они одни держат круг (TK-095)
MAIN = BASE + ["-n", "auto", "-m", "not endurance", "--durations=15"]
ENDU = BASE + ["-n", "4", "-m", "endurance"]


def local():
    t = time.time()
    ps = [subprocess.Popen([sys.executable] + a, cwd=ROOT) for a in (MAIN, ENDU)]
    rc = max(p.wait() for p in ps)
    return rc, time.time() - t


def calc(a):
    ssh = ["ssh", "-i", a.ssh_key, "-o", f"UserKnownHostsFile={a.known_hosts}", a.calc]
    d = f"/tmp/rpv-fastcheck-{os.getpid()}"
    t = time.time()
    tar = subprocess.Popen(["git", "-C", ROOT, "archive", "HEAD"], stdout=subprocess.PIPE)
    up = subprocess.run(ssh + [f"mkdir -p {d} && tar -xf - -C {d}"], stdin=tar.stdout)
    if up.returncode:
        return up.returncode, time.time() - t
    cmd = (f"python3 /data/sched/alsched.py submit --cls prod --name rpv-fastcheck --max-runtime 600 "
           f"--cores {a.cores} --mem {a.mem} -- bash -c 'cd {d}; python3 -m pytest .claude -q -p no:cacheprovider "
           f"-n {a.cores} -m \"not endurance\" --durations=15 & python3 -m pytest .claude -q -p no:cacheprovider "
           f"-n 4 -m endurance & r=0; for j in $(jobs -p); do wait $j || r=1; done; exit $r'")
    rc = subprocess.run(ssh + [cmd]).returncode
    subprocess.run(ssh + [f"rm -rf {d}"])
    return rc, time.time() - t


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--calc")
    p.add_argument("--ssh-key")
    p.add_argument("--known-hosts")
    p.add_argument("--cores", type=int, default=8)
    p.add_argument("--mem", type=int, default=8)
    a = p.parse_args()
    res = {"pc": local()}
    if a.calc:
        res["calc"] = calc(a)
    print(" | ".join(f"{k}: {'ok' if rc == 0 else 'fail'} {s:.0f} с" for k, (rc, s) in res.items()))
    return 0 if all(rc == 0 for rc, _ in res.values()) else 1


if __name__ == "__main__":
    sys.exit(main())