"""Короткий прогон на выносливость (TK-076 п.2): по раунду на сбой — 0 потерь, 0 blocked, простой в пределах."""
import sys
import time
import tempfile
import unittest
from pathlib import Path

import endurance


class EnduranceShort(unittest.TestCase):
    def test_base_round(self):
        self.assertEqual(endurance.main(["--modes", "base", "--idle-max", "30"]), 0)

    def test_bus_round(self):
        self.assertEqual(endurance.main(["--modes", "bus", "--idle-max", "30"]), 0)

    def test_reboot_round(self):
        self.assertEqual(endurance.main(["--modes", "reboot", "--idle-max", "30"]), 0)

    def test_watch_round(self):
        self.assertEqual(endurance.main(["--modes", "watch", "--idle-max", "30"]), 0)

    def test_close_kills_everything_started(self):  # №14/9: исключение в раунде не оставляет сторожа/шину/диспетчера
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            h = endurance.Harness(Path(tmp), 30, 1)
            procs = [subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"]) for _ in range(3)]
            self.addCleanup(lambda: [p.kill() for p in procs if p.poll() is None])
            h.proc, h.watch, h.bus = procs
            h.close()
            h.log_fh.close()
            self.assertTrue(all(p.poll() is not None for p in procs))

    def test_inbox_pages_past_100_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            h = endurance.Harness(Path(tmp), 30, 1)
            h.start_bus()
            try:
                sys.path.insert(0, str(endurance.HERE.parent / "bus"))
                import busclient
                end = time.time() + 60  # на медленном раннере (macOS) шина слушает порт позже 15 с ожидания start_bus
                while busclient.post("задача.TK-0.к_ceo", {"kind": "done"}, "pg-0", timeout=5, spool=False) is None:
                    self.assertLess(time.time(), end, "шина не ответила за 60 с")
                    time.sleep(1)
                for i in range(1, 130):
                    self.assertIsNotNone(busclient.post(f"задача.TK-{i}.к_ceo", {"kind": "done"}, f"pg-{i}", timeout=15, spool=False))
                self.assertIn("TK-129 [done]", h.inbox(timeout=15))
            finally:
                h.kill_bus()
                h.log_fh.close()


if __name__ == "__main__":
    unittest.main()
