"""TK-029 (В-12): сессия CEO не правит уставы `.claude/roles/*.md` файловыми инструментами (Write/Edit/MultiEdit/NotebookEdit),
перезаписью и удалением (`>`, cp/mv поверх, tee, rm) — отказ с подсказкой `tickets.py charter sync`. Блокнот, чтение и создание
нового файла (`/rpv-init`) — можно. Прочие записи через оболочку (sed -i, python open, `>>`) страж не разбирает (вырез 2.0.18)."""
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# как в test_delete_guard: корень проекта задаётся окружением ДО импорта стража (модуль общий на процесс pytest)
os.environ["CLAUDE_PROJECT_DIR"] = r"C:\visual projects\proj"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import delete_guard as dg  # noqa: E402
from dguard import config as cfg  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
PLUGIN = "C:/Users/me/.claude/plugins/cache/role-play-vibing/role-play-vibing/2.0.8"
SRC = f"{PLUGIN}/templates/roles/judge.md"


class CharterGuard(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="dg-ch-", dir=str(Path.home()))          # под домашним: не временный каталог
        self.addCleanup(shutil.rmtree, self.d, True)
        self.base = Path(self.d).as_posix()
        for rel in ("judge.md", "README.md", "notes/ceo.md"):
            f = Path(self.d, ".claude", "roles", rel)
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("x", encoding="utf-8")
        fields = ("LOCAL_ROOTS", "WRITE_HOME_ROOTS", "PROJECT_DIR", "PROTECTED", "PROTECTED_ERROR")
        saved = {k: getattr(cfg, k) for k in fields}                                # configure() меняет настройки стража на весь процесс
        self.addCleanup(lambda: [setattr(cfg, k, v) for k, v in saved.items()])
        patch = mock.patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": self.d})
        patch.start()
        self.addCleanup(patch.stop)
        self.as_role(None)
        dg.configure(self.d)

    def as_role(self, role):
        os.environ.pop("RPV_ROLE", None)
        if role:
            os.environ["RPV_ROLE"] = role

    def shell(self, cmd, tool="Bash", cwd=None):
        return dg.check_tool({"tool_name": tool, "cwd": cwd or self.d, "tool_input": {"command": cmd}})

    def no(self, cmd, tool="Bash", cwd=None):
        reason = self.shell(cmd, tool, cwd)
        self.assertIsNotNone(reason, f"пропущено: {cmd!r}")
        self.assertIn("charter sync", reason, cmd)

    def ok(self, cmd, tool="Bash", cwd=None):
        reason = self.shell(cmd, tool, cwd)
        self.assertIsNone(reason, f"ложный отказ: {cmd!r} → {reason}")

    def test_overwrite_and_delete_of_a_charter_are_refused_for_ceo(self):
        j = f"{self.base}/.claude/roles/judge.md"
        for cmd in (f'cp "{SRC}" .claude/roles/judge.md', f'cp "{SRC}" "{j}"', f'cp -t "{self.base}/.claude/roles" "{SRC}"',
                    f'echo x > {j}', f'cat x | tee {j}', f'mv /tmp/x {j}', 'rm .claude/roles/judge.md', f'rm -f "{j}"'):
            self.no(cmd)
        self.no("Set-Content .claude\\roles\\judge.md x", "PowerShell")

    def test_reading_notes_new_files_and_the_sync_command_are_free(self):
        for cmd in (f'cp {SRC} /tmp/x', 'cp .claude/roles/judge.md /tmp/x', 'cat .claude/roles/judge.md', 'grep -rn x .claude/roles',
                    'cp x .claude/roles/notes/ceo.md', 'echo x >> .claude/roles/notes/ceo.md', 'echo x > .claude/roles/new-role.md',
                    f'python "{PLUGIN}/.claude/dispatcher/tickets.py" --project "{self.base}" charter sync --ref В-1',
                    'git status --short .claude/roles'):
            self.ok(cmd)

    def test_first_rpv_init_still_creates_charters(self):
        """Блок Bash из commands/rpv-init.md (его гоняет CEO) называет `.claude/roles/$b` — создание нового файла не закрыто."""
        text = (ROOT / "commands" / "rpv-init.md").read_text(encoding="utf-8")
        script = re.search(r"```\n(.*?)\n```", text, re.S).group(1)
        self.assertIn(".claude/roles/$b", script)
        self.assertNotIn("charter sync", self.shell(script) or "")             # прочие правила стража к блоку — не TK-029

    def test_write_and_edit_tools_are_refused_for_ceo_with_the_same_hint(self):
        for tool, key in (("Write", "file_path"), ("Edit", "file_path"), ("MultiEdit", "file_path"), ("NotebookEdit", "notebook_path")):
            reason = dg.check_tool({"tool_name": tool, "cwd": self.d, "tool_input": {key: f"{self.base}/.claude/roles/judge.md"}})
            self.assertIn("charter sync", reason or "", tool)
            self.assertIsNone(dg.check_tool({"tool_name": tool, "cwd": self.d, "tool_input": {key: f"{self.base}/.claude/roles/notes/ceo.md"}}), tool)

    def test_project_addons_are_written_by_the_command_only(self):
        """TK-038: `.claude/roles/project/<роль>.md` закрыт CEO как устав — и существующий, и новый файл; чтение — свободно."""
        a = f"{self.base}/.claude/roles/project/judge.md"
        for cmd in (f'echo x > {a}', f'cp /tmp/x {a}', 'cp /tmp/x .claude/roles/project/engineer.md', f'cat /tmp/x | tee {a}'):
            self.no(cmd)
        for cmd in (f'cat {a}', 'grep -rn "правило" .claude/roles/project/', 'cp .claude/roles/project/judge.md /tmp/x',
                    f'python "{PLUGIN}/.claude/dispatcher/tickets.py" --project "{self.base}" charter addon judge --text "x" --ref В-1'):
            self.ok(cmd)
        for tool, key in (("Write", "file_path"), ("Edit", "file_path")):
            reason = dg.check_tool({"tool_name": tool, "cwd": self.d, "tool_input": {key: a}})
            self.assertIn("charter addon", reason or "", tool)

    def test_readme_table_rows_stay_editable(self):
        ev = lambda old, new: {"tool_name": "Edit", "cwd": self.d, "tool_input": {
            "file_path": f"{self.base}/.claude/roles/README.md", "old_string": old, "new_string": new}}
        self.assertIsNone(dg.check_tool(ev("| a | b |", "| a | c |")))
        self.assertIsNotNone(dg.check_tool(ev("правило", "другое")))

    def test_the_rule_is_for_the_ceo_session_only(self):
        self.as_role("engineer")
        reason = self.shell(f'cp "{SRC}" .claude/roles/judge.md')
        self.assertNotIn("Устав (", reason or "")


if __name__ == "__main__":
    unittest.main()
