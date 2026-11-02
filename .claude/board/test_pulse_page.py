"""Пульс, шаг 2 (TK-096): страница на фикстурах pulse.json трёх состояний — фраза, число клеток, классы; сервер по ключу в пути.
Фикстуры — настоящий вывод `pulse.build` (формат шага 1, TK-095): States из dispatcher/test_pulse_tg.py."""
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


def row_classes(html, kind="w"):
    block = re.search(rf'<div class="c {kind}">(.*?)</div>', html, re.S).group(1)
    return [re.search(r'class="([^"]*)"', s).group(1).split() for s in re.findall(r"<span[^>]*></span>", block)]


class PageStates(TG.States):
    def render(self, raw, name="rpv", now=None):
        now = now or self.at
        return S.body_html([V.normalise(name, raw, now)], now)

    def test_ok_state_phrase_cells_classes(self):
        html = self.render(self.ok_data())
        self.assertIn("Всё идёт. От вас ничего.", html)
        self.assertIn('<div class="phrase ">', html)
        wide, narrow = row_classes(html, "w"), row_classes(html, "k")
        self.assertEqual((len(wide), len(narrow)), (S.WINDOW, S.NARROW))
        self.assertEqual([c[0] if c else "" for c in wide[-5:]], ["", "n", "m", "h", "n"])  # клетки 48…52: запусков 0, 1, 3, 5, идёт
        self.assertIn("cur", wide[-1])
        self.assertIn("cur", narrow[self.cur // S.GROUP])
        self.assertIn("judge · TK-7", html)
        self.assertIn("13:00 · сегодня", html)

    def test_owner_state_is_yellow_only_for_owner_cells(self):
        data = self.owner_data()
        html = self.render(data)
        self.assertIn(data["phrase"], html)
        self.assertIn('class="phrase you"', html)
        self.assertEqual(sum(1 for c in row_classes(html) if c and c[0] == "y"), 8)
        self.assertEqual(len(row_classes(html)), 32)
        self.assertIn('class="meta you"', html)

    def test_stuck_state_from_file(self):
        data = self.stuck_data()
        html = self.render(data)
        self.assertIn(data["phrase"], html)
        self.assertIn('class="phrase stuck"', html)
        self.assertEqual(row_classes(html)[-1][0], "o")

    def test_stuck_state_dead_dispatcher_from_stale_generated(self):
        data = self.ok_data()
        html = self.render(data, now=self.at + timedelta(minutes=38))  # файл с 13:00, сейчас 13:38 — клетки 53 и 54 никто не писал
        self.assertIn("Застряло: rpv — диспетчер не отвечает 38 мин.", html)
        self.assertIn('class="phrase stuck"', html)
        wide = row_classes(html)
        self.assertEqual([c[0] for c in wide[-2:] if c], ["o", "o"])
        self.assertEqual([c[0] if c else "" for c in wide[-5:-2]], ["m", "h", "n"])  # записанное до молчания остаётся
        self.assertIn("cur", wide[-1])

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
        fresh = self.view("rpv", self.ok_data(), self.at + timedelta(minutes=5))
        self.assertEqual(fresh["state"], "ok")
        self.assertNotIn("не отвечает", fresh["phrase"])
        late = self.view("rpv", self.ok_data(), self.at + timedelta(minutes=11))
        self.assertEqual(late["state"], "stuck")
        self.assertIn("диспетчер не отвечает 11 мин", late["phrase"])

    def test_stale_view_does_not_show_old_now_and_pause(self):
        data = dict(self.ok_data(), pause="12:00–12:45 · 45 минут · тишина")
        self.assertIn("judge · TK-7", self.render(data))
        html = self.render(data, now=self.at + timedelta(minutes=11))
        self.assertNotIn("judge · TK-7", html)
        self.assertNotIn("12:00–12:45", html)
        self.assertIn("диспетчер не отвечает", html)

    def test_cell_and_clock_follow_the_zone_of_the_file_not_of_the_server(self):
        """Файл проекта в +04:00 (13:00 — клетка 52), сервер в UTC (09:03): клетка и часы — проектные."""
        project_zone = self.at.tzinfo
        self.assertEqual(self.at.utcoffset(), timedelta(hours=4))
        server_now = (self.at + timedelta(minutes=3)).astimezone(timezone.utc)
        view = V.normalise("rpv", self.ok_data(), server_now)
        self.assertEqual((view["idx"], view["zone"]), (self.cur, project_zone))
        html = S.body_html([view], server_now)
        self.assertIn("cur", row_classes(html)[-1])
        self.assertIn("13:03 · сегодня", html)
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
        self.assertEqual(V.summary([]), V.NO_PROJECTS)
        self.assertNotIn(V.OK_PHRASE, V.summary([]))
        html = S.body_html([], self.at)
        self.assertIn(V.NO_PROJECTS, html)
        self.assertIn('class="phrase stuck"', html)

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
        self.assertEqual({c[0] if c else "" for c in row_classes(html)} - {"cur"}, {""})
        self.assertNotIn("phrase stuck", html)

    def test_unreadable_pulse_is_stuck(self):
        for raw in (None, {}, {"phrase": "x", "ts": self.at.isoformat(), "cells": []}):  # нет файла, пусто, чужой формат
            with self.subTest(raw=raw):
                self.assertIn("Застряло: rpv — пульс не читается.", self.render(raw))

    def test_narrow_row_groups_two_hours_and_owner_wins(self):
        data = self.ok_data()
        data["cells"][0:2] = [{"i": 0, "n": 5, "c": "work"}, {"i": 1, "n": 5, "c": "work"}]
        data["cells"][9] = {"i": 9, "n": 0, "c": "owner"}
        data["cells"][10] = {"i": 10, "n": 9, "c": "work"}
        narrow = row_classes(self.render(data), "k")
        self.assertEqual(len(narrow), 12)
        self.assertEqual(narrow[0][0], "m")
        self.assertEqual(narrow[1][0], "y")

    def test_summary_orders_stuck_before_owner(self):
        ok, owner, stuck = self.ok_data(), self.owner_data(), self.stuck_data()
        views = [self.view("a", owner), self.view("b", stuck), self.view("c", ok)]
        self.assertEqual(V.summary(views), stuck["phrase"] + " (и ещё 1)")
        self.assertEqual(V.summary(views[2:]), V.OK_PHRASE)

    def test_names_are_escaped(self):
        self.assertNotIn("<script>", self.render(self.ok_data(), name="<script>"))


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
        owner = self.owner_data()
        self.owner_phrase = owner["phrase"]
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
        self.assertIn(self.owner_phrase, page)
        self.assertIn("<b>invoice</b>", page)  # удалённый проект — по URL
        self.assertIn("prefers-color-scheme:dark", page)
        self.assertIn("max-width:480px", page)
        self.assertIn("setInterval(tick,30000)", page)  # автообновление раз в 30 с
        self.assertIn("Не удалось обновить", page)  # ошибка обновления показывается, а не глотается
        code, body = self.get("/sekret/body")
        self.assertEqual(code, 200)
        self.assertNotIn("<style", body)
        self.assertIn('<div class="head">', body)

    def test_unreadable_rpv_json_is_not_all_clear(self):
        rpv = self.proj / ".claude" / "rpv.json"
        rpv.write_text("{не json", encoding="utf-8")
        os.utime(rpv, (time.time() + 5, time.time() + 5))  # кэш настроек — по времени изменения
        code, page = self.get("/sekret/")
        self.assertEqual(code, 200)
        self.assertIn(V.NO_PROJECTS, page)
        self.assertNotIn(V.OK_PHRASE, page)

    def test_wrong_token_or_path_is_404(self):
        self.assertEqual(self.get("/wrong/")[0], 404)
        self.assertEqual(self.get("/")[0], 404)
        self.assertEqual(self.get("/sekret/other")[0], 404)


if __name__ == "__main__":
    unittest.main()
