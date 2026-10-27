"""Тесты TK-002 (CP1-б): закрытие после слияния без запусков ролей, заказ CEO после приёмки, blocked исполнителя не будит CEO,
orphan-ticket не дублирует «ожидание снято» (alpha #107 П0/П1/П2б/П3 п.2, #109 п.2/п.4)."""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dispatch as D  # noqa: E402
import decide as DEC  # noqa: E402
import signals as SIG  # noqa: E402
import config as CFG  # noqa: E402
import merge_rule as M  # noqa: E402
import ticket as T  # noqa: E402
import watch as W  # noqa: E402

T0 = T.parse_dt("2026-10-17T12:40:00+04:00")
WAIT = "merged:org/repo#44"
ATTRS = ("TICKETS_DIR", "PROJECT_ROOT", "DISPATCHER_DIR", "STATE_FILE", "RUNS_DIR", "RUNS_LOG", "CEO_INBOX", "CEO_WAKE_LOG", "STOP_DIR")


class Sandbox(unittest.TestCase):
    def setUp(self):
        base = Path(tempfile.mkdtemp())
        saved = {k: getattr(CFG, k) for k in ATTRS}
        self.addCleanup(lambda: [setattr(CFG, k, v) for k, v in saved.items()])
        CFG.TICKETS_DIR, CFG.PROJECT_ROOT, CFG.DISPATCHER_DIR = base / "tickets", base, base / "dispatcher"
        CFG.TICKETS_DIR.mkdir()
        CFG.DISPATCHER_DIR.mkdir()
        CFG.STATE_FILE, CFG.RUNS_DIR, CFG.RUNS_LOG = CFG.DISPATCHER_DIR / "state.json", CFG.DISPATCHER_DIR / "runs", CFG.DISPATCHER_DIR / "runs.log"
        CFG.CEO_INBOX, CFG.CEO_WAKE_LOG, CFG.STOP_DIR = CFG.DISPATCHER_DIR / "ceo-inbox.md", CFG.DISPATCHER_DIR / "ceo-wake.log", CFG.DISPATCHER_DIR / "stop"
        D.RUNNING.clear()
        self.addCleanup(D.RUNNING.clear)

    def ticket(self, status="waiting", wait_for="", owner="engineer"):
        p = T.create_ticket(CFG.TICKETS_DIR, owner=owner, title="t", reviewer="judge", now=T0)
        T.write_header_updates(p, {"status": status, "wait_for": wait_for}, now=T0)
        return p

    def wake_lines(self):
        return CFG.CEO_WAKE_LOG.read_text(encoding="utf-8").splitlines() if CFG.CEO_WAKE_LOG.exists() else []


