"""Пульс v2 (TK-105, В-50/В-51): страница на фикстурах pulse.json трёх состояний — шапка, переключатель, итоги дня, карточка, часы над
клетками, подсказки; сервер по ключу в пути. Фикстуры — настоящий вывод `pulse.build` (States из dispatcher/test_pulse_tg.py);
поля версии 2, которые собирают тикеты, дописаны в данные руками."""
import html as H
import io
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "dispatcher"))
import pulse_server as S  # noqa: E402
import pulse_view as V  # noqa: E402
import test_pulse_tg as TG  # noqa: E402


def cells_of(html, kind="w", card=0):
    """Клетки карточки номер `card`: [(классы, подсказка)] ширины `kind` (w — 8 ч, n — 4 ч)."""
    block = re.findall(rf'<div class="tl {kind}">.*?<div class="cells">(.*?)</div></div>', html, re.S)[card]
    return [(m.group(1).split(), json.loads(H.unescape(m.group(2)))) for m in re.finditer(r'<i class="([^"]*)" data-k="-?\d+" data-info="([^"]*)">', block)]


def classes(html, kind="w", card=0):
    return [c[0] if c else "" for c, _ in cells_of(html, kind, card)]


def hours_of(html, kind="w", card=0):
    """[(с какой колонки, сколько колонок, метка)] часов над клетками."""
    block = re.findall(rf'<div class="tl {kind}"><div class="hours">(.*?)</div>', html, re.S)[card]
    return [(int(m.group(1)), int(m.group(2)), m.group(3)) for m in re.finditer(r'<span style="grid-column:(\d+) / span (\d+)">(\w*)</span>', block)]


