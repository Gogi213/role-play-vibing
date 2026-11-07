"""Пульс на сервере (TK-113, В-56): проект сам отправляет `pulse.json` — не чаще раза в минуту, ключ в заголовке, сбой тик не валит."""
import io
import json
import shutil
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pulse_push as G  # noqa: E402
import rpv_config as RC  # noqa: E402

KEY = "write-key-0123456789"
CFG = {"url": "https://13.140.29.171:9447", "name": "rpv", "key": KEY}
NOW = datetime(2026, 11, 7, 13, 0, 0, tzinfo=timezone(timedelta(hours=4)))


class Server:
    """Заглушка сервера страницы: пишет запросы; `answer` — код, исключение или None (принято)."""

    def __init__(self):
        self.calls, self.answer = [], None

    def __call__(self, req, timeout=0):
        self.calls.append((req.get_method(), req.full_url, dict(req.header_items()), req.data, timeout))
        if isinstance(self.answer, BaseException):
            raise self.answer
        if isinstance(self.answer, int):
            raise urllib.error.HTTPError(req.full_url, self.answer, "x", {}, io.BytesIO(b""))
        resp = io.BytesIO(b"ok")
        resp.status = 200
        resp.__enter__ = lambda: resp
        resp.__exit__ = lambda *a: False
        return resp


class PushBase(unittest.TestCase):
    def setUp(self):
        self.proj = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.proj, True)
        (self.proj / ".claude" / "dispatcher").mkdir(parents=True)
        self.body = json.dumps({"state": "ok", "cells": [], "generated": NOW.isoformat()}).encode("utf-8")
        (self.proj / ".claude" / "dispatcher" / "pulse.json").write_bytes(self.body)
        self.server, self.state = Server(), {}

    def settings(self, **kv):
        (self.proj / ".claude" / "rpv.json").write_text(json.dumps(kv), encoding="utf-8")

    def tick(self, at=NOW):
        return G.tick(self.proj, self.state, at, self.server)

    def notify_log(self):
        try:
            return (self.proj / ".claude" / "dispatcher" / "notify.log").read_text(encoding="utf-8")
        except OSError:
            return ""


class TickTests(PushBase):
    def test_not_configured_does_nothing_and_never_touches_network(self):
        self.assertEqual(self.tick(), "off")
        self.settings(pulse_push={"url": "https://h:1", "name": "rpv"})  # нет ключа — как не настроено
        self.assertEqual(self.tick(), "off")
        self.assertEqual((self.server.calls, self.state), ([], {}))

    def test_sends_file_as_is_with_key_in_header_only(self):
        self.settings(pulse_push=CFG)
        self.assertEqual(self.tick(), "sent")
        method, url, headers, data, timeout = self.server.calls[0]
        self.assertEqual((method, url, data), ("PUT", "https://13.140.29.171:9447/put/rpv", self.body))
        self.assertEqual(headers["Authorization"], f"Bearer {KEY}")
        self.assertNotIn(KEY, url)
        self.assertLessEqual(timeout, 5)

    def test_at_most_once_a_minute(self):
        self.settings(pulse_push=CFG)
        self.assertEqual([self.tick(NOW + timedelta(seconds=s)) for s in (0, 15, 59, 60, 75, 119, 120)],
                         ["sent", "wait", "wait", "sent", "wait", "wait", "sent"])
        self.assertEqual(len(self.server.calls), 3)

    def test_failures_do_not_raise_and_retry_after_a_minute(self):
        self.settings(pulse_push=CFG)
        for answer in (urllib.error.URLError("нет сети"), TimeoutError("timed out"), 401, 500, ConnectionResetError("reset")):
            self.server.answer, self.state = answer, {}
            self.assertEqual(self.tick(), "failed", answer)
        self.assertEqual(self.tick(NOW + timedelta(seconds=30)), "wait")

    def test_log_only_on_change_and_never_with_key(self):
        self.settings(pulse_push=CFG)
        self.server.answer = 401
        for minute in range(5):
            self.tick(NOW + timedelta(minutes=minute))
        self.assertEqual(self.notify_log().count("не отправлено (HTTP 401)"), 1)  # пять минут сбоя — одна строка
        self.server.answer = 500
        self.tick(NOW + timedelta(minutes=6))
        self.server.answer = None
        self.tick(NOW + timedelta(minutes=7))
        self.tick(NOW + timedelta(minutes=8))
        log = self.notify_log()
        self.assertEqual((log.count("не отправлено (HTTP 500)"), log.count("снова отправляется")), (1, 1))
        self.assertNotIn(KEY, log)

    def test_missing_file_is_nofile_without_network(self):
        self.settings(pulse_push=CFG)
        (self.proj / ".claude" / "dispatcher" / "pulse.json").unlink()
        self.assertEqual(self.tick(), "nofile")
        self.assertEqual(self.server.calls, [])


class ConfigTests(unittest.TestCase):
    def test_valid_shapes(self):
        self.assertTrue(G.valid(CFG))
        self.assertTrue(G.valid(dict(CFG, url="http://127.0.0.1:8791")))  # голый http — только на этой же машине (туннель)
        self.assertTrue(G.valid(dict(CFG, url="http://localhost:8791")))
        self.assertTrue(G.valid(dict(CFG, name="x" * 40)))

    def test_invalid_shapes(self):
        for bad in (None, "x", [], {}, dict(CFG, url="http://13.140.29.171:9447"), dict(CFG, url="https://h:9447/put"), dict(CFG, url="ftp://h"),
                    dict(CFG, url="https://h:9447 "), dict(CFG, name=""), dict(CFG, name="a/b"), dict(CFG, name="x" * 41), dict(CFG, name="имя"),
                    dict(CFG, key="short"), dict(CFG, key="has space 0123456789"), dict(CFG, key="line\r\nbreak0123456789"),
                    dict(CFG, key=123), {**CFG, "extra": 1}, {"url": CFG["url"], "name": "rpv"}):
            self.assertFalse(G.valid(bad), bad)

    def test_config_set_validator_is_the_same(self):
        check, words = RC.KEYS["pulse_push"]
        self.assertTrue(check(CFG))
        self.assertFalse(check(dict(CFG, key="short")))
        self.assertIn("url", words)


class OnceTests(PushBase):
    def run_main(self):
        import contextlib
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = G.main(["--project", str(self.proj)])
        return code, out.getvalue()

    def test_not_configured_is_2(self):
        self.assertEqual(self.run_main()[0], 2)

    def test_unreachable_is_1_and_does_not_print_the_key(self):
        self.settings(pulse_push=dict(CFG, url="http://127.0.0.1:1"))  # порт 1 никто не слушает
        code, out = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("не принято", out)
        self.assertNotIn(KEY, out)


if __name__ == "__main__":
    unittest.main()
