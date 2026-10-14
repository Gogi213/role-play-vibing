"""Стандарт сигналов CEO: строка в ceo-inbox.md и короткая копия в ceo-wake.log (Monitor CEO); `tickets.py inbox` печатает новые строки."""
import contextlib
import io
import re
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import dispatch as D  # noqa: E402
import tickets as TK  # noqa: E402


HEAD = chr(10).join(["---", "id: TK-001", "title: t", "owner: engineer", "status: in_progress", "reviewer: ", "wait_for: ",
                  "updated: 2026-10-06T10:00:00+04:00", "---", "", "d", "", "## Лог", ""])


class SignalsTest(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        saved = D.CEO_INBOX, D.CEO_WAKE_LOG, D.DISPATCHER_DIR
        self.addCleanup(lambda: setattr(D, "CEO_INBOX", saved[0]) or setattr(D, "CEO_WAKE_LOG", saved[1])
                        or setattr(D, "DISPATCHER_DIR", saved[2]))
        D.CEO_INBOX, D.CEO_WAKE_LOG, D.DISPATCHER_DIR = self.d / "ceo-inbox.md", self.d / "ceo-wake.log", self.d

    def inbox(self, *a):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = TK.cmd_inbox(SimpleNamespace(peek="--peek" in a))
        return rc, out.getvalue()

    def test_every_kind_reaches_inbox_and_short_wake_line(self):
        kinds = ["blocked", "needs_owner", "watch-deck", "done", "next-ceo", "wait-for", "model", "watch-summary", "работа-идёт"]
        for k in kinds:
            D.append_ceo_inbox("TK-1", k, f"текст {k}")
        lines = D.CEO_INBOX.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), len(kinds))
        for k, ln in zip(kinds, lines):
            self.assertRegex(ln, rf"^- \S+ TK-1 \[{re.escape(k)}\] текст {re.escape(k)}$")
        wake = D.CEO_WAKE_LOG.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(wake), len(kinds), "каждый сигнал будит Monitor коротким повтором в ceo-wake.log")
        self.assertTrue(all("текст" not in ln for ln in wake), "деталей в будильнике нет — они в ceo-inbox.md")

    def test_mechanics_kinds_go_to_log_only(self):
        D.append_ceo_inbox("*", "blind", "ssh:gpu1 недоступна")
        self.assertFalse(D.CEO_INBOX.exists())
        self.assertFalse(D.CEO_WAKE_LOG.exists())
        self.assertIn("[blind] ssh:gpu1 недоступна", (self.d / "mechanics.log").read_text(encoding="utf-8"))

    def test_inbox_prints_new_lines_once(self):
        D.append_ceo_inbox("TK-2", "blocked", "первая")
        rc, text = self.inbox()
        self.assertEqual(rc, 0)
        self.assertIn("TK-2 [blocked] первая", text)
        self.assertIn("пуста", self.inbox()[1], "прочитанное второй раз не показывается")
        D.append_ceo_inbox("TK-3", "done", "вторая")
        text = self.inbox()[1]
        self.assertIn("вторая", text)
        self.assertNotIn("первая", text)

    def test_inbox_peek_does_not_move_mark(self):
        D.append_ceo_inbox("*", "watch-deck", "тревога")
        self.assertIn("тревога", self.inbox("--peek")[1])
        self.assertIn("тревога", self.inbox("--peek")[1])
        self.assertIn("тревога", self.inbox()[1])
        self.assertIn("пуста", self.inbox()[1])

    def test_inbox_without_file_is_empty(self):
        rc, text = self.inbox()
        self.assertEqual(rc, 0)
        self.assertIn("пуста", text)

    def test_inbox_after_file_truncated_starts_over(self):
        for i in range(3):
            D.append_ceo_inbox("TK-4", "done", f"запись {i}")
        self.inbox()
        D.CEO_INBOX.write_text("- 2026-10-14T10:00:00+04:00 TK-5 [done] новая\n", encoding="utf-8")
        self.assertIn("новая", self.inbox()[1])

    def test_summary_flush_goes_to_inbox(self):
        D.flush_pending_summary({"pending_summary": ["a", "b"]}, D.T.parse_dt("2026-10-06T16:00:00+04:00"))
        self.assertIn("[summary] 2 сигнал(ов): a | b", D.CEO_INBOX.read_text(encoding="utf-8"))

    def test_comment_writes_no_signal(self):
        """Запись с «ВОПРОС ВЛАДЕЛЬЦУ…» и `--next ceo` сама ничего не шлёт: сигнал CEO даёт диспетчер (handle_next_ceo, с дедупом)."""
        tickets = self.d / "tickets"
        tickets.mkdir()
        (tickets / "TK-001.md").write_text(HEAD, encoding="utf-8")
        orig_dir, TK.TICKETS_DIR = TK.TICKETS_DIR, tickets
        try:
            args = lambda text, nxt=None: SimpleNamespace(id="TK-001", author="engineer", text=text, next=nxt)  # noqa: E731
            with contextlib.redirect_stdout(io.StringIO()):
                TK.cmd_comment(args("ВОПРОС ВЛАДЕЛЬЦУ: можно?"))
                TK.cmd_comment(args("готово", "ceo"))
        finally:
            TK.TICKETS_DIR = orig_dir
        self.assertFalse(D.CEO_INBOX.exists())
        self.assertFalse(D.CEO_WAKE_LOG.exists())

    def test_no_ceo_file_writes_outside_ceo_file_write(self):
        """Мимо стандарта писать ceo-inbox/ceo-wake нельзя: открытие на запись — только в _ceo_file_write."""
        src = (HERE / "dispatch.py").read_text(encoding="utf-8")
        for m in re.finditer(r'open\((CEO_INBOX|CEO_WAKE_LOG)[^)]*"a"', src):
            fn = src.rfind("\ndef ", 0, m.start())
            self.assertTrue(src[fn:].startswith("\ndef _ceo_file_write"), src[fn:fn + 60])
        for name in ("watch.py", "tickets.py", "ticket.py", "supervise.py"):
            t = (HERE / name).read_text(encoding="utf-8")
            self.assertNotRegex(t, r'open\([^)]*(ceo-inbox|ceo-wake|CEO_INBOX|CEO_WAKE_LOG)[^)]*"a"', name)


if __name__ == "__main__":
    unittest.main()
