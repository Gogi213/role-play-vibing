"""TK-092: блокнот роли > 4 КБ ужимается кодом при старте роли (`role_context.rotate_notebook`), старое — в notes/archive."""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="rotation-test-")
PROJECT = os.path.join(TMP, "proj")
shutil.copytree(os.path.join(PLUGIN, "templates", "roles"), os.path.join(PROJECT, ".claude", "roles"))
sys.path.insert(0, HERE)


def load_role_context():
    """Свой экземпляр модуля под проект-пустышку: sys.modules["role_context"] и окружение не трогаются —
    другие тесты хуков в том же процессе (xdist) держат свой проект."""
    keys = ("CLAUDE_PROJECT_DIR", "RPV_ROLE", "RPV_TICKET")
    saved = {k: os.environ.get(k) for k in keys}
    os.environ["CLAUDE_PROJECT_DIR"] = PROJECT
    os.environ.pop("RPV_ROLE", None)
    os.environ.pop("RPV_TICKET", None)
    try:
        spec = importlib.util.spec_from_file_location("role_context_rotation", os.path.join(HERE, "role_context.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


rc = load_role_context()

NOTES = os.path.join(PROJECT, ".claude", "roles", "notes")


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


def write(role, text):
    os.makedirs(NOTES, exist_ok=True)
    shutil.rmtree(os.path.join(NOTES, "archive"), ignore_errors=True)   # каждый тест — с чистым архивом
    with open(os.path.join(NOTES, f"{role}.md"), "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def read(*parts):
    with open(os.path.join(NOTES, *parts), encoding="utf-8", newline="") as fh:
        return fh.read()


class RotateNotebook(unittest.TestCase):
    def entries(self, n, size=300):
        return [f"- TK-{i:03d} " + "я" * size + "\n" for i in range(n)]

    def test_over_limit_old_entries_go_to_archive_tail_stays(self):
        lines = self.entries(40)
        write("engineer", "".join(lines))
        moved = rc.rotate_notebook("engineer")
        body = read("engineer.md")
        self.assertLessEqual(len(body.encode("utf-8")), rc.NOTEBOOK_MAX)
        self.assertTrue(body.endswith(lines[-1]), "хвост — последние записи, целыми")
        self.assertTrue(all(l in lines for l in body.splitlines(keepends=True)), "записи не порезаны")
        archive = read("archive", "engineer.md")
        self.assertGreater(moved, 0)
        self.assertEqual(archive.count("- TK-"), 40 - body.count("- TK-"))
        self.assertIn(lines[0], archive)

    def test_nothing_lost_across_repeated_rotations(self):
        lines = self.entries(60)
        write("engineer", "".join(lines[:30]))
        rc.rotate_notebook("engineer")
        with open(os.path.join(NOTES, "engineer.md"), "a", encoding="utf-8", newline="") as fh:
            fh.write("".join(lines[30:]))
        rc.rotate_notebook("engineer")
        everything = read("archive", "engineer.md") + read("engineer.md")
        for l in lines:
            self.assertIn(l, everything)

    def test_within_limit_untouched(self):
        text = "".join(self.entries(3))
        write("researcher", text)
        self.assertEqual(rc.rotate_notebook("researcher"), 0)
        self.assertEqual(read("researcher.md"), text)
        self.assertFalse(os.path.exists(os.path.join(NOTES, "archive", "researcher.md")))

    def test_single_huge_entry_kept_whole(self):
        text = "- старая\n- " + "ы" * 6000 + "\n"
        write("judge", text)
        rc.rotate_notebook("judge")
        self.assertEqual(read("judge.md"), "- " + "ы" * 6000 + "\n")
        self.assertEqual(read("archive", "judge.md").count("- старая"), 1)

    def test_session_start_rotates_and_inserts_tail(self):
        write("engineer", "".join(self.entries(40)))
        env = dict(os.environ, RPV_ROLE="engineer", CLAUDE_PROJECT_DIR=PROJECT)
        r = subprocess.run([sys.executable, os.path.join(HERE, "role_context.py")], env=env, capture_output=True,
                           input=json.dumps({"hook_event_name": "SessionStart", "source": "startup",
                                             "session_id": "t-rot"}).encode("utf-8"))
        self.assertEqual(r.returncode, 0, r.stderr)
        text = json.loads(r.stdout.decode("utf-8"))["hookSpecificOutput"]["additionalContext"]
        self.assertLessEqual(len(read("engineer.md").encode("utf-8")), rc.NOTEBOOK_MAX)
        self.assertIn("- TK-039 ", text, "последняя запись — во вставке")
        self.assertTrue(os.path.isfile(os.path.join(NOTES, "archive", "engineer.md")))


class HooksRegistration(unittest.TestCase):
    def test_posttooluse_skill_not_registered(self):
        """TK-092: PostToolUse:Skill был пустышкой (role_memory молчал на нём) — в hooks.json его нет."""
        with open(os.path.join(PLUGIN, "hooks", "hooks.json"), encoding="utf-8") as fh:
            hooks = json.load(fh)["hooks"]
        self.assertNotIn("PostToolUse", hooks)
        with open(os.path.join(HERE, "role_memory.py"), encoding="utf-8") as fh:
            self.assertNotIn('"PostToolUse"', fh.read())


if __name__ == "__main__":
    unittest.main()
