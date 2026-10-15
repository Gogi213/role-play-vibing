"""CP14: живой Надзиратель (`overseer.look`) и его запуск диспетчером (`dispatch._overseer`). Настоящий `claude` не зовётся:
ответ модели подменяется."""
import json
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dispatch as D  # noqa: E402
import overseer as O  # noqa: E402
import report as R  # noqa: E402
import ticket as T  # noqa: E402

TZ = timezone(timedelta(hours=4))
T0 = datetime(2026, 10, 14, 10, 0, tzinfo=TZ)
QUOTE = "замер снова без выигрыша, разница в шуме"


def ticket_text(tid, status, entries, desc="Ссылка на задачу владельца: G-001\n\nСделать быстрее в 4 раза."):
    log = "".join(f"### {ts.isoformat()} {role}\n{text}\n\n" for ts, role, text in entries)
    return f"---\nid: {tid}\ntitle: Задача {tid}\nowner: engineer\nstatus: {status}\nnext: \n---\n\n{desc}\n\n## Лог\n\n{log}"


def answer(verdict="тихо", **kw):
    return {"answer": json.dumps({"verdict": verdict, **kw}, ensure_ascii=False), "cost": 0.002, "error": False}


def finding(kind="не-движемся", quote=QUOTE, action="Остановить и перепланировать.", **kw):
    return answer("находка", kind=kind, ticket="TK-200", quote=quote, action=action, **kw)


class LookTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="rpv-overseer-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.tdir = self.root / ".claude" / "tickets"
        self.tdir.mkdir(parents=True)
        (self.root / ".claude" / "goals").mkdir()
        (self.root / ".claude" / "goals" / "G-001.md").write_text("цель: бэктест в 4 раза быстрее", encoding="utf-8")
        runs = []
        for i in range(6):  # закрытые тикеты проекта: по $10 — медиана $10, правило «дорого» с $30
            tid = f"TK-10{i}"
            ts = T0 - timedelta(days=2, hours=i)
            (self.tdir / f"{tid}.md").write_text(ticket_text(tid, "done", [(ts, "engineer", "сделано")]), encoding="utf-8")
            runs.append(f"{ts.isoformat()} {tid} engineer reason=todo cost_usd=10 resolved_cost=10 status=ok")
        self.runs = runs
        self.entries = [(T0 - timedelta(hours=3), "engineer", "начал замер"), (T0 - timedelta(hours=1), "engineer", QUOTE)]
        self.write_open(self.entries, spent=5)
        self.calls, self.routed = [], []

    def write_open(self, entries, spent):
        (self.tdir / "TK-200.md").write_text(ticket_text("TK-200", "in_progress", entries), encoding="utf-8")
        d = self.root / ".claude" / "dispatcher"
        d.mkdir(parents=True, exist_ok=True)
        (d / "runs.log").write_text("\n".join(self.runs + [f"{(T0 - timedelta(hours=2)).isoformat()} TK-200 engineer "
                                                         f"reason=todo cost_usd={spent} resolved_cost={spent} status=ok"]) + "\n",
                                    encoding="utf-8")

    def look(self, reply, now=T0, route=None):
        def call(prompt):
            self.calls.append(prompt)
            return reply
        route = route or (lambda tid, text: self.routed.append((tid, text)) or True)
        return O.look(self.root, now=now, call=call, route=route)

    def test_quiet_reads_ticket_once(self):
        rows = self.look(answer())
        self.assertEqual([(r["ticket"], r["outcome"]) for r in rows], [("TK-200", "quiet")])
        self.assertEqual(self.routed, [])
        self.assertIn("бэктест в 4 раза быстрее", self.calls[0], "цель владельца G-001 — в промпте")
        self.assertIn("медиана — $10.00", self.calls[0], "масштаб закрытых тикетов — в промпте")
        self.assertEqual(self.look(answer(), now=T0 + timedelta(minutes=15)), [], "новых записей нет — вызова нет")
        log = (self.root / ".claude" / "dispatcher" / "overseer.log").read_text(encoding="utf-8").splitlines()
        self.assertEqual(json.loads(log[0])["outcome"], "quiet")

    def test_finding_goes_to_judge_once_a_day_per_kind(self):
        self.look(finding())
        self.assertEqual(len(self.routed), 1, "находка — Судье через тикет")
        self.assertIn("не-движемся", self.routed[0][1])
        self.assertIn(QUOTE, self.routed[0][1])
        self.write_open(self.entries + [(T0 + timedelta(minutes=5), "engineer", QUOTE + " опять")], spent=5)
        rows = self.look(finding(), now=T0 + timedelta(minutes=20))
        self.assertTrue(rows[0].get("suppressed"))
        self.assertEqual(len(self.routed), 1, "та же находка по тикету — не чаще раза в сутки")
        self.write_open(self.entries + [(T0 + timedelta(minutes=30), "engineer", "регресс: сломали принятое " + QUOTE)], spent=5)
        self.look(finding("ломает-принятое"), now=T0 + timedelta(minutes=40))
        self.assertEqual(len(self.routed), 2, "другой вид на том же тикете не глушится")

    def test_invented_quote_is_dropped(self):
        rows = self.look(finding(quote="этого текста нет в логе тикета"))
        self.assertEqual((rows[0]["outcome"], rows[0]["reason"]), ("rejected", "цитаты нет в логе дословно"))
        self.assertEqual(self.routed, [])

    def test_invented_number_in_action_is_not_passed_on(self):
        rows = self.look(finding(action="Остановить: осталось 17 часов до срока."))
        self.assertEqual(rows[0]["invented_numbers"], ["17"])
        self.assertNotIn("17 часов", self.routed[0][1])
        self.assertIn("числа не из данных", self.routed[0][1])

    def test_call_error_keeps_ticket_unread(self):
        self.look({"answer": "OSError", "cost": None, "error": True})
        self.assertEqual(len(self.look(answer(), now=T0 + timedelta(minutes=15))), 1, "после сбоя тикет смотрим снова")

    def test_cost_rule_steps_without_model(self):
        self.write_open(self.entries, spent=31)  # больше 3× медианы ($10)
        self.look(answer())
        self.assertEqual([t for t, _ in self.routed], ["TK-200"])
        self.assertIn("дорого", self.routed[0][1])
        self.assertIn("3× медианы", self.routed[0][1])
        self.look(answer(), now=T0 + timedelta(minutes=15))
        self.assertEqual(len(self.routed), 1, "та же ступень — один раз")
        self.write_open(self.entries, spent=61)  # больше 6× медианы
        self.look(answer(), now=T0 + timedelta(minutes=30))
        self.assertEqual(len(self.routed), 2)
        self.assertIn("6×", self.routed[1][1])

    def test_spike_rule(self):
        """Всплеск: за 6 ч тикет потратил больше 3× обычных трат тикета за 6 ч в проекте."""
        base = T0 - timedelta(days=5)
        self.runs = [f"{(base + timedelta(hours=3 * i)).isoformat()} TK-10{i % 6} engineer reason=todo cost_usd=1 "
                     f"resolved_cost=1 status=ok" for i in range(40)]  # 40 замеров по $1 — обычные траты за 6 ч $1
        self.write_open(self.entries, spent=4)
        self.look(answer())
        self.assertTrue(any("всплеск" in text for _, text in self.routed), self.routed)
        self.look(answer(), now=T0 + timedelta(minutes=15))
        self.assertEqual(sum("всплеск" in text for _, text in self.routed), 1, "всплеск по тикету — раз в сутки")

    def test_abandoned_open_ticket_is_not_watched(self):
        """Открытый тикет без записей и запусков за сутки — брошен, деньги не тратит: ни вызова, ни «дорого»."""
        self.write_open(self.entries, spent=50)
        self.assertEqual(self.look(answer(), now=T0 + timedelta(days=2)), [])
        self.assertEqual((self.calls, self.routed), ([], []))

    def test_question_already_decided_is_checked_against_registry(self):
        (self.root / ".claude" / "decisions.md").write_text(
            "# Решения владельца\n\n## В-7 · 2026-10-01 12:00\n> сырьё остаётся\nСуть: сырьё остаётся в пуле\nЗаменяет: —\n\n"
            "## В-8 · 2026-10-02 12:00\n> про другое\nСуть: другое\nЗаменяет: —\n", encoding="utf-8")
        ask = "[итог: ask-owner] оставить сырьё в пуле?"
        self.write_open(self.entries + [(T0 - timedelta(minutes=30), "judge", ask)], spent=5)
        self.look(finding("вопрос-уже-решён", quote=ask, decision="В-7"))
        self.assertIn("В-7 · сырьё остаётся в пуле", self.calls[0], "все решения — по строке")
        self.assertIn("В-8 · другое", self.calls[0])
        self.assertIn("решение уже есть: В-7", self.routed[0][1])
        self.write_open(self.entries + [(T0 + timedelta(minutes=10), "judge", ask + " ещё раз")], spent=5)
        rows = self.look(finding("вопрос-уже-решён", quote=ask, decision="В-99"), now=T0 + timedelta(minutes=30))
        self.assertEqual(rows[-1].get("decision_unknown"), "В-99", "номера нет в реестре — не выдаём его за ответ")

    def test_report_line(self):
        self.write_open(self.entries, spent=31)
        self.look(finding())
        p = R.Period(self.root, T0 - timedelta(hours=1), T0 + timedelta(hours=1), [])
        self.assertEqual(R.m_overseer(p), "находок Судье 2 (дорого 1, не-движемся 1), взглядов 1, $0.00")