class CloseAfterMergeTests(Sandbox):
    """TK-266: accept → merged met → CEO done."""

    def accepted(self, sha="7a5547b"):
        p = self.ticket(wait_for=WAIT)
        T.append_log(p, "engineer", "[итог: pr] PR #44@7a5547b — готово", now=T0)
        T.append_log(p, "judge", f"[итог: accept] PR #44@{sha} проверка: docs/reviews/TK-1.md — принято", now=T0 + timedelta(minutes=1))
        return p

    def tick(self, minutes, head="7a5547b1111", merged=True, base="main"):
        info = {"head": head, "base": base, "default": "main"} if merged else {}
        with mock.patch.object(D, "launch_run") as launch, \
                mock.patch.object(M, "merged_done", return_value=merged), \
                mock.patch.object(M, "merged_pr", return_value=info):
            D.tick(T0 + timedelta(minutes=minutes))
        return launch

    def test_tk266_closed_by_dispatcher_no_runs_one_signal(self):
        p = self.accepted()
        launch = self.tick(20)
        launch.assert_not_called()
        tkt = T.read_ticket(p)
        self.assertEqual((tkt.status, tkt.header.get("wait_for")), ("done", ""))
        last = tkt.log[-1]
        self.assertEqual(last.author, "dispatcher")
        self.assertIn("результат: docs/reviews/TK-1.md", last.text)
        self.assertTrue(SIG.closed_without_review(tkt))
        self.assertEqual(len(self.wake_lines()), 1)
        # CEO закрыл гонкой следом, диспетчер тикает дальше — ни запусков, ни второго сигнала
        T.append_log(p, "ceo", "[итог: done] результат: x (закрыто без ревью) — закрыл", now=T0 + timedelta(minutes=21))
        T.write_header_updates(p, {"status": "done"}, now=T0 + timedelta(minutes=21))
        self.tick(22).assert_not_called()
        self.tick(23).assert_not_called()
        self.assertEqual(len(self.wake_lines()), 1)

    def test_ceo_done_first_then_merge_one_signal_no_runs(self):
        p = self.accepted()
        self.tick(2, merged=False)  # первый тик ставит базу done — история не пересказывается
        T.append_log(p, "ceo", "[итог: done] результат: x (закрыто без ревью) — закрыл", now=T0 + timedelta(minutes=5))
        T.write_header_updates(p, {"status": "done"}, now=T0 + timedelta(minutes=5))
        self.tick(6).assert_not_called()
        self.tick(20).assert_not_called()
        self.assertEqual(len(self.wake_lines()), 1)

    def test_other_head_not_closed(self):
        p = self.accepted(sha="aaaaaaa")
        launch = self.tick(20)
        self.assertEqual(T.read_ticket(p).status, "waiting")
        self.assertEqual(launch.call_args.kwargs.get("reason"), "wait_for-met")

    def test_merged_into_non_default_base_not_closed(self):  # заказ CEO 04:37: alpha TK-263, база tk262
        p = self.accepted()
        launch = self.tick(20, base="tk262")
        self.assertEqual(T.read_ticket(p).status, "waiting")
        self.assertEqual(launch.call_args.args[1], "engineer")
        self.assertEqual(launch.call_args.kwargs["reason"], "wait_for-met")

    def test_executor_wait_after_accept_wakes_executor(self):  # alpha TK-258, TK-077: после слияния — выкладка/выпуск
        p = self.accepted()
        T.append_log(p, "engineer", "[итог: wait] — слияние, потом выкладка", now=T0 + timedelta(minutes=2))
        launch = self.tick(20)
        self.assertEqual(T.read_ticket(p).status, "waiting")
        self.assertEqual(launch.call_args.args[1], "engineer")
        self.assertEqual(launch.call_args.kwargs["reason"], "wait_for-met")

    def test_judge_wait_after_accept_still_closes(self):
        p = self.accepted()
        T.append_log(p, "judge", "[итог: wait] — ждём слияния", now=T0 + timedelta(minutes=2))
        self.assertEqual(self.tick(20).call_count, 0)
        self.assertEqual(T.read_ticket(p).status, "done")

    def test_not_merged_not_closed(self):
        p = self.accepted()
        self.tick(20, merged=False).assert_not_called()
        self.assertEqual(T.read_ticket(p).status, "waiting")

    def test_no_accept_not_closed(self):
        p = self.ticket(wait_for=WAIT)
        launch = self.tick(20)
        self.assertEqual(T.read_ticket(p).status, "waiting")
        self.assertEqual(launch.call_args.kwargs.get("reason"), "wait_for-met")

    def test_ceo_order_after_accept_wakes_author_with_reason(self):  # П2б, TK-283
        p = self.accepted()
        T.append_log(p, "ceo", "Правило: elastic только для заданий ≤ 1 ч — учти.", now=T0 + timedelta(minutes=5))
        launch = self.tick(20)
        self.assertEqual(T.read_ticket(p).status, "waiting")
        launch.assert_called_once()
        self.assertEqual(launch.call_args.args[1], "engineer")
        self.assertEqual(launch.call_args.kwargs["reason"], "ceo-order-after-accept")

    def test_ceo_done_entry_is_not_an_order(self):
        p = self.accepted()
        T.append_log(p, "ceo", "[итог: done] — x", now=T0 + timedelta(minutes=5))
        self.assertFalse(DEC._ceo_order_after_accept(T.read_ticket(p), 44))


