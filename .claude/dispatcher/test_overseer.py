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

TZ = timezone(timedelta(hours=4))
T0 = datetime(2026, 10, 14, 10, 0, tzinfo=TZ)
QUOTE = "замер снова без выигрыша, разница в шуме"


def ticket_text(tid, status, entries, desc="Ссылка на задачу владельца: G-001\n\nСделать быстрее в 4 раза."):
    log = "".join(f"### {ts.isoformat()} {role}\n{text}\n\n" for ts, role, text in entries)
    return f"---\nid: {tid}\ntitle: Задача {tid}\nowner: engineer\nstatus: {status}\n---\n\n{desc}\n\n## Лог\n\n{log}"


def answer(verdict="тихо", **kw):
    return {"answer": json.dumps({"verdict": verdict, **kw}, ensure_ascii=False), "cost": 0.002, "error": False}


class LookTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="rpv-overseer-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.tdir = self.root / ".claude" / "tickets"
        self.tdir.mkdir(parents=True)
        (self.root / ".claude" / "goals").mkdir()
        (self.root / ".claude" / "goals" / "G-001.md").write_text("цель: бэктест в 4 раза быстрее", encoding="utf-8")
        runs = []
        for i in range(6):  # закрытые тикеты проекта: масштаб — по $10, 90-й процентиль $10
            tid = f"TK-10{i}"
            ts = T0 - timedelta(days=2, hours=i)
            (self.tdir / f"{tid}.md").write_text(ticket_text(tid, "done", [(ts, "engineer", "сделано")]), encoding="utf-8")
            runs.append(f"{ts.isoformat()} {tid} engineer reason=todo cost_usd=10 resolved_cost=10 status=ok")
        self.runs = runs
        self.entries = [(T0 - timedelta(hours=3), "engineer", "начал замер"), (T0 - timedelta(hours=1), "engineer", QUOTE)]
        self.write_open(self.entries, spent=5)
        self.sent, self.calls = [], []

    def write_open(self, entries, spent):
        (self.tdir / "TK-200.md").write_text(ticket_text("TK-200", "in_progress", entries), encoding="utf-8")
        d = self.root / ".claude" / "dispatcher"
        d.mkdir(parents=True, exist_ok=True)
        (d / "runs.log").write_text("\n".join(self.runs + [f"{(T0 - timedelta(hours=2)).isoformat()} TK-200 engineer "
                                                         f"reason=todo cost_usd={spent} resolved_cost={spent} status=ok"]) + "\n",
                                    encoding="utf-8")

    def look(self, reply, now=T0):
        def call(prompt):
            self.calls.append(prompt)
            return reply
        return O.look(self.root, now=now, call=call, notify=lambda tid, text: self.sent.append((tid, text)))

    def test_quiet_reads_ticket_once(self):
        rows = self.look(answer())
        self.assertEqual([(r["ticket"], r["outcome"]) for r in rows], [("TK-200", "quiet")])
        self.assertEqual(self.sent, [])
        self.assertIn("бэктест в 4 раза быстрее", self.calls[0], "цель владельца G-001 — в промпте")
        self.assertIn("90-й процентиль — $10.00", self.calls[0], "масштаб закрытых тикетов — в промпте")
        self.assertEqual(self.look(answer(), now=T0 + timedelta(minutes=15)), [], "новых записей нет — вызова нет")
        log = (self.root / ".claude" / "dispatcher" / "overseer.log").read_text(encoding="utf-8").splitlines()
        self.assertEqual(json.loads(log[0])["outcome"], "quiet")

    def test_finding_with_verbatim_quote_goes_to_ceo_once_a_day(self):
        found = answer("находка", kind="не-движемся", ticket="TK-200", quote=QUOTE, action="Остановить и перепланировать.")
        self.look(found)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("не-движемся", self.sent[0][1])
        self.assertIn(QUOTE, self.sent[0][1])
        self.write_open(self.entries + [(T0 + timedelta(minutes=5), "engineer", QUOTE + " опять")], spent=5)
        rows = self.look(found, now=T0 + timedelta(minutes=20))
        self.assertTrue(rows[0].get("suppressed"))
        self.assertEqual(len(self.sent), 1, "та же находка по тикету — не чаще раза в сутки")

    def test_invented_quote_is_dropped(self):
        rows = self.look(answer("находка", kind="не-движемся", ticket="TK-200", quote="этого текста нет в логе тикета",
                                action="Остановить."))
        self.assertEqual((rows[0]["outcome"], rows[0]["reason"]), ("rejected", "цитаты нет в логе дословно"))
        self.assertEqual(self.sent, [])

    def test_call_error_keeps_ticket_unread(self):
        self.look({"answer": "OSError", "cost": None, "error": True})
        self.assertEqual(len(self.look(answer(), now=T0 + timedelta(minutes=15))), 1, "после сбоя тикет смотрим снова")

    def test_cost_rule_steps_without_model(self):
        self.write_open(self.entries, spent=11)  # больше 1× p90 ($10)
        self.look(answer())
        self.assertEqual([t for t, _ in self.sent], ["TK-200"])
        self.assertIn("дорого", self.sent[0][1])
        self.look(answer(), now=T0 + timedelta(minutes=15))
        self.assertEqual(len(self.sent), 1, "та же ступень — один раз")
        self.write_open(self.entries, spent=25)  # больше 2× p90
        self.look(answer(), now=T0 + timedelta(minutes=30))
        self.assertEqual(len(self.sent), 2)
        self.assertIn("2×", self.sent[1][1])

    def test_abandoned_open_ticket_is_not_watched(self):
        """Открытый тикет без записей и запусков за сутки — брошен, деньги не тратит: ни вызова, ни «дорого»."""
        self.write_open(self.entries, spent=50)
        self.assertEqual(self.look(answer(), now=T0 + timedelta(days=2)), [])
        self.assertEqual((self.calls, self.sent), ([], []))

    def test_question_to_owner_brings_decisions_registry(self):
        (self.root / ".claude" / "decisions.md").write_text("В-7 «сырьё остаётся»", encoding="utf-8")
        self.write_open(self.entries + [(T0 - timedelta(minutes=30), "judge", "[итог: ask-owner] оставить сырьё?")], spent=5)
        self.look(answer())
        self.assertIn("В-7 «сырьё остаётся»", self.calls[0])

    def test_report_line(self):
        self.write_open(self.entries, spent=11)
        self.look(answer("находка", kind="не-движемся", ticket="TK-200", quote=QUOTE, action="Остановить."))
        p = R.Period(self.root, T0 - timedelta(hours=1), T0 + timedelta(hours=1), [])
        self.assertEqual(R.m_overseer(p), "находок CEO 2 (дорого 1, не-движемся 1), взглядов 1, $0.00")


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


if __name__ == "__main__":
    unittest.main()
