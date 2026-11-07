"""TK-121 (В-58): принятый PR с красным CI не висит невлитым — заказ «слей main» владельцу, событие CEO, класс в счётчике простоя."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ceo_wake as CW  # noqa: E402
import ci_watch as C  # noqa: E402
import downtime as DT  # noqa: E402
import merge_rule as M  # noqa: E402
import ticket as T  # noqa: E402
from test_merge_rule import NOW, REPO, SHA, MergeRuleBase  # noqa: E402

NEW = "b" * 40
DOCS = Path(__file__).resolve().parents[2] / "docs"


class RedAccepted(MergeRuleBase):
    def setUp(self):
        super().setUp()
        self.behind, self.compare_error, self.emitted = 3, None, []
        self.red_at = (datetime.now().astimezone() - timedelta(minutes=10)).isoformat(timespec="seconds")
        self.red_ci(SHA)

    def red_ci(self, sha, state="failure"):
        C.save_state({f"{REPO}#7": {"sha": sha, "state": state, "red": ["tests (windows)"] if state == "failure" else [], "at": self.red_at}})

    def gh(self, path, method="GET", **f):
        if "/compare/" in path:
            self.calls.append((method, path, f))
            if self.compare_error:
                raise RuntimeError(self.compare_error)
            return {"behind_by": self.behind}
        return super().gh(path, method, **f)

    def run_m(self):
        return M.merge_once(REPO, gh=self.gh, emit=lambda *a: self.emitted.append(a))

    def state(self):
        return M._load()

    def log_texts(self):
        return [e.text for e in self.tkt().log]

    def orders(self):
        return [t for t in self.log_texts() if "слей main" in t and "правок кода не делай" in t]

    def test_main_ahead_orders_owner_once_per_head(self):
        out = self.run_m()
        self.assertEqual(self.put(), [])
        self.assertEqual(self.emitted, [])
        self.assertEqual(self.tkt().header.get("next"), "engineer")
        self.assertEqual(len(self.orders()), 1)
        self.assertIn((7, "красный CI, main впереди → владельцу: слить main"), out)
        self.run_m()
        self.assertEqual(len(self.orders()), 1)
        self.assertEqual(self.emitted, [])

    def test_order_clears_merged_wait(self):
        T.write_header_updates(self.path, {"status": "waiting", "wait_for": f"merged:{REPO}#7"}, now=NOW)
        self.run_m()
        h = self.tkt().header
        self.assertEqual((h.get("wait_for") or "", h.get("status"), h.get("next")), ("", "in_progress", "engineer"))

    def test_main_not_ahead_tells_ceo_once(self):
        self.behind = 0
        self.run_m()
        self.run_m()
        self.assertEqual(len(self.emitted), 1)
        tid, kind, text = self.emitted[0]
        self.assertEqual((tid, kind), (self.tid, "merge-red"))
        self.assertTrue(text.startswith("принят, не влит: CI красный 10 мин"), text)
        self.assertIn("tests (windows)", text)
        self.assertEqual(self.tkt().header.get("next") or "", "")
        self.assertEqual(self.put(), [])

    def test_after_order_red_again_tells_ceo_once(self):
        self.run_m()
        self.head = NEW
        self.red_ci(NEW)
        out = self.run_m()
        self.run_m()
        self.assertEqual([(k, t.split(" — ")[0]) for _, k, t in self.emitted], [("merge-red", "принят, не влит: CI красный 10 мин")])
        self.assertIn((7, "после слияния main CI красный → CEO"), out)

    def test_after_order_green_or_pending_no_event_and_mark_off(self):
        self.run_m()
        for state in ("success", "pending"):
            self.head = NEW
            self.red_ci(NEW, state)
            self.run_m()
            self.assertEqual(self.emitted, [])
            self.assertNotIn(M.RED_MARK, self.state())

    def test_after_order_stale_ci_record_is_not_red(self):
        self.run_m()
        self.head = NEW  # ci-state ещё хранит старую красную голову
        self.run_m()
        self.assertEqual(self.emitted, [])
        self.assertNotIn(M.RED_MARK, self.state())

    def test_second_red_after_order_goes_to_ceo_not_second_order(self):
        self.run_m()
        self.head = NEW
        T.write_header_updates(self.path, {"accepted": f"7@{NEW}"}, now=NOW)
        self.red_ci(NEW)
        self.run_m()
        self.assertEqual(len(self.orders()), 1)
        self.assertEqual(len(self.emitted), 1)

    def test_compare_error_neither_orders_nor_tells(self):
        self.compare_error = "HTTP 502"
        self.run_m()
        self.assertEqual((self.emitted, self.orders()), ([], []))
        self.compare_error = None
        self.run_m()
        self.assertEqual(len(self.orders()), 1)

    def test_compare_without_field_counts_as_not_behind(self):
        self.behind = None
        self.run_m()
        self.assertEqual(len(self.emitted), 1)

    def test_mark_set_while_red_and_dropped_when_pr_gone(self):
        self.run_m()
        self.assertIsNotNone(T.parse_dt(self.state()[M.RED_MARK]))
        self.assertIn(f"red:{REPO}#7", self.state())

        def gone(path, method="GET", **f):
            if path == f"repos/{REPO}":
                return {"default_branch": "main"}
            return [] if "pulls?" in path else {}

        M.merge_once(REPO, gh=gone, emit=lambda *a: None)
        self.assertNotIn(M.RED_MARK, self.state())
        self.assertNotIn(f"red:{REPO}#7", self.state())

    def test_green_ci_merges_as_before(self):
        self.red_ci(SHA, "success")
        self.run_m()
        self.assertEqual(len(self.put()), 1)
        self.assertEqual(self.emitted, [])
        self.assertNotIn(M.RED_MARK, self.state())

    def test_not_accepted_or_other_head_stays_silent(self):
        T.write_header_updates(self.path, {"accepted": ""}, now=NOW)
        self.run_m()
        T.write_header_updates(self.path, {"accepted": f"7@{NEW}"}, now=NOW)
        self.run_m()
        self.assertEqual((self.emitted, self.put(), self.orders()), ([], [], []))
        self.assertNotIn(M.RED_MARK, self.state())

    def test_conflict_keeps_old_behaviour(self):
        self.mergeable = False
        self.run_m()
        self.assertEqual(self.emitted, [])
        self.assertTrue(any("конфликтует" in t for t in self.log_texts()))

    def test_has_ci_false_ignores_red_state(self):
        with mock.patch.object(M, "no_ci", return_value=True):
            self.run_m()
        self.assertEqual((len(self.put()), self.emitted), (1, []))

    def test_ci_watch_stamps_state_change_time(self):
        def gh(path, **k):
            if "pulls?" in path:
                return [{"number": 7, "title": self.tid, "head": {"sha": SHA, "ref": "feat/x"}, "base": {"ref": "main"}}]
            return {"check_runs": [{"name": "t", "status": "completed", "conclusion": "failure"}]}

        C.save_state({})
        C.run_once(REPO, gh=gh)
        self.assertIsNotNone(T.parse_dt(C.load_state()[f"{REPO}#7"]["at"]))


class Counter(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="rpv-tk121-"))
        self.now = datetime(2026, 11, 7, 20, 0, 0).astimezone()

    def mark(self, minutes_ago, raw=None):
        body = raw if raw is not None else json.dumps({M.RED_MARK: (self.now - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")})
        (self.d / "merge-state.json").write_text(body, encoding="utf-8")

    def flag(self):
        return DT.watch_flags(self.d, self.now)["unmerged"]

    def test_fresh_mark_counts_even_without_ceo_session(self):
        self.mark(2)
        self.assertTrue(self.flag())

    def test_stale_missing_broken_future_mark_does_not_count(self):
        self.assertFalse(self.flag())
        self.mark(20)
        self.assertFalse(self.flag())
        self.mark(0, raw="{не json")
        self.assertFalse(self.flag())
        self.mark(0, raw=json.dumps({M.RED_MARK: "когда-то"}))
        self.assertFalse(self.flag())
        self.mark(-5)
        self.assertFalse(self.flag())

    def test_account_accumulates_unmerged_and_total_sums_it(self):
        st = {}
        kw = dict(ready_unserved=False, work_present=False, waiting_nocond=False, poll_s=15.0)
        DT.account(st, self.now, unmerged=True, **kw)
        _, rec, _ = DT.account(st, self.now + timedelta(seconds=60), **kw)
        self.assertEqual(rec["unmerged_s"], 60.0)
        self.assertAlmostEqual(DT.total_min(rec), 1.0)
        _, rec, _ = DT.account(st, self.now + timedelta(seconds=120), **kw)
        self.assertEqual(rec["unmerged_s"], 60.0)


class Docs(unittest.TestCase):
    def test_event_kind_is_documented(self):
        self.assertEqual(CW.EVENT_OF["merge-red"], "принят, не влит")
        row = next(ln for ln in (DOCS / "signals.md").read_text(encoding="utf-8").splitlines() if ln.startswith("| `merge-red` |"))
        self.assertTrue(row.endswith("| будит событием |"))
        self.assertIn("merge-red", (DOCS / "img" / "ceo-signals.svg").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
