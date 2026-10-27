"""/rpv-update (В-13): порядок шагов с подменённым `claude`; PATH в plist присмотра и проверка doctor (П3)."""
import io
import plistlib
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import doctor as D  # noqa: E402
import supervise as SV  # noqa: E402
import update as U  # noqa: E402

PROJ = Path("/p/proj")
DOCTOR_FAIL = "OK    сторож     pid 1\nFAIL  диспетчер  не запущен (pid-файл: нет)\nFAIL  простой    26 мин из 10"


def _entry(version, scope="user", project=None):
    e = {"scope": scope, "version": version, "installPath": f"/cache/{version}"}
    if project:
        e["projectPath"] = str(project)
    return {"plugins": {U.PLUGIN: [e]}}


class Fake:
    """Подмена run/installed: помнит команды; `claude plugin update` меняет версию в installed_plugins.json."""

    def __init__(self, old="2.0.9", new="2.1.0", doctor=(0, "OK  всё"), fail_on=None):
        self.cmds, self.version, self.new, self.doctor, self.fail_on = [], old, new, doctor, fail_on

    def installed(self):
        return _entry(self.version)

    def run(self, cmd, timeout):
        self.cmds.append(cmd)
        if self.fail_on and self.fail_on in cmd:
            return 1, "boom\nпровал"
        if "update" in cmd and cmd[1:3] == ["plugin", "update"]:
            self.version = self.new
        if cmd[1:2] and cmd[1].endswith("doctor.py"):
            return self.doctor
        return 0, "ok"

    def steps(self):
        out = []
        for c in self.cmds:
            if c[0] == "claude":
                out.append(" ".join(c[1:4]))
            else:
                out.append(Path(c[1]).name + (" " + c[c.index("charter")] if "charter" in c else ""))
        return out


def _update(fake, **kw):
    with mock.patch.object(U.Path, "is_file", return_value=True):
        return U.update(PROJ, run=fake.run, installed=fake.installed, claude="claude", python="py", **kw)


class UpdateOrder(unittest.TestCase):
    def test_steps_in_order_from_new_version(self):
        f = Fake()
        rows, new = _update(f, ref="В-13")
        self.assertEqual(new, "2.1.0")
        self.assertEqual(f.steps(), ["plugin marketplace update", "plugin update role-play-vibing@role-play-vibing",
                                     "start.py", "supervise.py", "tickets.py charter", "doctor.py"])
        for c in f.cmds[2:]:
            self.assertTrue(c[1].replace("\\", "/").startswith("/cache/2.1.0/"), c)
        sup = next(c for c in f.cmds if c[1].endswith("supervise.py"))
        self.assertIn("--install", sup)
        charter = next(c for c in f.cmds if "charter" in c)
        self.assertEqual(charter[charter.index("--ref") + 1], "В-13")
        self.assertEqual([r[1] for r in rows], [D.OK] * len(rows))
        self.assertEqual(rows[1][2], "2.0.9 → 2.1.0")

    def test_scope_of_install_is_passed(self):
        f = Fake()
        f.installed = lambda: _entry(f.version, "project", PROJ)
        _update(f, ref="В-13")
        upd = next(c for c in f.cmds if c[1:3] == ["plugin", "update"])
        self.assertEqual(upd[-2:], ["--scope", "project"])

    def test_already_latest_still_checks_services_skips_charter(self):
        f = Fake(old="2.0.9", new="2.0.9")
        rows, _ = _update(f, ref="В-13")
        self.assertEqual(f.steps(), ["plugin marketplace update", "plugin update role-play-vibing@role-play-vibing",
                                     "start.py", "supervise.py", "doctor.py"])
        self.assertIn("уже последняя версия", dict((r[0], r[2]) for r in rows)["плагин"])
        self.assertEqual(dict((r[0], r[1]) for r in rows)["уставы"], D.OFF)

    def test_no_ref_charter_is_fail_not_silent(self):
        f = Fake()
        rows, _ = _update(f)
        self.assertNotIn("charter", " ".join(f.steps()))
        st = {r[0]: r[1] for r in rows}
        self.assertEqual(st["уставы"], D.FAIL)
        self.assertEqual(st["doctor"], D.OK)

    def test_doctor_fail_is_named_not_green(self):
        f = Fake(doctor=(1, DOCTOR_FAIL))
        rows, _ = _update(f, ref="В-13")
        doc = next(r for r in rows if r[0] == "doctor")
        self.assertEqual(doc[1], D.FAIL)
        self.assertIn("диспетчер", doc[2])
        self.assertIn("простой", doc[2])
        self.assertIn(DOCTOR_FAIL, next(r for r in rows if r[0] == "doctor, вывод")[2])

    def test_plugin_update_failure_stops_before_services(self):
        f = Fake(fail_on="update")
        rows, _ = _update(f, ref="В-13")
        self.assertEqual(f.steps(), ["plugin marketplace update"])
        self.assertEqual(rows[-1][:2], ("marketplace", D.FAIL))

    def test_install_entry_prefers_project_then_user(self):
        both = {"plugins": {U.PLUGIN: [{"scope": "user", "version": "1"}, {"scope": "project", "version": "2",
                                                                          "projectPath": str(PROJ)}]}}
        self.assertEqual(U.install_entry(both, PROJ)["version"], "2")
        self.assertEqual(U.install_entry(both, Path("/other"))["version"], "1")
        self.assertIsNone(U.install_entry({}, PROJ))


