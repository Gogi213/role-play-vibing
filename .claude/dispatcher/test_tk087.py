"""TK-087 (В-35): план шагов цели — приёмка шага заводит следующий кодом; `--tail ceo:` с кодом/слиянием отказывает."""
from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import test_result as TR  # первым: задаёт песочницу CLAUDE_PROJECT_DIR
import goal_steps as GS
import registry as R
import ticket as T
import tickets as TK

NOW = datetime(2026, 11, 2, 14, 0).astimezone()
STEPS = ["engineer | Сделать А | тест А зелёный", "researcher | Разобрать Б | отчёт Б в docs"]


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.tickets = self.root / "tickets"
        self.tickets.mkdir()
        patch = mock.patch.object(R, "_refuse_role", return_value="")
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(self.tmp.cleanup)
        self.state: dict = {}

    def goal(self, *steps, title="Цель"):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = R.main(["--project", str(self.root), "goal", "--title", title, "--quote", "сделай это и то",
                           *[x for s in steps for x in ("--step", s)]])
        self.assertEqual(code, 0)
        return out.getvalue().strip()

    def advance(self):
        with mock.patch.object(GS.SIG, "append_ceo_inbox") as sig:
            done = GS.advance(self.root, self.tickets, self.state, NOW)
        return done, sig

    def step_tickets(self):
        return [T.read_ticket(p) for p in T.list_tickets(self.tickets)]


class GoalPlan(Sandbox):
    def test_steps_written_and_read_back(self):
        gid = self.goal(*STEPS)
        self.assertEqual([(s["n"], s["role"], s["title"], s["done"]) for s in R.goal_steps(self.root, gid)],
                         [(1, "engineer", "Сделать А", "тест А зелёный"), (2, "researcher", "Разобрать Б", "отчёт Б в docs")])

    def test_goal_without_steps_has_no_plan(self):
        self.assertEqual(R.goal_steps(self.root, self.goal()), [])

    def test_bad_step_refused_and_nothing_written(self):
        for bad in ("engineer | только заголовок", "кто-то | Заголовок | готово", "engineer |  | готово"):
            with self.subTest(bad=bad), contextlib.redirect_stderr(io.StringIO()):
                code = R.main(["--project", str(self.root), "goal", "--title", "Ц", "--quote", "сделай это и то", "--step", bad])
                self.assertEqual(code, 1)
        self.assertEqual(R.goals(self.root), {})


class Advance(Sandbox):
    def test_accepting_first_step_files_second(self):
        gid = self.goal(*STEPS)
        done, _ = self.advance()
        self.assertEqual(done, [f"TK-001: шаг 1 цели {gid}"])
        first = self.step_tickets()[0]
        self.assertEqual((first.owner, first.reviewer, first.status), ("engineer", "judge", "todo"))
        self.assertIn(f"Шаг 1 из 2 цели {gid}.", first.description)
        self.assertIn(f"Ссылка на задачу владельца: {gid}", first.description)
        self.assertIn("Готово, когда: тест А зелёный", first.description)
        self.assertEqual(self.advance()[0], [])        # тикет шага не принят — второй не заводим, первый не дублируем
        T.write_header_updates(first.path, {"status": "in_progress"}, now=NOW)
        self.assertEqual(self.advance()[0], [])
        T.write_header_updates(first.path, {"status": "done"}, now=NOW)
        done, sig = self.advance()
        self.assertEqual(done, [f"TK-002: шаг 2 цели {gid}"])
        sig.assert_not_called()
        second = self.step_tickets()[1]
        self.assertEqual((second.owner, second.header["title"]), ("researcher", "Разобрать Б"))
        self.assertIn(f"Шаг 2 из 2 цели {gid}.", second.description)
        self.assertIn("Предыдущий шаг плана принят: TK-001.", second.description)
        self.assertEqual(self.advance()[0], [])        # второй уже заведён — повтора нет

    def test_plan_ends_signals_ceo_once(self):
        gid = self.goal(*STEPS)
        for _ in range(2):
            self.advance()
            for t in self.step_tickets():
                T.write_header_updates(t.path, {"status": "done"}, now=NOW)
        done, sig = self.advance()
        self.assertEqual(done, [f"{gid}: следующий не определён"])
        sig.assert_called_once()
        self.assertEqual(sig.call_args.args[:2], ("TK-002", "следующий-не-определён"))
        self.assertEqual(self.advance()[1].call_count, 0)  # одна строка на цель

    def test_stopped_step_signals_ceo_once_and_files_nothing(self):
        gid = self.goal(*STEPS)
        self.advance()
        first = self.step_tickets()[0]
        T.write_header_updates(first.path, {"status": "stopped"}, now=NOW)
        done, sig = self.advance()
        self.assertEqual(done, [f"TK-001: шаг 1 цели {gid} остановлен"])
        self.assertEqual(sig.call_args.args[:2], ("TK-001", "шаг-остановлен"))
        done, sig = self.advance()
        self.assertEqual((done, sig.call_count, len(self.step_tickets())), ([], 0, 1))  # одна строка на тикет, второй шаг не заведён

    def test_unreadable_ticket_blocks_the_pass(self):
        self.goal(*STEPS)
        (self.tickets / "TK-900.md").write_bytes(b"\xff\xfe not a ticket")
        with mock.patch.object(T, "read_ticket", side_effect=ValueError("битый тикет")):
            done, sig = self.advance()
        self.assertEqual(done, [])
        sig.assert_not_called()
        self.assertEqual([p.name for p in T.list_tickets(self.tickets)], ["TK-900.md"])  # шаг 1 не заведён: он мог стоять в битом тикете

    def test_goal_without_plan_or_not_open_is_left_alone(self):
        self.goal()
        gid = self.goal(*STEPS)
        with contextlib.redirect_stdout(io.StringIO()):
            R.main(["--project", str(self.root), "accept-goal", gid])
        self.assertEqual(self.advance()[0], [])
        self.assertEqual(self.step_tickets(), [])

    def test_tickets_of_other_goals_do_not_count(self):
        first = self.goal(*STEPS)
        second = self.goal(*STEPS, title="Другая")
        self.advance()
        self.assertEqual({t.description.split("Шаг 1 из 2 цели ")[1][:5] for t in self.step_tickets()}, {first, second})


