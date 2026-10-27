"""TK-010: один разбор шапки `---` ... `---` (project.frontmatter) для тикета, ошибки, цели, роли и Надзирателя;
`errors._set_header` пишет под замком записи."""
import sys
import tempfile
import threading
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import errors as E  # noqa: E402
import haiku_aux as HA  # noqa: E402
import overseer as O  # noqa: E402
import project as P  # noqa: E402
import registry as R  # noqa: E402
import ticket as T  # noqa: E402

TEXT = "---\nid: X-1\n\ntitle: заголовок: с двоеточием\nstatus: open\n---\nописание\n\n## Лог\n\n### 2026-10-27T07:00:00+04:00 ceo\nзапись\n"


class FrontmatterTests(unittest.TestCase):
    def test_parse_lf_crlf_blank_lines_and_no_header(self):
        want = {"id": "X-1", "title": "заголовок: с двоеточием", "status": "open"}
        self.assertEqual(P.frontmatter(TEXT)[0], want)
        self.assertEqual(TEXT[P.frontmatter(TEXT)[1]:].split("\n")[0], "описание")
        crlf = TEXT.replace("\n", "\r\n")
        self.assertEqual(P.frontmatter(crlf)[0], want)
        self.assertEqual(crlf[P.frontmatter(crlf)[1]:].split("\r\n")[0], "описание")
        self.assertIsNone(P.frontmatter("просто текст\n---\nid: 1\n---\n"))

    def test_every_reader_gets_the_same_header(self):
        want = P.frontmatter(TEXT)[0]
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "X-1.md"
            f.write_text(TEXT, encoding="utf-8", newline="\n")
            self.assertEqual(T._parse_header(TEXT)[0], want)
            self.assertEqual(E.header(f), want)
            self.assertEqual(R.goal_header(f), want)
            self.assertEqual(O.split_ticket(TEXT)[0], want)
            self.assertEqual(HA.agent_meta(f)[0], want)
            self.assertEqual(T._parse_header(TEXT)[1], P.frontmatter(TEXT)[1])

    def test_no_header_is_what_each_reader_always_said(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "plain.md"
            f.write_text("без шапки\n", encoding="utf-8")
            self.assertEqual((E.header(f), R.goal_header(f), O.split_ticket("без шапки\n")[0]), ({}, {}, {}))
            with self.assertRaises(ValueError):
                T._parse_header("без шапки\n")
            with self.assertRaises(ValueError):
                HA.agent_meta(f)


class SetHeaderLockTests(unittest.TestCase):
    def test_set_header_waits_for_the_record_lock(self):
        """`_set_header` идёт под замком записи: пока другой поток держит замок, правка не проходит."""
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "E-1.md"
            f.write_text("---\nid: E-1\nstatus: open\n---\nтело\n", encoding="utf-8")
            held, release, done = threading.Event(), threading.Event(), threading.Event()

            def holder():
                with T.ticket_lock(f):
                    held.set()
                    release.wait(10)

            def writer():
                E._set_header(f, {"status": "fixed"})
                done.set()

            h = threading.Thread(target=holder)
            h.start()
            self.assertTrue(held.wait(10))
            w = threading.Thread(target=writer)
            w.start()
            self.assertFalse(done.wait(0.5), "правка прошла мимо замка")
            self.assertEqual(E.header(f)["status"], "open")
            release.set()
            h.join(10)
            w.join(10)
            self.assertTrue(done.is_set())
            self.assertEqual(E.header(f)["status"], "fixed")
            self.assertTrue(f.read_text(encoding="utf-8").endswith("---\nтело\n"))


if __name__ == "__main__":
    unittest.main()
