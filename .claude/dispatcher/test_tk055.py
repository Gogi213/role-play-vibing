"""TK-055 (хвост TK-050): команда договора `python -m pytest -n auto …` (xdist) допущена; значение `-n` — не цель проверки."""
from __future__ import annotations

import unittest

import test_result as TR  # первым: задаёт песочницу CLAUDE_PROJECT_DIR
import errors as ER
import test_tk050 as T50

TEST = "tests/test_ok.py"


class XdistCommandTests(unittest.TestCase):
    setUp, tearDown, res, tkt, refused, _git = (TR.ResultTests.setUp, TR.ResultTests.tearDown, TR.ResultTests.res,
                                                TR.ResultTests.tkt, TR.ResultTests.refused, TR.ResultTests._git)
    _contract, accept = T50.ProofTests._contract, T50.ProofTests.accept

    def _repo(self):
        self._git("init", "-q")
        (self.base / "tests").mkdir()
        (self.base / TEST).write_text("def test_ok():\n    assert True\n", encoding="utf-8")
        self._git("add", TEST)
        self._git("commit", "-qm", "t")

    def test_n_flag_allowed_with_workers_and_not_a_target(self):
        self._repo()
        for spec in ("python -m pytest -n auto -q tests/test_ok.py", "python -m pytest tests -n 4 -q",
                     "python -m pytest -n logical tests/test_ok.py -k ok"):
            argv, why = ER.check_argv(spec, self.base)
            self.assertIsNotNone(argv, (spec, why))
            self.assertNotIn("auto", ER.check_targets(argv, self.base))
            self.assertTrue(all(t.startswith("tests") for t in ER.check_targets(argv, self.base)), spec)

    def test_n_flag_without_valid_workers_or_for_unittest_refused(self):
        self._repo()
        for spec in ("python -m pytest -n tests/test_ok.py", "python -m pytest -n lots tests/test_ok.py",
                     "python -m pytest tests/test_ok.py -n", "python -m pytest -n 0 tests/test_ok.py",
                     "python -m pytest -n 100 tests/test_ok.py", "python -m unittest -n 2 tests/test_ok.py"):
            argv, why = ER.check_argv(spec, self.base)
            self.assertIsNone(argv, spec)

    def test_n_auto_alone_is_not_a_test(self):
        self._repo()
        argv, why = ER.check_argv("python -m pytest -n auto", self.base)
        self.assertIsNone(argv)
        self.assertIn("без теста", why)

    def test_accept_reruns_contract_command_with_n_auto(self):
        self._repo()
        (self.base / "out.md").write_text("x", encoding="utf-8")
        (self.base / "review.md").write_text("x", encoding="utf-8")
        self._contract(f"ДОГОВОР. Полный прогон [запуск: python -m pytest -n auto -q {TEST}].")
        self.res("engineer", "done", path="out.md")
        rc, err = self.accept()
        self.assertEqual(rc, 0, err)