class PageStates(TG.States):
    def render(self, raw, name="rpv", now=None):
        now = now or self.at
        return S.body_html([V.normalise(name, raw, now)], now)

    def test_ok_state_header_cells_job(self):
        html = self.render(dict(self.ok_data(), job={"role": "judge", "who": "Судья", "verb": "проверяет", "ticket": "TK-7", "title": "Правка", "since": "25 мин"}))
        self.assertIn('<div class="logo"><span class="mark">RPV</span>Role Play Vibing <b>· Пульс</b></div>', html)
        self.assertNotIn(V.OK_TITLE, html)  # фразы-итога в шапке нет (В-74); она остаётся в Telegram
        wide, narrow = classes(html, "w"), classes(html, "n")
        self.assertEqual((len(wide), len(narrow)), (S.WINDOW, S.NARROW))
        self.assertEqual(wide[-5:], ["", "a", "b", "c", "a"])  # клетки 48…52: запусков 0, 1, 3, 5, идёт
        self.assertIn("now", cells_of(html, "w")[-1][0])
        self.assertIn("now", cells_of(html, "n")[-1][0])
        self.assertIn('<div class="now-line ">Судья проверяет Правка<span class="who">TK-7 · 25 мин</span></div>', html)
        self.assertIn("обновлено 13:00", html)

    def test_owner_state_is_yellow_only_for_owner_cells(self):
        data = dict(self.owner_data(), ask={"ticket": "TK-9", "question": "брать фьючерсы или только спот?"})
        html = self.render(data)
        self.assertIn('data-n="rpv" data-s="owner"', html)
        self.assertNotIn(V.OWNER_TITLE, html)  # фразы-итога в шапке нет (В-74)
        self.assertEqual(classes(html).count("o"), 8)
        self.assertEqual(len(classes(html)), 32)
        self.assertIn('<div class="now-line owner">брать фьючерсы или только спот?<span class="who">TK-9</span></div>', html)

    def test_stuck_state_from_file(self):
        html = self.render(dict(self.stuck_data(), fix={"text": "диспетчер молчит 23 мин", "who": "перезапускает CEO"}))
        self.assertIn('data-n="rpv" data-s="stuck"', html)
        self.assertEqual(classes(html)[-1], "s")
        self.assertIn('<div class="now-line stuck">диспетчер молчит 23 мин<span class="who">перезапускает CEO</span></div>', html)

    def test_stuck_state_dead_dispatcher_from_stale_generated(self):
        html = self.render(self.ok_data(), now=self.at + timedelta(minutes=38))  # файл с 13:00, сейчас 13:38 — клетки 53 и 54 никто не писал
        self.assertIn('data-n="rpv" data-s="stuck"', html)
        self.assertRegex(html, r'диспетчер не отвечает[^<]*<span class="who">перезапускает CEO</span>')  # длительность видна в строке «сейчас»
        self.assertNotIn("38 мин.", html)
        wide = classes(html)
        self.assertEqual(wide[-2:], ["s", "s"])
        self.assertEqual(wide[-5:-2], ["b", "c", "a"])  # записанное до молчания остаётся
        self.assertIn("now", cells_of(html)[-1][0])

    def test_no_raw_codes_and_no_minutes_in_thousands(self):
        data = dict(self.stuck_data(), generated=(self.at - timedelta(days=16, hours=7)).isoformat())
        html = self.render(data, now=self.at)
        self.assertIn("диспетчер не отвечает 16 дней", html)
        self.assertNotRegex(html, r"\d{4,} мин")
        for code in ("step_uncovered", "dispatcher_dead", "unserved", "merged_unreleased", "ci_red"):
            self.assertNotIn(code, html)
        self.assertNotIn("и ещё", self.render(self.ok_data()).split('class="legend"')[0].split("<details")[0])

    def test_old_cells_survive_stale_file(self):
        """Регресс возврата Судьи: клетки в формате шага 1 (`c`, `n`) читаются и у старого файла — работа не стирается в серое."""
        data = self.ok_data()
        data["cells"] = [{"i": i, "n": 1, "c": "work"} for i in range(96)]
        view = V.normalise("rpv", data, self.at + timedelta(hours=1))
        self.assertEqual(view["cells"][10], ("work", 1))
        self.assertIsNotNone(view["age_s"])
        self.assertEqual(view["cells"][self.cur + 1], ("stuck", 0))

    def test_stale_file_from_yesterday_is_all_stuck_up_to_now(self):
        view = V.normalise("rpv", self.ok_data(), self.at + timedelta(days=1, minutes=-30))  # 12:30 следующих суток
        self.assertEqual(view["state"], "stuck")
        self.assertEqual({c[0] for c in view["cells"][:view["idx"] + 1]}, {"stuck"})
        self.assertEqual({c[0] for c in view["cells"][view["idx"] + 1:]}, {"none"})

    def test_stale_threshold_is_the_one_of_pulse_read(self):
        """Один порог с pulse.read (heartbeat.STALE_S = 10 мин): 5 мин — ещё идёт, 11 мин — не отвечает."""
        self.assertEqual(V.STALE_S, V.HB.STALE_S)
        self.assertEqual(self.view("rpv", self.ok_data(), self.at + timedelta(minutes=5))["state"], "ok")
        late = self.view("rpv", self.ok_data(), self.at + timedelta(minutes=11))
        self.assertEqual(late["state"], "stuck")
        self.assertEqual(late["fix"]["text"], "диспетчер не отвечает 11 мин")

    def test_stale_view_does_not_show_old_job(self):
        data = dict(self.ok_data(), job={"role": "judge", "who": "Судья", "verb": "проверяет", "ticket": "TK-7", "title": "Правка", "since": "1 ч"})
        self.assertIn("Судья проверяет Правка", self.render(data))
        html = self.render(data, now=self.at + timedelta(minutes=11))
        self.assertNotIn("Судья проверяет Правка", html)
        self.assertIn("диспетчер не отвечает", html)

    def test_cell_and_clock_follow_the_zone_of_the_file_not_of_the_server(self):
        """Файл проекта в +04:00 (13:00 — клетка 52), сервер в UTC (09:03): клетка и часы — проектные."""
        project_zone = self.at.tzinfo
        self.assertEqual(self.at.utcoffset(), timedelta(hours=4))
        server_now = (self.at + timedelta(minutes=3)).astimezone(timezone.utc)
        view = V.normalise("rpv", self.ok_data(), server_now)
        self.assertEqual((view["idx"], view["zone"]), (self.cur, project_zone))
        html = S.body_html([view], server_now)
        self.assertIn("now", cells_of(html)[-1][0])
        self.assertIn("обновлено 13:03", html)
        self.assertNotIn("09:03", html)

    def test_stale_day_is_judged_in_the_zone_of_the_file(self):
        """Файл 23:50 (+04:00), сервер в UTC 20:20 того же UTC-дня, у проекта уже 00:20 следующих суток: клетки прошлых суток стёрты."""
        data = self.ok_data()
        data["generated"] = self.at.replace(hour=23, minute=50).isoformat()
        server_now = (self.at + timedelta(days=1)).replace(hour=0, minute=20).astimezone(timezone.utc)
        self.assertEqual(server_now.date(), (self.at.replace(hour=23, minute=50)).astimezone(timezone.utc).date())
        view = V.normalise("rpv", data, server_now)
        self.assertEqual({c[0] for c in view["cells"][:view["idx"] + 1]}, {"stuck"})
        self.assertEqual(view["idx"], 1)

    def test_no_projects_is_not_a_green_phrase(self):
        self.assertEqual(V.header([])["title"], V.NO_PROJECTS)
        html = S.body_html([], self.at)
        self.assertIn(f'<p class="state stuck">{V.NO_PROJECTS}</p>', html)
        self.assertNotIn(V.OK_TITLE, html)
        self.assertNotIn('class="seg"', html)

    def test_broken_cells_and_garbage_do_not_crash(self):
        for cells in ({"a": 1}, "x", 5, None):
            with self.subTest(cells=cells):
                self.assertIn("пульс не читается", self.render(dict(self.ok_data(), cells=cells)))
        self.assertIn("пульс не читается", self.render(dict(self.ok_data(), generated=12345)))
        junk = dict(self.ok_data(), cells=[1, "a", None, {"i": "x"}, {"i": 5, "n": "z", "c": []}, {"i": 7, "n": 2, "c": "work"}])
        self.assertEqual(self.view("rpv", junk)["cells"][7], ("work", 2))
        self.assertNotIn("пульс не читается", self.render(junk))

    def test_no_work_is_not_stuck(self):
        html = self.render(self.pulse())
        self.assertEqual({c for c in classes(html)} - {"now"}, {""})
        self.assertNotIn("state stuck", html)

    def test_unreadable_pulse_is_stuck(self):
        for raw in (None, {}, {"phrase": "x", "ts": self.at.isoformat(), "cells": []}):  # нет файла, пусто, чужой формат
            with self.subTest(raw=raw):
                self.assertIn('data-n="rpv" data-s="stuck"', self.render(raw))

    def test_header_orders_stuck_before_owner(self):
        ok, owner, stuck = self.ok_data(), self.owner_data(), self.stuck_data()
        views = [self.view("a", owner), self.view("b", stuck), self.view("c", ok)]
        self.assertEqual(V.header(views)["title"], "Застряло: b")
        self.assertEqual(V.header(views[2:])["title"], V.OK_TITLE)

    def test_names_and_texts_are_escaped(self):
        data = dict(self.ok_data(), goal={"id": "G-1", "title": "<b>цель</b>"}, queue=[{"ticket": "TK-1", "title": "<i>x</i>"}])
        html = self.render(data, name="<script>")
        self.assertNotIn("<script>", html)
        self.assertNotIn("<b>цель</b>", html)
        self.assertNotIn("<i>x</i>", html)

    def test_filter_counts_default_and_state_of_cards(self):
        ok, owner, stuck = self.ok_data(), self.owner_data(), self.stuck_data()  # по порядку: каждое следующее состояние не лечит прежнее
        views = [self.view("a", owner), self.view("b", stuck), self.view("c", ok), self.view("d", ok)]
        html = S.body_html(views, self.at)
        seg = re.search(r'<div class="seg" id="seg">(.*?)</div>', html).group(1)
        buttons = re.findall(r'<button data-s="(\w+)"( class=on)?>([^<]*?) <sup>(\d+)</sup></button>', seg)
        self.assertEqual([(b[0], b[2], int(b[3])) for b in buttons], [("all", "Все", 4), ("ok", "Идёт", 2), ("owner", "Нужен ответ", 1), ("stuck", "Застряло", 1)])
        self.assertEqual([b[0] for b in buttons if b[1]], ["all"])  # по умолчанию Все
        self.assertEqual(re.findall(r'<details class="proj" data-n="\w" data-s="(\w+)"', html), ["owner", "stuck", "ok", "ok"])
        self.assertEqual(sum(int(b[3]) for b in buttons[1:]), int(buttons[0][3]))

    def test_day_totals(self):
        a = dict(self.ok_data(), day={"accepted": 3, "returns": 1, "working": 1, "queued": 2})
        b = dict(self.ok_data(), day={"accepted": 4, "returns": 0, "working": 0, "queued": 0})
        old = {k: v for k, v in self.ok_data().items() if k != "day"}  # версия 1: без итогов
        html = S.body_html([self.view("a", a), self.view("b", b), self.view("old", old)], self.at)
        self.assertIn('<div class="sum"><div><b>7</b>принято сегодня</div><div><b>1</b>возврат</div><div><b>1</b>в работе</div><div><b>2</b>в очереди</div></div>', html)
        zero = S.body_html([self.view("a", old)], self.at)
        self.assertIn("<b>0</b>принято сегодня", zero)
        self.assertIn("<b>0</b>возвратов", zero)
        self.assertIn("<b>5</b>возвратов", S.body_html([self.view("a", dict(old, day={"returns": 5}))], self.at))

    def test_card_goal_and_bar_only_with_plan(self):
        planned = self.render(dict(self.ok_data(), goal={"id": "G-1", "title": "Пульс v2", "step": 2, "steps": 4}))
        self.assertIn('<div class="goal">Пульс v2<br>шаг 2 из 4</div><div class="bar"><i style="width:50%"></i></div>', planned)
        free = self.render(dict(self.ok_data(), goal={"id": "G-2", "title": "Без плана"}))
        self.assertIn('<div class="goal">Без плана</div>', free)
        self.assertNotIn("шаг ", free)
        self.assertNotIn('class="bar"', free)
        self.assertNotIn('class="goal"', self.render(self.ok_data()))
        broken = self.render(dict(self.ok_data(), goal={"id": "G-3", "title": "Кривой", "step": 5, "steps": 4}))
        self.assertNotIn('class="bar"', broken)

    def test_job_line_without_known_role_has_no_verb(self):
        job = {"role": "x", "who": "", "verb": "делает", "ticket": "TK-5", "title": "Правка", "since": ""}
        html = self.render(dict(self.ok_data(), job=job))
        self.assertIn('<div class="now-line ">Правка<span class="who">TK-5</span></div>', html)
        self.assertIn("Сейчас тихо", self.render(dict(self.ok_data(), job=None, now="")))

    def test_queue_in_card_body_and_no_done_today(self):
        data = dict(self.ok_data(), done_today=[{"time": "02:41", "ticket": "TK-104", "title": "Выпуск 2.0.20"}],
                    queue=[{"ticket": "TK-106", "title": "Строка «за сегодня»"}])
        html = self.render(data)
        self.assertNotIn("Сделано сегодня", html)  # блок убран (В-74), даже если старый pulse.json его присылает
        self.assertNotIn("Выпуск 2.0.20", html)
        self.assertIn("<h4>Дальше по очереди</h4><ul><li><span>Строка «за сегодня» <span class=\"tag\">TK-106</span></span></li></ul>", html)
        empty = self.render(self.ok_data())
        self.assertIn('<span class="mute">пусто</span>', empty)

    def test_answer_button_only_with_http_link(self):
        owner = self.owner_data()
        for link, shown in (("https://t.example/TK-9", True), ("javascript:alert(1)", False), ("", False), ("http://x/a\"b", True)):
            with self.subTest(link=link):
                html = self.render(dict(owner, ask={"ticket": "TK-9", "question": "вопрос", "link": link}))
                self.assertEqual("Ответить" in html, shown)
                self.assertNotIn("javascript:", html)
                self.assertNotIn('a"b', html)
        self.assertIn('<a class="act" href="https://t.example/TK-9">Ответить</a>', self.render(dict(owner, ask={"ticket": "TK-9", "question": "q", "link": "https://t.example/TK-9"})))
        self.assertNotIn("Ответить", self.render(owner))  # писателя ссылки в данных нет — кнопки нет

    def test_hours_sit_exactly_over_their_cells(self):
        """Метка часа занимает ровно клетки своего часа: у каждой метки первая клетка подсказки начинается на «чч:00» или это край окна."""
        html = self.render(self.ok_data())
        for kind, count in (("w", S.WINDOW), ("n", S.NARROW)):
            with self.subTest(kind=kind):
                spans, cells = hours_of(html, kind), cells_of(html, kind)
                self.assertEqual(sum(s[1] for s in spans), count)
                self.assertEqual([s[0] for s in spans], [1 + sum(t[1] for t in spans[:i]) for i in range(len(spans))])
                for col, span, label in spans:
                    if label:
                        for k in range(col - 1, col - 1 + span):
                            self.assertTrue(cells[k][1]["t"].startswith(f"{label}:"), (label, cells[k][1]["t"]))
        wide = [s[2] for s in hours_of(html, "w")]
        self.assertEqual(wide, ["05", "06", "07", "08", "09", "10", "11", "12", ""])  # 13:00 — одна клетка нового часа: метки нет
        self.assertEqual([s[2] for s in hours_of(html, "n")], ["09", "10", "11", "12", ""])

    def test_cell_tooltips(self):
        html = self.render(dict(self.ok_data(), job={"role": "judge", "who": "Судья", "verb": "проверяет", "ticket": "TK-7", "title": "Правка", "since": "1 ч"}))
        info = [i for _, i in cells_of(html)]
        quiet, one, three, five, now = info[-5], info[-4], info[-3], info[-2], info[-1]
        self.assertEqual((quiet["t"], quiet["st"][0], quiet["l"]), ("12:00–12:15", "Тихо", ["запусков не было"]))
        self.assertEqual((one["st"], one["l"]), (["1 запуск", "var(--c2)"], ["Инженер · TK-1"]))
        self.assertEqual(three["st"][0], "3 запуска")
        self.assertEqual(five["st"][0], "5 запусков")
        self.assertEqual((len(five["l"]), five["l"][-1]), (S.TIP_RUNS + 1, "и ещё 2"))
        self.assertEqual(now["t"], "13:00–13:15 · сейчас")

    def test_tooltip_of_owner_and_stuck_cells(self):
        html = self.render(dict(self.owner_data(), ask={"ticket": "TK-9", "question": "вопрос?"}))
        owner = [i for c, i in cells_of(html) if c and c[0] == "o"][0]
        self.assertEqual((owner["st"], owner["l"]), (["Ждали ответа", "var(--owner)"], ["TK-9: вопрос?"]))
        html = self.render(dict(self.stuck_data(), fix={"text": "диспетчер молчит 23 мин", "who": "перезапускает CEO"}))
        stuck = cells_of(html)[-1][1]
        self.assertEqual((stuck["st"], stuck["l"]), (["Застряло", "var(--stuck)"], ["диспетчер молчит 23 мин", "перезапускает CEO"]))

    def test_tooltip_runs_use_role_and_title(self):
        view = self.view("rpv", self.ok_data())
        view["runs"][51] = [{"who": "Судья", "verb": "проверяет", "ticket": "TK-4", "title": "Выпуск", "since": ""}, {"who": "", "verb": "делает", "ticket": "TK-5", "title": ""}]
        info = S.cell_info(view, 51, 52)
        self.assertEqual(info["l"], ["Судья · Выпуск (TK-4)", "TK-5", "и ещё 3"])  # в клетке 5 запусков, показано 2

    def test_column_key_is_the_time_slot(self):
        html = self.render(self.ok_data())
        self.assertEqual(re.findall(r'data-k="(\d+)"', html.split('<div class="tl n">')[0])[-1], str(self.cur))
        self.assertEqual(re.findall(r'data-k="(-?\d+)"', html.split('<div class="tl n">')[1])[0], str(self.cur - S.NARROW + 1))


