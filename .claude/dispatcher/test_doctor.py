import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import doctor as D  # noqa: E402


def iso(ts):
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


class DoctorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name).resolve()
        (self.project / ".claude" / "roles").mkdir(parents=True)
        (self.project / ".claude" / "tickets").mkdir()
        self.sd = self.project / ".claude" / "dispatcher"
        self.sd.mkdir()
        self.env = mock.patch.dict(os.environ, {k: "" for k in ("RPV_BUS_URL", "ALPHA_BUS_URL")})
        self.env.start()
        self.addCleanup(self.env.stop)

    def rows(self, **kw):
        kw.setdefault("installed", lambda p: True)
        return {r[0]: r for r in D.run_checks(self.project, **kw)}

    def test_clean_project_reports_everything_and_fails_on_services(self):
        rows = self.rows()
        self.assertEqual(rows["диспетчер"][1], D.FAIL)
        self.assertEqual(rows["сторож"][1], D.FAIL)
        self.assertEqual(rows["шина"][1], D.OFF)
        self.assertEqual(rows["присмотр ОС"][1], D.OK)
        self.assertIn("тикетов нет", rows["очередь"][2])
        self.assertEqual(rows["ошибки за сутки"][1], D.OK)
        self.assertEqual(D.main(["--project", str(self.project)]), 1)

    def test_services_ok_and_hung(self):
        now = time.time()
        (self.sd / "state.json").write_text(json.dumps({"last_tick": iso(now - 5)}), encoding="utf-8")
        (self.sd / "watch-heartbeat.json").write_text(json.dumps({"ts": iso(now - 5000)}), encoding="utf-8")
        with mock.patch.object(D.S, "is_ours", return_value=True), mock.patch.object(D.S, "read_pid", return_value=42):
            rows = self.rows(now=now)
        self.assertEqual(rows["диспетчер"][1], D.OK)
        self.assertEqual(rows["сторож"][1], D.FAIL)
        self.assertIn("завис", rows["сторож"][2])

    def test_scheduler_missing_warns_and_restarts_counted(self):
        now = time.time()
        (self.sd / "supervise.log").write_text(
            f"{iso(now - 100)} dispatch: start\n{iso(now - 3 * D.DAY)} старое\n", encoding="utf-8")
        rows = self.rows(now=now, installed=lambda p: False)
        self.assertEqual(rows["присмотр ОС"][1], D.WARN)
        self.assertIn("перезапусков за сутки: 1", rows["присмотр ОС"][2])

    def test_queue_flags_waiting_without_condition(self):
        t = self.project / ".claude" / "tickets"
        (t / "TK-001.md").write_text("---\nid: TK-001\nstatus: waiting\nwait_for: \n---\nx\n\n## Лог\n", encoding="utf-8")
        (t / "TK-002.md").write_text("---\nid: TK-002\nstatus: todo\n---\nx\n\n## Лог\n", encoding="utf-8")
        rows = self.rows()
        self.assertEqual(rows["ожидание без условия"][1], D.WARN)
        self.assertIn("TK-001", rows["ожидание без условия"][2])
        self.assertIn("todo: 1", rows["очередь"][2])

    def test_errors_in_last_day(self):
        now = time.time()
        (self.sd / "runs.log").write_text(
            f"{iso(now - 60)} TK-1 engineer status=ok\n{iso(now - 30)} TK-2 judge status=error\n"
            f"{iso(now - 5 * D.DAY)} TK-3 judge status=error\n", encoding="utf-8")
        r = self.rows(now=now)["ошибки за сутки"]
        self.assertEqual(r[1], D.WARN)
        self.assertIn("запусков за сутки: 2, не ok: 1", r[2])

    def test_bus_states(self):
        with mock.patch.dict(os.environ, {"RPV_BUS_URL": "http://x:1"}):
            ok = D.check_bus(lambda path: {"ok": True, "last_seq": 7, "queues": {"ceo.pending": 2}, "blocked": {}})
            self.assertEqual(ok[1], D.OK)
            self.assertIn("не доставлено 2", ok[2])
            blk = D.check_bus(lambda path: {"ok": True, "last_seq": 7, "queues": {}, "blocked": {"TK-1": "r"}})
            self.assertEqual(blk[1], D.WARN)

            def boom(path):
                raise OSError("refused")
            self.assertEqual(D.check_bus(boom)[1], D.FAIL)

    def test_prints_in_cp1252_environment(self):
        import subprocess
        env = {**os.environ, "PYTHONIOENCODING": "cp1252", "RPV_BUS_URL": "", "ALPHA_BUS_URL": ""}
        done = subprocess.run([sys.executable, str(Path(D.__file__)), "--project", str(self.project)],
                              capture_output=True, env=env, timeout=60)
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn("диспетчер", done.stdout.decode("utf-8"))

    def test_outside_project_exit_2(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"RPV_PROJECT": "", "CLAUDE_PROJECT_DIR": ""}):
            cwd = os.getcwd()
            os.chdir(d)
            try:
                self.assertEqual(D.main([]), 2)
            finally:
                os.chdir(cwd)


if __name__ == "__main__":
    unittest.main()