class ExecutorBlockedTests(Sandbox):
    """#107 П1, #109 п.2: blocked/ask-owner исполнителя идёт Судье — CEO узнаёт, когда Судья ответил blocked/ask-owner."""

    def blocked(self, role, result):
        p = self.ticket(status="in_progress", owner=role)
        T.append_log(p, role, f"[итог: {result}] — вопрос по методике", now=T0)
        T.write_header_updates(p, {"status": "blocked", "next": "judge"}, now=T0)
        return p

    def tick(self, minutes):
        with mock.patch.object(D, "launch_run"):
            D.tick(T0 + timedelta(minutes=minutes))

    def test_executor_blocked_and_ask_owner_do_not_wake_ceo(self):
        for result in ("blocked", "ask-owner"):
            p = self.blocked("engineer", result)
            self.tick(1)
            T.write_header_updates(p, {"next": ""}, now=T0)  # Судья запущен, поле очищено; статус пока blocked
            self.tick(2)
            self.assertEqual(self.wake_lines(), [], result)
            p.unlink()

    def test_judge_answer_blocked_or_ask_owner_wakes_ceo_once(self):
        p = self.blocked("engineer", "ask-owner")
        self.tick(1)
        T.append_log(p, "judge", "[итог: ask-owner] повод: деньги — нужен бюджет", now=T0 + timedelta(minutes=3))
        T.write_header_updates(p, {"status": "needs_owner", "next": ""}, now=T0 + timedelta(minutes=3))
        self.tick(4)
        self.tick(5)
        self.assertEqual(len(self.wake_lines()), 1)

    def watch_wake(self, p):
        W.notify_findings(W.collect_findings(T0 + timedelta(minutes=10)), {}, T0 + timedelta(minutes=10))
        return self.wake_lines()

    def test_watch_executor_blocked_and_ask_owner_do_not_wake_ceo(self):  # E-002: сторож не будит CEO на blocked исполнителя
        for role in ("engineer", "researcher"):
            for result in ("blocked", "ask-owner"):
                p = self.blocked(role, result)
                self.assertEqual(self.watch_wake(p), [], f"{role} {result} next=judge")
                T.write_header_updates(p, {"next": ""}, now=T0)  # Судья запущен, поле очищено; статус пока blocked
                self.assertEqual(self.watch_wake(p), [], f"{role} {result} next пуст")
                p.unlink()

    def test_watch_wakes_ceo_on_judge_blocked_and_manual_blocked(self):
        p = self.blocked("engineer", "blocked")
        T.append_log(p, "judge", "[итог: blocked] нужен доступ", now=T0 + timedelta(minutes=3))
        T.write_header_updates(p, {"next": ""}, now=T0 + timedelta(minutes=3))
        self.assertEqual(len(self.watch_wake(p)), 1)
        p.unlink()
        CFG.CEO_WAKE_LOG.unlink()
        p = self.ticket(status="in_progress")
        T.append_log(p, "engineer", "стоп, не могу", now=T0)
        T.write_header_updates(p, {"status": "blocked"}, now=T0)
        self.assertEqual(len(self.watch_wake(p)), 1)

    def test_dispatcher_grace_silent_then_signal_once(self):  # заказ CEO 04:37: Судье 25 мин, потом сигнал CEO
        p = self.blocked("engineer", "blocked")
        self.tick(24)
        self.assertEqual(self.wake_lines(), [])
        self.tick(26)
        self.tick(27)
        self.assertEqual(len(self.wake_lines()), 1)
        T.write_header_updates(p, {"next": ""}, now=T0)
        self.tick(30)
        self.assertEqual(len(self.wake_lines()), 1)

    def test_watch_grace_silent_then_signal_with_age(self):
        self.blocked("engineer", "ask-owner")
        for minutes in (24, 26):
            now = T0 + timedelta(minutes=minutes)
            W.notify_findings(W.collect_findings(now), {}, now)
            self.assertEqual(len(self.wake_lines()), int(minutes > 25), minutes)
        self.assertIn("blocked 26 мин", CFG.CEO_INBOX.read_text(encoding="utf-8"))

    def test_watch_age_in_hours(self):
        self.blocked("researcher", "blocked")
        now = T0 + timedelta(hours=26, minutes=10)
        W.notify_findings(W.collect_findings(now), {}, now)
        self.assertIn("blocked 26 ч", CFG.CEO_INBOX.read_text(encoding="utf-8"))

    def test_manual_blocked_without_result_still_wakes_ceo(self):
        p = self.ticket(status="in_progress")
        T.append_log(p, "engineer", "стоп, не могу", now=T0)
        T.write_header_updates(p, {"status": "blocked"}, now=T0)
        self.tick(1)
        self.assertEqual(len(self.wake_lines()), 1)


class OrphanDedupTests(Sandbox):
    """TK-278 02:20: «ожидание снято» и orphan-ticket на тот же тикет — одна находка, не две."""

    def make(self, released_ago_min):
        now = T.parse_dt("2026-10-17T12:00:00+04:00")
        p = self.ticket(status="in_progress")
        T.append_log(p, "engineer", "ждём", now=now - timedelta(hours=5))
        if released_ago_min is not None:
            T.append_log(p, "watch", "сторож: задание не живёт — ожидание снято, владелец будится",
                         now=now - timedelta(minutes=released_ago_min))
        return now

    def test_orphan_silent_right_after_release(self):
        now = self.make(0)
        self.assertEqual(W.check_orphan_tickets(now), [])

    def test_orphan_returns_after_ten_minutes(self):
        now = self.make(11)
        self.assertEqual([f.kind for f in W.check_orphan_tickets(now)], ["orphan-ticket"])

    def test_orphan_without_release_is_found(self):
        now = self.make(None)
        self.assertEqual(len(W.check_orphan_tickets(now)), 1)


if __name__ == "__main__":
    unittest.main()
