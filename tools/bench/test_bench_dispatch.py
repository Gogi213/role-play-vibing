"""E-003: `bench_dispatch.py` с `BENCH_PID_NAME` падал `NameError: procs` (после выноса `procs.py`, TK-011)."""
import os
import subprocess
import sys
import unittest
from pathlib import Path

BENCH = Path(__file__).resolve().parent


class BenchDispatchEnvTests(unittest.TestCase):
    def test_pid_name_reaches_procs(self):
        code = ("import runpy, sys; sys.path.insert(0, %r); import procs; runpy.run_path(%r, run_name='bench');"
                "print(procs.PID_EXPECT_NAME)") % (str(BENCH.parents[1] / ".claude" / "dispatcher"), str(BENCH / "bench_dispatch.py"))
        env = dict(os.environ, BENCH_PID_NAME="stub-image")
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "stub-image")


if __name__ == "__main__":
    unittest.main()
