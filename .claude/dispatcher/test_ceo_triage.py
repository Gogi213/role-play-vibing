"""Тесты сортировщика сигналов CEO (TK-086): правила, запасной вариант Haiku, ack/сводка/запуск CEO — без сети и модели."""
from __future__ import annotations

import atexit
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_SANDBOX = tempfile.mkdtemp(prefix="rpv-test-proj-")
os.makedirs(os.path.join(_SANDBOX, ".claude", "roles"))
os.environ["CLAUDE_PROJECT_DIR"] = _SANDBOX
os.environ["RPV_BUS_DISABLE"] = "1"
atexit.register(shutil.rmtree, _SANDBOX, True)
sys.path.insert(0, str(Path(__file__).resolve().parent))
import ceo_triage as C  # noqa: E402
import dispatch as D  # noqa: E402

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone(timedelta(hours=4)))


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        (base / "tickets").mkdir()
        self._orig = (D.DISPATCHER_DIR, D.TICKETS_DIR, D.CEO_INBOX, D.RUNS_DIR)
        D.DISPATCHER_DIR, D.TICKETS_DIR, D.CEO_INBOX, D.RUNS_DIR = base, base / "tickets", base / "ceo-inbox.md", base / "runs"

    def tearDown(self):
        D.DISPATCHER_DIR, D.TICKETS_DIR, D.CEO_INBOX, D.RUNS_DIR = self._orig
        self.tmp.cleanup()

    def ticket(self, tid, status, wait_for=""):
        (D.TICKETS_DIR / f"{tid}.md").write_text(
            f"---\nid: {tid}\ntitle: t\nowner: engineer\nstatus: {status}\nwait_for: {wait_for}\n---\n\nописание\n\n## Лог\n",
            encoding="utf-8")

    def inbox(self, *lines):
        D.CEO_INBOX.write_text("".join(f"- 2026-10-08T11:59:00+04:00 {l}\n" for l in lines), encoding="utf-8")


class Rules(Sandbox):
    def ev(self, kind, tid="TK-001", note="x"):
        return {"kind": kind, "tid": tid, "note": note}

    def test_noise_and_action_rules(self):
        self.ticket("TK-001", "waiting", "file:/x")
        self.assertEqual(C.rule_class(self.ev("bus-up", "*"), {}, NOW)[0], C.NOISE)
        self.assertEqual(C.rule_class(self.ev("watch-no-plan"), {}, NOW)[0], C.NOISE)
        self.assertEqual(C.rule_class(self.ev("watch-orphan"), {}, NOW)[0], C.NOISE)  # waiting + wait_for
        self.assertEqual(C.rule_class(self.ev("blocked"), {}, NOW)[0], C.ACTION)
        self.assertIsNone(C.rule_class(self.ev("watch-orphan", "*", "нет тикета"), {}, NOW))

    def test_closed_ticket_is_noise_but_done_is_not(self):
        self.ticket("TK-002", "done")
        self.assertEqual(C.rule_class(self.ev("watch-orphan", "TK-002"), {}, NOW)[0], C.NOISE)
        self.assertEqual(C.rule_class(self.ev("done", "TK-002"), {}, NOW)[0], C.INFO)

    def test_duplicate_within_hour(self):
        e = self.ev("watch-x", "*", "сбой 12:00 #5")
        recent = {f"*|watch-x|{C._norm(e['note'])}": "2026-10-08T11:30:00+04:00"}
        self.assertEqual(C.rule_class(dict(e, note="сбой 12:05 #6"), recent, NOW)[0], C.NOISE)
        old = {k: "2026-10-08T09:00:00+04:00" for k in recent}
        self.assertIsNone(C.rule_class(e, old, NOW))


class RunOnce(Sandbox):
    def test_pipeline_ack_digest_launch(self):
        self.ticket("TK-003", "waiting", "file:/y")
        self.inbox("TK-003 [watch-orphan] тихо", "* [watch-summary] 3 сигнала", "* [weird] что-то странное", "* [bus-up] ok")
        asked = []
        launched = []
        r = C.run_once(NOW, classify=lambda ev: (asked.extend(ev) or [(C.ACTION, "нужно")] * len(ev), {"input_tokens": 9}),
                       launch=lambda ev, now: launched.extend(ev) or True)
        self.assertEqual(r, {"noise": 2, "info": 1, "action": 1, "launched": True})
        self.assertEqual([e["kind"] for e in asked], ["weird"])  # Haiku видит только остаток
        self.assertEqual(len(launched), 1)
        self.assertIn("watch-summary", D.DISPATCHER_DIR.joinpath("ceo-digest.md").read_text(encoding="utf-8"))
        self.assertEqual(C.read_events(), [])  # отметка сдвинута — второй проход пуст
        recs = [json.loads(l) for l in D.DISPATCHER_DIR.joinpath("triage.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertTrue(any("haiku_usage" in x for x in recs))

    def test_ceo_busy_keeps_actions(self):
        self.inbox("* [weird] что-то")
        C.run_once(NOW, classify=lambda ev: ([(C.ACTION, "н")] * len(ev), {}), launch=lambda ev, now: False)
        self.assertEqual(len(C.read_events()), 1)  # CEO-роль занята — действие не потеряно

    def test_haiku_failure_is_action(self):
        res, _ = C.haiku_classify([{"tid": "*", "kind": "k", "note": "n"}], claude_bin="definitely-not-a-binary")
        self.assertEqual(res[0][0], C.ACTION)


if __name__ == "__main__":
    unittest.main()
