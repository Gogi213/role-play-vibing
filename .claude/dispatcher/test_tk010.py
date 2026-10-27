"""Тесты TK-010 (раунд ревью 1, правки Б): места, где копии одного и того же расходились, теперь читают одно значение."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ceo_inbox as CI  # noqa: E402
import ticket as T  # noqa: E402
import watch as W  # noqa: E402
import worktree_hygiene as WH  # noqa: E402

STATUSES = ("backlog", "todo", "in_progress", "waiting", "in_review", "blocked", "needs_owner", "stopped", "done", "cancelled")


class ClosedStatuses(unittest.TestCase):
    """«Закрыт»: watch.py знал done/cancelled, worktree_hygiene.py — done/stopped (`cancelled` как статус тикета нигде не ставится)."""

    def test_one_definition(self):
        self.assertEqual(T.CLOSED_STATUSES, ("done", "stopped"))
        for mod in (W, WH):
            self.assertFalse(hasattr(mod, "CLOSED") or hasattr(mod, "CLOSED_TICKET_STATUSES"), mod.__name__)

    def test_worktree_hygiene_uses_it(self):
        for st in STATUSES:
            with self.subTest(status=st):
                self.assertEqual(WH.is_closed(Path("x/tk007"), None, {"TK-007": st}), st in T.CLOSED_STATUSES)


class InboxMark(unittest.TestCase):
    """Отметка «прочитано» очереди CEO: хук читал `.ceo-inbox-seen`, `tickets.py inbox` — `.ceo-inbox-fallback-seen`."""

    def test_one_marker_name_in_code_and_gitignore(self):
        here = Path(__file__).resolve().parent
        self.assertEqual(CI.SEEN_NAME, ".ceo-inbox-seen")
        for src in (here / "tickets.py", here.parent / "hooks" / "role_memory.py"):
            self.assertNotIn("fallback-seen", src.read_text(encoding="utf-8"), src.name)
        self.assertIn(CI.SEEN_NAME, (here / ".gitignore").read_text(encoding="utf-8").split())

    def test_read_and_mark(self):
        d = Path(tempfile.mkdtemp())
        inbox, seen_f = d / "ceo-inbox.md", d / CI.SEEN_NAME
        self.assertEqual(CI.read(inbox, seen_f), ([], 0))
        inbox.write_text("a" + chr(10) + "b" + chr(10), encoding="utf-8")
        self.assertEqual(CI.read(inbox, seen_f), (["a", "b"], 0))
        CI.mark(seen_f, 2)
        self.assertEqual(CI.read(inbox, seen_f), (["a", "b"], 2))
        inbox.write_text("c" + chr(10), encoding="utf-8")  # файл усечён: отметка больше длины — с начала
        self.assertEqual(CI.read(inbox, seen_f), (["c"], 0))


if __name__ == "__main__":
    unittest.main()
