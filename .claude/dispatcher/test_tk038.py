"""TK-038 (В-13, PR #147 invoice): добавки проекта к уставам `.claude/roles/project/<роль>.md` — синк их не трогает; `charter sync`
и `charter addon` только с `--ref В-N`; строки, убранные синком из устава проекта, он выводит списком и пишет в журнал.
Контекст роли с добавкой — `.claude/hooks/test_role_hooks.py`, запрет прямой записи — `.claude/hooks/test_charter_guard.py`."""
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
import tickets as TK  # noqa: E402
from tickets_cli import build_parser  # noqa: E402

TZ = timezone(timedelta(hours=4))
NOW = datetime(2026, 10, 27, 18, 0, tzinfo=TZ)
DECISIONS = "# Решения владельца\n\n## В-13 · 2026-10-27 17:44\n> одна команда на обновление\nСуть: обновлять одной командой\nЗаменяет: —\n"
PROJECT_ENGINEER = "# Инженер\nстрока шаблона\nПроектное правило: гонять presubmit перед сдачей\n"


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "proj"
        self.roles = self.root / ".claude" / "roles"
        (self.root / ".claude" / "dispatcher").mkdir(parents=True)
        self.roles.mkdir(parents=True)
        (self.root / ".claude" / "decisions.md").write_text(DECISIONS, encoding="utf-8")
        self.tpl = Path(self.tmp.name) / "plugin" / "templates" / "roles"
        self.tpl.mkdir(parents=True)
        (self.tpl.parent.parent / ".claude-plugin").mkdir()
        (self.tpl.parent.parent / ".claude-plugin" / "plugin.json").write_text('{"version": "9.9.9"}', encoding="utf-8")
        for name in ("engineer", "judge", "README"):
            (self.tpl / f"{name}.md").write_text(f"# {name}\nстрока шаблона 2\n", encoding="utf-8")
        (self.roles / "engineer.md").write_text(PROJECT_ENGINEER, encoding="utf-8")
        self.addCleanup(self.tmp.cleanup)

    def sync(self, *names, **kw):
        return charter.sync(self.root, list(names) or None, templates=self.tpl, now=NOW, **kw)

    def addon(self, name="engineer", text="Правило проекта: прогон до сдачи", **kw):
        kw.setdefault("ref", "В-13")
        return charter.addon(self.root, name, text, templates=self.tpl, now=NOW, **kw)


class Addon(Fixture):
    def test_addon_survives_sync_on_a_new_template_and_lands_next_to_the_charter(self):
        entry, msg, err = self.addon(text="Правило проекта: прогон до сдачи", why="правила инженера")
        self.assertIsNone(err)
        path = self.roles / "project" / "engineer.md"
        self.assertEqual(path.read_text(encoding="utf-8"), "Правило проекта: прогон до сдачи\n")
        self.assertEqual((entry["kind"], entry["files"], entry["ref"], entry["quote"]),
                         ("addon", {"project/engineer.md": "created"}, "В-13", "одна команда на обновление"))
        self.assertIn("project/engineer.md", msg)
        synced = self.sync("engineer.md", "judge.md", "README.md", ref="В-13")
        self.assertIsNone(synced[2])
        self.assertEqual((self.roles / "engineer.md").read_text(encoding="utf-8"), "# engineer\nстрока шаблона 2\n")   # устав — как шаблон
        self.assertEqual(path.read_text(encoding="utf-8"), "Правило проекта: прогон до сдачи\n")                        # добавка цела
        self.assertFalse((self.roles / "project" / "judge.md").exists())

    def test_second_addon_is_appended_and_replace_overwrites(self):
        self.addon(text="Первое")
        entry, _, _ = self.addon(text="Второе")
        path = self.roles / "project" / "engineer.md"
        self.assertEqual(path.read_text(encoding="utf-8"), "Первое\n\nВторое\n")
        self.assertEqual(entry["files"], {"project/engineer.md": "appended"})
        entry, _, _ = self.addon(text="Только это", replace=True)
        self.assertEqual(path.read_text(encoding="utf-8"), "Только это\n")
        self.assertEqual(entry["files"], {"project/engineer.md": "replaced"})

    def test_readme_addon_and_role_name_forms(self):
        self.assertIsNone(self.addon("README", "Зона аналитика")[2])
        self.assertIsNone(self.addon("judge.md", "Судья начинает с presubmit")[2])
        self.assertTrue((self.roles / "project" / "README.md").is_file())
        self.assertTrue((self.roles / "project" / "judge.md").is_file())

    def test_refusals_leave_no_file_and_no_journal_line(self):
        for kw, text in (({"ref": ""}, "x"), ({"ref": "В-99"}, "x"), ({"ref": "да"}, "x"), ({}, "  \n"), ({"name": "ghost"}, "x")):
            self.assertTrue(self.addon(text=text, **kw)[2], (kw, text))
        self.assertFalse((self.roles / "project").exists())
        self.assertEqual(charter.read_log(self.root), [])
        self.assertIn("--ref", self.addon(ref="")[2])

    def test_report_names_the_addon_with_the_owner_quote(self):
        self.addon()
        lines = charter.report_lines(self.root, NOW - timedelta(hours=1), NOW + timedelta(hours=1))
        self.assertEqual(len(lines), 1)
        self.assertIn("добавка проекта к уставу: project/engineer.md", lines[0])
        self.assertIn("В-13: «одна команда на обновление»", lines[0])


