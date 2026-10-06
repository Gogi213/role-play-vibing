import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import downtime as D  # noqa: E402

T0 = datetime(2026, 10, 6, 12, 0, 0).astimezone()
KW = dict(poll_s=15, slo_min=10)


def step(st, t, **flags):
    base = dict(ready_unserved=False, work_present=False, waiting_nocond=False)
    return D.account(st, T0 + timedelta(seconds=t), **{**base, **flags}, **KW)


class DowntimeTests(unittest.TestCase):
    def test_idle_accrues_by_previous_tick_flags(self):
        st = {}
        step(st, 0, ready_unserved=True, work_present=True)
        _, rec, _ = step(st, 15)
        self.assertEqual(rec["idle_s"], 15)

    def test_flag_cleared_stops_accrual(self):
        st = {}
        step(st, 0, ready_unserved=True, work_present=True)
        step(st, 15, work_present=True)
        _, rec, _ = step(st, 30, work_present=True)
        self.assertEqual(rec["idle_s"], 15)

    def test_stall_only_beyond_poll_and_grace(self):
        st = {}
        step(st, 0, work_present=True)
        _, rec, _ = step(st, 60, work_present=True)  # 60 с < 15 + 60 — норма
        self.assertEqual(rec["stall_s"], 0)
        _, rec, _ = step(st, 60 + 600, work_present=True)  # диспетчер молчал 10 мин при работе
        self.assertEqual(rec["stall_s"], 600 - 15 - 60)

    def test_no_work_no_stall(self):
        st = {}
        step(st, 0)
        _, rec, _ = step(st, 3600)
        self.assertEqual(rec["stall_s"], 0)

    def test_waiting_without_condition_counts(self):
        st = {}
        step(st, 0, waiting_nocond=True)
        _, rec, _ = step(st, 120)
        self.assertEqual(rec["wait_s"], 120)

    def test_slo_breach_alerts_once_per_day(self):
        st = {}
        step(st, 0, ready_unserved=True)
        _, rec, b = step(st, 500, ready_unserved=True)
        self.assertFalse(b)
        _, rec, b = step(st, 700, ready_unserved=True)  # 700 с > 600
        self.assertTrue(b)
        _, rec, b = step(st, 800, ready_unserved=True)
        self.assertFalse(b)

    def test_old_days_pruned(self):
        st = {"downtime": {f"2026-09-{d:02d}": {"idle_s": 0, "wait_s": 0, "stall_s": 0, "alerted": False}
                           for d in range(1, 29)}}
        step(st, 0)
        self.assertEqual(len(st["downtime"]), D.KEEP_DAYS)


if __name__ == "__main__":
    unittest.main()
