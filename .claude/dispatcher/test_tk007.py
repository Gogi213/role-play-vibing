"""TK-007 (В-7): CEO правит настройки только командой `tickets.py config set`: закрытый список ключей, журнал с цитатой владельца,
проверка Судьи, откат через 2 ч без приёмки, раздел отчёта. Прямую запись rpv.json и уставов отказывает страж (test_delete_guard)."""
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
import rpv_config as cfg  # noqa: E402
import dispatch as D  # noqa: E402
import project_paths as CFG  # noqa: E402
import report as R  # noqa: E402
import tickets as TK  # noqa: E402

TZ = timezone(timedelta(hours=4))
NOW = datetime(2026, 10, 27, 12, 0, tzinfo=TZ)
QUOTE = "ну мы можем сделать чтобы ты да менял что то но творчества в этом было меньше"


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / ".claude" / "dispatcher").mkdir(parents=True)
        (self.root / ".claude" / "decisions.md").write_text(
            f"# Решения владельца\n\n## В-7 · 2026-10-27 04:57\n> {QUOTE}\nСуть: узкие рамки\nЗаменяет: —\n", encoding="utf-8")
        self.rpv = self.root / ".claude" / "rpv.json"
        self.rpv.write_text(json.dumps({"max_parallel": 3}), encoding="utf-8")
        self._orig = (CFG.TICKETS_DIR, TK.TICKETS_DIR, TK.PROJECT_ROOT)
        CFG.TICKETS_DIR = TK.TICKETS_DIR = self.root / "tickets"
        TK.PROJECT_ROOT = self.root
        self.role = ""
        patch = mock.patch.object(TK, "_caller_role", lambda: self.role)
        patch.start()
        self.addCleanup(patch.stop)

    def tearDown(self):
        CFG.TICKETS_DIR, TK.TICKETS_DIR, TK.PROJECT_ROOT = self._orig
        self.tmp.cleanup()

    def cmd(self, *argv):
        """tickets.py config … → (код, stderr)."""
        err = io.StringIO()
        import argparse
        p = argparse.ArgumentParser()
        p.add_argument("action")
        p.add_argument("key")
        p.add_argument("value", nargs="?", default=None)
        p.add_argument("--why", default="")
        p.add_argument("--ref", default="")
        a = p.parse_args(argv)
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            return TK.cmd_config(a), err.getvalue()

    def settings(self):
        return json.loads(self.rpv.read_text(encoding="utf-8"))

    def test_unknown_key_and_bad_values_are_refused(self):
        before = self.rpv.read_text(encoding="utf-8")
        for argv in (("set", "creativity", "9", "--why", "x"),
                     ("set", "max_parallel", "0", "--why", "x"),
                     ("set", "max_parallel", "много", "--why", "x"),
                     ("set", "report_at", "25:00", "--why", "x"),
                     ("set", "role_parallel", '{"ceo": 2}', "--why", "x"),
                     ("set", "role_mcp", "some", "--why", "x"),
                     ("set", "ci_repo", "no-slash", "--why", "x"),
                     ("set", "protected_paths", "[]", "--why", "x"),
                     ("set", "max_parallel", "5"),                                  # без --why
                     ("set", "max_parallel")):                                      # без значения
            code, err = self.cmd(*argv)
            self.assertEqual(code, 1, argv)
            self.assertTrue(err.strip(), argv)
        self.assertEqual(self.rpv.read_text(encoding="utf-8"), before)
        self.assertEqual(cfg.read_log(self.root), [])

    def test_unknown_ref_is_refused_known_ref_is_logged_with_quote(self):
        code, err = self.cmd("set", "max_parallel", "5", "--why", "нужно", "--ref", "В-99")
        self.assertEqual(code, 1)
        self.assertIn("В-99", err)
        self.assertEqual(self.settings(), {"max_parallel": 3})
        code, _ = self.cmd("set", "max_parallel", "5", "--why", "параллель", "--ref", "В-7")
        self.assertEqual(code, 0)
        self.assertEqual(self.settings(), {"max_parallel": 5})
        (e,) = cfg.read_log(self.root)
        self.assertEqual((e["key"], e["old"], e["new"], e["ref"], e["quote"], e["state"]),
                         ("max_parallel", 3, 5, "В-7", QUOTE, "pending"))
        judge = next((self.root / "tickets").glob("TK-*.md")).read_text(encoding="utf-8")   # Судье заведена проверка
        self.assertIn("owner: judge", judge)
        self.assertIn(QUOTE, judge)
        self.assertEqual(e["ticket"], "TK-001")

    def test_not_accepted_change_rolls_back_after_two_hours(self):
        self.cmd("set", "max_parallel", "5", "--why", "x")
        self.cmd("set", "report_at", "09:00", "--why", "x")                         # ключа не было — откат его уберёт
        at = datetime.fromisoformat(cfg.read_log(self.root)[0]["at"])
        self.assertEqual(cfg.rollback_due(self.root, at + timedelta(hours=2) - timedelta(minutes=1)), [])
        self.assertEqual(self.settings(), {"max_parallel": 5, "report_at": "09:00"})
        done = cfg.rollback_due(self.root, at + timedelta(hours=2, minutes=1))
        self.assertEqual(len(done), 2)
        self.assertEqual(self.settings(), {"max_parallel": 3})
        self.assertEqual({e["state"] for e in cfg.read_log(self.root)}, {"rolled_back"})
        self.assertEqual(cfg.rollback_due(self.root, at + timedelta(hours=9)), [])  # повторно не трогает

    def test_judge_acceptance_keeps_the_change(self):
        self.cmd("set", "max_parallel", "5", "--why", "x")
        self.role = "engineer"
        self.assertEqual(self.cmd("accept", "CFG-1")[0], 1)                         # принять может только Судья
        self.role = ""
        self.assertEqual(self.cmd("accept", "CFG-1")[0], 1)                         # и CEO не может
        self.role = "judge"
        self.assertEqual(self.cmd("accept", "CFG-1")[0], 0)
        self.assertEqual(self.cmd("accept", "CFG-1")[0], 1)                         # повторно — отказ
        at = datetime.fromisoformat(cfg.read_log(self.root)[0]["at"])
        self.assertEqual(cfg.rollback_due(self.root, at + timedelta(hours=5)), [])
        self.assertEqual(self.settings(), {"max_parallel": 5})

    def test_roles_cannot_set(self):
        self.role = "engineer"
        self.assertEqual(self.cmd("set", "max_parallel", "5", "--why", "x")[0], 1)
        self.assertEqual(self.settings(), {"max_parallel": 3})

    def test_second_change_of_same_key_rolls_back_to_original(self):
        self.cmd("set", "max_parallel", "5", "--why", "x")
        self.cmd("set", "max_parallel", "7", "--why", "y")
        at = datetime.fromisoformat(cfg.read_log(self.root)[1]["at"])
        cfg.rollback_due(self.root, at + timedelta(hours=3))
        self.assertEqual(self.settings(), {"max_parallel": 3})
        self.assertEqual([e["state"] for e in cfg.read_log(self.root)], ["superseded", "rolled_back"])

    def test_report_shows_change_with_quote_only_when_there_is_one(self):
        period = lambda: R.Period(self.root, NOW - timedelta(days=1), NOW + timedelta(days=1), [])
        text, _ = R.build(period())
        self.assertNotIn("CEO изменил работу команды", text)                        # правок нет — раздела нет
        self.cmd("set", "max_parallel", "5", "--why", "x", "--ref", "В-7")
        at = datetime.fromisoformat(cfg.read_log(self.root)[0]["at"])
        text, _ = R.build(R.Period(self.root, at - timedelta(hours=1), at + timedelta(hours=1), []))
        self.assertIn("CEO изменил работу команды", text)
        self.assertIn("max_parallel: 3 → 5", text)
        self.assertIn(QUOTE, text)

    def test_keys_match_documentation(self):
        doc = (Path(__file__).resolve().parents[2] / "docs" / "configuration.md").read_text(encoding="utf-8")
        for key in cfg.KEYS:
            self.assertIn(f"`{key}`", doc, key)


if __name__ == "__main__":
    unittest.main()
