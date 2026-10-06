"""Короткий прогон на выносливость (TK-076 п.2): один раунд — сбои, 0 потерь, 0 blocked, простой в пределах."""
import unittest

import endurance


class EnduranceShort(unittest.TestCase):
    def test_one_round_meets_criterion(self):
        self.assertEqual(endurance.main(["--rounds", "1", "--idle-max", "30"]), 0)


if __name__ == "__main__":
    unittest.main()
