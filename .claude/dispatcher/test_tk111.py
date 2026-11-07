"""TK-111 (В-55): договор Исследователя — сверка с материалом владельца, допущения списком, достижимость плюса, вывод по проверенному.
Правила лежат в шаблонах устава (`templates/roles`); проектам доезжают через `charter sync` / update.py."""
from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def charter(name: str) -> str:
    return " ".join((ROOT / "templates" / "roles" / name).read_text(encoding="utf-8").split())


class ResearcherContractTests(unittest.TestCase):
    def test_material_checked_line_by_line_with_unused_list(self):
        text = charter("researcher.md")
        self.assertIn("каждое утверждение протокола сверено со строкой или кадром источника", text)
        self.assertIn("список неиспользованного материала", text)

    def test_assumptions_listed_for_owner_before_count(self):
        text = charter("researcher.md")
        self.assertIn("пороги времени, способ входа, стоп, издержки", text)
        self.assertIn("отдельным списком владельцу на утверждение через `CEO` до счёта", text)

    def test_reachability_and_data_checked_before_count(self):
        text = charter("researcher.md")
        self.assertIn("положительный исход достижим", text)
        self.assertIn("история стакана", text)
        self.assertIn("до протокола", text)

    def test_conclusion_only_about_checked_question(self):
        self.assertIn("только про проверенный вопрос", charter("researcher.md"))

    def test_owner_quote_rule_not_duplicated(self):
        self.assertEqual(charter("researcher.md").count("только цитата владельца"), 1)

    def test_judge_checks_researcher_contract(self):
        text = charter("judge.md")
        self.assertIn("Договор Исследователя", text)
        self.assertIn("уставу Исследователя", text)
        self.assertEqual(text.count("Договор Исследователя"), 1)


if __name__ == "__main__":
    unittest.main()
