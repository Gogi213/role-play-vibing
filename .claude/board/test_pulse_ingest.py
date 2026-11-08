"""Пульс на сервере (TK-113, В-56): приём присланного `pulse.json` (`PUT /put/<имя>`) и страница по присланным файлам.
Фикстуры — настоящий вывод `pulse.build` (States из dispatcher/test_pulse_tg.py)."""
import http.client
import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from datetime import timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "dispatcher"))
import pulse_server as S  # noqa: E402
import pulse_view as V  # noqa: E402
import test_pulse_tg as TG  # noqa: E402

READ, WRITE = "read-key-0123456789", "write-key-0123456789"


class IngestServer(TG.States):
    def setUp(self):
        super().setUp()
        self.proj = Path(TG.CFG.PROJECT_ROOT)
        self.inbox = self.proj / "inbox"
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), S.make_handler(self.proj, READ, inbox=self.inbox, write_token=WRITE))
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def call(self, method, path, body=b"", key=WRITE, headers=None):
        head = {"Authorization": f"Bearer {key}"} if key is not None else {}
        req = urllib.request.Request(self.base + path, data=body, method=method, headers={**head, **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8")

    def put(self, name, data, **kw):
        return self.call("PUT", f"/put/{name}", data if isinstance(data, bytes) else json.dumps(data).encode("utf-8"), **kw)


class AcceptTests(IngestServer):
    def test_valid_pulse_is_stored_whole(self):
        data = self.ok_data()
        self.assertEqual(self.put("rpv", data)[0], 200)
        self.assertEqual(json.loads((self.inbox / "rpv.json").read_text(encoding="utf-8")), data)
        self.assertEqual([p.name for p in self.inbox.iterdir()], ["rpv.json"])  # временных файлов не осталось

    def test_post_works_like_put(self):
        self.assertEqual(self.call("POST", "/put/rpv", json.dumps(self.ok_data()).encode("utf-8"))[0], 200)

    def test_new_send_replaces_old(self):
        self.put("rpv", self.ok_data())
        newer = dict(self.ok_data(), phrase="Другая фраза")
        self.put("rpv", newer)
        self.assertEqual(json.loads((self.inbox / "rpv.json").read_text(encoding="utf-8"))["phrase"], "Другая фраза")

    def test_wrong_or_missing_write_key_is_401_and_nothing_stored(self):
        for key in ("wrong", "", READ, None):
            self.assertEqual(self.put("rpv", self.ok_data(), key=key)[0], 401, key)
        self.assertFalse(self.inbox.exists())

    def test_write_key_does_not_open_the_page_and_read_key_does_not_write(self):
        self.assertEqual(self.call("GET", f"/{WRITE}/", None, key=None)[0], 404)
        self.assertEqual(self.put("rpv", self.ok_data(), key=READ)[0], 401)

    def test_garbage_is_400(self):
        good = self.ok_data()
        for body in (b"not json", b"[1, 2]", b"{}", json.dumps(dict(good, state="boom")).encode(), json.dumps(dict(good, cells="x")).encode(),
                     json.dumps({k: v for k, v in good.items() if k != "generated"}).encode(),
                     json.dumps(dict(good, generated="2026-11-07T13:00:00")).encode(),  # время без пояса
                     json.dumps(dict(good, generated="2026-11-07")).encode()):  # дата без времени: «-07» не пояс
            self.assertEqual(self.put("rpv", body)[0], 400, body[:40])
        self.assertFalse(self.inbox.exists())

    def test_big_body_is_413_and_missing_length_is_411(self):
        self.assertEqual(self.put("rpv", b"x" * (S.MAX_BODY + 1))[0], 413)
        self.assertEqual(self.put("rpv", b"x" * S.MAX_BODY)[0], 400)  # ровно предел — читается и отвергается как не JSON
        conn = http.client.HTTPConnection("127.0.0.1", self.httpd.server_address[1], timeout=5)
        conn.putrequest("PUT", "/put/rpv")
        conn.putheader("Authorization", f"Bearer {WRITE}")
        conn.endheaders()  # без Content-Length
        self.assertEqual(conn.getresponse().status, 411)
        conn.close()

    def test_big_body_413_is_stable(self):  # сервер сбрасывает тело в пределе: клиент получает ответ, а не обрыв (Windows)
        for _ in range(8):
            self.assertEqual(self.put("rpv", b"x" * (S.MAX_BODY * 2))[0], 413)

    def test_rejection_reads_the_body_first(self):
        """TK-140: отказ (404/401) до чтения тела закрывал соединение с непрочитанными данными — на Windows клиент
        получал WinError 10053 вместо ответа. Тело вычитывается целиком до ответа, на любом отказе."""
        import io
        handler = S.make_handler(self.proj, READ, inbox=self.inbox, write_token=WRITE)
        for path, key in (("/put/a.b", WRITE), ("/put/rpv", "wrong"), ("/other/rpv", WRITE)):
            body = b"x" * 70000
            h = handler.__new__(handler)
            h.path, h.rfile, h.wfile = path, io.BytesIO(body), io.BytesIO()
            h.headers = http.client.HTTPMessage()
            h.headers["Authorization"] = f"Bearer {key}"
            h.headers["Content-Length"] = str(len(body))
            h.request_version, h.command, h.requestline = "HTTP/1.0", "PUT", "PUT " + path
            h.client_address, h.close_connection, h._headers_buffer = ("127.0.0.1", 1), True, []
            h.do_PUT()
            self.assertEqual(h.rfile.read(), b"", f"{path}: тело не прочитано до ответа")
            self.assertRegex(h.wfile.getvalue().decode("utf-8", "replace"), r"HTTP/1.0 (401|404)")

    def test_bad_names_are_404(self):
        for name in ("", "..", "a%2Fb", "a/b", "x" * 41, "%D0%B8%D0%BC%D1%8F", "a.b"):
            self.assertEqual(self.put(name, self.ok_data())[0], 404, name)
        self.assertEqual(self.put("x" * 40, self.ok_data())[0], 200)
        self.assertEqual(sorted(p.name for p in self.inbox.iterdir()), [("x" * 40) + ".json"])

    def test_receiver_off_without_inbox_or_write_key(self):
        for kw in ({}, {"inbox": self.inbox}, {"write_token": WRITE}):
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), S.make_handler(self.proj, READ, **kw))
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            self.addCleanup(httpd.server_close)
            self.addCleanup(httpd.shutdown)
            req = urllib.request.Request(f"http://127.0.0.1:{httpd.server_address[1]}/put/rpv", data=b"{}", method="PUT",
                                         headers={"Authorization": f"Bearer {WRITE}"})
            with self.assertRaises(urllib.error.HTTPError) as got:
                urllib.request.urlopen(req, timeout=5)
            self.assertEqual(got.exception.code, 404, kw)

    def test_main_refuses_inbox_without_distinct_write_key(self):
        self.assertEqual(S.main(["--project", str(self.proj), "--token", READ, "--inbox", str(self.inbox)]), 2)
        self.assertEqual(S.main(["--project", str(self.proj), "--token", READ, "--inbox", str(self.inbox), "--write-token", READ]), 2)


