"""Тесты `start.py` (`/rpv-start`): фоновый запуск диспетчера и сторожа, перезапуск по pid-замку.

Настоящие `dispatch.py`/`watch.py` не запускаются: вместо них — пробные скрипты во временной «папке плагина»
(пишут свой pid в замок и спят). Все запущенные процессы гасятся в конце теста.
"""
from __future__ import annotations

import atexit
import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
import warnings
from pathlib import Path
from unittest import mock

# проект-пустышка: импорт диспетчера не должен найти рабочий проект выше папки плагина
_SANDBOX = tempfile.mkdtemp(prefix="rpv-test-proj-")
os.makedirs(os.path.join(_SANDBOX, ".claude", "roles"))
os.environ["CLAUDE_PROJECT_DIR"] = _SANDBOX
atexit.register(shutil.rmtree, _SANDBOX, True)
sys.path.insert(0, str(Path(__file__).resolve().parent))
import dispatch as D  # noqa: E402
import start as S  # noqa: E402

FAKE = textwrap.dedent("""\
    import os, sys, time
    from pathlib import Path
    proj = Path(sys.argv[sys.argv.index("--project") + 1])
    d = proj / ".claude" / "dispatcher"
    d.mkdir(parents=True, exist_ok=True)
    (d / "NAME.pid").write_text(str(os.getpid()), encoding="utf-8")
    print("NAME started", os.environ.get("RPV_START_TEST"), os.environ.get("PYTHONUTF8"), flush=True)
    time.sleep(120)
    """)


