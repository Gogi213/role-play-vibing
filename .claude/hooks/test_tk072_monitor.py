"""TK-072 (К1): `Monitor` запускает команду оболочки — страж проверяет её, как Bash. Запуск: `python -m pytest .claude/hooks/test_tk072_monitor.py`."""
import json
import os
import sys
import unittest
from pathlib import Path

# корень — окружением ДО импорта, как в test_push_scope: модули стража общие на весь прогон; чужое значение возвращаем
_PREV = os.environ.get("CLAUDE_PROJECT_DIR")
os.environ["CLAUDE_PROJECT_DIR"] = r"C:\visual projects\proj"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import delete_guard as dg  # noqa: E402
from dguard.tools import GUARDED_TOOLS, SHELL_TOOLS  # noqa: E402
if _PREV is None:
    os.environ.pop("CLAUDE_PROJECT_DIR", None)
else:
    os.environ["CLAUDE_PROJECT_DIR"] = _PREV

CWD = r"C:\visual projects\proj"
HOOKS_JSON = Path(__file__).resolve().parents[2] / "hooks" / "hooks.json"
WAKE = "/c/proj/.claude/dispatcher/ceo-wake.log"


def verdict(tool, command):
    return dg.check_tool({"tool_name": tool, "tool_input": {"command": command, "description": "x", "timeout_ms": 1800000}, "cwd": CWD})


class MonitorGuardTests(unittest.TestCase):
    def test_monitor_is_a_guarded_shell_tool(self):
        self.assertIn("Monitor", SHELL_TOOLS)
        self.assertIn("Monitor", GUARDED_TOOLS)

    def test_hook_matcher_covers_monitor(self):
        matchers = [e["matcher"] for e in json.loads(HOOKS_JSON.read_text(encoding="utf-8"))["hooks"]["PreToolUse"]]
        self.assertTrue(any("Monitor" in m.split("|") for m in matchers), matchers)

    def test_denies_what_bash_denies(self):
        for command in ("git push --force origin main", "git push origin --delete feature", "git push origin :feature",
                        f"echo x >> {WAKE}", f"tee {WAKE}"):
            with self.subTest(command=command):
                self.assertIsNotNone(verdict("Bash", command), command)
                self.assertIsNotNone(verdict("Monitor", command), command)

    def test_allowed_watch_command_passes(self):
        self.assertIsNone(verdict("Monitor", f"tail -F {WAKE}"))
        self.assertIsNone(verdict("Monitor", f"tail -n 0 -F {WAKE}"))

    def test_only_the_command_key_is_read(self):
        # опасный текст в описании при безобидной команде — не отказ; пустая команда безопасна
        ev = {"tool_name": "Monitor", "tool_input": {"command": f"tail -F {WAKE}", "description": "git push --force"}, "cwd": CWD}
        self.assertIsNone(dg.check_tool(ev))
        self.assertIsNone(verdict("Monitor", ""))


if __name__ == "__main__":
    unittest.main()
