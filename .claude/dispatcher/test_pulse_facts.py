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

    def plan_goal(self, n=3, gid="G-001"):
        path = Path(self.root.name) / ".claude" / "goals" / f"{gid}.md"
        steps = "\n".join(f"{i}. engineer | Заголовок {i} | готово {i}" for i in range(1, n + 1))
        path.write_text(path.read_text(encoding="utf-8") + f"\n## План шагов\n{steps}\n", encoding="utf-8")

    def step(self, n, total=3, **kw):
        return self.ticket(desc=f"Шаг {n} из {total} цели G-001.", **kw)


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

    def plan(self, state=None, active=(), owner=(), runs=(), at=None):
        return F.plan_of("G-001", self.read(), state or {}, list(active), list(owner), list(runs), at or self.now)

    def test_marks_done_run_next_with_accept_time_and_role(self):
        self.plan_goal()
        self.step(1, status="done", accepted_at=self.now - timedelta(hours=2))
        tid = self.step(2, status="in_progress")
        state = {"active_runs": {tid: {"role": "engineer", "started": T.now_iso(self.now - timedelta(minutes=25))}}}
        plan = self.plan(state)
        self.assertEqual([r["mark"] for r in plan], ["done", "run", "next"])
        self.assertEqual((plan[0]["at"], plan[0]["title"]), ("10:00", "Заголовок 1"))
        self.assertEqual((plan[1]["who"], plan[1]["ticket"]), ("Инженер", tid))
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


