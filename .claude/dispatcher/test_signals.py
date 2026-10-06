"""Стандарт сигналов (В-192): сигнал CEO идёт в очередь шины с приоритетом и ack; при падении шины — запасной файл."""
import contextlib
import io
import os
import re
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("RPV_BUS_DISABLE", "1")
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "bus"))
import bus  # noqa: E402
import dispatch as D  # noqa: E402
import tickets as TK  # noqa: E402


HEAD = chr(10).join(["---", "id: TK-001", "title: t", "owner: engineer", "status: in_progress", "reviewer: ", "wait_for: ",
                  "updated: 2026-10-06T10:00:00+04:00", "---", "", "d", "", "## Лог", ""])


class SignalsTest(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        D.CEO_INBOX, D.CEO_WAKE_LOG = self.d / "ceo-inbox.md", self.d / "ceo-wake.log"
        self.b = bus.Bus(str(self.d / "b.db"), str(HERE.parent / "bus" / "routes.json"))
        self.srv = bus.ThreadingHTTPServer(("127.0.0.1", 0), bus.make_handler(self.b, "t"))
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.env = {k: os.environ.get(k) for k in ("RPV_BUS_URL", "RPV_BUS_TOKEN", "RPV_BUS_DISABLE")}
        self.up()

    def up(self):
        os.environ.update(RPV_BUS_URL=f"http://127.0.0.1:{self.srv.server_port}", RPV_BUS_TOKEN="t")
        os.environ.pop("RPV_BUS_DISABLE", None)

    def down(self):
        os.environ["RPV_BUS_URL"] = "http://127.0.0.1:1"

    def tearDown(self):
        self.srv.shutdown()
        for k, v in self.env.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        os.environ["RPV_BUS_DISABLE"] = "1"

    def inbox(self, *a):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = TK.cmd_inbox(SimpleNamespace(peek="--peek" in a))
        return rc, out.getvalue()

    def test_every_kind_reaches_queue_with_prio_and_is_acked(self):
        kinds = ["blocked", "needs_owner", "loop-warning", "stop-failed", "parse-error", "watch-deck", "owner-answer",
                 "bus-down", "done", "next-ceo", "wait-for", "model", "watch-summary", "bus-up"]
        for k in kinds:
            D.append_ceo_inbox("TK-1", k, f"текст {k}")
        self.assertFalse(D.CEO_INBOX.exists(), "шина жива — ceo-inbox не пишется")
        wake = D.CEO_WAKE_LOG.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(wake), len(kinds), "каждый сигнал будит Monitor коротким повтором в ceo-wake.log")
        self.assertTrue(all("текст" not in ln for ln in wake), "деталей в будильнике нет — они в очереди")
        rc, text = self.inbox()
        self.assertEqual(rc, 0)
        lines = text.splitlines()
        self.assertEqual(len(lines), len(kinds))
        for k in kinds:
            self.assertTrue(any(f" {k}: текст {k}" in ln for ln in lines), k)
        first_normal = next(i for i, ln in enumerate(lines) if ln.startswith("[обычное]"))
        self.assertTrue(all(ln.startswith("[СРОЧНО]") for ln in lines[:first_normal]), "срочное — первым")
        self.assertTrue(all(ln.startswith("[обычное]") for ln in lines[first_normal:]))
        self.assertEqual(self.b.fetch("ceo", 0), [], "ack пачкой")
        self.assertIn("пуста", self.inbox()[1])

    def test_next_ceo_with_live_bus_still_wakes_monitor(self):
        D.append_ceo_inbox("TK-9", "next-ceo", "передача")
        self.assertIn("TK-9 next-ceo", D.CEO_WAKE_LOG.read_text(encoding="utf-8"))
        self.assertIn("передача", self.inbox()[1])

    def test_ceo_queue_wake_line_has_no_details(self):
        D.ceo_queue_wake("задача.TK-5.вопрос_владельцу", 7)
        line = D.CEO_WAKE_LOG.read_text(encoding="utf-8")
        self.assertIn("вопрос_владельцу #7", line)
        self.assertFalse(D.CEO_INBOX.exists())

    def test_peek_does_not_ack(self):
        D.append_ceo_inbox("*", "watch-deck", "тревога")
        self.assertIn("СРОЧНО", self.inbox("--peek")[1])
        self.assertEqual(len(self.b.fetch("ceo", 0)), 1)

    def test_bus_down_uses_fallback_without_loss_and_inbox_shows_it_once(self):
        self.down()
        D.append_ceo_inbox("TK-2", "blocked", "нет шины")
        self.assertIn("[запасной путь]", D.CEO_INBOX.read_text(encoding="utf-8"))
        self.assertTrue(D.CEO_WAKE_LOG.exists())
        self.up()
        rc, text = self.inbox()
        self.assertEqual(rc, 0)
        self.assertIn("нет шины", text)
        self.assertIn("пуста", self.inbox()[1], "запасная строка показана один раз")

    def test_inbox_with_bus_down_reports_and_still_shows_fallback(self):
        self.down()
        D.append_ceo_inbox("TK-3", "wait-for", "x")
        rc, text = self.inbox()
        self.assertEqual(rc, 1)
        self.assertIn("шина недоступна", text)
        self.assertIn("[запасной путь]", text)

    def test_summary_flush_goes_through_bus(self):
        D.flush_pending_summary({"pending_summary": ["a", "b"]}, D.T.parse_dt("2026-10-06T16:00:00+04:00"))
        self.assertEqual([e["payload"]["kind"] for e in self.b.fetch("ceo", 0)], ["summary"])

    def test_comment_kinds(self):
        events = []
        orig = TK.bus_emit
        TK.bus_emit = lambda tid, kind, payload: events.append((kind, payload))
        try:
            ticket = self.d / "tickets"
            ticket.mkdir()
            orig_dir = TK.TICKETS_DIR
            TK.TICKETS_DIR = ticket
            (ticket / "TK-001.md").write_text(HEAD, encoding="utf-8")
            args = lambda text, nxt=None: SimpleNamespace(id="TK-001", author="engineer", text=text, next=nxt)  # noqa: E731
            with contextlib.redirect_stdout(io.StringIO()):
                TK.cmd_comment(args("ВОПРОС ВЛАДЕЛЬЦУ: можно?"))
                TK.cmd_comment(args("готово", "ceo"))
        finally:
            TK.bus_emit = orig
            TK.TICKETS_DIR = orig_dir
        self.assertEqual(events[0][0], "вопрос_владельцу")
        self.assertEqual(events[0][1]["prio"], "urgent")
        self.assertEqual(events[1][0], "сдано", "к_ceo пишет только диспетчер — без дубля")

    def test_no_ceo_file_writes_outside_fallback(self):
        """Мимо стандарта писать ceo-inbox/ceo-wake нельзя: открытие на запись — только в _ceo_file_write."""
        src = (HERE / "dispatch.py").read_text(encoding="utf-8")
        for m in re.finditer(r'open\((CEO_INBOX|CEO_WAKE_LOG)[^)]*"a"', src):
            fn = src.rfind("\ndef ", 0, m.start())
            self.assertTrue(src[fn:].startswith(("\ndef _ceo_file_write", "\ndef ceo_queue_wake")), src[fn:fn + 60])
        for name in ("watch.py", "tickets.py", "ticket.py", "bus_link.py", "supervise.py"):
            t = (HERE / name).read_text(encoding="utf-8")
            self.assertNotRegex(t, r'open\([^)]*(ceo-inbox|ceo-wake|CEO_INBOX|CEO_WAKE_LOG)[^)]*"a"', name)

    def test_ceo_queue_not_stale_after_dispatcher_threshold(self):
        t = [1000.0]
        b = bus.Bus(str(self.d / "s.db"), str(HERE.parent / "bus" / "routes.json"), clock=lambda: t[0])
        b.post("задача.TK-1.к_ceo", {}, "z")
        b.post("задача.TK-1.задание.готово", {}, "y")
        t[0] += 3600
        self.assertEqual([x["recipient"] for x in b.stale()], ["dispatcher"], "CEO опрашивает редко — не просрочка")
        t[0] += 4000
        self.assertIn("ceo", [x["recipient"] for x in b.stale()])


if __name__ == "__main__":
    unittest.main()
