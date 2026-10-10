"""Тесты сторожа CEO без модели (судья TK-002 п.2). По фикстуре на каждое условие (п.2д). stdlib
`unittest`, без сети (проверки сервера — `probes=False`)."""
from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
import types
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
            "CEO_WAKE_LOG": D.CEO_WAKE_LOG, "DISPATCHER_DIR": D.DISPATCHER_DIR,
        }
        D.TICKETS_DIR = self.tickets_dir
        D.STATE_FILE = base / "state.json"
        D.CEO_INBOX = base / "ceo-inbox.md"
        D.CEO_WAKE_LOG = base / "ceo-wake.log"
        D.DISPATCHER_DIR = base  # job-state.json / job-owners.json сторожа — в песочницу
        W.WATCH_STATE_FILE = base / "watch-state.json"
        W.WATCH_HEARTBEAT_FILE = base / "watch-heartbeat.json"
        self.now = dt("2026-09-27T12:00:00+04:00")
        env_patch = mock.patch.dict(os.environ)  # адаптеры проекта (боевой ssh) тестам не нужны; после теста среда как была
        env_patch.start()
        self.addCleanup(env_patch.stop)
        for name in ("RPV_JOB_OWNERS_CMD", "RPV_JOB_STATE_CMD", "RPV_STRAY_CMD"):
            os.environ.pop(name, None)

    def tearDown(self):
        for k, v in self._orig.items():
            setattr(D, k, v)
        self.tmp.cleanup()


