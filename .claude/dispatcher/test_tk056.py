"""TK-056 (В-21): команда договора — только тесты самой правки; полный прогон pytest приёмка отказывает, его делает CI PR."""
from __future__ import annotations

import unittest
from pathlib import Path

import test_result as TR  # первым: задаёт песочницу CLAUDE_PROJECT_DIR
import errors as ER
import test_tk050 as T50
import tickets_proof as TP

TEST = "tests/test_ok.py"
ROOT = Path(__file__).resolve().parents[2]
FULL = ("python -m pytest -n auto", "python -m pytest", "python -m pytest -q -n 4", "python -m pytest .claude",
        "python -m pytest .claude/", "python -m pytest ./.claude", "python -m pytest .", "python -m pytest -n auto .claude",
        f"python -m pytest {TEST} .claude", "python3 -m pytest -n auto -q .")
NARROW = (f"python -m pytest {TEST}", f"python -m pytest -n auto -q {TEST} -k ok", ".claude/dispatcher/test_x.py",
          "python -m pytest .claude/dispatcher/test_x.py", "python -m pytest .claude/dispatcher", "python -m pytest tools",
          f"python -m pytest {TEST}::test_ok", "python -m unittest tests.test_ok", "python tools/chk.py")


class FullRunTests(unittest.TestCase):
    setUp, tearDown, res, tkt, refused, _git = (TR.ResultTests.setUp, TR.ResultTests.tearDown, TR.ResultTests.res,
                                                TR.ResultTests.tkt, TR.ResultTests.refused, TR.ResultTests._git)
    _contract, accept = T50.ProofTests._contract, T50.ProofTests.accept

    def _repo(self):
        self._git("init", "-q")
        (self.base / "tests").mkdir()
        (self.base / TEST).write_text("def test_ok():\n    assert True\n", encoding="utf-8")
        self._git("add", TEST)
        self._git("commit", "-qm", "t")
        (self.base / "out.md").write_text("x", encoding="utf-8")
        (self.base / "review.md").write_text("x", encoding="utf-8")

    def test_full_run_recognised(self):
        for cmd in FULL:
            self.assertTrue(TP.full_run(cmd), cmd)

    def test_narrow_run_not_full_run(self):
        for cmd in NARROW:
            self.assertFalse(TP.full_run(cmd), cmd)

    def test_accept_refuses_full_run_with_hint_and_writes_nothing(self):
        self._repo()
        for cmd in ("python -m pytest -n auto", "python -m pytest .claude", "python -m pytest ."):
            self._contract(f"Договор: [тест: {cmd}] всё зелёное")
            self.res("engineer", "done", path="out.md")
            before = self.path.read_text(encoding="utf-8")
            rc, err = self.accept()
            self.assertEqual(rc, 1, cmd)
            self.assertIn("полный прогон проекта", err)
            self.assertIn("зелёный CI PR", err)
            self.assertIn("тесты самой правки", err)
            self.assertEqual(self.path.read_text(encoding="utf-8"), before, cmd)

    def test_accept_takes_narrow_test_not_full_run(self):
        self._repo()
        self._contract(f"Договор: [тест: python -m pytest -n auto -q {TEST}] тесты правки зелёные")
        self.res("engineer", "done", path="out.md")
        rc, err = self.accept()
        self.assertEqual(rc, 0, err)

    def test_check_targets_unchanged_by_shared_loop(self):
        self._repo()
        argv, why = ER.check_argv(f"python -m pytest -n 4 {TEST}::test_ok -k ok", self.base)
        self.assertIsNotNone(argv, why)
        self.assertEqual(ER.check_targets(argv, self.base), [TEST])
        self.assertEqual(ER.command_targets(argv, "pytest"), ([f"{TEST}::test_ok"], ""))
        self.assertEqual(ER.command_targets(["python", "-m", "pytest", "--bad", "x.py"], "pytest"), ([], "--bad"))


class TemplatesTests(unittest.TestCase):
    NAMES = ("README.md", "engineer.md", "researcher.md")

    def test_templates_carry_narrow_command_rule(self):
        for name in self.NAMES:
            text = " ".join((ROOT / "templates" / "roles" / name).read_text(encoding="utf-8").split())
            self.assertIn("только на тесты самой правки", text, name)
            self.assertIn("зелёный CI PR", text, name)
            self.assertIn("полный прогон", text, name)


if __name__ == "__main__":
    unittest.main()
