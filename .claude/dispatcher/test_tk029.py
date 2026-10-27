"""TK-029 (В-12): `tickets.py charter sync` — уставы проекта из шаблонов плагина как есть, строки таблицы зон README — проекта;
журнал и раздел отчёта. Запрет прямой записи уставов (cp, mv, >, python…) — `.claude/hooks/test_charter_guard.py`."""
from __future__ import annotations

import atexit
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

_SANDBOX = tempfile.mkdtemp(prefix="rpv-test-proj-")
os.makedirs(os.path.join(_SANDBOX, ".claude", "roles"))
os.environ["CLAUDE_PROJECT_DIR"] = _SANDBOX
atexit.register(shutil.rmtree, _SANDBOX, True)
sys.path.insert(0, str(Path(__file__).resolve().parent))
import charter  # noqa: E402
import report as R  # noqa: E402
import tickets as TK  # noqa: E402
from tickets_cli import build_parser  # noqa: E402

TZ = timezone(timedelta(hours=4))
NOW = datetime(2026, 10, 27, 12, 0, tzinfo=TZ)
TEMPLATE_README = ("# Команда\n\n| роль | зона |\n|---|---|\n| `Инженер` | код, тесты |\n| `Судья` | вердикты |\n\nПравило А.\nПравило Б.\n")
PROJECT_README = ("# Команда\n\n| роль | зона |\n|---|---|\n| `Инженер` | src/, tools/ |\n| `Судья` | вердикты |\n\nПравило А.\n")


