"""TK-072 (К1): `Monitor` запускает команду оболочки — страж проверяет её, как Bash; TK-075: источник `ws` (без `command`) — хост по
`guard_forbidden_hosts`. Запуск: `python -m pytest .claude/hooks/test_tk072_monitor.py`."""
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
from dguard import config as cfg  # noqa: E402
from dguard.tools import GUARDED_TOOLS, SHELL_TOOLS  # noqa: E402
if _PREV is None:
    os.environ.pop("CLAUDE_PROJECT_DIR", None)
else:
    os.environ["CLAUDE_PROJECT_DIR"] = _PREV

CWD = r"C:\visual projects\proj"
HOOKS_JSON = Path(__file__).resolve().parents[2] / "hooks" / "hooks.json"
WAKE = "/c/proj/.claude/dispatcher/ceo-wake.log"


def verdict(tool, command):
    return dg.check_tool({"tool_name": tool, "tool_input": {"command": command, "description": "x", "timeout_ms": 600000}, "cwd": CWD})


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


class MonitorWsGuardTests(unittest.TestCase):
    """TK-075: `Monitor` с источником `ws` вместо `command` — закрытый хост в адресе отказ; прочий адрес поток только читает."""
    CLOSED = "203.0.113.1"

    def setUp(self):
        old = cfg.CLOSED_RE
        cfg.CLOSED_RE = cfg.machine_rules({"guard_forbidden_hosts": [self.CLOSED]})[2]
        self.addCleanup(setattr, cfg, "CLOSED_RE", old)

    @staticmethod
    def ws(url="wss://events.example.com/stream", **extra):
        ti = {"ws": {"url": url, "protocols": ["v1"]}, "description": "x", "timeout_ms": 600000}
        return dg.check_tool({"tool_name": "Monitor", "tool_input": {**ti, **extra}, "cwd": CWD})

    def test_closed_host_is_denied(self):
        for url in (f"wss://{self.CLOSED}/stream", f"ws://user@{self.CLOSED}:8080/x?y=1"):
            with self.subTest(url=url):
                self.assertIn("ws", self.ws(url))

    def test_other_host_passes(self):
        self.assertIsNone(self.ws())

    def test_unreadable_source_is_denied(self):
        for ws in ({}, {"url": ""}, {"url": 5}, "wss://events.example.com", ["wss://events.example.com"], ""):
            with self.subTest(ws=ws):
                ev = {"tool_name": "Monitor", "tool_input": {"ws": ws, "description": "x", "timeout_ms": 1000}, "cwd": CWD}
                self.assertIsNotNone(dg.check_tool(ev))

    def test_command_alongside_ws_is_still_checked(self):
        self.assertIsNotNone(self.ws(command="git push --force origin main"))

    def test_trace_names_the_ws_branch(self):
        trace = []
        dg.check_tool({"tool_name": "Monitor", "tool_input": {"ws": {"url": f"wss://{self.CLOSED}/s"}}, "cwd": CWD}, trace)
        self.assertEqual(trace, [("ws", "deny")])


if __name__ == "__main__":
    unittest.main()
