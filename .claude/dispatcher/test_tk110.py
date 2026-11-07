"""TK-110 (В-55): побудка CEO по имени сессии, доставка только по маркеру, фон и пачка, `watch-unreleased` будит CEO.
Запуск: `python -m pytest .claude/dispatcher/test_tk110.py`."""
from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import threading
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
                      mock.patch.object(CW.notify, "send", lambda text, **kw: self.sent.append(text) or True)):
            patch.start()
            self.addCleanup(patch.stop)

    def run_stub(self, cmd, **kw):
        self.calls.append(kw["input"].decode("utf-8"))
        return subprocess.CompletedProcess(cmd, 0, self.out, b"")

    def signal(self, kind="needs_owner", tid="TK-001", note="причина"):
        SIG.append_ceo_inbox(tid, kind, note, NOW)

    def inbox(self):
        return CFG.CEO_INBOX.read_text(encoding="utf-8") if CFG.CEO_INBOX.exists() else ""


class DeliveryTests(Sandbox):
    def setUp(self):
        super().setUp()
        patch = mock.patch.object(CW, "BACKGROUND", False)
        patch.start()
        self.addCleanup(patch.stop)
        CW.write_session("sess-1", "RPV - CAO")

    def test_prompt_searches_by_name_and_asks_for_marker(self):
        self.signal()
        prompt = self.calls[0]
        self.assertIn('"RPV - CAO"', prompt)
        self.assertNotIn("sess-1", prompt)
        self.assertIn("DELIVERED", prompt)
        self.assertEqual(self.sent, [])

    def test_only_id_is_searched_by_id(self):
        CW.session_file().write_text(json.dumps({"id": "sess-9", "name": ""}), encoding="utf-8")
        self.signal()
        self.assertIn('"sess-9"', self.calls[0])

    def test_quotes_and_newlines_in_name_stay_one_line(self):
        CW.write_session("s", 'CEO "главный"\nвторая')
        self.signal()
        self.assertIn('"CEO \\"главный\\" вторая"', self.calls[0])

    def test_not_found_is_failure(self):
        self.out = b"NOT_FOUND"
        self.signal()
        self.assertIn("[ceo-не-разбужен]", self.inbox())
        self.assertEqual(len(self.sent), 1)

    def test_send_message_refusal_is_failure(self):
        self.out = b"FAILED: recipient not reachable"
        self.signal()
        self.assertIn("[ceo-не-разбужен]", self.inbox())
        self.assertIn("SendMessage отказал", self.inbox())
        self.assertEqual(len(self.sent), 1)

    def test_no_marker_is_failure(self):
        for out in (b"", b"ok", "Готово, отправил".encode()):
            self.out = out
            self.sent.clear()
            self.signal()
            self.assertEqual(len(self.sent), 1, out)
        self.assertIn("нет подтверждения доставки", self.inbox())


class BatchTests(Sandbox):
    def setUp(self):
        super().setUp()
        patch = mock.patch.object(CW, "BACKGROUND", True)  # conftest гасит фон для всех тестов; здесь он и проверяется
        patch.start()
        self.addCleanup(patch.stop)

    def test_events_during_a_run_go_in_one_run_and_wake_returns_at_once(self):
        gate, started = threading.Event(), threading.Event()
        runs = []

        def slow(cmd, **kw):
            runs.append(kw["input"].decode("utf-8"))
            started.set()
            gate.wait(5)
            return subprocess.CompletedProcess(cmd, 0, b"DELIVERED", b"")
        CW.write_session("sess-1", "RPV - CAO")
        with mock.patch.object(CW.haiku_aux.hide, "run", slow):
            self.signal(tid="TK-001")
            self.assertTrue(started.wait(5))
            for tid in ("TK-002", "TK-003", "TK-004"):
                self.signal(tid=tid)  # запуск занят: возврат без ожидания
            gate.set()
            CW.wait_idle(10)
        self.assertEqual(len(runs), 2, "первое событие и пачка из трёх — два запуска, не четыре")
        for tid in ("TK-002", "TK-003", "TK-004"):
            self.assertIn(f"{tid} —", runs[1])
        self.assertEqual(self.sent, [])

    def test_failed_batch_is_one_telegram_message_and_a_line_per_event(self):
        CW.write_session("sess-1", "X")
        self.out = b"NOT_FOUND"
        gate = threading.Event()
        real = CW._deliver

        def held(batch):
            gate.wait(5)
            return real(batch)
        with mock.patch.object(CW, "_deliver", held):
            for tid in ("TK-001", "TK-002", "TK-003"):
                self.signal(tid=tid)
            gate.set()
            CW.wait_idle(10)
        self.assertEqual(self.inbox().count("[ceo-не-разбужен]"), 3)
        self.assertLess(len(self.sent), 3)

    def test_not_an_event_starts_nothing(self):
        self.signal("done")
        CW.wait_idle(2)
        self.assertEqual(self.calls, [])


