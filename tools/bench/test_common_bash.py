"""TK-023: `bench/common.find_bash` не берёт заглушку WSL из System32 (раньше — просто `shutil.which("bash")`);
`clean_env` — окружение плагина без `RPV_*` и `CLAUDE_PROJECT_DIR`, плюс `extra`."""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

STUB = r"C:\Windows\System32\bash.exe"


class FindBashTests(unittest.TestCase):
    def setUp(self):  # путь `nt` ищет Git Bash на диске — здесь проверяется только отбор `which`
        patcher = mock.patch.object(common.os, "name", "posix")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_system32_stub_is_refused(self):
        with mock.patch.object(common.shutil, "which", return_value=STUB), \
                mock.patch.object(common.os.path, "isfile", return_value=True):
            with self.assertRaises(SystemExit):
                common.find_bash()

    def test_regular_bash_is_returned(self):
        with mock.patch.object(common.shutil, "which", return_value="/usr/bin/bash"), \
                mock.patch.object(common.os.path, "isfile", return_value=True):
            self.assertEqual(common.find_bash(), "/usr/bin/bash")

    def test_missing_bash_is_refused(self):
        with mock.patch.object(common.shutil, "which", return_value=None):
            with self.assertRaises(SystemExit):
                common.find_bash()


class CleanEnvTests(unittest.TestCase):
    def test_strips_role_vars_and_adds_extra(self):
        with mock.patch.dict(os.environ, {"RPV_ROLE": "x", "CLAUDE_PROJECT_DIR": "/p", "KEEP_ME": "1"}):
            env = common.clean_env(A=5)
        self.assertNotIn("RPV_ROLE", env)
        self.assertNotIn("CLAUDE_PROJECT_DIR", env)
        self.assertEqual((env["KEEP_ME"], env["A"]), ("1", "5"))


if __name__ == "__main__":
    unittest.main()
