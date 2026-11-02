"""TK-089 (В-36): `accept --dry-run` — все отказы гейтов одним списком; «ложное срабатывание» с E-NNN; ярлыки при записи."""
from __future__ import annotations

import contextlib
import io
import unittest

import test_result as TR  # первым: задаёт песочницу CLAUDE_PROJECT_DIR
import errors as ER
import tickets as TK

BAD_TAIL = "Готово. Не сделано: метрики у 431 строки"


class GatesTests(unittest.TestCase):
    setUp, tearDown, res, tkt, refused, _registry_with_decision = (
        TR.ResultTests.setUp, TR.ResultTests.tearDown, TR.ResultTests.res, TR.ResultTests.tkt, TR.ResultTests.refused,
        TR.ResultTests._registry_with_decision)

    def _done(self, why=BAD_TAIL):
        self._registry_with_decision()
        (self.base / "out.md").write_text("готово", encoding="utf-8")
        (self.base / "review.md").write_text("вердикт", encoding="utf-8")
        self.assertEqual(self.res("engineer", "done", path="out.md", why=why), 0)

    def _accept(self, **kw):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = self.res("judge", "accept", path="review.md", **kw)
        return rc, out.getvalue(), err.getvalue()

    def test_dry_run_lists_all_refusals_at_once_and_writes_nothing(self):
        self._done()
        before = self.path.read_text(encoding="utf-8")
        rc, _, err = self._accept(dry_run=True, tail="-", decisions=None, numbers=None)
        self.assertEqual(rc, 1)
        for part in ("--decisions", "таблицей ключевых чисел", "это хвост", "отказов 3"):
            self.assertIn(part, err)
        self.assertIn("--false-positive", err)
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)

    def test_dry_run_green_says_so_and_writes_nothing(self):
        self._done("Готово, тесты зелёные")
        before = self.path.read_text(encoding="utf-8")
        rc, out, _ = self._accept(dry_run=True, tail="-", decisions="-", numbers="-")
        self.assertEqual(rc, 0)
        self.assertIn("отказов нет", out)
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)
        self.assertEqual(self.tkt().status, "in_review")

    def test_dry_run_only_for_accept(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(self.res("engineer", "done", path="out.md", dry_run=True), 1)
        self.assertIn("только для accept", err.getvalue())

    def test_default_unchanged_first_refusal_only(self):
        self._done()
        before = self.path.read_text(encoding="utf-8")
        rc, _, err = self._accept(tail="-", decisions=None, numbers=None)
        self.assertEqual(rc, 1)
        self.assertIn("--decisions", err)
        self.assertNotIn("это хвост", err)
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)

    def _error(self, ticket=None):
        with contextlib.redirect_stdout(io.StringIO()):
            ER.cmd_add(type("A", (), dict(title="ложный хвост", ticket=ticket or self.tid, what=""))(), TK.PROJECT_ROOT)

    def test_false_positive_accepts_with_error_record_and_no_return(self):
        self._done()
        self._error()
        rc, _, _ = self._accept(tail="-", decisions="-", numbers="-", false_positive="E-001")
        self.assertEqual(rc, 0)
        self.assertIn("ложное срабатывание: E-001 (хвосты)", self.tkt().log[-1].text)
        self.assertEqual(self.tkt().header.get("next"), "")

    def test_false_positive_needs_error_for_this_ticket(self):
        self._done()
        for fp, setup in (("E-001", None), ("E-001", "TK-999"), ("E-7", None)):
            with self.subTest(fp=fp, setup=setup):
                if setup:
                    self._error(setup)
                rc, _, err = self._accept(tail="-", decisions="-", numbers="-", false_positive=fp)
                self.assertEqual(rc, 1)
                self.assertIn("--false-positive", err)

    def test_false_positive_does_not_lift_other_gates(self):
        self._done()
        self._error()
        rc, _, err = self._accept(tail="-", decisions=None, numbers="-", false_positive="E-001")
        self.assertEqual(rc, 1)
        self.assertIn("--decisions", err)

    def _comment(self, text, author="engineer"):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = TK.cmd_comment(type("A", (), dict(id=self.tid, author=author, text=text, next=None))())
        return rc, err.getvalue()

    def test_labels_at_write_refuse_bad_contract_and_write_nothing(self):
        for text, part in (("Договор: [тест] зелёный", "без команды"), ("Договор: [запуск: ] ок", "без команды"),
                           ("Договор: [тест: python -m pytest] ок", "полный прогон"),
                           ("Договор: [запуск: python -c \"print(1)\"] ок", "не допущена")):
            with self.subTest(text=text):
                before = self.path.read_text(encoding="utf-8")
                rc, err = self._comment(text)
                self.assertEqual(rc, 1)
                self.assertIn(part, err)
                self.assertEqual(self.path.read_text(encoding="utf-8"), before)

    def test_labels_at_write_pass_good_contract_and_foreign_entries(self):
        self.assertEqual(self._comment("Договор: [тест: python -m pytest .claude/dispatcher/test_tk089.py] ок; [чтение кода] п.5")[0], 0)
        self.assertEqual(self._comment("замечание: [тест] без команды", author="judge")[0], 0)
        self.assertEqual(self._comment("[итог: pr] PR #7 — цитата [тест]")[0], 0)


if __name__ == "__main__":
    unittest.main()
