"""Выпуск и откат (TK-076 п.5): версии согласованы, bump/update/rollback — по последовательности команд claude."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import release


class FakeClaude:
    def __init__(self, versions, fail_on=None):
        self.versions, self.calls, self.fail_on = list(versions), [], fail_on

    def __call__(self, cmd, **kw):
        self.calls.append(cmd[1:])
        if cmd[1:3] == ["plugin", "list"]:
            v = self.versions[0] if len(self.versions) == 1 else self.versions.pop(0)
            return subprocess.CompletedProcess(cmd, 0, f"  ❯ {release.NAME}@{release.NAME}\n    Version: {v}\n", "")
        return subprocess.CompletedProcess(cmd, 1 if cmd[1:4] == self.fail_on else 0, "", "boom")


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

    def test_update_records_previous_version(self):
        with tempfile.TemporaryDirectory() as t:
            st = Path(t) / "s.json"
            fake = FakeClaude(["1.7.1", "1.8.0"])
            self.assertEqual(release.update(fake, st), 0)
            self.assertEqual(json.loads(st.read_text())["previous"], "1.7.1")
            self.assertIn(["plugin", "update", f"{release.NAME}@{release.NAME}"], fake.calls)

    def test_rollback_uses_recorded_previous_and_tag_ref(self):
        with tempfile.TemporaryDirectory() as t:
            st = Path(t) / "s.json"
            st.write_text(json.dumps({"previous": "1.7.1"}))
            fake = FakeClaude(["1.8.0", "1.7.1"])
            self.assertEqual(release.rollback(None, fake, st), 0)
            self.assertIn(["plugin", "marketplace", "add", f"{release.REPO}#v1.7.1"], fake.calls)
            self.assertEqual(json.loads(st.read_text())["previous"], "1.8.0")  # откат обратим

    def test_rollback_without_target_refuses_and_failure_reported(self):
        with tempfile.TemporaryDirectory() as t:
            st = Path(t) / "none.json"
            self.assertEqual(release.rollback(None, FakeClaude(["1"]), st), 2)
            self.assertEqual(release.rollback("1.0.0", FakeClaude(["1"], fail_on=["plugin", "marketplace", "add"]), st), 1)


if __name__ == "__main__":
    unittest.main()
