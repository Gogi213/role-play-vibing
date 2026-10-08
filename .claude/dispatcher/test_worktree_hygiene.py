import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import worktree_hygiene as WH  # noqa: E402


def git(cwd, *a):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=cwd, check=True, capture_output=True)


class WorktreeHygieneTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.p = self.root / "proj"
        self.p.mkdir()
        git(self.p, "init", "-q", "-b", "main")
        (self.p / "a.txt").write_text("a")
        git(self.p, "add", "-A")
        git(self.p, "commit", "-qm", "init")
        self.wt = self.p / ".claude" / "worktrees"
        self.wt.mkdir(parents=True)

    def add(self, name, branch=None, base=None):
        args = ["worktree", "add", "-q"] + (["-b", branch] if branch else ["--detach"]) + [str(base or self.wt / name)]
        git(self.p, *args)
        return base or self.wt / name

    def test_closed_removed_dirty_committed_to_branch_live_kept(self):
        a = self.add("tk104-x", "br-closed")
        (a / "new.txt").write_text("wip")
        live = self.add("tk105-y", "br-live")
        out = WH.sweep(self.p, {"TK-104": "done", "TK-105": "in_progress"})
        self.assertEqual(len(out), 1)
        self.assertFalse(a.exists())
        self.assertTrue(live.exists())
        show = subprocess.run(["git", "show", "br-closed:new.txt"], cwd=self.p, capture_output=True, text=True)
        self.assertEqual(show.stdout, "wip")

    def test_dirty_detached_is_kept(self):
        a = self.add("tk104-d")
        (a / "new.txt").write_text("wip")
        WH.sweep(self.p, {"TK-104": "stopped"})
        self.assertTrue(a.exists())

    def test_closed_name_live_branch_kept(self):
        a = self.add("tk065-x", "tk084-r2dl")
        WH.sweep(self.p, {"TK-065": "done", "TK-084": "waiting"})
        self.assertTrue(a.exists())
        WH.sweep(self.p, {"TK-065": "done", "TK-084": "done"})
        self.assertFalse(a.exists())

    def test_ignored_data_kept_build_dirs_not(self):
        (self.p / ".gitignore").write_text("data/\ntarget/\n")
        git(self.p, "add", "-A")
        git(self.p, "commit", "-qm", "ign")
        a = self.add("tk104-i", "br-i")
        (a / "target").mkdir()
        (a / "target" / "x").write_text("b")
        WH.sweep(self.p, {"TK-104": "done"})
        self.assertFalse(a.exists())
        b = self.add("tk104-j", "br-j")
        (b / "data").mkdir()
        (b / "data" / "r.csv").write_text("r")
        out = WH.sweep(self.p, {"TK-104": "done"})
        self.assertTrue(b.exists())
        self.assertIn("data/", out[0])

    def test_foreign_copy_reported_not_removed(self):
        out = self.add("tk104-f", "br-f", base=self.root / "proj-tk104")
        closed, foreign = WH.findings(self.p, {"TK-104": "done"})
        self.assertEqual(closed, [])
        self.assertEqual([x.resolve() for x in foreign], [out.resolve()])
        WH.sweep(self.p, {"TK-104": "done"})
        self.assertTrue(out.exists())


if __name__ == "__main__":
    unittest.main()