class IsolationTests(Sandbox):
    """E-028: поток побудки переживает тест; пути, сессия и отправка берутся в момент wake(), а не в момент работы потока."""

    def setUp(self):
        super().setUp()
        patch = mock.patch.object(CW, "BACKGROUND", True)
        patch.start()
        self.addCleanup(patch.stop)

    def test_thread_writes_where_wake_was_called_not_where_paths_point_later(self):
        gate, runs, sends = threading.Event(), [], []
        CW.write_session("sess-1", "RPV - CAO")
        live = tempfile.TemporaryDirectory()
        self.addCleanup(live.cleanup)

        def held(cmd, **kw):
            runs.append(kw["input"].decode("utf-8"))
            gate.wait(5)
            return subprocess.CompletedProcess(cmd, 0, b"NOT_FOUND", b"")
        mine = CFG.CEO_INBOX
        with mock.patch.object(CW.haiku_aux.hide, "run", held),                 mock.patch.object(CW.notify, "send", lambda text, **kw: sends.append(kw.get("log_dir")) or True):
            self.signal(tid="TK-001")           # событие в очереди, поток держит запуск
            CFG.configure_project(live.name)    # тест кончился: пути снова «настоящие»
            (Path(live.name) / ".claude" / "dispatcher").mkdir(parents=True)
            gate.set()
            CW.wait_idle(10)
            live_inbox, live_dir = CFG.CEO_INBOX, CFG.DISPATCHER_DIR
            CFG.configure_project(self.tmp.name)
        self.assertEqual(len(runs), 1, "сессия читается при wake(): после смены путей поток всё равно шлёт именованной сессии")
        self.assertIn("[ceo-не-разбужен]", mine.read_text(encoding="utf-8"))
        self.assertFalse(live_inbox.exists(), "в «настоящий» ceo-inbox.md ничего не попало")
        self.assertEqual([d for d in sends if d is not None], [mine.parent])
        self.assertFalse((live_dir / "mechanics.log").exists())


class ConftestTests(unittest.TestCase):
    def test_background_is_off_in_every_test_by_default(self):
        self.assertFalse(CW.BACKGROUND, "conftest гасит фон побудки: поток не переживает тест и не пишет в живой проект (E-028)")


class SessionNameTests(Sandbox):
    def run_main(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = CW.main(["--project", self.tmp.name, *args])
        return code, out.getvalue(), err.getvalue()

    def test_set_without_name_keeps_previous_name(self):
        self.run_main("set", "--id", "old", "--name", "RPV - CAO")
        code, out, err = self.run_main("set", "--id", "new")
        self.assertEqual(code, 0)
        session = json.loads(out)
        self.assertEqual((session["id"], session["name"]), ("new", "RPV - CAO"))
        self.assertEqual(err, "")

    def test_set_without_any_name_warns(self):
        code, _out, err = self.run_main("set", "--id", "new")
        self.assertEqual(code, 0)
        self.assertIn("ПРЕДУПРЕЖДЕНИЕ", err)

    def test_name_is_trimmed_and_case_kept(self):
        self.run_main("set", "--id", "a", "--name", "  Rpv   CAO  ")
        self.assertEqual(CW.read_session()["name"], "Rpv CAO")

    def test_set_with_nothing_refuses(self):
        self.assertEqual(self.run_main("set")[0], 2)

    def test_command_text_takes_the_name_from_list_agents(self):
        text = (PLUGIN / "commands" / "ceo.md").read_text(encoding="utf-8")
        self.assertIn("This session is", text)
        self.assertIn("ПРЕДУПРЕЖДЕНИЕ", text)
        self.assertIn("ListAgents", text.split("---")[1])  # разрешён в allowed-tools


class UnreleasedWakesTests(Sandbox):
    def test_kind_is_an_event(self):
        self.assertIn("watch-unreleased", CW.EVENT_OF)

    def test_unreleased_signal_wakes_ceo(self):
        CW.write_session("s", "RPV - CAO")
        with mock.patch.object(CW, "BACKGROUND", False):
            self.signal("watch-unreleased", tid="*", note="влито, не выпущено: тест")
        self.assertEqual(len(self.calls), 1)
        self.assertIn("[влито, не выпущено] *", self.calls[0])


class UnreleasedDefaultTests(unittest.TestCase):
    def reaches_github(self, **rpv):
        import unreleased as UNR
        with tempfile.TemporaryDirectory() as tmp:
            old = CFG.PROJECT_ROOT
            CFG.configure_project(tmp)
            try:
                (Path(tmp) / ".claude").mkdir(exist_ok=True)
                (Path(tmp) / ".claude" / "rpv.json").write_text(json.dumps(rpv), encoding="utf-8")
                calls = []
                with mock.patch.object(UNR, "idle_minutes", return_value=60.0):
                    UNR.unreleased_message({}, NOW, lambda path: calls.append(path) or {"default_branch": "main"})
                return bool(calls)
            finally:
                CFG.configure_project(old)

    def test_on_by_default_when_ci_repo_is_set(self):
        self.assertTrue(self.reaches_github(ci_repo="owner/repo"))

    def test_true_is_on(self):
        self.assertTrue(self.reaches_github(ci_repo="owner/repo", watch_unreleased=True))

    def test_false_and_junk_are_off(self):
        self.assertFalse(self.reaches_github(ci_repo="owner/repo", watch_unreleased=False))
        self.assertFalse(self.reaches_github(ci_repo="owner/repo", watch_unreleased="false"))

    def test_no_ci_repo_is_silent(self):
        self.assertFalse(self.reaches_github())
        self.assertFalse(self.reaches_github(watch_unreleased=True))

    def test_docstring_is_not_stale(self):
        text = (PLUGIN / ".claude" / "dispatcher" / "unreleased.py").read_text(encoding="utf-8")
        self.assertNotIn("умолчание — выключен", text)


if __name__ == "__main__":
    unittest.main()
