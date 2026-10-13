#!/usr/bin/env python3
"""Быстрый прогон тестов плагина (TK-095): pytest -n auto на этой машине. Итог — одна строка ok/fail.

  python tools/fastcheck.py

Тесты лежат под .claude. Нужен pytest-xdist (`python -m pip install pytest pytest-xdist`)."""
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CMD = [sys.executable, "-m", "pytest", ".claude", "-q", "-p", "no:cacheprovider", "-n", "auto", "--durations=15"]


def main():
    t = time.time()
    rc = subprocess.run(CMD, cwd=ROOT).returncode
    print(f"pc: {'ok' if rc == 0 else 'fail'} {time.time() - t:.0f} с")
    return rc


if __name__ == "__main__":
    sys.exit(main())
