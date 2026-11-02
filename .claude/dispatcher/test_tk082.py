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

    def test_short_rule_for_own_messages(self):
        self.assertIn("до трёх строк", self.short)
        self.assertIn("Детали — только по вопросу владельца", self.short)
        self.assertIn("`TK-NNN` оставляй", self.short)

    def test_direct_answer_has_no_line_limit(self):
        self.assertIn("без лимита строк", self.direct)
        self.assertIn("вывод первым, без воды", self.direct)
        self.assertIn("термин объяснён рядом полфразой", self.direct)
        self.assertIn("`TK-NNN` оставляй", self.direct)
        self.assertIn("«Стою: жду вашего …»", self.direct)

    def test_no_critic_line_in_answer(self):
        self.assertNotIn("Критик: проверил", self.short + self.direct)

    def test_one_rule_per_head(self):
        for head in ("С владельцем", "Ответ на прямой вопрос владельца"):
            self.assertEqual(sum(ln.startswith(f"- **{head}") for ln in self.text.splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
