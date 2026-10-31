"""TK-081 (В-32, PR #189 п.6): проба нужна ожиданиям из `probe_kinds` (умолчание host, job), file: — нет; порог по сумме одного
расчёта — после [итог: done|accept] счёт заново; короткий --by для host:/job: порог по-прежнему не обходит."""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import probe as PR  # noqa: E402
import project as P  # noqa: E402
import rpv_config  # noqa: E402
import ticket as T  # noqa: E402
from test_tk069 import GOOD  # noqa: E402

NOW = datetime(2026, 10, 31, 12, 0, tzinfo=timezone(timedelta(hours=4)))
HOST, JOB, FILE = "host:gpu1:/tmp/job.rc", "job:gpu1:run7", "file:/tmp/result.csv"


class KindsTests(unittest.TestCase):
    def setUp(self):
        self.settings = {"require_probe": True}
        patch = mock.patch.object(P, "settings", lambda root=None: self.settings)
        patch.start()
        self.addCleanup(patch.stop)
        self.header: dict = {}

    def wait(self, spec, minutes, probe=None):
        err, upd, log = PR.gate(SimpleNamespace(header=self.header), spec, NOW + timedelta(minutes=minutes), probe, ".", now=NOW)
        if not err:
            self.header.update(upd)
        return err, log

    def test_file_wait_longer_than_threshold_passes_without_probe(self):
        self.assertEqual(self.wait(FILE, 600), ("", ""))
        self.assertEqual(self.header, {})  # file: в сумму не входит

    def test_host_and_job_over_threshold_need_probe(self):
        for spec in (HOST, JOB):
            with self.subTest(spec=spec):
                self.header.clear()
                self.assertIn("без пробы", self.wait(spec, 600)[0])

    def test_fractional_host_waits_add_up_over_threshold(self):
        self.assertEqual(self.wait(HOST, 7.5)[0], "")
        self.assertEqual(self.wait(HOST, 7.5)[0], "")  # 15.0 — ещё порог
        self.assertIn("без пробы", self.wait(HOST, 0.5)[0])  # 15.5 > 15 — нужна проба

    def test_short_waits_split_between_host_and_job_still_add_up(self):
        self.assertEqual(self.wait(HOST, 10)[0], "")
        self.assertNotEqual(self.wait(JOB, 10)[0], "")  # 10 + 10 > 15: короткий --by порог не обходит

    def test_probe_accepted_for_host_after_file_waits(self):
        self.wait(FILE, 600)
        self.assertEqual(self.wait(HOST, 130, GOOD)[0], "")
        self.assertIn("оценка 100 мин", self.header["probe"])

    def test_voluntary_probe_on_file_wait_goes_to_log_unchecked(self):
        err, log = self.wait(FILE, 600, "любая запись")
        self.assertEqual((err, log), ("", "[проба] любая запись"))
        self.assertNotIn("probe", self.header)

    def test_probe_kinds_setting(self):
        self.settings["probe_kinds"] = ["host", "job", "file"]
        self.assertIn("без пробы", self.wait(FILE, 600)[0])
        self.settings["probe_kinds"] = ["file"]
        self.assertEqual(self.wait(HOST, 600)[0], "")
        self.assertEqual(PR.kinds(), ("file",))

    def test_probe_kinds_garbage_falls_back_to_default(self):
        for raw in (None, [], "", 5, ["at"], ["ticket"], {"host": 1}):
            with self.subTest(raw=raw):
                self.settings["probe_kinds"] = raw
                self.assertEqual(PR.kinds(), PR.DEFAULT_KINDS)
        self.settings["probe_kinds"] = "host, file"
        self.assertEqual(PR.kinds(), ("host", "file"))

    def test_config_accepts_only_known_nonempty_kinds(self):
        check = rpv_config.KEYS["probe_kinds"][0]
        self.assertTrue(check(["host"]) and check(["host", "job", "file"]))
        for bad in ([], "host", ["at"], ["host", "ticket"], [1], None):
            with self.subTest(bad=bad):
                self.assertFalse(check(bad))


class ResetAfterDoneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = T.create_ticket(Path(self.tmp.name), owner="engineer", title="Т", status="in_progress", now=NOW - timedelta(hours=2))
        patch = mock.patch.object(P, "settings", lambda root=None: {"require_probe": True})
        patch.start()
        self.addCleanup(patch.stop)

    def wait(self, minutes, spec=HOST):
        tkt = T.read_ticket(self.path)
        err, upd, _ = PR.gate(tkt, spec, NOW + timedelta(minutes=minutes), None, ".", now=NOW)
        if not err:
            T.write_header_updates(self.path, upd, now=NOW)
        return err

    def log(self, text, minutes_from_now):
        T.append_log(self.path, "engineer", text, now=NOW + timedelta(minutes=minutes_from_now))

    def waited(self):
        return PR.waited_min(T.read_ticket(self.path))

    def test_sum_survives_unrelated_log_entries(self):
        self.assertEqual(self.wait(10), "")
        for text in ("[итог: wait] ждём — x", "[итог: continue] — x", "[итог: pr] PR #1@abc — x", "заметка [итог: done]"):
            self.log(text, 5)
        self.assertEqual(self.waited(), 10.0)
        self.assertNotEqual(self.wait(10), "")  # 10 + 10 > 15

    def test_done_after_the_sum_starts_a_new_calculation(self):
        self.assertEqual(self.wait(10), "")
        self.log("[итог: done] результат: x — расчёт готов", 30)
        self.assertEqual(self.waited(), 0.0)
        self.assertEqual(self.wait(10), "")  # новый расчёт: 10, а не 20

    def test_accept_after_the_sum_also_resets(self):
        self.assertEqual(self.wait(10), "")
        self.log("[итог: accept] PR #5@abc1234 — принято", 30)
        self.assertEqual(self.waited(), 0.0)

    def test_done_before_the_sum_does_not_reset_it(self):
        self.log("[итог: done] результат: x — прежний расчёт", -60)
        self.assertEqual(self.wait(10), "")
        self.assertEqual(self.waited(), 10.0)
        self.assertNotEqual(self.wait(10), "")

    def test_sum_without_timestamp_is_kept_as_before(self):
        self.assertEqual(PR.waited_min(SimpleNamespace(header={"probe_waited": "12.0"}, log=[])), 12.0)
        self.assertEqual(PR.waited_min(SimpleNamespace(header={"probe_waited": "12.0", "probe_waited_at": "когда-то"})), 12.0)

    def test_accepted_probe_clears_sum_and_stamp(self):
        self.assertEqual(self.wait(10), "")
        tkt = T.read_ticket(self.path)
        err, upd, _ = PR.gate(tkt, HOST, NOW + timedelta(minutes=130), GOOD, ".", now=NOW)
        self.assertEqual((err, upd["probe_waited"], upd["probe_waited_at"]), ("", "", ""))


if __name__ == "__main__":
    unittest.main()
