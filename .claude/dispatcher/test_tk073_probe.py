"""TK-073 (В-31, п.3, 4, 6–9): чтение записи пробы (пик по слову «пик», латинские единицы, «~», разметка, «1 ч 40 мин»), защита
от нечисла в пороге, срок против остатка оценки, относительный путь --probe, запись в лог при выключенном флаге,
строки шаблонов ролей — условные; hostload: новый пик строго меньше, формы OOM; dispatch: Sonnet-критик у Судьи."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dispatch as D  # noqa: E402
import hostload as HL  # noqa: E402
import probe as PR  # noqa: E402
import project as P  # noqa: E402
import rpv_config  # noqa: E402
from test_tk069 import GOOD  # noqa: E402

PLUGIN = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 31, 12, 0, tzinfo=timezone(timedelta(hours=4)))
SPEC = "host:gpu1:/tmp/job.rc"


class PeakTests(unittest.TestCase):
    def test_peak_word_wins_over_other_sizes(self):
        self.assertEqual(PR.peak_gb("Тяжёлый: x (вход 2 ГБ) · пик 5,5 ГБ"), 5.5)

    def test_several_sizes_without_peak_word_refused(self):
        self.assertIsNone(PR.peak_gb("Тяжёлый: x · вход 2 ГБ · 5 ГБ"))
        self.assertIn("нет пика памяти", PR.problem(GOOD.replace("пик 5,5 ГБ", "вход 2 ГБ, итог 5 ГБ"), 200))

    def test_single_size_without_peak_word_still_read(self):
        self.assertEqual(PR.peak_gb("Тяжёлый: x · 512 МБ"), 0.5)


class FormTests(unittest.TestCase):
    def test_estimate_forms(self):
        for text, minutes in (("оценка: 100 мин", 100), ("Оценка: ≈ 100 min", 100), ("оценка: ~2 h", 120), ("оценка: около 5 ч", 300),
                              ("**оценка:** 1 ч 40 мин", 100), ("оценка: 1 ч 40 мин", 100), ("оценка: 1,5 ч", 90)):
            with self.subTest(text=text):
                self.assertEqual(PR.estimate_min(text), minutes)

    def test_latin_units_in_methods(self):
        text = "a (как есть): 5 min, 512M\nb: 30 s, 1.5G\nТяжёлый: x · пик 4G\nN: 4\nоценка: 90 min"
        self.assertEqual(PR.problem(text, 200), "")

    def test_markup_around_keys_and_list_marker_before_n(self):
        text = "a (как есть): 5 мин, 1 ГБ\nb: 1 мин, 2 ГБ\n**Тяжёлый:** x · **пик 4 ГБ**\n- **N:** 6\n**Оценка:** 90 мин"
        self.assertEqual(PR.problem(text, 200), "")
        self.assertEqual(PR.procs("1) N: 3"), 3)
        self.assertEqual(PR.procs("* N = 8"), 8)

    def test_deadline_is_checked_against_remaining_estimate(self):
        self.assertIn("не короче", PR.problem(GOOD, 80))
        self.assertEqual(PR.problem(GOOD, 80, elapsed_min=20), "")  # 20 мин уже прошло: остаток оценки 80


class SettingAndPathTests(unittest.TestCase):
    def test_garbage_threshold_falls_back(self):
        for raw in ("много", None, [], "1.5.2"):
            with self.subTest(raw=raw), mock.patch.object(P, "settings", lambda root=None, r=raw: {"probe_threshold_min": r}):
                self.assertEqual(PR.threshold_min(), PR.THRESHOLD_MIN)

    def test_relative_probe_file_is_read_from_root_and_from_copy(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            (Path(a) / "p.txt").write_text("из корня", encoding="utf-8")
            (Path(b) / "q.txt").write_text("из копии", encoding="utf-8")
            old = os.getcwd()
            os.chdir(b)
            try:
                self.assertEqual(PR.load("p.txt", a), ("из корня", "p.txt"))
                self.assertEqual(PR.load("q.txt", a), ("из копии", "q.txt"))
            finally:
                os.chdir(old)

    def test_flag_off_writes_probe_to_log_without_checks(self):
        with mock.patch.object(P, "settings", lambda root=None: {}):
            err, upd, log = PR.gate(SimpleNamespace(header={}), SPEC, NOW + timedelta(minutes=600), "любая запись", ".", now=NOW)
        self.assertEqual((err, upd), ("", {}))
        self.assertEqual(log, "[проба] любая запись")

    def test_slice_then_probe_judged_by_remaining_estimate(self):
        with mock.patch.object(P, "settings", lambda root=None: {"require_probe": True}):
            tkt = SimpleNamespace(header={"probe_waited": "20.0"})
            err, _, _ = PR.gate(tkt, SPEC, NOW + timedelta(minutes=85), GOOD, ".", now=NOW)  # 85 + 20 ≥ 100
            self.assertEqual(err, "")
            err, _, _ = PR.gate(tkt, SPEC, NOW + timedelta(minutes=50), GOOD, ".", now=NOW)  # 50 + 20 < 100
            self.assertIn("не короче", err)


class TemplateTests(unittest.TestCase):
    def test_probe_lines_in_role_templates_are_conditional(self):
        for role in ("engineer", "researcher", "judge"):
            text = (PLUGIN / "templates" / "roles" / f"{role}.md").read_text(encoding="utf-8")
            with self.subTest(role=role):
                self.assertIn("require_probe", text)


class HostloadTests(unittest.TestCase):
    def test_new_peak_must_be_smaller_not_just_different(self):
        mark = {"peak": 5.0, "n": 8}
        self.assertEqual(HL._oom_problem(mark, (4.0, 8)), "")
        self.assertEqual(HL._oom_problem(mark, (5.0, 4)), "")
        self.assertNotEqual(HL._oom_problem(mark, (6.0, 8)), "")  # пик вырос — не «другой», а хуже
        self.assertNotEqual(HL._oom_problem(mark, (5.0, 8)), "")

    def test_oom_forms(self):
        for yes in ("Out of memory: Killed process 4242 (python)", "oom-kill: constraint=CONSTRAINT_NONE", "Result: oom-kill",
                    "exit code 137", "rc=137"):
            with self.subTest(yes=yes):
                self.assertTrue(HL.is_oom(yes))
        for no in ("oom_kill 0", "oom_kill 0\n", "не было oom", "exit code 1370"):
            with self.subTest(no=no):
                self.assertFalse(HL.is_oom(no))

    def test_host_free_cmd_must_contain_ticket(self):
        self.assertFalse(rpv_config.KEYS["host_free_cmd"][0]("ssh x free"))
        self.assertTrue(rpv_config.KEYS["host_free_cmd"][0]("ssh x 'cat /tmp/{ticket}.free'"))


class DispatchTests(unittest.TestCase):
    def test_sonnet_critic_in_judge_run_is_not_a_model_alarm(self):
        usage = {"claude-opus-5-5": {}, "claude-sonnet-5-5": {}}
        self.assertIsNotNone(D._model_usage_warning(usage, "opus"))
        self.assertIsNone(D._model_usage_warning(usage, "opus", also=("sonnet",)))
        self.assertIsNotNone(D._model_usage_warning({"claude-haiku-5-5": {}}, "opus", also=("sonnet",)))
        self.assertEqual(D._extra_models({"role": "judge"}, critic_on=True), ("sonnet",))
        self.assertEqual(D._extra_models({"role": "judge"}, critic_on=False), ())
        self.assertEqual(D._extra_models({"role": "engineer"}, critic_on=True), ())


if __name__ == "__main__":
    unittest.main()
