"""TK-045: `role_args._read_mcp_servers` сравнивает путь проекта через `plugin_version.norm` (единый способ нормализации)."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import plugin_version as PV  # noqa: E402
import role_args as RA  # noqa: E402


class ReadMcpServersNormTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.proj = self.root / "proj"
        self.proj.mkdir()
        self.cfg = self.root / "claude.json"
        # ключ записан с «..» и лишним разделителем — после нормализации это тот же путь
        key = str(self.root / "x" / ".." / "proj") + "/"
        self.cfg.write_text(json.dumps({"projects": {key: {"mcpServers": {"local": {"command": "l"}}}}}), encoding="utf-8")

    def test_project_key_matches_after_normalization(self):
        self.assertEqual(RA._read_mcp_servers(self.cfg, str(self.proj)), {"local": {"command": "l"}})

    def test_other_project_does_not_match(self):
        self.assertEqual(RA._read_mcp_servers(self.cfg, str(self.root / "other")), {})

    def test_comparison_goes_through_plugin_version_norm(self):
        with mock.patch.object(PV, "norm", wraps=PV.norm) as spy:
            RA._read_mcp_servers(self.cfg, str(self.proj))
        self.assertTrue(spy.called)


if __name__ == "__main__":
    unittest.main()
