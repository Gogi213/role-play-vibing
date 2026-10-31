"""TK-083 (В-32, хвост TK-080): остатки безусловного «полный прогон — зелёный CI» — по ключу `has_ci` (умолчание true — прежнее).
Подсказка отказа приёмки на полный прогон, автовлив без CI, устав Инженера (строка про push) и review-round."""
from __future__ import annotations

import re
import unittest
from pathlib import Path
from unittest import mock

import test_result as TR  # первым: задаёт песочницу CLAUDE_PROJECT_DIR
import test_tk050 as T50
import test_tk056 as T56
import merge_rule as M
import ticket as T
import tickets_proof as TP
from test_merge_rule import MergeRuleBase, NOW, REPO, SHA
import ci_watch as C

PLUGIN = Path(__file__).resolve().parents[2]


def has_ci(value):
    """`has_ci` в настройках проекта: None — ключа нет."""
    return mock.patch.object(TP.P, "settings", return_value={} if value is None else {"has_ci": value, "ci_repo": REPO})


class FullRunHint(unittest.TestCase):
    def test_default_and_true_point_to_ci(self):
        for value in (None, True):
            with has_ci(value):
                self.assertIn("зелёный CI PR", TP.full_run_hint(), value)
                self.assertNotIn("CI на PR нет", TP.full_run_hint(), value)

    def test_false_does_not_send_to_ci(self):
        with has_ci(False):
            hint = TP.full_run_hint()
        self.assertIn("CI на PR нет", hint)
        self.assertIn("отчёт «Итог»", hint)
        self.assertNotIn("зелёный CI PR", hint)
        self.assertIn("тесты самой правки", hint)

    def test_accept_refusal_carries_the_conditional_hint(self):
        body = T56.FullRunTests
        for value, want, deny in ((None, "зелёный CI PR", "CI на PR нет"), (False, "CI на PR нет", "зелёный CI PR")):
            case = body("test_full_run_recognised")
            case.setUp()
            try:
                case._repo()
                case._contract("Договор: [тест: python -m pytest -n auto] всё зелёное")
                case.res("engineer", "done", path="out.md")
                with has_ci(value):
                    rc, err = case.accept()
                self.assertEqual(rc, 1, value)
                self.assertIn("полный прогон проекта", err)
                self.assertIn(want, err)
                self.assertNotIn(deny, err)
            finally:
                case.tearDown()


class MergeWithoutCi(MergeRuleBase):
    def merges(self, value, ci_state):
        C.save_state({f"{REPO}#7": {"sha": SHA, "state": ci_state}} if ci_state else {})
        with mock.patch.object(M.P, "settings", return_value={"ci_repo": REPO} | ({} if value is None else {"has_ci": value})):
            return self.run_m()

    def test_no_ci_merges_on_verdict_alone(self):
        for state in (None, "pending", "failure"):
            self.calls.clear()
            self.assertEqual(self.merges(False, state), [(7, "влит")], state)
            self.assertEqual(self.put()[0][2]["sha"], SHA)
            T.write_header_updates(self.path, {"status": "in_review", "accepted": f"7@{SHA}"}, now=NOW)

    def test_default_and_true_still_need_green_ci(self):
        for value in (None, True):
            for state in (None, "pending", "failure"):
                self.assertEqual(self.merges(value, state), [], (value, state))
        self.assertEqual(self.put(), [])

    def test_no_ci_still_needs_verdict_on_current_head(self):
        T.write_header_updates(self.path, {"accepted": ""}, now=NOW)
        self.assertEqual(self.merges(False, None), [])
        T.write_header_updates(self.path, {"accepted": f"7@{SHA}"}, now=NOW)
        self.head = "b" * 40
        self.assertEqual(self.merges(False, None), [])
        self.assertEqual(self.put(), [])

    def test_no_ci_still_reports_conflict_instead_of_merging(self):
        self.mergeable = False
        self.assertEqual(self.merges(False, None), [(7, "конфликт → владельцу")])
        self.assertEqual(self.put(), [])


class ChartersAndRound(unittest.TestCase):
    def text(self, name):
        return re.sub(r"\s+", " ", (PLUGIN / "templates" / "roles" / name).read_text(encoding="utf-8"))

    def test_engineer_push_line_is_tests_of_the_change(self):
        text = self.text("engineer.md")
        self.assertNotIn("быстрый прогон тестов проекта зелёный", text)
        line = next(ln for ln in text.split("- ") if ln.startswith("**Перед каждым push в PR"))
        self.assertIn("тесты самой правки (`python -m pytest <файл правки>`), не весь проект", line)
        self.assertIn("при `has_ci: false` — быстрый прогон тестов проекта", line)

    def test_round_batch_is_not_an_unconditional_full_run(self):
        text = self.text("review-round.md")
        self.assertNotIn("на каждую партию — полный прогон тестов и линтеров", text)
        self.assertIn("на каждую партию — тесты самой правки и линтеры; полный прогон тестов проекта подтверждает зелёный CI PR "
                      "(`has_ci`, умолчание `true`), при `has_ci: false` его гоняет роль на каждую партию", text)


if __name__ == "__main__":
    unittest.main()
