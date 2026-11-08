"""TK-141 (В-73): `wait file:<путь>` принимается, только если путь — файл итога задания `tools/jobrun.sh`; прочее — отказ с подсказкой.
Уже ждущие тикеты с `file:` доживают по-старому (`file_wait_problem`, `signals.notify_wait_for_problem` не тронуты)."""
from __future__ import annotations

import atexit
import contextlib
import io
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
import dispatch as D  # noqa: E402
import project_paths as CFG  # noqa: E402
import routes as R  # noqa: E402
import signals as SIG  # noqa: E402
import ticket as T  # noqa: E402
import tickets as TK  # noqa: E402

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=timezone(timedelta(hours=4)))
RC = "run-1108123456-ab12.rc"
BAD = ("file:out/x.done", "file:out/result.csv", "file:out/t.rc", "file:out/run-110812345-ab12.rc", "file:out/run-11081234567-ab12.rc",
       "file:out/run-1108123456-AB12.rc", "file:out/run-1108123456-ab12.RC", "file:out/run-1108123456-ab12.rc.tmp", "file:out/run-1108123456-ab12.rc/",
       "file:/tmp/flag", "file:C:/work/job.log", "file:out/-1108123456-ab12.rc")


class UnregisteredFileTests(unittest.TestCase):
    def test_jobrun_result_names_are_accepted(self):
        for spec in (f"file:out/{RC}", f"file:C:/Users/x/runs/{RC}", f"file:C:\\Users\\x\\runs\\{RC}", "file:/var/lib/rpv/runs/a-b-c-1108123456-ab12.rc",
                     f"file:{RC}", "file: out/run-0101000000-0000.rc "):
            self.assertEqual(T.file_wait_unregistered(spec), "", spec)

    def test_other_files_are_refused_with_hint(self):
        for spec in BAD:
            why = T.file_wait_unregistered(spec)
            self.assertIn("tools/jobrun.sh", why, spec)
            self.assertIn("job:", why, spec)

    def test_other_forms_are_not_touched(self):
        for spec in ("host:gpu1:/var/lib/rpv/runs/x.done", f"host:gpu1:/var/lib/rpv/runs/{RC}", "job:gpu1:run7", "ticket:TK-001", "ci:o/r#5",
                     "merged:o/r#5", "ci-run:o/r#99", "at:2099-01-01T00:00+04:00", "", "ерунда", "file:"):
            self.assertEqual(T.file_wait_unregistered(spec), "", spec)

    def test_routes_check_refuses_wait_form_early(self):
        err = R.check("engineer", "wait", "ждём", form="file:out/x.done", root=Path(_SANDBOX))
        self.assertIn("tools/jobrun.sh", err)
        self.assertEqual(R.check("engineer", "wait", "ждём", form=f"file:out/{RC}", root=Path(_SANDBOX)), "")
        self.assertEqual(R.check("engineer", "wait", "ждём", form="host:gpu1:/var/lib/rpv/runs/x.done", root=Path(_SANDBOX)), "")

    def test_existing_waiting_tickets_are_not_complained_about(self):
        """Старая постановка (`file:` без jobrun) доживает: сигнал о негодном wait_for молчит, `file_wait_problem` тот же."""
        for spec in ("file:out/x.done", "file:flags/rpv-flag"):
            self.assertEqual(T.file_wait_problem(spec), "", spec)
        tkt = mock.Mock(status="waiting", id="TK-900", next_role="", header={"wait_for": "file:out/x.done"})
        state = {}
        with mock.patch.object(SIG, "append_ceo_inbox") as inbox:
            SIG.notify_wait_for_problem(tkt, state, NOW, set())
        inbox.assert_not_called()
        self.assertNotIn("ceo_wait_for_notified", state)


class WaitCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        (base / "tickets").mkdir()
        self._orig = (CFG.TICKETS_DIR, CFG.STATE_FILE, TK.TICKETS_DIR, TK.PROJECT_ROOT)
        CFG.TICKETS_DIR, CFG.STATE_FILE, TK.TICKETS_DIR, TK.PROJECT_ROOT = base / "tickets", base / "state.json", base / "tickets", base
        self.base = base
        self.path = T.create_ticket(CFG.TICKETS_DIR, owner="engineer", title="Т", status="todo", now=NOW)
        T.write_header_updates(self.path, {"status": "in_progress", "reviewer": "judge"}, now=NOW)
        self.tid = self.path.stem
        patch = mock.patch.object(D.P, "settings", lambda root=None: {"ci_repo": "o/r"})
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(lambda: setattr_all(CFG, TK, self._orig))

    def wait(self, spec, **kw):
        a = dict(id=self.tid, spec=spec, on_met=None, by="23:59")
        a.update(kw)
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            rc = TK.cmd_wait(type("A", (), a)())
        return rc, err.getvalue()

    def tkt(self):
        return T.read_ticket(self.path)

    def test_wait_refuses_file_that_is_not_jobrun_result(self):
        before = self.path.read_text(encoding="utf-8")
        for spec in BAD[:6]:
            rc, err = self.wait(spec)
            self.assertEqual(rc, 1, spec)
            self.assertIn("tools/jobrun.sh", err)
            self.assertIn("job:", err)
            self.assertEqual(self.path.read_text(encoding="utf-8"), before)  # отказ ничего не пишет

    def test_wait_accepts_jobrun_result(self):
        rc, err = self.wait(f"file:out/{RC}")
        self.assertEqual(rc, 0, err)
        self.assertEqual((self.tkt().status, self.tkt().header.get("wait_for")), ("waiting", f"file:out/{RC}"))

    def test_other_forms_still_accepted(self):
        for spec in ("at:2099-01-01T00:00+04:00", f"host:gpu1:/var/lib/rpv/runs/{RC}", "host:gpu1:/var/lib/rpv/runs/x.done"):
            T.write_header_updates(self.path, {"status": "in_progress", "wait_for": ""}, now=NOW)
            with mock.patch.object(TK.W, "check_wait_for", return_value=False):
                rc, err = self.wait(spec)
            self.assertEqual(rc, 0, f"{spec}: {err}")
            self.assertEqual(self.tkt().header.get("wait_for"), spec)

    def test_result_wait_goes_through_the_same_gate(self):
        os.environ["RPV_ROLE"] = "engineer"
        self.addCleanup(os.environ.pop, "RPV_ROLE", None)
        a = dict(id=self.tid, result="wait", why="ждём", pr=None, sha=None, path=None, form="file:out/x.done", tail="-", by="23:59")
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(TK.cmd_result(type("A", (), a)()), 1)
        self.assertIn("tools/jobrun.sh", err.getvalue())
        a["form"] = f"file:out/{RC}"
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(TK.cmd_result(type("A", (), a)()), 0)
        self.assertEqual(self.tkt().status, "waiting")


def setattr_all(CFG_, TK_, orig):
    CFG_.TICKETS_DIR, CFG_.STATE_FILE, TK_.TICKETS_DIR, TK_.PROJECT_ROOT = orig


if __name__ == "__main__":
    unittest.main()