class ToJudgeTests(unittest.TestCase):
    """Находка — в лог тикета и `next: judge`; тикет занят (идёт запуск, назначена передача) — на следующем взгляде."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="rpv-overseer-j-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.tdir = self.root / ".claude" / "tickets"
        self.tdir.mkdir(parents=True)
        (self.root / ".claude" / "dispatcher").mkdir()
        self.path = self.tdir / "TK-200.md"
        self.path.write_text(ticket_text("TK-200", "in_progress", [(T0 - timedelta(hours=1), "engineer", QUOTE)]),
                             encoding="utf-8")

    def state(self, active):
        (self.root / ".claude" / "dispatcher" / "state.json").write_text(
            json.dumps({"active_runs": {t: {} for t in active}}), encoding="utf-8")

    def test_free_ticket_goes_to_judge(self):
        self.assertTrue(O.to_judge(self.root, "TK-200", "Надзиратель, не-движемся: …", T0))
        t = T.read_ticket(self.path)
        self.assertEqual(t.header.get("next"), "judge")
        self.assertEqual(t.log[-1].author, "надзиратель")
        self.assertIn("Судье: проверь по делу", t.log[-1].text)

    def test_busy_ticket_waits_then_goes(self):
        self.state(["TK-200"])
        self.assertFalse(O.to_judge(self.root, "TK-200", "x", T0))
        self.assertEqual(T.read_ticket(self.path).header.get("next", ""), "")
        st = {"pending": [{"ticket": "TK-200", "text": "Надзиратель, дорого: …"}]}
        O.save_state(self.root, st)
        self.state([])
        O.look(self.root, now=T0, call=lambda p: answer())
        self.assertEqual(T.read_ticket(self.path).header.get("next"), "judge")
        self.assertEqual(O.load_state(self.root)["pending"], [])

    def test_own_entry_is_not_new_work(self):
        calls = []
        O.look(self.root, now=T0, call=lambda p: calls.append(p) or answer())
        T.append_log(self.path, "надзиратель", "запись Надзирателя", now=T0 + timedelta(minutes=1))
        O.look(self.root, now=T0 + timedelta(minutes=15), call=lambda p: calls.append(p) or answer())
        self.assertEqual(len(calls), 1)

    def ceo_files(self):
        d = self.root / ".claude" / "dispatcher"
        return [n for n in ("ceo-inbox.md", "ceo-wake.log") if (d / n).exists()]

    def test_finding_does_not_wake_ceo(self):
        """Находка — только Судье: ни строки в ceo-inbox.md, ни в ceo-wake.log (на нём стоит Monitor CEO)."""
        O.look(self.root, now=T0, call=lambda p: finding())
        self.assertEqual(T.read_ticket(self.path).header.get("next"), "judge")
        self.assertEqual(self.ceo_files(), [])

    def test_main_runs_without_dispatcher_and_does_not_wake_ceo(self):
        """Живой запуск `overseer.py --project` — без `dispatch` (он нужен был только для строки CEO). `main` берёт настоящие часы,
        поэтому запись в тикете — «час назад» от них, а не фиксированная дата (старше суток Надзиратель тикет не смотрит)."""
        now = datetime.now(TZ)
        self.path.write_text(ticket_text("TK-200", "in_progress", [(now - timedelta(hours=1), "engineer", QUOTE)]), encoding="utf-8")
        with mock.patch.object(O, "call_overseer", lambda *a, **kw: finding()):
            self.assertEqual(O.main(["--project", str(self.root)]), 0)
        self.assertEqual(T.read_ticket(self.path).header.get("next"), "judge")
        self.assertEqual(self.ceo_files(), [])


class DispatcherHookTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="rpv-overseer-d-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        (self.root / ".claude" / "tickets").mkdir(parents=True)
        saved = (D.PROJECT_ROOT, D.TICKETS_DIR, D.DISPATCHER_DIR, D._OVERSEER_PROC)
        self.addCleanup(lambda: setattr(D, "_OVERSEER_PROC", saved[3]))
        self.addCleanup(lambda: (setattr(D, "PROJECT_ROOT", saved[0]), setattr(D, "TICKETS_DIR", saved[1]),
                                 setattr(D, "DISPATCHER_DIR", saved[2])))
        D.PROJECT_ROOT, D.TICKETS_DIR = self.root, self.root / ".claude" / "tickets"
        D.DISPATCHER_DIR, D._OVERSEER_PROC = self.root / ".claude" / "dispatcher", None
        self.spawned = []

        class Proc:
            def poll(self):
                return 0
        self.popen = mock.patch.object(D, "_popen", lambda cmd, **kw: self.spawned.append(cmd) or Proc())
        self.popen.start()
        self.addCleanup(self.popen.stop)

    def test_every_15_min_and_only_when_tickets_changed(self):
        now = datetime.now().astimezone()
        state = {}
        D._overseer(state, now)
        self.assertEqual(self.spawned, [], "первый тик — только метка")
        (self.root / ".claude" / "tickets" / "TK-001.md").write_text("x", encoding="utf-8")
        D._overseer(state, now + timedelta(minutes=5))
        self.assertEqual(self.spawned, [], "раньше 15 мин — не зовём")
        D._overseer(state, now + timedelta(minutes=16))
        self.assertEqual(len(self.spawned), 1)
        self.assertTrue(str(self.spawned[0][1]).endswith("overseer.py"))
        D._overseer(state, now + timedelta(minutes=40))
        self.assertEqual(len(self.spawned), 1, "тикеты не менялись — не зовём")


class OwnerQuestionTests(unittest.TestCase):
    """Критерии 5, 9: вопрос Судьи владельцу — одно сообщение в Telegram на вопрос, повод и текст; отвечают CEO."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="rpv-owner-q-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        (self.root / ".claude" / "tickets").mkdir(parents=True)
        saved = (D.TICKETS_DIR, D.DISPATCHER_DIR)
        self.addCleanup(lambda: (setattr(D, "TICKETS_DIR", saved[0]), setattr(D, "DISPATCHER_DIR", saved[1])))
        D.TICKETS_DIR, D.DISPATCHER_DIR = self.root / ".claude" / "tickets", self.root / ".claude" / "dispatcher"
        self.sent = []
        p = mock.patch.object(D.notify, "send", lambda text, **kw: self.sent.append(text))
        p.start()
        self.addCleanup(p.stop)

    def write(self, status, entries):
        (D.TICKETS_DIR / "TK-300.md").write_text(ticket_text("TK-300", status, entries), encoding="utf-8")

    def test_one_message_per_question(self):
        ask = (T0, "judge", "[итог: ask-owner] повод: деньги — сервер на месяц $40: брать?")
        self.write("needs_owner", [ask])
        state = {}
        D._owner_questions(state, T0)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("Вопрос вам (деньги)", self.sent[0])
        self.assertIn("сервер на месяц $40: брать?", self.sent[0])
        self.assertIn("Ответьте CEO", self.sent[0])
        D._owner_questions(state, T0 + timedelta(seconds=15))
        self.assertEqual(len(self.sent), 1, "тот же вопрос — один раз")
        self.write("needs_owner", [ask, (T0 + timedelta(hours=1), "judge", "[итог: ask-owner] повод: направление — A или B?")])
        D._owner_questions(state, T0 + timedelta(hours=1))
        self.assertEqual(len(self.sent), 2)
        self.assertIn("(направление)", self.sent[1])

    def test_no_message_without_question_to_owner(self):
        self.write("in_progress", [(T0, "judge", "[итог: ask-owner] повод: деньги — вопрос")])
        self.write("blocked", [(T0, "engineer", "[итог: ask-owner] вопрос Судье")])
        D._owner_questions({}, T0)
        self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main()
