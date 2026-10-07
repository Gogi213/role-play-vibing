"""Короткий прогон на выносливость (TK-076 п.2): по раунду на сбой — 0 потерь, 0 blocked, простой в пределах."""
import sys
import tempfile
import unittest
from pathlib import Path

import endurance


class EnduranceShort(unittest.TestCase):
    def test_base_round(self):
        self.assertEqual(endurance.main(["--modes", "base", "--idle-max", "30"]), 0)

    def test_bus_round(self):
        self.assertEqual(endurance.main(["--modes", "bus", "--idle-max", "30"]), 0)

    def test_reboot_round(self):
        self.assertEqual(endurance.main(["--modes", "reboot", "--idle-max", "30"]), 0)

    def test_watch_round(self):
        self.assertEqual(endurance.main(["--modes", "watch", "--idle-max", "30"]), 0)

    def test_inbox_pages_past_100_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            h = endurance.Harness(Path(tmp), 30, 1)
            h.start_bus()
            try:
                sys.path.insert(0, str(endurance.HERE.parent / "bus"))
                import busclient
                for i in range(130):
                    busclient.post(f"задача.TK-{i}.к_ceo", {"kind": "done"}, f"pg-{i}")
                self.assertIn("TK-129 [done]", h.inbox())
            finally:
                h.kill_bus()
                h.log_fh.close()


if __name__ == "__main__":
    unittest.main()
