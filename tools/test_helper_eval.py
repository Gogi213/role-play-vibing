"""Определение помощника и команда запуска в tools/helper_eval.py. Запуск: python -m pytest -q -p no:cacheprovider tools/test_helper_eval.py"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import helper_eval as HE  # noqa: E402


class LoadAgent(unittest.TestCase):
    def test_tools_list_and_prompt(self):
        d = HE.load_agent("secretary")
        self.assertEqual(d["tools"], ["Read", "Grep", "Glob", "Bash"])
        self.assertTrue(d["prompt"])
        self.assertIn("description", d)

    def test_missing_frontmatter_exits(self):
        with self.assertRaises((SystemExit, FileNotFoundError)):
            HE.load_agent("нет-такого-помощника")


if __name__ == "__main__":
    unittest.main()
