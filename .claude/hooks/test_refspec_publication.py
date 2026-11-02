"""TK-044 (#3): разбор одной записи refspec `git push` вынесен из `push_publication`. Запуск:
`python -m pytest .claude/hooks/test_refspec_publication.py`."""
import os
import sys
import unittest

# корень — окружением ДО импорта, как в test_delete_guard; чужое значение возвращаем
_PREV = os.environ.get("CLAUDE_PROJECT_DIR")
os.environ["CLAUDE_PROJECT_DIR"] = r"C:\visual projects\proj"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dguard import git_scan as gs  # noqa: E402
if _PREV is None:
    os.environ.pop("CLAUDE_PROJECT_DIR", None)
else:
    os.environ["CLAUDE_PROJECT_DIR"] = _PREV


class Refspec(unittest.TestCase):
    def test_main_is_named(self):
        for spec in ("main", "HEAD:main", "x:refs/heads/main", "refs/heads/master"):
            self.assertEqual(gs.refspec_publication(spec), f"git push {spec}: основная ветка", spec)

    def test_tag_has_label(self):
        for spec in ("v1.2.3", "refs/tags/x", "HEAD:refs/tags/v2.0.0"):
            self.assertEqual(gs.refspec_publication(spec), f"git push {spec}: тег")

    def test_pattern_is_all_branches(self):
        self.assertIn("по шаблону", gs.refspec_publication("refs/heads/*:refs/heads/*"))

    def test_working_branch_publishes_nothing(self):
        self.assertIsNone(gs.refspec_publication("eng/tk-044"))
        self.assertIsNone(gs.refspec_publication("HEAD:eng/tk-044"))

    def test_first_label_wins(self):
        pub = gs.push_publication(["origin", "v1.0.0", "main", "x:master"], "/r")
        self.assertEqual(str(pub), "git push v1.0.0: тег")


class CurrentBranch(unittest.TestCase):
    def test_default_branch_unknown_and_working(self):
        for cur, want in (("main", "git push без ветки: текущая — main"), (None, "git push без ветки: текущую не определить"),
                          ("eng/x", None)):
            orig, gs.head_branch = gs.head_branch, lambda *_, cur=cur: cur
            try:
                pub = gs.push_publication(["origin"], "/r")
                self.assertEqual(None if pub is None else str(pub), want)
            finally:
                gs.head_branch = orig


if __name__ == "__main__":
    unittest.main()
