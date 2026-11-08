"""Общий помощник тестов: `git` во временном каталоге с повтором (TK-140).

На раннере Windows `git init/commit/worktree add` во временной папке изредка падают сами по себе, а `check=True` прятал
текст ошибки. Установка тестового репозитория повторима, поэтому нулевой код ждётся до четырёх раз; stderr каждой
неудачной попытки уходит в `warnings.warn` (видно в сводке pytest на CI — по нему и называется причина сбоя), не
получилось — исключение с выводом git. Файл с префиксом `test_`: страж TK-010 запрещает голый `git` вне тестов.
"""
from __future__ import annotations

import subprocess
import time
import warnings

TRIES = 4


def run(args: list, **kw) -> subprocess.CompletedProcess:
    """`git <args>` (`capture_output`, `text`); ненулевой код — предупреждение со stderr и повтор с паузой; после
    последней попытки — `CalledProcessError`, в тексте которого stderr git."""
    for attempt in range(TRIES):
        done = subprocess.run(["git", *args], capture_output=True, text=True, encoding="utf-8", errors="replace", **kw)
        if done.returncode == 0:
            return done
        warnings.warn(f"git {' '.join(map(str, args[:6]))}: попытка {attempt + 1}/{TRIES}, код {done.returncode}, "
                      f"stderr: {done.stderr.strip()!r}", RuntimeWarning, stacklevel=2)
        time.sleep(0.5 * (attempt + 1))
    raise subprocess.CalledProcessError(
        done.returncode, done.args, output=done.stdout,
        stderr=f"{done.stderr}\n[git {' '.join(map(str, args[:6]))}: {TRIES} попытки]") from None


import unittest  # noqa: E402
from unittest import mock  # noqa: E402


class RunRetryTests(unittest.TestCase):
    def test_failed_attempts_are_reported_with_stderr(self):
        with mock.patch.object(time, "sleep"), warnings.catch_warnings(record=True) as seen:
            warnings.simplefilter("always")
            with self.assertRaises(subprocess.CalledProcessError) as cm:
                run(["no-such-subcommand"])
        self.assertEqual(len(seen), TRIES)
        self.assertIn("no-such-subcommand", str(seen[0].message))
        self.assertIn("not a git command", str(seen[0].message))
        self.assertIn("not a git command", cm.exception.stderr)

    def test_success_is_silent(self):
        with warnings.catch_warnings(record=True) as seen:
            warnings.simplefilter("always")
            self.assertEqual(run(["--version"]).returncode, 0)
        self.assertEqual(seen, [])


if __name__ == "__main__":
    unittest.main()
