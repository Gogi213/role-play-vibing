"""Слово владельца 14.10: «нельзя притворяться CEO». Запуск роли не стирает и не меняет свою роль (страж), а стёртая —
находится по дереву процессов (`project.caller_role`): команды CEO (реестр решений, согласие на публикацию, исправление
ошибки) запуску роли закрыты и тогда."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
GUARD = HERE.parent / "hooks" / "delete_guard.py"
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


class GuardIdentityTests(unittest.TestCase):
    """Страж — отдельным процессом, как настоящий хук PreToolUse (его настройки не смешиваются с другими тестами)."""

    DENY = ['RPV_ROLE= python errors.py fix E-1', 'export RPV_ROLE=', 'unset RPV_ROLE; python x.py',
            'env -u RPV_ROLE python e.py', 'env -i python e.py', 'env - python e.py', '$env:RPV_ROLE = ""',
            'Remove-Item Env:RPV_ROLE', '[Environment]::SetEnvironmentVariable("RPV_ROLE", $null)',
            "python -c \"import os; os.environ.pop('RPV_ROLE')\"", 'cd x && RPV_TICKET=TK-9 python t.py',
            'declare -x RPV_ROLE=ceo', "python -c \"import os; del os.environ['RPV_TICKET']\"",
            'RPV_ROLE=judge python tickets.py result TK-1 accept', 'CLAUDE_PROJECT_DIR=/tmp/x python registry.py decide',
            'RPV_PROJECT=/tmp/x python tickets.py allow TK-1 publish']
    ALLOW = ['echo $RPV_ROLE', 'printenv RPV_ROLE', 'Write-Output $env:RPV_ROLE', 'Get-Item Env:RPV_ROLE',
             'python tickets.py result TK-1 done --path a', 'env | grep RPV']

    def setUp(self):
        self.proj = Path(tempfile.mkdtemp(prefix="rpv-ident-g-"))
        self.addCleanup(shutil.rmtree, self.proj, True)
        (self.proj / ".claude" / "roles").mkdir(parents=True)

    def verdicts(self, cmds, role):
        """Команды → причины отказа стража ('' — пропущена) одним процессом на команду."""
        env = {k: v for k, v in os.environ.items() if not k.startswith("RPV_")}
        env.update(CLAUDE_PROJECT_DIR=str(self.proj), PYTHONIOENCODING="utf-8", **({"RPV_ROLE": role, "RPV_TICKET": "TK-1"} if role else {}))
        out = []
        for cmd in cmds:
            ev = {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(self.proj)}
            r = subprocess.run([sys.executable, str(GUARD)], input=json.dumps(ev), capture_output=True, text=True,
                               encoding="utf-8", env=env, timeout=60)
            out.append(json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"] if r.stdout.strip() else "")
        return out

    def test_role_run_cannot_change_its_identity(self):
        for cmd, why in zip(self.DENY, self.verdicts(self.DENY, "engineer")):
            self.assertIn("притвориться CEO", why, cmd)
        for cmd, why in zip(self.ALLOW, self.verdicts(self.ALLOW, "engineer")):
            self.assertNotIn("притвориться CEO", why, cmd)

    def test_role_cannot_rewrite_dispatcher_record(self):
        """Права сверяются с `state.json` диспетчера — запуску роли он закрыт; чтение — можно."""
        deny, read = self.verdicts(["echo {} > .claude/dispatcher/state.json", "cat .claude/dispatcher/state.json"], "engineer")
        self.assertIn("state.json", deny)
        self.assertEqual(read, "")

    def test_ceo_session_is_not_limited(self):
        self.assertNotIn("притвориться CEO", self.verdicts(["RPV_ROLE= python x.py"], None)[0])


if __name__ == "__main__":
    unittest.main()
