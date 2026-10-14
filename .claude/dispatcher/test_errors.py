"""V2-3: реестр ошибок — ошибка исправлена, только когда проверка зелёная; `run` и приёмка гоняют проверки всех исправленных."""
import io
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import errors as E  # noqa: E402


class ErrorsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        env = {k: v for k, v in os.environ.items() if k != "RPV_ROLE"}
        self.env = mock.patch.dict(os.environ, env, clear=True)
        self.env.start()
        self.git("init", "-q")
        self.script("tools/ok.py", "print('зелёный')")
        self.script("tools/bad.py", "import sys\nprint('падаю: нет файла x')\nsys.exit(1)")
        # зелёная, пока рядом нет flag.txt — им тест «ломает» исправленную ошибку
        self.script("tools/flag.py", "import sys\nfrom pathlib import Path\n"
                    "sys.exit(3 if Path('flag.txt').exists() else 0)")
        self.script("tests/__init__.py", "")   # обычный пакет: чужой `tests` из site-packages не перекроет
        self.script("tests/test_good.py", "import unittest\n\n\nclass T(unittest.TestCase):\n"
                    "    def test_a(self):\n        self.assertTrue(True)\n")
        self.git("commit", "-qm", "init")

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def git(self, *a):
        return subprocess.run(["git", "-C", str(self.root), "-c", "user.name=t", "-c", "user.email=t@t", *a],
                              check=True, capture_output=True, text=True).stdout.strip()

    def script(self, rel, body, track=True):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body + "\n", encoding="utf-8")
        if track:
            self.git("add", rel)
        return p

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = E.main(["--project", str(self.root), *args])
        return code, out.getvalue().strip(), err.getvalue().strip()

    def head(self, eid):
        return E.header(E.errors_dir(self.root) / f"{eid}.md")

    def added(self, title="Ошибка", **kw):
        args = ["add", "--title", title]
        for k, v in kw.items():
            args += [f"--{k}", v]
        code, out, err = self.run_cli(*args)
        self.assertEqual(code, 0, err)
        return out

    def fixed(self, check="python tools/ok.py", title="Ошибка"):
        eid = self.added(title)
        code, _, err = self.run_cli("fix", eid, "--check", check)
        self.assertEqual(code, 0, err)
        return eid

    # --- add / list / show
    def test_add_numbers_and_record_format(self):
        code, out, _ = self.run_cli("add", "--title", "Цена  считается\nс  ошибкой", "--ticket", "tk-236",
                                    "--what", "цена 0 вместо 10\nпараметр check: x")
        self.assertEqual((code, out), (0, "E-001"))
        self.assertEqual(self.added("вторая"), "E-002")
        text = (E.errors_dir(self.root) / "E-001.md").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\nid: E-001\ntitle: Цена считается с ошибкой\nticket: TK-236\nfound: "), text)
        self.assertIn("status: open\ncheck:\nfixed:\nfixed_sha:\ntimeout:\n---\n## Что случилось\nцена 0 вместо 10\nпараметр check: x\n", text)
        self.assertIn("не описано", (E.errors_dir(self.root) / "E-002.md").read_text(encoding="utf-8"))

    def test_add_continues_numbering_after_max(self):
        d = E.errors_dir(self.root)
        d.mkdir(parents=True)
        (d / "E-041.md").write_text("---\nid: E-041\nstatus: fixed\n---\n", encoding="utf-8")
        (d / ".gitkeep").write_text("", encoding="utf-8")
        self.assertEqual(self.added("новая"), "E-042")

    def test_add_refuses_empty_title_and_bad_ticket(self):
        self.assertEqual(self.run_cli("add", "--title", "  ")[0], 1)
        code, _, err = self.run_cli("add", "--title", "т", "--ticket", "задача 5")
        self.assertEqual(code, 1)
        self.assertIn("TK-N", err)
        self.assertFalse(E.errors(self.root))

    def test_any_role_may_add_and_fix(self):
        for role in ("researcher", "engineer", "judge"):
            with mock.patch.dict(os.environ, {"RPV_ROLE": role}):
                eid = self.added(f"от {role}")
                self.assertEqual(self.run_cli("fix", eid, "--check", "python tools/ok.py")[0], 0, role)

    def test_list_show_and_open_filter(self):
        a = self.added("открытая", ticket="TK-5")
        b = self.fixed(title="исправленная")
        code, out, _ = self.run_cli("list")
        self.assertEqual(out.splitlines(), [f"{a} [open] открытая (TK-5)", f"{b} [fixed] исправленная"])
        self.assertEqual(self.run_cli("list", "--open")[1], f"{a} [open] открытая (TK-5)")
        self.assertIn("title: исправленная", self.run_cli("show", b)[1])
        self.assertEqual(self.run_cli("show", "E-9")[0], 1)

    # --- fix: проверка запускается, и только зелёная закрывает ошибку
    def test_fix_runs_check_and_records_it(self):
        eid = self.added("e")
        code, out, err = self.run_cli("fix", "E-1", "--check", "python tools/ok.py")   # E-1 = E-001
        self.assertEqual(code, 0, err)
        self.assertIn("E-001: fixed", out)
        h = self.head(eid)
        self.assertEqual((h["status"], h["check"], h["fixed_sha"]), ("fixed", "python tools/ok.py", self.git("rev-parse", "--short", "HEAD")))
        self.assertRegex(h["fixed"], r"^\d{4}-\d\d-\d\d \d\d:\d\d$")
        self.assertEqual(h["timeout"], "")

    def test_fix_body_is_not_touched_by_header_edit(self):
        eid = self.added("e", what="check: подделка\nstatus: open\nfixed: никогда")
        self.assertEqual(self.run_cli("fix", eid, "--check", "python tools/ok.py")[0], 0)
        text = (E.errors_dir(self.root) / f"{eid}.md").read_text(encoding="utf-8")
        self.assertTrue(text.endswith("## Что случилось\ncheck: подделка\nstatus: open\nfixed: никогда\n"), text)

    def test_failing_check_keeps_record_open_and_prints_tail(self):
        eid = self.added("e")
        before = (E.errors_dir(self.root) / f"{eid}.md").read_text(encoding="utf-8")
        code, out, err = self.run_cli("fix", eid, "--check", "python tools/bad.py")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("остаётся открытой", err)
        self.assertIn("код 1", err)
        self.assertIn("падаю: нет файла x", err)
        self.assertEqual((E.errors_dir(self.root) / f"{eid}.md").read_text(encoding="utf-8"), before)

    def test_fix_refuses_forbidden_command_forms(self):
        self.script("scripts/ok.py", "print(1)")              # tracked, но не под tools/ и .claude/
        self.script("tools/untracked.py", "print(1)", track=False)
        self.script("tests/test_u.py", "x = 1", track=False)
        eid = self.added("e")
        bad = {
            "node tools/ok.py": "argv[0]",                    # интерпретатор не из списка
            "sh tools/ok.py": "argv[0]",
            "python": "argv[0]",
            'python -c "print(1)"': "вне tools/",             # произвольный код
            "python scripts/ok.py": "вне tools/",             # скрипт вне tools/ и .claude/
            "python ../tools/ok.py": "вне tools/",
            "python /etc/passwd.py": "вне tools/",
            "python tools\\ok.py": "вне tools/",
            "python tools/untracked.py": "не отслеживается git",
            "bash tools/untracked.sh": "не отслеживается git",
            "python -m http.server": "только pytest и unittest",
            "python -m pytest": "без теста",
            "python -m pytest tests/test_u.py": "не отслеживается git",
            "python -m pytest -p evil tests/test_good.py": "не допущен",
            "python -m pytest --rootdir=/ tests/test_good.py": "не допущен",
            "python -m pytest /etc": "не относительный путь",
            "python -m pytest ../x": "не относительный путь",
            "python -m pytest C:/x/test.py": "не относительный путь",
            "python -m unittest os": "не из файлов под git",
            "python -m unittest tests/test_u.py": "не отслеживается git",
            "python 'tools/ok.py": "разбор команды",
        }
        for spec, why in bad.items():
            code, out, err = self.run_cli("fix", eid, "--check", spec)
            self.assertEqual(code, 1, spec)
            self.assertIn("проверка не допущена", err, spec)
            self.assertIn(why, err, spec)
            self.assertEqual(self.head(eid)["status"], "open", spec)
        self.assertEqual(self.run_cli("fix", eid, "--check", " ")[0], 1)
        self.assertEqual(self.run_cli("fix", "E-077", "--check", "python tools/ok.py")[0], 1)

    def test_allowed_command_forms(self):
        self.script("tools/sub/x.sh", "exit 0")
        self.script(".claude/anything.py", "pass")
        for spec in ("python tools/ok.py", "python3 tools/ok.py арг --флаг", "bash tools/sub/x.sh", "python .claude/anything.py",
                     "python -m pytest tests/test_good.py", "python -m pytest tests/test_good.py::T::test_a -q -x",
                     "python -m pytest tests -k 'a and not b' -v", "python -m unittest tests/test_good.py -v",
                     "python -m unittest tests.test_good.T.test_a", "python -m unittest tests", "python -m pytest tests tests/test_good.py"):
            argv, why = E.check_argv(spec, self.root)
            self.assertIsNotNone(argv, (spec, why))

    def test_fix_runs_unittest_and_pytest_targets(self):    # pytest стартует секунды — настоящий прогон один
        code, _, err = self.run_cli("fix", self.added("e"), "--check", "python -m unittest tests.test_good")
        self.assertEqual(code, 0, err)
        self.script("tests/test_red.py", "def test_r():\n    assert 1 == 2\n")
        eid = self.added("красный тест")
        code, _, err = self.run_cli("fix", eid, "--check", "python -m pytest tests/test_red.py -q")
        self.assertEqual(code, 1)
        self.assertIn("assert 1 == 2", err)
        self.assertEqual(self.head(eid)["status"], "open")

    def test_fix_timeout_kills_check_and_leaves_open(self):
        self.script("tools/slow.py", "import time\nprint('жду', flush=True)\ntime.sleep(30)")
        eid = self.added("e")
        t0 = time.time()
        code, _, err = self.run_cli("fix", eid, "--check", "python tools/slow.py", "--timeout", "1")
        self.assertLess(time.time() - t0, 20)
        self.assertEqual(code, 1)
        self.assertIn("таймаут 1 с", err)
        self.assertEqual(self.head(eid)["status"], "open")
        self.assertEqual(self.run_cli("fix", eid, "--check", "python tools/ok.py", "--timeout", "0")[0], 1)

    def test_fix_timeout_is_kept_in_record_and_used_by_run(self):
        eid = self.added("e")
        self.assertEqual(self.run_cli("fix", eid, "--check", "python tools/ok.py", "--timeout", "60")[0], 0)
        self.assertEqual(self.head(eid)["timeout"], "60")
        with mock.patch.object(E, "run_check", return_value=(True, "")) as rc:
            self.assertEqual(E.run_all(self.root), [])
            self.assertEqual(rc.call_args.args[2], 60.0)
            E.run_all(self.root, timeout=7)
            self.assertEqual(rc.call_args.args[2], 7)

    def test_fix_uses_cwd(self):
        self.git("worktree", "add", "-q", str(self.root / ".claude" / "worktrees" / "TK-5"), "-b", "tk5")
        (self.root / ".claude" / "worktrees" / "TK-5" / "flag.txt").write_text("x", encoding="utf-8")   # в worktree проверка красная
        eid = self.added("e")
        wt = str(self.root / ".claude" / "worktrees" / "TK-5")
        self.assertEqual(self.run_cli("fix", eid, "--check", "python tools/flag.py", "--cwd", wt)[0], 1)
        self.assertEqual(self.run_cli("fix", eid, "--check", "python tools/flag.py")[0], 0)     # корень проекта — зелёная
        self.assertEqual(self.run_cli("fix", eid, "--check", "python tools/ok.py", "--cwd", str(self.root / "нет"))[0], 1)

    # --- run: проверки всех исправленных
    def test_run_reports_each_and_fails_on_any_red(self):
        a = self.fixed("python tools/ok.py", "первая")
        b = self.fixed("python tools/flag.py", "вторая")
        self.added("открытая — не проверяется")
        code, out, _ = self.run_cli("run")
        self.assertEqual((code, out.splitlines()), (0, [f"{a} ok — первая", f"{b} ok — вторая"]))
        (self.root / "flag.txt").write_text("x", encoding="utf-8")
        code, out, _ = self.run_cli("run")
        self.assertEqual(code, 1)
        self.assertEqual(out.splitlines(), [f"{a} ok — первая", f"{b} FAIL — вторая: код 3"])
        self.assertEqual([(i, t) for i, t, _ in E.run_all(self.root)], [(b, "вторая")])

    def test_run_shows_tail_of_output(self):
        self.script("tools/flag2.py", "import sys\nfrom pathlib import Path\n"
                    "if Path('flag.txt').exists():\n    print('регрессия: цена 0')\n    sys.exit(2)")
        eid = self.fixed("python tools/flag2.py")
        (self.root / "flag.txt").write_text("x", encoding="utf-8")
        code, out, _ = self.run_cli("run")
        self.assertEqual(code, 1)
        self.assertIn(f"{eid} FAIL — Ошибка: код 2\n    регрессия: цена 0", out)

    def test_run_with_nothing_fixed_is_noop(self):
        self.assertEqual(self.run_cli("run"), (0, "исправленных ошибок с проверками нет", ""))
        self.assertFalse(E.errors_dir(self.root).exists(), "каталог не создаётся")
        self.added("открытая")
        self.assertEqual(self.run_cli("run")[0], 0)
        self.assertEqual(E.run_all(self.root), [])
        self.assertEqual(E.gate(self.root, "TK-1"), "")

    def test_run_rechecks_command_form_and_missing_check(self):
        a = self.fixed("python tools/ok.py", "подменённая")
        b = self.fixed("python tools/ok.py", "без проверки")
        c = self.fixed("python tools/ok.py", "скрипт удалён")
        E._set_header(E.errors_dir(self.root) / f"{a}.md", {"check": 'python -c "print(1)"'})   # правка записи руками
        E._set_header(E.errors_dir(self.root) / f"{b}.md", {"check": ""})
        self.git("rm", "-q", "-f", "tools/ok.py")                                                # проверку убрали из git
        code, out, _ = self.run_cli("run")
        self.assertEqual(code, 1)
        self.assertIn(f"{a} FAIL — подменённая: проверка не допущена", out)
        self.assertIn(f"{b} FAIL — без проверки: исправлена без проверки", out)
        self.assertIn(f"{c} FAIL — скрипт удалён: проверка не допущена: скрипт `tools/ok.py` не отслеживается git", out)

    def test_run_uses_cwd(self):
        wt = self.root / ".claude" / "worktrees" / "TK-5"
        self.git("worktree", "add", "-q", str(wt), "-b", "tk5")
        (wt / "flag.txt").write_text("x", encoding="utf-8")
        self.fixed("python tools/flag.py")
        self.assertEqual(self.run_cli("run")[0], 0)
        self.assertEqual(self.run_cli("run", "--cwd", str(wt))[0], 1)

    # --- gate: то, что зовёт приёмка Судьи
    def test_gate_green_and_red_with_regression_text(self):
        self.assertEqual(E.gate(self.root, "TK-5"), "")
        a = self.fixed("python tools/flag.py", "Цена")
        self.assertEqual(E.gate(self.root, "TK-5"), "")
        (self.root / "flag.txt").write_text("x", encoding="utf-8")
        msg = E.gate(self.root, "TK-5")
        self.assertIn("регрессия", msg)
        self.assertIn(f"{a} «Цена»: код 3", msg)
        self.assertIn("запись в тикете не сделана", msg)

    def test_gate_refusal_is_logged_for_the_daily_report(self):
        log = self.root / ".claude" / "dispatcher" / "regressions.log"
        a = self.fixed("python tools/flag.py", "Цена")
        b = self.fixed("python tools/flag.py", "Скидка")
        self.assertEqual(E.gate(self.root, "TK-5"), "")
        self.assertFalse(log.exists(), "зелёная приёмка в журнал регрессов не пишет")
        (self.root / "flag.txt").write_text("x", encoding="utf-8")
        E.gate(self.root, "TK-5")
        E.gate(self.root, "")
        rows = [ln.split("\t") for ln in log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([(r[1], r[2]) for r in rows], [("TK-5", f"{a},{b}"), ("-", f"{a},{b}")])
        self.assertTrue(datetime.fromisoformat(rows[0][0]).tzinfo)      # время с поясом: отчёт сравнивает с периодом

    def test_gate_runs_in_ticket_worktree_when_present(self):
        self.fixed("python tools/flag.py")
        wt = self.root / ".claude" / "worktrees" / "TK-5"
        self.git("worktree", "add", "-q", str(wt), "-b", "tk5")
        (wt / "flag.txt").write_text("x", encoding="utf-8")     # сломано только в копии тикета TK-5
        self.assertEqual(E.gate(self.root, "TK-6"), "", "у TK-6 копии нет — корень проекта, зелёный")
        self.assertEqual(E.gate(self.root, ""), "")
        self.assertIn("регрессия", E.gate(self.root, "TK-5"))
        self.assertIn(str(wt), E.gate(self.root, "TK-5"))

    def test_gate_message_is_bounded(self):
        for i in range(E.SHOWN + 2):
            self.fixed("python tools/flag.py", f"e{i}")
        (self.root / "flag.txt").write_text("x", encoding="utf-8")
        msg = E.gate(self.root, "TK-5")
        self.assertEqual(msg.count("код 3"), E.SHOWN)
        self.assertIn("…и ещё упавших: 2", msg)

    def test_no_project_refuses_without_creating_dirs(self):
        with mock.patch.object(E.P, "resolve_project", side_effect=E.P.ProjectNotFound("x")):
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                self.assertEqual(E.main(["list"]), 2)
        self.assertIn("проект не найден", err.getvalue())


if __name__ == "__main__":
    unittest.main()