class NoMoneyWatchTests(WatchSandbox):
    """В-173 (на тикет) и В-149 (в час/в сутки): лимитов денег нет — сторож трат не проверяет и не сигналит."""

    def test_money_checks_are_gone(self):
        self.assertFalse(hasattr(W, "check_budgets"))
        self.assertNotIn("budget-watch", W.WATCH_LONG_REPEAT_KINDS)

    def test_huge_spend_gives_no_finding(self):
        p = T.create_ticket(self.tickets_dir, owner="engineer", title="Дорогая", status="todo")
        state = {"last_tick": T.now_iso(self.now - timedelta(seconds=10)),
                 "daily_cost": {D._today(self.now): 1e6}, "cost_history": [[T.now_iso(self.now), 1e6]],
                 "ticket_cost": {p.stem: 1e6}, "ticket_budget": {p.stem: 5.0}}  # ticket_budget — наследие state.json
        self.assertEqual(W.collect_findings(self.now), [])


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
        t = T.read_ticket(p)
        self.assertEqual((t.status, t.header.get("next")), ("in_progress", "judge"))  # повтор — Судье (В-206), не blocked → CEO

    def test_unknown_probe_does_not_count(self):
        p = self._waiting("host:calc:/var/rpv/progress/job-a.json")
        ws = {}
        for _ in range(4):
            W.triage_waits(ws, self.now, probe=lambda *a: "unknown")
        self.assertEqual(T.read_ticket(p).status, "waiting")

    def test_waiting_without_wait_for_and_next_wakes_owner_after_strikes(self):
        """Случай (4) CEO 06:00: ci_watch снял wait_for, роль не запущена — тикет не висит до сироты через 2 ч."""
        p = self._waiting("")
        ws = {}
        for _ in range(W.EMPTY_WAIT_STRIKES - 1):
            W.triage_waits(ws, self.now)
        self.assertEqual(T.read_ticket(p).status, "waiting")
        W.triage_waits(ws, self.now)
        t = T.read_ticket(p)
        self.assertEqual((t.status, t.header.get("wait_for", "")), ("in_progress", ""))
        self.assertEqual(t.log[-1].author, "watch")
        self.assertIn("без wait_for и без next", t.log[-1].text)

    def test_waiting_without_wait_for_with_live_role_run_is_quiet(self):
        """Приёмка: ci_watch снял wait_for, диспетчер запустил Судью (active_runs) — 20 мин тихо, счётчик не растёт."""
        p = self._waiting("")
        tid = T.read_ticket(p).id
        D.save_state({"active_runs": {tid: {"role": "judge", "pid": 1}}})
        ws = {}
        for _ in range(W.EMPTY_WAIT_STRIKES * 4):
            W.triage_waits(ws, self.now)
        self.assertEqual(T.read_ticket(p).status, "waiting")
        self.assertEqual(ws.get("empty_wait", {}), {})

    def test_job_wake_then_empty_wait_is_not_blocked(self):
        """Пробуждение по заданию раньше + пустое ожидание теперь = первое пробуждение по этой причине, не blocked."""
        p = self._waiting("")
        tid = T.read_ticket(p).id
        ws = {"job_wakes": {tid: "job:calc:1"}}
        for _ in range(W.EMPTY_WAIT_STRIKES):
            W.triage_waits(ws, self.now)
        self.assertEqual(T.read_ticket(p).status, "in_progress")

    def test_host_path_met_but_not_woken_wakes_owner_after_grace(self):
        """Случай (6): файл на машине есть, а диспетчер не разбудил — через допуск владелец будится сам."""
        p = self._waiting("host:calc:/data/sched/validity/1008032552475.json")
        ws = {}
        W.triage_waits(ws, self.now, probe=lambda *a: "met")
        self.assertEqual(T.read_ticket(p).status, "waiting")
        W.triage_waits(ws, self.now + timedelta(minutes=W.MET_GRACE_MIN - 1), probe=lambda *a: "met")
        self.assertEqual(T.read_ticket(p).status, "waiting")
        W.triage_waits(ws, self.now + timedelta(minutes=W.MET_GRACE_MIN + 1), probe=lambda *a: "met")
        t = T.read_ticket(p)
        self.assertEqual((t.status, t.header.get("wait_for", "")), ("in_progress", ""))
        self.assertIn("диспетчер не разбудил", t.log[-1].text)

    def test_probe_progress_json_in_flight_is_producer_not_met(self):
        import subprocess as sp
        orig, orig_ssh = W.subprocess.run, W.D._ssh_cmd
        try:
            W.D._ssh_cmd = lambda alias, remote: ["ssh", alias, remote]  # хост из среды на раннере не задан
            W.subprocess.run = lambda *a, **k: sp.CompletedProcess(a, 0, stdout=b'exists\n{"done": 1, "total": 5}', stderr=b"")
            self.assertEqual(W.probe_wait_target("calc", "path", "/data/sched/job-a.json"), "producer")
            W.subprocess.run = lambda *a, **k: sp.CompletedProcess(a, 0, stdout=b'exists\n{"done": 5, "total": 5}', stderr=b"")
            self.assertEqual(W.probe_wait_target("calc", "path", "/data/sched/job-a.json"), "met")
            W.subprocess.run = lambda *a, **k: sp.CompletedProcess(a, 0, stdout=b'exists\n', stderr=b"")
            self.assertEqual(W.probe_wait_target("calc", "path", "/data/sched/job-a.mark"), "met")
        finally:
            W.subprocess.run, W.D._ssh_cmd = orig, orig_ssh

    def test_ci_run_form_parse_and_dispatcher_check(self):
        import ci_watch
        self.assertEqual(T.parse_wait_for("ci-run:o/r#123"), ("ci-run", "o/r", 123))
        self.assertIsNone(T.parse_wait_for("ci-run:bad"))
        self.assertEqual(ci_watch.run_state("o/r", 1, gh=lambda p: {"status": "in_progress"}), "running")
        self.assertTrue(ci_watch.run_done("o/r", 1, gh=lambda p: {"status": "completed", "conclusion": "failure"}))
        def gone(p):
            raise RuntimeError("gh: Not Found (HTTP 404)")
        def boom(p):
            raise RuntimeError("timeout")
        self.assertEqual(ci_watch.run_state("o/r", 1, gh=gone), "missing")
        self.assertEqual(ci_watch.run_state("o/r", 1, gh=boom), "error")

    def test_ci_run_missing_two_strikes_wakes_owner_running_is_alive(self):
        p = self._waiting("ci-run:o/r#99")
        ws = {}
        alive = W.triage_waits(ws, self.now, run_probe=lambda r, i: "running")
        self.assertEqual(len(alive), 1)
        W.triage_waits(ws, self.now, run_probe=lambda r, i: "error")
        self.assertEqual(T.read_ticket(p).status, "waiting")
        for _ in range(W.DEAD_WAIT_STRIKES):
            W.triage_waits(ws, self.now, run_probe=lambda r, i: "missing")
        t = T.read_ticket(p)
        self.assertEqual((t.status, t.header.get("wait_for", "")), ("in_progress", ""))
        self.assertIn("прогон CI", t.log[-1].text)

    def test_waiting_without_wait_for_but_with_next_is_left_alone(self):
        p = self._waiting("")
        T.write_header_updates(p, {"next": "engineer"}, now=self.now)
        ws = {}
        for _ in range(W.EMPTY_WAIT_STRIKES + 2):
            W.triage_waits(ws, self.now)
        self.assertEqual(T.read_ticket(p).status, "waiting")

    def test_stray_run_wakes_offender_ticket_owner_once(self):
        """Случай (5): замер мимо планировщика поверх чужого — сигнал владельцу тикета-нарушителя, повтор не чаще порога."""
        p = self._waiting("")
        tid = T.read_ticket(p).id
        ws = {}
        probe = lambda alias: [(tid, "systemd-run tk065-g2pgo поверх волны tk048")]
        self.assertEqual(W.check_strays(ws, self.now, probe=probe), [])
        t = T.read_ticket(p)
        self.assertEqual(t.header.get("next"), "engineer")
        self.assertIn("мимо планировщика", t.log[-1].text)
        n = len(t.log)
        W.check_strays(ws, self.now + timedelta(minutes=10), probe=probe)
        self.assertEqual(len(T.read_ticket(p).log), n)
        W.check_strays(ws, self.now + timedelta(hours=3), probe=probe)
        self.assertEqual(len(T.read_ticket(p).log), n + 1)

    def test_stray_run_without_ticket_goes_to_ceo_and_no_adapter_is_silent(self):
        f = W.check_strays({}, self.now, probe=lambda alias: [("", "голый замер")])
        self.assertEqual([x.kind for x in f], ["stray-run"])
        self.assertEqual(W.check_strays({}, self.now, probe=lambda alias: None), [])

    def test_ssh_silent_n_checks_wakes_owner_not_ceo(self):
        p = self._waiting("host:calc:/var/rpv/progress/job-a.json")
        ws = {}
        for _ in range(W.SSH_FAIL_STRIKES - 1):
            alive = W.triage_waits(ws, self.now, probe=lambda *a: "ssh-error", link_up=lambda: True)
            self.assertEqual(len(alive), 1)
        self.assertEqual(T.read_ticket(p).status, "waiting")
        W.triage_waits(ws, self.now, probe=lambda *a: "ssh-error", link_up=lambda: True)
        t = T.read_ticket(p)
        self.assertEqual((t.status, t.header.get("wait_for", "")), ("in_progress", ""))
        self.assertEqual(t.log[-1].author, "watch")
        self.assertIn("ssh", t.log[-1].text)

    def test_pc_link_down_never_drops_wait_for(self):
        """Обрыв связи ПК (Д-8): ssh молчит у всех, но это не машина — сколько бы проверок ни прошло, wait_for на месте."""
        p = self._waiting("host:calc:/var/rpv/progress/job-a.json")
        ws = {}
        for _ in range(W.SSH_FAIL_STRIKES * 3):
            alive = W.triage_waits(ws, self.now, probe=lambda *a: "ssh-error", link_up=lambda: False)
            self.assertEqual(len(alive), 1)
        t = T.read_ticket(p)
        self.assertEqual((t.status, t.header.get("wait_for")), ("waiting", "host:calc:/var/rpv/progress/job-a.json"))
        self.assertEqual(ws.get("ssh_fail_wait", {}), {})
        # связь вернулась, а ssh всё ещё молчит — счёт идёт с нуля и снимает ожидание только через N проверок
        for _ in range(W.SSH_FAIL_STRIKES - 1):
            W.triage_waits(ws, self.now, probe=lambda *a: "ssh-error", link_up=lambda: True)
        self.assertEqual(T.read_ticket(p).status, "waiting")
        W.triage_waits(ws, self.now, probe=lambda *a: "ssh-error", link_up=lambda: True)
        self.assertEqual(T.read_ticket(p).status, "in_progress")

    def test_pc_link_probe_uses_tcp_connect(self):
        with mock.patch.object(W.socket, "create_connection", side_effect=OSError("down")):
            self.assertFalse(W.pc_link_up())
        with mock.patch.object(W.socket, "create_connection") as cc:
            self.assertTrue(W.pc_link_up())
            cc.assert_called_once()

    def test_ssh_answer_resets_the_count(self):
        p = self._waiting("host:calc:/var/rpv/progress/job-a.json")
        ws = {}
        for _ in range(W.SSH_FAIL_STRIKES - 1):
            W.triage_waits(ws, self.now, probe=lambda *a: "ssh-error", link_up=lambda: True)
        W.triage_waits(ws, self.now, probe=lambda *a: "producer")
        for _ in range(W.SSH_FAIL_STRIKES - 1):
            W.triage_waits(ws, self.now, probe=lambda *a: "ssh-error", link_up=lambda: True)
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
        t = T.read_ticket(p)
        self.assertEqual((t.status, t.header.get("next")), ("in_progress", "judge"))  # повтор — Судье (В-206), не blocked → CEO

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

    def test_job_reason_is_one_line_from_haiku_and_empty_without_it(self):
        import lifewatch
        fake = types.SimpleNamespace(diagnose=lambda jid, tail: "кончилось место\nвторая строка")
        with mock.patch.dict(sys.modules, {"haiku_aux": fake}):
            self.assertEqual(lifewatch.reason("j1", "OSError"), "кончилось место")
        with mock.patch.dict(sys.modules, {"haiku_aux": None}):  # импорт невозможен
            self.assertEqual(lifewatch.reason("j1", "OSError"), "")
        self.assertEqual(lifewatch.reason("j1", "  "), "")

    # --- общее сопоставление «задание → тикет» (сторож и табло) ---
    def _owned(self, tid, state, jid="j1"):
        return lambda alias: [{"id": jid, "ticket": tid, "unit": "tk0s-x-" + jid, "state": state}]

    def test_owned_running_job_is_a_producer_not_a_dead_target(self):
        p = self._waiting("host:calc:/data/x/other-name.done")
        tid = T.read_ticket(p).id
        ws = {}
        for _ in range(W.DEAD_WAIT_STRIKES + 1):
            alive = W.triage_waits(ws, self.now, probe=lambda *a: "dead", owners_probe=self._owned(tid, "running"))
        self.assertEqual(alive, {tid})
        self.assertEqual(T.read_ticket(p).status, "waiting")

    def test_owned_failed_job_wakes_owner_once_and_owners_file_is_published(self):
        import json
        p = self._waiting("host:calc:/data/x/other-name.done")
        tid = T.read_ticket(p).id
        ws = {}
        W.triage_waits(ws, self.now, probe=lambda *a: "unknown", owners_probe=self._owned(tid, "failed"))
        t = T.read_ticket(p)
        self.assertEqual((t.status, t.header.get("wait_for", "")), ("in_progress", ""))
        self.assertIn("упало", t.log[-1].text)
        T.write_header_updates(p, {"status": "waiting", "wait_for": "host:calc:/data/x/other-name.done"})
        W.triage_waits(ws, self.now, probe=lambda *a: "unknown", owners_probe=self._owned(tid, "failed"))
        self.assertEqual(T.read_ticket(p).status, "waiting")  # то же упавшее задание второй раз не будит
        data = json.loads((D.DISPATCHER_DIR / "job-owners.json").read_text(encoding="utf-8"))
        self.assertEqual(data["calc"][0]["ticket"], tid)

    def test_old_failed_job_after_job_form_wake_does_not_block_for_ceo(self):
        """Судья 08.10: ждали job:A → A упало → владелец разбужен; поставил host:-ожидание другого задания; A (упало < 1 ч) не будит снова."""
        p = self._waiting("job:calc:A")
        tid = T.read_ticket(p).id
        ws = {}
        owners = lambda alias: [{"id": "A", "ticket": tid, "unit": "tk0s-x-A", "state": "failed"}]
        with mock.patch.object(W.LW, "reason", return_value=""):
            W.triage_waits(ws, self.now, job_probe=self._job("failed"), owners_probe=owners)
        self.assertEqual(T.read_ticket(p).status, "in_progress")
        T.write_header_updates(p, {"status": "waiting", "wait_for": "host:calc:/data/x/b.done"})
        W.triage_waits(ws, self.now, probe=lambda *a: "unknown", owners_probe=owners)
        self.assertEqual(T.read_ticket(p).status, "waiting")
        owners2 = lambda alias: [{"id": "A", "ticket": tid, "unit": "u", "state": "failed"},
                                 {"id": "B", "ticket": tid, "unit": "u", "state": "failed"}]
        W.triage_waits(ws, self.now, probe=lambda *a: "unknown", owners_probe=owners2)  # новое упавшее B — повтор после пробуждения: решение за CEO
        t = T.read_ticket(p)
        self.assertEqual((t.status, t.header.get("next")), ("in_progress", "judge"))  # повтор — Судье (В-206), не blocked → CEO

    # --- TK-117 П-1: ждёт, а работающих заданий нет ---
    def test_idle_wait_wakes_owner_after_grace_with_reason(self):
        """TK-114/115: цель не лежит, заданий тикета running/queued нет — через IDLE_WAIT_MIN владелец будится с причиной."""
        p = self._waiting("host:calc:/data/x/tier1.done")  # TK-114: файл не ляжет, задания tier1 кончились
        tid = T.read_ticket(p).id
        ws, owners = {}, self._jobs(tid, ("a", "tk0s-tk114-w", "done"))
        W.triage_waits(ws, self.now, probe=lambda *a: "producer", owners_probe=owners)
        self.assertEqual(T.read_ticket(p).status, "waiting")
        later = self.now + timedelta(minutes=W.IDLE_WAIT_MIN, seconds=30)
        W.triage_waits(ws, later, probe=lambda *a: "producer", owners_probe=lambda al: [{"id": "a", "ticket": tid, "unit": "tk0s-tk114-w", "state": "done"}])
        t = T.read_ticket(p)
        self.assertEqual((t.status, t.header.get("wait_for", "")), ("in_progress", ""))
        self.assertIn("работающих заданий тикета на машине 0", t.log[-1].text)
        self.assertIn("tk0s-tk114-w done", t.log[-1].text)

    def test_idle_wait_silent_with_running_job_or_without_adapter(self):
        p = self._waiting("host:calc:/data/x/tier1.done")
        tid = T.read_ticket(p).id
        later = self.now + timedelta(minutes=60)
        ws = {}
        for n in (self.now, later):
            W.triage_waits(ws, n, probe=lambda *a: "producer", owners_probe=self._jobs(tid, ("a", "u", "running")))
        self.assertEqual(T.read_ticket(p).status, "waiting")
        ws = {}
        for n in (self.now, later):  # адаптер заданий молчит (owners пуст) — не проверяем, ложных нет
            W.triage_waits(ws, n, probe=lambda *a: "producer", owners_probe=lambda al: None)
        self.assertEqual(T.read_ticket(p).status, "waiting")

    def test_idle_wait_timer_resets_when_job_appears(self):
        p = self._waiting("host:calc:/data/x/tier1.done")
        tid = T.read_ticket(p).id
        ws = {}
        W.triage_waits(ws, self.now, probe=lambda *a: "producer", owners_probe=self._jobs(tid))
        W.triage_waits(ws, self.now + timedelta(minutes=10), probe=lambda *a: "producer", owners_probe=self._jobs(tid, ("a", "u", "queued")))
        W.triage_waits(ws, self.now + timedelta(minutes=20), probe=lambda *a: "producer", owners_probe=self._jobs(tid))
        self.assertEqual(T.read_ticket(p).status, "waiting")  # таймер пошёл заново с 20-й минуты

    # --- TK-117: «поймала бы» на копиях TK-114 / TK-115 ---
    def test_replay_tk114_bad_form_wakes_within_three_cycles(self):
        """TK-114: wait_for `host:calc:file:/…` (файл лёг 05:58, никто не проверял) — тревога владельцу за EMPTY_WAIT_STRIKES циклов."""
        p = self._waiting("host:calc:/data/tk114/tier1.done")
        p.write_text(p.read_text(encoding="utf-8").replace("host:calc:/data", "host:calc:file:/data"), encoding="utf-8")  # старая запись, до проверки формы
        ws = {}
        for i in range(W.EMPTY_WAIT_STRIKES - 1):
            W.triage_waits(ws, self.now + timedelta(minutes=2 * i), probe=lambda *a: "unknown", owners_probe=lambda al: None)
            self.assertEqual(T.read_ticket(p).status, "waiting")
        W.triage_waits(ws, self.now + timedelta(minutes=6), probe=lambda *a: "unknown", owners_probe=lambda al: None)
        t = T.read_ticket(p)
        self.assertEqual((t.status, t.header.get("wait_for", "")), ("in_progress", ""))
        self.assertIn("форма wait_for", t.log[-1].text)

    def test_replay_tk115_oom_wave_dead_jobs_wakes_owner(self):
        """TK-115: 11 заданий rc 143 (oom), ждущий юнит жив (producer), работающих 0 — владелец будится через IDLE_WAIT_MIN с последними rc."""
        p = self._waiting("host:calc:/data/tk115/out/wave.done")
        tid = T.read_ticket(p).id
        rows = [(f"j{i}", f"tk115-w{i}", "failed") for i in range(11)]
        ws = {}
        W.triage_waits(ws, self.now, probe=lambda *a: "producer", owners_probe=self._jobs(tid, *rows))
        t = T.read_ticket(p)  # падение задания будит уже с первого цикла (ждущий юнит жив — не помеха)
        self.assertEqual(t.status, "in_progress")
        self.assertIn("tk115-w0", t.log[-1].text)

    def test_failed_job_reason_reaches_owner_log(self):
        """Судья TK-117: причина падения (5-е поле адаптера заданий) — в записи пробуждения и у П-1."""
        p = self._waiting("host:calc:/data/x/tier1.done")
        tid = T.read_ticket(p).id
        rows = lambda al: [{"id": "a", "ticket": tid, "unit": "tk0s-tk115-w1-a", "state": "failed", "reason": "rc 143: oom-kill (cgroup)"}]
        W.triage_waits({}, self.now, probe=lambda *a: "producer", owners_probe=rows)
        self.assertIn("[rc 143: oom-kill (cgroup)]", T.read_ticket(p).log[-1].text)
        p2 = self._waiting("host:calc:/data/y/tier1.done")
        tid2 = T.read_ticket(p2).id
        done = lambda al: [{"id": "b", "ticket": tid2, "unit": "tk0s-tk116-w-b", "state": "done", "reason": ""}]
        ws = {}
        W.triage_waits(ws, self.now, probe=lambda *a: "producer", owners_probe=done)
        W.triage_waits(ws, self.now + timedelta(minutes=W.IDLE_WAIT_MIN, seconds=30), probe=lambda *a: "producer", owners_probe=done)
        self.assertNotIn("[", T.read_ticket(p2).log[-1].text.split("последние:")[1])  # причины нет — скобок нет

    def test_fetch_owners_parses_reason_field(self):
        class R:
            returncode = 0
            stdout = "a\tTK-1\tu1\tfailed\trc 143: oom-kill\nb\tTK-1\tu2\trunning\t\nc\tTK-1\tu3\tdone\n".encode()
        with mock.patch.object(W.LW.hide, "run", return_value=R()), mock.patch.dict(os.environ, {"RPV_JOB_OWNERS_CMD": "x"}):
            got = W.LW.fetch_owners("calc", lambda a, c: ["ssh"])
        self.assertEqual([j["reason"] for j in got], ["rc 143: oom-kill", "", ""])

    # --- TK-117: срок ожидания ---
    def test_parse_by_and_text(self):
        now = datetime(2026, 10, 9, 17, 0).astimezone()
        self.assertEqual(T.parse_by("18:30", now).strftime("%d %H:%M"), "09 18:30")
        self.assertEqual(T.parse_by("06:00", now).strftime("%d %H:%M"), "10 06:00")  # ближайшее будущее
        for bad in ("25:00", "завтра", "2020-01-01T00:00+04:00"):
            with self.assertRaises(ValueError):
                T.parse_by(bad, now)
        self.assertTrue(T.wait_needs_by("host:calc:/x/y.done") and T.wait_needs_by("file:x") and not T.wait_needs_by("at:2026-10-10T02:00+04:00"))

    def test_deadline_passed_wakes_owner_only_after_grace(self):
        p = self._waiting("host:calc:/data/x/tier1.done")
        by = self.now + timedelta(hours=1)
        T.write_header_updates(p, {"wait_by": by.isoformat(timespec="seconds")})
        ws = {}
        for n in (self.now, by + timedelta(minutes=W.BY_GRACE_MIN - 1)):
            W.triage_waits(ws, n, probe=lambda *a: "producer", owners_probe=lambda al: None)
        self.assertEqual(T.read_ticket(p).status, "waiting")
        W.triage_waits(ws, by + timedelta(minutes=W.BY_GRACE_MIN + 1), probe=lambda *a: "producer", owners_probe=lambda al: None)
        t = T.read_ticket(p)
        self.assertEqual((t.status, t.header.get("wait_for", ""), t.header.get("wait_by", "")), ("in_progress", "", ""))
        self.assertIn("срок ожидания", t.log[-1].text)

    def test_deadline_ignored_when_target_met(self):
        p = self._waiting("host:calc:/data/x/tier1.done")
        T.write_header_updates(p, {"wait_by": (self.now - timedelta(hours=3)).isoformat(timespec="seconds")})
        W.triage_waits({}, self.now, probe=lambda *a: "met", owners_probe=lambda al: None)
        self.assertEqual(T.read_ticket(p).status, "waiting")  # цель выполнена — будит диспетчер, не срок

    # --- TK-101: ложные блоки сторожа жизни ---
    def _jobs(self, tid, *rows):
        return lambda alias: [{"id": i, "ticket": tid, "unit": u, "state": st} for i, u, st in rows]

    def test_failed_job_with_resubmit_of_same_name_does_not_wake(self):
        """п.1: волна пересдана под тем же именем (старая упала/снята, новая идёт или done) — владельца не будим."""
        for new_state in ("running", "queued", "done"):
            p = self._waiting("host:calc:/data/x/other-name.done")
            tid = T.read_ticket(p).id
            W.triage_waits({}, self.now, probe=lambda *a: "unknown",
                           owners_probe=self._jobs(tid, ("a", "tk065-w1", "failed"), ("b", "tk065-w1", new_state)))
            self.assertEqual(T.read_ticket(p).status, "waiting", new_state)
            T.write_header_updates(p, {"status": "done"})

    def test_failed_job_next_to_live_job_of_ticket_keeps_wait_for(self):
        """п.4: пока у тикета есть живое задание, упавшее соседнее wait_for не снимает."""
        p = self._waiting("host:calc:/data/x/other-name.done")
        tid = T.read_ticket(p).id
        ws = {}
        for _ in range(3):
            W.triage_waits(ws, self.now, probe=lambda *a: "dead",
                           owners_probe=self._jobs(tid, ("a", "u-a", "failed"), ("c", "u-c", "running")))
        t = T.read_ticket(p)
        self.assertEqual((t.status, t.header.get("wait_for")), ("waiting", "host:calc:/data/x/other-name.done"))

    def test_failure_beside_live_job_is_deferred_not_forgotten(self):
        """Судья TK-101: сосед доделал, упавшее не пересдано — владелец будится."""
        p = self._waiting("host:calc:/data/x/other-name.done")
        tid = T.read_ticket(p).id
        ws = {}
        W.triage_waits(ws, self.now, probe=lambda *a: "unknown",
                       owners_probe=self._jobs(tid, ("a", "u-a", "failed"), ("c", "u-c", "running")))
        self.assertEqual(T.read_ticket(p).status, "waiting")
        W.triage_waits(ws, self.now, probe=lambda *a: "unknown",
                       owners_probe=self._jobs(tid, ("a", "u-a", "failed"), ("c", "u-c", "done")))
        self.assertEqual(T.read_ticket(p).status, "in_progress")

    def test_job_form_failed_with_live_sibling_stays_waiting(self):
        p = self._waiting("job:calc:A")
        tid = T.read_ticket(p).id
        alive = W.triage_waits({}, self.now, job_probe=self._job("failed"),
                               owners_probe=self._jobs(tid, ("A", "u-a", "failed"), ("B", "u-b", "queued")))
        self.assertEqual((alive, T.read_ticket(p).status), ({tid}, "waiting"))

    def test_resubmitted_wave_scenario_zero_blocks_and_lone_failure_still_wakes(self):
        """Сценарий TK-065: волна пересдана, старые сняты — за все проходы ни одного blocked/next judge; одиночное падение будит."""
        p = self._waiting("host:calc:/data/x/other-name.done")
        tid = T.read_ticket(p).id
        ws = {}
        wave = [("o1", "w-1", "failed"), ("o2", "w-2", "failed"), ("n1", "w-1", "running"), ("n2", "w-2", "done")]
        for _ in range(6):
            W.triage_waits(ws, self.now, probe=lambda *a: "unknown", owners_probe=self._jobs(tid, *wave))
        t = T.read_ticket(p)
        self.assertEqual((t.status, t.header.get("next", "")), ("waiting", ""))
        W.triage_waits(ws, self.now, probe=lambda *a: "unknown", owners_probe=self._jobs(tid, ("x", "w-9", "failed")))
        self.assertEqual(T.read_ticket(p).status, "in_progress")

    def test_ceo_not_woken_by_watch_repeat(self):
        """п.3: повтор падения — next: judge, статус не blocked (на blocked диспетчер будит CEO)."""
        p = self._waiting("job:calc:A")
        tid = T.read_ticket(p).id
        ws = {}
        with mock.patch.object(W.LW, "reason", return_value=""):
            W.triage_waits(ws, self.now, job_probe=self._job("failed"))
            T.write_header_updates(p, {"status": "waiting", "wait_for": "job:calc:A"})
            W.triage_waits(ws, self.now, job_probe=self._job("failed"))
        t = T.read_ticket(p)
        self.assertEqual((t.status, t.header.get("next")), ("in_progress", "judge"))
        self.assertNotIn("CEO", t.log[-1].text.replace("не CEO", ""))

    # --- ожидание по времени at:<ISO> ---
    def test_at_form_parses_and_is_met_by_time(self):
        self.assertEqual(T.parse_wait_for("at:2026-10-09T02:00:00+04:00")[0], "at")
        self.assertIsNone(T.parse_wait_for("at:вчера"))
        self.assertTrue(D.check_wait_for("at:2000-01-01T00:00:00+00:00"))
        self.assertFalse(D.check_wait_for("at:2999-01-01T00:00:00+00:00"))

    def test_at_wait_is_alive_for_watch_and_shows_time_left(self):
        p = self._waiting("at:2999-01-01T00:00:00+00:00")
        alive = W.triage_waits({}, self.now, probe=lambda *a: "dead")
        self.assertEqual(alive, {T.read_ticket(p).id})
        self.assertEqual(T.read_ticket(p).status, "waiting")
        text = T.at_left_text("at:2026-10-09T02:00:00+04:00", datetime.fromisoformat("2026-10-08T05:00:00+04:00"))
        at = datetime.fromisoformat("2026-10-09T02:00:00+04:00").astimezone()  # пояс раннера не важен
        self.assertEqual(text, f"ждёт до {at:%d.%m %H:%M}, осталось 21 ч 00 мин")

    def test_at_wait_past_time_plus_grace_wakes_owner(self):
        p = self._waiting("at:2000-01-01T00:00:00+00:00")
        ws = {}
        for _ in range(W.DEAD_WAIT_STRIKES):
            W.triage_waits(ws, self.now)
        t = T.read_ticket(p)
        self.assertEqual((t.status, t.header.get("wait_for", "")), ("in_progress", ""))
        recent = (datetime.now().astimezone() - timedelta(minutes=W.AT_GRACE_MIN - 5)).isoformat(timespec="seconds")
        p2 = self._waiting("at:" + recent)
        for _ in range(W.DEAD_WAIT_STRIKES + 1):
            W.triage_waits({}, self.now)
        self.assertEqual(T.read_ticket(p2).status, "waiting")  # в допуске — молчим

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
        self.assertEqual((t.status, t.header.get("next"), t.log[-1].author), ("in_progress", "judge", "watch"))
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
        W.run_once(self.now, probes=False)
        self.assertTrue(W.WATCH_HEARTBEAT_FILE.exists())


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


