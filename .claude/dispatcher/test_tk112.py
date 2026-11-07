"""TK-112: счётчик простоя — «влито, не выпущено» и «CEO не разбужен»; время прочтения очереди; часы без CEO."""
from __future__ import annotations

import atexit
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

_SANDBOX = tempfile.mkdtemp(prefix="rpv-test-proj-")
os.makedirs(os.path.join(_SANDBOX, ".claude", "roles"))
os.environ["CLAUDE_PROJECT_DIR"] = _SANDBOX
atexit.register(shutil.rmtree, _SANDBOX, True)
sys.path.insert(0, str(Path(__file__).resolve().parent))
import ceo_inbox as CI  # noqa: E402
import downtime as D  # noqa: E402
import unreleased as UNR  # noqa: E402
import watch as W  # noqa: E402

NOW = datetime(2026, 11, 7, 20, 0, 0).astimezone()
OFF = {"unreleased": False, "ceo_unwoken": False, "unmerged": False}


def iso(minutes_ago: float) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")


def fail_line(minutes_ago: float, why: str = "SendMessage отказал: FAILED") -> str:
    return (f"- {iso(minutes_ago)} TK-1 [{CI.FAIL_KIND}] {why}; событие: [нужно решение владельца] TK-1 — x. "
            "Прочитай `tickets.py inbox`.")


class Dir(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="rpv-tk112-"))
        self.addCleanup(shutil.rmtree, self.d, True)

    def put(self, name: str, text: str):
        (self.d / name).write_text(text, encoding="utf-8")

    def session(self, minutes_ago: float = 600):
        self.put("ceo-session.json", json.dumps({"id": "s", "name": "CEO", "at": iso(minutes_ago)}))

    def inbox(self, *lines: str):
        self.put("ceo-inbox.md", "\n".join(lines) + "\n")

    def flags(self):
        return D.watch_flags(self.d, NOW)


class SeenTests(Dir):
    def test_seen_mark_writes_count_and_time(self):
        CI.mark(self.d / CI.SEEN_NAME, 7, NOW)
        self.assertEqual((self.d / CI.SEEN_NAME).read_text(encoding="utf-8"), f"7 {iso(0)}")
        self.assertEqual(CI.read_at(self.d / CI.SEEN_NAME), iso(0))

    def test_seen_old_format_and_garbage_still_read(self):
        f = self.d / CI.SEEN_NAME
        self.inbox("a", "b", "c")
        for text, seen, at in (("2", 2, ""), ("", 0, ""), ("0", 0, ""), ("x", 0, ""), ("  2 \n", 2, ""),
                               ("2 не-время", 2, ""), ("9 " + iso(1), 0, iso(1))):  # 9 больше длины очереди — с начала
            f.write_text(text, encoding="utf-8")
            self.assertEqual(CI.read(self.d / "ceo-inbox.md", f)[1], seen, text)
            self.assertEqual(CI.read_at(f), at, text)

    def test_seen_missing_file(self):
        self.assertEqual(CI.read_at(self.d / "none"), "")
        self.assertEqual(CI.read(self.d / "none", self.d / "none2"), ([], 0))


class UnreleasedTests(Dir):
    def test_unreleased_record_open_follows_finding(self):
        ws = {}
        UNR.record_open(ws, NOW, "влито, не выпущено: x")
        self.assertEqual(ws[UNR.OPEN_KEY], iso(0))
        UNR.record_open(ws, NOW + timedelta(minutes=2), None)  # GitHub молчит — отметка остаётся и стареет
        self.assertEqual(ws[UNR.OPEN_KEY], iso(0))
        UNR.record_open(ws, NOW, "")
        self.assertNotIn(UNR.OPEN_KEY, ws)

    def test_unreleased_check_unreleased_records_from_same_message(self):
        ws = {}
        with mock.patch.object(W.UNR, "unreleased_message", return_value="влито, не выпущено: x"):
            self.assertEqual(len(W.check_unreleased(ws, NOW)), 1)
        self.assertIn(UNR.OPEN_KEY, ws)
        with mock.patch.object(W.UNR, "unreleased_message", return_value=""):
            W.check_unreleased(ws, NOW)
        self.assertNotIn(UNR.OPEN_KEY, ws)

    def test_unreleased_fresh_and_stale_mark(self):
        self.session()
        self.put("watch-state.json", json.dumps({UNR.OPEN_KEY: iso(3)}))
        self.assertTrue(self.flags()["unreleased"])
        self.put("watch-state.json", json.dumps({UNR.OPEN_KEY: iso(30)}))
        self.assertFalse(self.flags()["unreleased"])
        self.put("watch-state.json", "не json")
        self.assertFalse(self.flags()["unreleased"])
        self.put("watch-state.json", json.dumps({UNR.OPEN_KEY: "мусор"}))
        self.assertFalse(self.flags()["unreleased"])

    def test_unreleased_account_bucket_and_total(self):
        st = {}
        kw = dict(ready_unserved=False, work_present=False, waiting_nocond=False, poll_s=15)
        D.account(st, NOW, unreleased=True, **kw)
        _, rec, _ = D.account(st, NOW + timedelta(minutes=22 * 60), **kw)
        self.assertEqual(rec["unreleased_s"], 22 * 3600)  # дыра из TK-105: 22 ч при пустой очереди теперь видна
        self.assertEqual(D.total_min(rec), 22 * 60)


