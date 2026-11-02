"""TK-089 (В-36): `accept --dry-run` — все отказы гейтов одним списком; «ложное срабатывание» с E-NNN; ярлыки при записи."""
from __future__ import annotations

import contextlib
import io
import re
import unittest
from pathlib import Path
from unittest import mock

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


    def _fields_only(self):
        return mock.patch.object(TK.P, "settings", return_value={"tail_fields_only": True})

    def _done_left(self, left, why=BAD_TAIL):
        self._registry_with_decision()
        (self.base / "out.md").write_text("готово", encoding="utf-8")
        (self.base / "review.md").write_text("вердикт", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.res("engineer", "done", path="out.md", why=why, left=left), 0)

    def test_tail_fields_only_ignores_free_why_and_scans_left_field(self):
        with self._fields_only():
            self._done_left("-")  # хвостовые слова только в --why, поле «ничего»
            rc, _, err = self._accept(tail="-", decisions="-", numbers="-")
            self.assertEqual(rc, 0, err)
            self.assertIn("хвосты: нет", self.tkt().log[-1].text)

    def test_tail_fields_only_left_field_with_tail_is_refused(self):
        with self._fields_only():
            self._done_left("не сделан п.3", why="Готово, тесты зелёные")
            rc, _, err = self._accept(tail="-", decisions="-", numbers="-")
            self.assertEqual(rc, 1)
            self.assertIn("это хвост", err)

    def test_tail_fields_only_empty_remainders_are_not_tails(self):
        for left in ("ничего", "нет", "приёмка Судьи"):
            with self.subTest(left=left), self._fields_only():
                self._done_left(left, why="Готово")
                rc, _, err = self._accept(tail="-", decisions="-", numbers="-", dry_run=True)
                self.assertEqual(rc, 0, err)

    def test_tail_fields_only_requires_left_on_done_and_default_scans_why(self):
        with self._fields_only(), contextlib.redirect_stderr(io.StringIO()) as err:
            (self.base / "out.md").write_text("готово", encoding="utf-8")
            self.assertEqual(self.res("engineer", "done", path="out.md"), 1)
        self.assertIn("--left", err.getvalue())
        self._done()  # флага нет: умолчание = старое, хвост в --why по-прежнему отказ
        rc, _, err2 = self._accept(tail="-", decisions="-", numbers="-")
        self.assertEqual((rc, "это хвост" in err2), (1, True))


class ReturnFixesTests(unittest.TestCase):
    """Возврат Судьи по PR #203@e6dea84 (docs/reviews/TK-089-critic.md): dry-run не падает и не путает «не запустилось» с
    регрессией; `--false-positive` снимает только эвристику; сдача без поля «остаток» при tail_fields_only — отказ."""
    setUp, tearDown, res, tkt, refused, _registry_with_decision = (
        TR.ResultTests.setUp, TR.ResultTests.tearDown, TR.ResultTests.res, TR.ResultTests.tkt, TR.ResultTests.refused,
        TR.ResultTests._registry_with_decision)
    _done, _accept, _error, _fields_only = GatesTests._done, GatesTests._accept, GatesTests._error, GatesTests._fields_only

    def _ok(self):
        self._done("Готово, тесты зелёные")

    def test_dry_run_pr_without_sha_is_a_line_not_a_crash(self):
        self._ok()
        import merge_rule
        with mock.patch.object(merge_rule, "check_pr", side_effect=AttributeError("sha.lower() от None")):
            rc, _, err = self._accept(pr=7, sha=None, dry_run=True, tail="-", decisions="-", numbers="-")
        self.assertEqual(rc, 1)
        self.assertIn("--sha", err)
        self.assertNotIn("Traceback", err)

    def test_dry_run_pr_without_repo_is_a_line(self):
        self._ok()
        self.settings = {}
        rc, _, err = self._accept(pr=7, sha="a" * 40, dry_run=True, tail="-", decisions="-", numbers="-")
        self.assertEqual(rc, 1)
        self.assertIn("репозиторий не задан", err)

    def test_dry_run_unrun_check_is_not_a_regression(self):
        self._ok()

        def unrun(root, cwd=None, timeout=None, report=None, infra=None):
            infra.append(("E-001", "проверка", "таймаут"))
            return []

        with mock.patch.object(ER, "run_all", unrun):
            rc, _, err = self._accept(dry_run=True, tail="-", decisions="-", numbers="-")
        self.assertEqual(rc, 1)
        self.assertIn("не проверено, не return", err)
        self.assertNotIn("регрессия", err)

    def test_dry_run_green_hint_lists_no_ci_gate(self):
        self.assertNotIn("CI", GATES_LINE.search(JUDGE_TEMPLATE).group(0))

    def _numbers_table(self, row):
        (self.base / "tbl.md").write_text(f"| число | файл:строка |\n|---|---|\n{row}\n", encoding="utf-8")

    def test_false_positive_does_not_lift_structural_refusals(self):
        self._ok()
        self._error()
        self._numbers_table("| 42 | out.md:1 |")  # в out.md числа 42 нет
        for kw, part in ((dict(numbers="nofile.md", tail="-"), "файла нет в проекте"),
                         (dict(numbers="tbl.md", tail="-"), "не сходятся с источником"),
                         (dict(numbers="-", tail="TK-999"), "нужен номер"),
                         (dict(numbers="-", tail="ceo:x"), "напиши, что сделать CEO")):
            with self.subTest(kw=kw):
                rc, _, err = self._accept(decisions="-", false_positive="E-001", **kw)
                self.assertEqual(rc, 1)
                self.assertIn(part, err)

    def test_false_positive_lifts_heuristic_numbers_refusal(self):
        self._ok()
        self._error()
        (self.base / "review.md").write_text("Метрика выросла до 431 строки, было 77", encoding="utf-8")
        rc, _, err = self._accept(decisions="-", tail="-", numbers="-")
        self.assertEqual(rc, 1)
        self.assertIn("в сдаче есть числа", err)
        rc, _, err = self._accept(decisions="-", tail="-", numbers="-", false_positive="E-001")
        self.assertEqual(rc, 0, err)
        self.assertIn("ложное срабатывание: E-001 (числа)", self.tkt().log[-1].text)

    def test_tail_fields_only_submission_without_left_field_is_refused(self):
        self._ok()  # сдача записана до включения ключа: поля «остаток» нет
        self._error()
        with self._fields_only():
            rc, _, err = self._accept(tail="-", decisions="-", numbers="-")
            self.assertEqual(rc, 1)
            self.assertIn("нет поля «остаток»", err)
            self.assertNotIn("--false-positive", err)  # структурный отказ эвристикой не снимается
            rc, _, err = self._accept(tail="-", decisions="-", numbers="-", false_positive="E-001")
            self.assertEqual(rc, 1)
            self.assertIn("нет поля «остаток»", err)
            rc, _, err = self._accept(tail="-", decisions="-", numbers="-", dry_run=True)
            self.assertEqual(rc, 1)
            self.assertIn("нет поля «остаток»", err)


GATES_LINE = re.compile(r"Отказ гейта — сначала `--dry-run`.*?\(([^)]*)\)")
JUDGE_TEMPLATE = (Path(__file__).resolve().parents[2] / "templates" / "roles" / "judge.md").read_text(encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