class CharterSync(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "proj"
        self.roles = self.root / ".claude" / "roles"
        (self.root / ".claude" / "dispatcher").mkdir(parents=True)
        (self.roles / "notes").mkdir(parents=True)
        self.tpl = Path(self.tmp.name) / "plugin" / "templates" / "roles"
        self.tpl.mkdir(parents=True)
        (self.tpl.parent.parent / ".claude-plugin").mkdir()
        (self.tpl.parent.parent / ".claude-plugin" / "plugin.json").write_text('{"version": "9.9.9"}', encoding="utf-8")
        (self.tpl / "judge.md").write_bytes("# Судья\r\nновая строка\r\n".encode("utf-8"))
        (self.tpl / "engineer.md").write_text("# Инженер\n", encoding="utf-8")
        (self.tpl / "README.md").write_text(TEMPLATE_README, encoding="utf-8")
        (self.roles / "judge.md").write_text("# Судья\nстарый\n", encoding="utf-8")
        (self.roles / "README.md").write_text(PROJECT_README, encoding="utf-8")
        (self.roles / "notes" / "ceo.md").write_text("блокнот", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def sync(self, *names, **kw):
        return charter.sync(self.root, list(names) or None, templates=self.tpl, now=NOW, **kw)

    def test_judge_is_copied_byte_for_byte_and_the_rest_is_untouched(self):
        entry, msg, err = self.sync("judge.md")
        self.assertIsNone(err)
        self.assertEqual((self.roles / "judge.md").read_bytes(), (self.tpl / "judge.md").read_bytes())
        self.assertFalse((self.roles / "engineer.md").exists())                       # не названный --file не создан
        self.assertEqual((self.roles / "README.md").read_text(encoding="utf-8"), PROJECT_README)
        self.assertEqual((self.roles / "notes" / "ceo.md").read_text(encoding="utf-8"), "блокнот")
        self.assertEqual(entry["files"], {"judge.md": "updated"})
        self.assertIn("9.9.9", msg)
        self.assertEqual(list(self.roles.glob("*.tmp")), [])

    def test_readme_keeps_project_table_rows_and_takes_the_rest_from_the_template(self):
        self.sync("README.md")
        text = (self.roles / "README.md").read_text(encoding="utf-8")
        self.assertIn("| `Инженер` | src/, tools/ |", text)                          # строка зон проекта осталась
        self.assertNotIn("код, тесты", text)
        self.assertIn("Правило Б.", text)                                            # новая строка устава пришла
        self.assertIn("| `Судья` | вердикты |", text)

    def test_readme_keeps_project_zones_that_the_template_lacks(self):
        (self.roles / "README.md").write_text(PROJECT_README.replace("\n\nПравило А.", "\n| `Аналитик` | notebooks/ |\n\nПравило А."), encoding="utf-8")
        self.sync("README.md")
        text = (self.roles / "README.md").read_text(encoding="utf-8")
        self.assertIn("| `Судья` | вердикты |\n| `Аналитик` | notebooks/ |\n\nПравило А.", text)       # после последней строки таблицы шаблона
        self.assertEqual(text.count("Аналитик"), 1)
        self.assertEqual(charter.merge_table_rows("# T\n", "| a | b |\n"), "# T\n| a | b |\n")              # в шаблоне нет таблицы — в конец
        self.assertEqual(charter.merge_table_rows("| a | 1 |", "| a | 2 |\n| z | 3 |"), "| a | 2 |\n| z | 3 |\n")
        self.sync("README.md")
        self.assertEqual(self.sync("README.md")[2], None)
        self.assertEqual(text, (self.roles / "README.md").read_text(encoding="utf-8"))                     # второй перенос ничего не меняет

    def test_readme_without_project_copy_is_the_template(self):
        (self.roles / "README.md").unlink()
        entry, _, err = self.sync("README.md")
        self.assertIsNone(err)
        self.assertEqual((self.roles / "README.md").read_text(encoding="utf-8"), TEMPLATE_README)
        self.assertEqual(entry["files"], {"README.md": "created"})

    def test_all_files_by_default_and_second_run_changes_nothing(self):
        entry, _, err = self.sync()
        self.assertIsNone(err)
        self.assertEqual(entry["files"], {"README.md": "updated", "engineer.md": "created", "judge.md": "updated"})
        again, msg, err = self.sync()
        self.assertEqual((again, err), (None, None))
        self.assertIn("менять нечего", msg)
        self.assertEqual(len(charter.read_log(self.root)), 1)                        # повтор в журнал не пишется

    def test_unknown_or_foreign_names_are_refused_before_any_write(self):
        before = (self.roles / "judge.md").read_bytes()
        for name in ("ceo.md", "../notes/ceo.md", "judge", "notes/x.md"):
            _, _, err = self.sync("judge.md", name)
            self.assertIn("нет в шаблонах плагина", err, name)
        self.assertEqual((self.roles / "judge.md").read_bytes(), before)
        self.assertEqual(charter.read_log(self.root), [])

    def test_no_roles_dir_or_no_templates_is_refused(self):
        _, _, err = charter.sync(self.root.parent / "none", templates=self.tpl)
        self.assertIn("/rpv-init", err)
        shutil.rmtree(self.tpl)
        self.tpl.mkdir()
        self.assertIn("нет шаблонов", self.sync()[2])

    def test_ref_is_checked_and_quote_goes_to_the_journal(self):
        self.assertIn("такого решения нет", self.sync("judge.md", ref="В-99")[2])
        (self.root / ".claude" / "decisions.md").write_text(
            "# Решения владельца\n\n## В-12 · 2026-10-27 05:10\n> без самодеятельности\nСуть: узко\nЗаменяет: —\n", encoding="utf-8")
        entry, _, err = self.sync("judge.md", ref="В-12", why="после 2.0.8")
        self.assertIsNone(err)
        self.assertEqual((entry["ref"], entry["quote"], entry["why"]), ("В-12", "без самодеятельности", "после 2.0.8"))

    def test_report_lists_the_sync_only_in_its_period(self):
        self.sync("judge.md")
        inside = charter.report_lines(self.root, NOW - timedelta(hours=1), NOW + timedelta(hours=1))
        self.assertEqual(len(inside), 1)
        self.assertIn("judge.md", inside[0])
        self.assertIn("слово владельца не названо", inside[0])
        self.assertEqual(charter.report_lines(self.root, NOW + timedelta(hours=1), NOW + timedelta(hours=2)), [])
        text, _ = R.build(R.Period(self.root, NOW - timedelta(hours=1), NOW + timedelta(hours=1), []))
        self.assertIn("уставы из шаблонов плагина 9.9.9", text)

    def test_real_templates_of_this_plugin_sync_into_an_empty_roles_dir(self):
        (self.root / ".claude" / "roles" / "judge.md").unlink()
        entry, _, err = charter.sync(self.root, ["judge.md"], now=NOW)
        self.assertIsNone(err)
        self.assertEqual((self.roles / "judge.md").read_bytes(), (charter.PLUGIN_ROOT / "templates" / "roles" / "judge.md").read_bytes())


class CharterCommand(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / ".claude" / "roles").mkdir(parents=True)
        (self.root / ".claude" / "dispatcher").mkdir(parents=True)
        self.role = ""
        for target in (mock.patch.object(TK, "_caller_role", lambda: self.role), mock.patch.object(TK, "PROJECT_ROOT", self.root)):
            target.start()
            self.addCleanup(target.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def run_cmd(self, *argv):
        args = build_parser().parse_args(["charter", *argv])
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = TK.cmd_charter(args)
        return rc, out.getvalue(), err.getvalue()

    def test_ceo_syncs_judge_and_a_role_session_is_refused(self):
        rc, out, _ = self.run_cmd("sync", "--file", "judge.md")
        self.assertEqual(rc, 0)
        self.assertIn("judge.md (created)", out)
        self.assertTrue((self.root / ".claude" / "roles" / "judge.md").exists())
        for role in ("engineer", "judge", "researcher"):
            self.role = role
            rc, _, err = self.run_cmd("sync")
            self.assertEqual(rc, 1, role)
            self.assertIn("только CEO", err)
        self.role = "ceo"
        self.assertEqual(self.run_cmd("sync", "--file", "judge.md")[0], 0)

    def test_foreign_file_name_is_an_error_not_a_crash(self):
        rc, _, err = self.run_cmd("sync", "--file", "../../rpv.json")
        self.assertEqual(rc, 1)
        self.assertIn("charter sync:", err)
        self.assertIn("charter", json.dumps(build_parser().format_help()))


if __name__ == "__main__":
    unittest.main()
