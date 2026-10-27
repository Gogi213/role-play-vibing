"""TK-040: `git push` в main/master с коммитами вне принятого тикета — отказ стража с перечнем sha (`guard_push_scope`).

Настоящие репозитории во временном каталоге: «удалённый» (bare) и рабочая копия. Запуск: `python -m pytest .claude/hooks/test_push_scope.py`.
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# корень — окружением ДО импорта, как в test_delete_guard: модули стража общие на весь прогон; чужое значение возвращаем
# (test_role_hooks держит там проект-пустышку: подмена ломает его подпроцессы)
_PREV = os.environ.get("CLAUDE_PROJECT_DIR")
os.environ["CLAUDE_PROJECT_DIR"] = r"C:\visual projects\proj"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import delete_guard as dg  # noqa: E402
from dguard import config as cfg, push_scope  # noqa: E402
if _PREV is None:
    os.environ.pop("CLAUDE_PROJECT_DIR", None)
else:
    os.environ["CLAUDE_PROJECT_DIR"] = _PREV

TID = "TK-7"
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}


def run(cwd, *args):
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, encoding="utf-8",
                       env={**os.environ, **GIT_ENV})
    assert r.returncode == 0, (args, r.stderr)
    return r.stdout.strip()


class PushScope(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="dg-scope-")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.project = root / "project"
        (self.project / ".claude" / "tickets").mkdir(parents=True)
        bare, self.work = root / "origin.git", root / "work"
        run(root, "init", "-q", "--bare", "-b", "main", str(bare))
        run(root, "clone", "-q", str(bare), str(self.work))
        self.commit("база")
        run(self.work, "push", "-q", "origin", "main")
        for p in (mock.patch.dict(os.environ, {"RPV_ROLE": "judge", "RPV_TICKET": TID}),
                  mock.patch.object(cfg, "PROJECT_DIR", str(self.project)),
                  mock.patch.object(cfg, "PUSH_SCOPE", True)):
            p.start()
            self.addCleanup(p.stop)
        self.write_ticket(TID, allow="publish (В-1)")

    def commit(self, subject, name=None):
        (self.work / (name or subject.replace(" ", "_").replace("[", "").replace("]", ""))).write_text(subject, encoding="utf-8")
        run(self.work, "add", "-A")
        run(self.work, "commit", "-q", "-m", subject)
        return run(self.work, "rev-parse", "HEAD")

    def write_ticket(self, tid, allow=None, accepted=None):
        head = [f"id: {tid}", "status: in_review"] + ([f"allow: {allow}"] if allow else []) + (
            [f"accepted: {accepted}"] if accepted else [])
        (self.project / ".claude" / "tickets" / f"{tid}.md").write_text("---\n" + "\n".join(head) + "\n---\n\n## Лог\n", encoding="utf-8")

    def reason(self, cmd="git push origin main", cwd=None):
        return dg.check_tool({"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(cwd or self.work)})

    def as_ceo(self):
        os.environ.pop("RPV_ROLE", None)
        os.environ.pop("RPV_TICKET", None)

    # ------------------------------------------------------------------------------------------ из задачи
    def test_ceo_commit_over_accepted_denied_with_sha(self):
        accepted = self.commit("Принятая правка")
        self.write_ticket(TID, allow="publish (В-1)", accepted=f"150@{accepted}")
        ceo = self.commit("[CEO] правка CLAUDE.md")
        reason = self.reason()
        self.assertIsNotNone(reason)
        self.assertIn(ceo[:8], reason)
        self.assertIn("[CEO] правка CLAUDE.md", reason)
        self.assertNotIn(accepted[:8], reason)
        for hint in ("Завести", "тикет", "пушить отдельно", "guard_push_scope"):
            self.assertIn(hint, reason)

    def test_only_accepted_commits_pass(self):
        self.commit("Первая")
        second = self.commit("Вторая")
        self.write_ticket(TID, allow="publish (В-1)", accepted=f"150@{second}")      # в пуше два коммита, оба — до принятой головы
        self.assertIsNone(self.reason())

    # ------------------------------------------------------------------------------------------ кто и что считается своим
    def test_flag_off_keeps_old_behavior(self):
        self.commit("[CEO] чужое")
        with mock.patch.object(cfg, "PUSH_SCOPE", False):
            self.assertIsNone(self.reason())

    def test_ceo_session_checked_too_against_any_accepted_ticket(self):
        head = self.commit("Принятая в другом тикете")
        self.write_ticket("TK-9", accepted=f"151@{head}")
        self.as_ceo()
        self.assertIsNone(self.reason())
        extra = self.commit("[CEO] мимо тикета")
        self.assertIn(extra[:8], self.reason())

    def test_role_counts_only_its_own_ticket(self):
        head = self.commit("Принятая в другом тикете")
        self.write_ticket("TK-9", accepted=f"151@{head}")
        self.assertIn(head[:8], self.reason())                                   # судья TK-7: чужой тикет не в счёт

    def test_no_accepted_means_every_commit_is_foreign(self):
        head = self.commit("Без приёмки")
        self.assertIn(head[:8], self.reason())
        self.as_ceo()
        self.assertIn(head[:8], self.reason())

    def test_nothing_to_push_passes(self):
        self.assertIsNone(self.reason())

    def test_merge_of_accepted_head_is_own(self):
        run(self.work, "checkout", "-q", "-b", "feat")
        head = self.commit("Правка ветки")
        run(self.work, "checkout", "-q", "main")
        run(self.work, "merge", "-q", "--no-ff", "-m", "Merge PR #150", "feat")
        self.write_ticket(TID, allow="publish (В-1)", accepted=f"150@{head[:10]}")       # sha в приёмке бывает короткий
        self.assertIsNone(self.reason())

    def test_merge_of_foreign_branch_denied(self):
        run(self.work, "checkout", "-q", "-b", "feat")
        foreign = self.commit("Чужая ветка")
        run(self.work, "checkout", "-q", "main")
        run(self.work, "merge", "-q", "--no-ff", "-m", "Merge чужой", "feat")
        reason = self.reason()
        self.assertIn(foreign[:8], reason)
        self.assertIn("Merge чужой", reason)

    def test_second_accepted_pr_of_same_ticket(self):
        a = self.commit("PR один")
        b = self.commit("PR два")
        self.write_ticket(TID, allow="publish (В-1)", accepted=f"150@{a}, 151@{b}")
        self.assertIsNone(self.reason())

    def test_long_list_is_cut(self):
        for i in range(push_scope.SHOW_FOREIGN + 2):
            self.commit(f"Чужой {i}")
        self.assertIn("и ещё 2", self.reason())

    # ------------------------------------------------------------------------------------------ формы команды
    def test_push_forms_to_main_all_checked(self):
        self.commit("[CEO] чужое")
        for cmd in ("git push origin main", "git push origin HEAD:main", "git push origin HEAD:refs/heads/main", "git push",
                    "git push origin", "git push origin HEAD", "git push --all", "git push --follow-tags origin main",
                    "git push origin v1.0 main", f'git -C "{self.work}" push origin main'):
            self.assertIn("вне принятого тикета", self.reason(cmd, cwd=self.work.parent if "-C" in cmd else self.work) or "", cmd)

    def test_working_branch_and_dry_run_not_checked(self):
        self.commit("[CEO] чужое")
        for cmd in ("git push origin HEAD:feat/x", "git push -u origin feat/x", "git push --dry-run origin main", "git push --tags"):
            reason = self.reason(cmd)
            self.assertFalse(reason and "вне принятого тикета" in reason, cmd)

    def test_main_from_working_branch_checks_local_main(self):
        run(self.work, "checkout", "-q", "-b", "feat")
        self.commit("Коммит ветки")
        self.assertIsNone(self.reason("git push origin main"))                    # main не двигался: уходить нечему
        self.assertIsNone(self.reason("git push origin feat"))

    # ------------------------------------------------------------------------------------------ не сверить — отказ
    def test_no_tracking_ref_denied_with_fetch_hint(self):
        run(self.work, "update-ref", "-d", "refs/remotes/origin/main")
        reason = self.reason()
        self.assertIn("git fetch origin", reason)

    def test_unknown_repo_dir_denied(self):
        reason = self.reason("git push origin main", cwd=Path(self.tmp.name) / "нет-такого")
        self.assertIn("не сверить", reason)

    def test_ssh_push_denied(self):
        reason = dg.check_tool({"tool_name": "Bash", "tool_input": {"command": "ssh host 'cd ~/p && git push origin main'"}, "cwd": str(self.work)})
        self.assertIn("не сверить", reason)

    def test_url_remote_denied(self):
        self.assertIn("не сверить", self.reason("git push https://example.org/r.git main"))

    def test_role_without_ticket_denied(self):
        os.environ.pop("RPV_TICKET")
        self.assertIsNotNone(self.reason())

    # ------------------------------------------------------------------------------------------ согласие роли — отдельно
    def test_role_without_consent_still_gets_publication_reason(self):
        self.write_ticket(TID)
        self.assertIn("Публикация", self.reason())

    def test_consent_does_not_skip_commit_check(self):
        head = self.commit("[CEO] чужое")
        reason = self.reason()
        self.assertIn(head[:8], reason)
        self.assertNotIn("согласия владельца", reason)

    def test_journal_group_is_publish(self):
        self.commit("[CEO] чужое")
        trace = []
        dg.check_tool({"tool_name": "Bash", "tool_input": {"command": "git push origin main"}, "cwd": str(self.work)}, trace)
        self.assertEqual(trace[-1], ("publish", "deny"))


class Setting(unittest.TestCase):
    def test_flag_default_off_and_bool_only(self):
        self.assertEqual(cfg.push_scope_rule({}), (False, None))
        self.assertEqual(cfg.push_scope_rule({"guard_push_scope": True}), (True, None))
        self.assertEqual(cfg.push_scope_rule({"guard_push_scope": False}), (False, None))
        for bad in ("true", 1, None, ["x"]):
            on, err = cfg.push_scope_rule({"guard_push_scope": bad})
            self.assertFalse(on)
            self.assertIn("guard_push_scope", err)

    def test_config_set_accepts_only_bool(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dispatcher"))
        import rpv_config
        check, _ = rpv_config.KEYS["guard_push_scope"]
        self.assertTrue(check(True) and check(False))
        self.assertFalse(check("true") or check(1))


if __name__ == "__main__":
    unittest.main()