def row(mark, n=0, title="Заголовок", **kw):
    return dict({"mark": mark, "n": n, "title": title}, **kw)


class PlanBlock(PageStates):
    """TK-108 (В-53, правка владельца): в свёрнутой карточке — «шаг N из M» и метка-таймер перед строкой «что сейчас»; плана в
    карточке нет (раскрытая — блок «Статус», TK-117). Страница только читает `plan` из pulse.json."""

    def pill(self, html):
        found = re.search(r'<div class="now-line[^"]*">(?:<span class="pill ?([^"]*)"[^>]*>(.*?)</span>)?', html)
        return (found.group(1), found.group(2)) if found and found.group(2) else None

    def test_collapsed_card_has_timer_before_the_now_line(self):
        plan = [row("done", 1), row("run", 2, who="Инженер", ticket="TK-2", idle="3 мин", level=""), row("next", 3)]
        html = self.render(dict(self.ok_data(), plan=plan))
        self.assertEqual(self.pill(html), ("", "3 мин назад"))
        head = html.split("</summary>")[0]
        self.assertLess(head.index('class="pill'), head.index("Судья проверяет"), "метка слева от строки")
        stuck = self.render(dict(self.ok_data(), plan=[row("stuck", 1, idle="23 мин", level="warn")]))
        self.assertEqual(self.pill(stuck), ("warn", "стоит 23 мин"))
        owner = self.render(dict(self.ok_data(), plan=[row("ask", 1, waiting="5 мин", ticket="TK-3")]))
        self.assertEqual(self.pill(owner), ("", "ждёт 5 мин"))

    def test_now_line_does_not_repeat_the_number_of_the_timer(self):
        data = dict(self.ok_data(), state="stuck", fix={"text": "диспетчер молчит 23 мин", "who": "перезапускает CEO"},
                    plan=[row("stuck", 1, idle="23 мин", level="warn")])
        line = self.render(data).split("</summary>")[0].split('<div class="now-line')[1]
        self.assertIn("стоит 23 мин", line)
        self.assertEqual(line.count("23"), 1, "число — только в метке")
        self.assertIn("диспетчер молчит<", line)
        data = dict(self.ok_data(), job={"who": "Инженер", "verb": "делает", "ticket": "TK-2", "title": "Правка", "since": "25 мин"},
                    plan=[row("run", 1, ticket="TK-2", idle="25 мин", level="warn")])
        line = self.render(data).split("</summary>")[0].split('<div class="now-line')[1]
        self.assertEqual(line.count("25 мин"), 1)
        data["plan"] = [row("run", 1, ticket="TK-2", idle="3 мин", level="")]
        self.assertIn("TK-2 · 25 мин", self.render(data), "разные числа — обе остаются")

    def test_no_plan_no_timer_and_card_as_before(self):
        for data in (self.ok_data(), dict(self.ok_data(), plan="мусор")):
            html = self.render(data)
            self.assertNotIn('class="plan"', html)
            self.assertNotIn('class="pill', html)

    def test_plan_is_not_a_list_in_the_card_any_more(self):
        html = self.render(dict(self.ok_data(), plan=[row("done", 1), row("run", 2, ticket="TK-2", idle="3 мин", level="")]))
        self.assertNotIn('class="plan"', html)
        self.assertNotIn("План цели", html)

    def test_stale_file_drops_timer(self):
        raw = dict(self.ok_data(), plan=[row("run", 1, who="Инженер", ticket="TK-2", idle="3 мин", level="warn")])
        old = self.at + timedelta(minutes=30)
        html = S.body_html([V.normalise("rpv", raw, old)], old)
        self.assertNotIn("назад", html)
        self.assertNotIn("стоит", html)
        self.assertNotIn('class="pill', html)

    def test_step_numbers_of_goal_come_from_the_file(self):
        goal = {"id": "G-1", "title": "Цель", "step": 2, "steps": 3}
        html = self.render(dict(self.ok_data(), goal=goal, plan=[row("done", 1), row("run", 2), row("next", 3)]))
        self.assertIn("шаг 2 из 3", html)


