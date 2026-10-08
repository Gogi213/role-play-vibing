"""Тесты сторожа CEO без модели (судья TK-002 п.2). По фикстуре на каждое условие (п.2д). stdlib
`unittest`, без сети (ssh — через инъекцию `ssh_run`, как просит сам watch.py)."""
from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

# проект-пустышка с `.claude/roles`: импорт диспетчера не должен найти рабочий проект выше папки плагина
_SANDBOX = tempfile.mkdtemp(prefix="rpv-test-proj-")
os.makedirs(os.path.join(_SANDBOX, ".claude", "roles"))
os.environ["CLAUDE_PROJECT_DIR"] = _SANDBOX
atexit.register(shutil.rmtree, _SANDBOX, True)
sys.path.insert(0, str(Path(__file__).resolve().parent))
import dispatch as D  # noqa: E402
import ticket as T  # noqa: E402
import watch as W  # noqa: E402

TZ = timezone(timedelta(hours=4))


def dt(s: str) -> datetime:
    return T.parse_dt(s)


class WatchSandbox(unittest.TestCase):
    """База: временные TICKETS_DIR/STATE_FILE/CEO_INBOX/heartbeat/watch-state — ничего боевого не трогаем."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.tickets_dir = base / "tickets"
        self.tickets_dir.mkdir(parents=True)
        self._orig = {
            "TICKETS_DIR": D.TICKETS_DIR, "STATE_FILE": D.STATE_FILE, "CEO_INBOX": D.CEO_INBOX,
            "CEO_WAKE_LOG": D.CEO_WAKE_LOG,
        }
        D.TICKETS_DIR = self.tickets_dir
        D.STATE_FILE = base / "state.json"
        D.CEO_INBOX = base / "ceo-inbox.md"
        D.CEO_WAKE_LOG = base / "ceo-wake.log"
        W.WATCH_STATE_FILE = base / "watch-state.json"
        W.WATCH_HEARTBEAT_FILE = base / "watch-heartbeat.json"
        W.DECK_OFF_FLAG = base / "deck-off"  # боевой флаг не влияет на тесты; тест «флаг есть» создаёт его сам
        self.now = dt("2026-09-27T12:00:00+04:00")
        os.environ["ALPHA_DECK_HOST"] = "deck@test-host"  # без переменной проверки второй машины выключены
        self.addCleanup(lambda: os.environ.pop("ALPHA_DECK_HOST", None))

    def tearDown(self):
        for k, v in self._orig.items():
            setattr(D, k, v)
        self.tmp.cleanup()


class DispatcherAliveTests(WatchSandbox):
    def test_no_last_tick_is_a_finding(self):
        findings = W.check_dispatcher_alive({}, self.now)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].kind, "dispatcher-down")

    def test_stale_last_tick_is_a_finding(self):
        state = {"last_tick": T.now_iso(self.now - timedelta(minutes=D.RUN_TIMEOUT))}
        findings = W.check_dispatcher_alive(state, self.now)
        self.assertEqual(len(findings), 1)

    def test_fresh_last_tick_is_silent(self):
        state = {"last_tick": T.now_iso(self.now - timedelta(seconds=30))}
        self.assertEqual(W.check_dispatcher_alive(state, self.now), [])


class NoDeckHostTests(WatchSandbox):
    def test_without_deck_host_variable_deck_checks_are_off(self):
        os.environ.pop("ALPHA_DECK_HOST", None)
        calls = []
        self.assertEqual(W.check_second_machine(lambda *a, **kw: calls.append(a) or (True, "")), [])
        self.assertEqual(calls, [])


class NoMoneyWatchTests(WatchSandbox):
    """В-173 (на тикет) и В-149 (в час/в сутки): лимитов денег нет — сторож трат не проверяет и не сигналит."""

    def test_money_checks_are_gone(self):
        self.assertFalse(hasattr(W, "check_budgets"))
        self.assertNotIn("budget-watch", W.WATCH_LONG_REPEAT_KINDS)

    def test_huge_spend_gives_no_finding(self):
        p = T.create_ticket(self.tickets_dir, owner="engineer", title="Дорогая", status="todo")
        W.DECK_OFF_FLAG.write_text("x", encoding="utf-8")  # ssh не нужен
        state = {"last_tick": T.now_iso(self.now - timedelta(seconds=10)),
                 "daily_cost": {D._today(self.now): 1e6}, "cost_history": [[T.now_iso(self.now), 1e6]],
                 "ticket_cost": {p.stem: 1e6}, "ticket_budget": {p.stem: 5.0}}  # ticket_budget — наследие state.json
        self.assertEqual(W.collect_findings(state, self.now), [])


class OrphanRepeatTests(WatchSandbox):
    def test_orphan_repeats_at_most_once_per_24h_and_closed_never(self):
        ws = {}
        f = [W.Finding("orphan-ticket", "TK-14", "TK-14: status=waiting без новой записи 90.2 ч")]
        self.assertEqual(len(W.notify_findings(f, ws, self.now)), 1)
        self.assertEqual(W.notify_findings(f, ws, self.now + timedelta(hours=5)), [])
        self.assertEqual(len(W.notify_findings(f, ws, self.now + timedelta(hours=24, minutes=1))), 1)

class BlockedNeedsOwnerTests(WatchSandbox):
    def test_blocked_and_needs_owner_are_findings(self):
        T.create_ticket(self.tickets_dir, owner="engineer", title="Застряла", status="todo")
        p2 = T.create_ticket(self.tickets_dir, owner="researcher", title="Ждёт владельца", status="todo")
        T.write_header_updates(p2, {"status": "needs_owner"})
        findings = W.check_blocked_and_needs_owner(self.now)
        kinds = {f.kind for f in findings}
        self.assertIn("needs_owner", kinds)

    def test_todo_is_silent(self):
        T.create_ticket(self.tickets_dir, owner="engineer", title="Обычная", status="todo")
        self.assertEqual(W.check_blocked_and_needs_owner(self.now), [])

    def test_unreadable_ticket_is_a_signal_not_silence(self):
        (self.tickets_dir / "BROKEN.md").write_text("нет шапки\n", encoding="utf-8")
        findings = W.check_blocked_and_needs_owner(self.now)
        self.assertTrue(any(f.kind == "ticket-unreadable" for f in findings))


class OrphanTicketTests(WatchSandbox):
    def test_stale_in_progress_is_orphan(self):
        p = T.create_ticket(self.tickets_dir, owner="engineer", title="Забытая", status="todo",
                             now=self.now - timedelta(hours=5))
        T.write_header_updates(p, {"status": "in_progress"}, now=self.now - timedelta(hours=5))
        T.append_log(p, "engineer", "начал", now=self.now - timedelta(hours=5))
        findings = W.check_orphan_tickets(self.now)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].kind, "orphan-ticket")

    def test_stale_in_review_is_orphan(self):
        """A4: `in_review` без новой записи дольше порога — сигнал, как для in_progress/waiting (ревьюер мог упасть)."""
        p = T.create_ticket(self.tickets_dir, owner="researcher", title="Зависла на ревью", status="todo",
                             reviewer="judge", now=self.now - timedelta(hours=5))
        T.write_header_updates(p, {"status": "in_review"}, now=self.now - timedelta(hours=5))
        T.append_log(p, "researcher", "готово, прошу проверку", now=self.now - timedelta(hours=5))
        findings = W.check_orphan_tickets(self.now)
        self.assertEqual([f.kind for f in findings], ["orphan-ticket"])
        self.assertIn("in_review", findings[0].message)

    def test_recent_in_review_is_silent(self):
        p = T.create_ticket(self.tickets_dir, owner="researcher", title="Свежее ревью", status="todo",
                             reviewer="judge")
        T.write_header_updates(p, {"status": "in_review"})
        T.append_log(p, "researcher", "прошу проверку", now=self.now - timedelta(minutes=5))
        self.assertEqual(W.check_orphan_tickets(self.now), [])

    def test_recent_in_progress_is_silent(self):
        p = T.create_ticket(self.tickets_dir, owner="engineer", title="Свежая", status="todo")
        T.write_header_updates(p, {"status": "in_progress"})
        T.append_log(p, "engineer", "начал", now=self.now - timedelta(minutes=5))
        self.assertEqual(W.check_orphan_tickets(self.now), [])

    def test_done_ticket_is_not_orphan_even_if_old(self):
        p = T.create_ticket(self.tickets_dir, owner="engineer", title="Готова", status="todo",
                             now=self.now - timedelta(hours=10))
        T.write_header_updates(p, {"status": "done"}, now=self.now - timedelta(hours=10))
        self.assertEqual(W.check_orphan_tickets(self.now), [])


    def test_cancelled_ticket_is_not_orphan_even_if_old(self):
        p = T.create_ticket(self.tickets_dir, owner="engineer", title="Отменена", status="todo",
                             now=self.now - timedelta(hours=10))
        T.write_header_updates(p, {"status": "cancelled"}, now=self.now - timedelta(hours=10))
        T.append_log(p, "engineer", "отменено", now=self.now - timedelta(hours=10))
        self.assertEqual(W.check_orphan_tickets(self.now), [])

    def test_stopped_ticket_is_not_orphan_even_if_old(self):
        """`stopped` (CEO остановил роль без --next) ждёт слова CEO — не сирота, сколько бы ни лежал."""
        p = T.create_ticket(self.tickets_dir, owner="engineer", title="Остановлена", status="todo",
                             now=self.now - timedelta(hours=10))
        T.write_header_updates(p, {"status": "stopped"}, now=self.now - timedelta(hours=10))
        T.append_log(p, "ceo", "Новая постановка", now=self.now - timedelta(hours=10))
        self.assertEqual(W.check_orphan_tickets(self.now), [])

class SecondMachineWatchTests(WatchSandbox):
    def test_alert_file_becomes_finding(self):
        def fake_ssh(cmd, timeout=10.0):
            if "ALERT" in cmd:
                return True, "ALERT-rework: 54 суток разобраны повторно"
            return True, ""
        findings = W.check_second_machine(fake_ssh)
        self.assertTrue(any(f.kind == "deck-alert" for f in findings))

    def test_ssh_failure_is_a_signal_not_silence(self):
        """п.2в: ошибка ssh/чтения — сигнал, не молчание."""
        def fake_ssh(cmd, timeout=10.0):
            return False, "Connection timed out"
        findings = W.check_second_machine(fake_ssh)
        self.assertTrue(any(f.kind == "deck-ssh-error" for f in findings))
        self.assertEqual(len(findings), 3)  # alerts-, hold- и frozen-запросы — все упали

    def test_no_alerts_and_empty_queue_is_silent(self):
        def fake_ssh(cmd, timeout=10.0):
            if "ALERT" in cmd:
                return True, ""
            return True, "HOLD"
        self.assertEqual(W.check_second_machine(fake_ssh), [])

    def test_idle_with_pending_queue_is_a_finding(self):
        def fake_ssh(cmd, timeout=10.0):
            if "ALERT" in cmd:
                return True, ""
            return True, f"3 {int(W.DECK_QUEUE_STALE_MINUTES * 60) + 120}"
        findings = W.check_second_machine(fake_ssh)
        self.assertTrue(any(f.kind == "deck-queue-stale" for f in findings))

    def test_queue_count_reads_pending_jobs_dir(self):
        """задания очереди — queue/pending/*.job (а не queue/*.json)."""
        cmds = []
        def fake_ssh(cmd, timeout=10.0):
            cmds.append(cmd)
            return True, ""
        W.check_second_machine(fake_ssh)
        queue_cmd = next(c for c in cmds if "STATUS" in c)
        self.assertIn("queue/pending/", queue_cmd)
        self.assertIn(".job", queue_cmd)

    def test_disk_full_mark_is_frozen_finding(self):
        """метка DISK-FULL → deck-frozen сразу, с числом юнитов и свободным местом."""
        def fake_ssh(cmd, timeout=10.0):
            if "DISK-FULL" in cmd:
                return True, "mark 10\nfree 8\n"
            if "ALERT" in cmd:
                return True, ""
            return True, "HOLD"
        f = [x for x in W.check_second_machine(fake_ssh) if x.kind == "deck-frozen"]
        self.assertEqual(len(f), 1)
        self.assertIn("10", f[0].message)
        self.assertIn("свободно 8 ГБ", f[0].message)
        self.assertNotIn("deck-frozen", W.WATCH_SUMMARY_KINDS)  # будит сразу, не сводкой

    def test_frozen_units_without_mark_is_frozen_finding(self):
        """TK-016 (3): systemctl --user list-units --state=frozen не пуст → deck-frozen."""
        def fake_ssh(cmd, timeout=10.0):
            if "DISK-FULL" in cmd:
                return True, "nomark\nfree 40\nfrozen queue-worker.service\n"
            if "ALERT" in cmd:
                return True, ""
            return True, "HOLD"
        f = [x for x in W.check_second_machine(fake_ssh) if x.kind == "deck-frozen"]
        self.assertEqual(len(f), 1)
        self.assertIn("queue-worker.service", f[0].message)

    def test_no_mark_no_frozen_is_silent(self):
        self.assertEqual(W.check_deck_frozen(lambda c, timeout=10.0: (True, "nomark\nfree 40\n")), [])

    def test_active_queue_is_silent(self):
        def fake_ssh(cmd, timeout=10.0):
            if "ALERT" in cmd:
                return True, ""
            return True, "3 30"  # свежо
        self.assertEqual(W.check_second_machine(fake_ssh), [])


class DeckOffFlagTests(WatchSandbox):
    """Флаг `deck-off` — сторож не ходит на вторую машину (ssh_run не вызывается)."""

    def _counting_ssh(self):
        calls = []

        def fake_ssh(cmd, timeout=10.0):
            calls.append(cmd)
            return False, "Connection timed out"
        return calls, fake_ssh

    def test_flag_present_check_second_machine_makes_no_ssh_call(self):
        W.DECK_OFF_FLAG.write_text("владелец: вторую машину не трогаем", encoding="utf-8")
        calls, fake_ssh = self._counting_ssh()
        self.assertEqual(W.check_second_machine(fake_ssh), [])
        self.assertEqual(calls, [])

    def test_flag_present_check_deck_frozen_makes_no_ssh_call(self):
        W.DECK_OFF_FLAG.write_text("x", encoding="utf-8")
        calls, fake_ssh = self._counting_ssh()
        self.assertEqual(W.check_deck_frozen(fake_ssh), [])
        self.assertEqual(calls, [])

    def test_flag_present_run_once_no_ssh_and_no_deck_findings(self):
        W.DECK_OFF_FLAG.write_text("x", encoding="utf-8")
        calls, fake_ssh = self._counting_ssh()
        posted = W.run_once(self.now, ssh_run=fake_ssh)
        self.assertEqual(calls, [])
        self.assertFalse(any(f.kind.startswith("deck-") for f in posted))
        self.assertTrue(W.WATCH_HEARTBEAT_FILE.exists())  # сторож жив, остальные проверки идут

    def test_flag_appearing_between_cycles_takes_effect_without_restart(self):
        calls, fake_ssh = self._counting_ssh()
        W.run_once(self.now, ssh_run=fake_ssh)
        self.assertTrue(calls)  # флага нет — прежнее поведение, на деку ходим
        calls.clear()
        W.DECK_OFF_FLAG.write_text("x", encoding="utf-8")
        W.run_once(self.now + timedelta(minutes=2), ssh_run=fake_ssh)
        self.assertEqual(calls, [])
        W.DECK_OFF_FLAG.unlink()
        W.run_once(self.now + timedelta(minutes=4), ssh_run=fake_ssh)
        self.assertTrue(calls)  # флаг снят — снова ходим

    def test_without_flag_behaviour_unchanged(self):
        calls, fake_ssh = self._counting_ssh()
        findings = W.check_second_machine(fake_ssh)
        self.assertEqual(len(calls), 3)  # alerts, hold, frozen
        self.assertTrue(any(f.kind == "deck-ssh-error" for f in findings))


class SshEncodingTests(WatchSandbox):
    def test_ssh_run_decodes_as_utf8(self):
        """CEO 27.09: вывод ssh шёл кракозябрами — subprocess.run без явной кодировки брал локальную
        (Windows-консоль), как уже исправлено для role_memory.py:deck_alert."""
        captured = {}
        orig_run = W.subprocess.run

        class FakeResult:
            returncode = 0
            stdout = "ALERT-rework: очередь застряла"
            stderr = ""

        def fake_run(cmd, **kwargs):
            captured.update(kwargs)
            return FakeResult()

        W.subprocess.run = fake_run
        try:
            W._ssh_run("echo test")
        finally:
            W.subprocess.run = orig_run
        self.assertEqual(captured.get("encoding"), "utf-8")
        self.assertEqual(captured.get("errors"), "replace")


class SshImmediateRetryTests(WatchSandbox):
    """CEO 27.09: разовый ssh-таймаут (23:59, 00:11), а сразу следом ssh отвечал за 0,44 с — один
    немедленный повтор внутри _ssh_run должен был отфильтровать это ещё до классификации находки."""

    def test_success_on_immediate_retry_counts_as_success(self):
        calls = []

        def fake_once(cmd_suffix, timeout=10.0):
            calls.append(cmd_suffix)
            if len(calls) == 1:
                return False, "Connection timed out"
            return True, "ok"

        orig = W._ssh_run_once
        W._ssh_run_once = fake_once
        try:
            ok, out = W._ssh_run("echo test")
        finally:
            W._ssh_run_once = orig
        self.assertTrue(ok)
        self.assertEqual(out, "ok")
        self.assertEqual(len(calls), 2)

    def test_failure_persists_after_retry_exhausted(self):
        def fake_once(cmd_suffix, timeout=10.0):
            return False, "Connection timed out"

        orig = W._ssh_run_once
        W._ssh_run_once = fake_once
        try:
            ok, out = W._ssh_run("echo test")
        finally:
            W._ssh_run_once = orig
        self.assertFalse(ok)

    def test_first_success_makes_no_retry_call(self):
        calls = []

        def fake_once(cmd_suffix, timeout=10.0):
            calls.append(cmd_suffix)
            return True, "ok"

        orig = W._ssh_run_once
        W._ssh_run_once = fake_once
        try:
            W._ssh_run("echo test")
        finally:
            W._ssh_run_once = orig
        self.assertEqual(len(calls), 1)


class SshFailStreakTests(WatchSandbox):
    """CEO 27.09: будить только после N ПОДРЯД неудачных ЦИКЛОВ; разовые/парные — в сводку."""

    def test_first_two_failures_are_transient_not_wake(self):
        ws = {}
        f = [W.Finding("deck-ssh-error", "alerts", "timeout")]
        out1 = W._apply_ssh_fail_streak(f, ws)
        self.assertEqual(out1[0].kind, "deck-ssh-error-transient")
        out2 = W._apply_ssh_fail_streak(f, ws)
        self.assertEqual(out2[0].kind, "deck-ssh-error-transient")

    def test_third_consecutive_failure_wakes(self):
        ws = {}
        f = [W.Finding("deck-ssh-error", "alerts", "timeout")]
        W._apply_ssh_fail_streak(f, ws)
        W._apply_ssh_fail_streak(f, ws)
        out3 = W._apply_ssh_fail_streak(f, ws)
        self.assertEqual(out3[0].kind, "deck-ssh-error")

    def test_success_in_between_resets_streak(self):
        ws = {}
        f_fail = [W.Finding("deck-ssh-error", "alerts", "timeout")]
        W._apply_ssh_fail_streak(f_fail, ws)
        W._apply_ssh_fail_streak(f_fail, ws)
        W._apply_ssh_fail_streak([], ws)  # цикл без ошибки — ssh снова отвечает
        out = W._apply_ssh_fail_streak(f_fail, ws)
        self.assertEqual(out[0].kind, "deck-ssh-error-transient", "счётчик должен был сброситься")

    def test_non_ssh_findings_are_untouched(self):
        ws = {}
        f = [W.Finding("orphan-ticket", "TK-1", "застряла")]
        out = W._apply_ssh_fail_streak(f, ws)
        self.assertEqual(out, f)

    def test_end_to_end_two_transient_cycles_go_to_summary_not_inbox(self):
        def fake_ssh_always_fails(cmd, timeout=10.0):
            return False, "Connection timed out"

        W.run_once(self.now, ssh_run=fake_ssh_always_fails)
        W.run_once(self.now + timedelta(minutes=2), ssh_run=fake_ssh_always_fails)
        inbox = D.CEO_INBOX.read_text(encoding="utf-8") if D.CEO_INBOX.exists() else ""
        self.assertNotIn("[watch-deck-ssh-error]", inbox)
        self.assertIn("watch-summary", inbox)

    def test_end_to_end_third_consecutive_cycle_wakes(self):
        def fake_ssh_always_fails(cmd, timeout=10.0):
            return False, "Connection timed out"

        for i in range(3):
            W.run_once(self.now + timedelta(minutes=2 * i), ssh_run=fake_ssh_always_fails)
        inbox = D.CEO_INBOX.read_text(encoding="utf-8")
        self.assertIn("watch-deck-ssh-error", inbox)


class SecondMachineHoldTests(WatchSandbox):
    """CEO 27.09: ALERT-idle-deck при активном HOLD — ожидаемое состояние (паузу ставит CEO по слову
    владельца) — не будить, а в сводку; другие тревоги (например ALERT-rework) под HOLD всё равно будят."""

    def test_idle_deck_alert_under_hold_goes_to_summary_kind(self):
        def fake_ssh(cmd, timeout=10.0):
            if "ALERT" in cmd:
                return True, "ALERT-idle-deck: очередь простаивает 18:26Z"
            return True, "HOLD"
        findings = W.check_second_machine(fake_ssh)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].kind, "deck-idle-expected")

    def test_other_alert_under_hold_still_wakes(self):
        def fake_ssh(cmd, timeout=10.0):
            if "ALERT" in cmd:
                return True, "ALERT-rework: 54 суток разобраны повторно 18:26Z"
            return True, "HOLD"
        findings = W.check_second_machine(fake_ssh)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].kind, "deck-alert")

    def test_idle_deck_alert_without_hold_still_wakes(self):
        """ALERT-idle-deck без HOLD — это уже НЕ ожидаемое состояние, будим как обычно."""
        def fake_ssh(cmd, timeout=10.0):
            if "ALERT" in cmd:
                return True, "ALERT-idle-deck: простаивает"
            return True, "NOHOLD"
        findings = W.check_second_machine(fake_ssh)
        self.assertTrue(any(f.kind == "deck-alert" for f in findings))
        self.assertFalse(any(f.kind == "deck-idle-expected" for f in findings))

    def test_summary_kind_goes_out_as_batched_summary_not_bare_wake(self):
        """Формат — «watch-summary» пачкой, не отдельная срочная строка watch-deck-idle-expected."""
        ws = {}
        f = [W.Finding("deck-idle-expected", "ALERT-idle-deck", "простаивает 18:26Z")]
        W.notify_findings(f, ws, self.now)
        inbox = D.CEO_INBOX.read_text(encoding="utf-8")
        self.assertIn("watch-summary", inbox)
        self.assertNotIn("watch-deck-idle-expected", inbox)

    def test_second_occurrence_within_window_batches_not_immediate(self):
        ws = {}
        f1 = [W.Finding("deck-idle-expected", "ALERT-idle-deck", "простаивает 18:26Z")]
        W.notify_findings(f1, ws, self.now)  # первое — само задаёт точку отсчёта окна и уходит сразу
        before = D.CEO_INBOX.read_text(encoding="utf-8")
        f2 = [W.Finding("deck-idle-expected", "ALERT-idle-deck", "простаивает ДРУГАЯ ПРИЧИНА")]
        W.notify_findings(f2, ws, self.now + timedelta(minutes=5))
        after = D.CEO_INBOX.read_text(encoding="utf-8")
        self.assertEqual(before, after, "второе в течение окна должно копиться, не уходить немедленно")
        later = self.now + timedelta(hours=W.WATCH_SUMMARY_EVERY_HOURS, minutes=5)
        W.notify_findings(f2, ws, later)
        self.assertIn("ДРУГАЯ ПРИЧИНА", D.CEO_INBOX.read_text(encoding="utf-8"))


    # --- v2 (02.10): таймаут проверки HOLD не превращает ожидаемый простой в тревогу ---

    def idle_ssh(self, hold_answer):
        def fake_ssh(cmd, timeout=10.0):
            if "ALERT" in cmd:
                return True, "ALERT-idle-deck: очередь простаивает"
            if "HOLD" in cmd and "NOHOLD" in cmd:  # проверка HOLD
                return hold_answer
            return True, ""
        return fake_ssh

    def test_hold_check_timeout_with_hold_hint_keeps_idle_expected(self):
        findings = W.check_second_machine(self.idle_ssh((False, "TimeoutExpired: timed out")), hold_hint=True)
        kinds = {f.kind for f in findings}
        self.assertIn("deck-idle-expected", kinds)
        self.assertNotIn("deck-alert", kinds)
        self.assertIn("deck-ssh-error", kinds)  # сам сбой проверки HOLD по-прежнему сообщается (через серию)

    def test_hold_check_timeout_without_hint_is_still_not_an_alert(self):
        findings = W.check_second_machine(self.idle_ssh((False, "TimeoutExpired: timed out")), hold_hint=None)
        self.assertNotIn("deck-alert", {f.kind for f in findings})

    def test_hold_check_timeout_with_nohold_hint_is_a_real_alert(self):
        findings = W.check_second_machine(self.idle_ssh((False, "TimeoutExpired: timed out")), hold_hint=False)
        self.assertIn("deck-alert", {f.kind for f in findings})

    def test_observed_hold_is_recorded_when_check_succeeds(self):
        observed = {}
        W.check_second_machine(self.idle_ssh((True, "HOLD")), observed=observed)
        self.assertIs(observed["hold"], True)
        observed = {}
        W.check_second_machine(self.idle_ssh((True, "NOHOLD")), observed=observed)
        self.assertIs(observed["hold"], False)
        observed = {}
        W.check_second_machine(self.idle_ssh((False, "timeout")), observed=observed)
        self.assertNotIn("hold", observed)

    def test_run_once_remembers_hold_and_uses_it_when_next_check_times_out(self):
        W.run_once(self.now, ssh_run=self.idle_ssh((True, "HOLD")))
        self.assertIs(W.load_watch_state()["deck_hold"], True)
        W.run_once(self.now + timedelta(minutes=2), ssh_run=self.idle_ssh((False, "TimeoutExpired")))
        inbox = D.CEO_INBOX.read_text(encoding="utf-8") if D.CEO_INBOX.exists() else ""
        self.assertNotIn("watch-deck-alert", inbox)
        self.assertIs(W.load_watch_state()["deck_hold"], True, "неудачная проверка подсказку не затирает")


class ContentFingerprintDedupTests(WatchSandbox):
    """CEO 27.09: ALERT-rework перезаписывается каждые ~15 мин с той же сутью, новой меткой времени —
    сравнивать текст без времени, будить один раз, не на каждое перезаписывание."""

    def test_same_content_different_timestamp_is_not_reposted(self):
        ws = {}
        W.notify_findings([W.Finding("deck-alert", "ALERT-rework", "разобрано повторно 18:26Z")], ws, self.now)
        posted2 = W.notify_findings(
            [W.Finding("deck-alert", "ALERT-rework", "разобрано повторно 18:41Z")], ws,
            self.now + timedelta(minutes=15))
        self.assertEqual(posted2, [])

    def test_genuinely_different_content_reposts_immediately(self):
        ws = {}
        W.notify_findings([W.Finding("deck-alert", "ALERT-rework", "разобрано повторно 18:26Z")], ws, self.now)
        posted2 = W.notify_findings(
            [W.Finding("deck-alert", "ALERT-rework", "СОВСЕМ ДРУГАЯ ПРИЧИНА 18:41Z")], ws,
            self.now + timedelta(minutes=15))
        self.assertEqual(len(posted2), 1)

    def test_non_deck_kinds_unaffected_by_message_drift(self):
        """orphan-ticket сообщения естественно меняются (возраст) — это НЕ повод
        считать сигнал новым; сигнатура для них не участвует, только временное окно."""
        ws = {}
        W.notify_findings([W.Finding("orphan-ticket", "TK-1", "TK-1: без записи 2.0 ч")], ws, self.now)
        posted2 = W.notify_findings([W.Finding("orphan-ticket", "TK-1", "TK-1: без записи 2.3 ч")], ws,
                                     self.now + timedelta(minutes=15))
        self.assertEqual(posted2, [])

    def test_same_content_after_long_repeat_window_reposts_as_reminder(self):
        """v2: та же тревога второй машины — раз в сутки (WATCH_LONG_REPEAT_HOURS), не раз в 2 ч; пока нет суток —
        тишина."""
        ws = {}
        f = [W.Finding("deck-alert", "ALERT-rework", "разобрано повторно 18:26Z")]
        W.notify_findings(f, ws, self.now)
        self.assertEqual(W.notify_findings(f, ws, self.now + timedelta(hours=W.WATCH_DEDUP_REPEAT_HOURS + 1)), [])
        later = self.now + timedelta(hours=W.WATCH_LONG_REPEAT_HOURS, minutes=1)
        posted2 = W.notify_findings(f, ws, later)
        self.assertEqual(len(posted2), 1)

    # --- v2 (02.10): нормализованная сигнатура — реальные тексты ALERT-* из ceo-inbox ---

    REWORK_A = ("Вторая машина: ALERT-rework: 2026-10-01T23:12:59Z 1 суток разобраны повторно за 24 ч (всего разборов 3); "
                "больше всех — /home/user/rpv/tk022/view/2026-01-01/root-2026-01-01: 3 р")
    REWORK_B = ("Вторая машина: ALERT-rework: 2026-10-02T00:03:40Z 2 суток разобраны повторно за 24 ч (всего разборов 4); "
                "больше всех — /home/user/rpv/tk022/view/2026-01-01/root-2026-01-01: 4 р")
    IDLE_A = ("Вторая машина: ALERT-idle-deck: 2026-10-01T22:07:01Z очередь пуста, заданий нет, load1 0.31 — "
              "простой дольше 30 мин")
    IDLE_B = ("Вторая машина: ALERT-idle-deck: 2026-10-01T23:04:30Z очередь пуста, заданий нет, load1 0.76 — "
              "простой дольше 30 мин")

    def test_normalize_strips_iso_times_t_fragments_and_counters(self):
        self.assertEqual(W.normalize_signature(self.REWORK_A), W.normalize_signature(self.REWORK_B))
        self.assertEqual(W.normalize_signature(self.IDLE_A), W.normalize_signature(self.IDLE_B))
        n = W.normalize_signature("x 2026-10-01T23: y T23: z 18:26Z (всего разборов 12)")
        self.assertNotIn("23", n)
        self.assertNotIn("12", n)
        self.assertNotIn("2026", n)

    def test_normalize_keeps_substance(self):
        other = self.REWORK_A.replace("разобраны повторно", "НЕ ХВАТАЕТ МЕСТА НА ДИСКЕ")
        self.assertNotEqual(W.normalize_signature(self.REWORK_A), W.normalize_signature(other))
        self.assertNotEqual(W.normalize_signature(self.REWORK_A), W.normalize_signature(self.IDLE_A))

    def test_hourly_timestamp_drift_does_not_repost(self):
        """Прежняя нормализация оставляла «2026-10-02T00:<t>» → сигнатура менялась каждый час."""
        ws = {}
        W.notify_findings([W.Finding("deck-alert", "ALERT-rework", self.REWORK_A)], ws, self.now)
        for h in range(1, 8):
            msg = self.REWORK_A.replace("2026-10-01T23:12:59Z", f"2026-10-02T{(23 + h) % 24:02d}:12:59Z")
            posted = W.notify_findings([W.Finding("deck-alert", "ALERT-rework", msg)], ws,
                                       self.now + timedelta(hours=h))
            self.assertEqual(posted, [], f"через {h} ч")

    def test_alert_known_before_ssh_error_cycle_is_not_resignalled_after(self):
        """Цикл с ошибкой ssh не видит тревог Deck; маркеры известных тревог не «забываются» — первый же
        успешный цикл не сообщает их заново."""
        ws = {}
        alert = W.Finding("deck-alert", "ALERT-rework", self.REWORK_A)
        W.notify_findings([alert], ws, self.now)
        ssh_err = W.Finding("deck-ssh-error-transient", "alerts", "не удалось проверить тревоги (сбой 1/3)")
        W.notify_findings([ssh_err], ws, self.now + timedelta(minutes=2))
        self.assertIn("deck-alert:ALERT-rework", ws["notified"])
        posted = W.notify_findings([W.Finding("deck-alert", "ALERT-rework", self.REWORK_B)], ws,
                                   self.now + timedelta(minutes=4))
        self.assertEqual(posted, [])

    def test_alert_forgotten_when_check_succeeded_without_it(self):
        ws = {}
        alert = W.Finding("deck-alert", "ALERT-rework", self.REWORK_A)
        W.notify_findings([alert], ws, self.now)
        W.notify_findings([], ws, self.now + timedelta(minutes=2))  # проверка прошла, тревоги нет — снята
        self.assertEqual(len(W.notify_findings([alert], ws, self.now + timedelta(minutes=4))), 1)

    def test_non_deck_marker_is_forgotten_even_in_ssh_error_cycle(self):
        ws = {}
        W.notify_findings([W.Finding("blocked", "TK-1", "TK-1: status=blocked")], ws, self.now)
        ssh_err = W.Finding("deck-ssh-error-transient", "alerts", "сбой")
        W.notify_findings([ssh_err], ws, self.now + timedelta(minutes=2))
        self.assertNotIn("blocked:TK-1", ws["notified"])

    def test_end_to_end_alert_rewritten_every_cycle_is_signalled_once(self):
        calls = {"n": 0}

        def fake_ssh(cmd, timeout=10.0):
            if "ALERT" in cmd:
                calls["n"] += 1
                return True, f"ALERT-rework: 2026-10-01T2{calls['n'] % 10}:12:59Z 1 суток (всего разборов {calls['n']})"
            return True, "NOHOLD" if "HOLD" in cmd else "0 0"

        for i in range(6):
            W.run_once(self.now + timedelta(minutes=2 * i), ssh_run=fake_ssh)
        inbox = D.CEO_INBOX.read_text(encoding="utf-8") if D.CEO_INBOX.exists() else ""
        self.assertEqual(inbox.count("watch-deck-alert"), 1)

class DedupTests(WatchSandbox):
    def test_first_occurrence_posts(self):
        ws = {}
        posted = W.notify_findings([W.Finding("blocked", "TK-1", "TK-1: status=blocked")], ws, self.now)
        self.assertEqual(len(posted), 1)
        self.assertIn("TK-1", D.CEO_INBOX.read_text(encoding="utf-8"))

    def test_repeat_within_window_is_suppressed(self):
        ws = {}
        f = [W.Finding("blocked", "TK-1", "TK-1: status=blocked")]
        W.notify_findings(f, ws, self.now)
        posted2 = W.notify_findings(f, ws, self.now + timedelta(minutes=10))
        self.assertEqual(posted2, [])

    def test_repeat_after_window_reposts(self):
        ws = {}
        f = [W.Finding("dispatcher-down", "last_tick", "последний тик диспетчера 9 мин назад")]
        W.notify_findings(f, ws, self.now)
        later = self.now + timedelta(hours=W.WATCH_DEDUP_REPEAT_HOURS, minutes=1)
        posted2 = W.notify_findings(f, ws, later)
        self.assertEqual(len(posted2), 1)

    def test_blocked_and_needs_owner_repeat_only_daily(self):
        """v2: диспетчер уже сообщил о blocked/needs_owner один раз; сторож страхует раз в сутки, не раз в 2 ч."""
        for kind in ("blocked", "needs_owner"):
            ws = {}
            f = [W.Finding(kind, "TK-27", f"TK-27: status={kind}")]
            W.notify_findings(f, ws, self.now)
            self.assertEqual(W.notify_findings(f, ws, self.now + timedelta(hours=W.WATCH_DEDUP_REPEAT_HOURS + 1)), [])
            later = self.now + timedelta(hours=W.WATCH_LONG_REPEAT_HOURS, minutes=1)
            self.assertEqual(len(W.notify_findings(f, ws, later)), 1)

    def test_resolved_finding_forgotten_so_recurrence_posts_immediately(self):
        ws = {}
        f = [W.Finding("blocked", "TK-1", "TK-1: status=blocked")]
        W.notify_findings(f, ws, self.now)
        W.notify_findings([], ws, self.now + timedelta(minutes=5))  # снято
        posted3 = W.notify_findings(f, ws, self.now + timedelta(minutes=6))  # снова — сразу, не ждём окна
        self.assertEqual(len(posted3), 1)


class TriageWaitsTests(WatchSandbox):
    def _waiting(self, spec, hours=5):
        p = T.create_ticket(self.tickets_dir, owner="engineer", title="Ждёт", status="todo",
                            now=self.now - timedelta(hours=hours))
        T.write_header_updates(p, {"status": "waiting", "wait_for": spec}, now=self.now - timedelta(hours=hours))
        T.append_log(p, "engineer", "жду", now=self.now - timedelta(hours=hours))
        return p

    def test_alive_target_is_not_orphan(self):
        self._waiting("host:calc:/var/rpv/progress/job-a.json")
        ws = {}
        alive = W.triage_waits(ws, self.now, probe=lambda *a: "producer")
        self.assertEqual(len(alive), 1)
        self.assertEqual(W.check_orphan_tickets(self.now, alive), [])
        self.assertEqual(len(W.check_orphan_tickets(self.now)), 1)

    def test_dead_target_two_strikes_wakes_owner_then_blocks(self):
        p = self._waiting("host:calc:/var/rpv/progress/rpv-b12flag.json")
        ws = {}
        W.triage_waits(ws, self.now, probe=lambda *a: "dead")
        self.assertEqual(T.read_ticket(p).status, "waiting")
        W.triage_waits(ws, self.now, probe=lambda *a: "dead")
        t = T.read_ticket(p)
        self.assertEqual((t.status, t.header.get("wait_for", "")), ("in_progress", ""))
        self.assertEqual(t.log[-1].author, "watch")
        T.write_header_updates(p, {"status": "waiting", "wait_for": "host:calc:/var/rpv/progress/rpv-b12flag.json"})
        W.triage_waits(ws, self.now, probe=lambda *a: "dead")
        W.triage_waits(ws, self.now, probe=lambda *a: "dead")
        self.assertEqual(T.read_ticket(p).status, "blocked")

    def test_unknown_probe_does_not_count(self):
        p = self._waiting("host:calc:/var/rpv/progress/job-a.json")
        ws = {}
        for _ in range(4):
            W.triage_waits(ws, self.now, probe=lambda *a: "unknown")
        self.assertEqual(T.read_ticket(p).status, "waiting")

    def test_ssh_silent_n_checks_wakes_owner_not_ceo(self):
        p = self._waiting("host:calc:/var/rpv/progress/job-a.json")
        ws = {}
        for _ in range(W.SSH_FAIL_STRIKES - 1):
            alive = W.triage_waits(ws, self.now, probe=lambda *a: "ssh-error")
            self.assertEqual(len(alive), 1)
        self.assertEqual(T.read_ticket(p).status, "waiting")
        W.triage_waits(ws, self.now, probe=lambda *a: "ssh-error")
        t = T.read_ticket(p)
        self.assertEqual((t.status, t.header.get("wait_for", "")), ("in_progress", ""))
        self.assertEqual(t.log[-1].author, "watch")
        self.assertIn("ssh", t.log[-1].text)

    def test_ssh_answer_resets_the_count(self):
        p = self._waiting("host:calc:/var/rpv/progress/job-a.json")
        ws = {}
        for _ in range(W.SSH_FAIL_STRIKES - 1):
            W.triage_waits(ws, self.now, probe=lambda *a: "ssh-error")
        W.triage_waits(ws, self.now, probe=lambda *a: "producer")
        for _ in range(W.SSH_FAIL_STRIKES - 1):
            W.triage_waits(ws, self.now, probe=lambda *a: "ssh-error")
        self.assertEqual(T.read_ticket(p).status, "waiting")

    def test_unknown_probe_is_not_orphan(self):
        self._waiting("host:calc:unit:rpv-job")
        alive = W.triage_waits({}, self.now, probe=lambda *a: "unknown")
        self.assertEqual(W.check_orphan_tickets(self.now, alive), [])

    def test_file_form_is_not_orphan(self):
        self._waiting("file:/tmp/rpv-flag")
        alive = W.triage_waits({}, self.now, probe=lambda *a: "dead")
        self.assertEqual(W.check_orphan_tickets(self.now, alive), [])

    def test_missing_ticket_target_is_dead(self):
        p = self._waiting("ticket:TK-999")
        ws = {}
        W.triage_waits(ws, self.now)
        W.triage_waits(ws, self.now)
        self.assertEqual(T.read_ticket(p).status, "in_progress")

    # --- сторож жизни (TK-092): форма job:<алиас>:<id> через адаптер проекта ---
    SPEC = "job:calc:0108041500123"

    def _job(self, state, tail=""):
        return lambda alias, jid: (state, tail)

    def test_job_form_parses(self):
        self.assertEqual(T.parse_wait_for(self.SPEC), ("job", "calc", "0108041500123"))
        self.assertIsNone(T.parse_wait_for("job:nohost:1"))
        self.assertIsNone(T.parse_wait_for("job:calc:"))

    def test_job_failed_wakes_owner_at_once_with_tail_and_reason(self):
        p = self._waiting(self.SPEC)
        ws = {}
        tail = "Traceback\nOSError: нет места"
        with mock.patch.object(W.LW, "reason", return_value="кончилось место"):
            alive = W.triage_waits(ws, self.now, job_probe=self._job("failed", tail))
        t = T.read_ticket(p)
        self.assertEqual((t.status, t.header.get("wait_for", "")), ("in_progress", ""))
        self.assertEqual(alive, set())
        self.assertEqual(t.log[-1].author, "watch")
        for part in ("упало", "OSError: нет места", "кончилось место"):
            self.assertIn(part, t.log[-1].text)

    def test_job_failed_again_after_wake_blocks_for_ceo(self):
        p = self._waiting(self.SPEC)
        ws = {}
        with mock.patch.object(W.LW, "reason", return_value=""):
            W.triage_waits(ws, self.now, job_probe=self._job("failed"))
            T.write_header_updates(p, {"status": "waiting", "wait_for": "job:calc:other2"})
            W.triage_waits(ws, self.now, job_probe=self._job("failed"))
        self.assertEqual(T.read_ticket(p).status, "blocked")

    def test_job_vanished_wakes_owner_after_strikes(self):
        p = self._waiting(self.SPEC)
        ws = {}
        for _ in range(W.DEAD_WAIT_STRIKES - 1):
            self.assertEqual(len(W.triage_waits(ws, self.now, job_probe=self._job("missing"))), 1)
        self.assertEqual(T.read_ticket(p).status, "waiting")
        W.triage_waits(ws, self.now, job_probe=self._job("missing"))
        t = T.read_ticket(p)
        self.assertEqual((t.status, t.header.get("wait_for", "")), ("in_progress", ""))
        self.assertIn("не найдено", t.log[-1].text)

    def test_job_running_waits_and_done_is_published_for_dispatcher(self):
        import lifewatch
        p = self._waiting(self.SPEC)
        ws = {}
        self.assertEqual(len(W.triage_waits(ws, self.now, job_probe=self._job("running"))), 1)
        self.assertFalse(lifewatch.job_done(D.DISPATCHER_DIR, "calc", "0108041500123"))
        W.triage_waits(ws, self.now, job_probe=self._job("done"))
        self.assertTrue(lifewatch.job_done(D.DISPATCHER_DIR, "calc", "0108041500123"))
        self.assertTrue(D.check_wait_for(self.SPEC))
        self.assertEqual(T.read_ticket(p).status, "waiting")

    def test_job_adapter_silent_wakes_owner_after_ssh_strikes(self):
        p = self._waiting(self.SPEC)
        ws = {}
        for _ in range(W.SSH_FAIL_STRIKES):
            W.triage_waits(ws, self.now, job_probe=self._job("ssh-error"))
        self.assertEqual(T.read_ticket(p).status, "in_progress")


class TriageStallsTests(WatchSandbox):
    def _ticket(self):
        p = T.create_ticket(self.tickets_dir, owner="engineer", title="Застой", status="todo",
                            now=self.now - timedelta(hours=5))
        T.write_header_updates(p, {"status": "in_progress"}, now=self.now - timedelta(hours=5))
        return p, T.read_ticket(p).id

    def _runs(self, tid, rows):
        f = Path(self.tmp.name) / "runs.log"
        f.write_text("".join(
            f"{(self.now - timedelta(minutes=m)).isoformat(timespec='seconds')} {tid} engineer reason=next status={st}\n"
            for m, st in rows), encoding="utf-8")
        return f

    def test_two_timeouts_in_a_row_block_once(self):
        p, tid = self._ticket()
        runs = self._runs(tid, [(60, "ok"), (40, "timeout"), (20, "timeout")])
        ws = {}
        self.assertEqual(W.triage_stalls(ws, self.now, runs), [tid])
        t = T.read_ticket(p)
        self.assertEqual((t.status, t.log[-1].author), ("blocked", "watch"))
        T.write_header_updates(p, {"status": "in_progress"})
        self.assertEqual(W.triage_stalls(ws, self.now, runs), [])

    def test_timeout_then_ok_is_not_stall(self):
        p, tid = self._ticket()
        runs = self._runs(tid, [(40, "timeout"), (20, "ok")])
        self.assertEqual(W.triage_stalls({}, self.now, runs), [])
        self.assertEqual(T.read_ticket(p).status, "in_progress")

    def test_two_idle_ok_runs_block_but_productive_run_does_not(self):
        p, tid = self._ticket()
        runs = self._runs(tid, [(60, "ok"), (40, "ok"), (20, "ok")])
        T.append_log(p, "engineer", "сделал", now=self.now - timedelta(minutes=30))  # запуск в -20 не холостой
        self.assertEqual(W.triage_stalls({}, self.now, runs), [])
        p2, tid2 = self._ticket()
        runs2 = self._runs(tid2, [(60, "ok"), (40, "ok"), (20, "ok")])
        self.assertEqual(W.triage_stalls({}, self.now, runs2), [tid2])

    def test_waiting_ticket_ignored(self):
        p, tid = self._ticket()
        T.write_header_updates(p, {"status": "waiting", "wait_for": "file:/x"})
        runs = self._runs(tid, [(40, "timeout"), (20, "timeout")])
        self.assertEqual(W.triage_stalls({}, self.now, runs), [])


class RunOnceTests(WatchSandbox):
    def test_writes_heartbeat_even_with_no_findings(self):
        def fake_ssh(cmd, timeout=10.0):
            return True, "" if "ALERT" in cmd else "HOLD"
        W.run_once(self.now, ssh_run=fake_ssh)
        self.assertTrue(W.WATCH_HEARTBEAT_FILE.exists())

    def test_first_ever_cycle_no_last_tick_is_grace_not_finding(self):
        """CEO 27.09: «нет last_tick» сразу после старта — грация 2 интервала, не находка."""
        def fake_ssh(cmd, timeout=10.0):
            return True, "" if "ALERT" in cmd else "HOLD"
        posted = W.run_once(self.now, ssh_run=fake_ssh)  # state.json нет вовсе -> last_tick отсутствует
        self.assertFalse(any(f.kind == "dispatcher-down" for f in posted))

    def test_no_last_tick_after_grace_window_is_a_finding(self):
        def fake_ssh(cmd, timeout=10.0):
            return True, "" if "ALERT" in cmd else "HOLD"
        W.run_once(self.now, ssh_run=fake_ssh)  # первый цикл — задаёт started_at
        later = self.now + timedelta(minutes=D.POLL_INTERVAL / 60 * 3)  # заведомо за пределами 2×POLL_INTERVAL
        posted = W.run_once(later, ssh_run=fake_ssh)
        self.assertTrue(any(f.kind == "dispatcher-down" for f in posted))


class WatchInstanceLockTests(WatchSandbox):
    def test_second_watch_loop_is_refused_without_running_cycles(self):
        import subprocess
        orig = (W.WATCH_PID_FILE, W.run_once)
        holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        cycles = []
        W.WATCH_PID_FILE = Path(self.tmp.name) / "watch.pid"
        W.run_once = lambda *a, **kw: cycles.append(1) or []
        W.WATCH_PID_FILE.write_text(str(holder.pid), encoding="utf-8")
        orig_alive = D._pid_alive
        D._pid_alive = lambda pid, expect_name=None: orig_alive(pid, "")
        try:
            self.assertEqual(W.main([]), 1)
            self.assertEqual(cycles, [])
        finally:
            D._pid_alive = orig_alive
            W.WATCH_PID_FILE, W.run_once = orig
            holder.kill()
            holder.wait(timeout=10)


class ServerIdleTK070Tests(WatchSandbox):
    """TK-070 п.1: сервер простаивает, задания ждут замка > 20 мин → будим владельца тикета держателя."""

    PROBE_IDLE = ("load 0.8\n"
                  "100 1 5000 bash /data/benchrun.sh wave systemd-run tk065-gate2 x\n"
                  "200 1 1900 bash /data/benchrun.sh stand tk071-job\n"
                  "201 200 1900 flock -x 9\n")

    def _ticket(self):
        (self.tickets_dir / "TK-065.md").write_text(
            "---\nid: TK-065\ntitle: t\nowner: engineer\nstatus: waiting\nwait_for: \nupdated: 2026-09-27T11:00:00+04:00\n---\n\nd\n\n## Лог\n",
            encoding="utf-8")

    def test_analyze(self):
        info = W.analyze_server(self.PROBE_IDLE)
        self.assertEqual(info["load"], 0.8)
        self.assertEqual(info["wait_s"], 1900)
        self.assertIn("tk065-gate2", info["holder"])

    def test_wakes_owner_of_holder_ticket(self):
        self._ticket()
        ws = {}
        self.assertEqual(W.check_server_idle(ws, self.now, ssh_run=lambda c: self.PROBE_IDLE), [])
        tkt = T.read_ticket(self.tickets_dir / "TK-065.md")
        self.assertEqual(tkt.next_role, "engineer")
        self.assertIn("простаивает", tkt.log_raw)
        T.write_header_updates(self.tickets_dir / "TK-065.md", {"next": ""}, stamp_updated=False)
        W.check_server_idle(ws, self.now + timedelta(minutes=10), ssh_run=lambda c: self.PROBE_IDLE)
        self.assertEqual(T.read_ticket(self.tickets_dir / "TK-065.md").next_role, "")

    def test_busy_server_or_short_wait_is_quiet(self):
        self._ticket()
        busy = self.PROBE_IDLE.replace("load 0.8", "load 9.0")
        self.assertEqual(W.check_server_idle({}, self.now, ssh_run=lambda c: busy), [])
        short = self.PROBE_IDLE.replace("1900 flock", "100 flock")
        self.assertEqual(W.check_server_idle({}, self.now, ssh_run=lambda c: short), [])
        self.assertEqual(T.read_ticket(self.tickets_dir / "TK-065.md").next_role, "")

    def test_unknown_holder_escalates_to_ceo(self):
        probe = "load 0.5\n300 1 5000 bash /data/benchrun.sh wave foo\n301 1 1900 bash /data/benchrun.sh stand bar\n302 301 1900 flock -x 9\n"
        f = W.check_server_idle({}, self.now, ssh_run=lambda c: probe)
        self.assertEqual([x.kind for x in f], ["server-idle"])


class NoPlanWakeTests(WatchSandbox):
    def test_no_plan_wakes_ticket_owner(self):
        p = T.create_ticket(self.tickets_dir, owner="engineer", title="Без плана", status="in_progress", now=self.now)
        ws = {}
        f = [W.Finding("no-plan", T.read_ticket(p).id, "x: в работе > 30 мин без плана шагов на табло")]
        self.assertEqual(len(W.notify_findings(f, ws, self.now)), 1)
        t = T.read_ticket(p)
        self.assertEqual(t.next_role, "engineer")
        self.assertIn("plan.py set", t.log[-1].text)
        W.notify_findings(f, ws, self.now + timedelta(minutes=5))
        self.assertEqual(len(T.read_ticket(p).log), 1)


class StalePlanTests(WatchSandbox):
    def test_stale_plan_wakes_owner_once(self):
        import json
        root = Path(self.tmp.name) / "root"
        (root / ".claude" / "pulse" / "plans").mkdir(parents=True)
        orig_root = D.PROJECT_ROOT
        D.PROJECT_ROOT = root
        try:
            p = T.create_ticket(self.tickets_dir, owner="engineer", title="План", status="in_progress",
                                now=self.now - timedelta(hours=3))
            tid = T.read_ticket(p).id
            plan = {"id": tid, "steps": [{"state": "run"}], "updated": T.now_iso(self.now - timedelta(hours=2))}
            (root / ".claude" / "pulse" / "plans" / f"{tid}.json").write_text(json.dumps(plan), encoding="utf-8")
            self.assertEqual(W.check_stale_plan(self.now), [])  # новой записи роли нет
            T.append_log(p, "engineer", "сделал", now=self.now - timedelta(hours=1))
            f = W.check_stale_plan(self.now)
            self.assertEqual([x.kind for x in f], ["plan-stale"])
            ws = {}
            self.assertEqual(len(W.notify_findings(f, ws, self.now)), 1)
            t = T.read_ticket(p)
            self.assertEqual(t.next_role, "engineer")
            self.assertIn("plan.py step", t.log[-1].text)
            W.notify_findings(f, ws, self.now + timedelta(minutes=5))
            self.assertEqual(len(T.read_ticket(p).log), 2)
        finally:
            D.PROJECT_ROOT = orig_root


class StalePlanWaitingTests(WatchSandbox):
    def test_waiting_gets_reminder_without_wake(self):
        import json
        root = Path(self.tmp.name) / "root"
        (root / ".claude" / "pulse" / "plans").mkdir(parents=True)
        orig_root = D.PROJECT_ROOT
        D.PROJECT_ROOT = root
        try:
            p = T.create_ticket(self.tickets_dir, owner="engineer", title="Ждёт", status="waiting",
                                now=self.now - timedelta(hours=3))
            tid = T.read_ticket(p).id
            plan = {"id": tid, "steps": [{"state": "run"}], "updated": T.now_iso(self.now - timedelta(hours=2))}
            (root / ".claude" / "pulse" / "plans" / f"{tid}.json").write_text(json.dumps(plan), encoding="utf-8")
            T.append_log(p, "engineer", "ждём счёт", now=self.now - timedelta(hours=1))
            f = W.check_stale_plan(self.now)
            self.assertEqual([x.kind for x in f], ["plan-stale-waiting"])
            ws = {}
            self.assertEqual(len(W.notify_findings(f, ws, self.now)), 1)
            t = T.read_ticket(p)
            self.assertFalse(t.next_role)
            self.assertIn("plan.py step", t.log[-1].text)
            W.notify_findings(f, ws, self.now + timedelta(minutes=5))
            self.assertEqual(len(T.read_ticket(p).log), 2)
        finally:
            D.PROJECT_ROOT = orig_root


class StalePlanLagTests(WatchSandbox):
    def test_log_right_after_plan_update_is_not_stale(self):
        import json
        root = Path(self.tmp.name) / "root"
        (root / ".claude" / "pulse" / "plans").mkdir(parents=True)
        orig_root = D.PROJECT_ROOT
        D.PROJECT_ROOT = root
        try:
            p = T.create_ticket(self.tickets_dir, owner="engineer", title="Лаг", status="in_progress",
                                now=self.now - timedelta(hours=3))
            tid = T.read_ticket(p).id
            upd = self.now - timedelta(hours=2)
            plan = {"id": tid, "steps": [{"state": "run"}], "updated": T.now_iso(upd)}
            (root / ".claude" / "pulse" / "plans" / f"{tid}.json").write_text(json.dumps(plan), encoding="utf-8")
            T.append_log(p, "engineer", "итог", now=upd + timedelta(minutes=2))
            self.assertEqual(W.check_stale_plan(self.now), [])
        finally:
            D.PROJECT_ROOT = orig_root


class NoProgressViewTests(WatchSandbox):
    def _status(self, root, procs, built_ts):
        import json
        d = root / ".claude" / "pulse"
        d.mkdir(parents=True, exist_ok=True)
        (d / "status.json").write_text(json.dumps({"view2": {"built_ts": built_ts, "processes": procs}}), encoding="utf-8")

    def test_in_progress_without_plan_is_found_only_with_fresh_collector(self):
        root = Path(self.tmp.name) / "root"
        orig_root = D.PROJECT_ROOT
        D.PROJECT_ROOT = root
        try:
            p = T.create_ticket(self.tickets_dir, owner="engineer", title="Без плана", status="in_progress",
                                now=self.now - timedelta(hours=1))
            tid = T.read_ticket(p).id
            self.assertEqual(W.check_no_progress_view(self.now), [])  # status.json нет — молчим
            self._status(root, [{"id": tid, "plan": None}], self.now.timestamp() - 600)
            self.assertEqual(W.check_no_progress_view(self.now), [])  # сборщик стоит — другая находка
            self._status(root, [{"id": tid, "plan": None}], self.now.timestamp() - 10)
            self.assertEqual([f.kind for f in W.check_no_progress_view(self.now)], ["no-plan"])
            self._status(root, [{"id": tid, "plan": {"steps": []}}], self.now.timestamp() - 10)
            self.assertEqual(W.check_no_progress_view(self.now), [])
        finally:
            D.PROJECT_ROOT = orig_root

    def test_plan_findings_never_reach_ceo_inbox(self):
        # no-plan / plan-stale / plan-stale-waiting адресуются владельцу тикета; CEO по ним ничего делать не может (TK-079 п.3)
        p = T.create_ticket(self.tickets_dir, owner="engineer", title="План", status="in_progress", now=self.now)
        tid = T.read_ticket(p).id
        for kind in ("no-plan", "plan-stale", "plan-stale-waiting"):
            self.assertEqual(len(W.notify_findings([W.Finding(kind, tid, "x")], {}, self.now)), 1)
        self.assertFalse(D.CEO_INBOX.exists() and "watch-" in D.CEO_INBOX.read_text(encoding="utf-8"))
        self.assertEqual(T.read_ticket(p).next_role, "engineer")  # владелец разбужен

    def test_watch_plan_reminder_keeps_ceo_handoff_alive(self):
        # напоминание сторожа не считается ответом CEO: метка передачи жива, «ждёт-ceo» придёт (ревью #27)
        p = T.create_ticket(self.tickets_dir, owner="engineer", title="Передача", status="waiting", now=self.now)
        tid = T.read_ticket(p).id
        W._remind_plan_waiting(tid)
        self.assertTrue(T.author_is(T.read_ticket(p).log[-1].author, "watch"))
        state = {"ceo_handoffs": {tid: T.now_iso(self.now - timedelta(minutes=1))}}
        self.assertTrue(D._ceo_handoff_pending(T.read_ticket(p), state, self.now + timedelta(hours=1)))
        self.assertIn(tid, state["ceo_handoffs"])
        self.assertIn("[ждёт-ceo]", D.CEO_INBOX.read_text(encoding="utf-8"))

    def test_watch_entries_do_not_reset_orphan_timer(self):
        # запись сторожа (напоминание, ssh-тревога) не сбрасывает таймер сироты (ревью #27)
        old = self.now - timedelta(hours=5)
        p = T.create_ticket(self.tickets_dir, owner="engineer", title="Сирота", status="todo", now=old)
        T.write_header_updates(p, {"status": "waiting"}, now=old)
        T.append_log(p, "engineer", "жду", now=old)
        T.append_log(p, "watch", "напоминание", now=self.now - timedelta(minutes=5))
        self.assertEqual([f.kind for f in W.check_orphan_tickets(self.now)], ["orphan-ticket"])

    def test_wake_text_points_to_plugin_plan_script(self):
        p = T.create_ticket(self.tickets_dir, owner="engineer", title="Без плана", status="in_progress", now=self.now)
        W.notify_findings([W.Finding("no-plan", T.read_ticket(p).id, "x")], {}, self.now)
        self.assertIn(W._PLAN_PY, T.read_ticket(p).log[-1].text)
        self.assertTrue(Path(W._PLAN_PY).exists())


if __name__ == "__main__":
    unittest.main()
