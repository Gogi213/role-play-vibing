"""Пульс, шаг 2 (TK-096): закреплённое сообщение Telegram — заглушка API: одна правка при смене текста, ноль при том же.
Фикстуры — настоящий вывод `pulse.build` (формат шага 1, TK-095), не свой формат."""
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import urllib.error
from datetime import timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_tk095 as K  # noqa: E402 — песочница шага 1 (PulseBase) и его запуски pulse.build
import pulse_tg as G  # noqa: E402
import pulse_view as V  # noqa: E402

CFG, T = K.CFG, K.T


class States(K.PulseBase):
    """Три состояния проекта — выводом `pulse.build`: ok (работа в клетках, идёт запуск), owner (needs_owner), stuck (возврат ×2)."""

    def setUp(self):
        super().setUp()
        self.name = Path(CFG.PROJECT_ROOT).name
        self.cur = K.PU.cell_index(self.at)

    def ok_data(self):
        for m in (40, 25, 24, 23, 10, 9, 8, 7, 6):  # клетки 49 (1 запуск), 50 (3), 51 (5); текущая 52 — идёт запуск
            self.run_line(m)
        self.state["active_runs"] = {"TK-7": {"role": "judge", "started": T.now_iso(self.at)}}
        return self.pulse()

    def owner_data(self):
        self.state["pulse_cells"] = {"day": self.at.date().isoformat(), "cells": {str(i): "owner" for i in range(self.cur - 7, self.cur)}}
        self.ticket(ref="", status="needs_owner")
        return self.pulse()

    def stuck_data(self):
        tid = self.ticket(ref="", status="in_progress")
        self.state["review_returns"] = {tid: 2}
        return self.pulse()

    def view(self, name, data, at=None):
        return V.normalise(name, data, at or self.at)


class Api:
    """Заглушка Telegram: пишет (метод, тело); ответы по умолчанию ok, `fail[метод]` — описание ошибки API."""

    def __init__(self):
        self.calls, self.fail = [], {}

    def __call__(self, req, timeout=0):
        method = req.full_url.rsplit("/", 1)[-1]
        self.calls.append((method, json.loads(req.data.decode("utf-8"))))
        if method in self.fail:
            body = json.dumps({"ok": False, "description": self.fail[method]}).encode("utf-8")
            raise urllib.error.HTTPError(req.full_url, 400, "bad", {}, io.BytesIO(body))
        result = {"message_id": 77} if method == "sendMessage" else True
        resp = io.BytesIO(json.dumps({"ok": True, "result": result}).encode("utf-8"))
        resp.__enter__ = lambda: resp
        resp.__exit__ = lambda *a: False
        return resp

    def methods(self):
        return [m for m, _ in self.calls]