def srow(mark, ticket="TK-1", title="Заголовок", where="", **kw):
    return dict({"ticket": ticket, "title": title, "mark": mark, "where": where}, **kw)


class StatusBlock(PageStates):
    """TK-117 (В-57): «Статус» — первый блок раскрытой карточки: таблица Задача | Что | Где сейчас, принятые серым, «Осталось · Держит».
    Страница только читает `status` из pulse.json."""

    def block(self, status, **kw):
        html = self.render(dict(self.ok_data(), status=status), **kw)
        body = html.split('<div class="body">')[1]
        self.assertTrue(body.startswith('<div class="planbox"><h4>Статус</h4>'), "первым блоком раскрытой карточки")
        self.assertNotIn('<table class="st"', html.split("</summary>")[0], "в свёрнутой карточке таблицы нет")
        return html, body.split("</div>")[0]

    def lines(self, block):
        rows = re.findall(r"<tr.*?</tr>", block.split("<tbody>")[1], re.S)
        return [re.sub(r"\|+", "|", H.unescape(re.sub(r"<[^>]+>", "|", r))).strip("|") for r in rows]

    def test_table_columns_rows_and_classes(self):
        html, block = self.block({"rows": [srow("done", "TK-1", "Схема", "принят 19:03"), srow("run", "TK-2", "Загрузчик", "Инженер делает"),
                                           srow("ask", "TK-3", "Фьючерсы", "ждёт вашего ответа"), srow("stuck", "TK-4", "Закачка", "стоит 47 мин", level="bad"),
                                           srow("next", "TK-5", "Сверка", "ждёт TK-4")], "left": "Закачка, Сверка"})
        self.assertIn("<th>Задача</th><th>Что</th><th>Где сейчас</th>", block)
        self.assertEqual(self.lines(block), ["TK-1|Схема|принят 19:03", "TK-2|Загрузчик|Инженер делает", "TK-3|Фьючерсы|ждёт вашего ответа",
                                             "TK-4|Закачка|стоит 47 мин", "TK-5|Сверка|ждёт TK-4"])
        self.assertEqual(re.findall(r'<tr class="(\w)">', block), ["d", "r", "o", "s", "w"])
        self.assertEqual(re.findall(r'<td class="where ([^"]*)">', block), ["", "", "", "bad", ""])

    def test_warn_level_colours_the_where_cell(self):
        _, block = self.block({"rows": [srow("run", where="стоит 15 мин", level="warn")]})
        self.assertIn('<td class="where warn">стоит 15 мин</td>', block)

    def test_left_and_hold_under_the_table(self):
        _, block = self.block({"rows": [srow("run")], "left": "принять TK-105, выпустить", "hold": "ваш ответ по TK-105"})
        self.assertIn('<p class="left">Осталось: принять TK-105, выпустить · <b>Держит:</b> ваш ответ по TK-105</p>', block)
        _, block = self.block({"rows": [srow("run")], "left": "Выпуск"})
        self.assertIn('<p class="left">Осталось: Выпуск</p>', block)
        self.assertNotIn("Держит", block)

    def test_more_accepted_is_one_line_first(self):
        _, block = self.block({"rows": [srow("done", "TK-4", where="принят 10:00")], "more": 5})
        self.assertEqual(self.lines(block), ["принято ещё 5", "TK-4|Заголовок|принят 10:00"])

    def test_only_left_still_shows_the_block(self):
        _, block = self.block({"rows": [], "left": "Шаг один"})
        self.assertNotIn("<table", block)
        self.assertIn("Осталось: Шаг один", block)

    def test_no_status_no_block_and_no_old_plan_block(self):
        for data in (self.ok_data(), dict(self.ok_data(), status="мусор"), dict(self.ok_data(), status={"rows": [], "left": ""}),
                     dict(self.ok_data(), plan=[row("run", 1, ticket="TK-2", idle="3 мин", level="")])):
            html = self.render(data)
            self.assertNotIn("<h4>Статус</h4>", html)
            self.assertNotIn("План цели", html)

    def test_bad_rows_are_dropped_and_texts_escaped(self):
        html, block = self.block({"rows": [srow("done", "TK-1", "<b>x</b>", "<i>y</i>"), srow("???", "TK-2"), {"mark": "run", "title": ""}, "мусор",
                                           srow("next", "TK-3", "Верная")], "left": "<u>z</u>", "hold": "<s>h</s>"})
        for raw_tag in ("<b>x</b>", "<i>y</i>", "<u>z</u>", "<s>h</s>"):
            self.assertNotIn(raw_tag, html)
        self.assertEqual(len(self.lines(block)), 2, "чужая отметка, без названия, не объект — не строки")

    def test_stale_file_keeps_accepted_and_left_but_drops_now_and_hold(self):
        status = {"rows": [srow("done", "TK-1", where="принят 10:00"), srow("run", "TK-2", where="Инженер делает", level="warn")],
                  "left": "Выпуск", "hold": "TK-2 стоит 23 мин"}
        old = self.at + timedelta(minutes=30)
        html = S.body_html([V.normalise("rpv", dict(self.ok_data(), status=status), old)], old)
        self.assertIn("принят 10:00", html)
        self.assertIn("Осталось: Выпуск", html)
        self.assertNotIn("Инженер делает", html)
        self.assertNotIn("Держит", html)


