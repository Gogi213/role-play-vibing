"""TK-082 (В-32): «до трёх строк» в уставе CEO — для сообщений по своей инициативе; ответ на прямой вопрос владельца — без лимита строк."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_tk065 import CEO_TEMPLATE, rule  # noqa: E402


class DirectAnswerRuleTests(unittest.TestCase):
    def setUp(self):
        self.text = CEO_TEMPLATE.read_text(encoding="utf-8")
        self.short = " ".join(rule(self.text, "С владельцем").split())
        self.direct = " ".join(rule(self.text, "Ответ на прямой вопрос владельца").split())

    def test_three_lines_only_for_own_initiative(self):
        self.assertIn("в сообщении по своей инициативе (статус, сигнал, «готово — выпустить?»)", self.short)
        self.assertIn("до трёх строк", self.short)

    def test_direct_answer_has_no_line_limit(self):
        self.assertIn("без лимита строк", self.direct)
        self.assertIn("«до трёх строк» его не держит", self.direct)

    def test_both_quality_rules_present(self):
        self.assertIn("вывод первым, без воды", self.direct)
        self.assertIn("каждый термин выброшен или объяснён рядом полфразой", self.direct)

    def test_details_only_on_question_and_ticket_number_kept(self):
        self.assertIn("Детали — только по вопросу владельца", self.short)
        self.assertIn("Номер тикета `TK-NNN` оставляй", self.short)
        self.assertIn("Номер тикета `TK-NNN` оставляй", self.direct)

    def test_consistent_with_neighbouring_rules(self):
        self.assertIn("«Стою: жду вашего …» остаётся первой строкой", self.direct)
        self.assertIn("«Критик: проверил …»", self.direct)
        self.assertIn("«Числа владельцу — после Судьи»", self.direct)
        self.assertIn("Критик: проверил … — изменилось …", rule(self.text, "Рекомендация или починка владельцу"))
        self.assertTrue(any(ln.startswith("- **Числа владельцу — после Судьи**") for ln in self.text.splitlines()))

    def test_one_rule_per_head(self):
        for head in ("С владельцем", "Ответ на прямой вопрос владельца"):
            self.assertEqual(sum(ln.startswith(f"- **{head}") for ln in self.text.splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
