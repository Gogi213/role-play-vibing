"""TK-080 (В-32, PR #189 п.9): «полный прогон подтверждает CI» — условно. Ключ `has_ci` в `.claude/rpv.json` (умолчание true —
прежнее поведение); в уставах условная формулировка; при `has_ci: false` хук запуска кладёт ролям строку «CI на PR нет»."""
from __future__ import annotations

import importlib.util
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rpv_config  # noqa: E402

PLUGIN = Path(__file__).resolve().parents[2]
_stub = Path(tempfile.mkdtemp(prefix="tk080-roles-"))  # без `.claude/roles` в проекте хук при импорте выходит
(_stub / ".claude" / "roles").mkdir(parents=True)
_spec = importlib.util.spec_from_file_location("role_context_tk080", PLUGIN / ".claude" / "hooks" / "role_context.py")
rc = importlib.util.module_from_spec(_spec)  # под своим именем: общий `role_context` держит корень других тестов (test_role_hooks)
with mock.patch.dict("os.environ", {"CLAUDE_PROJECT_DIR": str(_stub)}):
    _spec.loader.exec_module(rc)

CHARTERS = ("README.md", "engineer.md", "researcher.md")


def project_with(rpv_text):
    """Каталог-проект; `rpv_text` None — без `.claude/rpv.json`."""
    d = tempfile.mkdtemp(prefix="tk080-")
    if rpv_text is not None:
        (Path(d) / ".claude").mkdir()
        (Path(d) / ".claude" / "rpv.json").write_text(rpv_text, encoding="utf-8")
    return d


class HasCiKey(unittest.TestCase):
    def test_key_takes_only_bool(self):
        check = rpv_config.KEYS["has_ci"][0]
        self.assertTrue(check(True) and check(False))
        for bad in ("false", 0, 1, None, [], "нет"):
            self.assertFalse(check(bad), bad)


class NoCiLine(unittest.TestCase):
    def line(self, role, rpv_text):
        with mock.patch.object(rc, "ROOT", project_with(rpv_text)):
            return rc.no_ci_line(role)

    def test_false_gives_line_to_every_role_but_ceo(self):
        for role in ("engineer", "researcher", "judge"):
            self.assertIn("CI на PR нет", self.line(role, json.dumps({"has_ci": False})), role)
        self.assertEqual(self.line("ceo", json.dumps({"has_ci": False})), "")

    def test_default_keeps_old_behaviour(self):
        for rpv_text in (None, "{}", json.dumps({"has_ci": True}), json.dumps({"has_ci": ""}), "не json"):
            self.assertEqual(self.line("engineer", rpv_text), "", rpv_text)


class ChartersAreConditional(unittest.TestCase):
    def test_full_run_confirmed_by_ci_only_when_project_has_ci(self):
        for name in CHARTERS:
            text = re.sub(r"\s+", " ", (PLUGIN / "templates" / "roles" / name).read_text(encoding="utf-8"))
            self.assertIn("если у проекта есть CI на PR (`has_ci` в `.claude/rpv.json`, умолчание `true`)", text, name)
            self.assertIn("при `has_ci: false` полный прогон остаётся обязанностью роли и пишется в отчёт «Итог» (не в команду ярлыка)", text, name)
            self.assertNotIn("не пишется — его подтверждает зелёный CI PR", text, name)

    def test_engineer_pre_submit_full_run_names_the_no_ci_case(self):
        text = (PLUGIN / "templates" / "roles" / "engineer.md").read_text(encoding="utf-8")
        line = next(ln for ln in text.splitlines() if "перед сдачей — " in ln and "`errors.py run`" in ln)
        self.assertNotIn("перед сдачей — полный прогон", line)
        self.assertIn("перед сдачей — тесты самой правки и `errors.py run`", line)
        self.assertIn("полный прогон тестов и линтеров проекта — только при `has_ci: false`", line)
        self.assertIn("полный прогон не гонять — его делает зелёный CI", line)


if __name__ == "__main__":
    unittest.main()
