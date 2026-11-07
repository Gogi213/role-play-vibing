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


class PlanTests(FactsBase):
    """TK-108 (В-53): план цели по шагам, движение у текущего шага, числа «шаг N из M» из того же списка."""

    def plan_goal(self, n=3, gid="G-001"):
        path = Path(self.root.name) / ".claude" / "goals" / f"{gid}.md"
        steps = "\n".join(f"{i}. engineer | Заголовок {i} | готово {i}" for i in range(1, n + 1))
        path.write_text(path.read_text(encoding="utf-8") + f"\n## План шагов\n{steps}\n", encoding="utf-8")

    def step(self, n, total=3, **kw):
        return self.ticket(desc=f"Шаг {n} из {total} цели G-001.", **kw)

    def plan(self, state=None, active=(), owner=(), runs=(), at=None):
        return F.plan_of("G-001", self.read(), state or {}, list(active), list(owner), list(runs), at or self.now)

    def test_marks_done_run_next_with_accept_time_role_and_duration(self):
        self.plan_goal()
        self.step(1, status="done", accepted_at=self.now - timedelta(hours=2))
        tid = self.step(2, status="in_progress")
        state = {"active_runs": {tid: {"role": "engineer", "started": T.now_iso(self.now - timedelta(minutes=25))}}}
        plan = self.plan(state)
        self.assertEqual([r["mark"] for r in plan], ["done", "run", "next"])
        self.assertEqual((plan[0]["at"], plan[0]["title"]), ("10:00", "Заголовок 1"))
        self.assertEqual((plan[1]["who"], plan[1]["ticket"], plan[1]["since"]), ("Инженер", tid, "25 мин"))
        self.assertNotIn("ticket", plan[2])

    def test_acceptance_yesterday_is_named_so(self):
        self.plan_goal(1)
        self.step(1, 1, status="done", accepted_at=self.now - timedelta(days=1))
        self.assertEqual(self.plan()[0]["at"], "вчера")

    def test_movement_is_the_later_of_log_and_role_run(self):
        self.plan_goal()
        tid = self.step(1, status="in_progress")
        self.log(tid, "engineer", "заметка", self.now - timedelta(minutes=40))
        self.assertEqual((self.plan()[0]["idle"], self.plan()[0]["level"]), ("40 мин", "bad"))
        run = (self.now - timedelta(minutes=5), tid, "engineer", {})
        self.assertEqual((self.plan(runs=[run])[0]["idle"], self.plan(runs=[run])[0]["level"]), ("5 мин", ""), "запуск позже записи")
        other = (self.now - timedelta(minutes=1), "TK-999", "engineer", {})
        self.assertEqual(self.plan(runs=[other])[0]["idle"], "40 мин", "запуск чужого тикета не в счёт")

    def test_thresholds_15_warn_30_bad_and_boundaries(self):
        self.plan_goal()
        tid = self.step(1, status="in_progress")
        for minutes, level in [(14, ""), (15, "warn"), (29, "warn"), (30, "bad")]:
            self.log(tid, "engineer", f"заметка {minutes}", self.now - timedelta(minutes=minutes))
            self.assertEqual(self.plan()[0]["level"], level, minutes)

    def test_thresholds_follow_rpv_json(self):
        self.plan_goal()
        tid = self.step(1, status="in_progress")
        self.log(tid, "engineer", "заметка", self.now - timedelta(minutes=6))
        (Path(self.root.name) / ".claude" / "rpv.json").write_text('{"pulse_move_warn_min": 5, "pulse_move_bad_min": 6}', encoding="utf-8")
        self.assertEqual(self.plan()[0]["level"], "bad")
        (Path(self.root.name) / ".claude" / "rpv.json").write_text('{"pulse_move_warn_min": 0, "pulse_move_bad_min": "x"}', encoding="utf-8")
        self.assertEqual(F.move_limits(), (15, 30), "не целое ≥ 1 — умолчание")

    def test_waiting_for_owner_shows_wait_without_movement(self):
        self.plan_goal()
        tid = self.step(1, status="needs_owner")
        self.log(tid, "engineer", "[итог: ask-owner] Фьючерсы или спот? повод: необратимое", self.now - timedelta(minutes=45))
        row = self.plan(owner=[(tid, "ответить на вопрос")])[0]
        self.assertEqual((row["mark"], row["waiting"]), ("ask", "45 мин"))
        self.assertNotIn("idle", row)

    def test_stuck_when_a_reason_names_the_ticket(self):
        self.plan_goal()
        tid = self.step(1, status="in_progress")
        other = self.step(2, status="todo")
        reason = ("loop", f"{tid}: возвращён Судьёй 2 раз", T.now_iso(self.now))
        self.assertEqual([r["mark"] for r in self.plan(active=[reason])][:2], ["stuck", "run"])
        self.assertNotEqual(other, tid)

    def test_all_tickets_of_the_step_stopped_is_stuck(self):
        self.plan_goal()
        self.step(1, status="stopped")
        self.assertEqual(self.plan()[0]["mark"], "stuck")

    def test_goal_without_plan_lists_its_open_tickets(self):
        a = self.ticket(status="in_progress", title="Первый")
        self.ticket(status="done", title="Закрыт")
        self.ticket(ref="G-002", status="in_progress", title="Чужой")
        plan = self.plan()
        self.assertEqual([(r["n"], r["title"], r["ticket"], r["mark"]) for r in plan], [(0, "Первый", a, "run")])

    def test_step_and_steps_come_from_the_same_list(self):
        self.plan_goal(4)
        self.step(1, 4, status="done", accepted_at=self.now)
        self.step(2, 4, status="done", accepted_at=self.now)
        tid = self.step(3, 4, status="in_progress")
        out = F.facts({}, self.read(), [], [], self.now)
        self.assertEqual([r["mark"] for r in out["plan"]], ["done", "done", "run", "next"])
        self.assertEqual((out["goal"]["step"], out["goal"]["steps"]), (3, 4))
        self.assertEqual(F.plan_step(out["plan"]), (3, 4))
        self.assertEqual(out["plan"][2]["ticket"], tid)

    def test_all_steps_accepted_is_last_of_last(self):
        self.plan_goal(2)
        self.step(1, 2, status="done", accepted_at=self.now)
        self.step(2, 2, status="done", accepted_at=self.now)
        self.ticket(status="in_progress")  # открытый тикет цели — цель на карточке (после последней приёмки)
        goal = F.facts({}, self.read(), [], [], self.now)["goal"]
        self.assertEqual((goal["step"], goal["steps"]), (2, 2))

    def test_plan_without_tickets_yet_is_step_one_ahead(self):
        self.plan_goal(2)
        self.ticket(status="in_progress")  # тикет цели без отметки шага — для фокуса
        out = F.facts({}, self.read(), [], [], self.now)
        self.assertEqual(([r["mark"] for r in out["plan"]], out["goal"]["step"], out["goal"]["steps"]), (["next", "next"], 1, 2))

    def test_no_goal_no_plan(self):
        self.ticket(ref="", status="in_progress")
        self.assertNotIn("plan", F.facts({}, self.read(), [], [], self.now))


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
