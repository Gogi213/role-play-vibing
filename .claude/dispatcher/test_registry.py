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
        self.assertEqual(self.run_cli("decide", "--quote", "новое", "--gist", "g")[1], "В-42")

    def test_decide_refuses_unknown_replaces_and_empty_quote(self):
        self.assertEqual(self.run_cli("decide", "--quote", "a b", "--gist", "g", "--replaces", "В-9")[0], 1)
        self.assertEqual(self.run_cli("decide", "--quote", " ", "--gist", "g")[0], 1)
        self.assertFalse(R.decisions_file(self.root).exists())

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
        self.run_cli("decide", "--quote", "a b", "--gist", "g")
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
        self.run_cli("decide", "--quote", "a b", "--gist", "g")
        for role in ("researcher", "engineer", "judge"):
            with mock.patch.dict(os.environ, {"RPV_ROLE": role}):
                for args in (("decide", "--quote", "x y", "--gist", "g"), ("goal", "--title", "t", "--quote", "x y"),
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
        self.run_cli("decide", "--quote", "первое", "--gist", "г1", "--at", "2026-10-14 10:00")
        self.run_cli("decide", "--quote", "второе", "--gist", "г2", "--at", "2026-10-14 11:00")
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
        code, _, err = self.run_cli("decide", "--quote", "a b", "--gist", "g", "--at", "14.10 10:00")
        self.assertEqual(code, 1)
        self.assertIn("ГГГГ-ММ-ДД", err)


if __name__ == "__main__":
    unittest.main()
