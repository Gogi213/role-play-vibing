"""Тесты табло-сервера (команды, ключи, приём сводок): python -m unittest discover -s .claude/board"""
import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))


class TeamsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        (d / "token").write_text("x" * 32)
        os.environ["RPV_BOARD_DATA"] = str(d / "board")
        os.environ["RPV_BOARD_TOKEN_FILE"] = str(d / "token")
        (d / "board").mkdir()
        import server
        self.s = importlib.reload(server)

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_status_yet(self):
        doc, err = self.s.load_status()
        self.assertIsNone(doc)
        self.assertTrue(err)

    def test_home_stays_on_top_level(self):
        (self.s.DATA / "status.json").write_text(json.dumps({"view2": {"a": 1}, "built_at": "t"}))
        doc, _ = self.s.load_status()
        self.assertEqual(doc["view2"], {"a": 1})
        self.assertEqual([t["id"] for t in doc["teams"]], ["home"])
        self.assertEqual(doc["teams"][0]["name"], "Моя команда")

    def test_new_team_key_ingest_and_isolation(self):
        (self.s.DATA / "status.json").write_text(json.dumps({"view2": {"a": 1}}))
        t = self.s.new_team("  Инвойс ")
        self.assertEqual(t["name"], "Инвойс")
        self.assertNotIn(t["key"], self.s.TEAMS_FILE.read_text(encoding="utf-8"))  # ключ не хранится, только sha256
        self.assertEqual(self.s.team_by_key(t["key"])["id"], t["id"])
        self.assertIsNone(self.s.team_by_key("rpv_wrong"))
        self.assertTrue(self.s.ingest(t["id"], b'{"view2": {"b": 2}, "built_at": "x"}'))
        self.assertFalse(self.s.ingest(t["id"], b'{"no": 1}'))
        self.assertFalse(self.s.ingest(t["id"], b"not json"))
        doc, _ = self.s.load_status()
        by = {x["id"]: x for x in doc["teams"]}
        self.assertEqual(by[t["id"]]["view2"], {"b": 2})
        self.assertEqual(by["home"]["view2"], {"a": 1})
        self.assertEqual(doc["view2"], {"a": 1})

    def test_delete_revokes_key_and_drops_summary(self):
        (self.s.DATA / "status.json").write_text(json.dumps({"view2": {"a": 1}}))
        t, u = self.s.new_team("A"), self.s.new_team("B")
        self.assertTrue(self.s.ingest(t["id"], b'{"view2": {"b": 2}}'))
        self.assertTrue(self.s.delete_team(t["id"]))
        self.assertIsNone(self.s.team_by_key(t["key"]))
        self.assertFalse((self.s.TEAMS_DIR / (t["id"] + ".json")).exists())
        self.assertEqual(self.s.team_by_key(u["key"])["id"], u["id"])
        self.assertFalse(self.s.delete_team(t["id"]))
        self.assertFalse(self.s.delete_team("home"))
        doc, _ = self.s.load_status()
        self.assertEqual([x["id"] for x in doc["teams"]], ["home", u["id"]])

    def test_home_name_from_registry(self):
        (self.s.DATA / "status.json").write_text(json.dumps({"view2": {"a": 1}}))
        self.s.new_team("X")
        reg = json.loads(self.s.TEAMS_FILE.read_text(encoding="utf-8"))
        self.assertEqual(reg["home_name"], "Моя команда")
        reg["home_name"] = "Студия"
        self.s.TEAMS_FILE.write_text(json.dumps(reg, ensure_ascii=False), encoding="utf-8")
        doc, _ = self.s.load_status()
        self.assertEqual(doc["teams"][0]["name"], "Студия")

    def test_team_limit(self):
        for _ in range(self.s.MAX_TEAMS):
            self.assertIsNotNone(self.s.new_team("x"))
        self.assertIsNone(self.s.new_team("y"))


class HttpTest(unittest.TestCase):
    """Сервер целиком: токен в пути, ingest по ключу, страница из файла рядом, чужие пути — 404."""

    def test_routes(self):
        import threading
        import urllib.error
        import urllib.request
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "token").write_text("t" * 32)
            os.environ["RPV_BOARD_DATA"] = str(d / "board")
            os.environ["RPV_BOARD_TOKEN_FILE"] = str(d / "token")
            import server
            s = importlib.reload(server)
            srv = s.ThreadingHTTPServer(("127.0.0.1", 0), s.H)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            base = f"http://127.0.0.1:{srv.server_port}"
            tok = "t" * 32

            def call(path, data=None, headers=None):
                req = urllib.request.Request(base + path, data=data, headers=headers or {}, method="POST" if data is not None else "GET")
                try:
                    with urllib.request.urlopen(req, timeout=5) as r:
                        return r.status, r.read()
                except urllib.error.HTTPError as e:
                    return e.code, e.read()

            try:
                self.assertEqual(call("/status.json")[0], 404)  # без токена — как будто ничего нет
                self.assertEqual(call(f"/{tok}/dispetcher.html")[0], 404)
                self.assertEqual(call(f"/{tok}/old")[0], 404)  # старых страниц в плагине нет
                code, body = call(f"/{tok}/")
                self.assertEqual(code, 200)
                self.assertIn("Диспетчерская".encode(), body)
                self.assertEqual(call(f"/{tok}/status.json")[0], 503)
                code, body = call(f"/{tok}/teams", json.dumps({"name": "Студия"}, ensure_ascii=False).encode("utf-8"))
                team = json.loads(body)
                self.assertEqual((code, team["name"]), (200, "Студия"))
                self.assertEqual(call(f"/{tok}/ingest", b'{"view2": {"b": 2}}', {"X-Board-Key": "rpv_wrong"})[0], 404)
                self.assertEqual(call(f"/{tok}/ingest", b'{"view2": {"b": 2}, "built_at": "10:00"}', {"X-Board-Key": team["key"]})[0], 200)
                code, body = call(f"/{tok}/status.json")
                self.assertEqual(code, 200)
                self.assertEqual(json.loads(body)["teams"][0]["view2"], {"b": 2})
                self.assertEqual(call(f"/{tok}/teams/home/delete", b"{}")[0], 403)
            finally:
                srv.shutdown()
                srv.server_close()


class NoProjectSpecificsTest(unittest.TestCase):
    """В файлах табло не осталось следов проекта-источника."""

    def test_no_residue(self):
        here = Path(__file__).parent
        for f in here.iterdir():
            if f.is_file() and f.suffix in (".py", ".html", ".js", ".css", ".md", ".json", ".service") and not f.name.startswith("test_"):
                self.assertNotIn("alpha", f.read_text(encoding="utf-8").lower(), f.name)


if __name__ == "__main__":
    unittest.main()
