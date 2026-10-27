"""CP5: сообщение владельцу в Telegram — только отправка; не настроено или сбой — строка в лог, исключений нет."""
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import notify as N  # noqa: E402


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class NotifyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        env = {k: v for k, v in os.environ.items() if not k.startswith("RPV_TG_")}
        self.env = mock.patch.dict(os.environ, env, clear=True)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def log(self):
        p = self.root / ".claude" / "dispatcher" / "notify.log"
        return p.read_text(encoding="utf-8") if p.exists() else ""

    def test_not_configured_logs_and_returns_false(self):
        called = []
        self.assertFalse(N.send("Работа стоит", self.root, opener=lambda *a, **k: called.append(1)))
        self.assertEqual(called, [])
        self.assertIn("нет RPV_TG_TOKEN/RPV_TG_CHAT", self.log())

    def test_sends_utf8_json_to_owner_chat(self):
        seen = {}

        def opener(req, timeout):
            seen["url"], seen["body"], seen["ct"] = req.full_url, json.loads(req.data.decode("utf-8")), req.headers
            return Resp(b'{"ok": true}')
        with mock.patch.dict(os.environ, {"RPV_TG_TOKEN": "123:abc", "RPV_TG_CHAT": "251"}):
            self.assertTrue(N.send("Работа стоит 25 мин", self.root, opener=opener))
        self.assertEqual(seen["url"], "https://api.telegram.org/bot123:abc/sendMessage")
        self.assertEqual(seen["body"], {"chat_id": "251", "text": "Работа стоит 25 мин"})
        self.assertIn("отправлено: Работа стоит", self.log())
        self.assertNotIn("123:abc", self.log(), "токен в лог не пишется")

    def test_network_failure_retries_then_logs(self):
        calls = []

        def opener(req, timeout):
            calls.append(1)
            raise TimeoutError("read timed out")
        with mock.patch.dict(os.environ, {"RPV_TG_TOKEN": "t", "RPV_TG_CHAT": "c"}):
            self.assertFalse(N.send("x", self.root, opener=opener, sleep=lambda s: None))
        self.assertEqual(len(calls), N.TRIES)
        self.assertIn("не отправлено (TimeoutError", self.log())

    def test_client_error_is_not_retried(self):
        calls = []

        def opener(req, timeout):
            calls.append(1)
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)
        with mock.patch.dict(os.environ, {"RPV_TG_TOKEN": "t", "RPV_TG_CHAT": "c"}):
            self.assertFalse(N.send("x", self.root, opener=opener, sleep=lambda s: None))
        self.assertEqual(len(calls), 1)
        self.assertIn("HTTP 401", self.log())

    def test_empty_text_and_long_text(self):
        self.assertFalse(N.send("  ", self.root))
        seen = {}

        def opener(req, timeout):
            seen["text"] = json.loads(req.data.decode("utf-8"))["text"]
            return Resp(b'{"ok": true}')
        with mock.patch.dict(os.environ, {"RPV_TG_TOKEN": "t", "RPV_TG_CHAT": "c"}):
            N.send("я" * 5000, self.root, opener=opener)
        self.assertEqual(len(seen["text"]), N.LIMIT)


class EpisodeTests(unittest.TestCase):
    """TK-010: одна тревога на эпизод — общая для диспетчера (`_stall_alarm`) и присмотра (`supervise._alarm`)."""

    def run_episode(self, st, firing, cleared, **kw):
        sent = []
        with mock.patch.object(N, "send", side_effect=lambda text, **k: sent.append(text)):
            N.episode(st, "flag", firing, cleared, down=lambda: "стоит", up=lambda: "идёт", **kw)
        return sent

    def test_once_per_episode_then_recovery(self):
        st, ceo = {}, []
        kw = {"ceo_down": lambda t: ceo.append(("down", t)), "ceo_up": lambda t: ceo.append(("up", t))}
        self.assertEqual(self.run_episode(st, False, False, **kw), [])
        self.assertEqual(self.run_episode(st, True, False, **kw), ["стоит"])
        self.assertEqual(self.run_episode(st, True, False, **kw), [], "второй раз в том же эпизоде — тишина")
        self.assertEqual(self.run_episode(st, False, False, **kw), [], "ни стоит, ни ожило — флаг держится")
        self.assertEqual(self.run_episode(st, False, True, **kw), ["идёт"])
        self.assertEqual(self.run_episode(st, False, True, **kw), [], "«ожило» без тревоги не шлют")
        self.assertEqual(ceo, [("down", "стоит"), ("up", "идёт")])
        self.assertEqual(self.run_episode(st, True, False, **kw), ["стоит"], "новый эпизод — новая тревога")

    def test_ceo_line_is_optional(self):
        st = {}
        self.assertEqual(self.run_episode(st, True, False), ["стоит"])
        self.assertEqual(self.run_episode(st, False, True), ["идёт"])

    def test_both_alarms_use_it(self):
        """Обе тревоги идут через `notify.episode`: ни у одной нет своей копии «флаг + отправка»."""
        for name in ("dispatch.py", "supervise.py"):
            self.assertIn("notify.episode(", (HERE / name).read_text(encoding="utf-8"), name)


if __name__ == "__main__":
    unittest.main()
