import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import start as S  # noqa: E402
import supervise as V  # noqa: E402


class _Started:
    def __init__(self, pid):
        self.pid, self.how = pid, "test"


class SuperviseTest(unittest.TestCase):
    def test_decide(self):
        self.assertEqual(V.decide(5, True), "ok")
        self.assertEqual(V.decide(700, True), "restart")
        self.assertEqual(V.decide(None, True), "restart")
        self.assertEqual(V.decide(5, False), "start")

    def test_age(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "hb.json"
            now = datetime.now().astimezone()
            f.write_text(json.dumps({"ts": (now - timedelta(seconds=90)).isoformat()}), encoding="utf-8")
            self.assertAlmostEqual(V.heartbeat_age(f, "ts", now.timestamp()), 90, delta=1)
            self.assertIsNone(V.heartbeat_age(Path(d) / "none.json", "ts", now.timestamp()))
            f.write_text("{", encoding="utf-8")
            self.assertIsNone(V.heartbeat_age(f, "ts", now.timestamp()))

    def test_run_once_restarts_only_dead_or_stale(self):
        with tempfile.TemporaryDirectory() as d:
            project = Path(d)
            sd = project / ".claude" / "dispatcher"
            sd.mkdir(parents=True)
            now = datetime.now().astimezone()
            (sd / "state.json").write_text(json.dumps({"last_tick": now.isoformat()}), encoding="utf-8")  # свежий
            (sd / "watch-heartbeat.json").write_text(json.dumps({"ts": (now - timedelta(hours=1)).isoformat()}), encoding="utf-8")
            (sd / "dispatch.pid").write_text(str(os.getpid()), encoding="utf-8")
            (sd / "watch.pid").write_text(str(os.getpid()), encoding="utf-8")
            stopped, spawned = [], []
            real = S.is_ours
            S.is_ours = lambda pid, script: pid > 0   # оба «живы»
            try:
                out = V.run_once(project, now.timestamp(), stop=lambda p, s, unit=None: stopped.append(s) or 1,
                                 spawn=lambda script, proj, log, extra=(): spawned.append(script.name) or _Started(7))
            finally:
                S.is_ours = real
            self.assertEqual(out, {"dispatch": "ok", "watch": "restart"})
            self.assertEqual((stopped, spawned), (["watch.py"], ["watch.py"]))
            self.assertIn("watch: restart", (sd / "supervise.log").read_text(encoding="utf-8"))

    def test_missing_process_is_started(self):
        with tempfile.TemporaryDirectory() as d:
            project = Path(d)
            (project / ".claude" / "dispatcher").mkdir(parents=True)
            spawned = []
            out = V.run_once(project, stop=lambda p, s, unit=None: 0,
                             spawn=lambda script, proj, log, extra=(): spawned.append(script.name) or _Started(7))
            self.assertEqual(out, {"dispatch": "start", "watch": "start"})
            self.assertEqual(spawned, ["dispatch.py", "watch.py"])


class InstallFilesTest(unittest.TestCase):
    P = Path("/p/proj")

    def test_systemd_timer_every_5_min(self):
        f = V.systemd_files("rpv-supervise-ab12", "/usr/bin/python3", Path("/c/supervise.py"), self.P)
        self.assertIn("OnUnitActiveSec=5min", f["rpv-supervise-ab12.timer"])
        self.assertIn(f'ExecStart="/usr/bin/python3" "{Path("/c/supervise.py")}" --project "{self.P}"', f["rpv-supervise-ab12.service"])

    def test_launchd_interval(self):
        x = V.launchd_plist("dev.rpv.x", "/usr/bin/python3", Path("/c/supervise.py"), self.P)
        self.assertIn("<key>StartInterval</key><integer>300</integer>", x)
        self.assertIn(f"<string>--project</string><string>{self.P}</string>", x)

    def test_schtasks_full_path_no_semicolons(self):
        cmd = V.schtasks_create("rpv-supervise-ab12", "C:/Py/python.exe", Path("C:/c/supervise.py"), Path("C:/proj"))
        self.assertEqual(cmd[:6], ["schtasks", "/Create", "/F", "/TN", "rpv-supervise-ab12", "/SC"])
        self.assertIn("/MO", cmd)
        self.assertEqual(cmd[cmd.index("/MO") + 1], "5")
        self.assertTrue(cmd[-1].startswith(f'"{Path("C:/Py")}'))
        self.assertIn(f'--project "{Path("C:/proj")}"', cmd[-1])

    def test_python_for_service_is_not_pythonw(self):
        real = sys.executable
        try:
            sys.executable = str(Path("C:/Py/pythonw.exe"))
            self.assertTrue(S._python().lower().endswith("python.exe"))
        finally:
            sys.executable = real

    def test_env_snapshot_only_forwarded(self):
        with tempfile.TemporaryDirectory() as d:
            os.environ["RPV_TEST_X"], os.environ["ANTHROPIC_API_KEY_TK072"] = "1", "secret"
            try:
                V.snapshot_env(Path(d))
                saved = json.loads((Path(d) / "supervise.env.json").read_text(encoding="utf-8"))
            finally:
                del os.environ["RPV_TEST_X"], os.environ["ANTHROPIC_API_KEY_TK072"]
            self.assertEqual(saved.get("RPV_TEST_X"), "1")
            self.assertNotIn("ANTHROPIC_API_KEY_TK072", saved)


if __name__ == "__main__":
    unittest.main()
