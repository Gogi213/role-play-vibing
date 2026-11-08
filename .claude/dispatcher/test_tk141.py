"""TK-141 (В-73): `wait file:<путь>` принимается, только если путь — файл итога зарегистрированного задания `tools/jobrun.sh`
(имя итога и рядом запись `<unit>.job`); прочее — отказ с подсказкой. Сторож видит писателя такого файла по записи (`watch.probe_file_writer`),
а не по командной строке. Уже ждущие тикеты с `file:` доживают по-старому (`file_wait_problem`, `signals.notify_wait_for_problem` не тронуты)."""
from __future__ import annotations

import atexit
import contextlib
import io
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

_SANDBOX = tempfile.mkdtemp(prefix="rpv-test-proj-")
os.makedirs(os.path.join(_SANDBOX, ".claude", "roles"))
os.environ["CLAUDE_PROJECT_DIR"] = _SANDBOX
atexit.register(shutil.rmtree, _SANDBOX, True)
sys.path.insert(0, str(Path(__file__).resolve().parent))
import dispatch as D  # noqa: E402
import project_paths as CFG  # noqa: E402
import routes as R  # noqa: E402
import signals as SIG  # noqa: E402
import ticket as T  # noqa: E402
import tickets as TK  # noqa: E402
import watch as WT  # noqa: E402
import test_jobrun as TJ  # noqa: E402

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=timezone(timedelta(hours=4)))
RC = "run-1108123456-ab12.rc"
JOB_LINE = "bash tools/jobrun.sh lab -- python long.py"
BAD = ("file:out/x.done", "file:out/result.csv", "file:out/t.rc", "file:out/run-110812345-ab12.rc", "file:out/run-11081234567-ab12.rc",
       "file:out/run-1108123456-AB12.rc", "file:out/run-1108123456-ab12.RC", "file:out/run-1108123456-ab12.rc.tmp", "file:out/run-1108123456-ab12.rc/",
       "file:/tmp/flag", "file:C:/work/job.log", "file:out/-1108123456-ab12.rc")


def register(root, rel: str = f"out/{RC}", mode: str = "trap", pid: str = "4242", unit: str = "") -> Path:
    """Кладёт запись регистрации `<unit>.job` рядом с итогом `rel` (как это делает tools/jobrun.sh); возвращает путь итога."""
    rc = Path(root) / rel
    rc.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"mode={mode}", f"unit={unit or rc.stem}"] + ([f"pid={pid}"] if pid else [])
    rc.with_suffix(".job").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return rc


class UnregisteredFileTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="rpv-tk141-"))
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_registered_jobrun_result_is_accepted(self):
        register(self.root)
        register(self.root, "a-b-c-1108123456-ab12.rc")
        register(self.root, "run-0101000000-0000.rc")
        specs = [f"file:out/{RC}", f"file:{(self.root / 'out' / RC).as_posix()}", "file:a-b-c-1108123456-ab12.rc", "file: run-0101000000-0000.rc "]
        if os.name == "nt":
            specs.append("file:" + str(self.root / "out" / RC))
        for spec in specs:
            self.assertEqual(T.file_wait_unregistered(spec, self.root), "", spec)

    def test_jobrun_name_without_registration_is_refused(self):
        """Файл с подходящим именем, но без записи регистрации рядом — не итог задания, известного сторожу."""
        (self.root / "out").mkdir()
        (self.root / "out" / RC).write_text("SERVICE_RESULT=success EXIT_STATUS=0\n", encoding="utf-8")  # итог есть, записи нет
        for spec in (f"file:out/{RC}", f"file:{(self.root / 'out' / RC).as_posix()}", "file:run-1108123456-ab12.rc"):
            why = T.file_wait_unregistered(spec, self.root)
            self.assertIn("tools/jobrun.sh", why, spec)
            self.assertIn(".job", why, spec)
            self.assertIn("job:", why, spec)

    def test_broken_or_foreign_registration_is_refused(self):
        register(self.root, unit="other-1108123456-ab12")  # запись чужого задания, скопированная к имени итога
        self.assertIn(".job", T.file_wait_unregistered(f"file:out/{RC}", self.root))
        register(self.root, mode="nohup")
        self.assertIn(".job", T.file_wait_unregistered(f"file:out/{RC}", self.root))
        (self.root / "out" / RC).with_suffix(".job").write_text("", encoding="utf-8")
        self.assertIn(".job", T.file_wait_unregistered(f"file:out/{RC}", self.root))

    def test_other_files_are_refused_with_hint(self):
        for spec in BAD:
            why = T.file_wait_unregistered(spec, self.root)
            self.assertIn("tools/jobrun.sh", why, spec)
            self.assertIn("job:", why, spec)

    def test_other_forms_are_not_touched(self):
        for spec in ("host:gpu1:/var/lib/rpv/runs/x.done", f"host:gpu1:/var/lib/rpv/runs/{RC}", "job:gpu1:run7", "ticket:TK-001", "ci:o/r#5",
                     "merged:o/r#5", "ci-run:o/r#99", "at:2099-01-01T00:00+04:00", "", "ерунда", "file:"):
            self.assertEqual(T.file_wait_unregistered(spec, self.root), "", spec)

    def test_routes_check_refuses_wait_form_early(self):
        err = R.check("engineer", "wait", "ждём", form="file:out/x.done", root=self.root)
        self.assertIn("tools/jobrun.sh", err)
        self.assertIn(".job", R.check("engineer", "wait", "ждём", form=f"file:out/{RC}", root=self.root))
        register(self.root)
        self.assertEqual(R.check("engineer", "wait", "ждём", form=f"file:out/{RC}", root=self.root), "")
        self.assertEqual(R.check("engineer", "wait", "ждём", form="host:gpu1:/var/lib/rpv/runs/x.done", root=self.root), "")

    def test_existing_waiting_tickets_are_not_complained_about(self):
        """Старая постановка (`file:` без jobrun) доживает: сигнал о негодном wait_for молчит, `file_wait_problem` тот же."""
        for spec in ("file:out/x.done", "file:flags/rpv-flag"):
            self.assertEqual(T.file_wait_problem(spec), "", spec)
        tkt = mock.Mock(status="waiting", id="TK-900", next_role="", header={"wait_for": "file:out/x.done"})
        state = {}
        with mock.patch.object(SIG, "append_ceo_inbox") as inbox:
            SIG.notify_wait_for_problem(tkt, state, NOW, set())
        inbox.assert_not_called()
        self.assertNotIn("ceo_wait_for_notified", state)


class WaitCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        (base / "tickets").mkdir()
        self._orig = (CFG.TICKETS_DIR, CFG.STATE_FILE, TK.TICKETS_DIR, TK.PROJECT_ROOT)
        CFG.TICKETS_DIR, CFG.STATE_FILE, TK.TICKETS_DIR, TK.PROJECT_ROOT = base / "tickets", base / "state.json", base / "tickets", base
        self.base = base
        self.path = T.create_ticket(CFG.TICKETS_DIR, owner="engineer", title="Т", status="todo", now=NOW)
        T.write_header_updates(self.path, {"status": "in_progress", "reviewer": "judge"}, now=NOW)
        self.tid = self.path.stem
        patch = mock.patch.object(D.P, "settings", lambda root=None: {"ci_repo": "o/r"})
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(lambda: setattr_all(CFG, TK, self._orig))

    def wait(self, spec, **kw):
        a = dict(id=self.tid, spec=spec, on_met=None, by="23:59")
        a.update(kw)
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            rc = TK.cmd_wait(type("A", (), a)())
        return rc, err.getvalue()

    def tkt(self):
        return T.read_ticket(self.path)

    def test_wait_refuses_file_that_is_not_jobrun_result(self):
        before = self.path.read_text(encoding="utf-8")
        for spec in BAD[:6]:
            rc, err = self.wait(spec)
            self.assertEqual(rc, 1, spec)
            self.assertIn("tools/jobrun.sh", err)
            self.assertIn("job:", err)
            self.assertEqual(self.path.read_text(encoding="utf-8"), before)  # отказ ничего не пишет

    def test_wait_refuses_jobrun_name_without_registration(self):
        before = self.path.read_text(encoding="utf-8")
        rc, err = self.wait(f"file:out/{RC}")
        self.assertEqual(rc, 1)
        self.assertIn(".job", err)
        self.assertIn("tools/jobrun.sh", err)
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)

    def test_wait_accepts_registered_jobrun_result(self):
        register(self.base)
        rc, err = self.wait(f"file:out/{RC}")
        self.assertEqual(rc, 0, err)
        self.assertEqual((self.tkt().status, self.tkt().header.get("wait_for")), ("waiting", f"file:out/{RC}"))

    def test_other_forms_still_accepted(self):
        for spec in ("at:2099-01-01T00:00+04:00", f"host:gpu1:/var/lib/rpv/runs/{RC}", "host:gpu1:/var/lib/rpv/runs/x.done"):
            T.write_header_updates(self.path, {"status": "in_progress", "wait_for": ""}, now=NOW)
            with mock.patch.object(TK.W, "check_wait_for", return_value=False):
                rc, err = self.wait(spec)
            self.assertEqual(rc, 0, f"{spec}: {err}")
            self.assertEqual(self.tkt().header.get("wait_for"), spec)

    def test_result_wait_goes_through_the_same_gate(self):
        os.environ["RPV_ROLE"] = "engineer"
        self.addCleanup(os.environ.pop, "RPV_ROLE", None)
        a = dict(id=self.tid, result="wait", why="ждём", pr=None, sha=None, path=None, form="file:out/x.done", tail="-", by="23:59")
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(TK.cmd_result(type("A", (), a)()), 1)
        self.assertIn("tools/jobrun.sh", err.getvalue())
        a["form"] = f"file:out/{RC}"
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):  # имя итога есть, записи регистрации нет
            self.assertEqual(TK.cmd_result(type("A", (), a)()), 1)
        self.assertIn(".job", err.getvalue())
        register(self.base)
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(TK.cmd_result(type("A", (), a)()), 0)
        self.assertEqual(self.tkt().status, "waiting")


