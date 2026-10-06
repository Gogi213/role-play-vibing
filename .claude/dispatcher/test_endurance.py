"""Короткий прогон на выносливость (TK-076 п.2): по раунду на сбой — 0 потерь, 0 blocked, простой в пределах."""
import unittest

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


if __name__ == "__main__":
    unittest.main()
