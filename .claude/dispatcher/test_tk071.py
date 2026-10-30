"""TK-071 (G-010/G-011, В-29): критик перед приёмкой — ворота `critic_gate` за ключом `critic_before_accept`."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import critic_gate as CG  # noqa: E402
import project as P  # noqa: E402
import rpv_config  # noqa: E402

PLUGIN = Path(__file__).resolve().parents[2]
SHA = "a" * 40
OK = "1. «пункт один» — **держится**\n2. «пункт два» — **не хватает**\n"
BAD = "1. «пункт один» — **держится**\n2. «пункт два» — **не держится**\n   - не держится: файл.py:3\n"


def args(**kw):
    return SimpleNamespace(**{"critic": None, "critic_skip": None, **kw})


class GateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / ".claude" / "dispatcher").mkdir(parents=True)
        (self.root / "docs" / "reviews").mkdir(parents=True)
        self.set_key(True)

    def set_key(self, on):
        (self.root / ".claude" / "rpv.json").write_text(json.dumps({CG.KEY: on} if on is not None else {}), encoding="utf-8")

    def report(self, text, name="TK-9-critic.md", age=0):
        f = self.root / "docs" / "reviews" / name
        f.write_text(text, encoding="utf-8")
        if age:
            t = f.stat().st_mtime - age
            os.utime(f, (t, t))
        return f"docs/reviews/{name}"

    def ack(self, sha="", head=None, **kw):
        return CG.critic_ack(args(**kw), "TK-9", self.root, sha, head_ts=head or (lambda r, s: 0.0))

    def rows(self):
        p = CG.log_path(self.root)
        return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []

    def test_off_by_default_and_when_false(self):
        for state in (None, False):
            self.set_key(state)
            self.assertEqual(self.ack(), ("", ""))
        self.assertEqual(self.rows(), [])

    def test_on_without_critic_refuses_with_hint(self):
        err, _ = self.ack()
        self.assertIn("--critic", err)
        self.assertIn("critic-skip", err)

    def test_missing_wrong_dir_empty_report_refuse(self):
        self.assertIn("файла нет", self.ack(critic="docs/reviews/нет.md")[0])
        (self.root / "other.md").write_text(OK, encoding="utf-8")
        self.assertIn("docs/reviews/", self.ack(critic="other.md")[0])
        self.assertIn("нет пунктов", self.ack(critic=self.report("просто текст\n"))[0])

    def test_fresh_report_without_not_held_accepted_and_logged(self):
        err, note = self.ack(critic=self.report(OK))
        self.assertEqual(err, "")
        self.assertIn("критик: docs/reviews/TK-9-critic.md (пунктов 2, не держится 0)", note)
        row, = self.rows()
        self.assertEqual((row["kind"], row["tid"], row["items"], row["not_held"]), ("report", "TK-9", 2, 0))

    def test_report_older_than_head_refused(self):
        rel = self.report(OK, age=3600)
        mtime = (self.root / rel).stat().st_mtime
        err, _ = self.ack(sha=SHA, head=lambda r, s: mtime + 60, critic=rel)
        self.assertIn("старше головы", err)
        self.assertEqual(self.ack(sha=SHA, head=lambda r, s: mtime - 60, critic=rel)[0], "")

    def test_unknown_head_refused(self):
        err, _ = self.ack(sha=SHA, head=lambda r, s: None, critic=self.report(OK))
        self.assertIn("не найдена", err)

    def test_not_held_needs_judge_answer_per_item(self):
        rel = self.report(BAD)
        err, _ = self.ack(critic=rel)
        self.assertIn("без ответа Судьи по пунктам 2", err)
        self.assertEqual(self.rows(), [])
        self.report(BAD + "Ответ Судьи на 1: принял\n")  # ответ не на тот пункт
        self.assertIn("пунктам 2", self.ack(critic=rel)[0])
        self.report(BAD + "Ответ Судьи на 2: принял, правка в e5f6a7\n")
        err, note = self.ack(critic=rel)
        self.assertEqual(err, "")
        self.assertIn("не держится 1", note)
        self.assertEqual(self.rows()[0]["answered"], 1)

    def test_skip_needs_reason_is_logged_and_counted(self):
        self.assertIn("не короче", self.ack(critic_skip="нет")[0])
        self.assertIn("вместе нельзя", self.ack(critic="x.md", critic_skip="причина обхода есть")[0])
        err, note = self.ack(critic_skip="правка без кода, критику нечего читать")
        self.assertEqual(err, "")
        self.assertIn("обход (правка без кода", note)
        self.ack(critic_skip="правка без кода, критику нечего читать")  # повтор той же приёмки не удваивает строку
        self.ack(critic=self.report(OK))
        self.assertEqual([r["kind"] for r in self.rows()], ["skip", "report"])
        now = datetime.now(timezone.utc).astimezone()
        self.assertEqual(CG.skips(self.root, now - timedelta(hours=1), now + timedelta(hours=1)), (1, 1))
        self.assertEqual(CG.skips(self.root, now + timedelta(hours=1), now + timedelta(hours=2)), (0, 0))


class WiringTests(unittest.TestCase):
    def test_key_is_bool_in_config_and_documented(self):
        check, _ = rpv_config.KEYS[CG.KEY]
        self.assertTrue(check(True) and check(False))
        self.assertFalse(check("да") or check(1))
        self.assertIn(f"`{CG.KEY}`", (PLUGIN / "docs" / "configuration.md").read_text(encoding="utf-8"))

    def test_accept_without_pr_goes_through_gate(self):
        import tickets as TK
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".claude" / "dispatcher").mkdir(parents=True)
            (root / ".claude" / "rpv.json").write_text(json.dumps({CG.KEY: True}), encoding="utf-8")
            a = SimpleNamespace(id="TK-9", pr=None, sha=None, path=None, decisions="-", numbers="-", tail="-", live=None,
                                critic=None, critic_skip=None)
            with mock.patch.object(TK, "PROJECT_ROOT", root):
                err, _, _ = TK._accept_checks(a)
                self.assertIn("--critic", err)
                a.critic_skip = "правка без кода, критику нечего читать"
                err, ack, _ = TK._accept_checks(a)
                self.assertNotIn("--critic", err)
                self.assertIn("обход", ack)

    def test_critic_agent_and_judge_charter_describe_acceptance_input(self):
        critic = (PLUGIN / "agents" / "critic.md").read_text(encoding="utf-8")
        for must in ("договор тикета", "Ответ Судьи на N", "docs/reviews/<тикет>-critic.md"):
            self.assertIn(must, critic)
        judge = " ".join((PLUGIN / "templates" / "roles" / "judge.md").read_text(encoding="utf-8").split())
        for must in ("critic_before_accept", "--critic <файл>", "Ответ Судьи на N", "--critic-skip"):
            self.assertIn(must, judge)


if __name__ == "__main__":
    unittest.main()
