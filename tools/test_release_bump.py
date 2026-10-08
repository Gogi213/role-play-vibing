"""release_bump: версия обязана быть в CHANGELOG, обе записи версии обновляются."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import release_bump  # noqa: E402
import release  # noqa: E402,F401  (путь к .claude/dispatcher добавляет release_bump)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".claude" / "dispatcher"))
from test_release import make_root  # noqa: E402


class BumpTests(unittest.TestCase):
    def test_bump_requires_changelog_and_updates_both_files(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            make_root(root)
            with self.assertRaises(SystemExit):
                release_bump.bump("1.1.0", root)
            (root / "CHANGELOG.md").write_text("# C\n\n## 1.1.0\n- x\n\n## 1.0.0\n", encoding="utf-8")
            release_bump.bump("1.1.0", root)
            self.assertEqual(release.read_versions(root), {"plugin": "1.1.0", "marketplace": "1.1.0", "changelog": "1.1.0"})
            with self.assertRaises(SystemExit):
                release_bump.bump("v1.2", root)
