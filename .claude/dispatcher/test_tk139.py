"""TK-139 (В-72): CI не ждём. Красный CI на основной ветке (слитый PR приходит туда же) — один тикет Инженеру «Починить CI» на голову,
без дублей; слияние при красном и идущем CI — в test_merge_rule.py, запись о красном на сданном PR — в test_ci_watch.py."""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import project_paths as CFG
import ci_watch as C
import merge_rule as M
import ticket as T

REPO = "o/r"
NOW = datetime(2026, 11, 8, 12, 0).astimezone()
HEAD = "c" * 40
RED_RUN = {"id": 11, "name": "tests (windows-latest, py3.12)", "status": "completed", "conclusion": "failure",
           "head_sha": HEAD, "html_url": "https://github.com/o/r/actions/runs/1/job/11"}
GREEN_RUN = {"id": 12, "name": "tests (ubuntu-latest, py3.12)", "status": "completed", "conclusion": "success", "head_sha": HEAD}
LOG = "2026-11-08T11:00:00Z FAILED .claude/dispatcher/test_x.py::XTests::test_a - boom\n2026-11-08T11:00:01Z FAILED a/test_y.py::test_b\n"


class WatchMain(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        (base / "tickets").mkdir()
        self._orig = (CFG.TICKETS_DIR, CFG.STATE_FILE)
        CFG.TICKETS_DIR, CFG.STATE_FILE = base / "tickets", base / "state.json"
        self.runs = [RED_RUN, GREEN_RUN]

    def tearDown(self):
        CFG.TICKETS_DIR, CFG.STATE_FILE = self._orig
        self.tmp.cleanup()

    def gh(self, path, method="GET", **f):
        if path == f"repos/{REPO}":
            return {"default_branch": "main"}
        return {"check_runs": self.runs}

    def watch(self):
        return C.watch_main(REPO, gh=self.gh, job_log=lambda repo, job: LOG)

    def tickets(self):
        return [T.read_ticket(p) for p in T.list_tickets(CFG.TICKETS_DIR)]

    def test_red_main_makes_one_engineer_ticket_with_names_and_link(self):
        out = self.watch()
        (tkt,) = self.tickets()
        self.assertEqual(out, [(HEAD[:7], tkt.id)])
        self.assertEqual((tkt.owner, tkt.header.get("reviewer")), ("engineer", "judge"))
        self.assertIn("Починить CI", tkt.header["title"])
        for want in ("tests (windows-latest, py3.12)", "test_x.py::XTests::test_a", "test_y.py::test_b", RED_RUN["html_url"], "не откатываем"):
            self.assertIn(want, tkt.description)
        self.assertNotIn("ubuntu", tkt.description)

    def test_same_head_makes_no_second_ticket(self):
        self.watch()
        self.assertEqual(self.watch(), [])
        self.assertEqual(len(self.tickets()), 1)

    def test_new_red_head_goes_into_the_open_fix_ticket(self):
        self.watch()
        self.runs = [{**RED_RUN, "head_sha": "d" * 40}]
        out = self.watch()
        (tkt,) = self.tickets()
        self.assertEqual(out, [("d" * 7, tkt.id)])
        self.assertIn("ddddddd", tkt.log[-1].text)

    def test_closed_fix_ticket_allows_a_new_one(self):
        self.watch()
        (first,) = self.tickets()
        T.write_header_updates(first.path, {"status": "done"}, now=NOW)
        self.runs = [{**RED_RUN, "head_sha": "d" * 40}]
        self.watch()
        self.assertEqual(len(self.tickets()), 2)

    def test_green_pending_or_missing_runs_make_nothing(self):
        for runs in ([GREEN_RUN], [{**RED_RUN, "status": "in_progress", "conclusion": None}], []):
            self.runs = runs
            self.assertEqual(self.watch(), [], runs)
        self.assertEqual(self.tickets(), [])

    def test_unreadable_log_still_makes_the_ticket(self):
        def boom(repo, job):
            raise RuntimeError("no log")
        C.watch_main(REPO, gh=self.gh, job_log=boom)
        (tkt,) = self.tickets()
        self.assertIn("не удалось прочитать журнал", tkt.description)

    def test_accept_note_does_not_ask_for_ci(self):
        self.assertNotIn("CI зелёный", M.accept_note())
        self.assertIn("CI не ждём", M.accept_note())


if __name__ == "__main__":
    unittest.main()
