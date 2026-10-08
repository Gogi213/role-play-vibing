"""TK-105 пп.5–6: служебные процессы без видимых окон, роли — со скрытой консолью, пределы времени."""
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import hide  # noqa: E402

SERVICE_MODULES = ["dispatch", "watch", "start", "doctor", "ci_watch", "lifewatch", "ask", "worktree_hygiene"]


class HideTests(unittest.TestCase):
    def test_run_adds_flags_and_timeout(self):
        with mock.patch("subprocess.run") as r:
            hide.run(["x"], capture_output=True)
        kw = r.call_args.kwargs
        self.assertEqual(kw["timeout"], hide.DEFAULT_TIMEOUT_S)
        if os.name == "nt":
            self.assertEqual(kw["creationflags"], hide.CREATE_NO_WINDOW)

    def test_run_keeps_caller_timeout(self):
        with mock.patch("subprocess.run") as r:
            hide.run(["x"], timeout=5)
        self.assertEqual(r.call_args.kwargs["timeout"], 5)

    @unittest.skipUnless(os.name == "nt", "Windows")
    def test_role_console_is_hidden_not_absent(self):
        kw = hide.hidden_console()
        self.assertTrue(kw["creationflags"] & hide.CREATE_NEW_CONSOLE)
        self.assertFalse(kw["creationflags"] & hide.CREATE_NO_WINDOW)
        self.assertEqual(kw["startupinfo"].wShowWindow, 0)

    def test_services_have_no_bare_subprocess_run(self):
        for m in SERVICE_MODULES:
            src = (HERE / f"{m}.py").read_text(encoding="utf-8")
            self.assertIsNone(re.search(r"subprocess\.run\(", src), f"{m}.py: голый subprocess.run")
            self.assertNotIn("run=subprocess.run", src, m)

    def test_every_runner_default_is_hidden(self):
        for rel in ["dispatcher/haiku_aux.py", "board/machines.py", "board/plainify.py", "dispatcher/board_push.py",
                    "dispatcher/ticket.py"]:
            src = (HERE.parent / rel).read_text(encoding="utf-8")
            self.assertNotRegex(src, r"(runner|run)=subprocess\.run", rel)
            self.assertIsNone(re.search(r"return subprocess\.run\(\w+, capture_output", src), rel)

    def test_role_launch_uses_hidden_console(self):
        src = (HERE / "dispatch.py").read_text(encoding="utf-8")
        self.assertIn("**hide.hidden_console()", src)
        self.assertNotIn("start_new_session=True)", src)

    def test_hung_child_killed_by_timeout(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            hide.run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=1)


if __name__ == "__main__":
    unittest.main()