class PhoneFit(unittest.TestCase):
    """TK-118: на телефоне (390 px) карточка не шире экрана — длинное слово («test_pulse_facts::test_thresholds_follow_rpv_json») в строке
    «Дальше по очереди» переносится, а не растягивает колонку. Вид проверяется снимком 390 px (docs/reviews/TK-118-pulse-phone.py); здесь —
    три правила, на которых он держится."""

    css = (HERE / "pulse.html").read_text(encoding="utf-8").split("<style>")[1].split("</style>")[0]

    def rules(self, selector):
        found = re.findall(r"(?:^|[{}])\s*" + re.escape(selector) + r"\s*\{([^}]*)\}", self.css)
        self.assertTrue(found, f"правила {selector} нет")
        return found

    def test_long_words_wrap_in_the_whole_page(self):
        self.assertIn("overflow-wrap:anywhere", "".join(self.rules(".wrap")))

    def test_card_columns_may_shrink_below_the_longest_word(self):
        for selector in (".proj summary", ".body"):
            for rule in self.rules(selector):
                for track in re.findall(r"grid-template-columns:([^;}]*)", rule):
                    self.assertNotRegex(re.sub(r"minmax\(0,1fr\)", "", track), r"(?<![\w-])1fr", f"{selector}: колонка 1fr не сжимается ниже слова — minmax(0,1fr)")

    def test_ticket_tag_is_not_torn_apart_by_the_wrapping(self):
        self.assertIn("white-space:nowrap", "".join(self.rules(".tag")))

    def test_text_of_a_list_row_may_shrink_in_its_flex_row(self):
        self.assertIn("min-width:0", "".join(self.rules(".body li>span")))


    def test_logo_and_page_padding_as_on_sofia(self):
        """TK-142 (В-74): вид логотипа и отступ страницы — как на Софии, которую владелец видел и одобрил."""
        self.assertIn("padding:32px 32px 48px", "".join(self.rules(".wrap")))
        logo = "".join(self.rules(".logo"))
        for part in ("gap:12px", "font-size:24px", "font-weight:650", "letter-spacing:-.02em"):
            self.assertIn(part, logo)
        mark = "".join(self.rules(".logo .mark"))
        for part in ("height:36px", "padding:0 10px", "border-radius:9px", "font-weight:700", "letter-spacing:.04em"):
            self.assertIn(part, mark)
        self.assertNotIn(".logo{font-size", self.css.split("@media (max-width:760px)")[1])  # на телефоне логотип не уменьшается

    def test_status_table_has_vertical_lines_between_columns_but_not_before_the_first(self):
        self.assertIn("border-left:1px solid var(--line)", "".join(self.rules(".st th")))
        self.assertIn("border-left:1px solid var(--line)", "".join(self.rules(".st td")))
        first = "".join(self.rules(".st td:first-child,.st th:first-child"))
        self.assertIn("border-left:0", first)
        self.assertIn("padding-left:0", first)


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class ServerTests(TG.States):
    def setUp(self):
        super().setUp()
        self.proj = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.proj, True)
        (self.proj / ".claude").mkdir()
        fresh = datetime.now().astimezone().isoformat()  # сервер сверяет возраст файла с настоящим временем
        remote = dict(self.ok_data(), generated=fresh)
        owner = dict(self.owner_data(), ask={"ticket": "TK-9", "question": "вопрос?"})
        local = self.proj / "pulse.json"
        local.write_text(json.dumps(dict(owner, generated=fresh)), encoding="utf-8")
        (self.proj / ".claude" / "rpv.json").write_text(
            json.dumps({"pulse_projects": {"rpv": str(local), "invoice": "https://x.example/p.json"}}), encoding="utf-8")
        self.fetch = lambda url, timeout=0: FakeResponse(json.dumps(remote).encode("utf-8"))
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), S.make_handler(self.proj, "sekret", self.fetch))
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def get(self, path):
        try:
            with urllib.request.urlopen(self.base + path, timeout=5) as r:
                return r.status, r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            return e.code, ""

    def test_page_and_body_by_token_in_path(self):
        code, page = self.get("/sekret/")
        self.assertEqual(code, 200)
        self.assertIn('data-n="rpv" data-s="owner"', page)
        self.assertIn('<div class="name">invoice</div>', page)  # удалённый проект — по URL
        self.assertIn("prefers-color-scheme:dark", page)  # светлая и тёмная темы
        self.assertNotIn("max-width:1040px", page)  # страница на всю ширину (В-74)
        self.assertIn("max-width:760px", page)  # телефон: 4 часа, карточка в колонку
        self.assertIn("rpv-pulse-filter", page)  # выбор фильтра в localStorage
        self.assertIn("localStorage", page)
        self.assertIn("pointerover", page)  # подсветка клетки и столбца
        self.assertIn("setInterval(tick,30000)", page)  # автообновление раз в 30 с
        self.assertIn("Не удалось обновить", page)  # ошибка обновления показывается, а не глотается
        code, body = self.get("/sekret/body")
        self.assertEqual(code, 200)
        self.assertNotIn("<style", body)
        self.assertIn('<div class="top">', body)

    def test_unreadable_rpv_json_is_not_all_clear(self):
        rpv = self.proj / ".claude" / "rpv.json"
        rpv.write_text("{не json", encoding="utf-8")
        os.utime(rpv, (time.time() + 5, time.time() + 5))  # кэш настроек — по времени изменения
        code, page = self.get("/sekret/")
        self.assertEqual(code, 200)
        self.assertIn(V.NO_PROJECTS, page)
        self.assertNotIn(V.OK_TITLE, page)

    def test_broken_project_url_does_not_break_other_projects(self):
        """Порт не число (InvalidURL) и обрыв ответа (IncompleteRead) — не OSError: «пульс не читается» только у этого проекта."""
        import http.client
        good = json.dumps(dict(self.ok_data(), generated=datetime.now().astimezone().isoformat())).encode("utf-8")

        def fetch(url, timeout=0):
            if "bad-port" in url:
                return urllib.request.urlopen(url, timeout=timeout)  # InvalidURL: nonnumeric port, до сети не доходит
            if "cut" in url:
                raise http.client.IncompleteRead(b"{", 10)
            return FakeResponse(good)

        for url in ("http://host:port/bad-port", "http://host/cut"):
            with self.subTest(url=url):
                self.assertIsNone(V.read(url, fetch))
        rpv = self.proj / ".claude" / "rpv.json"
        rpv.write_text(json.dumps({"pulse_projects": {"bad": "http://host:port/bad-port", "cut": "http://host/cut", "fine": "http://host/ok"}}),
                       encoding="utf-8")
        os.utime(rpv, (time.time() + 5, time.time() + 5))  # кэш настроек — по времени изменения
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), S.make_handler(self.proj, "sekret", fetch))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        with urllib.request.urlopen(f"http://127.0.0.1:{httpd.server_address[1]}/sekret/", timeout=5) as r:
            page = r.read().decode("utf-8")
        self.assertIn('data-n="bad" data-s="stuck"', page)
        self.assertIn('<div class="name">fine</div>', page)
        self.assertEqual(page.count('>пульс не читается<span class="who">'), 2)  # bad и cut; fine — со своей строкой «сейчас»

    def test_wrong_token_or_path_is_404(self):
        self.assertEqual(self.get("/wrong/")[0], 404)
        self.assertEqual(self.get("/")[0], 404)
        self.assertEqual(self.get("/sekret/other")[0], 404)


if __name__ == "__main__":
    unittest.main()
