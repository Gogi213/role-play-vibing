"""CP8: `docs/configuration.md` совпадает с кодом — переменные окружения `RPV_*` и ключи `.claude/rpv.json`, которые читает
код плагина, и только они, описаны в документе."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOC = ROOT / "docs" / "configuration.md"
SOURCES = [p for pattern in (".claude/dispatcher/*.py", ".claude/hooks/*.py", "hooks/*", "tools/*")
           for p in ROOT.glob(pattern) if p.is_file() and not p.name.startswith(("test_", "conftest"))]
ENV_RE = re.compile(r"""RPV_[A-Z][A-Z0-9_]*|\b(?:P\.env|_env|env)\(\s*["']([A-Z][A-Z0-9_]*)["']""")
KEY_RE = re.compile(r"""\bsetting\(\s*["']([a-z_]+)["']|_str_list\(data,\s*["']([a-z_]+)["']|data\.get\(["'](guard_[a-z_]+)["']""")


def code_names():
    env, keys = set(), set()
    for p in SOURCES:
        text = p.read_text(encoding="utf-8", errors="replace")
        for m in ENV_RE.finditer(text):
            env.add(m.group(0) if m.group(0).startswith("RPV_") else "RPV_" + m.group(1))
        for m in KEY_RE.finditer(text):
            keys.add(next(g for g in m.groups() if g))
    return env, keys


class ConfigurationDocTests(unittest.TestCase):
    def test_env_names_match_code(self):
        env, _ = code_names()
        documented = set(re.findall(r"`(RPV_[A-Z0-9_]+)`", DOC.read_text(encoding="utf-8")))
        self.assertEqual(sorted(env - documented), [], "переменная читается кодом, но не описана")
        self.assertEqual(sorted(documented - env), [], "описана, но код её не читает")
        self.assertLessEqual(len(env), 10, "CP8: переменных — несколько, остальное — rpv.json или константы")

    def test_settings_keys_match_code(self):
        _, keys = code_names()
        documented = set(re.findall(r"^\| `([a-z_]+)` \|", DOC.read_text(encoding="utf-8"), re.M))
        self.assertEqual(sorted(keys - documented), [], "ключ rpv.json читается кодом, но не описан")
        self.assertEqual(sorted(documented - keys), [], "ключ описан, но код его не читает")


if __name__ == "__main__":
    unittest.main()
