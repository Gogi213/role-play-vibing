import json
import os
import tempfile
import threading
import unittest
import unittest.mock
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import board_push as B

HDR = "---\nid: {id}\ntitle: {t}\nowner: engineer\nstatus: {s}\n---\n\nописание\n\n## Лог\n"


class T(unittest.TestCase):
    def test_build_and_push(self):
        with tempfile.TemporaryDirectory() as d:
            for i, s in enumerate(["in_progress", "waiting", "done"], 1):
                (Path(d) / f"TK-00{i}.md").write_text(HDR.format(id=f"TK-00{i}", t="t", s=s), encoding="utf-8")
            (Path(d) / ".claude" / "tickets").mkdir(parents=True)
            for f in Path(d).glob("TK-*.md"):
                f.rename(Path(d) / ".claude" / "tickets" / f.name)
            v = B.build_view2(Path(d) / ".claude" / "tickets", now=1000.0)
            self.assertEqual(v["counters"]["run"], 1)
            self.assertEqual(v["counters"]["wait"], 1)
            self.assertEqual(v["waves"][0]["procs"], ["TK-001", "TK-002"])  # готовый TK-003 на табло не идёт (как у alpha)
            got = {}

            class H(BaseHTTPRequestHandler):
                def do_POST(self):
                    got["key"] = self.headers.get("X-Board-Key")
                    got["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    self.send_response(200)
                    self.end_headers()

                def log_message(self, *a):
                    pass

            srv = HTTPServer(("127.0.0.1", 0), H)
            threading.Thread(target=lambda: [srv.handle_request() for _ in range(2)], daemon=True).start()
            self.assertEqual(B.push(f"http://127.0.0.1:{srv.server_port}/x/", "K", v), 200)
            self.assertEqual(got["key"], "K")
            got.clear()
            os.environ["RPV_BOARD"] = f"http://127.0.0.1:{srv.server_port}/x/#K2"
            try:
                self.assertEqual(B.main(["--project", d]), 0)
            finally:
                del os.environ["RPV_BOARD"]
            self.assertEqual(got["key"], "K2")
            srv.server_close()
            self.assertEqual(got["body"]["view2"]["counters"]["run"], 1)

    def test_plan_steps_summary_machines(self):
        with tempfile.TemporaryDirectory() as d:
            td = Path(d) / ".claude" / "tickets"
            td.mkdir(parents=True)
            (td / "TK-001.md").write_text(HDR.format(id="TK-001", t="тикет", s="in_progress"), encoding="utf-8")
            pl = Path(d) / ".claude" / "pulse" / "plans"
            pl.mkdir(parents=True)
            (pl / "TK-001.json").write_text(json.dumps({"id": "TK-001", "title": "План", "steps": [
                {"title": "код", "who": "инженер", "on": "pc", "state": "done"},
                {"title": "счёт", "who": "автомат", "on": "calc", "state": "run", "detail": "3 из 9"},
                {"title": "проверка", "who": "судья", "on": "pc", "state": "todo"}]}), encoding="utf-8")
            p = B.build_view2(td, now=1000.0)["processes"][0]
            self.assertEqual((p["steps_total"], p["step_now"], p["state"], p["summary"]), (3, 2, "run", "сделано: код — 1 из 3"))
            self.assertEqual([s["wave"] for s in p["steps"]], [1, 2, 3])
            self.assertEqual([m["id"] for m in B.build_view2(td, now=1000.0)["machines"]], ["pc", "calc"])

    def test_view2_progress_questions_feed(self):
        with tempfile.TemporaryDirectory() as d:
            td = Path(d) / ".claude" / "tickets"
            td.mkdir(parents=True)
            (td / "TK-001.md").write_text(HDR.format(id="TK-001", t="тикет", s="in_progress"), encoding="utf-8")
            (td / "TK-002.md").write_text(HDR.format(id="TK-002", t="второй", s="todo").replace("status: todo", "status: todo\ndepends: TK-001"), encoding="utf-8")
            pulse = Path(d) / ".claude" / "pulse"
            (pulse / "plans").mkdir(parents=True)
            (pulse / "questions").mkdir()
            (pulse / "plans" / "TK-001.json").write_text(json.dumps({"id": "TK-001", "title": "План", "steps": [
                {"title": "код", "who": "инженер", "on": "pc", "state": "done", "finished_at": "2026-10-06T10:00:00+04:00", "detail": "42 файла"},
                {"title": "счёт", "who": "автомат", "on": "calc", "state": "run"}]}), encoding="utf-8")
            (pulse / "questions" / "q-TK-001-1.json").write_text(json.dumps({"id": "q-TK-001-1", "process": "TK-001", "from": "инженер",
                "on": "pc", "text": "Какой вариант?", "options": [{"key": "a", "label": "один", "effect": ""}], "default": "a",
                "since": "2026-10-06T10:05:00+04:00", "answered_at": None}), encoding="utf-8")
            now = 1791266400.0
            v = B.build_view2(td, now=now)
            self.assertEqual(v["progress"]["total"], 2)
            self.assertEqual(v["progress"]["pct"], 50)
            self.assertEqual([q["id"] for q in v["questions"]], ["q-TK-001-1"])
            self.assertEqual(v["headline"]["text"], "ждёт вас: 1")
            by = {p["id"]: p for p in v["processes"]}
            self.assertEqual((by["TK-001"]["wave"], by["TK-002"]["wave"], by["TK-002"]["depends"]), (1, 2, ["TK-001"]))
            self.assertIn("feed", v)


class ExtrasTest(unittest.TestCase):
    """1.7.0: status.json без RPV_BOARD, машины (RPV_MACHINES) и строки (RPV_PLAIN) в кадре."""

    def _project(self, d):
        td = Path(d) / ".claude" / "tickets"
        td.mkdir(parents=True)
        (td / "TK-001.md").write_text(HDR.format(id="TK-001", t="тикет", s="in_progress"), encoding="utf-8")
        pl = Path(d) / ".claude" / "pulse" / "plans"
        pl.mkdir(parents=True)
        (pl / "TK-001.json").write_text(json.dumps({"id": "TK-001", "title": "План", "steps": [
            {"title": "код", "who": "инженер", "on": "pc", "state": "done"},
            {"title": "счёт", "who": "автомат", "on": "calc", "state": "run"}]}), encoding="utf-8")
        return td

    def test_local_status_without_board(self):
        with tempfile.TemporaryDirectory() as d:
            self._project(d)
            os.environ.pop("RPV_BOARD", None)
            os.environ.pop("ALPHA_BOARD", None)
            self.assertEqual(B.main(["--project", d]), 0)
            doc = json.loads((Path(d) / ".claude" / "pulse" / "status.json").read_text(encoding="utf-8"))
            self.assertEqual(sorted(doc), ["built_at", "view2"])
            self.assertEqual(doc["view2"]["counters"]["run"], 1)
            self.assertFalse((Path(d) / ".claude" / "pulse" / "status.json.tmp").exists())

    def test_machines_merged_and_failure_survives(self):
        from unittest import mock
        found = ([{"id": "calc", "state": "ok", "load": "", "orphans": 0, "cpu": 40, "mem": 10, "disk_mb_s": 1.5,
                   "now": {"state": "idle", "text": "простаивает"}},
                  {"id": "srv", "state": "down", "load": "нет связи", "orphans": 0, "cpu": None, "mem": None, "disk_mb_s": None,
                   "now": {"state": "bad", "text": "нет связи"}}],
                 {"srv": {"tag": "SRV", "name": "srv", "color": "teal"}})
        with tempfile.TemporaryDirectory() as d:
            td = self._project(d)
            os.environ["RPV_MACHINES"] = "calc=x,srv=y"
            try:
                with mock.patch("machines.collect", return_value=found):
                    v = B.build_view2(td, now=1000.0)
                by = {m["id"]: m for m in v["machines"]}
                self.assertEqual((by["calc"]["cpu"], by["calc"]["disk_mb_s"], by["calc"]["now"]["state"]), (40, 1.5, "run"))
                self.assertEqual(by["srv"]["state"], "down")
                self.assertIn("srv", v["tags"])
                self.assertEqual(v["tags"]["calc"]["name"], "сервер счёта")  # метка из плана не затёрта
                with mock.patch("machines.collect", side_effect=RuntimeError("ssh")):
                    self.assertEqual(B.build_view2(td, now=1000.0)["counters"]["run"], 1)  # сбой сбора кадр не роняет
            finally:
                del os.environ["RPV_MACHINES"]

    def test_plain_off_by_default_and_on_with_cache(self):
        import plainify
        with tempfile.TemporaryDirectory() as d:
            td = self._project(d)
            base = B.build_view2(td, now=1000.0)["processes"][0]["summary"]
            self.assertEqual(base, "сделано: код — 1 из 2")
            os.environ["RPV_PLAIN"] = "1"
            try:
                p = B.build_view2(td, now=1000.0)["processes"][0]
                (Path(d) / ".claude" / "pulse" / "plain-auto.json").write_text(
                    json.dumps({"items": {plainify.key_of(plainify.payload(p)): "Код готов, идёт счёт"}}), encoding="utf-8")
                plainify._INSTANCES.clear()
                with unittest.mock.patch("plainify.find_claude", return_value=None):
                    self.assertEqual(B.build_view2(td, now=1000.0)["processes"][0]["summary"], "Код готов, идёт счёт — 1 из 2 готово")
            finally:
                del os.environ["RPV_PLAIN"]
                plainify._INSTANCES.clear()


if __name__ == "__main__":
    unittest.main()
