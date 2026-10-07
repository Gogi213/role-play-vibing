import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import haiku_aux as H  # noqa: E402


class R:
    def __init__(self, out=b"ok", code=0):
        self.stdout, self.returncode = out, code


class HaikuAux(unittest.TestCase):
    def test_ask_builds_cmd_and_trims_input(self):
        seen = {}

        def run(cmd, **kw):
            seen["cmd"], seen["in"] = cmd, kw["input"]
            return R("нехватка памяти".encode())
        out = H.ask("sys", "x" * 20000, runner=run, claude="claude")
        self.assertEqual(out, "нехватка памяти")
        self.assertEqual(seen["cmd"][seen["cmd"].index("--model") + 1], "claude-haiku-5-5")
        self.assertEqual(len(seen["in"]), H.MAX_IN)

    def test_failures_give_none(self):
        self.assertIsNone(H.ask("s", "t", runner=lambda *a, **k: R(b"", 1), claude="c"))
        self.assertIsNone(H.ask("s", "t", runner=lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired("c", 1)), claude="c"))
        self.assertIsNone(H.ask("s", "  ", runner=lambda *a, **k: R(), claude="c"))


if __name__ == "__main__":
    unittest.main()
