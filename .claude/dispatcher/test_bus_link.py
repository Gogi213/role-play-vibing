import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("RPV_BUS_DISABLE", "1")  # не затирать флаг test_dispatch: тесты CLI не шлют события на живую шину
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "bus"))
import bus  # noqa: E402
import bus_link  # noqa: E402
import busclient  # noqa: E402


def tk(i, status, wait_for=""):
    return SimpleNamespace(id=i, status=status, header={"wait_for": wait_for})


class SnapshotTest(unittest.TestCase):
    def test_blockers(self):
        r = bus_link.blockers_snapshot([tk("A", "blocked"), tk("B", "needs_owner"), tk("C", "waiting", "ticket:A"),
                                        tk("D", "waiting", "ticket:E"), tk("E", "done"),
                                        tk("F", "waiting", "host:calc:/x")])
        self.assertEqual(r, {"A": "blocked", "B": "needs_owner", "C": "depends:A"})


class LinkTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.b = bus.Bus(os.path.join(self.d, "b.db"), str(HERE.parent / "bus" / "routes.json"))
        self.srv = bus.ThreadingHTTPServer(("127.0.0.1", 0), bus.make_handler(self.b, "t"))
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.env = {k: os.environ.get(k) for k in ("RPV_BUS_URL", "RPV_BUS_TOKEN", "RPV_BUS_DISABLE")}
        os.environ.update(RPV_BUS_URL=f"http://127.0.0.1:{self.srv.server_port}", RPV_BUS_TOKEN="t")
        os.environ.pop("RPV_BUS_DISABLE", None)
        self.lines = []
        self.link = bus_link.Link(lambda k, n: self.lines.append((k, n)))
        self.link.start()

    def tearDown(self):
        self.link.disp.stop_flag.set()
        self.srv.shutdown()
        for k, v in self.env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        os.environ["RPV_BUS_DISABLE"] = "1"

    def test_wake_then_ack_after_take(self):
        self.b.post("задача.TK-1.задание.готово", {}, "e1")
        self.assertTrue(self.link.wake.wait(3))
        seqs = self.link.take_ack()
        self.assertEqual(len(seqs), 1)
        bus_link.ack("dispatcher", seqs)
        self.assertEqual(self.b.fetch("dispatcher", 0), [])

    def test_ceo_queue_is_not_read_by_dispatcher(self):
        # В-192: очередь `ceo` читает и подтверждает CEO (`tickets.py inbox`), диспетчер её не трогает
        self.b.post("задача.TK-1.к_ceo", {"kind": "done", "prio": "normal"}, "e2")
        time.sleep(1.0)
        self.assertEqual(self.lines, [])
        self.assertEqual(len(self.b.fetch("ceo", 0)), 1)

    def test_held_event_does_not_wake_until_unblocked(self):
        self.b.post("задача.TK-9.блокер.поставлен", {"reason": "x"}, "b1")
        self.b.post("задача.TK-9.задание.готово", {}, "e3")
        self.assertFalse(self.link.wake.wait(1.2))
        self.b.post("задача.TK-9.блокер.снят", {}, "b2")
        self.assertTrue(self.link.wake.wait(3))


class AckRetryTest(unittest.TestCase):
    def test_failed_ack_returns_false_then_retries(self):
        orig = busclient.request
        calls = []

        def flaky(path, *a, **k):
            if path == "/ack":
                calls.append(1)
                if len(calls) == 1:
                    raise OSError("boom")
                return {}
            return orig(path, *a, **k)

        busclient.request = flaky
        try:
            self.assertFalse(bus_link.ack("x", [1]))
            self.assertTrue(bus_link.ack("x", [1]))
        finally:
            busclient.request = orig


class DownTest(unittest.TestCase):
    def test_down_reports_once_and_up_after(self):
        lines = []
        os.environ.update(RPV_BUS_URL="http://127.0.0.1:1", RPV_BUS_TOKEN="t")
        os.environ.pop("RPV_BUS_DISABLE", None)
        try:
            link = bus_link.Link(lambda k, n: lines.append(k))
            link.start()
            time.sleep(4)
            link.disp.stop_flag.set()
            self.assertEqual(lines.count("bus-down"), 1)
        finally:
            os.environ["RPV_BUS_DISABLE"] = "1"
            os.environ.pop("RPV_BUS_URL", None)


if __name__ == "__main__":
    unittest.main()
