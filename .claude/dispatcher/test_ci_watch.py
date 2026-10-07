"""Тесты ci_watch (TK-079 п.1): красный → владелец, зелёный → Судья, зелёный с вердиктом на голове — тишина,
pending — тишина, повтор той же головы — тишина, новая голова — снова, wait_for ci:."""
from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_SANDBOX = tempfile.mkdtemp(prefix="rpv-test-proj-")
os.makedirs(os.path.join(_SANDBOX, ".claude", "roles"))
os.environ["CLAUDE_PROJECT_DIR"] = _SANDBOX
os.environ["RPV_BUS_DISABLE"] = "1"
atexit.register(shutil.rmtree, _SANDBOX, True)
sys.path.insert(0, str(Path(__file__).resolve().parent))
import ci_watch as C  # noqa: E402
import dispatch as D  # noqa: E402
import ticket as T  # noqa: E402

REPO = "o/r"
NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone(timedelta(hours=4)))


class CiWatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        (base / "tickets").mkdir()
        self._orig = (D.TICKETS_DIR, D.STATE_FILE)
        D.TICKETS_DIR, D.STATE_FILE = base / "tickets", base / "state.json"
        self.path = T.create_ticket(D.TICKETS_DIR, owner="engineer", title="Тикет", status="todo", now=NOW)
        T.write_header_updates(self.path, {"status": "in_review"}, now=NOW)
        T.append_log(self.path, "engineer", "PR отправлен", now=NOW)
        self.tid = self.path.stem
        self.sha, self.runs = "a" * 40, []

    def tearDown(self):
        D.TICKETS_DIR, D.STATE_FILE = self._orig
        self.tmp.cleanup()

    def gh(self, path):
        if "check-runs" in path:
            return {"check_runs": self.runs}
        return [{"number": 7, "title": f"fix {self.tid}", "head": {"sha": self.sha, "ref": "feat/x"}}]

    def run_ci(self):
        return C.run_once(REPO, gh=self.gh)

    def tkt(self):
        return T.read_ticket(self.path)

    def test_poll_errors_wake_owner_after_limit_once(self):
        def boom(path):
            if "check-runs" in path:
                raise RuntimeError("gh: HTTP 500")
            return self.gh(path)
        for _ in range(C.ERR_LIMIT - 1):
            C.run_once(REPO, gh=boom)
            self.assertNotEqual(self.tkt().header.get("next"), "engineer")
        C.run_once(REPO, gh=boom)
        t = self.tkt()
        self.assertEqual(t.header.get("next"), "engineer")
        self.assertIn("HTTP 500", t.log[-1].text)
        n = len(t.log)
        C.run_once(REPO, gh=boom)
        self.assertEqual(len(self.tkt().log), n)

    def test_poll_error_counter_resets_after_success(self):
        def boom(path):
            if "check-runs" in path:
                raise RuntimeError("gh: HTTP 500")
            return self.gh(path)
        for _ in range(C.ERR_LIMIT - 1):
            C.run_once(REPO, gh=boom)
        self.runs = []
        C.run_once(REPO, gh=self.gh)
        for _ in range(C.ERR_LIMIT - 1):
            C.run_once(REPO, gh=boom)
        self.assertNotEqual(self.tkt().header.get("next"), "engineer")

    def test_red_wakes_owner_with_names(self):
        self.runs = [{"name": "test (win)", "status": "completed", "conclusion": "failure"},
                     {"name": "lint", "status": "completed", "conclusion": "success"}]
        self.assertEqual(self.run_ci(), [(7, "aaaaaaa", "failure", "engineer")])
        t = self.tkt()
        self.assertEqual(t.header.get("next"), "engineer")
        self.assertIn("test (win)", t.log[-1].text)
        self.assertNotIn("lint", t.log[-1].text)

    def test_green_wakes_judge_once(self):
        self.runs = [{"name": "a", "status": "completed", "conclusion": "success"}]
        self.assertEqual(self.run_ci()[0][3], "judge")
        T.write_header_updates(self.path, {"next": ""}, stamp_updated=False)
        self.assertEqual(self.run_ci(), [])
        self.assertEqual(self.tkt().header.get("next") or "", "")

    def test_green_with_verdict_on_head_is_silent(self):
        T.append_log(self.path, "judge", "принято, голова aaaaaaa", now=NOW)
        self.runs = [{"name": "a", "status": "completed", "conclusion": "success"}]
        self.assertEqual(self.run_ci()[0][3], "")
        self.assertEqual(self.tkt().header.get("next") or "", "")

    def test_pending_is_silent_and_new_head_repeats(self):
        self.runs = [{"name": "a", "status": "in_progress", "conclusion": None}]
        self.assertEqual(self.run_ci()[0][3], "")
        self.runs = [{"name": "a", "status": "completed", "conclusion": "success"}]
        self.assertEqual(self.run_ci()[0][3], "judge")
        T.write_header_updates(self.path, {"next": ""}, stamp_updated=False)
        self.sha = "b" * 40
        self.assertEqual(self.run_ci()[0][3], "judge")

    def test_no_runs_is_pending(self):
        self.assertEqual(C.ci_result([])[0], "pending")

    def test_waiting_on_ci_green_wakes_judge_and_clears_wait(self):
        T.write_header_updates(self.path, {"status": "waiting", "wait_for": f"ci:{REPO}#7"}, now=NOW)
        self.runs = [{"name": "a", "status": "completed", "conclusion": "success"}]
        self.assertEqual(self.run_ci()[0][3], "judge")
        h = self.tkt().header
        self.assertEqual((h.get("next"), h.get("wait_for", "")), ("judge", ""))

    def test_waiting_on_ci_red_wakes_owner_and_clears_wait(self):
        T.write_header_updates(self.path, {"status": "waiting", "wait_for": f"ci:{REPO}#7"}, now=NOW)
        self.runs = [{"name": "a", "status": "completed", "conclusion": "failure"}]
        self.assertEqual(self.run_ci()[0][3], "engineer")
        self.assertEqual(self.tkt().header.get("wait_for", ""), "")

    def test_ci_done_requires_current_head(self):
        self.runs = [{"name": "a", "status": "completed", "conclusion": "success"}]
        self.run_ci()
        cur = lambda sha: (lambda p: {"head": {"sha": sha}})  # noqa: E731
        self.assertTrue(C.ci_done(REPO, 7, gh=cur(self.sha)))
        self.assertFalse(C.ci_done(REPO, 7, gh=cur("c" * 40)))  # пуш после опроса: в состоянии старая голова
        self.assertFalse(C.ci_done(REPO, 8, gh=cur(self.sha)))

    def test_ci_done_gh_failure_is_not_done(self):
        self.runs = [{"name": "a", "status": "completed", "conclusion": "success"}]
        self.run_ci()

        def boom(p):
            raise RuntimeError("gh")
        self.assertFalse(C.ci_done(REPO, 7, gh=boom))

    def test_event_posted_to_bus_once(self):
        import busclient
        posted = []
        orig = busclient.post
        busclient.post = lambda addr, payload=None, **k: posted.append((addr, payload))
        try:
            self.runs = [{"name": "a", "status": "completed", "conclusion": "failure"}]
            self.run_ci()
            self.run_ci()
        finally:
            busclient.post = orig
        self.assertEqual(len(posted), 1)
        self.assertEqual(posted[0][0], f"ci.7.{self.sha[:7]}")
        self.assertEqual((posted[0][1]["state"], posted[0][1]["red"]), ("failure", ["a"]))

    def test_pr_header_field_binds_ticket(self):
        T.write_header_updates(self.path, {"pr": "7"}, now=NOW)
        pr = {"number": 7, "title": "без номера", "head": {"ref": "x", "sha": self.sha}}
        self.assertEqual(C.ticket_for_pr(pr, [self.tkt()]).id, self.tid)

    def test_unbound_pr_is_ignored(self):
        self.gh = lambda p: ({"check_runs": [{"name": "a", "status": "completed", "conclusion": "failure"}]}
                             if "check-runs" in p else
                             [{"number": 9, "title": "чужое", "head": {"sha": self.sha, "ref": "docs/x"}}])
        self.assertEqual(self.run_ci()[0][3], "")

    def test_wait_for_form(self):
        self.assertEqual(T.parse_wait_for("ci:o/r#7"), ("ci", "o/r", 7))
        self.assertIsNone(T.parse_wait_for("ci:o/r"))


if __name__ == "__main__":
    unittest.main()
