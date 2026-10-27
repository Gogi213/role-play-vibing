"""TK-010: записи лога тикета делит на части один разбор (ticket.parse_entries); Надзиратель передаёт ему свой, более строгий заголовок."""
import sys
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import overseer as O  # noqa: E402
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


if __name__ == "__main__":
    unittest.main()
