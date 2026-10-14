"""Выпуск плагина вручную (CP4): версии согласованы, bump правит версии и требует запись в CHANGELOG; автовыпуска, update и
rollback нет."""
import contextlib
import io
import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

import release


def make_root(d: Path, version="1.0.0", changelog="## 1.0.0\n"):
    (d / ".claude-plugin").mkdir()
    (d / ".claude-plugin" / "plugin.json").write_text(json.dumps({"version": version}), encoding="utf-8")
    (d / ".claude-plugin" / "marketplace.json").write_text(json.dumps({"plugins": [{"version": version}]}), encoding="utf-8")
    (d / "CHANGELOG.md").write_text("# Changelog\n\n" + changelog, encoding="utf-8")


class ReleaseTests(unittest.TestCase):
    def test_repo_versions_consistent(self):
        self.assertIsNone(release.check())

    def test_bump_requires_changelog_and_updates_both_files(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            make_root(root)
            with self.assertRaises(SystemExit):
                release.bump("1.1.0", root)
            (root / "CHANGELOG.md").write_text("# C\n\n## 1.1.0\n- x\n\n## 1.0.0\n", encoding="utf-8")
            release.bump("1.1.0", root)
            self.assertEqual(release.read_versions(root), {"plugin": "1.1.0", "marketplace": "1.1.0", "changelog": "1.1.0"})
            with self.assertRaises(SystemExit):
                release.bump("v1.2", root)

    def test_check_reports_divergence(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            make_root(root)
            self.assertIsNone(release.check(root))
            (root / "CHANGELOG.md").write_text("# C\n\n## 1.0.1\n\n## 1.0.0\n", encoding="utf-8")
            self.assertIn("расходятся", release.check(root))

    def test_only_manual_commands_remain(self):
        """CP4 (критерий 16): автовыпуск, update и rollback удалены — осталось то, что делает мейнтейнер руками."""
        for cmd in ("auto", "update", "rollback"):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(release.main([cmd, "--project", "."]), 2, cmd)
            self.assertIn("bump X.Y.Z", out.getvalue())
        for name in ("autorelease", "run_auto_locked", "spawn_auto", "update", "rollback", "bump_problem", "tag_release"):
            self.assertFalse(hasattr(release, name), name)

    def test_run_timeout_returns_124_not_hang(self):
        """Б3: git без предела держал процесс часами."""
        def hang(cmd, **kw):
            self.assertIn("timeout", kw)
            raise subprocess.TimeoutExpired(cmd, kw["timeout"])
        r = release._run(["git", "push", "x"], hang)
        self.assertEqual(r.returncode, 124)


class ChangelogTagTests(unittest.TestCase):
    ROOT = Path(__file__).resolve().parents[2]

    def headings(self):
        return re.findall(r"^## (\d+\.\d+\.\d+)", (self.ROOT / "CHANGELOG.md").read_text(encoding="utf-8"), re.M)

    def test_manifest_version_is_top_changelog_entry(self):
        ver = json.loads((self.ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["version"]
        self.assertEqual(self.headings()[0], ver)

    def test_every_tag_has_changelog_entry(self):
        r = subprocess.run(["git", "tag", "--list", "v*"], cwd=self.ROOT, capture_output=True, text=True)
        tags = [t[1:] for t in r.stdout.split() if re.fullmatch(r"v\d+\.\d+\.\d+", t)]
        if r.returncode or not tags:
            self.skipTest("тегов нет (клон без тегов)")
        # 1.8.4 выпущена без тега — исторический пропуск; тег без записи — ошибка
        self.assertEqual([t for t in tags if t not in self.headings()], [])


if __name__ == "__main__":
    unittest.main()
