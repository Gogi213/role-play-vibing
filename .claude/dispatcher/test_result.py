"""Тесты `tickets.py result` и таблицы маршрутов (TK-079 п.0): по строке таблицы и по каждому отказу."""
from __future__ import annotations

import atexit
import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

_SANDBOX = tempfile.mkdtemp(prefix="rpv-test-proj-")
os.makedirs(os.path.join(_SANDBOX, ".claude", "roles"))
os.environ["CLAUDE_PROJECT_DIR"] = _SANDBOX
atexit.register(shutil.rmtree, _SANDBOX, True)
sys.path.insert(0, str(Path(__file__).resolve().parent))
import dispatch as D  # noqa: E402
import config as CFG  # noqa: E402
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
        self._orig = (CFG.TICKETS_DIR, CFG.STATE_FILE, TK.TICKETS_DIR, TK.PROJECT_ROOT)
        CFG.TICKETS_DIR, CFG.STATE_FILE, TK.TICKETS_DIR, TK.PROJECT_ROOT = base / "tickets", base / "state.json", base / "tickets", base
        self.base = base
        self.path = T.create_ticket(CFG.TICKETS_DIR, owner="engineer", title="Т", status="todo", now=NOW)
        T.write_header_updates(self.path, {"status": "in_progress", "reviewer": "judge"}, now=NOW)
        self.tid = self.path.stem
        self._role = os.environ.get("RPV_ROLE")
        import merge_rule
        self._chk = merge_rule.check_pr
        merge_rule.check_pr = lambda repo, n, sha, gh=None: ""
        self.settings = {"ci_repo": "o/r"}  # .claude/rpv.json проекта
        patch = mock.patch.object(D.P, "settings", lambda root=None: self.settings)
        patch.start()
        self.addCleanup(patch.stop)

    def tearDown(self):
        import merge_rule
        merge_rule.check_pr = self._chk
        CFG.TICKETS_DIR, CFG.STATE_FILE, TK.TICKETS_DIR, TK.PROJECT_ROOT = self._orig
        if self._role is None:
            os.environ.pop("RPV_ROLE", None)
        else:
            os.environ["RPV_ROLE"] = self._role
        self.tmp.cleanup()

    def res(self, role, result, **kw):
        os.environ.pop("RPV_ROLE", None)
        os.environ["RPV_ROLE"] = role
        a = dict(id=self.tid, result=result, why="потому что", pr=None, sha=None, path=None, form=None, tail="-")
        a.update(kw)  # tail="-" по умолчанию: ворота хвостов проверяются отдельными тестами ниже
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

    def test_done_goes_to_judge_by_default_closes_only_haiku_and_ceo(self):  # V2-1: Судья проверяет каждый done
        (self.base / "out.md").write_text("x", encoding="utf-8")
        self.assertEqual(self.res("engineer", "done", path="out.md"), 0)
        self.assertEqual((self.tkt().status, self.tkt().header.get("next")), ("in_review", "judge"))
        T.write_header_updates(self.path, {"reviewer": "", "next": "", "executor": "haiku"}, now=NOW)  # механическая задача
        self.assertEqual(self.res("engineer", "done", path="out.md"), 0)
        t = self.tkt()
        self.assertEqual((t.status, t.header.get("next"), t.reviewer), ("done", "", ""))
        T.write_header_updates(self.path, {"status": "in_progress", "executor": ""}, now=NOW)
        self.assertEqual(self.res("", "done", path="out.md"), 0)  # CEO закрывает сам
        self.assertEqual(self.tkt().status, "done")

    def test_done_on_old_ticket_without_reviewer_goes_to_judge_and_writes_reviewer(self):
        (self.base / "out.md").write_text("x", encoding="utf-8")
        for role in ("researcher", "engineer"):
            T.write_header_updates(self.path, {"reviewer": "", "next": "", "status": "in_progress", "owner": role}, now=NOW)
            self.assertEqual(self.res(role, "done", path="out.md"), 0)
            t = self.tkt()
            self.assertEqual((t.status, t.header.get("next"), t.reviewer), ("in_review", "judge", "judge"))
        # ревьюер, поставленный явно (не Судья), не перетирается
        T.write_header_updates(self.path, {"reviewer": "engineer", "next": "", "status": "in_progress", "owner": "researcher"}, now=NOW)
        self.assertEqual(self.res("researcher", "done", path="out.md"), 0)
        t = self.tkt()
        self.assertEqual((t.status, t.header.get("next"), t.reviewer), ("in_review", "engineer", "engineer"))

    def test_ticket_from_new_goes_through_judge_on_done(self):  # сквозной: new → done исполнителя → Судья, без флагов
        (self.base / "out.md").write_text("x", encoding="utf-8")
        self.assertEqual(TK.main(["new", "--ref", "В-229", "--owner", "researcher", "--title", "Сквозной тикет", "--force"]), 0)
        self.path = max(CFG.TICKETS_DIR.glob("TK-*.md"))
        self.tid = self.path.stem
        self.assertEqual(self.tkt().reviewer, "judge")
        self.assertEqual(self.res("researcher", "done", path="out.md"), 0)
        t = self.tkt()
        self.assertEqual((t.status, t.header.get("next"), t.reviewer), ("in_review", "judge", "judge"))
        d = D.decide(t, {}, NOW)
        self.assertEqual((d.role, d.reason), ("judge", "next"))

    def test_pr_on_old_ticket_without_reviewer_writes_judge_except_haiku(self):
        T.write_header_updates(self.path, {"reviewer": ""}, now=NOW)
        self.assertEqual(self.res("engineer", "pr", pr=7, sha=SHA), 0)
        t = self.tkt()
        self.assertEqual((t.status, t.header.get("next"), t.reviewer), ("in_review", "judge", "judge"))
        T.write_header_updates(self.path, {"reviewer": "", "executor": "haiku"}, now=NOW)  # PR всё равно к Судье, но шапку не трогаем
        self.assertEqual(self.res("engineer", "pr", pr=9, sha=SHA), 0)
        t = self.tkt()
        self.assertEqual((t.header.get("next"), t.reviewer), ("judge", ""))

    def test_route_default_reviewer_is_pure(self):
        self.assertEqual(R.route("engineer", "done", "engineer"),
                         {"status": "in_review", "next": "judge", "reviewer": "judge"})
        self.assertEqual(R.route("engineer", "done", "engineer", "judge"), {"status": "in_review", "next": "judge"})
        self.assertEqual(R.route("engineer", "done", "engineer", executor="haiku"), {"status": "done", "next": ""})
        self.assertEqual(R.route("judge", "done", "judge"), {"status": "done", "next": ""})
        self.assertEqual(R.route("ceo", "done", "engineer"), {"status": "done", "next": ""})
        self.assertEqual(R.default_reviewer(), "judge")
        self.assertEqual(R.default_reviewer("haiku"), "")

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

    def test_accept_with_sha_and_existing_path_but_no_pr_is_accept_by_artifact(self):
        """Замер 14.10: промпт учил `accept --sha`, код требовал ещё и --pr — у каждого второго запуска Судьи отказ. Теперь --sha рядом
        с существующим --path без --pr — приёмка без PR (sha попадает в журнал пометкой); без --path отказ с подсказкой."""
        (self.base / "review.md").write_text("x", encoding="utf-8")
        (self.base / "out.md").write_text("x", encoding="utf-8")
        self.res("engineer", "done", path="out.md")
        self.assertEqual(self.res("judge", "accept", path="review.md", sha=SHA), 0)
        t = self.tkt()
        self.assertEqual((t.status, t.header.get("next")), ("done", ""))
        text = self.path.read_text(encoding="utf-8")
        self.assertIn("проверка: review.md", text)
        self.assertIn(f"голова {SHA[:7]}", text)
        self.assertNotIn("PR #", text.split("[итог: accept]")[-1])
        import contextlib
        import io
        err = io.StringIO()
        T.write_header_updates(self.path, {"status": "in_review"}, now=NOW)
        with contextlib.redirect_stderr(err):
            self.refused("judge", "accept", sha=SHA)             # только sha: артефакта нет, PR нет
        self.assertIn("--path", err.getvalue())
        self.assertIn("--sha без --pr", err.getvalue())

    def test_accept_refuses_without_repo_or_unknown_pr(self):
        import merge_rule
        self.settings = {}
        self.refused("judge", "accept", pr=7, sha=SHA)  # репо не задан — без догадки по cwd
        self.assertEqual(self.res("judge", "accept", pr=7, sha=SHA, repo="x/y"), 0)
        merge_rule.check_pr = lambda repo, n, sha, gh=None: "PR не найден"
        self.refused("judge", "accept", pr=7, sha=SHA, repo="x/y")  # PR нет в репо — ничего не пишем

    def test_wait_ticket_cycle_writes_nothing(self):
        other = T.create_ticket(CFG.TICKETS_DIR, owner="researcher", title="Д", status="todo", now=NOW)
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
        self.assertEqual(self.res("judge", "ask-owner", why_owner="деньги"), 0)
        self.assertEqual((self.tkt().status, self.tkt().header.get("next")), ("needs_owner", ""))

    def test_wait_sets_waiting_with_form(self):
        self.assertEqual(self.res("engineer", "wait", form="file:flags/rpv-flag", by="2099-01-01T00:00+04:00"), 0)
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
        self.refused("engineer", "wait", form="file:flags/rpv-flag")  # TK-117: у file:/host:/job: срок --by обязателен
        self.refused("engineer", "wait", form="file:flags/rpv-flag", by="00:00-")  # срок непонятен
        self.refused("engineer", "wait", form="file:flags/rpv-flag", by="2020-01-01T00:00+04:00")  # срок в прошлом
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

    # --- TK-225: формальное закрытие тикета Судьи по артефакту в git
    def _git(self, *a):
        import subprocess
        subprocess.run(["git", "-C", str(self.base), "-c", "user.name=t", "-c", "user.email=t@t", *a], check=True,
                       capture_output=True)

    def test_judge_owned_ticket_closes_only_by_tracked_clean_artifact(self):
        T.write_header_updates(self.path, {"owner": "judge", "reviewer": ""}, now=NOW)
        self._git("init", "-q")
        (self.base / "art.md").write_text("x", encoding="utf-8")
        (self.base / "wip.md").write_text("x", encoding="utf-8")
        self._git("add", "art.md")
        self._git("commit", "-qm", "a")
        self.refused("judge", "done")                      # нет пути
        self.refused("judge", "done", path="wip.md")       # не tracked
        (self.base / "art.md").write_text("y", encoding="utf-8")
        self.refused("judge", "done", path="art.md")       # изменён в рабочем дереве
        self.refused("judge", "accept", path="wip.md")
        self._git("commit", "-qam", "b")
        self.assertEqual(self.res("judge", "done", path="art.md"), 0)
        t = self.tkt()
        self.assertEqual((t.status, t.header.get("next")), ("done", ""))
        self.assertRegex(t.log[-1].text, r"закрыт по артефакту art\.md@[0-9a-f]{7,}")

    def test_judge_owned_accept_by_artifact_closes_not_loops(self):
        T.write_header_updates(self.path, {"owner": "judge", "reviewer": "", "status": "in_progress"}, now=NOW)
        self._git("init", "-q")
        (self.base / "art.md").write_text("x", encoding="utf-8")
        self._git("add", "art.md")
        self._git("commit", "-qm", "a")
        self.assertEqual(self.res("judge", "accept", path="art.md"), 0)
        self.assertEqual((self.tkt().status, self.tkt().header.get("next")), ("done", ""))

    def test_judge_done_on_other_owner_ticket_still_refused(self):
        (self.base / "art.md").write_text("x", encoding="utf-8")
        self.refused("judge", "done", path="art.md")

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


    def test_comment_next_role_unblocks_blocked(self):  # №15: blocked + next: роль не остаётся записью без запуска
        T.write_header_updates(self.path, {"status": "blocked"}, now=NOW)
        TK.cmd_comment(type("A", (), dict(id=self.tid, author="ceo", text="вернуть", next="engineer"))())
        h = T.read_ticket(self.path).header
        self.assertEqual((h["status"], h["next"]), ("in_progress", "engineer"))
        T.write_header_updates(self.path, {"status": "blocked", "next": ""}, now=NOW)
        TK.cmd_comment(type("A", (), dict(id=self.tid, author="ceo", text="заметка", next=None))())
        self.assertEqual(T.read_ticket(self.path).status, "blocked")

    # --- TK-218
    def test_ask_owner_non_judge_goes_to_judge_judge_to_owner(self):
        for role in ("engineer", "researcher"):
            T.write_header_updates(self.path, {"status": "in_progress", "next": ""}, now=NOW)
            self.assertEqual(self.res(role, "ask-owner"), 0)
            t = self.tkt()
            self.assertEqual((t.status, t.header.get("next")), ("blocked", "judge"))
        T.write_header_updates(self.path, {"status": "in_review", "next": "judge"}, now=NOW)
        self.assertEqual(self.res("judge", "ask-owner", why_owner="необратимое"), 0)
        self.assertEqual((self.tkt().status, self.tkt().header.get("next")), ("needs_owner", ""))

    def test_wait_refuses_already_met_file_unless_allowed(self):
        (self.base / "old.done").write_text("x", encoding="utf-8")
        a = dict(id=self.tid, spec=f"file:{(self.base / 'old.done').as_posix()}", on_met=None, by="23:59")
        self.assertEqual(TK.cmd_wait(type("A", (), a)()), 1)
        self.assertNotEqual(self.tkt().status, "waiting")
        a["allow_met"] = True
        self.assertEqual(TK.cmd_wait(type("A", (), a)()), 0)
        self.assertEqual(self.tkt().status, "waiting")

    def test_new_refuses_similar_recent_title(self):
        self.assertTrue(T.similar_recent_ticket(CFG.TICKETS_DIR, "т", now=NOW))
        self.assertEqual(T.similar_recent_ticket(CFG.TICKETS_DIR, "совсем другое про счёт", now=NOW), "")
        self.assertEqual(T.similar_recent_ticket(CFG.TICKETS_DIR, "т", now=NOW + timedelta(hours=2)), "")

    # --- TK-233
    def test_judge_ask_owner_needs_owner_reason(self):
        T.write_header_updates(self.path, {"status": "in_review", "next": "judge"}, now=NOW)
        self.assertEqual(self.res("judge", "ask-owner"), 1)
        self.assertEqual(self.res("judge", "ask-owner", why_owner="любопытно"), 1)
        self.assertEqual(self.tkt().status, "in_review", "отказ ничего не пишет")
        for why in R.OWNER_REASONS:
            self.assertEqual(R.check("judge", "ask-owner", "x", why_owner=why), "")
        self.assertIn("повод: деньги", (self.res("judge", "ask-owner", why_owner="деньги"), self.tkt().log[-1].text)[1])

    def test_owner_reasons_are_money_direction_irreversible_business_rule(self):  # V2-1: «направление» вместо «видимого»
        self.assertEqual(R.OWNER_REASONS, ("деньги", "направление", "необратимое", "бизнес-правило"))
        self.assertEqual(R.check("judge", "ask-owner", "x", why_owner="направление"), "")
        self.assertIn("деньги|направление|необратимое|бизнес-правило", R.check("judge", "ask-owner", "x", why_owner="видимое"))
        T.write_header_updates(self.path, {"status": "in_review", "next": "judge"}, now=NOW)
        self.assertEqual(self.res("judge", "ask-owner", why_owner="видимое"), 1)
        self.assertEqual(self.tkt().status, "in_review", "отказ ничего не пишет")
        self.assertEqual(self.res("judge", "ask-owner", why_owner="направление"), 0)
        self.assertEqual((self.tkt().status, self.tkt().header.get("next")), ("needs_owner", ""))
        self.assertIn("повод: направление", self.tkt().log[-1].text)

    def test_cli_why_owner_choices_reject_visible(self):
        import contextlib
        import io
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                TK.main(["result", self.tid, "ask-owner", "--why", "x", "--why-owner", "видимое"])

    def test_non_judge_ask_owner_still_routes_to_judge_without_reason(self):
        self.assertEqual(self.res("engineer", "ask-owner"), 0)
        t = self.tkt()
        self.assertEqual((t.status, t.header.get("next")), ("blocked", "judge"))

    def test_new_requires_ref(self):
        def new(**kw):
            a = dict(owner="engineer", title="Новое дело", reviewer=None, effort=None, desc="", wait_for="", force=True,
                     backlog=False, executor=None, kind=None)
            a.update(kw)
            return TK.cmd_new(type("A", (), a)())
        before = len(list(CFG.TICKETS_DIR.glob("TK-*.md")))
        self.assertEqual(new(), 1)
        self.assertEqual(new(ref="да"), 1)
        self.assertEqual(len(list(CFG.TICKETS_DIR.glob("TK-*.md"))), before, "отказ не создаёт файл")
        self.assertEqual(new(ref="В-12"), 0)
        self.assertEqual(new(ref="В-"), 1)
        self.assertEqual(new(ref="В-229"), 0)
        made = max(CFG.TICKETS_DIR.glob("TK-*.md"))
        self.assertIn("Ссылка на задачу владельца: В-229", made.read_text(encoding="utf-8"))

    def test_new_ref_checked_against_owner_registry(self):
        """V2-2: есть реестр владельца — номер В-N/G-NNN в --ref обязан в нём быть; цель — открытой."""
        import contextlib
        import io
        import registry as R

        def new(ref):
            a = dict(owner="engineer", title=f"Дело {ref}", reviewer=None, effort=None, desc="", wait_for="",
                     force=True, backlog=False, executor=None, kind=None, ref=ref)
            with contextlib.redirect_stderr(io.StringIO()):
                return TK.cmd_new(type("A", (), a)())
        root = TK.PROJECT_ROOT
        self.assertEqual(new("G-001"), 1, "целей нет — G-001 не ссылка")
        self.assertEqual(new("В-3"), 0, "реестра решений нет — номер ведётся в другом месте, как раньше")
        with contextlib.redirect_stdout(io.StringIO()):
            R.main(["--project", str(root), "decide", "--quote", "слово владельца", "--gist", "g"])
            R.main(["--project", str(root), "goal", "--title", "т", "--quote", "сделай так"])
        self.assertEqual(new("В-1"), 0)
        self.assertEqual(new("В-1 п.2"), 0)
        self.assertEqual(new("В-3"), 1, "реестр есть — В-3 в нём нет")
        self.assertEqual(new("G-001"), 0)
        with contextlib.redirect_stdout(io.StringIO()):
            R.main(["--project", str(root), "accept-goal", "G-001"])
        self.assertEqual(new("G-001"), 1, "принятая цель — не ссылка для новой работы")
        self.assertEqual(new("«цитата владельца целиком»"), 1, "CP2: реестр есть — цитату сначала записывает secretary")
        R.decisions_file(root).unlink()
        self.assertEqual(new("«цитата владельца целиком»"), 0, "без реестра — цитата, как раньше")

    # --- CP2: приёмка сверяется с решениями владельца, срок владельцу записан
    def _registry_with_decision(self):
        import contextlib
        import io
        import registry as R
        os.environ.pop("RPV_ROLE", None)  # реестр пишет CEO, не роль
        with contextlib.redirect_stdout(io.StringIO()):
            R.main(["--project", str(TK.PROJECT_ROOT), "decide", "--quote", "шорты не добавляем", "--gist", "без шортов"])

    def test_accept_without_pr_needs_decisions_ack_when_registry_exists(self):
        (self.base / "out.md").write_text("x", encoding="utf-8")
        (self.base / "review.md").write_text("x", encoding="utf-8")
        self.res("engineer", "done", path="out.md")
        self.assertEqual(self.res("judge", "accept", path="review.md"), 0, "реестра нет — сверка не требуется")
        T.write_header_updates(self.path, {"status": "in_review"}, now=NOW)
        self._registry_with_decision()
        self.refused("judge", "accept", path="review.md")
        self.refused("judge", "accept", path="review.md", decisions="В-9")
        self.refused("judge", "accept", path="review.md", decisions="В-1")  # CP9: и таблица чисел
        self.assertEqual(self.res("judge", "accept", path="review.md", decisions="В-1", numbers="-"), 0)
        self.assertIn("решения: сверено В-1", self.tkt().log[-1].text)
        T.write_header_updates(self.path, {"status": "in_review"}, now=NOW)
        self.assertEqual(self.res("judge", "accept", path="review.md", decisions="-", numbers="-"), 0)
        self.assertIn("решения: связанных нет", self.tkt().log[-1].text)

    def test_accept_pr_needs_decisions_ack_on_both_entry_points(self):
        import contextlib
        import io
        self._registry_with_decision()
        self.refused("judge", "accept", pr=7, sha=SHA)
        os.environ["RPV_ROLE"] = "judge"
        with contextlib.redirect_stderr(io.StringIO()) as err:
            rc = TK.cmd_accept(type("A", (), dict(id=self.tid, pr=7, sha=SHA, text="", repo="o/r", decisions=None))())
        self.assertEqual(rc, 1)
        self.assertIn("--decisions", err.getvalue())
        self.assertEqual(self.res("judge", "accept", pr=7, sha=SHA, decisions="В-1", numbers="-"), 0)
        self.assertIn("решения: сверено В-1", self.tkt().log[-1].text)

    # --- «принято с хвостом» (разбор alpha 14–16.10): приёмка — с явным ответом про хвосты
    def _v2_done(self, text):
        self._registry_with_decision()
        (self.base / "out.md").write_text("готово", encoding="utf-8")
        (self.base / "review.md").write_text("вердикт", encoding="utf-8")
        self.assertEqual(self.res("engineer", "done", path="out.md", why=text), 0)

    def test_accept_needs_tail_answer_in_v2(self):
        self._v2_done("хук и тест 13/13 зелёные")
        self.refused("judge", "accept", path="review.md", decisions="-", numbers="-", tail=None)
        self.assertEqual(self.res("judge", "accept", path="review.md", decisions="-", numbers="-", tail="-"), 0)
        self.assertIn("хвосты: нет", self.tkt().log[-1].text)

    def test_no_tail_claim_checked_against_executor_submission(self):
        """TK-240/TK-245 альфы: исполнитель сам написал «не сделано / не достигнуто», Судья принял — теперь отказ."""
        for text in ("Хук + тест 13/13 зелёные. Не сделано (закрыто ролям): регистрация в settings.json",
                     "Выложено. Не достигнуто: вкладка Летопись 435–578 мс",
                     "сделал, остальное отдельным тикетом"):
            with self.subTest(text=text):
                T.write_header_updates(self.path, {"status": "in_progress"}, now=NOW)
                self._v2_done(text)
                self.refused("judge", "accept", path="review.md", decisions="-", numbers="-", tail="-")

    def test_tail_tickets_must_exist_and_differ(self):
        self._v2_done("Готово. Не сделано: метрики у 431 строки")
        for tail in ("TK-999", self.tid, "хвост", "TK-1x"):
            self.refused("judge", "accept", path="review.md", decisions="-", numbers="-", tail=tail)
        other = T.create_ticket(TK.TICKETS_DIR, owner="engineer", title="Хвост: метрики 431 строки").stem
        self.assertEqual(self.res("judge", "accept", path="review.md", decisions="-", numbers="-", tail=other), 0)
        self.assertIn(f"хвосты: {other}", self.tkt().log[-1].text)

    def test_tail_for_ceo_goes_to_ceo_signal(self):
        """Хвост, который ролям не сделать (TK-240: регистрация хука в settings.json), — строкой CEO."""
        self._v2_done("Хук готов. Не сделано (закрыто ролям): регистрация в settings.json")
        self.refused("judge", "accept", path="review.md", decisions="-", numbers="-", tail="ceo:")
        with mock.patch.object(TK.D, "append_ceo_inbox") as sig:
            self.assertEqual(self.res("judge", "accept", path="review.md", decisions="-", numbers="-",
                                      tail='ceo:"вписать хук в settings.json"'), 0)
        sig.assert_called_once_with(self.tid, "хвост", "вписать хук в settings.json")
        self.assertIn("хвост CEO: вписать хук в settings.json", self.tkt().log[-1].text)

    def test_tail_gate_off_without_registry(self):
        (self.base / "out.md").write_text("x", encoding="utf-8")
        (self.base / "review.md").write_text("x", encoding="utf-8")
        self.res("engineer", "done", path="out.md", why="не сделано: всё")
        self.assertEqual(self.res("judge", "accept", path="review.md", tail=None), 0, "до V2 (без реестра) ворот нет")

    def test_tail_negation_is_not_a_tail(self):
        """alpha TK-264 (PR #106): «… Хвостов нет» + `--tail -` отказывало корнем `хвост`; законченное отрицание — не хвост."""
        for text in ("Всё сделано, тесты зелёные. Хвостов нет", "хвоста нет, всё сделано", "Сделано без хвостов.",
                     "Сделано, ничего не осталось", "Сделано, осталось 0"):
            with self.subTest(text=text):
                T.write_header_updates(self.path, {"status": "in_progress"}, now=NOW)
                self._v2_done(text)
                self.assertEqual(self.res("judge", "accept", path="review.md", decisions="-", numbers="-", tail="-"), 0)
                self.assertIn("хвосты: нет", self.tkt().log[-1].text)

    def test_tail_with_colon_or_count_still_refused(self):
        for text in ("Готово. Хвост: нет метрик у 431 строки", "Не сделано: регистрация в settings.json",
                     "Готово, осталось 3 шага", "Хвост нет метрик у 431 строки", "Не осталось времени доделать экспорт"):
            with self.subTest(text=text):
                T.write_header_updates(self.path, {"status": "in_progress"}, now=NOW)
                self._v2_done(text)
                self.refused("judge", "accept", path="review.md", decisions="-", numbers="-", tail="-")

    # --- непустой next не затирается записью Судьи (alpha TK-282, PR #107 П2а)
    def _judge_comment(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            TK.cmd_comment(type("A", (), dict(id=self.tid, author="judge", text="заметка Судьи", next=None))())
        return out.getvalue()

    def test_judge_comment_keeps_nonempty_next(self):
        T.write_header_updates(self.path, {"owner": "researcher"}, now=NOW)
        with contextlib.redirect_stdout(io.StringIO()):
            TK.cmd_comment(type("A", (), dict(id=self.tid, author="ceo", text="нужна выкладка", next="engineer"))())
        out = self._judge_comment()
        self.assertEqual(self.tkt().header.get("next"), "engineer", "заказ CEO не сброшен на владельца тикета")
        self.assertIn("next оставлен: engineer", out)
        self.assertNotIn("проставлен автоматически", out)

    def test_judge_comment_sets_owner_when_next_empty(self):
        T.write_header_updates(self.path, {"owner": "researcher"}, now=NOW)
        with contextlib.redirect_stdout(io.StringIO()):
            TK.cmd_comment(type("A", (), dict(id=self.tid, author="ceo", text="нужна выкладка", next="engineer"))())
            TK.cmd_comment(type("A", (), dict(id=self.tid, author="engineer", text="сделал", next=None))())
        T.write_header_updates(self.path, {"next": ""}, now=NOW, stamp_updated=False)  # диспетчер погасил next при запуске
        out = self._judge_comment()
        self.assertEqual(self.tkt().header.get("next"), "researcher")
        self.assertIn("проставлен автоматически", out)
        self.assertNotIn("next оставлен", out)

    # --- живая проверка (разбор alpha 14–16.10, П1): пункт [вживую] — приёмка только с файлом «что увидит владелец»
    LIVE_TEXT = "Открыл /runs на calc (10 клеток R1): таблица за 1,8 с, у Г-39 и Г-35 статус «9/9», не жёлтый."

    def _live_done(self):
        T.write_header_updates(self.path, {"status": "in_progress"}, now=NOW)
        self.res("engineer", "done", path="out.md",
                 why="Сдам форму. Примут, если: [вживую] владелец открывает /runs и видит таблицу за ≤ 2 с")

    def test_live_mark_requires_live_file(self):
        (self.base / "out.md").write_text("x", encoding="utf-8")
        (self.base / "review.md").write_text("x", encoding="utf-8")
        self._live_done()
        self.refused("judge", "accept", path="review.md", live=None)
        self.refused("judge", "accept", path="review.md", live="docs/reviews/нет.md")
        self.refused("judge", "accept", pr=7, sha=SHA, decisions="-", numbers="-", live=None)  # приёмка PR — те же ворота
        (self.base / "docs").mkdir()
        (self.base / "docs" / "b.md").write_text("ок", encoding="utf-8")
        self.refused("judge", "accept", path="review.md", live="docs/b.md")  # пустая запись
        live = self.base / "docs" / "live.md"
        live.write_text(self.LIVE_TEXT + " Не dry_run: настоящие хеши.", encoding="utf-8")  # честное «не dry_run» — не отказ
        self.assertEqual(self.res("judge", "accept", path="review.md", live="docs/live.md"), 0)
        self.assertIn("вживую: docs/live.md", self.tkt().log[-1].text)

    def test_live_file_outside_project_refused(self):
        (self.base / "out.md").write_text("x", encoding="utf-8")
        (self.base / "review.md").write_text("x", encoding="utf-8")
        self._live_done()
        with tempfile.TemporaryDirectory() as out:
            (Path(out) / "live.md").write_text(self.LIVE_TEXT, encoding="utf-8")
            self.refused("judge", "accept", path="review.md", live=str(Path(out) / "live.md"))

    def test_live_mark_in_description_counts_and_no_mark_no_gate(self):
        (self.base / "out.md").write_text("x", encoding="utf-8")
        (self.base / "review.md").write_text("x", encoding="utf-8")
        self.res("engineer", "done", path="out.md")
        self.assertEqual(self.res("judge", "accept", path="review.md", live=None), 0, "без [вживую] ворот нет")
        p = T.create_ticket(CFG.TICKETS_DIR, owner="engineer", title="Ф", status="in_progress",
                            description="Примут, если: [вживую] владелец открывает форму и видит прогоны за 2 с", now=NOW)
        T.write_header_updates(p, {"reviewer": "judge"}, now=NOW)
        self.tid = p.stem
        self.res("engineer", "done", path="out.md")
        self.refused("judge", "accept", path="review.md", live=None)

    def test_accept_numbers_must_be_in_their_sources(self):
        """CP9: в V2-проекте приёмка — с таблицей «число · файл»; число, которого нет в источнике, — отказ."""
        self._registry_with_decision()
        (self.base / "out.md").write_text("x", encoding="utf-8")
        self.res("engineer", "done", path="out.md")
        (self.base / "docs").mkdir()
        (self.base / "docs" / "sieve.md").write_text("годна 12\nне годна 287\nΔSR 0,072\n", encoding="utf-8")

        def table(name, rows):
            (self.base / name).write_text("| число | откуда |\n|---|---|\n" + "".join(f"| {a} | {b} |\n" for a, b in rows),
                                          encoding="utf-8")
            return name
        T.write_header_updates(self.path, {"status": "in_review"}, now=NOW)
        self.refused("judge", "accept", path="out.md", decisions="-")
        self.refused("judge", "accept", path="out.md", decisions="-", numbers="нет-файла.md")
        self.refused("judge", "accept", path="out.md", decisions="-", numbers=table("t0.md", []))
        self.refused("judge", "accept", path="out.md", decisions="-",
                     numbers=table("t1.md", [("годна 14", "docs/sieve.md")]))          # 14 «из головы»
        self.refused("judge", "accept", path="out.md", decisions="-",
                     numbers=table("t2.md", [("287", "`docs/sieve.md:30`")]))          # не в названной строке ±2
        self.refused("judge", "accept", path="out.md", decisions="-",
                     numbers=table("t3.md", [("12", "docs/нет.md")]))                   # источника нет
        ok = table("t4.md", [("годна 12", "docs/sieve.md:1"), ("0.072", "docs/sieve.md:3"), ("287", "docs/sieve.md:2")])
        self.assertEqual(self.res("judge", "accept", path="out.md", decisions="-", numbers=ok), 0)
        self.assertIn("числа: сверено 3 (t4.md)", self.tkt().log[-1].text)

    def test_accept_numbers_row_needs_line_number(self):
        """CP9: без номера строки короткое «3» нашлось бы где угодно — `файл:строка` обязателен, строка ±2."""
        import contextlib
        import io
        self._registry_with_decision()
        (self.base / "out.md").write_text("x", encoding="utf-8")
        self.res("engineer", "done", path="out.md")
        src = ["строка %d" % i for i in range(1, 21)]
        src[9] = "годна 3 из 12"   # строка 10
        (self.base / "src.md").write_text("\n".join(src) + "\n", encoding="utf-8")

        def table(name, rows):
            (self.base / name).write_text("| число | откуда |\n|---|---|\n" + "".join(f"| {a} | {b} |\n" for a, b in rows),
                                          encoding="utf-8")
            return name
        T.write_header_updates(self.path, {"status": "in_review"}, now=NOW)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.refused("judge", "accept", path="out.md", decisions="-", numbers=table("n0.md", [("3", "src.md")]))
        self.assertIn("нет номера строки", err.getvalue())
        self.assertIn("число есть в строках 3, 10", err.getvalue(), "подсказка: где число есть")
        self.refused("judge", "accept", path="out.md", decisions="-", numbers=table("n1.md", [("3", "`src.md:13`")]))  # дальше ±2
        self.refused("judge", "accept", path="out.md", decisions="-", numbers=table("n2.md", [("3", "src.md:7")]))
        self.refused("judge", "accept", path="out.md", decisions="-", numbers=table("n3.md", [("3", "src.md:99")]))    # строки нет
        self.refused("judge", "accept", path="out.md", decisions="-", numbers=table("n4.md", [("3", "src.md:0")]))
        self.refused("judge", "accept", path="out.md", decisions="-", numbers=table("n5.md", [("1", "src.md:10")]))    # 1 — не в 12
        for n, line in (("n6.md", "src.md:8"), ("n7.md", "src.md:12"), ("n8.md", "`src.md:10`")):
            T.write_header_updates(self.path, {"status": "in_review"}, now=NOW)
            self.assertEqual(self.res("judge", "accept", path="out.md", decisions="-", numbers=table(n, [("3", line)])), 0)

    def test_accept_numbers_dash_checked_in_text_artifact(self):
        """CP9: `--numbers -` при приёмке по --path — код ищет числа в текстовом артефакте; по PR проверки нет."""
        import contextlib
        import io
        self._registry_with_decision()
        (self.base / "out.md").write_text("x", encoding="utf-8")
        self.res("engineer", "done", path="out.md")
        (self.base / "clean.md").write_text(
            "## 1\n1. TK-236 принят 2026-10-14 в 13:45, PR #7@a1b2c3d, версия 1.8.48, В-12, G-001 (см. src.md:30).\n", encoding="utf-8")
        (self.base / "dirty.md").write_text("Годна 12 из 287, ΔSR 0,072.\n", encoding="utf-8")
        (self.base / "data.csv").write_text("a,b\n1,2\n", encoding="utf-8")
        (self.base / "tool.py").write_text("X = 42\n", encoding="utf-8")   # не текстовый артефакт — читать нечем
        T.write_header_updates(self.path, {"status": "in_review"}, now=NOW)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.refused("judge", "accept", path="dirty.md", decisions="-", numbers="-")
        self.assertIn("в сдаче есть числа (первые: 12, 287, 0,072)", err.getvalue())
        self.assertIn("--numbers", err.getvalue())
        self.refused("judge", "accept", path="data.csv", decisions="-", numbers="-")
        self.assertEqual(self.res("judge", "accept", path="tool.py", decisions="-", numbers="-"), 0)
        T.write_header_updates(self.path, {"status": "in_review"}, now=NOW)
        self.assertEqual(self.res("judge", "accept", path="clean.md", decisions="-", numbers="-"), 0)
        self.assertIn("числа: нет", self.tkt().log[-1].text)
        T.write_header_updates(self.path, {"status": "in_review"}, now=NOW)
        self.assertEqual(self.res("judge", "accept", pr=7, sha=SHA, path="dirty.md", decisions="-", numbers="-"), 0)  # код — как раньше

    def test_accept_numbers_dash_scans_directory_text_files(self):
        self._registry_with_decision()
        (self.base / "rep").mkdir()
        (self.base / "rep" / "one.md").write_text("чисел нет\n", encoding="utf-8")
        (self.base / "rep" / "pic.png").write_bytes(b"\x89PNG 12345")
        self.assertEqual(TK._artifact_numbers(self.base / "rep"), [])
        (self.base / "rep" / "two.txt").write_text("получили 17 штук\n", encoding="utf-8")
        self.assertEqual(TK._artifact_numbers(self.base / "rep"), ["17"])
        self.assertEqual(TK._artifact_numbers(None), [])

    def test_text_numbers_skips_labels_but_not_values(self):
        """CP9: числом не считаются даты и время, номера TK-/В-/G-/E-/PR #/п., версии X.Y.Z, sha, номера пунктов и заголовков."""
        labels = [
            "2026-10-14", "2026-10-14T12:30:00+04:00", "2026-10-14 09:05", "14.10.2026", "14.10", "5.10.2026", "13:45", "9:05:30",
            "14 октября 2026 г.", "TK-236", "В-12", "В-229 п.4", "G-001", "E-3", "PR #7", "pr 7", "#12", "№5", "п. 4", "пункт 5",
            "версия 1.8.48", "v2.0.1", "1.2.3.4", "a1b2c3d", "0123456789abcdef0123456789abcdef01234567",
            "1b4e28ba-2fa1-11d2-883f-b9a761bde3fb", "1. первое", "2) второе", "  - 3. вложенное", "12.1. раздел", "## 1", "### 2.3 Раздел",
            "docs/sieve.md:30", "test_result.py:447-450", "https://github.com/o/r/pull/123", "CP9", "V2", "sha256", "UTF-8", "python3",
        ]
        for s in labels:
            self.assertEqual(TK._text_numbers(s), [], s)
        values = {"годна 12 штук": ["12"], "ΔSR 0,072": ["0,072"], "цена 3.5": ["3.5"], "доля 12.5%": ["12.5"],
                  "## Итоги 12 тестов": ["12"], "в 2026-10-14 было 17 ошибок": ["17"], "шаг 2 из 3": ["2", "3"],
                  "1234567 строк": ["1234567"], "- 12 тестов": ["12"], "ушло 2.05 с": ["2.05"], "0.072": ["0.072"]}
        for s, want in values.items():
            self.assertEqual(TK._text_numbers(s), want, s)
        self.assertTrue(TK._has_number("ΔSR 0,072", "0.072"))
        self.assertFalse(TK._has_number("их 13", "3"))
        self.assertFalse(TK._has_number("их 0,3", "3"))
        self.assertFalse(TK._has_number("12.5", "12"))
        self.assertTrue(TK._has_number("итого 12.", "12"))

    def test_due_records_first_and_current_deadline_ceo_only(self):
        import contextlib
        import io
        os.environ.pop("RPV_ROLE", None)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(TK.cmd_due(type("A", (), dict(id=self.tid, by="2099-01-02T18:00+04:00", said="к шести"))()), 0)
            self.assertEqual(TK.cmd_due(type("A", (), dict(id=self.tid, by="2099-01-03T12:00+04:00", said=None))()), 0)
        h = self.tkt().header
        self.assertEqual((h.get("due_first"), h.get("due")), ("2099-01-02T18:00+04:00", "2099-01-03T12:00+04:00"))
        self.assertIn("Срок владельцу: 2099-01-02T18:00+04:00 — «к шести»", self.tkt().log[-2].text)
        os.environ["RPV_ROLE"] = "engineer"
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(TK.cmd_due(type("A", (), dict(id=self.tid, by="2099-01-04T12:00+04:00", said=None))()), 1)
            os.environ.pop("RPV_ROLE", None)
            self.assertEqual(TK.cmd_due(type("A", (), dict(id=self.tid, by="вчера", said=None))()), 1)
        self.assertEqual(self.tkt().header.get("due"), "2099-01-03T12:00+04:00")

    def test_new_with_due(self):
        import contextlib
        import io
        a = dict(owner="engineer", title="Срочно", reviewer=None, effort=None, desc="", wait_for="", force=True,
                 backlog=False, executor=None, kind=None, ref="В-229", due="2099-01-02T18:00+04:00", said=None)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(TK.cmd_new(type("A", (), a)()), 0)
        made = T.read_ticket(max(CFG.TICKETS_DIR.glob("TK-*.md")))
        self.assertEqual(made.header.get("due_first"), "2099-01-02T18:00+04:00")
        before = len(list(CFG.TICKETS_DIR.glob("TK-*.md")))
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(TK.cmd_new(type("A", (), dict(a, title="Другое", due="не срок"))()), 1)
        self.assertEqual(len(list(CFG.TICKETS_DIR.glob("TK-*.md"))), before, "непонятый срок — файл не создан")


    # --- V2-3: приёмка Судьи гоняет проверки всех исправленных ошибок
    def _errors_repo(self):
        """git-репо в base: проверка tools/chk.py зелёная, пока рядом нет red.flag; одна исправленная ошибка E-001 «Цена»
        (красная проверка на коммите с red.flag, зелёная после коммита, который его убрал)."""
        import contextlib
        import io
        import errors as ER
        self._git("init", "-q")
        (self.base / "tools").mkdir()
        (self.base / "tools" / "chk.py").write_text(
            "import sys\nfrom pathlib import Path\nprint('цена не нулевая')\nsys.exit(4 if Path('red.flag').exists() else 0)\n",
            encoding="utf-8")
        (self.base / "red.flag").write_text("ошибка есть", encoding="utf-8")
        self._git("add", "tools/chk.py", "red.flag")
        self._git("commit", "-qm", "chk с ошибкой")
        with contextlib.redirect_stdout(io.StringIO()):
            ER.main(["--project", str(self.base), "add", "--title", "Цена"])           # added_sha — коммит с ошибкой
            self._git("rm", "-q", "-f", "red.flag")
            self._git("commit", "-qm", "исправление")
            with mock.patch.dict(os.environ, {"RPV_ROLE": "judge"}):                  # исправленной объявляет Судья
                self.assertEqual(ER.main(["--project", str(self.base), "fix", "E-001", "--check", "python tools/chk.py"]), 0)

    def refused_regression(self, role, result, **kw):
        import contextlib
        import io
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.refused(role, result, **kw)   # файл тикета не тронут
        self.assertIn("регрессия", err.getvalue())
        self.assertIn("E-001 «Цена»: код 4", err.getvalue())
        self.assertIn("цена не нулевая", err.getvalue())

    def test_accept_without_pr_refused_while_error_check_fails(self):
        self._errors_repo()
        (self.base / "review.md").write_text("x", encoding="utf-8")
        T.write_header_updates(self.path, {"status": "in_review", "next": "judge"}, now=NOW)
        (self.base / "red.flag").write_text("x", encoding="utf-8")
        self.refused_regression("judge", "accept", path="review.md")
        self.assertEqual(self.res("judge", "return", path="review.md"), 0)       # возврат — не приёмка, проверки не гоняет
        T.write_header_updates(self.path, {"status": "in_review", "next": "judge"}, now=NOW)
        (self.base / "red.flag").unlink()
        self.assertEqual(self.res("judge", "accept", path="review.md"), 0)       # проверки зелёные — принято
        self.assertIn("[итог: accept]", self.tkt().log[-1].text)

    def test_accept_pr_refused_while_error_check_fails_on_both_entry_points(self):
        self._errors_repo()
        T.write_header_updates(self.path, {"status": "in_review", "next": "judge"}, now=NOW)
        (self.base / "red.flag").write_text("x", encoding="utf-8")
        self.refused_regression("judge", "accept", pr=7, sha=SHA)                # tickets.py result accept --pr --sha
        import contextlib
        import io
        before = self.path.read_text(encoding="utf-8")
        os.environ["RPV_ROLE"] = "judge"
        accept = lambda: TK.cmd_accept(type("A", (), {"id": self.tid, "pr": 7, "sha": SHA, "text": ""})())  # tickets.py accept
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(accept(), 1)
        self.assertIn("accept: регрессия", err.getvalue())
        self.assertEqual(self.path.read_text(encoding="utf-8"), before, "отказ ничего не пишет")
        (self.base / "red.flag").unlink()
        self.assertEqual(accept(), 0)
        self.assertEqual(self.tkt().header.get("accepted"), f"7@{SHA}")

    def test_accept_error_checks_run_in_ticket_worktree_when_it_exists(self):
        self._errors_repo()
        (self.base / "review.md").write_text("x", encoding="utf-8")
        wt = self.base / ".claude" / "worktrees" / self.tid
        self._git("worktree", "add", "-q", str(wt), "-b", "work")
        T.write_header_updates(self.path, {"status": "in_review", "next": "judge"}, now=NOW)
        (self.base / "red.flag").write_text("x", encoding="utf-8")               # красно только в основном дереве
        self.assertEqual(self.res("judge", "accept", path="review.md"), 0)       # проверки шли в копии тикета
        T.write_header_updates(self.path, {"status": "in_review", "next": "judge"}, now=NOW)
        (wt / "red.flag").write_text("x", encoding="utf-8")                      # теперь красно в копии
        self.refused_regression("judge", "accept", path="review.md")


if __name__ == "__main__":
    unittest.main()