class MainLines(unittest.TestCase):
    def _main(self, running, rows, new="2.1.0"):
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as d, redirect_stdout(out), \
                mock.patch.object(U.P, "resolve_project", return_value=d), \
                mock.patch.object(U.shutil, "which", return_value="claude"), \
                mock.patch.object(U.charter, "plugin_version", return_value=running), \
                mock.patch.object(U, "update", return_value=(rows, new)):
            rc = U.main(["--ref", "В-13"])
        return rc, out.getvalue()

    def test_restart_line_and_green(self):
        rc, text = self._main("2.0.9", [("плагин", D.OK, "2.0.9 → 2.1.0")])
        self.assertEqual(rc, 0)
        self.assertIn(U.RESTART, text)
        self.assertIn("Готово", text)

    def test_fail_row_is_not_green(self):
        rc, text = self._main("2.1.0", [("doctor", D.FAIL, "FAIL: сторож")])
        self.assertEqual(rc, 1)
        self.assertIn("НЕ ГОТОВО", text)
        self.assertNotIn("Готово:", text)
        self.assertNotIn(U.RESTART, text)


class PlistPath(unittest.TestCase):
    ENV = {"CLAUDE_BIN": "/opt/homebrew/bin/claude"}

    def test_plist_has_path_and_claude_bin(self):
        x = SV.launchd_plist("dev.rpv.x", "/usr/bin/python3", Path("/c/supervise.py"), PROJ,
                             env=SV.launchd_env("/usr/bin/python3", environ=self.ENV))
        env = plistlib.loads(x.encode())["EnvironmentVariables"]
        self.assertEqual(env["PATH"].split(":"), ["/opt/homebrew/bin", "/usr/bin", "/usr/local/bin", "/bin"])
        self.assertEqual(env["CLAUDE_BIN"], "/opt/homebrew/bin/claude")

    def test_path_from_which_and_no_claude_bin_when_unset(self):
        env = SV.launchd_env("/opt/py/bin/python3", which=lambda n: "/home/u/.local/bin/claude", environ={})
        self.assertEqual(env["PATH"], "/home/u/.local/bin:/opt/py/bin:/usr/local/bin:/usr/bin:/bin")
        self.assertNotIn("CLAUDE_BIN", env)

    def test_default_plist_carries_environment(self):
        x = SV.launchd_plist("dev.rpv.x", "/usr/bin/python3", Path("/c/supervise.py"), PROJ)
        self.assertIn("<key>PATH</key>", plistlib.loads(x.encode())["EnvironmentVariables"] and x)

    def test_values_are_xml_escaped(self):
        x = SV.launchd_plist("dev.rpv.x", "py", Path("/c/s.py"), PROJ, env={"PATH": "/a&b:/c"})
        self.assertEqual(plistlib.loads(x.encode())["EnvironmentVariables"]["PATH"], "/a&b:/c")


class DoctorPlist(unittest.TestCase):
    def _check(self, env, which, claude_bin=None):
        d = {"Label": "x", "ProgramArguments": ["py"]}
        if env is not None:
            d["EnvironmentVariables"] = env
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "dev.rpv.x.plist"
            p.write_bytes(plistlib.dumps(d))
            return D.check_launchd_path(p, which=which)

    def test_plist_without_path_is_fail(self):
        self.assertEqual(self._check(None, lambda *a, **k: "/x/claude")[1], D.FAIL)
        self.assertEqual(self._check({"CLAUDE_BIN": "/x"}, lambda *a, **k: "/x/claude")[1], D.FAIL)

    def test_claude_not_on_path_is_fail(self):
        row = self._check({"PATH": "/usr/bin:/bin"}, lambda *a, **k: None)
        self.assertEqual(row[1], D.FAIL)
        self.assertIn("/usr/bin:/bin", row[2])

    def test_path_with_claude_is_ok(self):
        seen = {}

        def which(name, path=None):
            seen["path"] = path
            return "/opt/homebrew/bin/claude"

        self.assertEqual(self._check({"PATH": "/opt/homebrew/bin:/usr/bin"}, which)[1], D.OK)
        self.assertEqual(seen["path"], "/opt/homebrew/bin:/usr/bin")

    def test_no_plist_nothing_to_check(self):
        self.assertIsNone(D.check_launchd_path(Path("/no/such.plist")))


if __name__ == "__main__":
    unittest.main()
