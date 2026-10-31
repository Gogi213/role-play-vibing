"""E-001 (TK-001): `--tail -` пропускал «Не осталось времени доделать экспорт» — отрицание хвоста было шире заказа
(«ничего не осталось»). Падает на 012fbf3, проходит после."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tickets as TK  # noqa: E402


def tail_hit(text: str):
    """Как в `tickets._tail_ack`: снять отрицания, искать слова хвоста."""
    return TK.TAIL_WORDS.search(TK.TAIL_NEGATION.sub(lambda m: " " * len(m.group()), text))


class TailNotLeftTests(unittest.TestCase):
    def test_not_left_is_still_a_tail(self):
        for text in ("Не осталось времени доделать экспорт", "Сделано. Не осталось сил на тесты",
                     "Ничего не осталось доделать, кроме экспорта"):
            with self.subTest(text=text):
                self.assertIsNotNone(tail_hit(text))

    def test_nothing_left_is_not_a_tail(self):
        for text in ("Сделано, ничего не осталось", "Сделано, ничего не осталось.", "Ничего не осталось, всё сдано",
                     "Хвостов нет", "хвоста нет, всё сделано", "Сделано без хвостов.", "Сделано, осталось 0",
                     "Осталось: приёмка.\nПосле слияния: тег в следующем выпуске"):  # E-018
            with self.subTest(text=text):
                self.assertIsNone(tail_hit(text))


if __name__ == "__main__":
    unittest.main()
