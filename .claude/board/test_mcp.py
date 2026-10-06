import json
import tempfile
import unittest
from pathlib import Path

import mcp_server as S


class McpTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.pulse = Path(self.d.name) / ".claude" / "pulse"
        self.pulse.mkdir(parents=True)
        S.ROOT = Path(self.d.name)

    def tearDown(self):
        S.ROOT = None
        self.d.cleanup()

    def call(self, name, args=None):
        return S.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args or {}}})["result"]

    def test_tools_and_server_name(self):
        init = S.handle({"id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})["result"]
        self.assertEqual(init["serverInfo"]["name"], "rpv-pulse")
        tools = S.handle({"id": 2, "method": "tools/list"})["result"]["tools"]
        self.assertEqual([t["name"] for t in tools], ["pulse_status", "pulse_answer"])
        self.assertIsNone(S.handle({"method": "notifications/initialized"}))

    def test_status_missing_then_present(self):
        r = self.call("pulse_status")
        self.assertTrue(r["isError"])
        self.assertIn("board_push.py", r["content"][0]["text"])
        (self.pulse / "status.json").write_text(json.dumps({"view2": {"time": "10:00", "built_ts": 1.0}, "built_at": "10:00"}), encoding="utf-8")
        r = self.call("pulse_status")
        self.assertNotIn("isError", r)
        self.assertEqual(r["structuredContent"]["view2"]["time"], "10:00")
        self.assertGreater(r["structuredContent"]["stale_s"], 0)
        (self.pulse / "status.json").write_text("{битый", encoding="utf-8")
        self.assertTrue(self.call("pulse_status")["isError"])

    def test_unknown_tool(self):
        self.assertIn("error", S.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "nope"}}))


if __name__ == "__main__":
    unittest.main()