class PageTests(IngestServer):
    def page(self, key=READ):
        return self.call("GET", f"/{key}/", None, key=None)

    def test_sent_project_is_on_the_page_without_pulse_projects(self):
        self.put("alpha", self.ok_data())
        status, html = self.page()
        self.assertEqual(status, 200)
        self.assertIn('<div class="name">alpha</div>', html)

    def test_configured_first_then_sent_by_name(self):
        local = self.proj / "local.json"
        local.write_text(json.dumps(self.ok_data()), encoding="utf-8")
        (self.proj / ".claude" / "rpv.json").write_text(json.dumps({"pulse_projects": {"zeta": str(local)}}), encoding="utf-8")
        for name in ("rpv", "invoice", "zeta"):
            self.put(name, self.ok_data())
        names = [v["name"] for v in V.load_all(self.proj, self.at, extra=S.inbox_projects(self.inbox))]
        self.assertEqual(names, ["zeta", "invoice", "rpv"])  # проект из rpv.json один раз и первым; присланные — по алфавиту

    def test_stale_sent_project_says_no_data_not_old_numbers(self):
        data = self.ok_data()
        self.put("invoice", data)
        late = self.at + timedelta(minutes=23)
        views = V.load_all(self.proj, late, extra=S.inbox_projects(self.inbox), silence=V.NO_DATA)
        self.assertEqual((views[0]["state"], views[0]["fix"]), ("stuck", {"text": "нет данных 23 мин", "who": "проверяет CEO"}))
        html = S.body_html(views, late)
        self.assertIn("Застряло: invoice", html)
        self.assertIn("нет данных 23 мин", html)
        self.assertNotIn("диспетчер не отвечает", html)
        self.assertEqual(views[0]["cells"][self.cur + 1], ("stuck", 0))  # клетки после последней записи красные, не прежние цифры

    def test_local_page_keeps_old_wording(self):
        view = V.normalise("rpv", self.ok_data(), self.at + timedelta(minutes=23))
        self.assertEqual(view["fix"]["text"], "диспетчер не отвечает 23 мин")

    def test_page_sees_file_sent_after_start(self):
        self.assertIn("Проектов нет", self.page()[1])
        self.put("alpha", self.ok_data())
        self.assertIn('<div class="name">alpha</div>', self.page()[1])


if __name__ == "__main__":
    unittest.main()
