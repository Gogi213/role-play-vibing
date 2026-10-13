import json
import unittest
from pathlib import Path

B = Path(__file__).resolve().parents[2] / "templates" / "brief"


class BriefTemplateTests(unittest.TestCase):
    def test_files_present(self):
        for rel in ("task.md", "preferences.md", "state/bookmarks.json", "state/ledger.md", "state/runs/.gitkeep"):
            self.assertTrue((B / rel).is_file(), rel)

    def test_bookmarks_valid_json_with_cursor(self):
        d = json.loads((B / "state" / "bookmarks.json").read_text(encoding="utf-8"))
        self.assertIn("last_report_at", d)

    def test_task_has_required_rules(self):
        t = (B / "task.md").read_text(encoding="utf-8")
        for needle in ("только чтение", "bookmarks.json", "ledger.md", "state/runs/",
                       "Сбой источника ≠ тишина", "Перепроверка", "15 строк"):
            self.assertIn(needle, t)

    def test_preferences_has_size_cap(self):
        self.assertIn("15 строк", (B / "preferences.md").read_text(encoding="utf-8"))

    def test_readme_mentions_template(self):
        r = (B.parents[1] / "README.md").read_text(encoding="utf-8")
        self.assertIn("templates/brief/", r)


if __name__ == "__main__":
    unittest.main()
