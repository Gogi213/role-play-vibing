"""Тесты TK-010 (раунд ревью 1, правки Б): места, где копии одного и того же расходились, теперь читают одно значение."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
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


if __name__ == "__main__":
    unittest.main()