class CeoTail(unittest.TestCase):
    """`--tail ceo:` — только то, что ролям не сделать; код и слияние — тикетом (реестр решений есть в песочнице)."""
    setUp_base, tearDown, tkt, _registry_with_decision = (TR.ResultTests.setUp, TR.ResultTests.tearDown, TR.ResultTests.tkt,
                                                          TR.ResultTests._registry_with_decision)

    def setUp(self):
        self.setUp_base()
        self._registry_with_decision()

    def ack(self, tail):
        return TK._tail_ack(type("A", (), {"tail": tail})(), self.tid)

    def test_code_and_merge_refused_with_hint(self):
        for text in ("влить PR 7 и поправить тест", "дописать код проверки", "слить ветку eng/tk-1", "исправить ошибку в модуле"):
            with self.subTest(text=text):
                err, mark, ceo = self.ack(f'ceo:"{text}"')
                self.assertIn("ролям по силам", err)
                self.assertIn(f"Хвост {self.tid}", err)
                self.assertEqual((mark, ceo), ("", ""))

    def test_missed_code_verbs_refused(self):
        for text in ("поправить проверку в диспетчере", "смержить ветку после CI", "запушить ветку", "починить диспетчер",
                     "согласие нужно на PR 5"):
            with self.subTest(text=text):
                self.assertIn("ролям по силам", self.ack(f'ceo:"{text}"')[0])

    def test_charter_sync_tail_of_tk050_passes(self):
        for text in ("charter sync --ref В-14 после влива PR #162", "charter sync с правкой устава CEO", "синхронизировать устав проекта после влива"):
            with self.subTest(text=text):
                err, _, ceo = self.ack(f'ceo:"{text}"')
                self.assertEqual((err, ceo), ("", text))

    def test_owner_zone_text_passes_even_with_code_words(self):
        for text in ("вписать хук в settings.json", "поставить настройку tail_fields_only", "слово владельца про код выпуска"):
            with self.subTest(text=text):
                err, mark, ceo = self.ack(f'ceo:"{text}"')
                self.assertEqual((err, ceo), ("", text))

    def test_tail_of_tk027_passes(self):
        """TK-027.md:36 — путь устава роли и флаг `--after-merge` (TK-099)."""
        text = "обновить .claude/roles/judge.md проекта из templates/roles/judge.md (пункт --after-merge)"
        err, _, ceo = self.ack(f'ceo:"{text}"')
        self.assertEqual((err, ceo), ("", text))

    def test_roles_path_is_owner_zone(self):
        text = "слить правки в .claude/roles/engineer.md"
        err, _, ceo = self.ack(f'ceo:"{text}"')
        self.assertEqual((err, ceo), ("", text))

    def test_flag_name_is_not_code_but_bare_merge_is(self):
        err, _, ceo = self.ack('ceo:"описать пункт --after-merge"')
        self.assertEqual((err, ceo), ("", "описать пункт --after-merge"))
        for text in ("сделать merge ветки", "влить PR --after-merge"):
            with self.subTest(text=text):
                self.assertIn("ролям по силам", self.ack(f'ceo:"{text}"')[0])

    def test_ticket_tail_still_accepted(self):
        other = T.create_ticket(TK.TICKETS_DIR, owner="engineer", title="Хвост: код").stem
        self.assertEqual(self.ack(other), ("", f" хвосты: {other}", ""))


if __name__ == "__main__":
    unittest.main()
