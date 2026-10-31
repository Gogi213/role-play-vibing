"""TK-075 (хвост TK-072): ключ дедупа «влито, не выпущено» несёт ветку репозитория; «потолок Monitor — 10 мин (600000)» во всех
уставах и документах. Запуск: `python -m pytest .claude/dispatcher/test_tk075.py`."""
from __future__ import annotations

import sys
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_tk062 import CEO_COMMAND, CEO_TEMPLATE, PLUGIN, SIGNALS_DOC, T, UNR, W  # noqa: E402
from test_tk072 import Base  # noqa: E402


class DedupKeyHasBranchTests(Base):
    def test_key_is_the_default_branch(self):
        self.branch = "trunk"
        out, _ = self.check(0, self.ws)
        self.assertEqual([(f.kind, f.key) for f in out], [("unreleased", "trunk")])

    def test_main_keeps_the_old_marker(self):
        out, _ = self.check(0, self.ws)
        self.assertEqual(out[0].key, "main")

    def test_branch_change_is_a_new_finding_not_a_repeat(self):
        ws = {"notified": {}, **self.ws}
        self.assertEqual(len(W.notify_findings(self.check(0, self.ws)[0], ws, self.now)), 1)
        self.branch = "trunk"
        later = self.now + timedelta(minutes=2)
        self.assertEqual(len(W.notify_findings(self.check(2, self.ws)[0], ws, later)), 1)  # окно суток не ждём
        self.assertIn("unreleased:trunk", ws["notified"])
        self.assertNotIn("unreleased:main", ws["notified"])

    def test_gh_failure_keeps_the_branch_marker(self):
        self.branch = "trunk"
        ws = {"notified": {}, **self.ws}
        W.notify_findings(self.check(0, self.ws)[0], ws, self.now)
        self.gh_down = True
        down = self.check(2, self.ws)[0]
        self.assertEqual([(f.key, f.unknown) for f in down], [("trunk", True)])
        self.assertEqual(W.notify_findings(down, ws, self.now + timedelta(minutes=2)), [])
        self.assertIn("unreleased:trunk", ws["notified"])

    def test_branch_is_remembered_in_watch_state(self):
        self.branch = "trunk"
        _, ws = self.check(0, self.ws)
        self.assertEqual(ws[UNR.BRANCH_KEY], "trunk")


class MonitorCeilingTextTests(unittest.TestCase):
    """Схема Monitor: умолчание 5 мин, `timeout_ms` выше 600000 обрезается до 600000 — 1800000/«30 мин» в тексте не остаётся."""
    FILES = {"устав CEO": CEO_TEMPLATE, "команда /ceo": CEO_COMMAND, "docs/signals.md": SIGNALS_DOC,
             "CHANGELOG": PLUGIN / "CHANGELOG.md"}

    def test_no_1800000_in_any_text(self):
        for name, path in self.FILES.items():
            with self.subTest(name=name):
                self.assertNotIn("1800000", path.read_text(encoding="utf-8"))

    def test_text_states_600000_and_ten_minutes(self):
        for name, path in self.FILES.items():
            if name == "CHANGELOG":
                continue
            with self.subTest(name=name):
                text = path.read_text(encoding="utf-8")
                self.assertIn("timeout_ms: 600000", text)
                self.assertRegex(text, r"(потолок|не дольше) 10 мин")

    def test_changelog_has_no_old_ceiling(self):
        text = (PLUGIN / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertNotIn("потолок 30 мин", text)
        self.assertNotIn("живёт до 30 мин", text)


if __name__ == "__main__":
    unittest.main()
