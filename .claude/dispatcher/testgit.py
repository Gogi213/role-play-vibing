"""Общий помощник тестов: `git` во временном каталоге с повтором (TK-140).

На раннере Windows `git init/commit/worktree add` во временной папке изредка падают сами по себе (замок индекса или
конфига, антивирус держит файл), причина в тексте ошибки, а `check=True` её прятал. Установка тестового репозитория
повторима, поэтому нулевой код ждётся до четырёх раз; не получилось — исключение с выводом git.
"""
from __future__ import annotations

import subprocess
import time

TRIES = 4


def run(args: list, **kw) -> subprocess.CompletedProcess:
    """`git <args>` (`capture_output`, `text`); ненулевой код — повтор с паузой; после последней попытки —
    `CalledProcessError`, в тексте которого stderr git."""
    for attempt in range(TRIES):
        done = subprocess.run(["git", *args], capture_output=True, text=True, encoding="utf-8", errors="replace", **kw)
        if done.returncode == 0:
            return done
        time.sleep(0.5 * (attempt + 1))
    raise subprocess.CalledProcessError(
        done.returncode, done.args, output=done.stdout,
        stderr=f"{done.stderr}\n[git {' '.join(map(str, args[:6]))}: {TRIES} попытки]") from None
