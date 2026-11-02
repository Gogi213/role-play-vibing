"""TK-105 (В-50): Пульс v2 — факты карточки проекта: цель и шаг, кто что делает, вопрос владельцу, причина по-человечески, день."""
from __future__ import annotations

import sys
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_tk086 import GoalSandbox, T  # noqa: E402 — песочница тикетов и целей
import pulse_facts as F  # noqa: E402


class FactsBase(GoalSandbox):
    def read(self):
        return [T.read_ticket(p) for p in T.list_tickets(self.tickets_dir)]

    def log(self, tid, author, text, at=None):
        T.append_log(self.tickets_dir / f"{tid}.md", author, text, now=at or self.now)


class AgoTests(unittest.TestCase):
    def test_human_durations(self):
        for seconds, want in [(-5, "меньше минуты"), (0, "меньше минуты"), (59, "меньше минуты"), (60, "1 мин"), (59 * 60, "59 мин"),
                              (3600, "1 ч"), (7800, "2 ч 10 мин"), (86400, "1 день"), (3 * 86400, "3 дня"), (5 * 86400, "5 дней"),
                              (11 * 86400, "11 дней"), (22 * 86400, "22 дня"), (23578 * 60, "16 дней")]:
            self.assertEqual(F.ago(seconds), want, seconds)


class GoalCardTests(FactsBase):
    def test_step_is_the_highest_marked_step_of_the_goal(self):
        self.goal("G-002")
        a = self.ticket(ref="G-002", status="done", desc="Шаг 1 из 3 цели G-002.")
        b = self.ticket(ref="G-002", status="in_progress", desc="Шаг 2 из 3 цели G-002.")
        self.ticket(ref="G-001", status="todo", desc="Шаг 3 из 5 цели G-001.")  # чужая цель шаг не двигает
        card = F.goal_card(self.read(), [b, a])
        self.assertEqual((card["id"], card["step"], card["steps"]), ("G-002", 2, 3))

    def test_steps_total_comes_from_the_line_of_the_highest_step(self):
        self.goal("G-002")
        a = self.ticket(ref="G-002", status="done", desc="Шаг 1 из 3 цели G-002.")
        b = self.ticket(ref="G-002", status="in_progress", desc="Шаг 2 из 4 цели G-002.")
        card = F.goal_card(self.read(), [a, b])
        self.assertEqual((card["step"], card["steps"]), (2, 4), "«из M» берётся из строки шага 2, а не из первой найденной")

    def test_goal_without_plan_has_no_step(self):
        tid = self.ticket(status="in_progress")
        card = F.goal_card(self.read(), [tid])
        self.assertEqual(card, {"id": "G-001", "title": "Цель G-001"})

    def test_stopped_step_does_not_count(self):
        self.ticket(status="stopped", desc="Шаг 4 из 4 цели G-001.")
        tid = self.ticket(status="in_progress", desc="Шаг 2 из 4 цели G-001.")
        self.assertEqual(F.goal_card(self.read(), [tid])["step"], 2)

    def test_closed_or_missing_goal_gives_none(self):
        self.goal("G-002", status="accepted")
        closed = self.ticket(ref="G-002", status="in_progress")
        free = self.ticket(ref="", status="in_progress")
        self.assertIsNone(F.goal_card(self.read(), [closed, free, "TK-999"]))

    def test_spaces_in_step_line_are_not_a_mark(self):
        tid = self.ticket(status="in_progress", desc="Шаг  2  из 4 цели G-001.")
        self.assertNotIn("step", F.goal_card(self.read(), [tid]))


