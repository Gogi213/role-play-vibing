"""CP6: tools/jobrun.sh пишет итог задания (SERVICE_RESULT, EXIT_STATUS) в <RPV_RUNS_DIR>/<unit>.rc — без шины, на любой машине.

Путь без systemd (trap) гоняется целиком под bash — Linux, macOS, Git Bash на Windows. Путь systemd проверен по аргументам с
поддельным systemd-run и по хуку --stop-post; живой systemd (ExecStopPost, $SERVICE_RESULT от самого systemd) здесь не проверяется."""
import os
import re
import shutil
import subprocess
import tempfile
import time
import sys
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[2] / "tools" / "jobrun.sh"


def _find_bash():
    cands = [shutil.which("bash")]
    if os.name == "nt":  # bash из System32 — заглушка WSL, нужен Git Bash
        cands = [os.path.join(b, "Git", "bin", "bash.exe") for b in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")) if b] + cands
    for c in cands:
        if not c or not os.path.isfile(c):
            continue
        try:
            r = subprocess.run([c, "-c", "echo ok"], capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            continue
        if r.returncode == 0 and b"ok" in r.stdout:
            return c
    return None


BASH = _find_bash()


@unittest.skipUnless(BASH, "нет рабочего bash")
class JobrunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runs = Path(self.tmp.name, "runs").as_posix()
        self.env = dict(os.environ, RPV_RUNS_DIR=self.runs, RPV_JOBRUN_MODE="trap")

    def jobrun(self, *args, env=None, timeout=60):
        return subprocess.run([BASH, SCRIPT.as_posix(), *args], env=env or self.env, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout)

    def run_job(self, shell_cmd: str):
        """Запускает задание `bash -c <команда>`; возвращает (stdout скрипта, путь файла итога); итог ждёт до 20 с."""
        r = self.jobrun("t", "--", "bash", "-c", shell_cmd)
        self.assertEqual(r.returncode, 0, r.stderr)
        m = re.fullmatch(r"UNIT=(t-\d{10}-[0-9a-f]{4}) RC=(\S+)", r.stdout.strip())
        self.assertIsNotNone(m, r.stdout)
        self.assertEqual(m.group(2), f"{self.runs}/{m.group(1)}.rc")
        return r.stdout, Path(m.group(2))

    def wait_rc(self, rc: Path) -> str:
        for _ in range(200):
            if rc.exists():
                return rc.read_text(encoding="utf-8").strip()
            time.sleep(0.1)
        self.fail(f"файл итога не появился: {rc}")

    def test_success_is_recorded(self):
        _, rc = self.run_job("exit 0")
        self.assertEqual(self.wait_rc(rc), "SERVICE_RESULT=success EXIT_STATUS=0")

    def test_failure_exit_code_is_recorded(self):
        _, rc = self.run_job("exit 3")
        self.assertEqual(self.wait_rc(rc), "SERVICE_RESULT=exit-code EXIT_STATUS=3")

    def test_command_not_found_is_failure(self):
        r = self.jobrun("t", "--", "no-such-command-rpv")
        rc = Path(re.search(r"RC=(\S+)", r.stdout).group(1))
        self.assertEqual(self.wait_rc(rc), "SERVICE_RESULT=exit-code EXIT_STATUS=127")

    def test_signal_kill_is_recorded(self):
        _, rc = self.run_job("kill -TERM $$")
        self.assertEqual(self.wait_rc(rc), "SERVICE_RESULT=signal EXIT_STATUS=TERM")

    def test_file_appears_only_after_job_ends_and_whole(self):
        _, rc = self.run_job("sleep 1.5; exit 0")
        self.assertFalse(rc.exists(), "итог не должен появиться, пока задание идёт")
        self.wait_rc(rc)
        self.assertFalse(Path(str(rc) + ".tmp").exists(), "временный файл остался")

    def test_job_output_goes_to_log(self):
        _, rc = self.run_job("echo привет-из-задания; exit 0")
        self.wait_rc(rc)
        log = Path(str(rc)[:-3] + ".log")
        self.assertIn("привет-из-задания", log.read_text(encoding="utf-8"))

    def test_two_runs_with_one_label_do_not_clash(self):
        _, a = self.run_job("exit 0")
        time.sleep(1.1)
        _, b = self.run_job("exit 1")
        self.assertNotEqual(a, b)
        self.assertIn("EXIT_STATUS=0", self.wait_rc(a))
        self.assertIn("EXIT_STATUS=1", self.wait_rc(b))

    def test_usage_errors(self):
        for args in ((), ("t",), ("t", "--"), ("t", "-p", "X=1")):
            r = self.jobrun(*args)
            self.assertEqual(r.returncode, 2, args)
            self.assertIn("jobrun.sh <метка>", r.stderr)

    def test_stop_post_hook_writes_what_systemd_gives(self):
        os.makedirs(self.runs, exist_ok=True)
        rc = f"{self.runs}/u.rc"
        r = self.jobrun("--stop-post", rc, env=dict(self.env, SERVICE_RESULT="exit-code", EXIT_STATUS="2"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(Path(rc).read_text(encoding="utf-8"), "SERVICE_RESULT=exit-code EXIT_STATUS=2\n")
        self.assertFalse(Path(rc + ".tmp").exists())

    def test_systemd_mode_builds_transient_unit_with_stop_post(self):
        shim_dir = Path(self.tmp.name, "bin")
        shim_dir.mkdir()
        out = Path(self.tmp.name, "argv.txt").as_posix()
        shim = shim_dir / "systemd-run"
        shim.write_text('#!/bin/bash\nprintf "%s\\n" "$@" > "$SHIM_OUT"\n', encoding="utf-8", newline="\n")
        shim.chmod(0o755)
        env = dict(self.env, RPV_JOBRUN_MODE="systemd", SHIM_OUT=out, PATH=shim_dir.as_posix() + os.pathsep + os.environ["PATH"])
        r = self.jobrun("lbl", "-p", "MemoryMax=1G", "--", "python", "-u", "run.py", "--n", "5", env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        argv = Path(out).read_text(encoding="utf-8").splitlines()
        unit = re.fullmatch(r"UNIT=(lbl-\d{10}-[0-9a-f]{4}) RC=(\S+)", r.stdout.strip())
        self.assertIsNotNone(unit, r.stdout)
        self.assertEqual(argv[:3], ["--unit", unit.group(1), "--collect"])
        self.assertEqual(argv[3], "-p")
        self.assertTrue(re.fullmatch(r'ExecStopPost="[^"]*jobrun\.sh" --stop-post "' + re.escape(unit.group(2)) + '"', argv[4]), argv[4])
        self.assertEqual(argv[5:], ["-p", "MemoryMax=1G", "python", "-u", "run.py", "--n", "5"])
        self.assertEqual(unit.group(2), f"{self.runs}/{unit.group(1)}.rc")
        self.assertFalse(Path(unit.group(2)).exists(), "итог пишет systemd по концу задания, не запуск")

    def test_systemd_refusal_is_reported(self):
        shim_dir = Path(self.tmp.name, "bin")
        shim_dir.mkdir()
        shim = shim_dir / "systemd-run"
        shim.write_text("#!/bin/bash\nexit 1\n", encoding="utf-8", newline="\n")
        shim.chmod(0o755)
        env = dict(self.env, RPV_JOBRUN_MODE="systemd", PATH=shim_dir.as_posix() + os.pathsep + os.environ["PATH"])
        r = self.jobrun("lbl", "--", "true", env=env)
        self.assertEqual(r.returncode, 1)
        self.assertIn("systemd-run: отказ", r.stderr)

    def test_dispatcher_sees_the_result_through_a_loopback_ssh(self):
        """Вся цепочка без сети: jobrun.sh пишет файл итога, «ssh» (удалённая команда в локальном bash) отдаёт его сверке диспетчера."""
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import dispatch as D
        _, rc = self.run_job("sleep 1.5; exit 3")
        remote = rc.as_posix()
        if re.match(r"[A-Za-z]:/", remote):  # Git Bash: C:/x → /c/x, путь на «машине» обязан быть абсолютным (/…)
            remote = "/" + remote[0].lower() + remote[2:]
        key = ("loop", "path", remote)
        spec = f"host:loop:{remote}"
        D._WAIT_WATCH.add(key)
        self.addCleanup(lambda: (D._WAIT_WATCH.discard(key), D._WAIT_CACHE.pop(key, None), D._WAIT_RC.pop(key, None)))
        with mock.patch.object(D, "_ssh_cmd", lambda alias, cmd: [BASH, "-c", cmd]), mock.patch.object(D, "BLIND", D.blind.Tracker()):
            D._reconcile()
            self.assertFalse(D._WAIT_CACHE[key][1], "задание идёт — итога нет, сверка условие не закрывает")
            self.wait_rc(rc)
            D._reconcile()
        self.assertTrue(D._WAIT_CACHE[key][1])
        self.assertEqual(D.wait_rc(spec), "SERVICE_RESULT=exit-code EXIT_STATUS=3")
        self.assertTrue(D._rc_failed(D.wait_rc(spec)))


if __name__ == "__main__":
    unittest.main()
