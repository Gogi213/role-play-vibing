"""TK-029 (В-12): сессия CEO не пишет уставы `.claude/roles/*.md` никаким путём (cp, mv, tee, `>`, python, sed, install, rsync,
Write/Edit) — отказ с подсказкой `tickets.py charter sync`. Блокнот, чтение и создание нового файла (`/rpv-init`) — можно."""
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
        fields = ("LOCAL_ROOTS", "WRITE_HOME_ROOTS", "PROJECT_DIR", "PROTECTED", "PROTECTED_ERROR", "REMOTE_ROOTS", "STAGE", "CLOSED_RE")
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

    def test_every_way_to_write_judge_md_is_refused_for_ceo(self):
        j = f"{self.base}/.claude/roles/judge.md"
        for cmd in (f'cp "{SRC}" .claude/roles/judge.md', f'cp "{SRC}" "{j}"', f'cp -f "{SRC}" {j}', f'cp -t "{self.base}/.claude/roles" "{SRC}"',
                    f'cp -r "{PLUGIN}/templates/roles/." "{self.base}/.claude/roles/"', f'cp "{PLUGIN}/templates/roles/"*.md .claude/roles/',
                    f'cp x .claude/roles/*.md', f'mv /tmp/x {j}', f'echo x > {j}', f'echo x >> {j}', f'cat "{SRC}" | tee {j}',
                    f'python -c "open(r\'{j}\', \'w\').write(\'x\')"', f'python -c "import shutil; shutil.copy(r\'{SRC}\', r\'{j}\')"',
                    f'sed -i s/a/b/ {j}', f'perl -pi -e s/a/b/ {j}', f'install -m 644 "{SRC}" "{j}"', f'rsync -a "{SRC}" "{j}"',
                    f'ln -sf "{SRC}" "{j}"', f'dd if="{SRC}" of="{j}"', f'cd "{self.base}" && cp "{SRC}" .claude/roles/judge.md',
                    f'P="{PLUGIN}"; cp "$P/templates/roles/judge.md" {j}'):
            self.no(cmd)

    def test_powershell_and_windows_paths(self):
        j = f"{self.d}\\.claude\\roles\\judge.md"
        for cmd in (f'Copy-Item "{SRC}" "{j}"', f'Set-Content -Path "{j}" -Value x', f'"x" | Out-File "{j}"',
                    f'Move-Item x "{j}" -Force'):
            self.no(cmd, "PowerShell")
        self.no(f'cp "{SRC}" "{j}"')

    def test_wrong_cwd_does_not_hide_a_relative_target_that_exists(self):
        self.no(f'cp "{SRC}" .claude/roles/judge.md', cwd=self.d)
        outside = tempfile.mkdtemp(prefix="dg-ch-out-", dir=str(Path.home()))
        self.addCleanup(shutil.rmtree, outside, True)
        self.ok(f'cp "{SRC}" .claude/roles/judge.md', cwd=outside)                 # там файла нет — создаётся новый (cp сам упадёт)

    def test_reading_notes_new_files_and_the_sync_command_are_free(self):
        j = f"{self.base}/.claude/roles/judge.md"
        for cmd in (f'cat {j}', 'grep -rn "правило" .claude/roles/*.md', 'ls -la .claude/roles', f'diff "{SRC}" {j}', 'sed -n 1,5p .claude/roles/judge.md',
                    f'python -c "print(open(r\'{j}\', encoding=\'utf-8\').read())"', 'wc -l .claude/roles/judge.md > /dev/null',
                    'echo x > .claude/roles/notes/ceo.md', 'cp x .claude/roles/notes/y.md', f'cp "{SRC}" .claude/roles/new-role.md',
                    f'python "{PLUGIN}/.claude/dispatcher/tickets.py" --project "{self.base}" charter sync --file judge.md',
                    f'python tickets.py comment TK-1 --author ceo --text "cp x .claude/roles/judge.md"', 'git status --short .claude/roles'):
            self.ok(cmd)

    def test_first_rpv_init_still_creates_charters(self):
        """Блок Bash из commands/rpv-init.md (его гоняет CEO) называет `.claude/roles/$b` — создание нового файла не закрыто."""
        text = (ROOT / "commands" / "rpv-init.md").read_text(encoding="utf-8")
        script = re.search(r"```\n(.*?)\n```", text, re.S).group(1)
        self.assertIn(".claude/roles/$b", script)
        from dguard.policy import charter_shell_write
        self.assertIsNone(charter_shell_write("Bash", {"command": script}, self.d))      # прочие правила стража к блоку — не TK-029

    def test_write_and_edit_tools_are_refused_for_ceo_with_the_same_hint(self):
        for tool, key in (("Write", "file_path"), ("Edit", "file_path"), ("MultiEdit", "file_path"), ("NotebookEdit", "notebook_path")):
            reason = dg.check_tool({"tool_name": tool, "cwd": self.d, "tool_input": {key: f"{self.base}/.claude/roles/judge.md"}})
            self.assertIn("charter sync", reason or "", tool)
            self.assertIsNone(dg.check_tool({"tool_name": tool, "cwd": self.d, "tool_input": {key: f"{self.base}/.claude/roles/notes/ceo.md"}}), tool)

    def test_readme_table_rows_stay_editable(self):
        ev = lambda old, new: {"tool_name": "Edit", "cwd": self.d, "tool_input": {
            "file_path": f"{self.base}/.claude/roles/README.md", "old_string": old, "new_string": new}}
        self.assertIsNone(dg.check_tool(ev("| a | b |", "| a | c |")))
        self.assertIsNotNone(dg.check_tool(ev("правило", "другое")))

    def test_charter_journal_is_written_by_the_command_only(self):
        for role in (None, "engineer"):
            self.as_role(role)
            reason = self.shell("echo '[]' > .claude/dispatcher/charter-log.json")
            self.assertIn("charter-log.json", reason or "", role)
            for tool in ("Write", "Edit"):
                reason = dg.check_tool({"tool_name": tool, "cwd": self.d, "tool_input": {"file_path": f"{self.base}/.claude/dispatcher/charter-log.json"}})
                self.assertIn("charter-log.json", reason or "", (role, tool))

    def test_the_rule_is_for_the_ceo_session_only(self):
        self.as_role("engineer")
        reason = self.shell(f'cp "{SRC}" .claude/roles/judge.md')
        self.assertNotIn("Устав (", reason or "")


if __name__ == "__main__":
    unittest.main()