class JobrunProbeTests(unittest.TestCase):
    """Сторож видит писателя итога jobrun по записи регистрации: в командной строке задания имени итога нет."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="rpv-tk141p-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        patch = mock.patch.object(CFG, "PROJECT_ROOT", self.root)
        patch.start()
        self.addCleanup(patch.stop)
        self.raw = f"out/{RC}"

    def probe(self, rows):
        return WT.probe_file_writer(self.raw, rows)

    def test_live_job_is_producer_dead_job_is_dead(self):
        register(self.root, pid="4242")
        other = [(100, 1, "python long.py"), (101, 100, "python -u long.py")]  # тот же python, но не оболочка jobrun
        self.assertEqual(self.probe([(4242, 1, JOB_LINE)] + other), "producer")
        self.assertEqual(self.probe(other), "dead")
        self.assertEqual(self.probe([(4243, 1, JOB_LINE)] + other), "dead")  # pid другой

    def test_pid_taken_by_a_foreign_process_is_dead(self):
        register(self.root, pid="4242")
        self.assertEqual(self.probe([(4242, 1, "C:\\Windows\\notepad.exe a.txt"), (1, 0, "init")]), "dead")
        self.assertEqual(self.probe([(4242, 1, ""), (1, 0, "init")]), "producer")  # командную строку не прочитать — не судим

    def test_unreadable_table_or_pid_is_not_dead(self):
        register(self.root, pid="4242")
        self.assertEqual(self.probe([]), "producer")
        register(self.root, pid="")
        self.assertEqual(self.probe([(1, 0, "init")]), "producer")

    def test_result_file_wins(self):
        rc = register(self.root, pid="4242")
        rc.write_text("SERVICE_RESULT=success EXIT_STATUS=0\n", encoding="utf-8")
        self.assertEqual(self.probe([(1, 0, "init")]), "exists")

    def test_systemd_unit_state(self):
        register(self.root, mode="systemd", pid="")
        for text, want in (("active\n", "producer"), ("activating\n", "producer"), ("deactivating\n", "producer"),
                           ("inactive\n", "dead"), ("failed\n", "dead"), ("", "dead")):
            with mock.patch.object(WT.hide, "run", return_value=mock.Mock(stdout=text, returncode=0)) as run:
                self.assertEqual(self.probe([(1, 0, "init")]), want, text)
            self.assertEqual(run.call_args[0][0], ["systemctl", "is-active", "run-1108123456-ab12"])
        with mock.patch.object(WT.hide, "run", side_effect=FileNotFoundError("systemctl")):
            self.assertEqual(self.probe([(1, 0, "init")]), "producer")  # systemctl нет — не видим, не снимаем

    def test_without_registration_the_old_probe_by_command_lines_stays(self):
        """Файл без записи регистрации не принимается при постановке; ждущий с таким именем с до-TK-141 доживает по командным строкам."""
        self.assertEqual(self.probe([(1, 0, "python long.py")]), "dead")
        self.assertEqual(self.probe([(1, 0, f"python writer.py {RC}")]), "producer")

    @unittest.skipUnless(TJ.BASH, "нет рабочего bash")
    def test_real_jobrun_job_alive_then_killed(self):
        """Настоящий tools/jobrun.sh: ожидание принимается; живое задание — producer; убитое без итога — dead."""
        runs = self.root / "runs"
        env = dict(os.environ, RPV_RUNS_DIR=runs.as_posix(), RPV_JOBRUN_MODE="trap")
        r = subprocess.run([TJ.BASH, TJ.SCRIPT.as_posix(), "lab", "--", "sleep", "25"], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.addCleanup(lambda: subprocess.run([TJ.BASH, "-c", "pkill -f 'sleep 25'"], capture_output=True, timeout=30))
        unit = r.stdout.split()[0].split("=", 1)[1]
        self.raw = (runs / f"{unit}.rc").as_posix()
        rec = T.jobrun_record(self.raw, self.root)
        self.assertEqual(rec and rec["unit"], unit)
        self.assertEqual(T.file_wait_unregistered(f"file:{self.raw}", self.root), "")
        self.assertEqual(WT.probe_file_writer(self.raw), "producer")
        os.kill(int(rec["pid"]), getattr(signal, "SIGKILL", signal.SIGTERM))  # оболочка задания убита, итог не записан
        for _ in range(100):
            if WT.probe_file_writer(self.raw) == "dead":
                break
            time.sleep(0.2)
        self.assertEqual(WT.probe_file_writer(self.raw), "dead")
        self.assertFalse(Path(self.raw).exists())


def setattr_all(CFG_, TK_, orig):
    CFG_.TICKETS_DIR, CFG_.STATE_FILE, TK_.TICKETS_DIR, TK_.PROJECT_ROOT = orig


if __name__ == "__main__":
    unittest.main()