class StatusTests(FactsBase):
    """TK-117 (В-57): блок «Статус» раскрытой карточки — таблица Задача | Что | Где сейчас, «Осталось», «Держит»."""

    def status(self, state=None, active=(), owner=(), runs=(), plan=None, ask=None, fix=None, gid="G-001", at=None):
        return F.status_of(gid, self.read(), state or {}, list(active), list(owner), list(runs), at or self.now, plan or [], ask, fix)

    def where(self, **kw):
        return [(r["ticket"], r["where"], r["mark"], r.get("level", "")) for r in self.status(**kw)["rows"]]

    def test_short_title_drops_service_prefix_and_cuts_by_word(self):
        self.assertEqual(F.short("Хвост TK-112: нестабильный test"), "Нестабильный test")
        self.assertEqual(F.short("TK-5 — выпуск"), "Выпуск")
        long = F.short("Правка настроек для проверки длинного заголовка тикета, который не влезает " * 2)
        self.assertLessEqual(len(long), F.SHORT)
        self.assertTrue(long.endswith("…") and not long.endswith(" …"), long)
        self.assertEqual(F.short("Пульс: таблица", 40), "Пульс: таблица", "тематический префикс — не служебный")
        self.assertEqual(F.short("x" * 60, 10), "X" + "x" * 8 + "…", "слова нет — режем по знакам")

    def test_title_limits_are_120_for_what_and_60_for_left_steps(self):
        """TK-133 (В-66): заголовок в «Что» не резали до 40 знаков — таблица переносит строкой."""
        self.assertEqual((F.SHORT, F.LEFT_SHORT), (120, 60))
        mid = " ".join(["слово"] * 16)  # 95 знаков
        self.assertEqual(F.short(mid), F.short(mid)[:1].upper() + mid[1:], "95 знаков — без обрезки")
        self.assertFalse(F.short(mid).endswith("…"))
        cut = F.short(" ".join(["слово"] * 30))  # 179 знаков
        self.assertTrue(len(cut) <= 120 and cut.endswith("…") and len(cut) > 100, len(cut))
        steps = [{"n": 1, "mark": "run", "title": " ".join(["шаг"] * 14)}, {"n": 2, "mark": "next", "title": " ".join(["шаг"] * 30)}]  # 55 и 119 знаков
        left = self.status(plan=steps)["left"].split(", ")
        self.assertEqual(len(left[0]), 55, "55 знаков в «Осталось» не режем")
        self.assertTrue(len(left[1]) <= 60 and left[1].endswith("…") and len(left[1]) > 40, left[1])

    def test_rows_are_tickets_of_the_goal_only_and_titles_are_short(self):
        a = self.ticket(status="in_progress", title="Хвост TK-081: " + "слово " * 12)
        self.ticket(ref="G-002", status="in_progress", title="Чужой")
        self.ticket(status="stopped", title="Остановлен")
        rows = self.status()["rows"]
        self.assertEqual([r["ticket"] for r in rows], [a])
        self.assertTrue(rows[0]["title"].startswith("Слово слово") and len(rows[0]["title"]) <= F.SHORT)

    def test_step_ticket_without_goal_ref_line_still_belongs_to_the_goal(self):
        tid = self.ticket(ref="", status="in_progress", desc="Шаг 1 из 2 цели G-001.")
        self.assertEqual([r["ticket"] for r in self.status()["rows"]], [tid])

    def test_accepted_is_grey_with_time_or_day(self):
        today = self.ticket(status="done", accepted_at=self.now - timedelta(hours=2))
        old = self.ticket(status="done", accepted_at=self.now - timedelta(days=1))
        self.assertEqual(self.where(), [(today, "принят 10:00", "done", ""), (old, "принят вчера", "done", "")])

    def test_more_than_three_accepted_keeps_last_three_and_counts_the_rest(self):
        ids = [self.ticket(status="done", accepted_at=self.now) for _ in range(5)]
        got = self.status()
        self.assertEqual([r["ticket"] for r in got["rows"]], ids[-3:])
        self.assertEqual(got["more"], 2)
        self.ticket(status="done", accepted_at=self.now)
        self.assertEqual(self.status()["more"], 3)

    def test_three_accepted_are_all_shown(self):
        for _ in range(3):
            self.ticket(status="done", accepted_at=self.now)
        got = self.status()
        self.assertEqual((len(got["rows"]), got["more"]), (3, 0))

    def test_who_holds_the_ticket(self):
        eng = self.ticket(status="in_progress")
        judge = self.ticket(status="in_review", owner="judge")
        todo = self.ticket(status="todo")
        state = {"active_runs": {eng: {"role": "engineer", "started": T.now_iso(self.now)}}}
        self.assertEqual(self.where(state=state), [(eng, "Инженер делает", "run", ""), (judge, "у Судьи", "run", ""), (todo, "в очереди", "next", "")])

    def test_engineer_after_judge_return(self):
        tid = self.ticket(status="in_progress")
        self.log(tid, "judge", "[итог: return] PR #1@abcdef0 — не хватает теста", self.now)
        self.assertEqual(self.where()[0][:2], (tid, "Инженер правит после возврата"))

    def test_waits_on_ticket_from_wait_for_and_other_forms(self):
        base = "Ссылка на задачу владельца: G-001\n"
        at = (self.now + timedelta(hours=5)).astimezone()
        forms = (("ticket:TK-005", "ждёт TK-005"), ("merged:o/r#5", "ждёт слияния"), ("ci:o/r#5", "ждёт CI"), ("ci-run:o/r#5", "ждёт CI"),
                 ("file:/tmp/x", "ждёт файл x"), (r"file:C:\data\out\calc.json", "ждёт файл calc.json"),
                 ("host:sofia:unit:rpv-pulse", "ждёт процесс rpv-pulse на sofia"), ("host:sofia:/opt/rpv/out.json", "ждёт итог расчёта на sofia"),
                 ("job:sofia:calc-7", "ждёт задание на sofia"), (f"at:{T.now_iso(at)}", f"ждёт до {at:%d.%m %H:%M}"), ("", "ждёт, не сказано чего"))
        ids = [T.read_ticket(T.create_ticket(self.tickets_dir, "engineer", f"ждёт {i}", "judge", base, spec, self.now, status="waiting")).id
               for i, (spec, _) in enumerate(forms)]
        self.assertEqual(self.where(at=self.now + timedelta(hours=2)), [(tid, text, "next", "") for tid, (_, text) in zip(ids, forms)],
                         "ожидание не «стоит»; «ждёт результата» нигде нет")

    def test_waits_on_unknown_form_is_not_said_as_a_result(self):
        tid = self.ticket(status="waiting")
        path = self.tickets_dir / f"{tid}.md"
        path.write_text(path.read_text(encoding="utf-8").replace("wait_for: ", "wait_for: что-то странное", 1), encoding="utf-8")
        self.assertEqual(self.where(), [(tid, "ждёт, не сказано чего", "next", "")])

    def test_queued_step_waits_on_nearest_unaccepted_step_before_it(self):
        self.plan_goal()
        first = self.step(1, status="done", accepted_at=self.now)
        second = self.step(2, status="in_progress")
        third = self.step(3, status="todo")
        state = {"active_runs": {second: {"role": "engineer", "started": T.now_iso(self.now)}}}
        plan = F.plan_of("G-001", self.read(), state, [], [], [], self.now)
        self.assertEqual(self.where(state=state, plan=plan), [(first, "принят 12:00", "done", ""), (second, "Инженер делает", "run", ""),
                                                              (third, f"ждёт {second}", "next", "")])

    def test_first_unaccepted_step_in_queue_does_not_wait(self):
        self.plan_goal()
        self.step(1, status="done", accepted_at=self.now)
        second, third = self.step(2, status="todo"), self.step(3, status="todo")
        plan = F.plan_of("G-001", self.read(), {}, [], [], [], self.now)
        self.assertEqual([r[:2] for r in self.where(plan=plan)][1:], [(second, "в очереди"), (third, f"ждёт {second}")])

    def test_blocked_is_not_with_the_judge(self):
        blocked, review = self.ticket(status="blocked"), self.ticket(status="in_review", owner="judge")
        self.log(blocked, "engineer", "заметка", self.now - timedelta(minutes=40))
        self.assertEqual(self.where(), [(blocked, "ждёт CEO", "stuck", "bad"), (review, "у Судьи", "run", "")])

    def test_asks_owner(self):
        tid = self.ticket(status="needs_owner")
        self.assertEqual(self.where(owner=[(tid, "ответить")]), [(tid, "ждёт вашего ответа", "ask", "")])
        self.assertEqual(self.where()[0][1], "ждёт вашего ответа", "needs_owner и без списка владельца")

    def test_stands_after_thresholds_and_queue_does_not_stand(self):
        tid = self.ticket(status="in_progress")
        queued = self.ticket(status="todo")
        for minutes, level in [(14, ""), (15, "warn"), (29, "warn"), (30, "bad")]:
            self.log(tid, "engineer", f"заметка {minutes}", self.now - timedelta(minutes=minutes))
            row = self.where()[0]
            self.assertEqual((row[1], row[3]), (f"стоит {minutes} мин" if level else "Инженер делает", level), minutes)
        self.assertEqual(self.where()[1], (queued, "в очереди", "next", ""))

    def test_named_by_a_reason_is_stuck(self):
        tid = self.ticket(status="in_progress")
        reason = ("loop", f"{tid}: возвращён Судьёй 2 раз", T.now_iso(self.now))
        self.assertEqual(self.where(active=[reason]), [(tid, "застряло", "stuck", "bad")], "движения не найдено — без числа")
        self.log(tid, "engineer", "заметка", self.now - timedelta(minutes=3))
        self.assertEqual(self.where(active=[reason]), [(tid, "стоит 3 мин", "stuck", "bad")])
        self.assertEqual(self.where(active=[("loop", "TK-0010: чужой", T.now_iso(self.now))])[0][2], "run", "TK-001 не часть TK-0010")

    def test_without_goal_open_tickets_of_the_project(self):
        a = self.ticket(ref="", status="in_progress")
        b = self.ticket(ref="G-002", status="todo")
        self.ticket(ref="", status="done", accepted_at=self.now)
        self.assertEqual([r["ticket"] for r in self.status(gid="")["rows"]], [a, b])

    def test_left_is_open_steps_of_the_plan_else_ticket_numbers(self):
        steps = [{"mark": m, "n": i, "title": t} for i, (m, t) in enumerate([("done", "Готов"), ("run", "Делаем"), ("next", "Потом")], 1)]
        self.ticket(status="in_progress")
        self.assertEqual(self.status(plan=steps)["left"], "Делаем, Потом")
        a, b = self.ticket(status="todo"), self.ticket(status="todo")
        self.assertEqual(self.status()["left"].split(", ")[-2:], [a, b])
        many = [{"mark": "next", "n": i, "title": f"Шаг {i}"} for i in range(1, 8)]
        self.assertEqual(self.status(plan=many)["left"], "Шаг 1, Шаг 2, Шаг 3, Шаг 4 и ещё 3")

    def test_hold_question_then_stuck_then_longest_without_movement(self):
        a, b = self.ticket(status="in_progress"), self.ticket(status="in_progress")
        self.log(a, "engineer", "заметка", self.now - timedelta(minutes=20))
        self.log(b, "engineer", "заметка", self.now - timedelta(minutes=40))
        ask, fix = {"ticket": "TK-077", "question": "?"}, {"text": "диспетчер молчит 23 мин", "who": "x"}
        self.assertEqual(self.status(ask=ask, fix=fix)["hold"], "ваш ответ по TK-077")
        self.assertEqual(self.status(fix=fix)["hold"], "диспетчер молчит 23 мин")
        self.assertEqual(self.status()["hold"], f"{b} стоит 40 мин")

    def test_no_hold_while_everything_moves(self):
        self.ticket(status="in_progress")
        self.assertEqual(self.status()["hold"], "")

    def test_nothing_to_show_is_none(self):
        self.assertIsNone(self.status())
        self.ticket(ref="G-002", status="in_progress")
        self.assertIsNone(self.status())

    def test_facts_put_status_next_to_plan_without_changing_plan(self):
        tid = self.ticket(status="in_progress", desc="Шаг 1 из 2 цели G-001.")
        out = F.facts({}, self.read(), [], [], self.now)
        self.assertEqual([r["ticket"] for r in out["status"]["rows"]], [tid])
        self.assertEqual(out["plan"][0]["ticket"], tid)

    def test_facts_without_goal_use_open_tickets(self):
        tid = self.ticket(ref="", status="in_progress")
        out = F.facts({}, self.read(), [], [], self.now)
        self.assertNotIn("plan", out)
        self.assertEqual([r["ticket"] for r in out["status"]["rows"]], [tid])


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
