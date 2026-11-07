"""Тесты TK-131 (E-037): сметание не трогает копию закрытого тикета с ходом роли; приёмка не гоняет проверки в мёртвой копии."""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dispatch as D  # noqa: E402
import errors as E  # noqa: E402
import project_paths as CFG  # noqa: E402
import tickets_proof as TP  # noqa: E402

CONTRACT_CMD = "python -m pytest a/test_x.py"


def git(cwd, *a):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=cwd, check=True, capture_output=True)


class Sandbox(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        git(self.root, "init", "-q", "-b", "main")
        (self.root / "a.txt").write_text("a")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-qm", "init")
        self.tickets = self.root / ".claude" / "tickets"
        self.tickets.mkdir(parents=True)
        self.copies = self.root / ".claude" / "worktrees"
        self.copies.mkdir(parents=True)

    def ticket(self, tid, status, nxt=""):
        (self.tickets / f"{tid}.md").write_text(
            f"---\nid: {tid}\ntitle: t\nowner: engineer\nstatus: {status}\nreviewer: judge\nwait_for: \nnext: {nxt}\n"
            f"updated: 2026-11-08T00:00:00+04:00\n---\n\n## Лог\n", encoding="utf-8")

    def copy(self, tid):
        wt = self.copies / tid
        git(self.root, "worktree", "add", "-q", str(wt), "-b", tid.lower())
        return wt

    def sweep(self):
        with mock.patch.object(CFG, "TICKETS_DIR", self.tickets), mock.patch.object(CFG, "PROJECT_ROOT", self.root):
            D.sweep_closed_worktrees()


class SweepTests(Sandbox):
    def test_closed_ticket_with_next_keeps_copy(self):
        self.ticket("TK-009", "done", nxt="judge")
        wt = self.copy("TK-009")
        self.sweep()
        self.assertTrue((wt / "a.txt").is_file())

    def test_closed_ticket_without_next_loses_copy(self):
        self.ticket("TK-009", "done")
        wt = self.copy("TK-009")
        self.sweep()
        self.assertFalse(wt.exists())

    def test_stopped_ticket_with_next_keeps_copy_until_next_is_gone(self):
        self.ticket("TK-009", "stopped", nxt="judge")
        wt = self.copy("TK-009")
        self.sweep()
        self.assertTrue(wt.exists())
        self.ticket("TK-009", "stopped")
        self.sweep()
        self.assertFalse(wt.exists())


class DeadCopyTests(Sandbox):
    def test_missing_live_and_dead_copy(self):
        self.assertIsNone(E.dead_copy(self.root, "TK-9"))                 # копии нет — запасной путь в корне, как прежде
        wt = self.copy("TK-9")
        self.assertIsNone(E.dead_copy(self.root, "TK-9"))                 # живой checkout
        self.assertIsNone(E.dead_copy(self.root, ""))                     # тикета нет — копии нет
        dead = self.copies / "TK-8"
        dead.mkdir()
        self.assertEqual(E.dead_copy(self.root, "TK-8"), dead)            # пустой каталог
        (dead / ".git").write_text("gitdir: x")
        self.assertEqual(E.dead_copy(self.root, "TK-8"), dead)            # остался один .git
        self.assertTrue(wt.exists())

    def test_gate_refuses_dead_copy_as_not_checked(self):
        (self.copies / "TK-8").mkdir()
        out = E.gate(self.root, "TK-8")
        self.assertIn("Не проверено", out)
        self.assertIn("не вина работы", out)
        self.assertNotIn("регрессия", out)
        self.assertEqual(E.gate(self.root, "TK-7"), "")                   # копии нет, исправленных ошибок нет — принять можно

    def test_proof_ack_refuses_dead_copy_as_not_checked(self):
        (self.copies / "TK-8").mkdir()
        tkt = mock.Mock(owner="engineer", log=[mock.Mock(author="engineer", text=f"Договор: 1 [тест: {CONTRACT_CMD}]")])
        refusal, note = TP.proof_ack("TK-8", tkt, self.root)
        self.assertIn("Не проверено", refusal)
        self.assertNotIn("не допущена", refusal)
        self.assertNotIn("упала", refusal)
        self.assertEqual(note, "")

    def test_proof_ack_without_copy_still_runs_in_root(self):
        tkt = mock.Mock(owner="engineer", log=[mock.Mock(author="engineer", text=f"Договор: 1 [тест: {CONTRACT_CMD}]")])
        refusal, _ = TP.proof_ack("TK-7", tkt, self.root)
        self.assertIn("не допущена", refusal)                             # путь обработан в корне, как прежде (теста там нет)


if __name__ == "__main__":
    unittest.main()
