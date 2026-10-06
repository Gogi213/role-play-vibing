import json
import os
import subprocess
import unittest

import machines as M

os.environ["RPV_PC"] = "0"

OUT = """up 1000.5
cpu 5000 4000
sect 2000
mem 8000000 2000000
job calc-1 {"done": 231, "total": 492, "step": "сверка", "unit": "монето-месяцев"}
job done-1 {"done": 5, "total": 5}
job broken {not json
"""
OUT2 = OUT.replace("up 1000.5", "up 1010.5").replace("cpu 5000 4000", "cpu 5100 4050").replace("sect 2000", "sect 22000")


class SpecTest(unittest.TestCase):
    def test_parse_spec(self):
        self.assertEqual(M.parse_spec("a=user@h1, b=h2 ,c=!h3,bad,=x,d=,a=again"),
                         [("a", "user@h1", False), ("b", "h2", False), ("c", "h3", True)])
        self.assertEqual(M.parse_spec(None), [])
        self.assertEqual(M.parse_spec(""), [])

    def test_empty_without_env(self):
        old = os.environ.pop("RPV_MACHINES", None)
        try:
            self.assertEqual(M.collect(lambda *a: self.fail("ssh без RPV_MACHINES")), ([], {}))
        finally:
            if old is not None:
                os.environ["RPV_MACHINES"] = old


class ParseTest(unittest.TestCase):
    def test_parse_output(self):
        s = M.parse_output(OUT)
        self.assertEqual((s["up"], s["cpu"], s["sect"], s["mem"]), (1000.5, (5000.0, 4000.0), 2000.0, (8000000.0, 2000000.0)))
        self.assertEqual(sorted(s["jobs"]), ["calc-1", "done-1"])  # битый JSON пропущен
        self.assertIsNone(M.parse_output("up 1\ncpu 1 1\n"))  # не хватает строк
        self.assertIsNone(M.parse_output("up x\n"))
        self.assertIsNone(M.parse_output(""))

    def test_view_first_and_delta(self):
        s1, s2 = M.parse_output(OUT), M.parse_output(OUT2)
        v1 = M.machine_view("calc", s1)
        self.assertEqual((v1["state"], v1["mem"], v1["cpu"], v1["disk_mb_s"]), ("ok", 75, None, None))
        self.assertEqual(v1["now"], {"state": "run", "text": "сверка 231/492 монето-месяцев"})
        v2 = M.machine_view("calc", s2, (s1["up"], s1["cpu"][0], s1["cpu"][1], s1["sect"]))
        self.assertEqual(v2["cpu"], 50)  # 100 общих тиков, 50 из них простой
        self.assertEqual(v2["disk_mb_s"], round(20000 * 512 / 10 / 1e6, 1))
        self.assertEqual(set(v2), {"id", "state", "load", "now", "orphans", "cpu", "mem", "disk_mb_s"})

    def test_down_off_idle(self):
        self.assertEqual(M.machine_view("x", None)["state"], "down")
        self.assertEqual(M.machine_view("x", None, off=True)["state"], "off")
        idle = M.parse_output("up 1\ncpu 2 1\nsect 0\nmem 10 5\n")
        self.assertEqual(M.machine_view("x", idle)["now"]["state"], "idle")


class CollectTest(unittest.TestCase):
    def setUp(self):
        self.old = {k: os.environ.get(k) for k in ("RPV_MACHINES", "RPV_PROGRESS_DIR", "RPV_DECK_KEY", "RPV_DECK_KNOWN_HOSTS")}
        M._PREV.clear()

    def tearDown(self):
        for k, v in self.old.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v
        M._PREV.clear()

    def test_collect_tags_down_and_off(self):
        os.environ["RPV_MACHINES"] = "a=h1,b=h2,c=!h3"
        os.environ["RPV_PROGRESS_DIR"] = "/prog"
        asked = []

        def sampler(target, pdir):
            asked.append((target, pdir))
            return M.parse_output(OUT) if target == "h1" else None

        machines, tags = M.collect(sampler)
        self.assertEqual(asked, [("h1", "/prog"), ("h2", "/prog")])  # выключенную не опрашиваем
        self.assertEqual([(m["id"], m["state"]) for m in machines], [("a", "ok"), ("b", "down"), ("c", "off")])
        self.assertEqual(set(tags), {"a", "b", "c"})
        self.assertEqual(tags["a"]["tag"], "A")
        self.assertIn("a", M._PREV)

    def test_ssh_command_and_errors(self):
        os.environ["RPV_DECK_KEY"] = "/k"
        os.environ["RPV_DECK_KNOWN_HOSTS"] = "/kh"
        seen = {}

        def run(cmd, **kw):
            seen["cmd"], seen["input"] = cmd, kw["input"]
            return subprocess.CompletedProcess(cmd, 0, stdout=OUT, stderr="")

        self.assertIsNotNone(M.ssh_sample("u@h", "/prog", run))
        c = seen["cmd"]
        self.assertIn("BatchMode=yes", c)
        self.assertEqual(c[c.index("-i") + 1], "/k")
        self.assertIn("UserKnownHostsFile=/kh", c)
        self.assertEqual(c[-4:], ["sh", "-s", "--", "/prog"])
        self.assertIn("/proc/diskstats", seen["input"])
        self.assertIsNone(M.ssh_sample("h", "", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 255, stdout="", stderr="x")))

        def boom(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, 8)

        self.assertIsNone(M.ssh_sample("h", "", boom))
        self.assertIsNone(M.ssh_sample("h", "", lambda cmd, **kw: (_ for _ in ()).throw(FileNotFoundError("ssh"))))


class MergeTest(unittest.TestCase):
    def test_merge(self):
        v2 = {"machines": [{"id": "pc", "state": "ok", "load": "инженер · задача", "now": {"state": "run", "text": "код"},
                            "orphans": 0, "cpu": None, "mem": None, "disk_mb_s": None}], "tags": {"pc": {"tag": "ПК"}}}
        new = [{"id": "pc", "state": "ok", "load": "", "now": {"state": "idle", "text": "простаивает"}, "orphans": 0,
                "cpu": 7, "mem": 30, "disk_mb_s": 0.0},
               {"id": "s", "state": "down", "load": "нет связи", "now": {"state": "bad", "text": "нет связи"}, "orphans": 0,
                "cpu": None, "mem": None, "disk_mb_s": None}]
        M.merge(v2, new, {"pc": {"tag": "PC"}, "s": {"tag": "S"}})
        self.assertEqual([m["id"] for m in v2["machines"]], ["pc", "s"])
        pc = v2["machines"][0]
        self.assertEqual((pc["cpu"], pc["mem"], pc["load"], pc["now"]["text"]), (7, 30, "инженер · задача", "код"))  # план главнее
        self.assertEqual(v2["tags"], {"pc": {"tag": "ПК"}, "s": {"tag": "S"}})
        json.dumps(v2)


if __name__ == "__main__":
    unittest.main()


class PcTest(unittest.TestCase):
    def test_pc_view_and_collect(self):
        v = M.pc_view()
        self.assertEqual(v["id"], "pc")
        for k in ("cpu", "mem"):
            self.assertTrue(v[k] is None or 0 <= v[k] <= 100)
        os.environ["RPV_PC"] = "1"
        try:
            ms, tags = M.collect(lambda *a: None)
        finally:
            os.environ["RPV_PC"] = "0"
        self.assertEqual([m["id"] for m in ms], ["pc"])
        self.assertIn("pc", tags)
