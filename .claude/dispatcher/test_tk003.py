"""TK-003 (CP1-в): runs.log с terminal_reason/is_error, ошибка API (ECONNRESET, вход) — пауза и повтор, не холостой ход."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from test_dispatch import D, T, dt  # песочница проекта поднимает test_dispatch при импорте

NOW = dt("2026-10-27T12:00:00+04:00")
ECONNRESET = {"session_id": "s1", "is_error": True, "terminal_reason": "api_error", "api_error_status": None,
              "result": "API Error: Unable to connect to API (ECONNRESET)", "usage": {"input_tokens": 0, "output_tokens": 0}}
AUTH = {"session_id": "s1", "is_error": True, "terminal_reason": "api_error", "api_error_status": 401,
        "result": "Failed to authenticate. API Error: 401"}
OK = {"session_id": "s1", "is_error": False, "terminal_reason": "completed", "result": "готово", "total_cost_usd": 0.01}


class ApiErrorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.tickets_dir = base / "tickets"
        self.tickets_dir.mkdir()
        self.disp = base / "dispatcher"
        self.disp.mkdir()
        names = ("TICKETS_DIR", "PROJECT_ROOT", "DISPATCHER_DIR", "STATE_FILE", "RUNS_DIR", "RUNS_LOG", "CEO_INBOX",
                 "CEO_WAKE_LOG", "STOP_DIR")
        self._orig = {k: getattr(D, k) for k in names}
        D.TICKETS_DIR, D.PROJECT_ROOT, D.DISPATCHER_DIR = self.tickets_dir, base, self.disp
        D.STATE_FILE, D.RUNS_DIR, D.RUNS_LOG = self.disp / "state.json", self.disp / "runs", self.disp / "runs.log"
        D.CEO_INBOX, D.CEO_WAKE_LOG, D.STOP_DIR = self.disp / "ceo-inbox.md", self.disp / "ceo-wake.log", self.disp / "stop"
        D.RUNNING.clear()
        self._orig_popen = D._popen
        D._popen = lambda cmd, **kw: subprocess.Popen([sys.executable, "-c", "pass"], **kw)

    def tearDown(self):
        D._popen = self._orig_popen
        D.RUNNING.clear()
        for k, v in self._orig.items():
            setattr(D, k, v)
        self.tmp.cleanup()

    def finish(self, path, result, n=0, state=None):
        run_file = self.disp / f"r{n}.json"
        run_file.write_text(json.dumps(result), encoding="utf-8")
        info = {"role": "engineer", "popen": None, "pid": None, "started": NOW, "attempt": 0, "run_file": run_file,
                "err_file": run_file, "out_fh": None, "err_fh": None, "reason": "todo", "status_at_launch": "todo",
                "log_keys_at_launch": []}
        state = state if state is not None else D.load_state()
        D._finish_run(path.stem, info, state, NOW + timedelta(minutes=n), timed_out=False)
        return state

    def ticket(self):
        return T.create_ticket(self.tickets_dir, owner="engineer", title="API", now=NOW)

    def test_runs_log_has_terminal_reason_and_is_error(self):
        path = self.ticket()
        T.append_log(path, "engineer", "готово", now=NOW)
        T.write_header_updates(path, {"status": "in_progress"}, now=NOW)  # запись есть, статус не todo: повтора нет
        self.finish(path, OK)
        line = D.RUNS_LOG.read_text(encoding="utf-8").splitlines()[-1]
        self.assertIn("terminal_reason=completed is_error=False status=ok", line)

    def test_runs_log_api_error_is_not_ok(self):
        path = self.ticket()
        self.finish(path, ECONNRESET)
        line = D.RUNS_LOG.read_text(encoding="utf-8").splitlines()[-1]
        self.assertIn("terminal_reason=api_error is_error=True status=api_error", line)

    def test_econnreset_is_pause_not_idle_run(self):
        path = self.ticket()
        state = self.finish(path, ECONNRESET)
        self.assertEqual(T.read_ticket(path).status, "todo")
        self.assertNotIn("idle_runs", {k: v for k, v in state.items() if v})
        self.assertEqual(D.RUNNING, {}, "повтор не сразу: пауза")
        self.assertTrue(D._limit_paused(state, NOW + timedelta(minutes=1)))
        self.assertFalse(D._limit_paused(state, NOW + timedelta(minutes=6)))

    def test_three_api_errors_in_a_row_block_with_real_reason(self):
        path = self.ticket()
        state = D.load_state()
        for n in range(D.MAX_API_ERRORS):
            self.assertNotEqual(T.read_ticket(path).status, "blocked")
            self.finish(path, ECONNRESET, n, state)
        tkt = T.read_ticket(path)
        self.assertEqual(tkt.status, "blocked")
        self.assertIn("ECONNRESET", tkt.log_raw)
        self.assertIn("ECONNRESET", D.CEO_INBOX.read_text(encoding="utf-8"))

    def test_good_run_resets_api_error_count(self):
        path = self.ticket()
        T.append_log(path, "engineer", "готово", now=NOW)
        T.write_header_updates(path, {"status": "in_progress"}, now=NOW)  # запись есть, статус не todo: повтора нет
        state = D.load_state()
        self.finish(path, ECONNRESET, 0, state)
        self.finish(path, ECONNRESET, 1, state)
        self.finish(path, OK, 2, state)
        self.assertNotIn(f"{path.stem}::engineer", state.get("api_errors", {}))

    def test_auth_error_signals_ceo_at_once(self):
        path = self.ticket()
        state = self.finish(path, AUTH)
        self.assertIn("auth", D.CEO_INBOX.read_text(encoding="utf-8"))
        self.assertEqual(T.read_ticket(path).status, "todo", "тикет не виноват и не blocked")
        self.assertTrue(D._limit_paused(state, NOW + timedelta(minutes=20)))

    def test_429_is_still_limit_not_api_error(self):
        self.assertFalse(D._api_error({"api_error_status": 429, "terminal_reason": "api_error", "is_error": True,
                                       "result": "You've hit your session limit"}))


if __name__ == "__main__":
    unittest.main()
