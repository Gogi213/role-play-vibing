"""Пульс, шаг 2 (TK-096): закреплённое сообщение Telegram — заглушка API: одна правка при смене текста, ноль при том же."""
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pulse_tg as G  # noqa: E402
import pulse_view as V  # noqa: E402

TZ = timezone(timedelta(hours=4))
T0 = datetime(2026, 11, 2, 16, 0, tzinfo=TZ)


def view(name, phrase, now=T0):
    return V.normalise(name, {"ts": now.isoformat(), "phrase": phrase, "cells": []}, now)


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


class PulseTelegram(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.api, self.state = Api(), {}
        patch = mock.patch.dict(os.environ, {"RPV_TG_TOKEN": "t", "RPV_TG_CHAT": "42"})
        patch.start()
        self.addCleanup(patch.stop)
        self.ok = [view("rpv", V.OK_PHRASE), view("alpha", V.OK_PHRASE)]
        self.stuck = [view("rpv", "Застряло: rpv — CI красный."), view("alpha", V.OK_PHRASE)]

    def sync(self, views, minutes=0):
        return G.sync(self.tmp, self.state, views, T0 + timedelta(minutes=minutes), self.api)

    def log(self):
        p = self.tmp / ".claude" / "dispatcher" / "notify.log"
        return p.read_text(encoding="utf-8") if p.exists() else ""

    def sent_lines(self):
        return [ln for ln in self.log().splitlines() if "	отправлено:" in ln]

    def test_text_first_line_phrase_then_project_lines(self):
        views = [view("rpv", "Нужны вы: rpv — ответить по TK-5."), view("alpha", V.OK_PHRASE), view("inv", "Застряло: inv — CI красный.")]
        self.assertEqual(V.tg_text(views), "Застряло: inv — CI красный. (и ещё 1)\nrpv — нужны вы: ответить по TK-5\n"
                                           "alpha — идёт\ninv — застряло: CI красный")

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
        self.assertTrue(self.api.calls[0][1]["text"].startswith("Застряло: rpv"))

    def test_not_more_than_once_a_minute(self):
        self.sync(self.ok)
        self.api.calls.clear()
        self.assertEqual(G.sync(self.tmp, self.state, self.stuck, T0 + timedelta(seconds=30), self.api), "wait")
        self.assertEqual(self.api.calls, [])
        self.assertEqual(G.sync(self.tmp, self.state, self.stuck, T0 + timedelta(seconds=61), self.api), "edited")

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
        self.assertEqual(self.api.methods(), ["sendMessage"])  # закреплено уже было — второй раз не закрепляем

    def test_create_failure_sends_nothing_and_remembers_nothing(self):
        self.api.fail["sendMessage"] = "Bad Request: chat not found"
        self.assertEqual(self.sync(self.ok), "failed")
        self.assertNotIn("message_id", self.state["pulse_tg"])
        self.assertEqual(self.sent_lines(), [])

    def test_tick_is_off_without_flag_and_reads_projects_with_it(self):
        (self.tmp / ".claude").mkdir(exist_ok=True)
        pj = self.tmp / "p.json"
        pj.write_text(json.dumps({"ts": T0.isoformat(), "phrase": V.OK_PHRASE, "cells": []}), encoding="utf-8")
        rpv = self.tmp / ".claude" / "rpv.json"
        rpv.write_text(json.dumps({"pulse_projects": {"rpv": str(pj)}}), encoding="utf-8")
        self.assertEqual(G.tick(self.tmp, self.state, T0, self.api), "off")
        self.assertEqual(self.api.calls, [])
        rpv.write_text(json.dumps({"pulse_projects": {"rpv": str(pj)}, "pulse_telegram": True}), encoding="utf-8")
        os.utime(rpv, (T0.timestamp() + 5, T0.timestamp() + 5))  # кэш настроек — по времени изменения
        self.assertEqual(G.tick(self.tmp, self.state, T0, self.api), "sent")
        self.assertEqual(self.api.calls[0][1]["text"], f"{V.OK_PHRASE}\nrpv — идёт")


if __name__ == "__main__":
    unittest.main()