class SyncRemovedLines(Fixture):
    def test_lines_dropped_from_the_project_charter_are_listed_and_journaled(self):
        entry, msg, err = self.sync("engineer.md", ref="В-13")
        self.assertIsNone(err)
        self.assertEqual(entry["removed"], {"engineer.md": ["# Инженер", "строка шаблона", "Проектное правило: гонять presubmit перед сдачей"]})
        self.assertIn("перенеси нужное в добавку", msg)
        self.assertIn("    Проектное правило: гонять presubmit перед сдачей", msg)
        self.assertIn("engineer.md: 3 стр.", msg)
        self.assertEqual(charter.read_log(self.root)[0]["removed"], entry["removed"])

    def test_nothing_to_list_when_the_charter_only_gained_lines_or_was_created(self):
        (self.roles / "judge.md").write_text("# judge\n", encoding="utf-8")
        entry, msg, _ = self.sync("judge.md", "README.md", ref="В-13")
        self.assertNotIn("removed", entry)
        self.assertNotIn("добавку", msg)

    def test_readme_table_rows_of_the_project_are_not_reported_as_removed(self):
        (self.roles / "README.md").write_text("# README\n| `Инженер` | src/ |\nстарая\n", encoding="utf-8")
        (self.tpl / "README.md").write_text("# README\n| `Инженер` | код |\nновая\n", encoding="utf-8")
        entry, _, _ = self.sync("README.md", ref="В-13")
        self.assertEqual(entry["removed"], {"README.md": ["старая"]})


class CharterCommand(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / ".claude" / "roles").mkdir(parents=True)
        (self.root / ".claude" / "dispatcher").mkdir(parents=True)
        (self.root / ".claude" / "decisions.md").write_text(DECISIONS, encoding="utf-8")
        self.role = ""
        for target in (mock.patch.object(TK, "_caller_role", lambda: self.role), mock.patch.object(TK, "PROJECT_ROOT", self.root)):
            target.start()
            self.addCleanup(target.stop)
        self.addCleanup(self.tmp.cleanup)

    def run_cmd(self, *argv):
        args = build_parser().parse_args(["charter", *argv])
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = TK.cmd_charter(args)
        return rc, out.getvalue(), err.getvalue()

    def test_sync_without_ref_is_refused_and_writes_nothing(self):
        rc, _, err = self.run_cmd("sync", "--file", "judge.md")
        self.assertEqual(rc, 1)
        self.assertIn("--ref", err)
        self.assertFalse((self.root / ".claude" / "roles" / "judge.md").exists())
        self.assertEqual(charter.read_log(self.root), [])
        self.assertEqual(self.run_cmd("sync", "--file", "judge.md", "--ref", "В-99")[0], 1)       # решения нет в реестре
        self.assertEqual(self.run_cmd("sync", "--file", "judge.md", "--ref", "В-13")[0], 0)
        self.assertEqual(charter.read_log(self.root)[0]["ref"], "В-13")

    def test_addon_through_the_command_writes_the_file_and_a_role_session_is_refused(self):
        rc, out, err = self.run_cmd("addon", "judge", "--text", "Судья начинает с presubmit", "--ref", "В-13")
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("project/judge.md", out)
        self.assertEqual((self.root / ".claude" / "roles" / "project" / "judge.md").read_text(encoding="utf-8"), "Судья начинает с presubmit\n")
        self.assertEqual(self.run_cmd("addon", "judge", "--text", "x")[0], 1)                      # без --ref
        for role in ("engineer", "judge", "researcher"):
            self.role = role
            rc, _, err = self.run_cmd("addon", "judge", "--text", "x", "--ref", "В-13")
            self.assertEqual(rc, 1, role)
            self.assertIn("только CEO", err)
        self.assertEqual(json.loads((self.root / ".claude" / "dispatcher" / "charter-log.json").read_text(encoding="utf-8"))[0]["kind"], "addon")
        self.assertEqual(len(charter.read_log(self.root)), 1)