def wait_alive(pid, want=True, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if bool(D._pid_alive(pid, "py")) == want:
            return True
        time.sleep(0.1)
    return False


class StartTests(unittest.TestCase):
    def test_board_service_only_with_rpv_board(self):
        old = os.environ.pop("RPV_BOARD", None), os.environ.pop("ALPHA_BOARD", None)
        try:
            self.assertEqual(S.services(), S.SERVICES)
            os.environ["RPV_BOARD"] = "http://x/#k"
            self.assertEqual(S.services(), S.SERVICES + ("board_push",))
        finally:
            os.environ.pop("RPV_BOARD", None)
            for k, v in zip(("RPV_BOARD", "ALPHA_BOARD"), old):
                if v is not None:
                    os.environ[k] = v

    def setUp(self):
        warnings.simplefilter("ignore", ResourceWarning)   # Popen-объекты фоновых процессов не ждём — они живут дальше
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name).resolve()
        self.plugin = base / "plugin" / ".claude" / "dispatcher"
        self.plugin.mkdir(parents=True)
        for name in S.SERVICES:
            (self.plugin / f"{name}.py").write_text(FAKE.replace("NAME", name), encoding="utf-8")
        self.project = base / "proj"
        (self.project / ".claude" / "roles").mkdir(parents=True)
        self.pids = []
        self.addCleanup(self.cleanup)
        self.addCleanup(setattr, S, "_launcher", S._launcher)
        S._launcher = lambda: "popen"   # проверяем запуск Popen; WMI и systemd — подменой вызовов в тестах ниже

    def cleanup(self):
        for pid in self.pids:
            D._kill_tree(pid)
        for pid in self.pids:
            wait_alive(pid, want=False, timeout=5)
        self.tmp.cleanup()

    def launch(self):
        res = S.start(self.project, code_dir=self.plugin, settle=1.0)
        self.pids += [r["pid"] for r in res]
        self.pids += [p for p in self.lock_pids() if p]   # служба — потомок cmd/юнита: свой pid в замке
        return {r["name"]: r for r in res}

    def test_first_start_runs_both_in_background_and_logs_to_project(self):
        res = self.launch()
        self.assertEqual(list(res), ["dispatch", "watch"])
        state = self.project / ".claude" / "dispatcher"
        for name, r in res.items():
            self.assertTrue(r["ok"], (name, r))
            self.assertEqual(r["restarted"], 0)
            self.assertTrue(wait_alive(r["pid"]), name)
            self.assertEqual(r["log"], state / f"{name}.run.log")
            self.assertIn(f"{name} started", r["log"].read_text(encoding="utf-8"))     # вывод ушёл в лог проекта
            self.assertEqual(S.read_pid(state / f"{name}.pid"), r["pid"])               # замок = pid запущенного процесса

    def test_second_start_stops_running_pair_and_starts_fresh(self):
        first = self.launch()
        second = self.launch()
        for name in S.SERVICES:
            old, new = first[name]["pid"], second[name]["pid"]
            self.assertNotEqual(old, new)
            self.assertEqual(second[name]["restarted"], old, name)
            self.assertTrue(wait_alive(old, want=False), f"{name}: прежний процесс не остановлен")
            self.assertTrue(wait_alive(new), name)
            self.assertEqual(S.read_pid(self.project / ".claude" / "dispatcher" / f"{name}.pid"), new)

    def test_stale_lock_with_foreign_live_process_is_not_killed(self):
        """pid из замка переиспользован чужим python — его не трогаем, замок осиротел и просто убирается."""
        stranger = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.pids.append(stranger.pid)
        self.assertTrue(wait_alive(stranger.pid))
        state = self.project / ".claude" / "dispatcher"
        state.mkdir(parents=True)
        (state / "dispatch.pid").write_text(str(stranger.pid), encoding="utf-8")
        res = self.launch()
        self.assertEqual(res["dispatch"]["restarted"], 0)
        self.assertTrue(wait_alive(stranger.pid), "чужой процесс убит")
        self.assertNotEqual(S.read_pid(state / "dispatch.pid"), stranger.pid)

    def test_cmdline_of_live_process_is_known_on_every_os(self):
        """Командную строку чужого процесса видно на каждой ОС (Linux — /proc, macOS — ps): иначе is_ours()
        принимает любой живой pid за свой и останавливает его."""
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)  # rpv-cmdline-probe"])
        self.pids.append(child.pid)
        self.assertTrue(wait_alive(child.pid))
        line = S._cmdline(child.pid)
        self.assertIsNotNone(line, "командная строка не прочитана")
        self.assertIn("rpv-cmdline-probe", line)
        self.assertFalse(S.is_ours(child.pid, "dispatch.py"))

    def test_main_prints_pid_and_log_path(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = S.main(["--project", str(self.project)], code_dir=self.plugin, settle=1.0)
        out = buf.getvalue()
        for name in S.SERVICES:
            pid = S.read_pid(self.project / ".claude" / "dispatcher" / f"{name}.pid")
            self.pids.append(pid)
            self.assertIn(f"{name}: запущен, pid {pid}", out)
            self.assertIn(f"{name}.run.log", out)
        self.assertEqual(rc, 0)

    def test_outside_project_or_without_roles_is_refused_and_creates_nothing(self):
        plain = Path(self.tmp.name).resolve() / "plain"
        plain.mkdir()
        env = {k: v for k, v in os.environ.items() if k not in ("RPV_PROJECT", "CLAUDE_PROJECT_DIR")}
        env["PYTHONIOENCODING"] = "utf-8"
        for args, cwd in (([], plain), (["--project", str(plain)], plain)):
            done = subprocess.run([sys.executable, str(Path(S.__file__)), *args], cwd=str(cwd), env=env,
                                  capture_output=True, text=True, encoding="utf-8", timeout=120)
            self.assertEqual(done.returncode, 2, (args, done.stderr))
            self.assertIn("/rpv-init", done.stderr)
        self.assertEqual(list(plain.iterdir()), [])


    # --- отвязка от сессии: WMI (Windows), systemd (Linux), «отвязан: да/нет» — подменой вызовов -----------------------

    def lock_pids(self):
        return [S.read_pid(self.project / ".claude" / "dispatcher" / f"{n}.pid") for n in S.SERVICES]

    def test_cim_cmdline_quotes_every_part_and_redirects_to_log(self):
        line = S._cim_cmdline([r"C:\Program Files\Py\python.exe", "-u", r"C:\pl ug\watch.py", "--project", r"C:\my proj"],
                              r"C:\my proj\.claude\dispatcher\watch.run.log", {"PYTHONUTF8": "1", "RPV_X": "a b"})
        self.assertEqual(line, r'cmd.exe /d /s /c "set "PYTHONUTF8=1" && set "RPV_X=a b" && "C:\Program Files\Py\python.exe" '
                               r'"-u" "C:\pl ug\watch.py" "--project" "C:\my proj" '
                               r'>> "C:\my proj\.claude\dispatcher\watch.run.log" 2>&1"')

    def test_forward_env_carries_dispatcher_settings_but_no_secrets_or_unsafe_values(self):
        extra = {"RPV_DISPATCH_MODEL": "m", "ALPHA_DISPATCH_X": "1", "CLAUDE_BIN": "/b/claude",
                 "ANTHROPIC_API_KEY": "sk-secret", "RPV_BAD": 'a"b', "RPV_PCT": "50%"}
        with mock.patch.dict(os.environ, extra):
            env = S._forward_env()
        self.assertEqual((env["RPV_DISPATCH_MODEL"], env["ALPHA_DISPATCH_X"], env["CLAUDE_BIN"]), ("m", "1", "/b/claude"))
        self.assertEqual((env["PYTHONUTF8"], env["PYTHONIOENCODING"]), ("1", "utf-8"))
        for absent in ("ANTHROPIC_API_KEY", "RPV_BAD", "RPV_PCT"):
            self.assertNotIn(absent, env)

    @unittest.skipUnless(os.name == "nt", "WMI — только Windows")
    def test_wmi_path_starts_service_via_cim_with_cwd_env_and_log(self):
        """Вместо WMI — та же командная строка обычным процессом без окружения вызывающего (как у WMI): проверяем
        кавычки, лог, окружение через set, рабочий каталог и pid из ответа."""
        S._launcher = lambda: "wmi"
        seen = []

        def fake_cim(cmdline, cwd):
            bare = {k: v for k, v in os.environ.items() if k not in ("RPV_START_TEST", "PYTHONUTF8", "PYTHONIOENCODING")}
            p = subprocess.Popen(cmdline, cwd=cwd, env=bare, creationflags=S.CREATE_NO_WINDOW)
            seen.append((cmdline, cwd, p.pid))
            self.pids.append(p.pid)
            return p.pid

        with mock.patch.object(S, "_cim_create", fake_cim), mock.patch.dict(os.environ, {"RPV_START_TEST": "ok"}):
            res = self.launch()
        for (cmdline, cwd, pid), name in zip(seen, S.SERVICES):
            r = res[name]
            self.assertEqual((r["how"], r["pid"], r["ok"]), ("wmi", pid, True), name)
            self.assertEqual(cwd, str(self.project))
            self.assertTrue(cmdline.startswith('cmd.exe /d /s /c "'), cmdline)
            self.assertIn(f"{name} started ok 1", r["log"].read_text(encoding="utf-8"))   # вывод и окружение дошли
        for pid in self.lock_pids():
            self.assertTrue(wait_alive(pid), "служба (потомок cmd) жива")

    def test_wmi_failure_falls_back_to_popen(self):
        S._launcher = lambda: "wmi"

        def refuse(cmdline, cwd):
            raise OSError("нет PowerShell")

        with mock.patch.object(S, "_cim_create", refuse):
            res = self.launch()
        for name, r in res.items():
            self.assertEqual((r["how"], r["ok"]), ("popen", True), name)

    def test_systemd_run_command_line_and_pid_from_mainpid(self):
        calls = []

        def fake_run(args, **kw):
            calls.append(list(args))
            return subprocess.CompletedProcess(args, 0, stdout="MainPID=4321\n" if args[0] == "systemctl" else "", stderr="")

        cmd = ["/usr/bin/python3", "-u", "/p/dispatch.py", "--project", "/my proj"]
        with mock.patch.object(S.subprocess, "run", fake_run):
            pid = S._systemd_run("rpv-dispatch-abc", cmd, "/my proj", "/my proj/.claude/dispatcher/dispatch.run.log",
                                 {"PYTHONUTF8": "1"})
        self.assertEqual(pid, 4321)
        run = calls[0]
        self.assertEqual(run[:5], ["systemd-run", "--user", "--unit", "rpv-dispatch-abc", "--collect"])
        self.assertIn("WorkingDirectory=/my proj", run)
        self.assertIn("--setenv=PYTHONUTF8=1", run)
        self.assertEqual(run[-3:-1], ["bash", "-c"])
        self.assertEqual(run[-1], "exec /usr/bin/python3 -u /p/dispatch.py --project '/my proj' "
                                  ">> '/my proj/.claude/dispatcher/dispatch.run.log' 2>&1")
        self.assertEqual(calls[1], ["systemctl", "--user", "show", "-p", "MainPID", "rpv-dispatch-abc.service"])

    def test_unit_name_is_per_project_and_stable(self):
        a, b = Path(self.tmp.name) / "a", Path(self.tmp.name) / "b"
        self.assertRegex(S.unit_name("dispatch", a), r"^rpv-dispatch-[0-9a-f]{8}$")
        self.assertEqual(S.unit_name("dispatch", a), S.unit_name("dispatch", a))
        self.assertNotEqual(S.unit_name("dispatch", a), S.unit_name("dispatch", b))
        self.assertNotEqual(S.unit_name("dispatch", a), S.unit_name("watch", a))

    def test_spawn_uses_systemd_unit_and_falls_back_to_popen_when_it_refuses(self):
        S._launcher = lambda: "systemd"
        script = self.plugin / "dispatch.py"
        log = self.project / ".claude" / "dispatcher" / "dispatch.run.log"
        seen = []

        def fake_run(unit, cmd, project, log_, env):
            seen.append((unit, cmd, env))
            return 777

        with mock.patch.object(S, "_systemd_run", fake_run):
            st = S.spawn(script, self.project, log)
        self.assertEqual((st.how, st.pid, st.unit), ("systemd", 777, S.unit_name("dispatch", self.project)))
        unit, cmd, env = seen[0]
        self.assertEqual(cmd[1:], ["-u", str(script), "--project", str(self.project)])
        self.assertEqual(env["PYTHONUTF8"], "1")
        self.assertIn("старт dispatch.py", log.read_text(encoding="utf-8"))

        def refuse(*args):
            raise OSError("менеджер юнитов не ответил")

        with mock.patch.object(S, "_systemd_run", refuse):
            st = S.spawn(script, self.project, log)
        self.pids.append(st.pid)
        self.assertEqual(st.how, "popen")

    def test_stop_running_stops_active_unit_and_leaves_inactive_alone(self):
        calls, active = [], {"v": "active"}

        def fake_ctl(*args, **kw):
            calls.append(args)
            out = f"ActiveState={active['v']}\nMainPID=77\n" if args[0] == "show" else ""
            return subprocess.CompletedProcess(args, 0, stdout=out, stderr="")

        pid_file = self.project / ".claude" / "dispatcher" / "dispatch.pid"
        with mock.patch.object(S, "_systemctl", fake_ctl):
            self.assertEqual(S.stop_running(pid_file, "dispatch.py", unit="rpv-dispatch-x"), 77)
            self.assertIn(("stop", "rpv-dispatch-x.service"), calls)
            calls.clear()
            active["v"] = "inactive"
            self.assertEqual(S.stop_running(pid_file, "dispatch.py", unit="rpv-dispatch-x"), 0)
            self.assertNotIn(("stop", "rpv-dispatch-x.service"), calls)

    def test_start_stops_own_units_before_launch_when_systemd(self):
        S._launcher = lambda: "systemd"
        stops = []

        def fake_stop(pid_file, script, timeout=0, unit=None):
            stops.append(unit)
            return 0

        with mock.patch.object(S, "stop_running", fake_stop), \
                mock.patch.object(S, "spawn", lambda *a: S.Started(0, "systemd")):
            S.start(self.project, code_dir=self.plugin, settle=0)
        self.assertEqual(stops, [S.unit_name("dispatch", self.project), S.unit_name("watch", self.project)])

    @unittest.skipUnless(os.name == "nt", "job-объекты — только Windows")
    def test_is_detached_on_windows_means_not_in_job(self):
        for job, want in ((True, False), (False, True), (None, None)):
            with mock.patch.object(S, "_in_job", return_value=job):
                self.assertIs(S.is_detached(123), want)

    def test_detached_on_linux_compares_cgroup_with_this_session(self):
        cgroups = {os.getpid(): "0::/session.scope", 5: "0::/app.slice/rpv-dispatch-x.service", 6: "0::/session.scope",
                   7: None}
        with mock.patch.object(S, "_cgroup", side_effect=lambda pid: cgroups[pid]):
            self.assertIs(S._detached_linux(5), True)
            self.assertIs(S._detached_linux(6), False)
            self.assertIsNone(S._detached_linux(7))

    def test_main_prints_detached_yes_no_per_service(self):
        answers = iter([True, False])
        buf = io.StringIO()
        with mock.patch.object(S, "is_detached", lambda pid: next(answers)), contextlib.redirect_stdout(buf):
            rc = S.main(["--project", str(self.project)], code_dir=self.plugin, settle=1.0)
        self.pids += [p for p in self.lock_pids() if p]
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertRegex(out, r"dispatch: запущен, pid \d+; отвязан: да;")
        self.assertRegex(out, r"watch: запущен, pid \d+; отвязан: нет;")
        self.assertEqual((S._yes_no(True), S._yes_no(False), S._yes_no(None)), ("да", "нет", "нет (проверить не удалось)"))


if __name__ == "__main__":
    unittest.main()
