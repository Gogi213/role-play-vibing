"""E-007 (TK-042): штатная строка сдачи «Осталось: приёмка.» считалась хвостом — `accept --tail -` отказывал (TK-040).
Гасится только законченный оборот про приёмку; «осталось: приёмка и …» и прочее «осталось» — по-прежнему хвост.
Падает на 11c3ccb, проходит после."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_tail_not_left import tail_hit  # noqa: E402


class TailAcceptLineTests(unittest.TestCase):
    def test_acceptance_left_is_not_a_tail(self):
        for text in ("Итог 3084ccd: страж пуша; 1379 passed; Алгоритм: git rev-list. Осталось: приёмка.",
                     "[итог: pr] PR #151@3084ccd — всё сделано. Осталось: приёмка", "Осталось: приёмка Судьи.",
                     "Осталось только приёмка.", "осталось — приёмка"):
            with self.subTest(text=text):
                self.assertIsNone(tail_hit(text))

    def test_acceptance_with_more_is_still_a_tail(self):
        for text in ("Осталось: приёмка и экспорт", "Осталось: приёмка Судьи и экспорт отчёта", "Осталось: приёмка, затем экспорт",
                     "Осталось: экспорт", "Осталось: приёмка. Не сделано: экспорт"):
            with self.subTest(text=text):
                self.assertIsNotNone(tail_hit(text))


if __name__ == "__main__":
    unittest.main()
