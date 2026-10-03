"""Тесты `start.py` (`/rpv-start`): фоновый запуск диспетчера и сторожа, перезапуск по pid-замку.

Настоящие `dispatch.py`/`watch.py` не запускаются: вместо них — пробные скрипты во временной «папке плагина»
(пишут свой pid в замок и спят). Все запущенные процессы гасятся в конце теста.
"""
from __future__ import annotations

import atexit
import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
import warnings
from pathlib import Path

# проект-пустышка: импорт диспетчера не должен найти рабочий проект выше папки плагина
_SANDBOX = tempfile.mkdtemp(prefix="rpv-test-proj-")
os.makedirs(os.path.join(_SANDBOX, ".claude", "roles"))
os.environ["CLAUDE_PROJECT_DIR"] = _SANDBOX
atexit.register(shutil.rmtree, _SANDBOX, True)
sys.path.insert(0, str(Path(__file__).resolve().parent))
import dispatch as D  # noqa: E402
import start as S  # noqa: E402

FAKE = textwrap.dedent("""\
    import os, sys, time
    from pathlib import Path
    proj = Path(sys.argv[sys.argv.index("--project") + 1])
    d = proj / ".claude" / "dispatcher"
    d.mkdir(parents=True, exist_ok=True)
    (d / "NAME.pid").write_text(str(os.getpid()), encoding="utf-8")
    print("NAME started", flush=True)
    time.sleep(120)
    """)


def wait_alive(pid, want=True, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if bool(D._pid_alive(pid, "py")) == want:
            return True
        time.sleep(0.1)
    return False


class StartTests(unittest.TestCase):
    def setUp(self):
        warnings.simplefilter("ignore", ResourceWarning)   # Popen-объекты фоновых процессов не ждём — они живут дальше
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name).resolve()
        self.plugin = base / "plugin" / ".claude" / "dispatcher"
        self.plugin.mkdir(parents=True)
        for name in S.SERVICES:
            (self.plugin / f"{name}.py").write_text(FAKE.replace("NAME", name), encoding="utf-8")
        self.project = base / "proj"
        (self.project / ".claude" / "roles").mkdir(parents=True)
        self.pids = []
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for pid in self.pids:
            D._pid_kill(pid)
        for pid in self.pids:
            wait_alive(pid, want=False, timeout=5)
        self.tmp.cleanup()

    def launch(self):
        res = S.start(self.project, code_dir=self.plugin, settle=1.0)
        self.pids += [r["pid"] for r in res]
        return {r["name"]: r for r in res}

    def test_first_start_runs_both_in_background_and_logs_to_project(self):
        res = self.launch()
        self.assertEqual(list(res), ["dispatch", "watch"])
        state = self.project / ".claude" / "dispatcher"
        for name, r in res.items():
            self.assertTrue(r["ok"], (name, r))
            self.assertEqual(r["restarted"], 0)
            self.assertTrue(wait_alive(r["pid"]), name)
            self.assertEqual(r["log"], state / f"{name}.run.log")
            self.assertIn(f"{name} started", r["log"].read_text(encoding="utf-8"))     # вывод ушёл в лог проекта
            self.assertEqual(S.read_pid(state / f"{name}.pid"), r["pid"])               # замок = pid запущенного процесса

    def test_second_start_stops_running_pair_and_starts_fresh(self):
        first = self.launch()
        second = self.launch()
        for name in S.SERVICES:
            old, new = first[name]["pid"], second[name]["pid"]
            self.assertNotEqual(old, new)
            self.assertEqual(second[name]["restarted"], old, name)
            self.assertTrue(wait_alive(old, want=False), f"{name}: прежний процесс не остановлен")
            self.assertTrue(wait_alive(new), name)
            self.assertEqual(S.read_pid(self.project / ".claude" / "dispatcher" / f"{name}.pid"), new)

    def test_stale_lock_with_foreign_live_process_is_not_killed(self):
        """pid из замка переиспользован чужим python — его не трогаем, замок осиротел и просто убирается."""
        stranger = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.pids.append(stranger.pid)
        self.assertTrue(wait_alive(stranger.pid))
        state = self.project / ".claude" / "dispatcher"
        state.mkdir(parents=True)
        (state / "dispatch.pid").write_text(str(stranger.pid), encoding="utf-8")
        res = self.launch()
        self.assertEqual(res["dispatch"]["restarted"], 0)
        self.assertTrue(wait_alive(stranger.pid), "чужой процесс убит")
        self.assertNotEqual(S.read_pid(state / "dispatch.pid"), stranger.pid)

    def test_main_prints_pid_and_log_path(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = S.main(["--project", str(self.project)], code_dir=self.plugin, settle=1.0)
        out = buf.getvalue()
        for name in S.SERVICES:
            pid = S.read_pid(self.project / ".claude" / "dispatcher" / f"{name}.pid")
            self.pids.append(pid)
            self.assertIn(f"{name}: запущен, pid {pid}", out)
            self.assertIn(f"{name}.run.log", out)
        self.assertEqual(rc, 0)

    def test_outside_project_or_without_roles_is_refused_and_creates_nothing(self):
        plain = Path(self.tmp.name).resolve() / "plain"
        plain.mkdir()
        env = {k: v for k, v in os.environ.items() if k not in ("RPV_PROJECT", "CLAUDE_PROJECT_DIR")}
        for args, cwd in (([], plain), (["--project", str(plain)], plain)):
            done = subprocess.run([sys.executable, str(Path(S.__file__)), *args], cwd=str(cwd), env=env,
                                  capture_output=True, text=True, encoding="utf-8", timeout=120)
            self.assertEqual(done.returncode, 2, (args, done.stderr))
            self.assertIn("/rpv-init", done.stderr)
        self.assertEqual(list(plain.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
