"""TK-073 (В-31, п.4–5): `wait --check` (проверка нагрузки без записи), снятие отметки OOM с --why, хвост лога не даёт отметку,"""
from __future__ import annotations

import atexit
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

_SANDBOX = tempfile.mkdtemp(prefix="rpv-test-proj-")
os.makedirs(os.path.join(_SANDBOX, ".claude", "roles"))
os.environ["CLAUDE_PROJECT_DIR"] = _SANDBOX
atexit.register(shutil.rmtree, _SANDBOX, True)
sys.path.insert(0, str(Path(__file__).resolve().parent))
import hostload as HL  # noqa: E402
import project as P  # noqa: E402
import project_paths as CFG  # noqa: E402
import ticket as T  # noqa: E402
import tickets as TK  # noqa: E402
from test_tk070 import record  # noqa: E402

NOW = datetime(2026, 10, 31, 12, 0, tzinfo=timezone(timedelta(hours=4)))
SPEC = "host:gpu1:/tmp/job.rc"


class LoadCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        (base / "tickets").mkdir()
        self._orig = (CFG.TICKETS_DIR, CFG.STATE_FILE, CFG.DISPATCHER_DIR, TK.TICKETS_DIR, TK.PROJECT_ROOT)
        CFG.TICKETS_DIR, CFG.STATE_FILE, CFG.DISPATCHER_DIR = base / "tickets", base / "state.json", base
        TK.TICKETS_DIR, TK.PROJECT_ROOT = base / "tickets", base
        self.path = T.create_ticket(CFG.TICKETS_DIR, owner="engineer", title="Т", status="todo", now=NOW)
        T.write_header_updates(self.path, {"status": "in_progress", "reviewer": "judge"}, now=NOW)
        self.tid = self.path.stem
        self.settings = {"require_probe": True, "host_free_cmd": "freecheck"}
        for repl in (mock.patch.object(P, "settings", lambda root=None: self.settings),
                     mock.patch.object(P, "setting", lambda k, d=None: self.settings.get(k, d)),
                     mock.patch.object(HL, "free", lambda alias, tid, ssh: (24, 40.0))):
            repl.start()
            self.addCleanup(repl.stop)
        self._role = os.environ.get("RPV_ROLE")
        os.environ["RPV_ROLE"] = "engineer"

    def tearDown(self):
        CFG.TICKETS_DIR, CFG.STATE_FILE, CFG.DISPATCHER_DIR, TK.TICKETS_DIR, TK.PROJECT_ROOT = self._orig
        if self._role is None:
            os.environ.pop("RPV_ROLE", None)
        else:
            os.environ["RPV_ROLE"] = self._role
        self.tmp.cleanup()

    def tkt(self):
        return T.read_ticket(self.path)

    def check(self, probe):
        a = dict(id=self.tid, spec=SPEC, on_met=None, by=None, probe=probe, check=True)
        err, out = mock.MagicMock(), mock.MagicMock()
        with mock.patch("sys.stderr", err), mock.patch("sys.stdout", out):
            rc = TK.cmd_wait(type("A", (), a)())
        self.err = "".join(c.args[0] for c in err.write.call_args_list)
        self.out = "".join(c.args[0] for c in out.write.call_args_list)
        return rc

    def oom_clear(self, why, role):
        os.environ["RPV_ROLE"] = role
        err = mock.MagicMock()
        with mock.patch("sys.stderr", err), mock.patch("sys.stdout", mock.MagicMock()), \
                mock.patch.object(TK, "_caller_role", lambda: role):  # не по state.json настоящего проекта
            rc = TK.cmd_oom_clear(type("A", (), dict(id=self.tid, why=why))())
        self.err = "".join(c.args[0] for c in err.write.call_args_list)
        return rc

    def test_check_passes_without_by_and_writes_nothing(self):
        before = self.path.read_text(encoding="utf-8")
        self.assertEqual(self.check(record(n="4")), 0)
        self.assertIn("N_max = 6", self.out)
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)  # ни шапки, ни лога
        self.assertEqual(self.tkt().status, "in_progress")

    def test_check_refuses_too_many_processes_and_writes_nothing(self):
        before = self.path.read_text(encoding="utf-8")
        self.assertEqual(self.check(record(n="10")), 1)
        self.assertIn("N_max = 6", self.err)
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)

    def test_check_keeps_oom_mark(self):
        T.write_header_updates(self.path, {"probe_load": "пик 5.00 ГБ · N 4"}, now=NOW)
        HL.note_oom(self.tkt(), "rc 137: oom-kill", NOW)
        self.assertEqual(self.check(record(peak="5 ГБ", n="4")), 1)
        self.assertIn("OOM", self.err)
        self.assertIsNotNone(HL.oom_mark(self.tid))
        self.assertEqual(self.check(record(peak="3 ГБ", n="4")), 0)
        self.assertIsNotNone(HL.oom_mark(self.tid))  # проверка отметку не снимает — снимает только принятое ожидание

    def test_wait_without_by_still_refused_without_check(self):
        a = dict(id=self.tid, spec=SPEC, on_met=None, by=None, probe=record(n="4"))
        with mock.patch("sys.stderr", mock.MagicMock()):
            self.assertEqual(TK.cmd_wait(type("A", (), a)()), 1)

    def test_oom_clear_by_judge_removes_mark_and_logs_why(self):
        HL.note_oom(self.tkt(), "rc 137: oom-kill", NOW)
        self.assertEqual(self.oom_clear("память машины расширена, пик прежний", "judge"), 0)
        self.assertIsNone(HL.oom_mark(self.tid))
        self.assertIn("память машины расширена", self.path.read_text(encoding="utf-8"))

    def test_oom_clear_refused_for_engineer_and_short_why(self):
        HL.note_oom(self.tkt(), "rc 137: oom-kill", NOW)
        self.assertEqual(self.oom_clear("память расширена, пик прежний", "engineer"), 1)
        self.assertEqual(self.oom_clear("ок", "judge"), 1)
        self.assertIsNotNone(HL.oom_mark(self.tid))

    def test_code_entries_of_ceo_and_judge_are_marked_service(self):
        """TK-097: проба, нагрузка и снятие OOM пишутся кодом от имени вызвавшего; у ceo/judge — с пометкой, не заказ."""
        import precheck as PC
        HL.note_oom(self.tkt(), "rc 137: oom-kill", NOW)
        self.assertEqual(self.oom_clear("память машины расширена, пик прежний", "judge"), 0)
        self.assertEqual(self.tkt().log[-1].author, "judge (служебная)")
        HL.note_oom(self.tkt(), "rc 137: oom-kill", NOW)
        self.assertEqual(self.oom_clear("память машины расширена, пик прежний", ""), 0)  # CEO: роли нет
        self.assertEqual(self.tkt().log[-1].author, "ceo (служебная)")
        by = (datetime.now().astimezone() + timedelta(hours=2)).isoformat(timespec="minutes")
        a = dict(id=self.tid, spec=SPEC, on_met=None, by=by, probe=record(n="4"))
        with mock.patch.object(TK, "_caller_role", lambda: ""), mock.patch("sys.stdout", mock.MagicMock()):
            self.assertEqual(TK.cmd_wait(type("A", (), a)()), 0)
        authors = [e.author for e in self.tkt().log]
        self.assertEqual(authors[-2:], ["ceo (служебная)", "ceo (служебная)"])  # [проба] и [нагрузка]
        self.assertEqual(PC.late_entries(self.tkt(), "engineer", NOW - timedelta(minutes=1)), [])

    def test_wait_entries_of_engineer_keep_plain_author(self):
        by = (datetime.now().astimezone() + timedelta(hours=2)).isoformat(timespec="minutes")
        a = dict(id=self.tid, spec=SPEC, on_met=None, by=by, probe=record(n="4"))
        with mock.patch.object(TK, "_caller_role", lambda: "engineer"), mock.patch("sys.stdout", mock.MagicMock()):
            self.assertEqual(TK.cmd_wait(type("A", (), a)()), 0)
        self.assertEqual({e.author for e in self.tkt().log}, {"engineer"})

    def test_log_tail_with_oom_text_does_not_mark(self):
        import watch
        job = {"ticket": self.tid, "state": "failed", "unit": "u1", "id": "7", "reason": "rc 1: ошибка данных"}
        T.write_header_updates(self.path, {"status": "waiting", "wait_for": "job:gpu1:7"}, now=NOW)
        with mock.patch.object(watch, "_to_judge"):
            watch._triage_job(self.path, self.tkt(), T.parse_wait_for("job:gpu1:7"), {}, NOW,
                              lambda alias, jid: ("failed", "Out of memory: Killed process 4242 (python)"), {}, {"gpu1": [job]})
        self.assertIsNone(HL.oom_mark(self.tid))


if __name__ == "__main__":
    unittest.main()