class WatchLogTests(WatchSandbox):
    def test_watch_plan_reminder_keeps_ceo_handoff_alive(self):
        # напоминание сторожа не считается ответом CEO: метка передачи жива, «ждёт-ceo» придёт (ревью #27)
        p = T.create_ticket(self.tickets_dir, owner="engineer", title="Передача", status="waiting", now=self.now)
        tid = T.read_ticket(p).id
        T.append_log(p, "watch", "напоминание")
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

    def test_posix_file_wait_rejected_on_windows(self):
        # случай 7 (TK-065): file:/tmp/x на Windows — диспетчер ищет <диск>:\tmp, роль пишет в %TEMP% → вечное ожидание
        import tickets as TK
        p = T.create_ticket(self.tickets_dir, owner="engineer", title="Ждёт /tmp", status="in_progress", now=self.now)
        tid = T.read_ticket(p).id
        orig, TK.TICKETS_DIR = TK.TICKETS_DIR, self.tickets_dir
        try:
            with mock.patch.object(T, "_IS_WINDOWS", True):
                self.assertEqual(TK.main(["wait", tid, "file:/tmp/done.json"]), 1)
                self.assertEqual(T.read_ticket(p).status, "in_progress")  # отказ — шапка не тронута
                for ok in ("file:C:/tmp/x", "file:data/x.json", "file://srv/share/x"):
                    self.assertEqual(T.file_wait_problem(ok), "")
            with mock.patch.object(T, "_IS_WINDOWS", False):
                self.assertEqual(T.file_wait_problem("file:/tmp/done.json"), "")
        finally:
            TK.TICKETS_DIR = orig

    def test_posix_file_wait_on_windows_is_reported_to_ceo(self):
        p = T.create_ticket(self.tickets_dir, owner="engineer", title="Ждёт /tmp", status="waiting", now=self.now)
        with mock.patch.object(T, "_IS_WINDOWS", False):  # запись допустима: старый тикет мог уже ждать так
            T.write_header_updates(p, {"wait_for": "file:/tmp/done.json"}, now=self.now)
        state = {}
        with mock.patch.object(T, "_IS_WINDOWS", True), mock.patch.object(D, "append_ceo_inbox") as inbox:
            D.notify_wait_for_problem(T.read_ticket(p), state, self.now)
        self.assertEqual(inbox.call_count, 1)
        self.assertIn("posix-путь на Windows", inbox.call_args[0][2])


class HostAlertsAdapterTests(WatchSandbox):
    """№17: RPV_HOST_ALERTS_CMD → строки «ждёт вас» через ask.new_notice."""

    def test_lines_become_notices_without_duplicates(self):
        root = Path(self.tmp.name) / "proj"
        out = types.SimpleNamespace(stdout="диск 95 %\n\nсвязь пропала\n")
        with mock.patch.dict(os.environ, {"RPV_HOST_ALERTS_CMD": "x"}), mock.patch.object(W.D, "PROJECT_ROOT", root):
            self.assertEqual(W.check_host_alerts(run=lambda *a, **k: out), 2)
            W.check_host_alerts(run=lambda *a, **k: out)
        qs = sorted((root / ".claude" / "pulse" / "questions").glob("q-host-*.json"))
        self.assertEqual(len(qs), 2)

    def test_no_setting_is_silent(self):
        with mock.patch.dict(os.environ, {"RPV_HOST_ALERTS_CMD": ""}):
            self.assertEqual(W.check_host_alerts(run=lambda *a, **k: 1 / 0), 0)


if __name__ == "__main__":
    unittest.main()
