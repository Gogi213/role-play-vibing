import os
import tempfile
import unittest

import bus as B
import busclient
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


class Outbox(unittest.TestCase):
    """TK-221: шина лежит в момент события — оно в постоянном spool, уходит по таймеру сторожа после возврата, без дублей."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.bus = B.Bus(os.path.join(self.d, "b.db"), os.path.join(os.path.dirname(os.path.abspath(__file__)), "routes.json"))
        self.up = False
        self.old_req, self.old_env = busclient.request, {k: os.environ.pop(k, None) for k in ("RPV_BUS_SPOOL", "RPV_BUS_DISABLE")}
        os.environ["RPV_BUS_SPOOL"] = os.path.join(self.d, "sub", "outbox.jsonl")

        def req(path, body=None, timeout=5):
            if not self.up:
                raise OSError("шина лежит")
            return self.bus.post(body["addr"], body.get("payload"), body.get("id"))
        busclient.request = req

    def tearDown(self):
        busclient.request = self.old_req
        for k, v in self.old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def n_events(self):
        return len(self.bus.fetch("dispatcher")) + len(self.bus.fetch("ceo"))

    def test_event_survives_bus_down_and_arrives_once(self):
        units = {"tk1-a.service": "active"}
        w = W.Watcher("calc", ["tk*"], [], "x", snap=lambda pat: dict(units), showf=lambda u: {"InvocationID": "i1"},
                      prog=lambda pg: {}, watch_file=os.path.join(self.d, "none"))
        w.run_once()
        units.clear()
        self.assertEqual([e[0] for e in w.run_once()], ["машина.calc.юнит.остановлен"])   # шина лежит
        self.assertEqual(self.n_events(), 0)
        self.assertFalse(busclient.flush_spool(1))                                       # таймер: шина всё ещё лежит
        with open(busclient.spool_path(), encoding="utf-8") as f:
            self.assertEqual(len(f.read().splitlines()), 1)                              # событие на диске, не потеряно
        self.assertEqual(w.run_once(), [])                                               # повторов от сторожа нет
        self.up = True
        self.assertTrue(busclient.flush_spool(1))                                        # ближайший тик таймера
        self.assertEqual(self.n_events(), 1)
        busclient.post("машина.calc.юнит.остановлен", {"unit": "tk1-a.service", "host": "calc", "result": "success",
                       "exit": "0", "invocation": "i1"}, "unit:calc:tk1-a.service:i1")  # повторная отправка того же id
        self.assertEqual(self.n_events(), 1)                                             # events.id UNIQUE
        self.assertFalse(os.path.exists(busclient.spool_path()))


class T(unittest.TestCase):
    def test_clean_stop_and_gc(self):
        f = Fake(); w = f.make()
        f.units = {"tk1-a.service": "active"}
        self.assertEqual(w.run_once(), [])          # baseline / запущен
        f.units = {}                                 # юнит собран GC после успеха
        ev = w.run_once()
        self.assertEqual([e[0] for e in ev], ["машина.calc.юнит.остановлен"])
        self.assertEqual(ev[0][1]["invocation"], "inv1")
        self.assertEqual(w.run_once(), [])          # без повторов

    def test_start_event_carries_invocation(self):
        f = Fake(); w = f.make()
        w.run_once()                                 # baseline без юнитов
        f.units = {"tk1-a.service": "active"}
        ev = w.run_once()
        self.assertEqual([(e[0], e[1]["invocation"]) for e in ev], [("машина.calc.юнит.запущен", "inv1")])
        self.assertEqual(w.run_once(), [])

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
