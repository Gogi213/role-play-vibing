"""TK-070 (G-009): пик памяти и N в записи пробы, проверка свободных ресурсов в `wait`, отказ повтора после OOM-kill."""
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
import hostload as HL  # noqa: E402
import probe as PR  # noqa: E402
import project as P  # noqa: E402
import project_paths as CFG  # noqa: E402
import rpv_config  # noqa: E402
import ticket as T  # noqa: E402
import tickets as TK  # noqa: E402

NOW = datetime(2026, 10, 31, 12, 0, tzinfo=timezone(timedelta(hours=4)))
BASE = """Способ 1 (как есть): 4 мин, пик 900 МБ на срезе 1000 строк
Способ 2 (векторно): 0,2 мин, пик 1,5 ГБ на срезе 1000 строк
{heavy}
{procs}
Оценка: 100 мин"""
GOOD = BASE.format(heavy="Тяжёлый: инструмент с длинной историей · пик 5 ГБ", procs="N: 4")


def record(peak="5 ГБ", n="4"):
    return BASE.format(heavy=f"Тяжёлый: инструмент · пик {peak}", procs=f"N: {n}")


def in_min(n: int) -> str:
    return (datetime.now().astimezone() + timedelta(minutes=n)).isoformat(timespec="seconds")


class RecordFormTests(unittest.TestCase):
    def test_peak_and_n_are_read(self):
        self.assertEqual(PR.problem(GOOD, 120), "")
        self.assertEqual(PR.peak_gb(GOOD), 5.0)
        self.assertEqual(PR.procs(GOOD), 4)
        self.assertEqual(PR.peak_gb("тяжёлый: x · пик 512 МБ"), 0.5)
        self.assertEqual(PR.peak_gb("Тяжёлый: x · пик 1,5 ГБ"), 1.5)

    def test_heavy_line_without_peak_refused(self):
        text = BASE.format(heavy="Тяжёлый: инструмент без цифр", procs="N: 4")
        self.assertIn("нет пика памяти", PR.problem(text, 120))
        self.assertIsNone(PR.peak_gb(text))

    def test_peak_must_be_on_heavy_line_not_on_method_lines(self):
        self.assertIsNone(PR.peak_gb("Способ: 4 мин, пик 900 МБ\nТяжёлый: инструмент"))

    def test_no_n_line_or_zero_refused(self):
        self.assertIn("«N:", PR.problem(BASE.format(heavy="Тяжёлый: x · пик 5 ГБ", procs=""), 120))
        self.assertIn("«N:", PR.problem(record(n="0"), 120))


class MathTests(unittest.TestCase):
    def test_n_max_is_min_of_cores_and_memory(self):
        self.assertEqual(HL.n_max(24, 40, 5.5, 1.2), 6)   # память: 40 / 6,6
        self.assertEqual(HL.n_max(4, 400, 5.5, 1.2), 4)   # ядра
        self.assertEqual(HL.n_max(8, 1, 5.5, 1.2), 0)

    def test_free_parses_first_line_two_numbers(self):
        run = mock.Mock(return_value=mock.Mock(returncode=0, stdout="16 24,5\nлишнее".encode()))
        with mock.patch.object(P, "setting", lambda k, d=None: "freecheck {ticket}" if k == "host_free_cmd" else d), \
                mock.patch.object(HL.hide, "run", run):
            self.assertEqual(HL.free("gpu1", "TK-9", lambda a, c: ["ssh", a, c]), (16, 24.5))
        self.assertIn("TK-9", run.call_args[0][0][2])

    def test_free_bad_output_is_none(self):
        for out, rc in ((b"", 0), ("нет данных".encode(), 0), (b"16", 0), (b"16 24", 255)):
            run = mock.Mock(return_value=mock.Mock(returncode=rc, stdout=out))
            with mock.patch.object(P, "setting", lambda k, d=None: "x" if k == "host_free_cmd" else d), mock.patch.object(HL.hide, "run", run):
                self.assertIsNone(HL.free("gpu1", "TK-9", lambda a, c: ["ssh"]))


class WaitLoadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        (base / "tickets").mkdir()
        self._orig = (CFG.TICKETS_DIR, CFG.STATE_FILE, CFG.DISPATCHER_DIR, TK.TICKETS_DIR, TK.PROJECT_ROOT)
        CFG.TICKETS_DIR, CFG.STATE_FILE, CFG.DISPATCHER_DIR = base / "tickets", base / "state.json", base
        TK.TICKETS_DIR, TK.PROJECT_ROOT = base / "tickets", base
        self.base = base
        self.path = T.create_ticket(CFG.TICKETS_DIR, owner="engineer", title="Т", status="todo", now=NOW)
        T.write_header_updates(self.path, {"status": "in_progress", "reviewer": "judge"}, now=NOW)
        self.tid = self.path.stem
        self.settings = {"require_probe": True, "host_free_cmd": "freecheck"}
        self.free = (24, 40.0)
        for target, repl in ((P, mock.patch.object(P, "settings", lambda root=None: self.settings)),
                             (P, mock.patch.object(P, "setting", lambda k, d=None: self.settings.get(k, d))),
                             (HL, mock.patch.object(HL, "free", lambda alias, tid, ssh: self.free))):
            repl.start()
            self.addCleanup(repl.stop)
        self._role = os.environ.get("RPV_ROLE")
        os.environ["RPV_ROLE"] = "engineer"

    def tearDown(self):
        CFG.TICKETS_DIR, CFG.STATE_FILE, CFG.DISPATCHER_DIR, TK.TICKETS_DIR, TK.PROJECT_ROOT = self._orig
        if self._role is None:
            os.environ.pop("RPV_ROLE", None)
        else:
            os.environ["RPV_ROLE"] = self._role
        self.tmp.cleanup()

    def wait(self, probe=None, spec="host:gpu1:/tmp/job.rc", by_min=130):
        a = dict(id=self.tid, spec=spec, on_met=None, by=in_min(by_min), probe=probe)
        err = mock.MagicMock()
        with mock.patch("sys.stderr", err):
            rc = TK.cmd_wait(type("A", (), a)())
        self.err = "".join(c.args[0] for c in err.write.call_args_list)
        return rc

    def tkt(self):
        return T.read_ticket(self.path)

    def log(self):
        return self.path.read_text(encoding="utf-8")

    def test_fits_machine_is_accepted_and_load_written_to_header(self):
        self.assertEqual(self.wait(record(n="4")), 0)
        self.assertEqual(self.tkt().header["probe_load"], "пик 5.00 ГБ · N 4")
        self.assertIn("N_max = 6", self.log())
        self.assertIn("можно больше", self.log())  # N=4 < N_max=6 — недогруз виден

    def test_too_many_for_memory_refused_with_n_max(self):
        self.assertEqual(self.wait(record(n="10")), 1)
        self.assertIn("N_max = 6", self.err)
        self.assertEqual(self.tkt().status, "in_progress")  # отказ ничего не пишет

    def test_too_many_for_cores_refused(self):
        self.free = (3, 400.0)
        self.assertEqual(self.wait(record(n="4")), 1)
        self.assertIn("N_max = 3", self.err)

    def test_exactly_n_max_passes(self):
        self.assertEqual(self.wait(record(n="6")), 0)
        self.assertNotIn("можно больше", self.log())

    def test_reserve_key_moves_the_limit(self):
        self.settings["host_mem_reserve"] = 2.0
        self.assertEqual(self.wait(record(n="5")), 1)  # 5 × 5 × 2 = 50 > 40
        self.assertIn("N_max = 4", self.err)

    def test_no_host_free_cmd_only_form(self):
        del self.settings["host_free_cmd"]
        self.assertEqual(self.wait(record(n="99")), 0)

    def test_free_cmd_silent_passes_with_note(self):
        self.free = None
        self.assertEqual(self.wait(record(n="99")), 0)
        self.assertIn("не проверены", self.log())

    def test_flag_off_nothing_changes(self):
        self.settings["require_probe"] = False
        self.assertEqual(self.wait(None), 0)
        self.assertNotIn("probe_load", self.tkt().header)

    def test_file_wait_is_not_checked_against_machine(self):
        rc = self.wait(record(n="99"), spec="file:out/x.done")
        self.assertEqual(rc, 0, self.err)

    def test_second_wait_uses_header_load(self):
        self.assertEqual(self.wait(record(n="4")), 0)
        T.write_header_updates(self.path, {"status": "in_progress", "wait_for": ""})
        self.free = (2, 40.0)  # машина стала теснее — по записи из шапки теперь не помещается
        self.assertEqual(self.wait(None), 1)
        self.assertIn("N_max = 2", self.err)

    def test_oom_after_failure_needs_new_peak_or_smaller_n(self):
        self.assertEqual(self.wait(record(n="4")), 0)
        T.write_header_updates(self.path, {"status": "in_progress", "wait_for": ""})
        self.assertTrue(HL.note_oom(self.tkt(), "сторож: задание упало [rc 137: oom-kill в юните]", NOW))
        self.assertEqual(HL.oom_mark(self.tid)["peak"], 5.0)
        self.assertEqual(self.wait(None), 1)  # без записи
        self.assertIn("OOM", self.err)
        self.assertEqual(self.wait(record(n="4")), 1)  # тот же пик и тот же N
        self.assertIn("новый пик", self.err)
        self.assertEqual(self.wait(record(n="5")), 1)  # N больше
        self.assertEqual(self.wait(record(n="3")), 0)  # меньшее N
        self.assertIsNone(HL.oom_mark(self.tid))  # отметка снята

    def test_oom_new_peak_with_same_n_passes(self):
        self.assertEqual(self.wait(record(n="2")), 0)
        T.write_header_updates(self.path, {"status": "in_progress", "wait_for": ""})
        HL.note_oom(self.tkt(), "упало: out of memory", NOW)
        self.assertEqual(self.wait(record(peak="7 ГБ", n="2")), 0)
        self.assertEqual(self.tkt().header["probe_load"], "пик 7.00 ГБ · N 2")

    def test_oom_mark_only_for_oom_text_and_flag(self):
        self.assertFalse(HL.note_oom(self.tkt(), "сторож: задание упало [rc 1: ошибка данных]", NOW))
        self.settings["require_probe"] = False
        self.assertFalse(HL.note_oom(self.tkt(), "rc 137: oom-kill", NOW))
        self.assertIsNone(HL.oom_mark(self.tid))

    def test_oom_does_not_touch_other_tickets(self):
        HL.note_oom(self.tkt(), "oom-kill", NOW)
        self.assertIsNone(HL.oom_mark("TK-999"))

    def test_watch_wake_records_oom(self):
        import watch
        with mock.patch.object(watch, "_to_judge"):
            watch._wake_owner_job(self.path, self.tkt(), {}, "job:gpu1:7", "сторож: задание упало [rc 137: oom-kill]", NOW)
        self.assertIsNotNone(HL.oom_mark(self.tid))


class ConfigTests(unittest.TestCase):
    def test_keys_known(self):
        self.assertEqual({"host_free_cmd", "host_mem_reserve"} - set(rpv_config.KEYS), set())
        ok, bad = rpv_config.KEYS["host_mem_reserve"][0], rpv_config.KEYS["host_mem_reserve"][0]
        self.assertTrue(ok(1.2) and ok(2))
        self.assertFalse(bad(0.5) or bad(True) or bad("1.2"))
        self.assertTrue(rpv_config.KEYS["host_free_cmd"][0]("freecheck"))

    def test_docs_describe_keys(self):
        doc = (Path(__file__).resolve().parents[2] / "docs" / "configuration.md").read_text(encoding="utf-8")
        for must in ("`host_free_cmd`", "`host_mem_reserve`", "N_max", "OOM-kill"):
            self.assertIn(must, doc)


if __name__ == "__main__":
    unittest.main()
