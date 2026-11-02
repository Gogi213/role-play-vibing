"""Пульс, шаг 2 (TK-096): страница на фикстурах pulse.json трёх состояний — фраза, число клеток, классы; сервер по ключу в пути."""
import io
import json
import re
import sys
import tempfile
import threading
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

TZ = timezone(timedelta(hours=4))
NOW = datetime(2026, 11, 2, 16, 2, tzinfo=TZ)  # клетка 64 (16:00–16:15)
IDX = 64


def pulse(phrase, cells=None, ts=NOW, **extra):
    day = [{"runs": 0, "cls": "none"} for _ in range(96)]
    for i, (cls, runs) in (cells or {}).items():
        day[i] = {"runs": runs, "cls": cls}
    return {"project": "p", "ts": ts.isoformat(), "phrase": phrase, "now": extra.pop("now_line", ""), "pause": "", "cells": day, **extra}


def row_classes(html, kind="w"):
    block = re.search(rf'<div class="c {kind}">(.*?)</div>', html, re.S).group(1)
    return [re.search(r'class="([^"]*)"', s).group(1).split() for s in re.findall(r"<span[^>]*></span>", block)]


def render(name, raw, now=NOW):
    return S.body_html([V.normalise(name, raw, now)], now)


class PageStates(unittest.TestCase):
    def test_ok_state_phrase_cells_classes(self):
        raw = pulse(V.OK_PHRASE, {IDX - 3: ("work", 1), IDX - 2: ("work", 3), IDX - 1: ("work", 5), IDX: ("work", 2)},
                    now_line="Судья смотрит TK-087")
        html = render("rpv", raw)
        self.assertIn("Всё идёт. От вас ничего.", html)
        self.assertIn('<div class="phrase ">', html)
        wide, narrow = row_classes(html, "w"), row_classes(html, "k")
        self.assertEqual((len(wide), len(narrow)), (S.WINDOW, S.NARROW))
        self.assertEqual([c[0] if c else "" for c in wide[-5:]], ["", "n", "m", "h", "m"])
        self.assertIn("cur", wide[-1])
        self.assertIn("cur", narrow[IDX // S.GROUP])
        self.assertIn("Судья смотрит TK-087", html)
        self.assertIn("16:02 · сегодня", html)

    def test_owner_state_is_yellow_only_for_owner_cells(self):
        raw = pulse("Нужны вы: ответить invoice по TK-112.", {i: ("owner", 0) for i in range(IDX - 7, IDX + 1)})
        html = render("invoice", raw)
        self.assertIn("Нужны вы: ответить invoice по TK-112.", html)
        self.assertIn('class="phrase you"', html)
        self.assertEqual(sum(1 for c in row_classes(html) if c and c[0] == "y"), 8)
        self.assertEqual(len(row_classes(html)), 32)
        self.assertIn('class="meta you"', html)

    def test_stuck_state_dead_dispatcher_from_stale_ts(self):
        raw = pulse(V.OK_PHRASE, {IDX - 25: ("work", 4)}, ts=NOW - timedelta(minutes=23))
        html = render("rpv", raw)
        self.assertIn("Застряло: rpv — диспетчер не отвечает 23 мин.", html)
        self.assertIn('class="phrase stuck"', html)
        wide = row_classes(html)
        self.assertEqual(len(wide), 32)
        self.assertEqual([c[0] for c in wide[-2:] if c], ["o", "o"])
        self.assertIn("cur", wide[-1])

    def test_stuck_cells_and_phrase_from_file(self):
        raw = pulse("Застряло: alpha — TK-9 — третий возврат.", {IDX: ("stuck", 0)})
        html = render("alpha", raw)
        self.assertIn('class="phrase stuck"', html)
        self.assertEqual(row_classes(html)[-1][0], "o")

    def test_no_work_is_not_stuck(self):
        html = render("rpv", pulse(V.OK_PHRASE))
        self.assertEqual({c[0] if c else "" for c in row_classes(html)} - {"cur"}, {""})
        self.assertNotIn("phrase stuck", html)

    def test_unreadable_pulse_is_stuck(self):
        html = S.body_html([V.normalise("rpv", None, NOW)], NOW)
        self.assertIn("Застряло: rpv — пульс не читается.", html)

    def test_narrow_row_groups_two_hours_and_owner_wins(self):
        raw = pulse(V.OK_PHRASE, {0: ("work", 5), 1: ("work", 5), 9: ("owner", 0), 10: ("work", 9)})
        narrow = row_classes(render("rpv", raw), "k")
        self.assertEqual(len(narrow), 12)
        self.assertEqual(narrow[0][0], "m")
        self.assertEqual(narrow[1][0], "y")

    def test_summary_orders_stuck_before_owner(self):
        views = [V.normalise("a", pulse("Нужны вы: a — ответить по TK-1."), NOW),
                 V.normalise("b", pulse("Застряло: b — CI красный."), NOW),
                 V.normalise("c", pulse(V.OK_PHRASE), NOW)]
        self.assertEqual(V.summary(views), "Застряло: b — CI красный. (и ещё 1)")
        self.assertEqual(V.summary(views[2:]), V.OK_PHRASE)

    def test_names_are_escaped(self):
        self.assertNotIn("<script>", render("<script>", pulse(V.OK_PHRASE)))


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / ".claude").mkdir()
        local = self.tmp / "pulse.json"
        local.write_text(json.dumps(pulse("Нужны вы: rpv — ответить по TK-5.", ts=datetime.now().astimezone())), encoding="utf-8")
        remote = pulse(V.OK_PHRASE, ts=datetime.now().astimezone())
        (self.tmp / ".claude" / "rpv.json").write_text(
            json.dumps({"pulse_projects": {"rpv": str(local), "invoice": "https://x.example/p.json"}}), encoding="utf-8")
        self.fetch = lambda url, timeout=0: FakeResponse(json.dumps(remote).encode("utf-8"))
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), S.make_handler(self.tmp, "sekret", self.fetch))
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
        self.assertIn("Нужны вы: rpv — ответить по TK-5.", page)
        self.assertIn("<b>invoice</b>", page)  # удалённый проект — по URL
        self.assertIn("prefers-color-scheme:dark", page)
        self.assertIn("max-width:480px", page)
        code, body = self.get("/sekret/body")
        self.assertEqual(code, 200)
        self.assertNotIn("<style", body)
        self.assertIn('<div class="head">', body)

    def test_wrong_token_or_path_is_404(self):
        self.assertEqual(self.get("/wrong/")[0], 404)
        self.assertEqual(self.get("/")[0], 404)
        self.assertEqual(self.get("/sekret/other")[0], 404)


if __name__ == "__main__":
    unittest.main()
