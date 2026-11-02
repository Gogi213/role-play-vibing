"""TK-088 (В-34, В-26): CEO пишет владельцу по своей инициативе только по четырём событиям и всегда со строкой «Что от вас: …»."""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import charter  # noqa: E402
from test_tk065 import CEO_TEMPLATE, PLUGIN, rule  # noqa: E402

HEAD = "Владельцу по своей инициативе"
NOW = datetime(2026, 11, 2, 18, 0, tzinfo=timezone.utc)


class FourEventsRuleTests(unittest.TestCase):
    def setUp(self):
        self.text = CEO_TEMPLATE.read_text(encoding="utf-8")
        self.rule = " ".join(rule(self.text, HEAD).split())

    def test_four_events(self):
        for must in ("(1) нужно его решение", "(2) сломалось или сорван срок",
                     "(3) шаг цели принят, следующий не определён", "(4) цель закрыта"):
            self.assertIn(must, self.rule)

    def test_what_is_not_written(self):
        for must in ("статус тикета", "«done»", "промежуточные проценты", "не пиши"):
            self.assertIn(must, self.rule)

    def test_what_from_you_line(self):
        for must in ("«Что от вас: …»", "«Что от вас: ничего, к сведению»"):
            self.assertIn(must, self.rule)
        self.assertIn("«Стою: жду вашего …» и есть эта строка", self.rule)

    def test_owner_question_goes_by_style_rules(self):
        self.assertIn("На вопрос владельца отвечай по В-26", self.rule)
        for head in ("С владельцем", "Ответ на прямой вопрос владельца"):
            self.assertIn(f"«{head.split(' — ')[0]}»", self.rule)
            self.assertEqual(sum(ln.startswith(f"- **{head}") for ln in self.text.splitlines()), 1)

    def test_one_rule_and_no_regular_status_lines(self):
        self.assertEqual(sum(ln.startswith(f"- **{HEAD}") for ln in self.text.splitlines()), 1)
        short = " ".join(rule(self.text, "С владельцем").split())
        self.assertIn("«Что от вас: …»", short)
        self.assertNotIn("свой статус", short)
        for old in ("Ничего не нужно", "раз в час отчёт", "регулярный статус"):
            self.assertNotIn(old, self.text)


class SignalsDocExampleTests(unittest.TestCase):
    def setUp(self):
        self.doc = (PLUGIN / "docs" / "signals.md").read_text(encoding="utf-8")
        self.section = self.doc.split("## Что CEO пишет владельцу", 1)[1].split("\n## ", 1)[0]

    def test_section_names_four_events_and_examples(self):
        for must in ("(1) нужно решение владельца", "(2) сломалось или сорван срок",
                     "(3) шаг цели принят, следующий не определён", "(4) цель закрыта"):
            self.assertIn(must, self.section)
        self.assertIn("Что от вас: назвать следующий шаг.", self.section)
        self.assertIn("Что от вас: ничего, к сведению.", self.section)

    def test_done_still_wakes_ceo_but_not_owner(self):
        self.assertIn("`done`, статусы тикетов и проценты владельцу не уходят", self.section)


class CharterSyncCarriesRuleTests(unittest.TestCase):
    def test_sync_copies_the_rule_into_the_project_charter(self):
        with tempfile.TemporaryDirectory() as tmp:
            roles = Path(tmp) / ".claude" / "roles"
            roles.mkdir(parents=True)
            (roles / "ceo.md").write_text("# CEO\n- **С владельцем:** коротко, без техники.\n", encoding="utf-8")
            _, _, err = charter.sync(Path(tmp), ["ceo.md"], now=NOW)
            self.assertIsNone(err)
            synced = (roles / "ceo.md").read_text(encoding="utf-8")
            self.assertEqual(synced, CEO_TEMPLATE.read_text(encoding="utf-8"))
            self.assertIn(f"**{HEAD} — только четыре события:**", synced)


if __name__ == "__main__":
    unittest.main()
