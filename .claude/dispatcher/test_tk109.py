"""Тесты TK-109 (В-209, критик-2 часть А): merge_rule пп.1/2/14, tick п.15, ответ с табло п.16, доказательство done п.3."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_merge_rule as TM  # noqa: E402  (песочница проекта и окружение — оттуда)

Base = TM.MergeRuleTests
del TM.MergeRuleTests  # унаследованные тесты базы здесь не повторяем
import dispatch as D  # noqa: E402
import merge_rule as M  # noqa: E402
import routes as R  # noqa: E402
import ticket as T  # noqa: E402


class MergeAfterPutTests(Base):
    def test_verify_timeout_after_successful_merge_keeps_post_merge(self):  # пп.1/14
        base_gh, state = self.gh, {"put": False}

        def gh(path, method="GET", **f):
            if method == "PUT":
                state["put"] = True
            elif state["put"] and path == f"repos/{TM.REPO}/pulls/7":
                raise subprocess.TimeoutExpired("gh", 30)
            return base_gh(path, method, **f)
        self.assertEqual(M.merge_once(TM.REPO, gh=gh), [(7, "влит")])
        cur = T.read_ticket(self.path)
        self.assertTrue(any("PR #7 влит" in e.text for e in cur.log))
        self.assertNotIn("7@", cur.header.get("accepted") or "")

    def test_patch_failure_does_not_abort_pass(self):  # п.2
        self.base = "feat/parent"
        base_gh = self.gh

        def gh(path, method="GET", **f):
            if method == "PATCH":
                raise RuntimeError("HTTP 502")
            return base_gh(path, method, **f)
        self.assertEqual(M.merge_once(TM.REPO, gh=gh), [])


class ProofTests(unittest.TestCase):  # п.3
    def test_done_path_rejects_root_dot_and_outside(self):
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as o:
            root = Path(d)
            (root / "a.md").write_text("x")
            (Path(o) / "b.md").write_text("x")
            chk = lambda p: R.check("engineer", "done", "ok", path=p, root=root)  # noqa: E731
            for bad in (".", "/", str(root), "", str(Path(o) / "b.md"), "../x", "нет.md"):
                self.assertTrue(chk(bad), bad)
            self.assertEqual(chk("a.md"), "")


class AnswerRetryTests(unittest.TestCase):  # п.16
    def test_comment_timeout_keeps_answer_and_retries_once(self):
        import ask
        with tempfile.TemporaryDirectory() as d:
            qdir = Path(d)
            q = {"id": "q-TK-9-1", "process": "TK-9", "from": "engineer", "from_role": "engineer", "text": "?",
                 "options": [{"key": "a", "label": "да", "effect": ""}], "since": "x", "answered_at": None}
            (qdir / "q-TK-9-1.json").write_text(json.dumps(q), encoding="utf-8")
            calls = []

            def fake(cmd, **kw):
                calls.append(cmd)
                if len(calls) == 1:
                    raise subprocess.TimeoutExpired(cmd, 15)
                return subprocess.CompletedProcess(cmd, 0, "", "")
            with mock.patch.object(ask.P, "questions_dir", return_value=qdir), mock.patch.object(ask.hide, "run", fake), \
                    mock.patch.object(ask.D if hasattr(ask, "D") else D, "append_ceo_inbox", lambda *a, **k: None), \
                    mock.patch.object(D, "TICKETS_DIR", qdir):
                ok, _, warns = ask.answer_question("q-TK-9-1", "a")
                self.assertTrue(ok)
                self.assertTrue(warns)
                ok, msg, warns = ask.answer_question("q-TK-9-1", "a")  # повтор: только запись, не новый ответ
                self.assertEqual((ok, warns, len(calls)), (True, [], 2))
                self.assertNotIn("log_pending", json.loads((qdir / "q-TK-9-1.json").read_text(encoding="utf-8")))
                ok, _, _ = ask.answer_question("q-TK-9-1", "a")
                self.assertFalse(ok)
                self.assertEqual(len(calls), 2)


class TickIsolationTests(Base):  # п.15 (песочница тикетов — из MergeRuleTests.setUp)
    def test_exception_on_one_ticket_does_not_stop_others(self):
        second = T.create_ticket(D.TICKETS_DIR, owner="engineer", title="Второй", status="todo", now=TM.NOW)
        seen = []
        real = D.handle_next_ceo

        def boom(path, tkt, state, now):
            seen.append(tkt.id)
            if tkt.id == self.tid:
                raise RuntimeError("сбой на одном")
            return real(path, tkt, state, now)
        with mock.patch.object(D, "handle_next_ceo", boom), mock.patch.object(D, "launch_run"), \
                mock.patch.object(D, "sweep_closed_worktrees"), mock.patch.object(D, "recover_active_runs"), \
                mock.patch.object(D, "append_ceo_inbox"):
            D.tick(TM.NOW)
        self.assertIn(second.stem, seen)
        self.assertIn(self.tid, seen)



if __name__ == "__main__":
    unittest.main()
