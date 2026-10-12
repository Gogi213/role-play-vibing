"""TK-226: подогрев кэша сессий ожидающих ролей."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

_SANDBOX = tempfile.mkdtemp(prefix="rpv-warm-proj-")
os.makedirs(os.path.join(_SANDBOX, ".claude", "roles"))
os.environ["CLAUDE_PROJECT_DIR"] = _SANDBOX
os.environ["RPV_BUS_DISABLE"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parent))
import dispatch as D  # noqa: E402

TZ = timezone(timedelta(hours=4))
NOW = datetime(2026, 10, 13, 12, 0, tzinfo=TZ)


def _tkt(status="waiting", wait_for="file:/x", updated=NOW - timedelta(hours=2)):
    t = mock.Mock()
    t.id, t.owner, t.status, t.executor = "TK-1", "engineer", status, ""
    t.header = {"wait_for": wait_for, "updated": updated.isoformat()}
    return t


class Warm(unittest.TestCase):
    def run_warm(self, tkt, state, enabled=True):
        pings = []
        with mock.patch.object(D, "WARM_ENABLED", enabled), \
             mock.patch.object(D.T, "list_tickets", return_value=[Path("TK-1.md")]), \
             mock.patch.object(D.T, "read_ticket", return_value=tkt), \
             mock.patch.object(D.threading, "Thread") as th:
            th.return_value.is_alive.return_value = False
            n = D.warm_waiting_sessions(state, NOW)
            if th.called:
                pings.append(th.call_args.kwargs["args"][0])
        D.WARMING.clear()
        return n, pings

    def state(self, last="2026-10-13T10:30:00+04:00"):
        return {"ticket_sessions": {"TK-1::engineer": {"session_id": "sid-1", "last_context_tokens": 90000}},
                "sessions": {"TK-1::engineer": {"last_woken": last}}}

    def test_off_by_default(self):
        self.assertEqual(self.run_warm(_tkt(), self.state(), enabled=False)[0], 0)

    def test_pings_resume_after_50_min(self):
        st = self.state()
        n, pings = self.run_warm(_tkt(), st)
        self.assertEqual(n, 1)
        self.assertEqual(pings[0][pings[0].index("--resume") + 1], "sid-1")
        self.assertEqual(st["cache_warm"]["TK-1"]["n"], 1)
        self.assertNotIn("active_runs", st)  # лимитов запусков не тратит

    def test_fresh_cache_not_pinged(self):
        self.assertEqual(self.run_warm(_tkt(), self.state(last="2026-10-13T11:30:00+04:00"))[0], 0)

    def test_cap_and_status_and_no_session(self):
        self.assertEqual(self.run_warm(_tkt(updated=NOW - timedelta(hours=13)), self.state())[0], 0)
        self.assertEqual(self.run_warm(_tkt(status="in_progress"), self.state())[0], 0)
        self.assertEqual(self.run_warm(_tkt(), {})[0], 0)


if __name__ == "__main__":
    unittest.main()


class WarmLog(unittest.TestCase):
    def test_usage_line_in_runs_log(self):
        log = Path(_SANDBOX) / "runs.log"
        with mock.patch.object(D, "RUNS_LOG", log):
            D._warm_log("TK-1", "engineer", '{"usage":{"input_tokens":3,"output_tokens":5,'
                        '"cache_read_input_tokens":90000,"cache_creation_input_tokens":40}}')
        line = log.read_text(encoding="utf-8")
        self.assertIn("kind=warm", line)
        self.assertIn("cr_tok=90000 cw_tok=40", line)
        self.assertTrue(line.endswith("\n") and line.count("\n") == 1)

    def test_no_second_ping_while_warming(self):
        alive = mock.Mock()
        alive.is_alive.return_value = True
        D.WARMING["TK-1"] = alive
        try:
            n = Warm().run_warm(_tkt(), Warm().state())[0]
        finally:
            D.WARMING.clear()
        self.assertEqual(n, 0)
