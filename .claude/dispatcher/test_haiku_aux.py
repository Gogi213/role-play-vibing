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
        self.assertEqual(seen["cmd"][seen["cmd"].index("--effort") + 1], "xhigh")

    def test_failures_give_none(self):
        self.assertIsNone(H.ask("s", "t", runner=lambda *a, **k: R(b"", 1), claude="c"))
        self.assertIsNone(H.ask("s", "t", runner=lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired("c", 1)), claude="c"))
        self.assertIsNone(H.ask("s", "  ", runner=lambda *a, **k: R(), claude="c"))

    def test_compact_log_adds_digest_when_enabled(self):
        import os, tempfile
        import ticket as T
        nl = chr(10)
        os.environ["RPV_HAIKU_COMPACT"] = "1"
        orig, H.compact = H.compact, lambda t: "KONSPEKT"
        try:
            p = Path(tempfile.mkdtemp()) / "TK-9.md"
            head = nl.join(["---", "id: TK-9", "title: t", "owner: engineer", "status: todo", "---", "", "d", "", "## Лог", "", ""])
            ents = "".join(f"### 2026-10-08T01:0{i}:00+04:00 ceo{nl}entry {i} " + "x" * 400 + nl + nl for i in range(9))
            p.write_text(head + ents, encoding="utf-8")
            self.assertGreater(T.compact_log(p, keep=2, limit_bytes=100), 0)
            self.assertIn("KONSPEKT", p.read_text(encoding="utf-8"))
        finally:
            H.compact = orig
            del os.environ["RPV_HAIKU_COMPACT"]


if __name__ == "__main__":
    unittest.main()
