"""TK-097: заголовок записи «роль (уточнение)» («ceo (служебная)», «engineer (запуск 2)») читает Надзиратель как запись роли."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import overseer as O  # noqa: E402


class ParseLogLabelTests(unittest.TestCase):
    def test_header_with_label_is_an_entry_of_its_role(self):
        text = ("### 2026-11-02T10:00:00+04:00 engineer\nпервая\n"
                "### 2026-11-02T10:05:00+04:00 ceo (служебная)\nСрок владельцу: 18:00\n"
                "### 2026-11-02T10:10:00+04:00 engineer (запуск 2)\nвторая\n")
        self.assertEqual([(e.role, e.text) for e in O.parse_log(text)],
                         [("engineer", "первая"), ("ceo", "Срок владельцу: 18:00"), ("engineer", "вторая")])

    def test_header_with_extra_words_without_brackets_is_still_not_an_entry(self):
        text = "### 2026-11-02T10:00:00+04:00 engineer\nа\n### 2026-11-02T10:05:00+04:00 ceo служебная\nб\n"
        self.assertEqual([e.role for e in O.parse_log(text)], ["engineer"])


if __name__ == "__main__":
    unittest.main()
