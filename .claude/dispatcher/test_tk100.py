"""TK-100 (В-40): второй Судья — только когда очередь сдач на приёмку не меньше `judge_scale_queue` (умолчание 2),
верхний предел — `role_parallel.judge`; упавшая очередь идущий запуск не снимает; решение — одной строкой, при смене."""
from __future__ import annotations

import contextlib
import io
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dispatch as D  # noqa: E402
import rpv_config as RC  # noqa: E402

NOW = datetime(2026, 11, 2, 18, 0, tzinfo=timezone.utc)


def cand(tid, role):
    return Path(tid + ".md"), SimpleNamespace(id=tid), SimpleNamespace(role=role, header_updates=None, reason="тест")


class JudgeScale(unittest.TestCase):
    def setUp(self):
        D.RUNNING.clear()
        D._JUDGE_SCALE_MODE[0] = "idle"
        self.cfg = {"role_parallel": {"engineer": 2, "judge": 2}}
        for p in (mock.patch.object(D.P, "setting", side_effect=lambda k: self.cfg.get(k)),
                  mock.patch.object(D, "max_parallel", return_value=10),
                  mock.patch.object(D, "_rate_limited", return_value=False),
                  mock.patch.object(D, "_candidate_sort_key", return_value=0),
                  mock.patch.object(D, "launch_run", side_effect=lambda path, role, *a, **k: D.RUNNING.update(
                      {path.stem: {"role": role}}))):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(D.RUNNING.clear)

    def run_tick(self, waiting, running=()):
        """Сколько новых запусков Судьи за тик при `waiting` ждущих сдачах и уже идущих `running` Судьях; (запущено, лог)."""
        D.RUNNING.clear()
        D.RUNNING.update({tid: {"role": "judge"} for tid in running})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            launched, _ = D._launch_candidates([cand(t, "judge") for t in waiting], {}, NOW, False)
        return launched, out.getvalue()

    def test_queue_below_threshold_one_judge(self):
        self.assertEqual(self.run_tick(["TK-1"])[0], 1, "свободных слотов хватает — единственная сдача берётся")
        self.assertEqual(self.run_tick(["TK-2"], running=["TK-1"])[0], 0, "очередь 1 < 2 — второй Судья не стартует")

    def test_queue_at_threshold_two_judges(self):
        self.assertEqual(self.run_tick(["TK-2", "TK-3"], running=["TK-1"])[0], 1, "очередь 2 — второй Судья")
        self.assertEqual(self.run_tick(["TK-1", "TK-2"])[0], 2)
        launched = self.run_tick(["TK-3", "TK-4", "TK-5"], running=["TK-1"])[0]
        self.assertEqual((launched, len(D.RUNNING)), (1, 2), "выше порога — всё равно не больше role_parallel.judge")

    def test_queue_drop_keeps_running_judges(self):
        self.assertEqual(self.run_tick([], running=["TK-1", "TK-2"])[0], 0)
        self.assertEqual(sorted(D.RUNNING), ["TK-1", "TK-2"], "очередь упала — идущие запуски на месте")
        self.assertEqual(self.run_tick(["TK-3"], running=["TK-1", "TK-2"])[0], 0, "и третий не стартует")

    def test_default_threshold_is_two(self):
        self.assertNotIn("judge_scale_queue", self.cfg)
        self.assertEqual(D.judge_scale_queue(), 2)
        self.assertEqual(self.run_tick(["TK-2"], running=["TK-1"])[0], 0)
        self.assertEqual(self.run_tick(["TK-2", "TK-3"], running=["TK-1"])[0], 1)
        for bad in (0, -1, True, "3"):
            self.cfg["judge_scale_queue"] = bad
            self.assertEqual(D.judge_scale_queue(), 2, bad)

    def test_custom_threshold(self):
        self.cfg["judge_scale_queue"] = 3
        self.assertEqual(self.run_tick(["TK-2", "TK-3"], running=["TK-1"])[0], 0)
        self.assertEqual(self.run_tick(["TK-2", "TK-3", "TK-4"], running=["TK-1"])[0], 1)

    def test_judge_cap_one_never_scales(self):
        for rp in ({"judge": 1}, {}):
            self.cfg["role_parallel"] = rp
            self.assertEqual(self.run_tick(["TK-2", "TK-3", "TK-4"], running=["TK-1"])[0], 0, rp)

    def test_engineers_unchanged(self):
        D.RUNNING.clear()
        launched, _ = D._launch_candidates([cand(f"TK-{n}", "engineer") for n in (1, 2, 3)], {}, NOW, False)
        self.assertEqual((launched, sorted(D.RUNNING)), (2, ["TK-1", "TK-2"]), "инженеры — по role_parallel.engineer")

    def test_decision_logged_once_per_change(self):
        _, log = self.run_tick(["TK-2", "TK-3"], running=["TK-1"])
        self.assertEqual(log.count("второй Судья разрешён: очередь на приёмку 2 ≥ порога 2"), 1, log)
        self.assertEqual(self.run_tick(["TK-2", "TK-3"], running=["TK-1"])[1], "", "решение то же — строки нет")
        _, log = self.run_tick([], running=["TK-1"])
        self.assertEqual(log.count("второй Судья не нужен: очередь на приёмку 0 < порога 2"), 1, log)
        self.assertEqual(self.run_tick([])[1], "")

    def test_cap_one_reason_logged(self):
        self.cfg["role_parallel"] = {"judge": 1}
        _, log = self.run_tick(["TK-2", "TK-3"], running=["TK-1"])
        self.assertIn("второй Судья не запущен: очередь на приёмку 2 ≥ порога 2, но role_parallel.judge = 1", log)

    def test_config_key_validated(self):
        check = RC.KEYS["judge_scale_queue"][0]
        self.assertTrue(check(1) and check(5))
        self.assertFalse(check(0) or check(True) or check("2"))


if __name__ == "__main__":
    unittest.main()