class DayTests(FactsBase):
    def test_accepted_today_returns_and_queue(self):
        yesterday = self.now - timedelta(days=1)
        today = self.ticket(status="done", accepted_at=self.now, title="Сегодня")
        self.ticket(status="done", accepted_at=yesterday, title="Вчера")
        back = self.ticket(status="in_review", title="Вернули")
        self.log(back, "judge", "[итог: return] проверка: x — нет")
        self.log(back, "judge", "[итог: return] проверка: x — снова", yesterday)  # вчерашний возврат не в счёт
        running = self.ticket(status="in_progress", title="Идёт")
        self.ticket(status="todo", title="Ждёт")
        data = F.day_of(self.read(), {"active_runs": {running: {"role": "engineer"}}}, self.now)
        self.assertEqual(data["day"], {"accepted": 1, "returns": 1, "working": 1, "queued": 2})
        self.assertEqual([x["ticket"] for x in data["done_today"]], [today])
        self.assertEqual([x["title"] for x in data["queue"]], ["Вернули", "Ждёт"])

    def test_empty_project_is_zeros(self):
        self.assertEqual(F.day_of([], {}, self.now),
                         {"done_today": [], "queue": [], "day": {"accepted": 0, "returns": 0, "working": 0, "queued": 0}})

    def test_judge_ticket_closed_by_artifact_is_not_accepted_work(self):
        self.ticket(owner="judge", status="done", accepted_at=self.now)
        self.assertEqual(F.day_of(self.read(), {}, self.now)["day"]["accepted"], 0)


class JobAskFixTests(FactsBase):
    def test_job_role_verb_and_duration(self):
        tid = self.ticket(status="in_progress", title="TK-5: страница Пульса")
        started = T.now_iso(self.now - timedelta(minutes=130))
        job = F.job_of({"active_runs": {tid: {"role": "judge", "started": started}}}, self.read(), self.now)
        self.assertEqual((job["who"], job["verb"], job["title"], job["since"]), ("Судья", "проверяет", "страница Пульса", "2 ч 10 мин"))
        eng = F.job_of({"active_runs": {tid: {"role": " Engineer ", "started": started}}}, self.read(), self.now)
        self.assertEqual((eng["who"], eng["verb"]), ("Инженер", "делает"))  # регистр и пробелы роли не мешают
        odd = F.job_of({"active_runs": {tid: {"role": "intern", "started": started}}}, self.read(), self.now)
        self.assertEqual(odd["who"], "")  # незнакомая роль — без имени, не выдумываем
        self.assertIsNone(F.job_of({}, self.read(), self.now))

    def test_ask_takes_the_question_from_ask_owner_entry(self):
        tid = self.ticket(status="needs_owner", title="Закачка")
        self.log(tid, "judge", "[итог: ask-owner] брать фьючерсы или только спот? повод: необратимое", self.now - timedelta(minutes=45))
        ask = F.ask_of(self.read(), [(tid, "ответить на вопрос")], self.now)
        self.assertEqual((ask["ticket"], ask["question"], ask["since"]), (tid, "брать фьючерсы или только спот?", "45 мин"))

    def test_ask_without_entry_falls_back_to_title(self):
        tid = self.ticket(status="needs_owner", title="Закачка")
        self.assertEqual(F.ask_of(self.read(), [(tid, "x")], self.now)["question"], "Закачка")
        self.assertIsNone(F.ask_of(self.read(), [], self.now))

    def test_fix_has_no_raw_codes_or_minutes(self):
        since = T.now_iso(self.now - timedelta(days=3))
        fix = F.fix_of([("step_uncovered", "цель G-006: принят TK-9 23578 мин назад, следующего шага нет — завести тикет", since)], self.now)
        self.assertEqual(fix, {"text": "у цели G-006 нет следующего шага уже 3 дня", "who": "заводит тикет CEO"})
        dead = F.fix_of([("dispatcher_dead", "диспетчер молчал 9 мин", T.now_iso(self.now - timedelta(minutes=23)))], self.now)
        self.assertEqual(dead, {"text": "диспетчер молчит 23 мин", "who": "перезапускает CEO"})
        self.assertIsNone(F.fix_of([], self.now))

    def test_facts_without_readable_tickets_is_empty(self):
        self.assertEqual(F.facts({}, None, [], [], self.now), {})

    def test_facts_collects_all_fields(self):
        tid = self.ticket(status="in_progress", desc="Шаг 1 из 2 цели G-001.")
        state = {"active_runs": {tid: {"role": "engineer", "started": T.now_iso(self.now)}}}
        out = F.facts(state, self.read(), [], [], self.now)
        self.assertEqual((out["goal"]["step"], out["job"]["ticket"], out["day"]["working"]), (1, tid, 1))
        self.assertNotIn("ask", out)
        self.assertNotIn("fix", out)


if __name__ == "__main__":
    unittest.main()
