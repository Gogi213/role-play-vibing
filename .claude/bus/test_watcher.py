import unittest

import watcher as W


class Fake:
    def __init__(self):
        self.units, self.info, self.progress, self.posted = {}, {}, {}, []

    def make(self):
        return W.Watcher("calc", ["tk*"], ["rpv-bus"], "x", post=lambda a, p, i, t: self.posted.append((a, i)),
                         snap=lambda pat: dict(self.units), showf=lambda u: self.info.get(u, {"InvocationID": "inv1"}),
                         prog=lambda pg: dict(self.progress))


class FileMarkers(unittest.TestCase):
    def test_file_appears_once(self):
        import os
        import tempfile
        d = tempfile.mkdtemp()
        lst, marker = os.path.join(d, "watch.list"), os.path.join(d, "DONE")
        with open(lst, "w", encoding="utf-8") as f:
            f.write(marker + "\nrelative\n")
        f = Fake()
        w = W.Watcher("calc", ["tk*"], [], "x", post=lambda a, p, i, t: f.posted.append((a, i)),
                      snap=lambda pat: {}, prog=lambda pg: {}, watch_file=lst)
        self.assertEqual(w.run_once(), [])
        open(marker, "w").close()
        self.assertEqual([e[0] for e in w.run_once()], ["машина.calc.файл.появился"])
        self.assertEqual(w.run_once(), [])


class T(unittest.TestCase):
    def test_clean_stop_and_gc(self):
        f = Fake(); w = f.make()
        f.units = {"tk1-a.service": "active"}
        self.assertEqual(w.run_once(), [])          # baseline / запущен
        f.units = {}                                 # юнит собран GC после успеха
        ev = w.run_once()
        self.assertEqual([e[0] for e in ev], ["машина.calc.юнит.остановлен"])
        self.assertEqual(w.run_once(), [])          # без повторов

    def test_failure_maps_to_ticket(self):
        f = Fake(); w = f.make()
        f.progress = {"tk1-a": {"ticket": "TK-1", "step": "s", "done": 1, "total": 5, "updated": "t1"}}
        f.units = {"tk1-a.service": "active"}
        w.run_once()
        f.units = {"tk1-a.service": "failed"}
        f.info["tk1-a.service"] = {"Result": "exit-code", "ExecMainStatus": "2", "InvocationID": "inv1"}
        ev = w.run_once()
        self.assertEqual([e[0] for e in ev], ["машина.calc.юнит.упал", "задача.TK-1.задание.упало"])
        self.assertEqual(w.run_once(), [])

    def test_failed_between_ticks_and_old_failed_ignored(self):
        f = Fake(); w = f.make()
        f.units = {"tk0-old.service": "failed"}
        self.assertEqual(w.run_once(), [])          # старая неудача — фон
        f.units = {"tk0-old.service": "failed", "tk2-q.service": "failed"}
        f.info["tk2-q.service"] = {"Result": "exit-code", "ExecMainStatus": "1", "InvocationID": "i2"}
        self.assertEqual([e[0] for e in w.run_once()], ["машина.calc.юнит.упал"])
        self.assertEqual(w.run_once(), [])

    def test_exclude(self):
        f = Fake(); w = f.make()
        f.units = {"rpv-bus.service": "active"}
        w.run_once()
        f.units = {}
        self.assertEqual(w.run_once(), [])

    def test_progress_start_step_done(self):
        f = Fake(); w = f.make()
        w.run_once()
        f.progress = {"j": {"ticket": "TK-1", "step": "A", "done": 0, "total": 3, "updated": "u1"}}
        self.assertEqual([e[0] for e in w.run_once()], ["задача.TK-1.задание.старт"])
        f.progress = {"j": {"ticket": "TK-1", "step": "A", "done": 1, "total": 3, "updated": "u2"}}
        self.assertEqual(w.run_once(), [])          # тот же шаг — не шум
        f.progress = {"j": {"ticket": "TK-1", "step": "B", "done": 2, "total": 3, "updated": "u3"}}
        self.assertEqual([e[0] for e in w.run_once()], ["задача.TK-1.задание.ход"])
        f.progress = {"j": {"ticket": "TK-1", "step": "B", "done": 3, "total": 3, "updated": "u4"}}
        self.assertEqual([e[0] for e in w.run_once()], ["задача.TK-1.задание.готово"])
        self.assertEqual(w.run_once(), [])

    def test_progress_done_at_startup_is_baseline(self):
        f = Fake()
        f.progress = {"j": {"ticket": "TK-1", "step": "B", "done": 3, "total": 3, "updated": "u4"}}
        w = f.make()
        self.assertEqual(w.run_once(), [])


if __name__ == "__main__":
    unittest.main()
