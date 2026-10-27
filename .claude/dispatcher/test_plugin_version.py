import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import plugin_version as PV  # noqa: E402

PROJECT = "/work/proj"


def make_install(base, versions, entries):
    """Каталог настроек `base` с кэшем плагина: `plugins/cache/mk/role-play-vibing/<версия>/.claude-plugin/plugin.json`
    на каждую версию и `plugins/installed_plugins.json`; entries — [(область, projectPath|None, версия)].
    Возвращает (каталог настроек, {версия: корень плагина})."""
    base = Path(base)
    roots = {}
    for v in versions:
        root = base / "plugins" / "cache" / "mk" / "role-play-vibing" / v
        (root / ".claude-plugin").mkdir(parents=True, exist_ok=True)
        (root / ".claude-plugin" / "plugin.json").write_text(
            json.dumps({"name": "role-play-vibing", "version": v}), encoding="utf-8")
        roots[v] = str(root)
    listed = [{"scope": scope, **({"projectPath": proj} if proj else {}), "installPath": roots[v], "version": v}
              for scope, proj, v in entries]
    other = [{"scope": "user", "installPath": str(base / "elsewhere"), "version": "9.9.9"}]
    (base / "plugins" / "installed_plugins.json").write_text(
        json.dumps({"version": 2, "plugins": {"role-play-vibing@mk": listed, "other@mk": other}}), encoding="utf-8")
    return str(base), roots


class StaleLine(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def install(self, entries, versions=("2.0.5", "2.0.9")):
        return make_install(self.tmp.name, versions, entries)

    def test_old_directory_gets_restart_line(self):
        cfg, roots = self.install([("user", None, "2.0.9")])
        line = PV.stale_line(PROJECT, roots["2.0.5"], cfg)
        self.assertIn("сессия на плагине 2.0.5, установлена 2.0.9", line)
        self.assertIn("перезапустите Claude Code", line)
        self.assertIn("запреты 2.0.9 на эту сессию не действуют", line)

    def test_same_version_has_no_line(self):
        cfg, roots = self.install([("user", None, "2.0.9")])
        self.assertIsNone(PV.stale_line(PROJECT, roots["2.0.9"], cfg))

    def test_narrowest_scope_for_this_project_wins(self):
        cfg, roots = self.install([("user", None, "2.0.9"), ("local", PROJECT, "2.0.5"), ("project", "/other", "2.0.9")])
        self.assertIsNone(PV.stale_line(PROJECT, roots["2.0.5"], cfg))        # local этого проекта — на 2.0.5
        self.assertIsNone(PV.stale_line(PROJECT, roots["2.0.9"], cfg))        # свой каталог среди подходящих записей — не устарел
        self.assertIn("установлена 2.0.9", PV.stale_line("/third", roots["2.0.5"], cfg))   # чужие области не в счёт

    def test_own_directory_among_scoped_entries_is_current(self):
        cfg, roots = self.install([("user", None, "2.0.9"), ("local", PROJECT, "2.0.5")])
        self.assertIsNone(PV.mismatch(PROJECT, roots["2.0.5"], cfg))

    def test_source_checkout_is_not_stale(self):
        cfg, roots = self.install([("user", None, "2.0.9")], versions=("2.0.9",))
        src = Path(self.tmp.name) / "src"
        (src / ".claude-plugin").mkdir(parents=True)
        (src / ".claude-plugin" / "plugin.json").write_text(
            json.dumps({"name": "role-play-vibing", "version": "2.0.8"}), encoding="utf-8")
        self.assertIsNone(PV.stale_line(PROJECT, str(src), cfg))

    def test_no_installed_file_or_entry_has_no_line(self):
        cfg, roots = self.install([("user", None, "2.0.9")])
        (Path(cfg) / "plugins" / "installed_plugins.json").write_text("{плохой json", encoding="utf-8")
        self.assertIsNone(PV.stale_line(PROJECT, roots["2.0.5"], cfg))
        self.assertIsNone(PV.stale_line(PROJECT, roots["2.0.5"], str(Path(self.tmp.name) / "нет")))
        cfg, roots = self.install([("project", "/other", "2.0.9")], versions=("2.0.5", "2.0.9"))
        self.assertIsNone(PV.stale_line(PROJECT, roots["2.0.5"], cfg))

    def test_root_of_walks_up_to_plugin_json(self):
        _, roots = self.install([("user", None, "2.0.9")])
        deep = Path(roots["2.0.5"]) / ".claude" / "dispatcher" / "dispatch.py"
        self.assertEqual(PV.root_of(deep), roots["2.0.5"])
        self.assertIsNone(PV.root_of(Path(self.tmp.name) / "x" / "y.py"))


if __name__ == "__main__":
    unittest.main()
