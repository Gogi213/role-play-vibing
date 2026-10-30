"""TK-069 (G-008): проба до долгого расчёта — `wait --probe`, форма записи, один раз на тикет, строки в уставах."""
from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

_SANDBOX = tempfile.mkdtemp(prefix="rpv-test-proj-")
os.makedirs(os.path.join(_SANDBOX, ".claude", "roles"))
os.environ["CLAUDE_PROJECT_DIR"] = _SANDBOX
atexit.register(shutil.rmtree, _SANDBOX, True)
sys.path.insert(0, str(Path(__file__).resolve().parent))
import project as P  # noqa: E402
import project_paths as CFG  # noqa: E402
import probe as PR  # noqa: E402
import rpv_config  # noqa: E402
import ticket as T  # noqa: E402
import tickets as TK  # noqa: E402

PLUGIN = Path(__file__).resolve().parents[2]
ROLES = PLUGIN / "templates" / "roles"
NOW = datetime(2026, 10, 31, 12, 0, tzinfo=timezone(timedelta(hours=4)))
GOOD = """Способ 1 (как есть): 4 мин, пик 900 МБ на срезе 1000 строк
Способ 2 (векторно): 0,2 мин, пик 1,5 ГБ на срезе 1000 строк
Тяжёлый: инструмент с самой длинной историей
Оценка: 100 мин"""


def in_min(n: int) -> str:
    return (datetime.now().astimezone() + timedelta(minutes=n)).isoformat(timespec="seconds")


class ProbeFormTests(unittest.TestCase):
    def test_good_record_passes_and_estimate_is_read(self):
        self.assertEqual(PR.problem(GOOD, 120), "")
        self.assertEqual(PR.estimate_min(GOOD), 100)
        self.assertEqual(PR.estimate_min("оценка: 5,5 ч"), 330)

    def test_full_russian_unit_words_count(self):
        text = GOOD.replace("4 мин, пик 900 МБ", "12 минут, пик 300 МБ").replace("0,2 мин", "40 секунд")
        self.assertEqual(PR.problem(text, 120), "")
        self.assertEqual(PR.estimate_min("оценка: 90 минут"), 90)
        self.assertEqual(PR.estimate_min("Оценка: 2 часа"), 120)

    def test_one_method_refused(self):
        text = GOOD.replace("Способ 2 (векторно): 0,2 мин, пик 1,5 ГБ на срезе 1000 строк\n", "")
        self.assertIn("нужно ≥ 2", PR.problem(text, 120))

    def test_method_without_number_and_unit_does_not_count(self):
        text = GOOD.replace("0,2 мин, пик 1,5 ГБ", "быстро, памяти немного")
        self.assertIn("нужно ≥ 2", PR.problem(text, 120))
        self.assertIn("нужно ≥ 2", PR.problem(GOOD.replace("пик 1,5 ГБ", "пик 1,5"), 120))  # единицы памяти нет

    def test_no_as_is_method_refused(self):
        self.assertIn("как есть", PR.problem(GOOD.replace("(как есть)", "(базовый)"), 120))

    def test_no_heavy_item_refused(self):
        self.assertIn("тяжёлый", PR.problem(GOOD.replace("Тяжёлый: инструмент с самой длинной историей\n", ""), 120))

    def test_no_estimate_or_unit_refused(self):
        self.assertIn("оценка", PR.problem(GOOD.replace("Оценка: 100 мин", ""), 120))
        self.assertIn("оценка", PR.problem(GOOD.replace("Оценка: 100 мин", "Оценка: долго"), 120))
        self.assertIn("оценка", PR.problem(GOOD.replace("Оценка: 100 мин", "Оценка: 100"), 120))

    def test_deadline_shorter_than_estimate_refused(self):
        self.assertIn("не короче оценки", PR.problem(GOOD, 99))
        self.assertEqual(PR.problem(GOOD, 100), "")


class WaitProbeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        (base / "tickets").mkdir()
        self._orig = (CFG.TICKETS_DIR, CFG.STATE_FILE, TK.TICKETS_DIR, TK.PROJECT_ROOT)
        CFG.TICKETS_DIR, CFG.STATE_FILE, TK.TICKETS_DIR, TK.PROJECT_ROOT = base / "tickets", base / "state.json", base / "tickets", base
        self.base = base
        self.path = T.create_ticket(CFG.TICKETS_DIR, owner="engineer", title="Т", status="todo", now=NOW)
        T.write_header_updates(self.path, {"status": "in_progress", "reviewer": "judge"}, now=NOW)
        self.tid = self.path.stem
        self.settings = {"require_probe": True}
        patch = mock.patch.object(P, "settings", lambda root=None: self.settings)
        patch.start()
        self.addCleanup(patch.stop)
        self._role = os.environ.get("RPV_ROLE")
        os.environ["RPV_ROLE"] = "engineer"

    def tearDown(self):
        CFG.TICKETS_DIR, CFG.STATE_FILE, TK.TICKETS_DIR, TK.PROJECT_ROOT = self._orig
        if self._role is None:
            os.environ.pop("RPV_ROLE", None)
        else:
            os.environ["RPV_ROLE"] = self._role
        self.tmp.cleanup()

    def wait(self, spec="host:gpu1:/tmp/job.rc", by_min=60, **kw):
        a = dict(id=self.tid, spec=spec, on_met=None, by=in_min(by_min), probe=None)
        a.update(kw)
        with mock.patch("sys.stderr"):
            return TK.cmd_wait(type("A", (), a)())

    def tkt(self):
        return T.read_ticket(self.path)

    def test_flag_off_wait_without_probe_passes_as_before(self):
        self.settings = {}
        self.assertEqual(self.wait(by_min=600), 0)
        self.assertEqual((self.tkt().status, self.tkt().header.get("probe")), ("waiting", None))

    def test_deadline_within_threshold_needs_no_probe(self):
        self.assertEqual(self.wait(by_min=10), 0)

    def test_threshold_key_moves_the_limit(self):
        self.settings = {"require_probe": True, "probe_threshold_min": 90}
        self.assertEqual(self.wait(by_min=60), 0)
        self.assertEqual(self.wait(by_min=120), 1)

    def test_long_wait_without_probe_refused_and_writes_nothing(self):
        self.assertEqual(self.wait(by_min=60), 1)
        t = self.tkt()
        self.assertEqual((t.status, t.header.get("wait_for") or ""), ("in_progress", ""))

    def test_wait_forms_without_compute_need_no_probe(self):
        self.assertEqual(self.wait(spec="at:" + in_min(300), by_min=300), 0)

    def test_good_inline_probe_accepted_and_written_once(self):
        self.assertEqual(self.wait(by_min=130, probe=GOOD), 0)
        t = self.tkt()
        self.assertEqual(t.status, "waiting")
        self.assertIn("оценка 100 мин", t.header["probe"])
        self.assertIn("[проба] " + GOOD.splitlines()[0], t.path.read_text(encoding="utf-8"))
        T.write_header_updates(self.path, {"status": "in_progress", "wait_for": ""})
        self.assertEqual(self.wait(spec="host:gpu1:/tmp/second.rc", by_min=600), 0)  # дальше — из шапки

    def test_probe_from_file(self):
        f = self.base / "probe.md"
        f.write_text(GOOD, encoding="utf-8")
        self.assertEqual(self.wait(by_min=130, probe=str(f)), 0)
        self.assertIn("файл " + str(f), self.tkt().header["probe"])
        self.assertIn("[проба] файл " + str(f) + " · оценка 100 мин", self.tkt().path.read_text(encoding="utf-8"))

    def test_bad_probe_refused_with_form_hint(self):
        self.assertEqual(self.wait(by_min=130, probe="оценка: 10 мин"), 1)
        self.assertEqual(self.tkt().status, "in_progress")
        self.assertIsNone(self.tkt().header.get("probe"))

    def test_deadline_shorter_than_estimate_refused(self):
        self.assertEqual(self.wait(by_min=60, probe=GOOD), 1)
        self.assertEqual(self.wait(by_min=130, probe=GOOD), 0)

    def test_result_wait_passes_probe_through(self):
        a = dict(id=self.tid, result="wait", why="жду", pr=None, sha=None, path=None, form="host:gpu1:/tmp/job.rc",
                 tail="-", by=in_min(130), probe=GOOD)
        with mock.patch("sys.stderr"):
            self.assertEqual(TK.cmd_result(type("A", (), a)()), 0)
        self.assertIn("оценка 100 мин", self.tkt().header["probe"])
        a["probe"], a["form"] = None, "host:gpu1:/tmp/other.rc"
        T.write_header_updates(self.path, {"status": "in_progress", "probe": ""})
        with mock.patch("sys.stderr"):
            self.assertEqual(TK.cmd_result(type("A", (), a)()), 1)

    def test_keys_are_known_to_config(self):
        self.assertEqual(set(("require_probe", "probe_threshold_min")) - set(rpv_config.KEYS), set())
        self.assertTrue(rpv_config.KEYS["require_probe"][0](True))
        self.assertFalse(rpv_config.KEYS["probe_threshold_min"][0](0))


class CharterTests(unittest.TestCase):
    def text(self, name):
        return " ".join((ROLES / name).read_text(encoding="utf-8").split())

    def test_judge_protocol_and_actual_vs_estimate(self):
        t = self.text("judge.md")
        for must in ("проба и оценка", "≥ 2 способа (один «как есть»)", "пик памяти", "оценка полного времени",
                     "«оценка / факт»", "Факт больше 2× оценки без объяснения"):
            self.assertIn(must, t)

    def test_researcher_and_engineer_have_probe_line(self):
        for name in ("researcher.md", "engineer.md"):
            t = self.text(name)
            for must in ("Проба до долгого", "probe_threshold_min", "≥ 2 способа (один «как есть»)", "`wait --probe <запись|файл>`"):
                self.assertIn(must, t, name)

    def test_no_project_words(self):
        for name in ("judge.md", "researcher.md", "engineer.md"):
            t = self.text(name).lower()
            for word in ("alpha", "бектест", "backtest", "tk-461"):
                self.assertNotIn(word, t, name)


if __name__ == "__main__":
    unittest.main()
