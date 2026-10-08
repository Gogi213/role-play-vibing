"""Тесты `tickets.py result` и таблицы маршрутов (TK-079 п.0): по строке таблицы и по каждому отказу."""
from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_SANDBOX = tempfile.mkdtemp(prefix="rpv-test-proj-")
os.makedirs(os.path.join(_SANDBOX, ".claude", "roles"))
os.environ["CLAUDE_PROJECT_DIR"] = _SANDBOX
os.environ["RPV_BUS_DISABLE"] = "1"
os.environ["RPV_CI_REPO"] = "o/r"
atexit.register(shutil.rmtree, _SANDBOX, True)
sys.path.insert(0, str(Path(__file__).resolve().parent))
import dispatch as D  # noqa: E402
import routes as R  # noqa: E402
import ticket as T  # noqa: E402
import tickets as TK  # noqa: E402

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone(timedelta(hours=4)))
SHA = "a" * 40


class ResultTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        (base / "tickets").mkdir()
        self._orig = (D.TICKETS_DIR, D.STATE_FILE, TK.TICKETS_DIR, TK.PROJECT_ROOT)
        D.TICKETS_DIR, D.STATE_FILE, TK.TICKETS_DIR, TK.PROJECT_ROOT = base / "tickets", base / "state.json", base / "tickets", base
        self.base = base
        self.path = T.create_ticket(D.TICKETS_DIR, owner="engineer", title="Т", status="todo", now=NOW)
        T.write_header_updates(self.path, {"status": "in_progress", "reviewer": "judge"}, now=NOW)
        self.tid = self.path.stem
        self._role = os.environ.get("RPV_ROLE")
        import merge_rule
        self._chk = merge_rule.check_pr
        merge_rule.check_pr = lambda repo, n, sha, gh=None: ""

    def tearDown(self):
        import merge_rule
        merge_rule.check_pr = self._chk
        D.TICKETS_DIR, D.STATE_FILE, TK.TICKETS_DIR, TK.PROJECT_ROOT = self._orig
        if self._role is None:
            os.environ.pop("RPV_ROLE", None)
        else:
            os.environ["RPV_ROLE"] = self._role
        self.tmp.cleanup()

    def res(self, role, result, **kw):
        os.environ.pop("ALPHA_ROLE", None)
        os.environ["RPV_ROLE"] = role
        a = dict(id=self.tid, result=result, why="потому что", pr=None, sha=None, path=None, form=None)
        a.update(kw)
        return TK.cmd_result(type("A", (), a)())

    def tkt(self):
        return T.read_ticket(self.path)

    # --- строки таблицы маршрутов
    def test_pr_goes_to_judge_in_review_and_records_pr(self):
        self.assertEqual(self.res("engineer", "pr", pr=7, sha=SHA), 0)
        t = self.tkt()
        self.assertEqual((t.status, t.header.get("next"), t.header.get("pr")), ("in_review", "judge", "7"))
        self.assertIn("[итог: pr]", t.log[-1].text)
        self.res("engineer", "pr", pr=9, sha=SHA)
        self.assertEqual(self.tkt().header.get("pr"), "7, 9")

    def test_done_with_reviewer_goes_to_reviewer_else_closes(self):
        (self.base / "out.md").write_text("x", encoding="utf-8")
        self.assertEqual(self.res("engineer", "done", path="out.md"), 0)
        self.assertEqual((self.tkt().status, self.tkt().header.get("next")), ("in_review", "judge"))
        T.write_header_updates(self.path, {"reviewer": "", "next": ""}, now=NOW)
        self.assertEqual(self.res("engineer", "done", path="out.md"), 0)
        self.assertEqual(self.tkt().status, "done")

    def test_accept_writes_verdict_list(self):
        self.assertEqual(self.res("judge", "accept", pr=7, sha=SHA), 0)
        self.assertEqual(self.tkt().header.get("accepted"), f"7@{SHA}")

    def test_accept_pr_writes_result_entry_for_strict_stop(self):
        self.assertEqual(self.res("judge", "accept", pr=7, sha=SHA), 0)
        last = self.tkt().log[-1]
        self.assertEqual(last.author, "judge")
        self.assertTrue(last.text.lstrip().startswith("[итог: accept]"), last.text)

    def test_accept_pr_waits_for_merge_then_wakes_owner_not_judge(self):
        T.write_header_updates(self.path, {"status": "in_review", "next": "judge"}, now=NOW)
        self.assertEqual(self.res("judge", "accept", pr=7, sha=SHA), 0)
        t = self.tkt()
        self.assertEqual((t.status, t.header.get("wait_for"), t.header.get("next")), ("waiting", "merged:o/r#7", ""))
        import merge_rule as M
        orig = M.merged_done
        try:
            M.merged_done = lambda repo, n, gh=None: False
            self.assertFalse(D.check_wait_for("merged:o/r#7"))
            M.merged_done = lambda repo, n, gh=None: True
            self.assertTrue(D.check_wait_for("merged:o/r#7"))
        finally:
            M.merged_done = orig

    def test_judge_accept_and_return_without_pr_by_artifact(self):
        (self.base / "review.md").write_text("x", encoding="utf-8")
        T.write_header_updates(self.path, {"status": "in_review"}, now=NOW)
        self.assertEqual(self.res("judge", "return", path="review.md"), 0)
        self.assertEqual((self.tkt().status, self.tkt().header.get("next")), ("in_progress", "engineer"))
        T.write_header_updates(self.path, {"status": "in_review"}, now=NOW)
        self.assertEqual(self.res("judge", "accept", path="review.md"), 0)  # владелец итога done не сдавал → не закрываем
        self.assertEqual((self.tkt().status, self.tkt().header.get("next")), ("in_progress", "engineer"))
        (self.base / "out.md").write_text("x", encoding="utf-8")
        self.res("engineer", "done", path="out.md")
        self.assertEqual(self.res("judge", "accept", path="review.md"), 0)  # последний итог владельца — done → закрыть
        self.assertEqual((self.tkt().status, self.tkt().header.get("next")), ("done", ""))
        self.refused("judge", "accept", path="нет-такого.md")
        self.refused("judge", "accept")

    def test_accept_refuses_without_repo_or_unknown_pr(self):
        import merge_rule
        os.environ.pop("RPV_CI_REPO")
        try:
            self.refused("judge", "accept", pr=7, sha=SHA)  # репо не задан — без догадки по cwd
            self.assertEqual(self.res("judge", "accept", pr=7, sha=SHA, repo="x/y"), 0)
            merge_rule.check_pr = lambda repo, n, sha, gh=None: "PR не найден"
            self.refused("judge", "accept", pr=7, sha=SHA, repo="x/y")  # PR нет в репо — ничего не пишем
        finally:
            os.environ["RPV_CI_REPO"] = "o/r"

    def test_wait_ticket_cycle_writes_nothing(self):
        other = T.create_ticket(D.TICKETS_DIR, owner="researcher", title="Д", status="todo", now=NOW)
        T.write_header_updates(other, {"status": "waiting", "wait_for": f"ticket:{self.tid}"}, now=NOW)
        self.refused("engineer", "wait", form=f"ticket:{other.stem}")

    def test_return_goes_to_owner_in_progress(self):
        T.write_header_updates(self.path, {"status": "in_review"}, now=NOW)
        self.assertEqual(self.res("judge", "return", sha=SHA), 0)
        t = self.tkt()
        self.assertEqual((t.status, t.header.get("next")), ("in_progress", "engineer"))

    def test_blocked_goes_to_judge_ask_owner_to_owner(self):
        self.assertEqual(self.res("engineer", "blocked"), 0)
        self.assertEqual((self.tkt().status, self.tkt().header.get("next")), ("blocked", "judge"))
        self.assertEqual(self.res("judge", "blocked"), 0)  # Судья сам встал — Судье некуда, ждёт владельца
        self.assertEqual((self.tkt().status, self.tkt().header.get("next")), ("blocked", ""))
        self.assertEqual(self.res("engineer", "ask-owner"), 0)
        self.assertEqual((self.tkt().status, self.tkt().header.get("next")), ("needs_owner", ""))

    def test_wait_sets_waiting_with_form(self):
        self.assertEqual(self.res("engineer", "wait", form="file:flags/rpv-flag"), 0)
        t = self.tkt()
        self.assertEqual((t.status, t.header.get("wait_for")), ("waiting", "file:flags/rpv-flag"))

    # --- отказы
    def refused(self, role, result, **kw):
        before = self.path.read_text(encoding="utf-8")
        self.assertEqual(self.res(role, result, **kw), 1)
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)  # отказ ничего не пишет

    def test_refusals(self):
        self.refused("engineer", "pr", pr=7)                      # нет sha
        self.refused("engineer", "pr", sha=SHA)                   # нет номера PR
        self.refused("engineer", "pr", pr=7, sha="zzz")           # sha не hex
        self.refused("engineer", "done")                          # нет пути
        self.refused("engineer", "done", path="нет-такого.md")    # пути нет на диске
        self.refused("engineer", "wait")                          # нет формы
        self.refused("engineer", "wait", form="когда-нибудь")     # форма неизвестна
        self.refused("judge", "return")                           # нет sha
        self.refused("engineer", "blocked", why="")               # нет why
        self.refused("engineer", "blocked", why="я" * 201)        # why длиннее 200
        self.refused("engineer", "готово")                        # неизвестный итог
        self.refused("engineer", "accept", pr=7, sha=SHA)         # accept не для инженера
        self.refused("judge", "pr", pr=7, sha=SHA)                # pr не для Судьи
        self.refused("", "blocked")                               # не роль
        self.refused("", "continue")                              # не роль
        self.refused("judge", "continue")                         # continue не для Судьи
        self.refused("", "done")                                  # CEO: done требует --path

    # --- TK-090: закрытие CEO, done после принятых PR, continue
    def test_ceo_closes_ticket_with_done(self):
        (self.base / "out.md").write_text("x", encoding="utf-8")
        self.assertEqual(self.res("", "done", path="out.md"), 0)
        t = self.tkt()
        self.assertEqual((t.status, t.header.get("next")), ("done", ""))
        self.assertEqual(t.log[-1].author, "ceo")

    def test_engineer_done_after_landed_prs_still_goes_to_judge(self):  # done несёт и не-PR работу — Судья принимает финал
        (self.base / "out.md").write_text("x", encoding="utf-8")
        T.write_header_updates(self.path, {"pr": "7"}, now=NOW)
        T.append_log(self.path, "merge", "PR #7 влит в main на голове aaaaaaa (проверено: merged).", now=NOW)
        self.res("engineer", "done", path="out.md")
        t = self.tkt()
        self.assertEqual((t.status, t.header.get("next")), ("in_review", "judge"))

    def test_landed_prs_only_from_merge_author(self):
        import merge_rule
        T.append_log(self.path, "engineer", "PR #7 влит в main на голове aaaaaaa", now=NOW)
        self.assertEqual(merge_rule.landed_prs(self.tkt()), set())
        T.append_log(self.path, "merge", "PR #9 влит в main на голове bbbbbbb (проверено: merged).", now=NOW)
        self.assertEqual(merge_rule.landed_prs(self.tkt()), {9})

    def test_ceo_close_and_comment_after_do_not_wake_judge(self):  # TK-090 б
        (self.base / "out.md").write_text("x", encoding="utf-8")
        self.res("", "done", path="out.md")
        self.assertIsNone(D.decide(self.tkt(), {}, NOW))
        TK.cmd_comment(type("A", (), {"id": self.tid, "author": "ceo", "text": "закрыто", "next": None})())
        t = self.tkt()
        self.assertEqual(t.status, "done")
        self.assertIsNone(D.decide(t, {}, NOW))

    def test_continue_keeps_role_and_is_capped(self):
        for _ in range(R.CONTINUE_MAX):
            self.assertEqual(self.res("engineer", "continue"), 0)
        t = self.tkt()
        self.assertEqual((t.status, t.header.get("next")), ("in_progress", "engineer"))
        self.refused("engineer", "continue")                    # шестой подряд
        self.assertEqual(self.res("engineer", "blocked"), 0)
        self.assertEqual(self.res("engineer", "continue"), 0)   # серию оборвал другой итог

    def test_check_unit(self):
        self.assertEqual(R.check("engineer", "blocked", "x"), "")
        self.assertIn("неизвестный", R.check("engineer", "zzz", "x"))


if __name__ == "__main__":
    unittest.main()
