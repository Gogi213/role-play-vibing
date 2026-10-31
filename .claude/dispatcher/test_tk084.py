"""TK-084 (В-32, хвост TK-083): при `has_ci: false` запись приёмки не требует CI, `wait`/`result wait` с `ci:<репо>#<PR>`
отказывают (клеток CI нет — ожидание не снялось бы), цепочка PR (база не main) идёт без CI. Умолчание (`true`) — прежнее."""
from __future__ import annotations

import contextlib
import io
import os
import unittest
from pathlib import Path
from unittest import mock

import test_result as TR  # первым: задаёт песочницу CLAUDE_PROJECT_DIR
import ci_watch as C
import merge_rule as M
import routes as R
import ticket as T
import tickets as TK
from test_merge_rule import MergeRuleBase, NOW, REPO, SHA


def settings(value):
    """Настройки проекта с `ci_repo`; `has_ci`: None — ключа нет."""
    return mock.patch.object(M.P, "settings", return_value={"ci_repo": REPO} | ({} if value is None else {"has_ci": value}))


class AcceptNote(MergeRuleBase):
    def accept_text(self, value):
        T.write_header_updates(self.path, {"accepted": "", "status": "in_review"}, now=NOW)
        os.environ.pop("RPV_ROLE", None)
        with settings(value), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(TK.cmd_accept(type("A", (), {"id": self.tid, "pr": 7, "sha": SHA, "text": ""})()), 0)
        return self.tkt().log[-1].text

    def test_default_and_true_still_name_green_ci(self):
        for value in (None, True):
            self.assertIn("Влить, когда CI зелёный и нет конфликта", self.accept_text(value), value)

    def test_false_does_not_ask_for_ci(self):
        text = self.accept_text(False)
        self.assertIn("Влить, когда нет конфликта", text)
        self.assertIn("CI на PR нет", text)
        self.assertNotIn("CI зелёный", text)

    def test_explicit_text_wins(self):
        T.write_header_updates(self.path, {"accepted": "", "status": "in_review"}, now=NOW)
        os.environ.pop("RPV_ROLE", None)
        with settings(False), contextlib.redirect_stdout(io.StringIO()):
            TK.cmd_accept(type("A", (), {"id": self.tid, "pr": 7, "sha": SHA, "text": "Свой текст."})())
        self.assertIn("Свой текст.", self.tkt().log[-1].text)
        self.assertNotIn("Влить", self.tkt().log[-1].text)


class CiWaitRefusal(unittest.TestCase):
    SPEC = f"ci:{REPO}#7"

    def test_problem_only_for_ci_form_without_ci(self):
        with settings(False):
            why = T.ci_wait_problem(self.SPEC)
            self.assertIn("нет CI", why)
            self.assertIn(f"merged:{REPO}#7", why)
            for other in (f"merged:{REPO}#7", f"ci-run:{REPO}#7", "ticket:TK-001", "file:out.done", "at:2030-01-01T00:00+04:00"):
                self.assertEqual(T.ci_wait_problem(other), "", other)
        for value in (None, True):
            with settings(value):
                self.assertEqual(T.ci_wait_problem(self.SPEC), "", value)

    def test_signals_check_sees_it_too(self):
        with settings(False):
            self.assertIn("нет CI", T.file_wait_problem(self.SPEC))
        with settings(True):
            self.assertEqual(T.file_wait_problem(self.SPEC), "")

    def test_result_wait_form_refused_without_ci(self):
        args = dict(role="engineer", result="wait", why="жду CI", form=self.SPEC)
        with settings(False):
            err = R.check(**args)
        self.assertIn("нет CI", err)
        with settings(True):
            self.assertEqual(R.check(**args), "")


class WaitCommand(unittest.TestCase):
    setUp, tearDown, tkt = TR.ResultTests.setUp, TR.ResultTests.tearDown, TR.ResultTests.tkt

    def wait(self, value, spec):
        a = dict(id=self.tid, spec=spec, on_met=None, by=None)
        err = io.StringIO()
        with settings(value), contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            rc = TK.cmd_wait(type("A", (), a)())
        return rc, err.getvalue()

    def test_wait_ci_refused_without_ci_and_ticket_untouched(self):
        before = self.path.read_text(encoding="utf-8")
        rc, err = self.wait(False, f"ci:{REPO}#7")
        self.assertEqual(rc, 1)
        self.assertIn("нет CI", err)
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)

    def test_wait_ci_accepted_by_default_and_merged_without_ci(self):
        self.assertEqual(self.wait(None, f"ci:{REPO}#7")[0], 0)
        self.assertEqual(self.wait(False, f"merged:{REPO}#7")[0], 0)
        self.assertEqual(self.tkt().header.get("wait_for"), f"merged:{REPO}#7")


class ChainWithoutCi(MergeRuleBase):
    """База PR не main (цепочка): без CI база переключается, когда родитель влит; с CI-умолчанием и без CI-клеток — нет."""

    def setUp(self):
        super().setUp()
        self.base = "feat/parent"
        self.parent = [{"number": 6, "title": "p", "head": {"sha": "c" * 40, "ref": "feat/parent"}, "base": {"ref": "main"}}]
        C.save_state({})  # CI-клеток нет совсем

    def merge(self, value):
        with settings(value):
            return self.run_m()

    def test_waits_for_open_parent_then_retargets_then_merges(self):
        self.others = self.parent
        self.assertEqual(self.merge(False), [])
        self.assertEqual([c for c in self.calls if c[0] in ("PATCH", "PUT")], [])
        self.others = []
        self.assertEqual(self.merge(False), [(7, "база feat/parent → main")])
        self.assertEqual([c for c in self.calls if c[0] == "PATCH"][0][2], {"base": "main"})
        self.assertEqual(self.put(), [])
        self.base = "main"  # GitHub после PATCH
        self.assertEqual(self.merge(False), [(7, "влит")])

    def test_without_verdict_on_head_chain_stays(self):
        T.write_header_updates(self.path, {"accepted": ""}, now=NOW)
        self.assertEqual(self.merge(False), [])
        self.assertEqual([c for c in self.calls if c[0] == "PATCH"], [])

    def test_default_needs_ci_even_in_chain(self):
        for value in (None, True):
            self.assertEqual(self.merge(value), [], value)
        self.assertEqual([c for c in self.calls if c[0] == "PATCH"], [])


if __name__ == "__main__":
    unittest.main()