class PulseTelegram(States):
    def setUp(self):
        super().setUp()
        self.proj = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.proj, True)
        self.api, self.state = Api(), {}
        patch = mock.patch.dict(os.environ, {"RPV_TG_TOKEN": "t", "RPV_TG_CHAT": "42"})
        patch.start()
        self.addCleanup(patch.stop)
        self.ok_raw = self.ok_data()
        self.ok = [self.view("rpv", self.ok_raw), self.view("alpha", self.ok_raw)]
        self.stuck = [self.view("rpv", self.stuck_data()), self.view("alpha", self.ok_raw)]

    def sync(self, views, minutes=0):
        return G.sync(self.proj, self.state, views, self.at + timedelta(minutes=minutes), self.api)

    def log(self):
        p = self.proj / ".claude" / "dispatcher" / "notify.log"
        return p.read_text(encoding="utf-8") if p.exists() else ""

    def sent_lines(self):
        return [ln for ln in self.log().splitlines() if "	отправлено:" in ln]

    def test_first_run_creates_and_pins_once(self):
        self.assertEqual(self.sync(self.ok), "sent")
        self.assertEqual(self.api.methods(), ["sendMessage", "pinChatMessage"])
        self.assertEqual(self.state["pulse_tg"]["message_id"], 77)
        self.assertEqual(self.api.calls[0][1]["chat_id"], "42")
        self.assertEqual(len(self.sent_lines()), 1)

    def test_same_text_no_calls_changed_text_one_edit(self):
        self.sync(self.ok)
        self.api.calls.clear()
        self.assertEqual(self.sync(self.ok, 2), "same")
        self.assertEqual(self.api.calls, [])
        self.assertEqual(self.sync(self.stuck, 4), "edited")
        self.assertEqual(self.api.methods(), ["editMessageText"])
        self.assertEqual(self.api.calls[0][1]["message_id"], 77)
        self.assertTrue(self.api.calls[0][1]["text"].startswith("Застряло:"))

    def test_not_more_than_once_a_minute(self):
        self.sync(self.ok)
        self.api.calls.clear()
        self.assertEqual(G.sync(self.proj, self.state, self.stuck, self.at + timedelta(seconds=30), self.api), "wait")
        self.assertEqual(self.api.calls, [])
        self.assertEqual(G.sync(self.proj, self.state, self.stuck, self.at + timedelta(seconds=61), self.api), "edited")

    def test_failed_edit_is_not_logged_as_sent_and_text_not_remembered(self):
        self.sync(self.ok)
        self.api.fail["editMessageText"] = "Bad Request: network down"
        before = len(self.sent_lines())
        self.assertEqual(self.sync(self.stuck, 2), "failed")
        self.assertEqual(len(self.sent_lines()), before)
        self.assertIn("не отправлено", self.log())
        self.api.fail.clear()
        self.assertEqual(self.sync(self.stuck, 4), "edited")  # текст не запомнен — повтор уходит

    def test_not_modified_is_same_deleted_message_is_recreated(self):
        self.sync(self.ok)
        self.api.fail["editMessageText"] = "Bad Request: message is not modified"
        self.assertEqual(self.sync(self.stuck, 2), "same")
        self.api.fail["editMessageText"] = "Bad Request: message to edit not found"
        self.assertEqual(self.sync(self.ok, 4), "failed")
        self.assertNotIn("message_id", self.state["pulse_tg"])
        self.api.fail.clear()
        self.api.calls.clear()
        self.assertEqual(self.sync(self.ok, 6), "sent")
        self.assertEqual(self.api.methods(), ["sendMessage", "pinChatMessage"])  # пересозданное сообщение закрепляется заново
        self.assertTrue(self.state["pulse_tg"]["pinned"])
        self.api.calls.clear()
        self.assertEqual(self.sync(self.ok, 8), "same")
        self.assertEqual(self.api.calls, [])  # закреплено — больше не трогаем

    def test_pin_failure_is_retried_a_few_times_then_stops(self):
        self.api.fail["pinChatMessage"] = "Bad Request: not enough rights"
        self.assertEqual(self.sync(self.ok), "sent")
        self.assertFalse(self.state["pulse_tg"]["pinned"])
        for minute in (2, 4):
            self.assertEqual(self.sync(self.ok, minute), "same")
        self.assertEqual(self.api.methods().count("pinChatMessage"), G.PIN_TRIES)
        self.api.calls.clear()
        self.assertEqual(self.sync(self.ok, 6), "same")
        self.assertEqual(self.api.calls, [])  # попытки вышли — молчим
        self.assertIn("не закреплено (Bad Request: not enough rights), попытка 3 из 3", self.log())

    def test_pin_retry_succeeds_after_failure(self):
        self.api.fail["pinChatMessage"] = "Bad Request: not enough rights"
        self.sync(self.ok)
        self.api.fail.clear()
        self.assertEqual(self.sync(self.ok, 2), "same")
        self.assertTrue(self.state["pulse_tg"]["pinned"])
        self.assertEqual(self.api.methods()[-1], "pinChatMessage")

    def test_create_failure_sends_nothing_and_remembers_nothing(self):
        self.api.fail["sendMessage"] = "Bad Request: chat not found"
        self.assertEqual(self.sync(self.ok), "failed")
        self.assertNotIn("message_id", self.state["pulse_tg"])
        self.assertEqual(self.sent_lines(), [])

    def test_tick_is_off_without_flag_and_reads_projects_with_it(self):
        (self.proj / ".claude").mkdir(exist_ok=True)
        pj = self.proj / "p.json"
        pj.write_text(json.dumps(self.ok_raw), encoding="utf-8")
        rpv = self.proj / ".claude" / "rpv.json"
        rpv.write_text(json.dumps({"pulse_projects": {"rpv": str(pj)}}), encoding="utf-8")
        self.assertEqual(G.tick(self.proj, self.state, self.at, self.api), "off")
        self.assertEqual(self.api.calls, [])
        rpv.write_text(json.dumps({"pulse_projects": {"rpv": str(pj)}, "pulse_telegram": True}), encoding="utf-8")
        os.utime(rpv, (self.at.timestamp() + 5, self.at.timestamp() + 5))  # кэш настроек — по времени изменения
        self.assertEqual(G.tick(self.proj, self.state, self.at, self.api), "sent")
        self.assertEqual(self.api.calls[0][1]["text"], f"{V.OK_PHRASE}\nrpv — идёт")

    def test_tick_is_off_without_token_and_does_not_read_projects(self):
        (self.proj / ".claude").mkdir(exist_ok=True)
        (self.proj / ".claude" / "rpv.json").write_text(json.dumps({"pulse_projects": {"rpv": "http://x/"}, "pulse_telegram": True}), encoding="utf-8")
        fetched = []
        for env in ({"RPV_TG_TOKEN": ""}, {"RPV_TG_CHAT": ""}):
            with self.subTest(env=env), mock.patch.dict(os.environ, env):
                self.assertEqual(G.tick(self.proj, self.state, self.at, self.api, lambda *a, **k: fetched.append(a)), "off")
        self.assertEqual((fetched, self.api.calls), ([], []))

    def test_tick_without_projects_sends_no_projects_text(self):
        (self.proj / ".claude").mkdir(exist_ok=True)
        (self.proj / ".claude" / "rpv.json").write_text(json.dumps({"pulse_projects": {}, "pulse_telegram": True}), encoding="utf-8")
        self.assertEqual(G.tick(self.proj, self.state, self.at, self.api), "sent")
        self.assertEqual(self.api.calls[0][1]["text"], V.NO_PROJECTS)


class TextTests(States):
    def test_text_first_line_phrase_then_project_lines(self):
        ok = self.ok_data()
        owner = self.owner_data()
        stuck = self.stuck_data()
        views = [self.view("rpv", owner), self.view("alpha", ok), self.view("inv", stuck)]
        tid = stuck["reasons"][0]["detail"].split(":")[0]
        self.assertEqual(V.tg_text(views), f"{stuck['phrase']} (и ещё 1)\n"
                                           f"rpv — нужны вы: ответить на вопрос по {owner['owner'][0]['ticket']}\n"
                                           f"alpha — идёт\n"
                                           f"inv — застряло: {tid}: возвращён Судьёй 2 раз")


if __name__ == "__main__":
    unittest.main()
