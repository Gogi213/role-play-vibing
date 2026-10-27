"""Тесты TK-010 (раунд ревью 1, правки Б): места, где копии одного и того же расходились, теперь читают одно значение."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ceo_inbox as CI  # noqa: E402
import dispatch as D  # noqa: E402
import doctor as DOC  # noqa: E402
import heartbeat as HB  # noqa: E402
import supervise as SV  # noqa: E402
import ticket as T  # noqa: E402
import transcript as TR  # noqa: E402
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


def _row(ctx_in, out=0, side=False):
    row = {"type": "assistant", "message": {"usage": {
        "input_tokens": ctx_in, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "output_tokens": out}}}
    if side:
        row["isSidechain"] = True
    return json.dumps(row)


class UsageKeys(unittest.TestCase):
    """Контекст хода: диспетчер суммировал 3 ключа usage и пропускал `isSidechain`, хук — 4 (с выводом) и не пропускал."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        proj = self.dir / "slug"
        proj.mkdir()
        self.path = proj / "sess-1.jsonl"

    def write(self, *rows):
        self.path.write_text(chr(10).join(rows) + chr(10), encoding="utf-8")

    def test_one_definition_of_context(self):
        self.assertEqual(TR.CONTEXT_KEYS, ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
        self.assertIs(D._tokens_of, TR.context_of)
        self.assertEqual(TR.context_of({"input_tokens": 1, "cache_read_input_tokens": 2, "cache_creation_input_tokens": 4,
                                        "output_tokens": 800}), 7)

    def test_output_not_counted_and_sidechain_skipped(self):
        self.write(_row(100, out=7), _row(5000, out=1, side=True))
        self.assertEqual(TR.last_turn_tokens(self.path), 100)

    def test_dispatch_reads_through_it(self):
        self.write(_row(100, out=7), _row(5000, out=1, side=True))
        old, D.CLAUDE_PROJECTS_DIR = D.CLAUDE_PROJECTS_DIR, self.dir
        try:
            self.assertEqual(D._transcript_last_turn_tokens("sess-1"), 100)
            self.assertEqual(D._transcript_last_turn_tokens("../sess-1"), 0)
            self.assertEqual(D._transcript_last_turn_tokens("nope"), 0)
        finally:
            D.CLAUDE_PROJECTS_DIR = old

    def test_broken_rows_are_skipped(self):
        self.write(_row(100), '{"usage": [', '[1, 2, "usage"]', '{"message": "usage"}')
        self.assertEqual(TR.last_turn_tokens(self.path), 100)
        self.assertEqual(TR.last_turn_tokens(self.dir / "none.jsonl"), 0)


class HeartbeatStale(unittest.TestCase):
    """Свежесть сердцебиения: сторож служб ждал 600 с, хук подсказок — 240 с. Теперь порог один — `heartbeat.STALE_S`."""

    def test_one_threshold(self):
        self.assertEqual(HB.STALE_S, 600.0)
        self.assertEqual(SV.STALE_S, HB.STALE_S)
        self.assertIs(SV.heartbeat_age, HB.read_age)
        src = (Path(__file__).resolve().parent.parent / "hooks" / "role_memory.py").read_text(encoding="utf-8")
        self.assertNotIn("WATCH_STALE_S", src)
        self.assertIn("heartbeat.STALE_S", src)

    def test_age(self):
        now = datetime.now().astimezone()
        f = Path(tempfile.mkdtemp()) / "hb.json"
        f.write_text(json.dumps({"ts": (now - timedelta(seconds=90)).isoformat()}), encoding="utf-8")
        self.assertAlmostEqual(HB.read_age(f, "ts", now.timestamp()), 90, delta=1)
        f.write_text(json.dumps({"ts": (now - timedelta(seconds=90)).isoformat().replace("+00:00", "Z")}), encoding="utf-8")
        self.assertIsNotNone(HB.read_age(f, "ts", now.timestamp()))
        self.assertIsNone(HB.read_age(f.with_name("none.json"), "ts", now.timestamp()))

    def test_decide_and_doctor_share_it(self):
        self.assertEqual(SV.decide(HB.STALE_S - 1, True), "ok")
        self.assertEqual(SV.decide(HB.STALE_S + 1, True), "restart")
        self.assertIs(DOC.SV, SV)


if __name__ == "__main__":
    unittest.main()
