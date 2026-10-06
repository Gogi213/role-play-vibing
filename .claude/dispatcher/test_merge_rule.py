"""Тесты merge_rule (TK-079 п.2): влить / не влить без вердикта / голова сменилась / CI красный / конфликт → владельцу /
цепочка (ждать, переключить базу) / слияние не подтвердилось / tickets.py accept."""
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
os.environ["RPV_CI_REPO"] = "o/r"
atexit.register(shutil.rmtree, _SANDBOX, True)
sys.path.insert(0, str(Path(__file__).resolve().parent))
import ci_watch as C  # noqa: E402
import dispatch as D  # noqa: E402
import merge_rule as M  # noqa: E402
import ticket as T  # noqa: E402
import tickets as TK  # noqa: E402

REPO = "o/r"
SHA = "a" * 40
NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone(timedelta(hours=4)))


class MergeRuleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        (base / "tickets").mkdir()
        self._orig = (D.TICKETS_DIR, D.STATE_FILE, TK.TICKETS_DIR)
        D.TICKETS_DIR, D.STATE_FILE = base / "tickets", base / "state.json"
        TK.TICKETS_DIR = D.TICKETS_DIR
        self.path = T.create_ticket(D.TICKETS_DIR, owner="engineer", title="Тикет", status="todo", now=NOW)
        T.write_header_updates(self.path, {"status": "in_review", "accepted": f"7@{SHA}"}, now=NOW)
        self.tid = self.path.stem
        self.head, self.base, self.mergeable, self.merged = SHA, "main", True, True
        self.calls, self.others = [], []
        C.save_state({f"{REPO}#7": {"sha": SHA, "state": "success"}})
        self._chk = M.check_pr
        M.check_pr = lambda repo, n, sha, gh=None: ""

    def tearDown(self):
        M.check_pr = self._chk
        D.TICKETS_DIR, D.STATE_FILE, TK.TICKETS_DIR = self._orig
        self.tmp.cleanup()

    def gh(self, path, method="GET", **f):
        self.calls.append((method, path, f))
        if path == f"repos/{REPO}":
            return {"default_branch": "main"}
        if path.endswith("pulls?state=open&per_page=100"):
            return [{"number": 7, "title": self.tid, "head": {"sha": self.head, "ref": "feat/x"},
                     "base": {"ref": self.base}}] + self.others
        if path.endswith("/merge"):
            return {"merged": True}
        return {"mergeable": self.mergeable, "merged": self.merged and any(c[0] == "PUT" for c in self.calls)}

    def run_m(self):
        return M.merge_once(REPO, gh=self.gh)

    def tkt(self):
        return T.read_ticket(self.path)

    def put(self):
        return [c for c in self.calls if c[0] == "PUT"]

    def test_merges_when_all_conditions_hold(self):
        self.assertEqual(self.run_m(), [(7, "влит")])
        self.assertEqual(self.put()[0][2]["sha"], SHA)
        self.assertIn("влит", self.tkt().log[-1].text)
        self.assertEqual(self.tkt().log[-1].author, "merge")

    def test_no_verdict_no_merge(self):
        T.write_header_updates(self.path, {"accepted": ""}, now=NOW)
        self.assertEqual(self.run_m(), [])
        self.assertEqual(self.put(), [])

    def test_head_changed_after_verdict_no_merge(self):
        self.head = "b" * 40
        C.save_state({f"{REPO}#7": {"sha": self.head, "state": "success"}})
        self.assertEqual(self.run_m(), [])
        self.assertEqual(self.put(), [])

    def test_verdict_for_other_pr_no_merge(self):
        T.write_header_updates(self.path, {"accepted": f"8@{SHA}"}, now=NOW)
        self.assertEqual(self.run_m(), [])

    def test_red_or_pending_ci_no_merge(self):
        for state in ("failure", "pending"):
            C.save_state({f"{REPO}#7": {"sha": SHA, "state": state}})
            self.assertEqual(self.run_m(), [])
        self.assertEqual(self.put(), [])

    def test_conflict_wakes_owner_once(self):
        self.mergeable = False
        self.assertEqual(self.run_m(), [(7, "конфликт → владельцу")])
        t = self.tkt()
        self.assertEqual(t.header.get("next"), "engineer")
        self.assertIn("конфликтует", t.log[-1].text)
        self.assertEqual(self.run_m(), [])
        self.assertEqual(self.put(), [])

    def test_mergeable_unknown_waits(self):
        self.mergeable = None
        self.assertEqual(self.run_m(), [])
        self.assertEqual(self.put(), [])

    def test_chain_waits_for_open_parent_then_retargets(self):
        self.base = "feat/parent"
        self.others = [{"number": 6, "title": "p", "head": {"sha": "c" * 40, "ref": "feat/parent"},
                        "base": {"ref": "main"}}]
        self.assertEqual(self.run_m(), [])
        self.others = []
        self.assertEqual(self.run_m(), [(7, "база feat/parent → main")])
        self.assertEqual(self.put(), [])
        self.assertEqual([c for c in self.calls if c[0] == "PATCH"][0][2], {"base": "main"})

    def test_unconfirmed_merge_wakes_owner(self):
        self.merged = False
        self.assertEqual(self.run_m(), [(7, "не подтверждено → владельцу")])
        self.assertEqual(self.tkt().header.get("next"), "engineer")

    def test_check_pr_and_404(self):
        M.check_pr = self._chk
        ok = lambda p, **k: {"head": {"sha": SHA}}
        self.assertEqual(M.check_pr(REPO, 7, SHA[:7], gh=ok), "")
        self.assertIn("сверь --sha", M.check_pr(REPO, 7, "b" * 7, gh=ok))
        def nf(p, **k):
            raise RuntimeError("Not Found (HTTP 404)")
        self.assertIn("не найден", M.check_pr(REPO, 7, SHA, gh=nf))
        self.assertTrue(M.merged_done(REPO, 7, gh=nf))  # 404 — не вечное ожидание: владелец просыпается

    def test_accept_writes_verdict(self):
        T.write_header_updates(self.path, {"accepted": ""}, now=NOW)
        os.environ.pop("RPV_ROLE", None)
        os.environ.pop("ALPHA_ROLE", None)
        args = type("A", (), {"id": self.tid, "pr": 7, "sha": SHA, "text": ""})()
        self.assertEqual(TK.cmd_accept(args), 0)
        t = self.tkt()
        self.assertEqual(t.header.get("accepted"), f"7@{SHA}")
        self.assertTrue(T.author_is(t.log[-1].author, "judge"))
        self.assertEqual(M.accepted_head(t, 7), SHA)
        self.assertIsNone(M.accepted_head(t, 8))
        self.assertEqual((t.status, t.header.get("wait_for")), ("waiting", "merged:o/r#7"))

    def test_merged_done_asks_github(self):
        self.assertTrue(M.merged_done(REPO, 7, gh=lambda p, **k: {"merged": True}))
        self.assertFalse(M.merged_done(REPO, 7, gh=lambda p, **k: {"merged": False}))
        def boom(p, **k):
            raise RuntimeError("net")
        self.assertFalse(M.merged_done(REPO, 7, gh=boom))

    def test_two_prs_one_ticket_keep_both_verdicts(self):
        os.environ.pop("RPV_ROLE", None)
        os.environ.pop("ALPHA_ROLE", None)
        sha9 = "9" * 40
        for pr, sha in ((7, SHA), (9, sha9)):
            self.assertEqual(TK.cmd_accept(type("A", (), {"id": self.tid, "pr": pr, "sha": sha, "text": ""})()), 0)
        t = self.tkt()
        self.assertEqual((M.accepted_head(t, 7), M.accepted_head(t, 9)), (SHA, sha9))
        self.others = [{"number": 9, "title": self.tid, "head": {"sha": sha9, "ref": "feat/y"}, "base": {"ref": "main"}}]
        self.assertEqual(self.run_m(), [(7, "влит")])  # у #9 CI не зелёный — ждёт
        t = self.tkt()
        self.assertIsNone(M.accepted_head(t, 7))
        self.assertEqual(M.accepted_head(t, 9), sha9)

    def test_reaccept_same_pr_replaces_its_head(self):
        os.environ.pop("RPV_ROLE", None)
        os.environ.pop("ALPHA_ROLE", None)
        new = "c" * 40
        TK.cmd_accept(type("A", (), {"id": self.tid, "pr": 7, "sha": new, "text": ""})())
        self.assertEqual(self.tkt().header.get("accepted"), f"7@{new}")

    def test_accept_refuses_non_judge_and_bad_sha(self):
        os.environ.pop("ALPHA_ROLE", None)
        os.environ["RPV_ROLE"] = "judge"
        args = type("A", (), {"id": self.tid, "pr": 7, "sha": "zzz", "text": ""})()
        self.assertEqual(TK.cmd_accept(args), 1)
        self.assertEqual(self.tkt().header.get("accepted"), f"7@{SHA}")
        os.environ["RPV_ROLE"] = "engineer"
        try:
            args.sha = SHA
            self.assertEqual(TK.cmd_accept(args), 1)
        finally:
            os.environ.pop("RPV_ROLE", None)


if __name__ == "__main__":
    unittest.main()
