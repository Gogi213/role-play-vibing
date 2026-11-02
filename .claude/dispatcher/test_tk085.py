"""TK-085 (В-33, В-35): побудка CEO событием — один headless-вызов `claude` на событие, ноль на обычный `done`; устав и команда `/ceo`
без `Monitor`. Запуск: `python -m pytest .claude/dispatcher/test_tk085.py`."""
from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ceo_wake as CW  # noqa: E402
import dispatch as D  # noqa: E402
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
        for n, kind in enumerate(("needs_owner", "blocked", "due", "следующий-не-определён", "goal-done"), 1):
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

    def test_goal_closed_is_one_event_per_goal(self):
        goals = Path(self.tmp.name) / ".claude" / "goals"
        goals.mkdir(parents=True)

        def goal(gid, status):
            text = "\n".join(["---", f"id: {gid}", f"status: {status}", "set: 2026-11-02 10:00", "accepted:", "---", "# цель", ""])
            (goals / f"{gid}.md").write_text(text, encoding="utf-8")
        goal("G-001", "accepted")
        goal("G-002", "open")
        state: dict = {}
        SIG.notify_goal_closed(state, NOW)
        self.assertEqual(self.stub.calls, [], "уже закрытые цели при первом обходе не будят")
        goal("G-002", "accepted")
        goal("G-003", "dropped")
        SIG.notify_goal_closed(state, NOW)
        SIG.notify_goal_closed(state, NOW)
        self.assertEqual(len(self.stub.calls), 2)
        self.assertIn("[цель закрыта] G-002", self.stub.calls[0][1])
        self.assertIn("[цель закрыта] G-003", self.stub.calls[1][1])

    def test_haiku_refusal_blocks_once_over_two_ticks(self):
        tickets = Path(self.tmp.name) / ".claude" / "tickets"
        tickets.mkdir(parents=True, exist_ok=True)
        head = ["---", "id: TK-900", "title: t", "owner: engineer", "status: todo", "executor: haiku", "kind: something-else",
                "updated: 2026-11-02T13:00:00+04:00", "---", "", "## Лог", ""]
        (tickets / "TK-900.md").write_text("\n".join(head), encoding="utf-8")
        state: dict = {}
        D._collect_candidates(state, NOW)
        D._collect_candidates(state, NOW + timedelta(minutes=1))
        self.assertEqual(len(self.stub.calls), 1, "Haiku-отказ — одно событие «сломалось», не два")
        self.assertIn("[сломалось] TK-900", self.stub.calls[0][1])

    def test_collect_candidates_wakes_on_goal_closed(self):
        goals = Path(self.tmp.name) / ".claude" / "goals"
        goals.mkdir(parents=True)
        (Path(self.tmp.name) / ".claude" / "tickets").mkdir(parents=True, exist_ok=True)

        def goal(status):
            text = "\n".join(["---", "id: G-001", f"status: {status}", "set: 2026-11-02 10:00", "accepted:", "---", "# цель", ""])
            (goals / "G-001.md").write_text(text, encoding="utf-8")
        goal("open")
        state: dict = {}
        D._collect_candidates(state, NOW)
        goal("accepted")
        D._collect_candidates(state, NOW + timedelta(minutes=1))
        D._collect_candidates(state, NOW + timedelta(minutes=2))
        self.assertEqual(len(self.stub.calls), 1, "обход тикетов: закрытая цель — одно событие")
        self.assertIn("[цель закрыта] G-001", self.stub.calls[0][1])

    def test_next_undefined_kind_is_the_one_goal_steps_sends(self):
        self.signal("следующий-не-определён", tid="TK-002", note="цель G-001: все шаги плана приняты")
        self.assertEqual(len(self.stub.calls), 1)
        self.assertIn("[шаг цели принят, следующий не определён] TK-002", self.stub.calls[0][1])

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
        charter = (PLUGIN / "templates/roles/ceo.md").read_text(encoding="utf-8")
        self.assertIn("**Побудка:**", charter)
        self.assertNotIn("или вопрос владельцу, не пересказ.", charter, "висящий обрывок старого пункта")
        self.assertNotIn("Monitor", (PLUGIN / "docs/signals.md").read_text(encoding="utf-8").split("## Побудка CEO")[1].split("## Передача")[0])

    def _svg_block(self, title):
        svg = (PLUGIN / "docs/img/ceo-signals.svg").read_text(encoding="utf-8")
        after = svg.split(f'class="t">{title}</text>')[1].split("<rect")[0]
        text = " ".join(re.findall(r">([^<]+)</text>", after))
        return {k.strip() for k in re.split(r"[·\s]+", text) if k.strip()}

    def test_scheme_wake_kinds_are_event_of(self):
        self.assertEqual(self._svg_block("будит"), set(CW.EVENT_OF))
        queue = self._svg_block("строка очереди")
        self.assertFalse(queue & set(CW.EVENT_OF), queue)
        self.assertIn("done", queue)

    def test_signals_table_wake_rows_are_event_of_and_has_not_woken_row(self):
        doc = (PLUGIN / "docs/signals.md").read_text(encoding="utf-8")
        rows = [r for r in doc.splitlines() if r.startswith("| `")]
        woken = {k for r in rows if r.rstrip().endswith("| будит событием |") for k in re.findall(r"`([^`]+)`", r.split("|")[1])}
        self.assertEqual(woken, set(CW.EVENT_OF))
        self.assertTrue(any(r.startswith("| `ceo-не-разбужен` |") for r in rows))


if __name__ == "__main__":
    unittest.main()
