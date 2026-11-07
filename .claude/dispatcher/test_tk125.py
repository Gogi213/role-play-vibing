"""TK-125 (В-62): побудка CEO адресует сессию по метке из ListAgents — две одноимённые сессии не путаются.
Запуск: `python -m pytest .claude/dispatcher/test_tk125.py`."""
from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ceo_wake as CW  # noqa: E402
import project_paths as CFG  # noqa: E402
import signals as SIG  # noqa: E402

PLUGIN = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 11, 7, 14, 0).astimezone()
NAME = "RPV - CAO"


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._old = CFG.PROJECT_ROOT
        CFG.configure_project(self.tmp.name)
        self.addCleanup(CFG.configure_project, self._old)
        (Path(self.tmp.name) / ".claude" / "dispatcher").mkdir(parents=True)
        self.sent, self.calls, self.out = [], [], b"DELIVERED"
        for patch in (mock.patch.object(CW.haiku_aux, "_claude", return_value="claude"),
                      mock.patch.object(CW.haiku_aux.hide, "run", self.run_stub),
                      mock.patch.object(CW, "BACKGROUND", False),
                      mock.patch.object(CW.notify, "send", lambda text, **kw: self.sent.append(text) or True)):
            patch.start()
            self.addCleanup(patch.stop)

    def run_stub(self, cmd, **kw):
        self.calls.append(kw["input"].decode("utf-8"))
        return subprocess.CompletedProcess(cmd, 0, self.out, b"")

    def signal(self):
        SIG.append_ceo_inbox("TK-001", "needs_owner", "причина", NOW)

    def run_main(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = CW.main(["--project", self.tmp.name, *args])
        return code, out.getvalue(), err.getvalue()


class AddressTests(Sandbox):
    def test_ceo_wake_two_namesakes_address_carries_the_ref(self):
        """В ListAgents две «RPV - CAO»: [3fb94d] и [62ccf2]. CEO — вторая; адрес в промпте — с её меткой, не с чужой."""
        CW.write_session("sess-1", NAME, "62ccf2")
        self.signal()
        prompt = self.calls[0]
        self.assertIn(f'"{NAME} [62ccf2]"', prompt)
        self.assertIn("to — \"RPV - CAO [62ccf2]\"", prompt)
        self.assertNotIn("3fb94d", prompt)
        self.assertIn("другой меткой — не она", prompt)
        self.assertNotIn("to — имя сессии", prompt)
        self.assertEqual(self.sent, [])

    def test_ceo_wake_ref_not_in_list_is_not_found_not_a_namesake(self):
        """Метки в ListAgents нет → запуск отвечает NOT_FOUND → неуспех (строка в очереди, Telegram), а не доставка тёзке."""
        CW.write_session("sess-1", NAME, "dead00")
        self.out = b"NOT_FOUND"
        self.signal()
        self.assertIn("[ceo-не-разбужен]", CFG.CEO_INBOX.read_text(encoding="utf-8"))
        self.assertEqual(len(self.sent), 1)
        self.assertIn(CW.NOT_FOUND, self.calls[0])

    def test_ceo_wake_without_ref_the_prompt_is_by_name_as_before(self):
        CW.write_session("sess-1", NAME)
        self.signal()
        prompt = self.calls[0]
        self.assertIn(f'"{NAME}"', prompt)
        self.assertIn("to — имя сессии", prompt)
        self.assertNotIn("метк", prompt)

    def test_ref_without_name_addresses_the_bare_ref(self):
        CW.session_file().write_text(json.dumps({"id": "s", "name": "", "ref": "62ccf2"}), encoding="utf-8")
        self.signal()
        self.assertIn('to — "[62ccf2]"', self.calls[0])

    def test_ref_only_session_file_is_a_session(self):
        CW.session_file().write_text(json.dumps({"ref": "62ccf2"}), encoding="utf-8")
        self.assertEqual(CW.read_session()["ref"], "62ccf2")
        self.signal()
        self.assertEqual(len(self.calls), 1)

    def test_name_with_quotes_and_ref_stay_one_line(self):
        CW.write_session("s", 'CEO "главный"\nвторая', "62ccf2")
        self.signal()
        self.assertIn('"CEO \\"главный\\" вторая [62ccf2]"', self.calls[0])


class RefStorageTests(Sandbox):
    def test_ceo_wake_ref_is_stored_without_brackets_and_spaces(self):
        for raw in ("62ccf2", "[62ccf2]", "  62ccf2 ", " [ 62ccf2 ]\n"):
            CW.write_session("s", NAME, raw)
            self.assertEqual(CW.read_session()["ref"], "62ccf2", repr(raw))

    def test_brackets_inside_and_newline_collapse_to_one_line(self):
        CW.write_session("s", NAME, "ab]c\nd")
        self.assertEqual(CW.read_session()["ref"], "abcd")

    def test_ceo_wake_second_set_without_ref_clears_the_old_ref_and_keeps_the_name(self):
        """Метка принадлежит одной сессии: после /clear она другая. Старую подставлять нельзя — уйдёт мёртвому адресу."""
        self.run_main("set", "--id", "old", "--name", NAME, "--ref", "3fb94d")
        code, out, _err = self.run_main("set", "--id", "new")
        self.assertEqual(code, 0)
        session = json.loads(out)
        self.assertEqual((session["id"], session["name"], session["ref"]), ("new", NAME, ""))

    def test_set_with_ref_prints_it_and_no_ref_warning(self):
        code, out, err = self.run_main("set", "--id", "s", "--name", NAME, "--ref", "[62ccf2]")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["ref"], "62ccf2")
        self.assertEqual(err, "")

    def test_ceo_wake_set_without_ref_warns_about_the_ref_and_exits_zero(self):
        code, _out, err = self.run_main("set", "--id", "s", "--name", NAME)
        self.assertEqual(code, 0)
        self.assertIn("ПРЕДУПРЕЖДЕНИЕ", err)
        self.assertIn("метка сессии не записана", err)
        self.assertNotIn("имя сессии не записано", err)

    def test_set_without_name_and_ref_warns_about_both(self):
        _code, _out, err = self.run_main("set", "--id", "s")
        self.assertIn("имя сессии не записано", err)
        self.assertIn("метка сессии не записана", err)

    def test_set_with_only_ref_is_accepted(self):
        self.assertEqual(self.run_main("set", "--ref", "62ccf2")[0], 0)
        self.assertEqual(self.run_main("set")[0], 2)


class CommandTextTests(unittest.TestCase):
    def test_ceo_wake_ceo_command_takes_the_ref_from_list_agents(self):
        text = (PLUGIN / "commands" / "ceo.md").read_text(encoding="utf-8")
        self.assertIn("This session is <имя> [<метка>]", text)
        self.assertIn('--ref "<метка из ListAgents>"', text)
        self.assertIn("нет метки", text)


if __name__ == "__main__":
    unittest.main()
