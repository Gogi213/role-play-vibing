"""TK-105 (В-50): Пульс v2, читатель — шапка в три состояния по-человечески, числа переключателя, итоги дня, поля версии 2 и версия 1."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pulse_view as V  # noqa: E402

NOW = datetime(2026, 11, 3, 13, 0, tzinfo=timezone(timedelta(hours=4)))
RAW_CODES = ("step_uncovered", "dispatcher_dead", "merged_unreleased", "ci_red", "unserved")


def raw(state="ok", at=NOW, **extra):
    cells = [{"i": i, "n": 0, "c": "none"} for i in range(96)]
    return {"version": 2, "project": "p", "generated": at.isoformat(timespec="seconds"), "state": state, "phrase": "Всё идёт. От вас ничего.",
            "now": "", "pause": "", "cells": cells, **extra}


def view(name, **kw):
    return V.normalise(name, raw(**kw), NOW)


class HeaderTests(unittest.TestCase):
    def test_three_states_in_human_words(self):
        ok = view("rpv")
        owner = view("alpha", state="owner", ask={"ticket": "TK-485", "question": "брать фьючерсы или только спот?"})
        stuck = view("invoice", state="stuck", fix={"text": "диспетчер молчит 23 мин", "who": "перезапускает CEO"})
        self.assertEqual(V.header([ok]), {"kind": "ok", "title": "Всё идёт", "sub": "От вас ничего не нужно"})
        self.assertEqual(V.header([ok, owner]), {"kind": "owner", "title": "Нужен ваш ответ",
                                                 "sub": "alpha · TK-485: брать фьючерсы или только спот?"})
        self.assertEqual(V.header([ok, owner, stuck]), {"kind": "stuck", "title": "Застряло: invoice",
                                                        "sub": "диспетчер молчит 23 мин — перезапускает CEO"})

    def test_no_projects_is_not_all_clear(self):
        self.assertEqual(V.header([]), {"kind": "stuck", "title": V.NO_PROJECTS, "sub": ""})

    def test_no_extra_count_in_header_it_is_in_the_filter(self):
        views = [view(f"p{i}", state="stuck", fix={"text": "причина", "who": "разбирает CEO"}) for i in range(66)]
        head = V.header(views)
        self.assertNotIn("ещё", head["title"] + head["sub"])
        self.assertEqual(V.counts(views)["stuck"], 66)

    def test_silent_dispatcher_is_days_not_minutes(self):
        stale = V.normalise("rpv", raw(at=NOW - timedelta(minutes=23578)), NOW)
        head = V.header([stale])
        self.assertEqual(head["sub"], "диспетчер не отвечает 16 дней — перезапускает CEO")
        self.assertNotIn("23578", head["sub"] + stale["phrase"])

    def test_stale_for_2h10(self):
        stale = V.normalise("rpv", raw(at=NOW - timedelta(minutes=130)), NOW)
        self.assertIn("диспетчер не отвечает 2 ч 10 мин", V.header([stale])["sub"])

    def test_raw_reason_codes_never_reach_the_header(self):
        data = raw("stuck", reasons=[{"code": "step_uncovered", "detail": "G-001: нет шага", "since": "x"}],
                   phrase="Застряло: rpv — цель G-001 без шага.")
        text = " ".join(V.header([V.normalise("rpv", data, NOW)]).values()) + V.tg_text([V.normalise("rpv", data, NOW)])
        for code in RAW_CODES:
            self.assertNotIn(code, text)

    def test_unreadable_project_is_stuck_with_a_reason(self):
        head = V.header([V.normalise("rpv", None, NOW)])
        self.assertEqual((head["title"], head["sub"]), ("Застряло: rpv", "пульс не читается — разбирает CEO"))


class VersionOneTests(unittest.TestCase):
    """Файл версии 1 (без полей v2): карточка без них, шапка из фразы без «(и ещё N)»."""

    def test_stuck_phrase_tail_without_and_more(self):
        v = view("rpv", state="stuck", phrase="Застряло: rpv — тикет в очереди, слот свободен, запуска нет (лимит сессии) (и ещё 65).")
        self.assertEqual(v["fix"], {"text": "тикет в очереди, слот свободен, запуска нет (лимит сессии)", "who": "разбирает CEO"})
        self.assertEqual((v["goal"], v["job"], v["queue"], v["day"]), (None, None, [], {}))

    def test_owner_from_v1_owner_list(self):
        v = view("rpv", state="owner", owner=[{"ticket": "TK-9", "action": "ответить на вопрос"}], phrase="Нужны вы: rpv — ответить на вопрос по TK-9.")
        self.assertEqual(V.header([v])["sub"], "rpv · TK-9: ответить на вопрос")

    def test_owner_without_anything_still_has_a_line(self):
        v = view("rpv", state="owner", phrase="Нужны вы")
        self.assertTrue(V.header([v])["sub"].startswith("rpv · "))


class CountsAndTotalsTests(unittest.TestCase):
    def test_counts_add_up_to_projects(self):
        views = [view("a"), view("b"), view("c", state="owner"), view("d", state="stuck"), view("e", state="stuck")]
        self.assertEqual(V.counts(views), {"all": 5, "ok": 2, "owner": 1, "stuck": 2})
        self.assertEqual(V.counts([]), {"all": 0, "ok": 0, "owner": 0, "stuck": 0})

    def test_totals_sum_days_and_skip_v1(self):
        a = view("a", day={"accepted": 9, "returns": 1, "working": 2, "queued": 3})
        b = view("b", day={"accepted": 0, "returns": 0, "working": 0, "queued": 4})
        old = view("c")
        self.assertEqual(V.totals([a, b, old]), {"accepted": 9, "returns": 1, "working": 2, "queued": 7})
        self.assertEqual(V.totals([]), {"accepted": 0, "returns": 0, "working": 0, "queued": 0})

    def test_bad_day_values_are_dropped(self):
        v = view("a", day={"accepted": -1, "returns": True, "working": "3", "queued": 2})
        self.assertEqual(v["day"], {"queued": 2})


class FieldsTests(unittest.TestCase):
    def test_goal_step_only_in_range(self):
        self.assertEqual(view("a", goal={"id": "G-1", "title": "Цель", "step": 2, "steps": 5})["goal"], {"id": "G-1", "title": "Цель", "step": 2, "steps": 5})
        self.assertEqual(view("a", goal={"id": "G-1", "title": "Цель"})["goal"], {"id": "G-1", "title": "Цель"})
        self.assertEqual(view("a", goal={"id": "G-1", "title": "Цель", "step": 6, "steps": 5})["goal"], {"id": "G-1", "title": "Цель"})
        self.assertEqual(view("a", goal={"id": "G-1", "title": "Цель", "step": 0, "steps": 5})["goal"], {"id": "G-1", "title": "Цель"})
        self.assertIsNone(view("a", goal={"id": "G-1", "title": "  "})["goal"])
        self.assertIsNone(view("a", goal="цель")["goal"])

    def test_job_needs_a_ticket(self):
        job = {"who": "Инженер", "verb": "делает", "ticket": "TK-105", "title": "Пульс v2", "since": "25 мин"}
        self.assertEqual(view("a", job=job)["job"], job)
        self.assertIsNone(view("a", job={"who": "Инженер"})["job"])

    def test_queue_entries_need_ticket_and_title_and_done_today_is_dropped(self):
        v = view("a", done_today=[{"time": "02:41", "ticket": "TK-104", "title": "Выпуск"}],
                 queue=[{"ticket": "TK-106", "title": "Строка"}, {"title": "без тикета"}])
        self.assertNotIn("done_today", v)  # В-74: блок «Сделано сегодня» убран
        self.assertEqual(v["queue"], [{"ticket": "TK-106", "title": "Строка"}])

    def test_cell_runs_and_why_by_index(self):
        data = raw()
        data["cells"][5] = {"i": 5, "n": 5, "c": "work", "runs": [{"who": "Инженер", "verb": "делает", "ticket": "TK-1", "title": "A"}, {"who": "x"}, 7]}
        data["cells"][6] = {"i": 6, "n": 0, "c": "stuck", "why": "dispatcher_dead"}
        v = V.normalise("a", data, NOW)
        self.assertEqual(v["runs"][5], [{"who": "Инженер", "verb": "делает", "ticket": "TK-1", "title": "A"}])
        self.assertEqual((v["why"][6], v["runs"][6], len(v["runs"])), ("dispatcher_dead", [], V.CELLS))

    def test_stale_drops_job_and_ask_keeps_goal_and_day(self):
        job = {"who": "Инженер", "verb": "делает", "ticket": "TK-1", "title": "A", "since": "1 мин"}
        v = V.normalise("a", raw(at=NOW - timedelta(minutes=30), job=job, goal={"id": "G-1", "title": "Цель"}, day={"accepted": 2}), NOW)
        self.assertEqual((v["state"], v["job"], v["goal"]["id"], v["day"]), ("stuck", None, "G-1", {"accepted": 2}))


class StatusTests(unittest.TestCase):
    """TK-117 (В-57): поле status — таблица «Статус» раскрытой карточки."""

    ROWS = [{"ticket": "TK-1", "title": "Схема", "mark": "done", "where": "принят 10:00"},
            {"ticket": "TK-2", "title": "Загрузчик", "mark": "run", "where": "стоит 23 мин", "level": "warn"}]

    def test_rows_left_hold_and_accepted_rows_dropped(self):
        """В-79: принятая строка из файла старой версии и счётчик `more` на страницу не попадают."""
        v = view("a", status={"rows": self.ROWS, "more": 2, "left": "Загрузчик", "hold": "TK-2 стоит 23 мин"})["status"]
        self.assertEqual(v["rows"], [self.ROWS[1]])
        self.assertEqual((v["left"], v["hold"]), ("Загрузчик", "TK-2 стоит 23 мин"))
        self.assertNotIn("more", v)

    def test_no_field_or_junk_is_empty(self):
        empty = {"rows": [], "left": "", "hold": ""}
        for junk in (None, "мусор", [], {"rows": "x", "more": -1, "left": 5}):
            self.assertEqual(V.normalise("a", raw(**({} if junk is None else {"status": junk})), NOW)["status"], empty, junk)
        self.assertEqual(V.normalise("a", None, NOW)["status"], empty, "пульс не читается")

    def test_bad_rows_and_levels_are_dropped(self):
        rows = [{"ticket": "TK-1", "title": "A", "mark": "???"}, {"ticket": "TK-2", "title": " ", "mark": "run"}, "x", {"mark": "run"},
                {"ticket": "TK-3", "title": "B", "mark": "next", "where": "ждёт TK-2", "level": "красный"}]
        self.assertEqual(view("a", status={"rows": rows})["status"]["rows"], [{"ticket": "TK-3", "title": "B", "mark": "next", "where": "ждёт TK-2", "level": ""}])

    def test_stale_keeps_left_drops_now_and_hold(self):
        old = raw(at=NOW - timedelta(minutes=30), status={"rows": self.ROWS, "left": "Выпуск", "hold": "TK-2 стоит 23 мин"})
        v = V.normalise("a", old, NOW)["status"]
        self.assertEqual([(r["ticket"], r["where"], r["level"]) for r in v["rows"]], [("TK-2", "", "")])
        self.assertEqual((v["left"], v["hold"]), ("Выпуск", ""))


class HiddenGoalTests(unittest.TestCase):
    """В-77: ключ rpv.json `pulse_hide_goal` — имена проектов, у которых карточка не показывает цель (план и «Статус» остаются)."""

    GOAL = {"id": "G-1", "title": "Цель", "step": 1, "steps": 2}

    def load(self, setting, extra=False):
        """{имя: вид} проектов a (из pulse_projects) и, если extra, c (присланный на сервер); у всех в файле есть цель."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".claude").mkdir()
            paths = {}
            for name in ("a", "b", "c"):
                paths[name] = root / f"{name}.json"
                paths[name].write_text(json.dumps(raw(goal=self.GOAL, plan=[{"mark": "run", "n": 1, "title": "Шаг"}])), encoding="utf-8")
            body = {"pulse_projects": {"a": str(paths["a"]), "b": str(paths["b"])}, **({} if setting is None else {"pulse_hide_goal": setting})}
            (root / ".claude" / "rpv.json").write_text(json.dumps(body), encoding="utf-8")
            return {v["name"]: v for v in V.load_all(root, NOW, extra={"c": str(paths["c"])} if extra else None)}

    def test_listed_project_has_no_goal_but_keeps_plan_and_others_keep_goal(self):
        got = self.load(["a"])
        self.assertIsNone(got["a"]["goal"])
        self.assertEqual(got["a"]["plan"][0]["title"], "Шаг")
        self.assertEqual(got["b"]["goal"], self.GOAL)

    def test_sent_project_can_be_listed_too(self):
        got = self.load(["c", "a"], extra=True)
        self.assertEqual({n: v["goal"] is None for n, v in got.items()}, {"a": True, "b": False, "c": True})

    def test_boundary_inputs_hide_nothing_by_accident(self):
        """Ключа нет · пустой список · строка вместо списка · другой регистр · пустое имя · не строки — цель на месте у всех."""
        for setting in (None, [], "a", ["A"], ["", " "], [1, None, ["a"]], {"a": True}, True):
            got = self.load(setting)
            self.assertEqual([v["goal"] for v in got.values()], [self.GOAL, self.GOAL], repr(setting))

    def test_non_string_items_do_not_spoil_the_valid_ones(self):
        self.assertIsNone(self.load([1, "b", None])["b"]["goal"])

    def test_key_is_valid_in_rpv_config(self):
        import rpv_config as RC
        check, words = RC.KEYS["pulse_hide_goal"]
        self.assertTrue(check(["alpha"]) and check(["a", "b"]))
        self.assertFalse(any(check(v) for v in ([], "alpha", [""], [1], {"a": 1}, None, True)))
        self.assertIn("имён", words)


class AgoTests(unittest.TestCase):
    def test_boundaries(self):
        for seconds, want in [(-5, "меньше минуты"), (0, "меньше минуты"), (59, "меньше минуты"), (60, "1 мин"), (59 * 60, "59 мин"),
                              (60 * 60, "1 ч"), (61 * 60, "1 ч 1 мин"), (7800, "2 ч 10 мин"), (86400, "1 день"), (3 * 86400, "3 дня"),
                              (5 * 86400, "5 дней"), (11 * 86400, "11 дней"), (23578 * 60, "16 дней")]:
            self.assertEqual(V.ago(seconds), want, seconds)


if __name__ == "__main__":
    unittest.main()
