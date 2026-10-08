import json
import os
import sys
import tempfile
import unittest
from unittest import mock
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import start as S  # noqa: E402
import supervise as V  # noqa: E402


class _Started:
    def __init__(self, pid):
        self.pid, self.how = pid, "test"


class SuperviseTest(unittest.TestCase):
    def setUp(self):  # другие тесты набора ставят RPV_CI_REPO на уровне модуля — здесь ci_watch по умолчанию не нужен
        p = mock.patch.dict(os.environ)
        p.start()
        self.addCleanup(p.stop)
        os.environ.pop("RPV_CI_REPO", None)

    def test_decide(self):
        self.assertEqual(V.decide(5, True), "ok")
        self.assertEqual(V.decide(700, True), "restart")
        self.assertEqual(V.decide(None, True), "restart")
        self.assertEqual(V.decide(5, False), "start")

    def test_decide_unknown_alive_fresh_heartbeat_is_ok(self):
        """В-209 Д-1: tasklist не ответил (alive=None) при свежем сердцебиении — не start/restart."""
        self.assertEqual(V.decide(5, None), "ok")
        self.assertEqual(V.decide(700, None), "restart")

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
            S.is_ours_state = lambda pid, script: pid > 0   # оба «живы»
            try:
                out = V.run_once(project, now.timestamp(), stop=lambda p, s, unit=None: stopped.append(s) or 1,
                                 spawn=lambda script, proj, log, extra=(): spawned.append(script.name) or _Started(7))
            finally:
                S.is_ours_state = real
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


    def test_ci_watch_supervised_only_with_repo(self):  # TK-090 Д-1
        with tempfile.TemporaryDirectory() as d:
            project = Path(d)
            (project / ".claude" / "dispatcher").mkdir(parents=True)
            spawned = []
            kw = dict(stop=lambda p, s, unit=None: 0,
                      spawn=lambda script, proj, log, extra=(): spawned.append(script.name) or _Started(7))
            with mock.patch.dict(os.environ, {"RPV_CI_REPO": "o/r"}):
                out = V.run_once(project, **kw)
            self.assertEqual(out, {"dispatch": "start", "watch": "start", "ci_watch": "start"})
            self.assertEqual(spawned, ["dispatch.py", "watch.py", "ci_watch.py"])

    def test_load_env_rereads_project_settings(self):  # TK-090 Д-5: settings.json свежее снимка, секреты не берём
        with tempfile.TemporaryDirectory() as d:
            sd = Path(d) / ".claude" / "dispatcher"
            sd.mkdir(parents=True)
            (sd / "supervise.env.json").write_text(json.dumps({"RPV_X_OLD": "1", "RPV_Y": "old"}), encoding="utf-8")
            (sd.parent / "settings.json").write_text(json.dumps(
                {"env": {"RPV_Y": "new", "RPV_CI_REPO": "o/r", "RPV_BUS_TOKEN": "s", "PATH": "x"}}), encoding="utf-8")
            with mock.patch.dict(os.environ, {}, clear=False):
                for k in ("RPV_X_OLD", "RPV_Y", "RPV_CI_REPO", "RPV_BUS_TOKEN"):
                    os.environ.pop(k, None)
                V.load_env(sd)
                self.assertEqual((os.environ["RPV_X_OLD"], os.environ["RPV_Y"], os.environ["RPV_CI_REPO"]),
                                 ("1", "new", "o/r"))
                self.assertNotIn("RPV_BUS_TOKEN", os.environ)
                for k in ("RPV_X_OLD", "RPV_Y", "RPV_CI_REPO"):
                    os.environ.pop(k, None)


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

    def test_schtasks_settings_run_on_battery_and_catch_up(self):
        ps = V.schtasks_settings("rpv-supervise-ab12")[-1]
        for flag in ("-AllowStartIfOnBatteries", "-DontStopIfGoingOnBatteries", "-StartWhenAvailable"):
            self.assertIn(flag, ps)
        self.assertIn("'rpv-supervise-ab12'", ps)

    def test_install_on_windows_applies_battery_settings_after_create(self):
        calls = []
        proj = Path("C:/proj")  # до подмены os.name: на posix WindowsPath не создать
        with mock.patch.object(V.os, "name", "nt"), mock.patch.object(V, "snapshot_env"), \
                mock.patch.object(V, "schtasks_create", return_value=["schtasks", "/Create"]), \
                mock.patch.object(V.subprocess, "run", side_effect=lambda cmd, **kw: calls.append(cmd)):
            V.install(proj)
        self.assertEqual([c[0] for c in calls], ["schtasks", "powershell"])
        calls.clear()
        with mock.patch.object(V.os, "name", "nt"), mock.patch.object(V.subprocess, "run",
                                                                      side_effect=lambda cmd, **kw: calls.append(cmd)):
            V.install(proj, remove=True)
        self.assertEqual([c[:2] for c in calls], [["schtasks", "/Delete"]])

    def test_python_for_service_is_not_pythonw(self):
        real = sys.executable
        try:
            sys.executable = str(Path("C:/Py/pythonw.exe"))
            self.assertTrue(S._python().lower().endswith("python.exe"))
        finally:
            sys.executable = real

    def test_env_snapshot_only_forwarded(self):
        keep = {"RPV_TEST_X": "1", "RPV_DISPATCH_ROTATE_TOKENS": "150000", "ALPHA_DISPATCH_ROTATE_TOKENS": "1",
                "RPV_CONTEXT_WARN_TOKENS": "2", "ALPHA_CONTEXT_WARN_TOKENS": "3", "RPV_DECK_KEY": "C:/k/id_rsa",
                "ALPHA_DECK_KEY": "/k", "RPV_BUS_TOKEN_FILE": "/p/tok"}
        drop = {"ANTHROPIC_API_KEY_TK072": "s", "RPV_BUS_TOKEN": "tok-SECRET", "ALPHA_BUS_TOKEN": "t2",
                "RPV_X_API_KEY": "k", "ALPHA_DB_PASSWORD": "p"}
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {**keep, **drop}):
            V.snapshot_env(Path(d))
            saved = json.loads((Path(d) / "supervise.env.json").read_text(encoding="utf-8"))
        for k, v in keep.items():
            self.assertEqual(saved.get(k), v, k)
        for k in drop:
            self.assertNotIn(k, saved)

    def test_snapshot_skips_settings_keys_so_deleted_key_disappears(self):  # TK-090 Д-5: settings.json — источник правды
        with tempfile.TemporaryDirectory() as d:
            sd = Path(d) / ".claude" / "dispatcher"
            sd.mkdir(parents=True)
            sf = sd.parent / "settings.json"
            sf.write_text(json.dumps({"env": {"RPV_LIVE": "1"}}), encoding="utf-8")
            with mock.patch.dict(os.environ, {"RPV_LIVE": "1", "RPV_ONLY_SNAP": "2"}):
                V.snapshot_env(sd)
            saved = json.loads((sd / "supervise.env.json").read_text(encoding="utf-8"))
            self.assertEqual(saved.get("RPV_ONLY_SNAP"), "2")
            self.assertNotIn("RPV_LIVE", saved)
            sf.write_text(json.dumps({"env": {}}), encoding="utf-8")    # ключ убран из settings.json
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("RPV_LIVE", None)
                V.load_env(sd)
                self.assertNotIn("RPV_LIVE", os.environ)
                os.environ.pop("RPV_ONLY_SNAP", None)

    def test_supervise_and_start_share_one_service_list(self):  # TK-090 Д-1: выпуск версии перезапускает и ci_watch
        with mock.patch.dict(os.environ, {"RPV_CI_REPO": "o/r"}):
            self.assertEqual(S.services(), ("dispatch", "watch", "ci_watch"))
            self.assertTrue(all(n in V.BEATS for n in S.services()))

    def test_session_env_never_snapshotted_and_install_refused_from_role(self):  # TK-090 г
        sess = {"RPV_ROLE": "engineer", "RPV_TICKET": "TK-1", "ALPHA_ROLE": "engineer", "ALPHA_TICKET": "TK-1"}
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {**sess, "RPV_OK": "1"}):
            V.snapshot_env(Path(d))
            saved = json.loads((Path(d) / "supervise.env.json").read_text(encoding="utf-8"))
            self.assertEqual(saved.get("RPV_OK"), "1")
            for k in sess:
                self.assertNotIn(k, saved)
            with mock.patch.object(V, "install") as inst:
                self.assertEqual(V.main(["--install", "--project", d]), 1)
                inst.assert_not_called()


if __name__ == "__main__":
    unittest.main()
