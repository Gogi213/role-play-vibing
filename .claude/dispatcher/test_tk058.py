"""TK-058 (В-22): помощник `worker-light` (agents/worker-light.md) и правило устава CEO «помощник общего назначения — только
worker-light»; `charter sync` переносит правило из шаблона в устав проекта. Обычного `worker` на Sonnet нет."""
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import charter  # noqa: E402
import haiku_aux as HA  # noqa: E402

PLUGIN = Path(__file__).resolve().parents[2]
WORKER = PLUGIN / "agents" / "worker-light.md"
CEO_TEMPLATE = PLUGIN / "templates" / "roles" / "ceo.md"
TOOLS = {"Read", "Grep", "Glob", "Bash", "Write"}
NOW = datetime(2026, 10, 30, 3, 40, tzinfo=timezone.utc)


class WorkerLightAgentTests(unittest.TestCase):
    def setUp(self):
        self.meta, self.body = HA.agent_meta(WORKER)

    def test_model_is_haiku_with_xhigh_effort(self):
        self.assertEqual(self.meta["model"], "claude-haiku-5-5")
        self.assertEqual(self.meta["effort"], "xhigh")

    def test_tools_are_exactly_the_five(self):
        tools = {t.strip() for t in self.meta["tools"].split(",") if t.strip()}
        self.assertEqual(tools, TOOLS)

    def test_name_description_and_definition(self):
        self.assertEqual(self.meta["name"], "worker-light")
        self.assertTrue(self.meta["description"].strip())
        d = HA.agent_def(WORKER, with_tools=True)
        self.assertEqual(d["model"], "claude-haiku-5-5")
        self.assertEqual(d["effort"], "xhigh")

    def test_body_states_the_limits(self):
        for must in ("готовый рецепт", "Выводов не делаешь", "scratchpad сессии", "не пишешь",
                     "на проверке", "тикет Инженеру/Исследователю"):
            self.assertIn(must.lower(), self.body.lower())

    def test_no_regular_worker_on_sonnet(self):
        self.assertFalse((PLUGIN / "agents" / "worker.md").exists())
        for p in (PLUGIN / "agents").glob("*.md"):
            meta, _ = HA.agent_meta(p)
            self.assertNotEqual(meta["name"], "worker")


class CeoTemplateTests(unittest.TestCase):
    def setUp(self):
        text = CEO_TEMPLATE.read_text(encoding="utf-8")
        self.rule = next(ln for ln in text.splitlines() if "Помощник общего назначения — только `worker-light`" in ln)

    def test_template_routes_routine_to_worker_light(self):
        self.assertIn("рутина без мысли", self.rule)
        self.assertIn("простой скрипт по готовому рецепту", self.rule)
        self.assertIn("«на проверке»", self.rule)

    def test_template_routes_thinking_to_a_ticket(self):
        self.assertIn("несложный разбор и подсчёт по xlsx, JSON, БД", self.rule)
        self.assertIn("расчётный скрипт — Исследователю", self.rule)
        self.assertIn("`tickets.py new --effort medium` — план готов", self.rule)
        self.assertIn("`--effort high` — нужен протокол или отладка", self.rule)
        self.assertIn("правка кода продукта — Инженеру", self.rule)

    def test_template_forbids_builtin_general_purpose_and_fork(self):
        self.assertIn("не `fork`", self.rule)
        self.assertIn("`general-purpose`", self.rule)
        self.assertIn("не зовёшь", self.rule)


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
            self.assertIn("Помощник общего назначения — только `worker-light`", synced)


if __name__ == "__main__":
    unittest.main()
