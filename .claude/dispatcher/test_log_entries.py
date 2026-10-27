"""TK-010: записи лога тикета делит на части один разбор (ticket.parse_entries); Надзиратель передаёт ему свой, более строгий заголовок."""
import os
import sys
import tempfile
import time
import unittest
from unittest import mock
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dispatch as D  # noqa: E402
import overseer as O  # noqa: E402
import project as P  # noqa: E402
import start as S  # noqa: E402
import ticket as T  # noqa: E402

LOG = (
    "до первого заголовка\n\n"
    "### 2026-10-27T07:00:00+04:00 engineer\nпервая\nстрока\n\n"
    "### Заметка\nне запись\n\n"
    "### 2026-10-27T07:05:00 engineer (запуск 2)\nавтор из нескольких слов\n\n"
    "### 2026-13-45T07:10:00 judge\nвремени нет в календаре\n\n"
    "### 2026-10-27T07:20:00Z judge\n[итог: return] вернул\n"
)
TICKET = "---\nid: TK-1\ntitle: t\nstatus: done\n---\nописание\n\n## Лог\n\n" + LOG


class ParseEntriesTests(unittest.TestCase):
    def test_ticket_reader_takes_every_header_with_a_valid_time(self):
        got = T.parse_entries(LOG)
        self.assertEqual([e.author for e in got], ["engineer", "engineer (запуск 2)", "judge"])
        self.assertEqual(got[0].ts_raw, "2026-10-27T07:00:00+04:00")
        self.assertEqual(got[0].text, "первая\nстрока")  # заголовок с невозможным временем всё же обрывает запись
        self.assertEqual(got[1].text, "автор из нескольких слов")

    def test_overseer_reader_is_stricter_through_its_own_header(self):
        got = O.parse_log(LOG)
        self.assertEqual([e.role for e in got], ["engineer", "judge"])
        self.assertIn("### Заметка", got[0].text)
        self.assertIn("engineer (запуск 2)", got[0].text)  # автор из нескольких слов — не граница записей
        self.assertTrue(O.is_judge_return(got[1]))

    def test_overseer_naive_time_is_owner_time_ticket_naive_time_is_utc(self):
        text = "### 2026-10-27T07:00:00 engineer\nx\n"
        self.assertEqual(O.parse_log(text)[0].ts.utcoffset(), timedelta(hours=4))
        self.assertEqual(T.parse_entries(text)[0].ts.utcoffset(), timedelta(0))

    def test_log_without_heading_or_headers_is_empty(self):
        self.assertEqual(T.parse_text("---\nid: TK-1\n---\nописание\n").log, [])
        self.assertEqual(O.parse_log("просто текст"), [])

    def test_ticket_file_log_is_the_same_in_both_readers(self):
        mine = T.parse_text(TICKET).log
        head, desc, log = O.split_ticket(TICKET)
        self.assertEqual((head["status"], desc), ("done", "описание"))
        theirs = O.parse_log(log)
        self.assertEqual(len(mine), 3)
        self.assertEqual((theirs[-1].ts, theirs[-1].text), (mine[-1].ts, mine[-1].text.strip()))


class LockFileTests(unittest.TestCase):
    """Замки и pid-файлы: один разбор pid и один замок-файл O_EXCL для мьютекса диспетчера и замка перезапуска."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def test_read_pid_missing_empty_garbage_number(self):
        f = self.dir / "x.pid"
        self.assertEqual(P.read_pid(f), 0)
        for text, want in (("", 0), ("  \n", 0), ("abc", 0), (" 4242\n", 4242)):
            f.write_text(text, encoding="utf-8")
            self.assertEqual(P.read_pid(f), want, text)
        self.assertIs(S.read_pid, P.read_pid)

    def test_take_lock_file_free_held_and_stale(self):
        f = self.dir / "a.lock"
        self.assertTrue(P.take_lock_file(f, wait_s=1, stale_s=30, poll_s=0.01))
        self.assertFalse(P.take_lock_file(f, wait_s=0.1, stale_s=30, poll_s=0.01))   # свежий — не отдают
        old = time.time() - 100
        os.utime(f, (old, old))
        self.assertTrue(P.take_lock_file(f, wait_s=1, stale_s=30, poll_s=0.01))     # бесхозный — снят и взят
        P.drop_lock_file(f)
        self.assertFalse(f.exists())
        P.drop_lock_file(f)   # второй раз — не падает

    def test_both_lock_users_go_through_the_shared_function(self):
        with mock.patch.object(P, "take_lock_file", wraps=P.take_lock_file) as take:
            with S.restart_lock(self.dir):
                self.assertTrue((self.dir / "restart.lock").exists())
            self.assertFalse((self.dir / "restart.lock").exists())
            ok, _ = D.acquire_instance_lock(self.dir / "d.pid")
            self.assertTrue(ok)
            self.assertEqual(P.read_pid(self.dir / "d.pid"), os.getpid())
            D.release_instance_lock(self.dir / "d.pid")
            self.assertFalse((self.dir / "d.pid").exists())
        self.assertEqual([c.args[0].name for c in take.call_args_list], ["restart.lock", "d.pid.mx"])


if __name__ == "__main__":
    unittest.main()
