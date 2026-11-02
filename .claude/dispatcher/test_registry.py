"""V2: реестр владельца — решения В-N и цели G-NNN пишет только CEO, номер ставит команда, роли только читают."""
import io
import subprocess
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import registry as R  # noqa: E402


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        env = {k: v for k, v in os.environ.items() if k != "RPV_ROLE"}
        self.env = mock.patch.dict(os.environ, env, clear=True)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = R.main(["--project", str(self.root), *args])
        return code, out.getvalue().strip(), err.getvalue().strip()

    def test_decide_numbers_and_quotes_verbatim(self):
        code, out, _ = self.run_cli("decide", "--quote", "Шорты не добавляем пока стоп.\nМне нужны монеты",
                                    "--gist", "без шортов", "--at", "2026-10-14 10:00")
        self.assertEqual((code, out), (0, "В-1"))
        code, out, _ = self.run_cli("decide", "--quote", "давай раз в час", "--gist", "обход раз в час",
                                    "--replaces", "В-1", "--at", "2026-10-14 11:00")
        self.assertEqual((code, out), (0, "В-2"))
        text = R.decisions_file(self.root).read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# Решения владельца\n\n## В-1 · 2026-10-14 10:00\n"))
        self.assertIn("> Шорты не добавляем пока стоп.\n> Мне нужны монеты\nСуть: без шортов\nЗаменяет: —", text)
        self.assertIn("## В-2 · 2026-10-14 11:00\n> давай раз в час\nСуть: обход раз в час\nЗаменяет: В-1", text)
        self.assertEqual(list(R.decisions(self.root)), ["В-1", "В-2"])

    def test_decide_continues_existing_numbering(self):
        R.decisions_file(self.root).parent.mkdir(parents=True)
        R.decisions_file(self.root).write_text("# Решения владельца\n\n## В-41 · 2026-10-01 09:00\n> x y\n",
                                               encoding="utf-8")
        self.assertEqual(self.run_cli("decide", "--quote", "новое решение владельца", "--gist", "g")[1], "В-42")

    def test_decide_refuses_unknown_replaces_and_empty_quote(self):
        self.assertEqual(self.run_cli("decide", "--quote", "a b c d e f g h", "--gist", "g", "--replaces", "В-9")[0], 1)
        self.assertEqual(self.run_cli("decide", "--quote", " ", "--gist", "g")[0], 1)
        self.assertFalse(R.decisions_file(self.root).exists())

    def test_short_answer_needs_the_question(self):
        """TK-007 (дополнение 05:09): слово короче 15 знаков — ответ на вопрос CEO; без `--question` отказ с подсказкой."""
        for quote in ("да", "1 - да", "  да,\nда  ", "14 знаков-----"):
            code, _, err = self.run_cli("decide", "--quote", quote, "--gist", "g")
            self.assertEqual(code, 1, quote)
            self.assertIn("--question", err)
        self.assertFalse(R.decisions_file(self.root).exists())
        self.assertEqual(self.run_cli("decide", "--quote", "да", "--gist", "g", "--question", " ")[0], 1)   # пустой вопрос — не вопрос
        code, out, _ = self.run_cli("decide", "--quote", "да", "--gist", "оба сразу", "--at", "2026-10-27 04:41",
                                    "--question", "какой из двух вариантов\nили оба сразу")
        self.assertEqual((code, out), (0, "В-1"))
        text = R.decisions_file(self.root).read_text(encoding="utf-8")
        self.assertIn("## В-1 · 2026-10-27 04:41\n> да\nВопрос CEO: какой из двух вариантов или оба сразу\nСуть: оба сразу\n", text)
        self.assertEqual(R.question_of(R.decisions(self.root)["В-1"]), "какой из двух вариантов или оба сразу")
        # цитата от 15 знаков — вопрос не нужен; его строки нет, если не дан
        self.assertEqual(self.run_cli("decide", "--quote", "ровно пятнадцать", "--gist", "g")[0], 0)
        self.assertEqual(R.question_of(R.decisions(self.root)["В-2"]), "")
        self.assertNotIn("Вопрос CEO", R.decisions(self.root)["В-2"])

    def test_goal_file_and_not_named_criteria(self):
        code, out, _ = self.run_cli("goal", "--title", "Ускорение ×4", "--quote", "нужно еще раза в 4 быстрее",
                                    "--at", "2026-10-14 10:00")
        self.assertEqual((code, out), (0, "G-001"))
        text = (R.goals_dir(self.root) / "G-001.md").read_text(encoding="utf-8")
        self.assertIn("id: G-001\nstatus: open\nset: 2026-10-14 10:00\naccepted:\n---\n# Ускорение ×4", text)
        self.assertIn(f"## Критерии приёмки\n- {R.NOT_NAMED}", text)
        self.assertIn("Бюджет: не назван\nСрок: не назван", text)
        code, out, _ = self.run_cli("goal", "--title", "t", "--quote", "q q", "--criterion", "в 4 раза", "--criterion",
                                    "без потери качества")
        self.assertEqual(out, "G-002")
        self.assertIn("- в 4 раза\n- без потери качества", (R.goals_dir(self.root) / "G-002.md").read_text(encoding="utf-8"))

    def test_accept_and_drop_goal_and_ref_exists(self):
        self.run_cli("goal", "--title", "t", "--quote", "q q")
        self.run_cli("decide", "--quote", "a b c d e f g h", "--gist", "g")
        self.assertTrue(R.ref_exists(self.root, "G-001"))
        self.assertTrue(R.ref_exists(self.root, "В-1"))
        self.assertFalse(R.ref_exists(self.root, "В-2"))
        self.assertEqual(self.run_cli("accept-goal", "G-001", "--at", "2026-10-15 12:00")[0], 0)
        h = R.goal_header(R.goals_dir(self.root) / "G-001.md")
        self.assertEqual((h["status"], h["accepted"]), ("accepted", "2026-10-15 12:00"))
        self.assertFalse(R.ref_exists(self.root, "G-001"))  # принятая цель — не ссылка для новой работы
        self.run_cli("goal", "--title", "t2", "--quote", "q q")
        self.assertEqual(self.run_cli("drop-goal", "G-002", "--quote", "не надо")[0], 0)
        text = (R.goals_dir(self.root) / "G-002.md").read_text(encoding="utf-8")
        self.assertIn("status: dropped", text)
        self.assertIn("## Снята владельцем\n> не надо", text)

    def test_roles_only_read(self):
        self.run_cli("decide", "--quote", "a b c d e f g h", "--gist", "g")
        for role in ("researcher", "engineer", "judge"):
            with mock.patch.dict(os.environ, {"RPV_ROLE": role}):
                for args in (("decide", "--quote", "x y x y x y x y", "--gist", "g"), ("goal", "--title", "t", "--quote", "x y"),
                             ("accept-goal", "G-001")):
                    code, _, err = self.run_cli(*args)
                    self.assertEqual(code, 1, (role, args))
                    self.assertIn("только CEO", err)
                self.assertEqual(self.run_cli("show", "В-1")[0], 0)
                self.assertEqual(self.run_cli("list")[0], 0)
        self.assertEqual(list(R.decisions(self.root)), ["В-1"])

    def test_quote_from_stdin_kept_verbatim(self):
        quote = 'нет "не надо" сразу.\nсначала макет покажи'
        with mock.patch("sys.stdin", io.StringIO(quote + "\n")):
            self.assertEqual(self.run_cli("decide", "--quote", "-", "--gist", "сначала макет")[0], 0)
        self.assertIn('> нет "не надо" сразу.\n> сначала макет покажи\n',
                      R.decisions_file(self.root).read_text(encoding="utf-8"))

    def stdin_bytes(self, data: bytes, encoding: str):
        """stdin как у Windows с не-UTF-8 локалью: байты — UTF-8 владельца, текстовая обёртка — кодировка локали."""
        return io.TextIOWrapper(io.BytesIO(data), encoding=encoding)

    def test_quote_from_stdin_is_utf8_whatever_the_locale(self):
        quote = "их когда-то посчитали и это никогда не выводится"
        for locale_encoding in ("cp1251", "cp866", "cp1252"):
            with self.subTest(locale=locale_encoding), mock.patch("sys.stdin", self.stdin_bytes(quote.encode("utf-8") + b"\n", locale_encoding)):
                self.assertEqual(self.run_cli("decide", "--quote", "-", "--gist", f"проба {locale_encoding}")[0], 0)
        text = R.decisions_file(self.root).read_text(encoding="utf-8")
        self.assertEqual(text.count(f"> {quote}\n"), 3, text)

    def test_quote_from_stdin_drops_bom(self):
        with mock.patch("sys.stdin", self.stdin_bytes(b"\xef\xbb\xbf" + "слово владельца".encode("utf-8"), "cp1251")):
            self.assertEqual(self.run_cli("decide", "--quote", "-", "--gist", "bom")[0], 0)
        self.assertIn("> слово владельца\n", R.decisions_file(self.root).read_text(encoding="utf-8"))

    def test_quote_from_stdin_not_utf8_is_refused_not_garbled(self):
        with mock.patch("sys.stdin", self.stdin_bytes("слово владельца".encode("cp1251"), "cp1251")):
            code, _, err = self.run_cli("decide", "--quote", "-", "--gist", "не utf-8")
        self.assertEqual(code, 1)
        self.assertIn("не в UTF-8", err)
        self.assertFalse(R.decisions_file(self.root).exists(), "порванная цитата в реестр не попала")

    def test_real_process_with_windows_like_locale(self):
        """Настоящий процесс: stdin по умолчанию читается в cp1251 (как на Windows), цитата в реестре — без порчи."""
        quote = "пересчитать нельзя с нашего веба"
        env = dict(os.environ, PYTHONIOENCODING="cp1251", PYTHONUTF8="0")
        out = subprocess.run([sys.executable, str(Path(R.__file__)), "--project", str(self.root), "decide", "--quote", "-", "--gist", "веб"],
                             input=quote.encode("utf-8"), capture_output=True, env=env, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr.decode("utf-8", "replace"))
        self.assertIn(f"> {quote}\n", R.decisions_file(self.root).read_text(encoding="utf-8"))

    def test_repeat_marks_existing_decision_without_new_number(self):
        self.run_cli("decide", "--quote", "первое решение владельца", "--gist", "г1", "--at", "2026-10-14 10:00")
        self.run_cli("decide", "--quote", "второе решение владельца", "--gist", "г2", "--at", "2026-10-14 11:00")
        code, out, _ = self.run_cli("repeat", "В-1", "--quote", "я же говорил: первое", "--at", "2026-10-15 09:00")
        self.assertEqual((code, out), (0, "В-1: повтор №1"))
        self.run_cli("repeat", "В-2", "--quote", "второе!", "--at", "2026-10-15 09:30")
        self.run_cli("repeat", "В-2", "--quote", "второе!!", "--at", "2026-10-15 09:40")
        self.assertEqual(list(R.decisions(self.root)), ["В-1", "В-2"], "нового номера нет")
        self.assertEqual(R.repeats(self.root), {"В-1": 1, "В-2": 2})
        text = R.decisions_file(self.root).read_text(encoding="utf-8")
        self.assertIn("Заменяет: —\nПовторено владельцем · 2026-10-15 09:00: «я же говорил: первое»\n\n## В-2", text)
        self.assertTrue(text.endswith("Повторено владельцем · 2026-10-15 09:40: «второе!!»\n"))
        self.assertIn("В-2 — г2 (повторено владельцем: 2)", self.run_cli("list")[1])
        self.assertEqual(self.run_cli("repeat", "В-7", "--quote", "x y")[0], 1)
        with mock.patch.dict(os.environ, {"RPV_ROLE": "judge"}):
            self.assertEqual(self.run_cli("repeat", "В-1", "--quote", "x y")[0], 1)

    def test_bad_time(self):
        code, _, err = self.run_cli("decide", "--quote", "a b c d e f g h", "--gist", "g", "--at", "14.10 10:00")
        self.assertEqual(code, 1)
        self.assertIn("ГГГГ-ММ-ДД", err)


