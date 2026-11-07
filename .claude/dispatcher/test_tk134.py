"""TK-134 (E-039): приёмка без копии `.claude/worktrees/<ID>` гоняет договор не в корне на чужой ветке, а на голове PR (--sha)."""
from __future__ import annotations

import contextlib
import io
import subprocess
import unittest
from unittest import mock

import test_result as TR  # первым: задаёт песочницу CLAUDE_PROJECT_DIR
import errors as ER
import merge_rule
import tickets_proof as TP
import ticket as T

OLD = "tools/old.py"   # есть и в корне, и на голове PR
NEW = "tools/new.py"   # есть только на голове PR — в корне, стоящем на другой ветке, его нет
OK_PY = "import sys\nsys.exit(0)\n"


class AcceptTreeTests(unittest.TestCase):
    setUp, tearDown, res, tkt, _git = (TR.ResultTests.setUp, TR.ResultTests.tearDown, TR.ResultTests.res, TR.ResultTests.tkt,
                                       TR.ResultTests._git)

    def _head(self) -> str:
        return subprocess.run(["git", "-C", str(self.base), "rev-parse", "HEAD"], check=True, capture_output=True,
                              text=True).stdout.strip()

    def _repo(self):
        """Корень стоит на ветке `stale` (есть только OLD); голова PR `feat` (OLD и NEW) — другой коммит. → sha головы PR."""
        self._git("init", "-q", "-b", "stale")
        (self.base / "tools").mkdir()
        (self.base / OLD).write_text(OK_PY, encoding="utf-8")
        self._git("add", OLD)
        self._git("commit", "-qm", "old")
        self._git("checkout", "-qb", "feat")
        (self.base / NEW).write_text(OK_PY, encoding="utf-8")
        self._git("add", NEW)
        self._git("commit", "-qm", "new")
        sha = self._head()
        self._git("checkout", "-q", "stale")
        self.assertFalse((self.base / NEW).exists())
        return sha

    def _worktrees(self) -> list[str]:
        out = subprocess.run(["git", "-C", str(self.base), "worktree", "list", "--porcelain"], check=True, capture_output=True,
                             text=True).stdout
        return [ln for ln in out.splitlines() if ln.startswith("worktree ")]

    def _ready(self, contract: str):
        sha = self._repo()
        T.append_log(self.path, "engineer", contract)
        (self.base / "out.md").write_text("x", encoding="utf-8")
        self.res("engineer", "done", path="out.md")
        T.write_header_updates(self.path, {"status": "in_review"}, now=TR.NOW)
        return sha

    def _accept_pr(self, sha: str):
        err = io.StringIO()
        with mock.patch.object(merge_rule, "merged_done", return_value=False), contextlib.redirect_stderr(err), \
                contextlib.redirect_stdout(io.StringIO()):
            rc = self.res("judge", "accept", pr=7, sha=sha, decisions="-", numbers="-")
        return rc, err.getvalue()

    def test_accept_without_copy_runs_contract_at_pr_head(self):  # сам E-039: «tools/new.py не отслеживается git»
        sha = self._ready(f"Договор: [запуск: python {NEW}] код 0")
        rc, err = self._accept_pr(sha)
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("проверки договора: 1 кмд, код 0", self.tkt().log[-2].text)

    def test_temp_copy_is_removed_after_failed_command(self):
        sha = self._ready(f"Договор: [запуск: python {NEW}] код 0")
        self._git("checkout", "-q", "feat")
        (self.base / NEW).write_text("import sys\nprint('красная')\nsys.exit(3)\n", encoding="utf-8")
        self._git("commit", "-qam", "red")
        red = self._head()
        self._git("checkout", "-q", "stale")
        before = self._worktrees()
        rc, err = self._accept_pr(red)
        self.assertEqual(rc, 1)
        self.assertIn(f"команда договора `python {NEW}` упала", err)
        self.assertIn("красная", err)
        self.assertEqual(self._worktrees(), before)   # временная копия снята и из реестра git
        self.assertNotEqual(sha, red)

    def test_ticket_copy_wins_over_sha(self):
        sha = self._ready(f"Договор: [запуск: python {OLD}] код 0")
        wt = self.base / ".claude" / "worktrees" / self.tid
        self._git("worktree", "add", "-q", "--detach", str(wt), "stale")
        cwds = []
        real = ER.run_check
        with mock.patch.object(ER, "run_check", side_effect=lambda a, c, t: cwds.append(c) or real(a, c, t)):
            self.assertEqual(TP.proof_ack(self.tid, self.tkt(), self.base, sha)[0], "")
        self.assertEqual(cwds, [wt])
        self.assertEqual(len(self._worktrees()), 2)   # корень и копия тикета — временной не появилось

    def test_unknown_or_missing_sha_runs_in_root_as_before(self):
        self._ready(f"Договор: [запуск: python {OLD}] код 0")
        for sha in ("", "b" * 40, "не-хеш"):
            with self.subTest(sha=sha):
                cwds = []
                real = ER.run_check
                with mock.patch.object(ER, "run_check", side_effect=lambda a, c, t: cwds.append(c) or real(a, c, t)):
                    self.assertEqual(TP.proof_ack(self.tid, self.tkt(), self.base, sha)[0], "")
                self.assertEqual(cwds, [self.base])

    def test_command_missing_on_pr_head_is_still_refused(self):
        sha = self._ready(f"Договор: [запуск: python {NEW}] код 0")
        self._git("checkout", "-q", "feat")
        self._git("rm", "-q", NEW)
        self._git("commit", "-qm", "rm")
        gone = self._head()
        self._git("checkout", "-q", "stale")
        refusal = TP.proof_ack(self.tid, self.tkt(), self.base, gone)[0]
        self.assertIn("не отслеживается git", refusal)
        self.assertNotEqual(sha, gone)

    def test_gate_runs_fixed_error_checks_at_pr_head(self):
        sha = self._ready("Договор: [чтение кода] правка в одном месте")
        seen = []

        def spy(root, cwd=None, **kw):
            seen.append((cwd, (cwd / NEW).is_file()))
            return []
        with mock.patch.object(ER, "run_all", side_effect=spy):
            self.assertEqual(ER.gate(self.base, self.tid, sha=sha), "")
            self.assertEqual(ER.gate(self.base, self.tid), "")
        (tree, has_new), (root_cwd, root_has_new) = seen
        self.assertTrue(has_new)
        self.assertNotEqual(tree, self.base)
        self.assertEqual((root_cwd, root_has_new), (self.base, False))
        self.assertEqual(len(self._worktrees()), 1)


if __name__ == "__main__":
    unittest.main()