class UnwokenTests(Dir):
    def test_unwoken_unread_failure_counts_until_read(self):
        self.session()
        self.inbox("- старое", fail_line(40))
        self.assertTrue(self.flags()["ceo_unwoken"])
        CI.mark(self.d / CI.SEEN_NAME, 2, NOW)  # CEO прочитал очередь
        self.assertFalse(self.flags()["ceo_unwoken"])

    def test_unwoken_other_lines_and_old_failure_not_counted(self):
        self.session()
        self.inbox(f"- {iso(5)} TK-1 [needs_owner] вопрос")
        self.assertFalse(self.flags()["ceo_unwoken"])
        self.inbox(fail_line(13 * 60))  # старше 12 ч — CEO очередь не читает
        self.assertFalse(self.flags()["ceo_unwoken"])
        self.inbox(f"- без времени [{CI.FAIL_KIND}] причина", f"- мусор TK-1 [{CI.FAIL_KIND}] причина; событие: x")
        self.assertFalse(self.flags()["ceo_unwoken"])

    def test_unwoken_account_does_not_double_with_unreleased(self):
        st = {}
        kw = dict(ready_unserved=False, work_present=False, waiting_nocond=False, poll_s=15)
        D.account(st, NOW, unreleased=True, ceo_unwoken=True, **kw)
        _, rec, _ = D.account(st, NOW + timedelta(seconds=600), ceo_unwoken=True, **kw)
        self.assertEqual((rec["unreleased_s"], rec["unwoken_s"]), (600, 0))
        _, rec, _ = D.account(st, NOW + timedelta(seconds=900), **kw)
        self.assertEqual(rec["unwoken_s"], 300)


class AbsentTests(Dir):
    def setUp(self):
        super().setUp()
        self.put("watch-state.json", json.dumps({UNR.OPEN_KEY: iso(1)}))

    def test_absent_no_session_file(self):
        self.inbox(fail_line(20))
        self.assertEqual(self.flags(), OFF)

    def test_absent_listagents_not_found_after_last_presence(self):
        self.session(minutes_ago=300)
        self.inbox(fail_line(30, CI.ABSENT_WHY), fail_line(20))
        self.assertEqual(self.flags(), OFF)

    def test_absent_ends_when_ceo_reads_queue_or_calls_ceo_set(self):
        self.session(minutes_ago=300)
        self.inbox(fail_line(30, CI.ABSENT_WHY))
        CI.mark(self.d / CI.SEEN_NAME, 0, NOW - timedelta(minutes=10))  # прочёл позже отказа
        self.assertTrue(self.flags()["unreleased"])
        (self.d / CI.SEEN_NAME).unlink()
        self.assertFalse(self.flags()["unreleased"])
        self.session(minutes_ago=5)  # `/ceo set` позже отказа
        self.assertTrue(self.flags()["unreleased"])

    def test_absent_failure_reason_is_not_unwoken(self):
        self.session()
        self.inbox(fail_line(20, CI.ABSENT_WHY))
        self.assertFalse(self.flags()["ceo_unwoken"])


class WiringTests(unittest.TestCase):
    def test_wake_uses_shared_markers(self):
        src = (Path(__file__).resolve().parent / "ceo_wake.py").read_text(encoding="utf-8")
        self.assertIn("CI.FAIL_KIND", src)
        self.assertIn("CI.ABSENT_WHY", src)
        self.assertNotIn('"сессия CEO не найдена в ListAgents"', src)


if __name__ == "__main__":
    unittest.main()
