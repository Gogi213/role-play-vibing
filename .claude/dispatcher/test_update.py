"""/rpv-update (В-13): порядок шагов с подменённым `claude`; PATH в plist присмотра и проверка doctor (П3)."""
import io
import os
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
from test_plugin_version import make_install  # noqa: E402

PROJ = Path("/p/proj")
DOCTOR_FAIL = "OK    сторож     pid 1\nFAIL  диспетчер  не запущен (pid-файл: нет)\nFAIL  простой    26 мин из 10"


def _entry(version, scope="user", project=None):
    e = {"scope": scope, "version": version, "installPath": f"/cache/{version}"}
    if project:
        e["projectPath"] = str(project)
    return e


class Fake:
    """Подмена run/installed: помнит команды; `claude plugin update` меняет версию в installed_plugins.json."""

    def __init__(self, old="2.0.9", new="2.1.0", doctor=(0, "OK  всё"), fail_on=None):
        self.cmds, self.version, self.new, self.doctor, self.fail_on = [], old, new, doctor, fail_on

    def installed(self, project):
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
        f.installed = lambda project: _entry(f.version, "project", PROJ)
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

    def _entry_for(self, entries, project=PROJ):
        """`install_entry` на настоящем `installed_plugins.json` во временном каталоге настроек (TK-044, #4)."""
        with tempfile.TemporaryDirectory() as d:
            cfg, _ = make_install(d, sorted({v for *_, v in entries}) or ["0"], entries)
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": cfg}):
                return U.install_entry(project)

    def test_install_entry_looks_up_the_marketplace_plugin(self):
        """Алиаса `PLUGIN_NAME` нет: запись ищется по `MARKETPLACE` (TK-047, #9)."""
        self.assertFalse(hasattr(U, "PLUGIN_NAME"))
        with mock.patch.object(U.PV, "plugin_entry", return_value={"version": "1"}) as pe:
            U.install_entry(PROJ)
        self.assertEqual(pe.call_args.args, (PROJ, U.MARKETPLACE))

    def test_install_entry_is_the_hook_reader_narrowest_scope_wins(self):
        proj = str(PROJ)
        self.assertEqual(self._entry_for([("user", None, "1"), ("project", proj, "2")])["version"], "2")
        self.assertEqual(self._entry_for([("user", None, "1"), ("project", proj, "2")], Path("/other"))["version"], "1")
        # несколько записей этого проекта: самая узкая область, а не первая в файле (раньше update брал первую)
        self.assertEqual(self._entry_for([("project", proj, "2"), ("local", proj, "3"), ("user", None, "1")])["version"], "3")
        # запись только чужого проекта — не установка этого (раньше update брал entries[0])
        self.assertIsNone(self._entry_for([("project", "/other", "2")]))
        self.assertIsNone(self._entry_for([]))


class MainLines(unittest.TestCase):
    """Последняя строка: «уже на» — только когда версия сессии (CLAUDE_PLUGIN_ROOT) совпала с установленной (TK-079)."""

    def _main(self, session, rows, new="2.1.0"):
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as d, redirect_stdout(out),                 mock.patch.object(U.P, "resolve_project", return_value=d),                 mock.patch.object(U.shutil, "which", return_value="claude"),                 mock.patch.object(U, "session_version", return_value=session),                 mock.patch.object(U, "update", return_value=(rows, new)):
            rc = U.main(["--ref", "В-13"])
        return rc, out.getvalue()

    def test_restart_line_and_green(self):
        rc, text = self._main("2.0.9", [("плагин", D.OK, "2.0.9 → 2.1.0")])
        self.assertEqual(rc, 0)
        self.assertIn(U.RESTART, text)
        self.assertIn("Готово", text)

    def test_session_on_old_version_restart_not_already(self):
        _, text = self._main("2.0.9", [("плагин", D.OK, "2.0.9 → 2.1.0")])
        self.assertIn(U.RESTART, text)
        self.assertNotIn("уже на", text)

    def test_no_session_version_restart_not_already(self):
        _, text = self._main("?", [("плагин", D.OK, "уже последняя версия ?")], new="?")
        self.assertIn(U.RESTART, text)
        self.assertNotIn("уже на", text)

    def test_same_version_is_already(self):
        _, text = self._main("2.1.0", [("плагин", D.OK, "уже последняя версия 2.1.0")])
        self.assertIn("Эта сессия уже на 2.1.0.", text)
        self.assertNotIn(U.RESTART, text)

    def test_fail_row_is_not_green(self):
        rc, text = self._main("2.1.0", [("doctor", D.FAIL, "FAIL: сторож")])
        self.assertEqual(rc, 1)
        self.assertIn("НЕ ГОТОВО", text)
        self.assertNotIn("Готово:", text)


class SessionVersion(unittest.TestCase):
    """Версия сессии — по CLAUDE_PLUGIN_ROOT, а не по каталогу скрипта (TK-079)."""

    def _root(self, d, version):
        r = Path(d) / version
        (r / ".claude-plugin").mkdir(parents=True)
        (r / ".claude-plugin" / "plugin.json").write_text('{"name": "role-play-vibing", "version": "%s"}' % version)
        return r

    def test_version_from_env_root(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(U.session_version({"CLAUDE_PLUGIN_ROOT": str(self._root(d, "2.0.17"))}), "2.0.17")

    def test_no_env_is_unknown(self):
        self.assertEqual(U.session_version({}), "?")

    def test_script_from_new_dir_session_on_old_says_restart(self):
        """Скрипт лежит в каталоге новой версии, переменная указывает на старую → строка о перезапуске."""
        with tempfile.TemporaryDirectory() as d:
            old, new = self._root(d, "2.0.16"), "2.0.17"
            with mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_ROOT": str(old)}):
                _, text = MainLines()._main(U.session_version(), [("плагин", D.OK, "2.0.16 → 2.0.17")], new=new)
        self.assertIn(U.RESTART, text)
        self.assertNotIn("уже на", text)


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
