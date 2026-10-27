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
from test_plugin_version import make_install  # noqa: E402


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

    def rows(self, **kw):
        kw.setdefault("installed", lambda p: True)
        return {r[0]: r for r in D.run_checks(self.project, **kw)}

    def test_clean_project_reports_everything_and_fails_on_services(self):
        rows = self.rows()
        self.assertEqual(rows["диспетчер"][1], D.FAIL)
        self.assertEqual(rows["сторож"][1], D.FAIL)
        self.assertNotIn("шина", rows)
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

    def test_prints_in_cp1252_environment(self):
        import subprocess
        env = {**os.environ, "PYTHONIOENCODING": "cp1252"}
        done = subprocess.run([sys.executable, str(Path(D.__file__)), "--project", str(self.project)],
                              capture_output=True, env=env, timeout=60)
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn("диспетчер", done.stdout.decode("utf-8"))

    def test_idle_row(self):
        now = time.time()
        day = datetime.fromtimestamp(now).date().isoformat()
        (self.sd / "state.json").write_text(json.dumps({
            "last_tick": iso(now),
            "downtime": {day: {"idle_s": 900, "wait_s": 0, "stall_s": 0, "alerted": True}}}), encoding="utf-8")
        rows = self.rows(now=now)
        self.assertEqual(rows["простой сегодня"][1], D.FAIL)
        self.assertIn("15 мин из 10", rows["простой сегодня"][2])

    def test_idle_row_shows_throttle_and_limit_buckets(self):
        now = time.time()
        day = datetime.fromtimestamp(now).date().isoformat()
        (self.sd / "state.json").write_text(json.dumps({
            "last_tick": iso(now),
            "downtime": {day: {"idle_s": 0, "wait_s": 0, "stall_s": 0, "throttle_s": 720, "limit_s": 3600,
                               "alerted": True}}}), encoding="utf-8")
        r = self.rows(now=now)["простой сегодня"]
        self.assertEqual(r[1], D.FAIL)
        self.assertIn("тормоз запусков 12", r[2])
        self.assertIn("вне SLO 60", r[2])

    def test_outside_project_exit_2(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"RPV_PROJECT": "", "CLAUDE_PROJECT_DIR": ""}):
            cwd = os.getcwd()
            os.chdir(d)
            try:
                self.assertEqual(D.main([]), 2)
            finally:
                os.chdir(cwd)

    def test_all_services_checked_and_duplicate_copy_fails(self):
        """№12: проверяются все S.services(); две копии одной службы — FAIL."""
        with mock.patch.object(D.S, "services", return_value=("dispatch", "watch", "ci_watch")):
            self.assertIn("ci_watch", self.rows())
        proj = str(self.project)
        procs = [(11, f"python -u /x/dispatch.py --project {proj}"), (12, f"python -u /y/dispatch.py --project {proj}")]
        rows = self.rows(procs=procs)
        self.assertEqual(rows["копии dispatch"][1], D.FAIL)
        self.assertNotIn("копии watch", rows)


    def test_versions_row_dispatcher_and_watch_against_installed(self):
        """TK-039: служба на каталоге старой версии — FAIL со строкой версий; совпадает — OK; нет служб — строки нет."""
        cfg, roots = make_install(Path(self.tmp.name) / "cfg", ("2.0.5", "2.0.9"), [("user", None, "2.0.9")])
        proj = str(self.project)

        def procs(dispatch, watch):
            return [(31, f'python -u "{roots[dispatch]}/.claude/dispatcher/dispatch.py" --project {proj}'),
                    (32, f'python -u "{roots[watch]}/.claude/dispatcher/watch.py" --project {proj}')]
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": cfg}):
            self.assertNotIn("версии плагина", self.rows(procs=[]))
            row = self.rows(procs=procs("2.0.5", "2.0.9"))["версии плагина"]
            self.assertEqual(row[1], D.FAIL)
            self.assertIn("диспетчер 2.0.5", row[2])
            self.assertIn("сторож 2.0.9", row[2])
            self.assertIn("установлена 2.0.9", row[2])
            row = self.rows(procs=procs("2.0.9", "2.0.9"))["версии плагина"]
            self.assertEqual(row[1], D.OK)
            self.assertIn("диспетчер 2.0.9, сторож 2.0.9", row[2])


if __name__ == "__main__":
    unittest.main()
