"""Слово владельца 14.10: «нельзя притворяться CEO». Роль запуска стёртая или подделанная в окружении находится по дереву
процессов (`project.caller_role`): команды CEO (реестр решений, согласие на публикацию, исправление ошибки) запуску роли
закрыты и тогда. Ветку стража «identity» (запрет трогать RPV_ROLE в команде) снял вырез 2.0.18: давала ложные отказы."""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import project as P  # noqa: E402


class CallerRoleTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="rpv-ident-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        (self.root / ".claude" / "dispatcher").mkdir(parents=True)
        (self.root / ".claude" / "dispatcher" / "state.json").write_text(json.dumps(
            {"active_runs": {"TK-1": {"role": "engineer", "pid": 4242}, "TK-2": {"role": "judge", "pid": None}}}),
            encoding="utf-8")
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("RPV_ROLE", None)

    def test_env_role_outside_runs(self):
        os.environ["RPV_ROLE"] = "Judge"
        self.assertEqual(P.caller_role(self.root, ancestors=[99]), "judge")

    def test_forged_env_inside_role_run_is_the_real_role(self):
        """Инженер с `RPV_ROLE=judge` остаётся Инженером: права — по записи диспетчера, не по переменной."""
        os.environ["RPV_ROLE"] = "judge"
        self.assertEqual(P.caller_role(self.root, ancestors=[99, 4242]), "engineer")

    def test_wiped_env_inside_role_run_is_still_that_role(self):
        self.assertEqual(P.caller_role(self.root, ancestors=[99, 4242, 1]), "engineer")

    def test_ceo_outside_any_run(self):
        self.assertEqual(P.caller_role(self.root, ancestors=[99, 1]), "")
        (self.root / ".claude" / "dispatcher" / "state.json").write_text("{битый", encoding="utf-8")
        self.assertEqual(P.caller_role(self.root, ancestors=[4242]), "")

    def test_ancestors_walk(self):
        self.assertEqual(P.ancestor_pids(10, {10: 7, 7: 3, 3: 0}), [7, 3])
        self.assertEqual(P.ancestor_pids(10, {10: 7, 7: 10}), [7, 10], "цикл не зацикливает")
        self.assertIn(os.getppid(), P.ancestor_pids(), "настоящее дерево процессов читается")



if __name__ == "__main__":
    unittest.main()
