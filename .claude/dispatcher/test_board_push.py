import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import board_push as B

HDR = "---\nid: {id}\ntitle: {t}\nowner: engineer\nstatus: {s}\n---\n\nописание\n\n## Лог\n"


class T(unittest.TestCase):
    def test_build_and_push(self):
        with tempfile.TemporaryDirectory() as d:
            for i, s in enumerate(["in_progress", "waiting", "done"], 1):
                (Path(d) / f"TK-00{i}.md").write_text(HDR.format(id=f"TK-00{i}", t="t", s=s), encoding="utf-8")
            v = B.build_view2(d, now=1000.0)
            self.assertEqual(v["counters"]["run"], 1)
            self.assertEqual(v["counters"]["wait"], 1)
            self.assertEqual(v["waves"][0]["procs"][-1], "TK-003")
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
            threading.Thread(target=srv.handle_request, daemon=True).start()
            self.assertEqual(B.push(f"http://127.0.0.1:{srv.server_port}/x/", "K", v), 200)
            srv.server_close()
            self.assertEqual(got["key"], "K")
            self.assertEqual(got["body"]["view2"]["counters"]["done"], 1)


if __name__ == "__main__":
    unittest.main()
