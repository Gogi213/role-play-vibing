"""TK-065 (В-26): короткий стиль ответов CEO владельцу — в шаблоне устава CEO, для всех проектов; `charter sync` его переносит."""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import charter  # noqa: E402

PLUGIN = Path(__file__).resolve().parents[2]
CEO_TEMPLATE = PLUGIN / "templates" / "roles" / "ceo.md"
NOW = datetime(2026, 10, 30, 18, 0, tzinfo=timezone.utc)


def rule(text: str, head: str) -> str:
    """Строка устава, начинающаяся с `- **head`, вместе с продолжением (отступ в 2 пробела)."""
    lines = text.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith(f"- **{head}"))
    end = start + 1
    while end < len(lines) and lines[end].startswith("  "):
        end += 1
    return "\n".join(lines[start:end])


class ShortStyleRuleTests(unittest.TestCase):
    def setUp(self):
        self.text = CEO_TEMPLATE.read_text(encoding="utf-8")
        self.rule = " ".join(rule(self.text, "С владельцем").split())

    def test_up_to_three_lines_change_plus_need(self):
        for must in ("до трёх строк", "что изменилось", "что нужно от владельца", "«Ничего не нужно»"):
            self.assertIn(must, self.rule)

    def test_ticket_number_is_kept(self):
        self.assertIn("Номер тикета `TK-NNN` оставляй", self.rule)

    def test_no_mechanics(self):
        for must in ("Механику не пиши", "PR", "файлы и строки", "наблюдение / `Monitor`",
                     "пересказ отчётов критика, Инженера, Судьи", "«жду»", "«наблюдение включено»"):
            self.assertIn(must, self.rule)

    def test_details_only_on_question_and_no_empty_messages(self):
        self.assertIn("Детали — только по вопросу владельца", self.rule)
        self.assertIn("Сообщение без события (ничего не изменилось, спросить нечего) не шли", self.rule)

    def test_before_after_example(self):
        self.assertIn("Было: абзац про PR, файлы и наблюдение; стало: «TK-064 готов. Выпустить новую версию?»", self.rule)

    def test_style_does_not_override_neighbouring_format_rules(self):
        # строки о формате, с которыми стиль согласован, на месте и названы в правиле
        self.assertIn("«Числа владельцу — после Судьи»", self.rule)
        self.assertTrue(any(ln.startswith("- **Числа владельцу — после Судьи**") for ln in self.text.splitlines()))
        self.assertIn("«Критик: проверил …»", self.rule)
        self.assertIn("Критик: проверил … — изменилось …", rule(self.text, "Рекомендация или починка владельцу"))
        self.assertIn("«Стою: жду вашего …»", self.rule)
        waiting = " ".join(rule(self.text, "Утверждённое не переспрашиваешь").split())
        self.assertIn("первой строкой ответа «Стою: жду вашего …»", waiting)
        self.assertIn("это и есть строка «что нужно от владельца»", waiting)

    def test_old_one_liner_is_replaced_not_duplicated(self):
        self.assertEqual(sum(ln.startswith("- **С владельцем") for ln in self.text.splitlines()), 1)
        self.assertNotIn("коротко, без техники", self.text)


class CharterSyncCarriesStyleTests(unittest.TestCase):
    def test_sync_copies_the_rule_into_the_project_charter(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            roles = root / ".claude" / "roles"
            roles.mkdir(parents=True)
            (roles / "ceo.md").write_text("# CEO\n- **С владельцем:** коротко, без техники.\n", encoding="utf-8")
            _, _, err = charter.sync(root, ["ceo.md"], now=NOW)
            self.assertIsNone(err)
            synced = (roles / "ceo.md").read_text(encoding="utf-8")
            self.assertEqual(synced, CEO_TEMPLATE.read_text(encoding="utf-8"))
            self.assertIn("**С владельцем — до трёх строк:**", synced)


if __name__ == "__main__":
    unittest.main()
