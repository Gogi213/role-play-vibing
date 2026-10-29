"""TK-053 (В-17): помощник `critic` (agents/critic.md) и правило устава CEO «рекомендация владельцу — только после критика»;
`charter sync` переносит правило из шаблона в устав проекта."""
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import charter  # noqa: E402
import haiku_aux as HA  # noqa: E402

PLUGIN = Path(__file__).resolve().parents[2]
CRITIC = PLUGIN / "agents" / "critic.md"
CEO_TEMPLATE = PLUGIN / "templates" / "roles" / "ceo.md"
READ_ONLY = {"Read", "Grep", "Glob", "Bash"}
WRITERS = {"Write", "Edit", "NotebookEdit", "MultiEdit", "Agent", "Task"}
NOW = datetime(2026, 10, 30, 2, 0, tzinfo=timezone.utc)


class CriticAgentTests(unittest.TestCase):
    def setUp(self):
        self.meta, self.body = HA.agent_meta(CRITIC)

    def test_agent_tools_are_read_only(self):
        tools = {t.strip() for t in self.meta["tools"].split(",") if t.strip()}
        self.assertEqual(tools, READ_ONLY)
        self.assertFalse(tools & WRITERS)

    def test_agent_model_is_sonnet(self):
        self.assertEqual(self.meta["model"], "claude-sonnet-5-5")

    def test_agent_name_and_description(self):
        self.assertEqual(self.meta["name"], "critic")
        self.assertTrue(self.meta["description"].strip())
        self.assertEqual(HA.agent_def(CRITIC, with_tools=True)["model"], "claude-sonnet-5-5")

    def test_agent_body_checks_raw_data_and_answers_per_item(self):
        for must in ("сырьём", "держится", "не держится", "не хватает", "файл:строка", "Исправленная формулировка",
                     "Bash` — только чтение", "Изменилось: да | нет"):
            self.assertIn(must, self.body)

    def test_agent_does_not_invent_tasks(self):
        self.assertIn("Новых задач", self.body)
        self.assertIn("вне предложения", self.body)


class CeoTemplateTests(unittest.TestCase):
    def setUp(self):
        self.text = CEO_TEMPLATE.read_text(encoding="utf-8")
        self.rule = next(ln for ln in self.text.splitlines() if "только после критика" in ln)

    def test_template_requires_critic_before_recommendation(self):
        self.assertIn("`critic`", self.rule)
        self.assertIn("Рекомендация или починка владельцу", self.rule)
        self.assertIn("дословно", self.rule)
        self.assertIn("сырьё", self.rule)

    def test_template_has_the_one_line_for_the_owner(self):
        self.assertIn("Критик: проверил … — изменилось …", self.rule)
        self.assertIn("не изменилось", self.rule)


class CharterSyncCarriesRuleTests(unittest.TestCase):
    def test_sync_copies_the_rule_into_the_project_charter(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            roles = root / ".claude" / "roles"
            roles.mkdir(parents=True)
            (roles / "ceo.md").write_text("# CEO\nстарый устав без правила\n", encoding="utf-8")
            entry, _, err = charter.sync(root, ["ceo.md"], now=NOW)
            self.assertIsNone(err)
            self.assertEqual(entry["files"], {"ceo.md": "updated"})
            synced = (roles / "ceo.md").read_text(encoding="utf-8")
            self.assertEqual(synced, CEO_TEMPLATE.read_text(encoding="utf-8"))
            self.assertIn("только после критика", synced)
            self.assertIn("Критик: проверил", synced)


if __name__ == "__main__":
    unittest.main()
