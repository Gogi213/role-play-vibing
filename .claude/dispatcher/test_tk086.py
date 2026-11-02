"""TK-086 (В-33): сторож `goal-idle` — у открытой цели принят шаг, а открытого тикета на неё нет: CEO будят даже при живом
постороннем тикете; тикет без цели сигнала не дает; один раз на событие; причина — в ceo-wake.log."""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_watch import WatchSandbox, CFG, T, W  # noqa: E402 — песочница сторожа
import goal_idle as GI  # noqa: E402
import project as P  # noqa: E402
import rpv_config  # noqa: E402
import signals as SIG  # noqa: E402

PLUGIN = Path(__file__).resolve().parents[2]


class GoalIdleTests(WatchSandbox):
    def setUp(self):
        super().setUp()
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        base = Path(self.root.name)
        (base / ".claude" / "goals").mkdir(parents=True)
        old_root, old_p = CFG.PROJECT_ROOT, P._ROOT
        CFG.PROJECT_ROOT = base
        P.set_root(base)
        self.addCleanup(setattr, CFG, "PROJECT_ROOT", old_root)
        self.addCleanup(P.set_root, old_p)
        self.goal("G-001")

    def goal(self, gid, status="open"):
        (Path(self.root.name) / ".claude" / "goals" / f"{gid}.md").write_text(
            f"---\nid: {gid}\nstatus: {status}\nset: 2026-11-01 10:00\naccepted:\n---\n# Цель {gid}\n", encoding="utf-8")

    def ticket(self, ref="G-001", status="in_progress", accepted_at=None, owner="engineer"):
        path = T.create_ticket(self.tickets_dir, owner, "шаг", reviewer="judge", status=status,
                               description=f"Ссылка на задачу владельца: {ref}" if ref else "", now=self.now)
        if accepted_at is not None:
            T.append_log(path, "judge", "[итог: accept] PR #1@abcdef0 — принято", now=accepted_at)
        return T.read_ticket(path).id

    def events(self, minutes=0, ws=None):
        ws = {} if ws is None else ws
        return GI.goal_idle_events(ws, self.now + timedelta(minutes=minutes)), ws

    def test_accepted_step_without_next_ticket_signals_despite_foreign_live_ticket(self):
        step = self.ticket(status="done", accepted_at=self.now)
        self.ticket(ref="", status="in_progress")  # посторонний живой тикет без цели
        out, _ = self.events(30)
        self.assertEqual([(g, t) for g, t, _ in out], [("G-001", step)])
        self.assertIn("следующего шага нет", out[0][2])

    def test_next_ticket_of_the_goal_covers_it(self):
        self.ticket(status="done", accepted_at=self.now)
        self.ticket(status="todo")
        self.assertEqual(self.events(30)[0], [])

    def test_done_without_goal_gives_no_signal(self):
        self.ticket(ref="", status="done", accepted_at=self.now)
        self.assertEqual(self.events(30)[0], [])

    def test_goal_without_accepted_step_gives_no_signal(self):
        self.ticket(status="done")  # закрыт без приёмки Судьи
        self.assertEqual(self.events(30)[0], [])

    def test_closed_goal_gives_no_signal(self):
        self.goal("G-002", status="accepted")
        self.ticket(ref="G-002", status="done", accepted_at=self.now)
        self.assertEqual(self.events(30)[0], [])

    def test_below_threshold_silent_then_fires(self):
        self.ticket(status="done", accepted_at=self.now)
        self.assertEqual(self.events(GI.GOAL_IDLE_MIN - 1)[0], [])
        self.assertEqual(len(self.events(GI.GOAL_IDLE_MIN)[0]), 1)

    def test_threshold_key(self):
        (Path(self.root.name) / ".claude" / "rpv.json").write_text(json.dumps({"goal_idle_min": 45}), encoding="utf-8")
        self.ticket(status="done", accepted_at=self.now)
        self.assertEqual(self.events(30)[0], [])
        self.assertEqual(len(self.events(45)[0]), 1)

    def test_once_per_event_no_timer_repeat(self):
        self.ticket(status="done", accepted_at=self.now)
        out, ws = self.events(30)
        self.assertEqual(len(out), 1)
        self.assertEqual(self.events(31, ws)[0], [])
        self.assertEqual(self.events(60 * 30, ws)[0], [])

    def test_new_accepted_step_is_a_new_event(self):
        self.ticket(status="done", accepted_at=self.now)
        _, ws = self.events(30)
        second = self.ticket(status="done", accepted_at=self.now + timedelta(minutes=40))
        out, _ = self.events(60, ws)
        self.assertEqual([t for _, t, _ in out], [second])

    def test_unreadable_ticket_is_silent(self):
        self.ticket(status="done", accepted_at=self.now)
        (self.tickets_dir / "TK-999.md").write_bytes(b"\xff\xfe\x00 not a ticket")
        self.assertEqual(self.events(30)[0], [])

    def test_run_once_posts_reason_in_wake_log(self):
        step = self.ticket(status="done", accepted_at=self.now)
        posted = W.run_once(self.now + timedelta(minutes=30), probes=False)
        self.assertEqual([f.kind for f in posted], ["goal-idle"])
        line = next(ln for ln in CFG.CEO_WAKE_LOG.read_text(encoding="utf-8").splitlines() if "watch-goal-idle" in ln)
        for must in ("G-001", step, "следующего шага нет"):
            self.assertIn(must, line)
        self.assertIn("[watch-goal-idle]", CFG.CEO_INBOX.read_text(encoding="utf-8"))
        self.assertEqual(SIG.classify_signal("watch-goal-idle"), "wake")
        self.assertEqual(W.run_once(self.now + timedelta(minutes=31), probes=False), [])  # второго сигнала нет

    def test_other_wake_lines_stay_short(self):
        SIG.append_ceo_inbox("TK-1", "done", "длинное описание", self.now)
        self.assertTrue(CFG.CEO_WAKE_LOG.read_text(encoding="utf-8").rstrip().endswith("TK-1 done"))


class GoalIdleDocsTests(WatchSandbox):
    def test_key_is_valid_and_documented(self):
        check, _ = rpv_config.KEYS["goal_idle_min"]
        self.assertTrue(check(10))
        self.assertFalse(check(0))
        self.assertFalse(check(True))
        self.assertIn("goal_idle_min", (PLUGIN / "docs" / "configuration.md").read_text(encoding="utf-8"))

    def test_signals_doc_row(self):
        text = (PLUGIN / "docs" / "signals.md").read_text(encoding="utf-8")
        row = next(ln for ln in text.splitlines() if ln.startswith("| `watch-goal-idle`"))
        for must in ("`goal_idle_min`", "один раз на событие", "ceo-wake.log", "будит"):
            self.assertIn(must, row)
