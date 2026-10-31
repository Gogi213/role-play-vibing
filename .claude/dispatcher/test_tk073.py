"""TK-073 (В-31, разбор критика): порог пробы по сумме ожиданий тикета (нарезка коротким --by не обходит); ворота критика —
вердикт без учёта регистра, повторный номер берёт худший вердикт."""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import critic_gate as CG  # noqa: E402
import probe as PR  # noqa: E402
import project as P  # noqa: E402
from test_tk069 import GOOD  # noqa: E402

NOW = datetime(2026, 10, 31, 12, 0, tzinfo=timezone(timedelta(hours=4)))
SPEC = "host:gpu1:/tmp/job.rc"


class SlicedWaitTests(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.object(P, "settings", lambda root=None: {"require_probe": True})
        patch.start()
        self.addCleanup(patch.stop)
        self.header: dict = {}

    def wait(self, minutes, probe=None):
        """Один `wait` с --by через `minutes`; принятые поля шапки применяются, как это делает cmd_wait."""
        err, upd, log = PR.gate(SimpleNamespace(header=self.header), SPEC, NOW + timedelta(minutes=minutes), probe, ".", now=NOW)
        if not err:
            self.header.update(upd)
        return err, log

    def test_short_waits_add_up_and_the_sum_needs_a_probe(self):
        self.assertEqual(self.wait(10)[0], "")  # 10 ≤ 15
        self.assertEqual(self.header.get("probe_waited"), "10.0")
        err, _ = self.wait(10)  # 10 + 10 = 20 > 15
        self.assertIn("20 мин", err)
        self.assertIn("прежние без пробы 10", err)
        self.assertEqual(self.header.get("probe_waited"), "10.0")  # отказ счёт не меняет

    def test_many_slices_of_a_long_run_are_caught(self):
        refused_at = next((i for i in range(1, 10) if self.wait(5)[0]), None)
        self.assertEqual(refused_at, 4)  # 5+5+5 = 15 проходит, четвёртый 5 → 20

    def test_one_short_wait_still_passes_without_probe(self):
        self.assertEqual(self.wait(15)[0], "")
        self.assertEqual(self.wait(1)[0] != "", True)  # 15 + 1 > 15

    def test_probe_after_slices_is_accepted_and_clears_the_counter(self):
        self.wait(10)
        err, log = self.wait(130, GOOD)
        self.assertEqual(err, "")
        self.assertIn("оценка 100 мин", self.header["probe"])
        self.assertEqual(self.header["probe_waited"], "")
        self.assertEqual(self.wait(600)[0], "")  # проба в шапке — дальше без счёта

    def test_probe_header_stops_accumulating(self):
        self.header = {"probe": "в логе · оценка 100 мин"}
        self.assertEqual(self.wait(10)[0], "")
        self.assertNotIn("probe_waited", self.header)

    def test_flag_off_counts_nothing(self):
        with mock.patch.object(P, "settings", lambda root=None: {}):
            self.assertEqual(self.wait(600)[0], "")
        self.assertNotIn("probe_waited", self.header)

    def test_garbage_counter_counts_as_zero(self):
        for raw in ("", "много", "-5"):
            with self.subTest(raw=raw):
                self.assertEqual(PR.waited_min(SimpleNamespace(header={"probe_waited": raw})), 0.0)


class CriticParseTests(unittest.TestCase):
    def test_capitalised_verdict_is_read(self):
        items, _ = CG.parse_report("1. «а» — **Держится**\n2. «б» — **Не держится**\n3. «в» — **НЕ ХВАТАЕТ**\n")
        self.assertEqual(items, {"1": "держится", "2": "не держится", "3": "не хватает"})

    def test_capitalised_not_held_still_needs_an_answer(self):
        items, answered = CG.parse_report("1. «а» — **Не держится**\n")
        self.assertEqual([n for n, v in items.items() if v == "не держится" and n not in answered], ["1"])

    def test_repeated_number_keeps_the_worst_verdict(self):
        items, _ = CG.parse_report("1. «а» — **не держится**\n\n1. «а, ещё раз» — **держится**\n")
        self.assertEqual(items, {"1": "не держится"})
        items, _ = CG.parse_report("2. «б» — **держится**\n2. «б» — **не хватает**\n")
        self.assertEqual(items, {"2": "не хватает"})

    def test_plain_report_unchanged(self):
        items, answered = CG.parse_report("1. «а» — **держится**\n2. «б» — **не держится**\nОтвет Судьи на 2: принял правку\n")
        self.assertEqual((items, answered), ({"1": "держится", "2": "не держится"}, {"2"}))


if __name__ == "__main__":
    unittest.main()
