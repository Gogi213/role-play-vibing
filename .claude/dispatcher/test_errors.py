"""V2-3: реестр ошибок — ошибка исправлена, только когда проверка зелёная; `run` и приёмка гоняют проверки всех исправленных.
CP1 (критерий 7): исправленной объявляет только Судья; проверка обязана падать до исправления и проходить после; файлы проверки
при приёмке сверяются с версией на fixed_sha."""
import io
import os
import re
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
        # Коммит C0 — «до исправления»: в нём есть flag.txt и нет fixed.txt. Коммит исправления (`fix_commit`) убирает flag.txt
        # и кладёт fixed.txt. Проверки ниже красные на C0 и зелёные после исправления.
        self.script("tools/ok.py", "import sys\nfrom pathlib import Path\nprint('зелёный')\n"
                    "sys.exit(0 if Path('fixed.txt').exists() else 1)")
        self.script("tools/bad.py", "import sys\nprint('падаю: нет файла x')\nsys.exit(1)")
        # зелёная, пока рядом нет flag.txt — им тест и «ломает» исправленную ошибку
        self.script("tools/flag.py", "import sys\nfrom pathlib import Path\n"
                    "sys.exit(3 if Path('flag.txt').exists() else 0)")
        self.script("tests/__init__.py", "")   # обычный пакет: чужой `tests` из site-packages не перекроет
        self.script("tests/test_good.py", "import unittest\nfrom pathlib import Path\n\n\nclass T(unittest.TestCase):\n"
                    "    def test_a(self):\n        self.assertTrue(Path('fixed.txt').exists())\n")
        self.script("flag.txt", "ошибка есть")
        self.git("commit", "-qm", "init")
        self.c0 = self.git("rev-parse", "HEAD")

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

    def fix_commit(self):
        """Коммит исправления: flag.txt уходит, fixed.txt появляется; заодно фиксирует всё, что добавлено `script()`."""
        if self.git("ls-files", "flag.txt"):
            self.git("rm", "-q", "-f", "flag.txt")
        (self.root / "fixed.txt").write_text("исправлено\n", encoding="utf-8")
        self.git("add", "fixed.txt")
        self.git("commit", "-qm", "fix", "--allow-empty")

    def run_cli(self, *args, root=None):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = E.main(["--project", str(root or self.root), *args])
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
        """Ошибка, объявленная исправленной: заведена, исправление закоммичено, проверка красная на C0 и зелёная сейчас."""
        eid = self.added(title)
        self.fix_commit()
        code, _, err = self.run_cli("fix", eid, "--check", check, "--before", self.c0)
        self.assertEqual(code, 0, err)
        return eid

    def fixed_fast(self, check="python tools/flag.py", title="Ошибка"):
        """Как `fixed()`, но запись переводится в fixed напрямую, без `fix` (и без прогона «до»): для тестов приёмки, где
        важны прогон и отказ, а не сам `fix` — их много, а каждый настоящий `fix` стоит секунд."""
        eid = self.added(title)
        self.fix_commit()
        E._set_header(E.errors_dir(self.root) / f"{eid}.md", {"status": "fixed", "check": check, "fixed": "2026-10-14 12:00",
                                                              "fixed_sha": self.git("rev-parse", "--short", "HEAD")})
        return eid

    # --- add / list / show
    def test_add_numbers_and_record_format(self):
        code, out, _ = self.run_cli("add", "--title", "Цена  считается\nс  ошибкой", "--ticket", "tk-236",
                                    "--what", "цена 0 вместо 10\nпараметр check: x")
        self.assertEqual((code, out), (0, "E-001"))
        self.assertEqual(self.added("вторая"), "E-002")
        text = (E.errors_dir(self.root) / "E-001.md").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\nid: E-001\ntitle: Цена считается с ошибкой\nticket: TK-236\nfound: "), text)
        self.assertIn(f"added_sha: {self.git('rev-parse', '--short', 'HEAD')}\nstatus: open\ncheck:\nfixed:\nfixed_sha:\n"
                      "timeout:\n---\n## Что случилось\nцена 0 вместо 10\nпараметр check: x\n", text)
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

    def test_list_show_and_open_filter(self):
        a = self.added("открытая", ticket="TK-5")
        b = self.fixed(title="исправленная")
        code, out, _ = self.run_cli("list")
        self.assertEqual(out.splitlines(), [f"{a} [open] открытая (TK-5)", f"{b} [fixed] исправленная"])
        self.assertEqual(self.run_cli("list", "--open")[1], f"{a} [open] открытая (TK-5)")
        self.assertIn("title: исправленная", self.run_cli("show", b)[1])
        self.assertEqual(self.run_cli("show", "E-9")[0], 1)

    # --- fix: исправленной ошибку объявляет Судья (или сессия без роли)
    def test_any_role_may_add_but_only_judge_or_session_without_role_may_fix(self):
        for role in ("researcher", "engineer", "judge"):
            with mock.patch.dict(os.environ, {"RPV_ROLE": role}):
                self.added(f"от {role}")                                    # заводить может любая роль
        eid = self.added("e")
        self.fix_commit()
        before = (E.errors_dir(self.root) / f"{eid}.md").read_text(encoding="utf-8")
        for role in ("researcher", "engineer", "ceo", "неизвестная"):
            with mock.patch.dict(os.environ, {"RPV_ROLE": role}):
                code, out, err = self.run_cli("fix", eid, "--check", "python tools/ok.py", "--before", self.c0)
            self.assertEqual((code, out), (1, ""), role)
            self.assertIn("исправленной ошибку объявляет Судья", err, role)
            self.assertIn(role, err)
            self.assertIn("tickets.py result", err)
            self.assertIn("с указанием проверки", err)
            self.assertEqual((E.errors_dir(self.root) / f"{eid}.md").read_text(encoding="utf-8"), before, role)
        with mock.patch.dict(os.environ, {"RPV_ROLE": " Judge "}):       # Судья — объявляет
            code, _, err = self.run_cli("fix", eid, "--check", "python tools/ok.py", "--before", self.c0)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.head(eid)["status"], "fixed")
        eid2 = self.added("e2")                                            # сессия без роли (CEO, владелец) — тоже
        self.assertEqual(self.run_cli("fix", eid2, "--check", "python tools/ok.py", "--before", self.c0)[0], 0)

    def test_role_refusal_comes_before_any_check_runs(self):
        eid = self.added("e")
        with mock.patch.dict(os.environ, {"RPV_ROLE": "engineer"}), \
                mock.patch.object(E, "run_check", side_effect=AssertionError("проверка не должна запускаться")):
            self.assertEqual(self.run_cli("fix", eid, "--check", "python tools/ok.py")[0], 1)
            self.assertEqual(self.run_cli("fix", "E-077", "--check", "python tools/ok.py")[0], 1)

    def test_roles_may_still_read_and_run(self):
        eid = self.fixed()
        with mock.patch.dict(os.environ, {"RPV_ROLE": "engineer"}):
            self.assertEqual(self.run_cli("run")[0], 0)
            self.assertEqual(self.run_cli("list")[0], 0)
            self.assertIn(eid, self.run_cli("show", eid)[1])

    # --- fix: проверка запускается, и только красная до и зелёная после закрывает ошибку
    def test_fix_runs_check_and_records_it(self):
        eid = self.added("e")                                              # added_sha = C0, где ошибка есть
        self.fix_commit()
        code, out, err = self.run_cli("fix", "E-1", "--check", "python tools/ok.py")   # E-1 = E-001; «до» — added_sha
        self.assertEqual(code, 0, err)
        self.assertIn("E-001: fixed", out)
        self.assertIn(f"падала на {self.c0[:10]}", out)
        h = self.head(eid)
        self.assertEqual((h["status"], h["check"], h["fixed_sha"]), ("fixed", "python tools/ok.py", self.git("rev-parse", "--short", "HEAD")))
        self.assertEqual(h["added_sha"], self.git("rev-parse", "--short", self.c0), "отметка «до» остаётся прежней")
        self.assertRegex(h["fixed"], r"^\d{4}-\d\d-\d\d \d\d:\d\d$")
        self.assertEqual(h["timeout"], "")

    def test_fix_body_is_not_touched_by_header_edit(self):
        eid = self.added("e", what="check: подделка\nstatus: open\nfixed: никогда")
        self.fix_commit()
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

    def test_fix_refuses_check_that_is_already_green_before(self):
        self.fix_commit()
        eid = self.added("e")                         # added_sha — уже исправленный коммит: на нём проверка зелёная
        code, out, err = self.run_cli("fix", eid, "--check", "python tools/ok.py")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("проверка не ловит ошибку", err)
        self.assertIn(f"на {self.head(eid)['added_sha']}", err)
        self.assertIn("уже зелёная", err)
        self.assertEqual(self.head(eid)["status"], "open")
        self.assertEqual(self.head(eid)["fixed_sha"], "")
        code, out, err = self.run_cli("fix", eid, "--check", "python tools/ok.py", "--before", self.c0)   # коммит, где ошибка была
        self.assertEqual(code, 0, err)
        self.assertEqual(self.head(eid)["status"], "fixed")

    def test_fix_before_flag_overrides_added_sha(self):
        eid = self.added("e")
        self.fix_commit()
        later = self.added("e2")                       # added_sha позже исправления — «до» не годится
        self.assertEqual(self.run_cli("fix", later, "--check", "python tools/ok.py")[0], 1)
        self.assertEqual(self.run_cli("fix", later, "--check", "python tools/ok.py", "--before", self.c0[:9])[0], 0)
        self.assertEqual(self.run_cli("fix", eid, "--check", "python tools/ok.py")[0], 0)

    def test_fix_old_record_without_added_sha_needs_before(self):
        eid = self.added("e")
        self.fix_commit()
        path = E.errors_dir(self.root) / f"{eid}.md"
        text = path.read_text(encoding="utf-8")
        for old in (re.sub(r"^added_sha:.*\n", "", text, flags=re.M), text.replace(f"added_sha: {self.head(eid)['added_sha']}", "added_sha:")):
            path.write_text(old, encoding="utf-8")
            code, out, err = self.run_cli("fix", eid, "--check", "python tools/ok.py")
            self.assertEqual((code, out), (1, ""))
            self.assertIn("--before <коммит>", err)
            self.assertIn("остаётся открытой", err)
            self.assertEqual(self.head(eid)["status"], "open")
        self.assertEqual(self.run_cli("fix", eid, "--check", "python tools/ok.py", "--before", self.c0)[0], 0)

    def test_fix_refuses_unknown_before_commit(self):
        eid = self.added("e")
        self.fix_commit()
        for before in ("0123456789abcdef", "нет-такого-коммита", "HEAD:tools/ok.py"):
            code, _, err = self.run_cli("fix", eid, "--check", "python tools/ok.py", "--before", before)
            self.assertEqual(code, 1, before)
            self.assertIn("не найден", err, before)
        code, _, err = self.run_cli("fix", eid, "--check", "python tools/ok.py", "--before=-x")
        self.assertEqual(code, 1)
        self.assertIn("нужен коммит", err)
        self.assertEqual(self.head(eid)["status"], "open")

    def test_fix_outside_git_repository_refuses(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve()
            (root / "tools").mkdir()
            (root / "tools" / "ok.py").write_text("pass\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"GIT_CEILING_DIRECTORIES": str(root.parent)}):
                code, out, err = self.run_cli("add", "--title", "вне git", root=root)
                self.assertEqual((code, out), (0, "E-001"))
                code, out, err = self.run_cli("fix", "E-001", "--check", "python tools/ok.py", "--before", "HEAD", root=root)
            self.assertEqual((code, out), (1, ""))
            self.assertIn("не git-репозиторий", err)
            self.assertEqual(E.header(E.errors_dir(root) / "E-001.md")["status"], "open")

    def test_new_check_file_is_copied_to_the_old_commit(self):
        eid = self.added("e")                                              # added_sha = C0: новых проверок в нём нет
        self.script("tools/new_green.py", "print('зелёная всегда')")
        self.script("tools/new_red.py", "import sys\nfrom pathlib import Path\n"
                    "sys.exit(0 if Path('fixed.txt').exists() else 1)")
        self.fix_commit()
        # Без копирования файла на C0 эта проверка «упала» бы от его отсутствия и ошибку закрыли бы зря.
        code, _, err = self.run_cli("fix", eid, "--check", "python tools/new_green.py")
        self.assertEqual(code, 1)
        self.assertIn("уже зелёная", err)
        self.assertEqual(self.head(eid)["status"], "open")
        code, _, err = self.run_cli("fix", eid, "--check", "python tools/new_red.py")     # красная на C0 уже по делу
        self.assertEqual(code, 0, err)

    def test_new_unittest_module_is_copied_to_the_old_commit(self):
        eid = self.added("e")
        self.script("tests/test_new.py", "import unittest\nfrom pathlib import Path\n\n\nclass T(unittest.TestCase):\n"
                    "    def test_a(self):\n        self.assertTrue(Path('fixed.txt').exists())\n\n"
                    "    def test_b(self):\n        self.assertTrue(True)\n")
        self.fix_commit()
        code, _, err = self.run_cli("fix", eid, "--check", "python -m unittest tests.test_new.T.test_b")
        self.assertEqual(code, 1)                                          # test_b зелёный и до исправления
        self.assertIn("уже зелёная", err)
        code, _, err = self.run_cli("fix", eid, "--check", "python -m unittest tests.test_new.T.test_a")
        self.assertEqual(code, 0, err)

    def test_before_run_uses_temp_worktree_and_cleans_up(self):
        eid = self.added("e")
        self.fix_commit()
        made, real = [], tempfile.mkdtemp

        def spy(*a, **kw):
            made.append(Path(real(*a, **kw)))
            return str(made[-1])
        with mock.patch.object(E.tempfile, "mkdtemp", side_effect=spy), \
                mock.patch.object(E, "run_check", wraps=E.run_check) as rc:
            code, _, err = self.run_cli("fix", eid, "--check", "python tools/ok.py")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(made), 1)
        self.assertEqual(made[0].parent.resolve(), Path(tempfile.gettempdir()).resolve(), "каталог ОС, не проект")
        self.assertFalse(made[0].exists(), "временное дерево убрано")
        self.assertEqual([Path(c.args[1]).resolve() for c in rc.call_args_list][0], self.root)
        self.assertEqual(Path(rc.call_args_list[1].args[1]).name, "tree", "второй прогон — во временном дереве")
        self.assertEqual(len(self.git("worktree", "list").splitlines()), 1, "git не помнит временного дерева")
        self.assertEqual(self.run_cli("fix", eid, "--check", "python tools/ok.py", "--before", self.fix_sha())[0], 1)
        self.assertEqual(len(self.git("worktree", "list").splitlines()), 1, "и после отказа тоже")

    def fix_sha(self):
        return self.git("rev-parse", "HEAD")

    def test_fix_requires_check_files_committed(self):
        eid = self.added("e")
        self.fix_commit()
        p = self.root / "tools" / "ok.py"
        p.write_text(p.read_text(encoding="utf-8") + "# правка\n", encoding="utf-8")   # изменён, не закоммичен
        code, out, err = self.run_cli("fix", eid, "--check", "python tools/ok.py")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("не закоммичены", err)
        self.assertIn("tools/ok.py", err)
        self.git("add", "tools/ok.py")                                                  # в индексе, но не в коммите — тоже
        self.assertEqual(self.run_cli("fix", eid, "--check", "python tools/ok.py")[0], 1)
        self.git("commit", "-qm", "правка")
        self.assertEqual(self.run_cli("fix", eid, "--check", "python tools/ok.py")[0], 0)
        self.assertEqual(self.head(eid)["fixed_sha"], self.git("rev-parse", "--short", "HEAD"))

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

    def test_check_targets_name_the_files_of_every_command_form(self):
        self.script("tools/sub/x.sh", "exit 0")
        for spec, want in (("python tools/ok.py арг --флаг", ["tools/ok.py"]), ("bash tools/sub/x.sh", ["tools/sub/x.sh"]),
                           ("python -m pytest tests/test_good.py::T::test_a -q -x", ["tests/test_good.py"]),
                           ("python -m pytest tests tests/test_good.py -k 'a and not b' -v", ["tests", "tests/test_good.py"]),
                           ("python -m unittest tests.test_good.T.test_a", ["tests/test_good.py"]),
                           ("python -m unittest tests/test_good.py -v", ["tests/test_good.py"]),
                           ("python -m unittest tests", ["tests"])):
            argv, why = E.check_argv(spec, self.root)
            self.assertEqual(E.check_targets(argv, self.root), want, spec)

    def test_fix_runs_unittest_and_pytest_targets(self):    # pytest стартует секунды — настоящий прогон один
        eid = self.added("e")
        self.fix_commit()
        code, _, err = self.run_cli("fix", eid, "--check", "python -m unittest tests.test_good")
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
        self.fix_commit()
        self.assertEqual(self.run_cli("fix", eid, "--check", "python tools/ok.py", "--timeout", "60")[0], 0)
        self.assertEqual(self.head(eid)["timeout"], "60")
        with mock.patch.object(E, "run_check", return_value=(True, "")) as rc:
            self.assertEqual(E.run_all(self.root), [])
            self.assertEqual(rc.call_args.args[2], 60.0)
            E.run_all(self.root, timeout=7)
            self.assertEqual(rc.call_args.args[2], 7)

    def test_fix_uses_cwd(self):
        eid = self.added("e")
        self.fix_commit()
        wt = self.root / ".claude" / "worktrees" / "TK-5"
        self.git("worktree", "add", "-q", str(wt), "-b", "tk5")
        (wt / "flag.txt").write_text("x", encoding="utf-8")   # в worktree проверка красная
        self.assertEqual(self.run_cli("fix", eid, "--check", "python tools/flag.py", "--cwd", str(wt))[0], 1)
        self.assertEqual(self.run_cli("fix", eid, "--check", "python tools/flag.py")[0], 0)     # корень проекта — зелёная
        self.assertEqual(self.run_cli("fix", eid, "--check", "python tools/ok.py", "--cwd", str(self.root / "нет"))[0], 1)

    def test_fix_in_ticket_worktree_checks_red_on_added_sha_and_records_its_head(self):
        eid = self.added("e")                                              # added_sha = C0 основного дерева
        wt = self.root / ".claude" / "worktrees" / "TK-5"
        self.git("worktree", "add", "-q", str(wt), "-b", "tk5")            # копия тикета — от C0, до исправления

        def wt_git(*a):
            return subprocess.run(["git", "-C", str(wt), "-c", "user.name=t", "-c", "user.email=t@t", *a],
                                  check=True, capture_output=True, text=True).stdout.strip()
        wt_git("rm", "-q", "-f", "flag.txt")                               # исправление и новая проверка — в ветке тикета
        (wt / "fixed.txt").write_text("исправлено\n", encoding="utf-8")
        (wt / "tools" / "own.py").write_text("import sys\nfrom pathlib import Path\n"
                                             "sys.exit(0 if Path('fixed.txt').exists() else 1)\n", encoding="utf-8")
        wt_git("add", "fixed.txt", "tools/own.py")
        wt_git("commit", "-qm", "исправление")
        code, out, err = self.run_cli("fix", eid, "--check", "python tools/own.py", "--cwd", str(wt))
        self.assertEqual(code, 0, err)                                     # новой проверки на C0 нет — её скопировали
        self.assertEqual(self.head(eid)["fixed_sha"], wt_git("rev-parse", "--short", "HEAD"))
        self.assertEqual(self.run_cli("run", "--cwd", str(wt))[0], 0)
        self.assertEqual(E.gate(self.root, "TK-5"), "")

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
        self.fixed("python tools/flag.py")
        wt = self.root / ".claude" / "worktrees" / "TK-5"
        self.git("worktree", "add", "-q", str(wt), "-b", "tk5")
        (wt / "flag.txt").write_text("x", encoding="utf-8")
        self.assertEqual(self.run_cli("run")[0], 0)
        self.assertEqual(self.run_cli("run", "--cwd", str(wt))[0], 1)

    # --- run / gate: файлы проверки сверяются с версией на fixed_sha
    def worktree(self, name="TK-5"):
        wt = self.root / ".claude" / "worktrees" / name
        self.git("worktree", "add", "-q", str(wt), "-b", name.lower())
        return wt

    def test_run_fails_when_check_file_was_weakened_in_the_working_copy(self):
        eid = self.fixed("python tools/flag.py", "Цена")
        wt = self.worktree()
        self.assertEqual(self.run_cli("run", "--cwd", str(wt))[0], 0, "копия как на fixed_sha — зелёная")
        (wt / "flag.txt").write_text("x", encoding="utf-8")                      # ошибка вернулась…
        (wt / "tools" / "flag.py").write_text("import sys\nsys.exit(0)\n", encoding="utf-8")   # …а автор ослабил проверку
        code, out, _ = self.run_cli("run", "--cwd", str(wt))
        self.assertEqual(code, 1)
        self.assertIn(f"{eid} FAIL — Цена: проверка изменена после исправления", out)
        self.assertIn("только через Судью (`errors.py fix` заново)", out)
        self.assertIn("tools/flag.py отличается от версии на " + self.head(eid)["fixed_sha"], out)
        msg = E.gate(self.root, "TK-5")                                          # приёмка идёт в worktree тикета
        self.assertIn("регрессия", msg)
        self.assertIn("проверка изменена после исправления", msg)
        self.assertEqual(E.gate(self.root, "TK-6"), "", "у TK-6 копии нет — корень проекта, правки там нет")

    def test_weakened_check_is_not_run_at_all(self):
        self.fixed("python tools/flag.py", "Цена")
        (self.root / "tools" / "flag.py").write_text("import sys\nsys.exit(0)\n", encoding="utf-8")   # зелёная, но не та
        with mock.patch.object(E, "run_check", side_effect=AssertionError("запускать нельзя")):
            bad = E.run_all(self.root)
        self.assertEqual([(i, t) for i, t, _ in bad], [("E-001", "Цена")])

    def test_deleted_check_file_fails_too(self):
        self.fixed("python -m unittest tests.test_good", "Тест")
        wt = self.worktree()
        (wt / "tests" / "test_good.py").unlink()                                 # в индексе есть, в рабочей копии нет
        code, out, _ = self.run_cli("run", "--cwd", str(wt))
        self.assertEqual(code, 1)
        self.assertIn("проверка изменена после исправления", out)
        self.assertIn("tests/test_good.py удалён", out)

    def test_directory_target_compares_every_file_that_was_there(self):
        eid = self.fixed("python -m unittest tests.test_good", "Каталог")
        self.assertEqual(E.tampered(self.root, self.root, self.head(eid), ["python", "-m", "unittest", "tests"]), ("", ""))
        new = self.root / "tests" / "test_extra.py"                              # новый файл рядом — не ослабление
        new.write_text("x = 1\n", encoding="utf-8")
        self.assertEqual(E.tampered(self.root, self.root, self.head(eid), ["python", "-m", "unittest", "tests"]), ("", ""))
        (self.root / "tests" / "test_good.py").write_text("x = 1\n", encoding="utf-8")   # файл каталога правили
        why, _ = E.tampered(self.root, self.root, self.head(eid), ["python", "-m", "unittest", "tests"])
        self.assertIn("tests/test_good.py отличается", why)

    def test_line_endings_alone_are_not_a_change(self):
        self.fixed("python tools/flag.py")
        p = self.root / "tools" / "flag.py"
        raw = p.read_bytes().replace(b"\r\n", b"\n")
        p.write_bytes(raw.replace(b"\n", b"\r\n"))
        self.assertEqual(self.run_cli("run")[0], 0)
        p.write_bytes(raw)
        self.assertEqual(self.run_cli("run")[0], 0)

    def test_run_skips_comparison_for_old_records_and_says_so(self):
        self.script("tools/late.py", "import sys\nfrom pathlib import Path\n"
                    "sys.exit(0 if Path('fixed.txt').exists() else 1)")           # на C0 такого файла нет
        eid = self.fixed("python tools/late.py", "Старая")
        path = E.errors_dir(self.root) / f"{eid}.md"
        for sha, note in (("", "в записи нет fixed_sha"), (self.c0[:7], f"на {self.c0[:7]} файла проверки нет"),
                          ("0123abc", "коммита 0123abc нет в репозитории")):
            E._set_header(path, {"fixed_sha": sha})
            code, out, _ = self.run_cli("run")
            self.assertEqual(code, 0, out)
            self.assertEqual(out, f"{eid} ok — Старая (сверка файла проверки пропущена: {note})")
        self.assertEqual(E.gate(self.root, "TK-1"), "", "пропуск сверки приёмку не блокирует")

    def test_fix_again_after_a_legitimate_change_moves_fixed_sha(self):
        eid = self.fixed("python tools/flag.py")
        old = self.head(eid)["fixed_sha"]
        p = self.root / "tools" / "flag.py"
        p.write_text(p.read_text(encoding="utf-8") + "# пояснение\n", encoding="utf-8")
        self.git("add", "tools/flag.py")
        self.git("commit", "-qm", "пояснение")
        self.assertEqual(self.run_cli("run")[0], 1, "без Судьи изменённая проверка приёмку не проходит")
        with mock.patch.dict(os.environ, {"RPV_ROLE": "engineer"}):
            self.assertEqual(self.run_cli("fix", eid, "--check", "python tools/flag.py")[0], 1, "и заново объявляет только Судья")
        with mock.patch.dict(os.environ, {"RPV_ROLE": "judge"}):
            code, _, err = self.run_cli("fix", eid, "--check", "python tools/flag.py")
        self.assertEqual(code, 0, err)
        self.assertNotEqual(self.head(eid)["fixed_sha"], old)
        self.assertEqual(self.run_cli("run")[0], 0)

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
        a = self.fixed_fast("python tools/flag.py", "Цена")
        b = self.fixed_fast("python tools/flag.py", "Скидка")
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
        wt = self.worktree()
        (wt / "flag.txt").write_text("x", encoding="utf-8")     # сломано только в копии тикета TK-5
        self.assertEqual(E.gate(self.root, "TK-6"), "", "у TK-6 копии нет — корень проекта, зелёный")
        self.assertEqual(E.gate(self.root, ""), "")
        self.assertIn("регрессия", E.gate(self.root, "TK-5"))
        self.assertIn(str(wt), E.gate(self.root, "TK-5"))

    def test_gate_message_is_bounded(self):
        for i in range(E.SHOWN + 2):
            self.fixed_fast("python tools/flag.py", f"e{i}")
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