if __name__ == "__main__":
    unittest.main()


class ActiveRegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        env = {k: v for k, v in os.environ.items() if k != "RPV_ROLE"}
        self.env = mock.patch.dict(os.environ, env, clear=True)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def run_cli(self, *args):
        with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()):
            code = R.main(["--project", str(self.root), *args])
        return code, out.getvalue().strip()

    def active(self):
        return R.active_file(self.root).read_text(encoding="utf-8")

    def test_replaced_entry_is_dropped_and_chain_collapses(self):
        self.run_cli("decide", "--quote", "первое слово владельца", "--gist", "ПЕРВОЕ", "--at", "2026-10-01 10:00")
        self.run_cli("decide", "--quote", "второе слово владельца", "--gist", "ВТОРОЕ", "--replaces", "В-1", "--at", "2026-10-02 10:00")
        self.run_cli("decide", "--quote", "третье слово владельца", "--gist", "ТРЕТЬЕ", "--replaces", "В-2", "--at", "2026-10-03 10:00")
        self.run_cli("decide", "--quote", "независимое слово", "--gist", "НЕЗАВИСИМОЕ", "--at", "2026-10-04 10:00")
        text = self.active()
        self.assertEqual(R.active_decisions(self.root), ["В-3", "В-4"])
        self.assertNotIn("ПЕРВОЕ", text)
        self.assertNotIn("ВТОРОЕ", text)
        self.assertIn("В-3 · 2026-10-03 10:00 · ТРЕТЬЕ\n> третье слово владельца", text)
        self.assertEqual(text.splitlines()[0], R.ACTIVE_HEAD)

    def test_repeat_not_duplicated_and_question_kept(self):
        self.run_cli("decide", "--quote", "да", "--question", "Заводить тикет?", "--gist", "заводить", "--at", "2026-10-01 10:00")
        self.run_cli("repeat", "В-1", "--quote", "ну да же", "--at", "2026-10-02 10:00")
        text = self.active()
        self.assertEqual(text.count("В-1 ·"), 1)
        self.assertNotIn("Повторено", text)
        self.assertIn("Вопрос CEO: Заводить тикет?", text)

    def test_active_command_rebuilds_and_reports(self):
        R.decisions_file(self.root).parent.mkdir(parents=True)
        R.decisions_file(self.root).write_text(
            "# Решения владельца\n\n## В-1 · 2026-10-01 09:00\n> a b\nСуть: x\nЗаменяет: —\n\n"
            "## В-2 · 2026-10-02 09:00\n> c d\nСуть: y\nЗаменяет: В-1\n", encoding="utf-8")
        code, out = self.run_cli("active")
        self.assertEqual(code, 0)
        self.assertIn("пересобран, действующих 1 из 2", out)
        self.assertIn("без изменений", self.run_cli("active")[1])
        self.assertNotIn("В-1 ·", self.active())

    def test_claude_md_line_is_replaced_idempotently(self):
        (self.root / "CLAUDE.md").write_text("# P\n\nправила\n\n@.claude/decisions.md\n\n@other.md\n", encoding="utf-8")
        R.decisions_file(self.root).parent.mkdir(parents=True, exist_ok=True)
        R.decisions_file(self.root).write_text("# Решения владельца\n", encoding="utf-8")
        for _ in range(2):
            self.run_cli("link")
        text = (self.root / "CLAUDE.md").read_text(encoding="utf-8")
        self.assertEqual(text, "# P\n\nправила\n\n@.claude/decisions-active.md\n\n@other.md\n")
        self.assertTrue(R.active_file(self.root).exists())

    def test_claude_md_missing_or_without_line_gets_active_line_once(self):
        R.link_claude_md(self.root)
        R.link_claude_md(self.root)
        self.assertEqual((self.root / "CLAUDE.md").read_text(encoding="utf-8"), "@.claude/decisions-active.md\n")
        (self.root / "CLAUDE.md").write_text("текст", encoding="utf-8")
        R.link_claude_md(self.root)
        self.assertEqual((self.root / "CLAUDE.md").read_text(encoding="utf-8"), "текст\n\n@.claude/decisions-active.md\n")

    def test_both_lines_present_keeps_one(self):
        (self.root / "CLAUDE.md").write_text("@.claude/decisions-active.md\n@.claude/decisions.md\n", encoding="utf-8")
        R.link_claude_md(self.root)
        self.assertEqual((self.root / "CLAUDE.md").read_text(encoding="utf-8"), "@.claude/decisions-active.md\n")
