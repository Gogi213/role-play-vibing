"""TK-085 (В-33, В-35): побудка CEO событием — один headless-вызов `claude` на событие, ноль на обычный `done`; устав и команда `/ceo`
без `Monitor`. Запуск: `python -m pytest .claude/dispatcher/test_tk085.py`."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ceo_wake as CW  # noqa: E402
import project_paths as CFG  # noqa: E402
import signals as SIG  # noqa: E402
import ticket as T  # noqa: E402

PLUGIN = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 11, 2, 14, 0).astimezone()


class Stub:
    """Заглушка `claude`: считает вызовы; ответ — по умолчанию «сессия найдена»."""
    def __init__(self, out=b"ok", code=0):
        self.calls, self.out, self.code = [], out, code

    def __call__(self, cmd, **kw):
        self.calls.append((cmd, kw["input"].decode("utf-8")))
        return subprocess.CompletedProcess(cmd, self.code, self.out, b"")


class WakeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._old = CFG.PROJECT_ROOT
        CFG.configure_project(self.tmp.name)
        self.addCleanup(CFG.configure_project, self._old)
        (Path(self.tmp.name) / ".claude" / "dispatcher").mkdir(parents=True)
        CW.write_session("sess-1", "CEO главный")
        self.stub, self.sent = Stub(), []
        for patch in (mock.patch.object(CW.haiku_aux, "_claude", return_value="claude"),
                      mock.patch.object(CW.haiku_aux.hide, "run", self.stub),
                      mock.patch.object(CW.notify, "send", lambda text, **kw: self.sent.append(text) or True)):
            patch.start()
            self.addCleanup(patch.stop)

    def signal(self, kind, tid="TK-001", note="причина"):
        SIG.append_ceo_inbox(tid, kind, note, NOW)

    def test_one_headless_call_per_event(self):
        for n, kind in enumerate(("needs_owner", "blocked", "due", "goal-step", "goal-done"), 1):
            self.signal(kind)
            self.assertEqual(len(self.stub.calls), n, kind)

    def test_done_and_other_kinds_do_not_wake(self):
        for kind in ("done", "next-ceo", "wait-for", "хвост", "watch-blocked", "summary", "model"):
            self.signal(kind)
        self.assertEqual(self.stub.calls, [])

    def test_call_is_minimal_and_names_the_session(self):
        self.signal("needs_owner", note="выбрать вариант")
        cmd, prompt = self.stub.calls[0]
        self.assertEqual(cmd[1:3], ["-p", "--model"])
        self.assertIn("bypassPermissions", cmd)
        self.assertEqual(cmd[cmd.index("--allowedTools") + 1], "ListAgents,SendMessage")
        self.assertIn("sess-1", prompt)
        self.assertIn("[нужно решение владельца] TK-001 — выбрать вариант", prompt)

    def test_session_not_found_goes_to_inbox_and_telegram(self):
        self.stub.out = b"NOT_FOUND"
        self.signal("needs_owner")
        self.assertIn("[ceo-не-разбужен]", CFG.CEO_INBOX.read_text(encoding="utf-8"))
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(len(self.stub.calls), 1, "сообщение об отказе побудкой не является")

    def test_failures_do_not_raise(self):
        for stub in (Stub(code=1), mock.Mock(side_effect=subprocess.TimeoutExpired("claude", 90))):
            self.stub.out, self.stub.code = b"", 0
            with mock.patch.object(CW.haiku_aux.hide, "run", stub):
                self.signal("blocked")
        self.assertEqual(len(self.sent), 2)

    def test_no_session_file_is_a_logged_failure(self):
        CW.session_file().unlink()
        self.signal("needs_owner")
        self.assertEqual(self.stub.calls, [])
        self.assertIn("[ceo-не-разбужен]", (CFG.DISPATCHER_DIR / "mechanics.log").read_text(encoding="utf-8"))
        self.assertEqual(len(self.sent), 1)

    def test_switch_off(self):
        with mock.patch.object(CW.P, "setting", lambda k, d=None, root=None: False if k == "ceo_wake" else d):
            self.signal("needs_owner")
        self.assertEqual(self.stub.calls, [])

    def test_due_missed_is_one_event_per_due(self):
        tkt = mock.Mock(id="TK-009", status="in_progress", header={"due": (NOW - timedelta(minutes=5)).isoformat()})
        state: dict = {}
        SIG.notify_due_missed(tkt, state, NOW)
        SIG.notify_due_missed(tkt, state, NOW)
        self.assertEqual(len(self.stub.calls), 1)
        tkt.header = {"due": (NOW + timedelta(hours=1)).isoformat()}
        SIG.notify_due_missed(tkt, {}, NOW)
        tkt.status, tkt.header = "done", {"due": (NOW - timedelta(hours=1)).isoformat()}
        SIG.notify_due_missed(tkt, {}, NOW)
        self.assertEqual(len(self.stub.calls), 1, "срок не наступил или тикет закрыт — события нет")

    def test_set_session_cli(self):
        self.assertEqual(CW.main(["--project", self.tmp.name, "set", "--id", "abc", "--name", "CEO"]), 0)
        self.assertEqual(json.loads(CW.session_file().read_text(encoding="utf-8"))["id"], "abc")


class TextTests(unittest.TestCase):
    def test_charter_and_command_without_monitor(self):
        for rel in ("templates/roles/ceo.md", "commands/ceo.md"):
            text = (PLUGIN / rel).read_text(encoding="utf-8")
            self.assertNotIn("Monitor", text, rel)
            self.assertNotIn("будильник", text.lower().replace("таймеров и наблюдения нет", ""), rel)
        self.assertIn("ceo_wake.py", (PLUGIN / "commands/ceo.md").read_text(encoding="utf-8"))
        self.assertIn("**Побудка:**", (PLUGIN / "templates/roles/ceo.md").read_text(encoding="utf-8"))
        self.assertNotIn("Monitor", (PLUGIN / "docs/signals.md").read_text(encoding="utf-8").split("## Побудка CEO")[1].split("## Передача")[0])


if __name__ == "__main__":
    unittest.main()
