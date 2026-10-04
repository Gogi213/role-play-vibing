import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

import bus as B

ROUTES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "routes.json")


class Core(unittest.TestCase):
    def setUp(self):
        self.t = [1000.0]
        self.d = tempfile.mkdtemp()
        self.bus = B.Bus(os.path.join(self.d, "b.db"), ROUTES, stale_after=600, clock=lambda: self.t[0])

    def addrs(self, rcp, after=0):
        return [e["addr"] for e in self.bus.fetch(rcp, after)]

    def test_dedup_and_order(self):
        a = self.bus.post("задача.TK-1.задание.готово", eid="x")
        b = self.bus.post("задача.TK-1.задание.готово", eid="x")
        self.assertEqual(a["seq"], b["seq"])
        self.assertTrue(b["dup"])
        self.bus.post("задача.TK-2.задание.готово")
        self.assertEqual([e["seq"] for e in self.bus.fetch("dispatcher")], [1, 2])

    def test_ack_and_redelivery(self):
        self.bus.post("сборка.готова")
        self.assertEqual(len(self.bus.fetch("dispatcher")), 1)
        self.assertEqual(len(self.bus.fetch("dispatcher")), 1)
        self.assertEqual(self.bus.ack("dispatcher", [1]), 1)
        self.assertEqual(self.bus.fetch("dispatcher"), [])
        self.assertEqual(self.bus.ack("dispatcher", [1]), 0)

    def test_routing_failed_goes_to_ceo(self):
        self.bus.post("задача.TK-1.задание.упало")
        self.bus.post("задача.TK-1.задание.ход")
        self.assertEqual(self.addrs("ceo"), ["задача.TK-1.задание.упало"])
        self.assertEqual(self.addrs("dispatcher"), ["задача.TK-1.задание.упало"])
        self.assertEqual(self.addrs("board"), [])

    def test_unrouted_is_journaled_only(self):
        r = self.bus.post("что.то.левое")
        self.assertEqual(r["seq"], 1)
        self.assertEqual(self.bus.health()["queues"], {})

    def test_hold_and_release(self):
        self.bus.post("задача.TK-9.блокер.поставлен", {"reason": "depends TK-3"})
        self.bus.post("задача.TK-9.задание.готово")
        self.bus.post("задача.TK-8.задание.готово")
        self.assertEqual(len(self.bus.fetch("dispatcher")), 1)
        self.assertEqual(self.bus.health()["queues"]["dispatcher.held"], 1)
        self.bus.post("задача.TK-9.блокер.снят")
        self.assertEqual(self.addrs("dispatcher"), ["задача.TK-9.задание.готово", "задача.TK-8.задание.готово"])

    def test_hold_does_not_block_ceo_question(self):
        self.bus.post("задача.TK-9.блокер.поставлен", {"reason": "вопрос"})
        self.bus.post("задача.TK-9.вопрос_владельцу")
        self.assertEqual(self.addrs("ceo"), ["задача.TK-9.вопрос_владельцу"])

    def test_to_ceo_is_not_owner_question(self):
        self.bus.post("задача.TK-9.к_ceo")
        self.assertEqual(self.addrs("ceo"), ["задача.TK-9.к_ceo"])

    def test_snapshot_replaces_blockers(self):
        self.bus.post("задача.TK-1.блокер.поставлен")
        self.bus.post("задача.TK-1.задание.готово")
        self.bus.post(B.SNAPSHOT_ADDR, {"blocked": {"TK-2": "x"}})
        self.assertEqual(self.addrs("dispatcher"), ["задача.TK-1.задание.готово"])
        self.assertEqual(self.bus.health()["blocked"], {"TK-2": "x"})

    def test_long_poll_wakes(self):
        out = []
        th = threading.Thread(target=lambda: out.extend(self.bus.fetch("dispatcher", 0, 5)))
        th.start()
        time.sleep(0.2)
        t0 = time.monotonic()
        self.bus.post("сборка.упала")
        th.join(3)
        self.assertEqual(len(out), 1)
        self.assertLess(time.monotonic() - t0, 1)

    def test_long_poll_times_out_empty(self):
        t0 = time.monotonic()
        self.assertEqual(self.bus.fetch("dispatcher", 0, 0.3), [])
        self.assertGreaterEqual(time.monotonic() - t0, 0.25)

    def test_stale_flag_once_and_not_for_held(self):
        self.bus.post("сборка.готова")
        self.bus.post("задача.TK-5.блокер.поставлен")
        self.bus.post("задача.TK-5.сдано")
        self.assertEqual(self.bus.scan_stale(), 0)
        self.t[0] += 601
        self.assertEqual(self.bus.scan_stale(), 1)  # сборка: dispatcher; сдано TK-5 у dispatcher held
        self.assertEqual(self.bus.scan_stale(), 0)
        self.assertEqual(len(self.addrs("ceo")), 1)
        self.assertEqual({(s["recipient"], s["seq"]) for s in self.bus.stale()}, {("dispatcher", 1)})
        self.bus.ack("dispatcher", [1])
        self.assertNotIn(("dispatcher", 1), {(s["recipient"], s["seq"]) for s in self.bus.stale()})

    def test_persistence(self):
        self.bus.post("сборка.готова")
        b2 = B.Bus(os.path.join(self.d, "b.db"), ROUTES)
        self.assertEqual(len(b2.fetch("dispatcher")), 1)


class Http(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.bus = B.Bus(os.path.join(d, "b.db"), ROUTES)
        self.srv = B.serve(self.bus, "tok", "127.0.0.1", 0)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def call(self, path, body=None, token="tok"):
        req = urllib.request.Request(self.url + path, data=None if body is None else json.dumps(body).encode(),
                                     headers={"Authorization": "Bearer " + token})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())

    def test_flow(self):
        with self.assertRaises(urllib.error.HTTPError) as c:
            self.call("/q/dispatcher", token="bad")
        self.assertEqual(c.exception.code, 401)
        self.assertEqual(self.call("/health", token="bad"), {"ok": True})
        self.assertEqual(self.call("/event", {"addr": "машина.calc.юнит.упал", "id": "e1"})["seq"], 1)
        ev = self.call("/q/dispatcher?after=0&wait=2")["events"]
        self.assertEqual(ev[0]["addr"], "машина.calc.юнит.упал")
        self.assertEqual(self.call("/ack", {"recipient": "dispatcher", "seqs": [1]}), {"acked": 1})
        self.assertEqual(self.call("/q/dispatcher?wait=0")["events"], [])
        with self.assertRaises(urllib.error.HTTPError) as c:
            self.call("/event", {"payload": {}})
        self.assertEqual(c.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
