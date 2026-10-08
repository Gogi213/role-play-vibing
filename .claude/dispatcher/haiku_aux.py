#!/usr/bin/env python3
"""Рутина на Haiku 5.5 (TK-087): диагноз упавшего юнита и конспект длинного текста (лог тикета, сессия роли).

`claude -p` из временного каталога вне проекта (без CLAUDE.md, хуков, инструментов). Сбой/таймаут/нет claude — None, вызывающий
идёт прежним путём. Числа и вердикты исследования Haiku не выдаёт: в промптах это запрещено явно.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile

MODEL = os.environ.get("RPV_HAIKU_MODEL") or "claude-haiku-5-5"
EFFORT = os.environ.get("RPV_HAIKU_EFFORT") or "xhigh"
TIMEOUT_S = 90
MAX_IN = 12000  # знаков входа: хвост, не начало

DIAG_SYSTEM = ("Ты разбираешь упавшее задание на машине. По хвосту journalctl/вывода назови причину и предложение, 2-4 строки "
               "по-русски. Первая строка: класс причины — одно из: нехватка памяти | не тот путь или окно замера | битые данные | "
               "ошибка кода | таймаут | прочее. Дальше — на что в выводе опираешься (цитата) и что сделать. Чисел и выводов "
               "исследования не давай. Не уверен — так и скажи.")
COMPACT_SYSTEM = ("Сожми текст в конспект для следующей сессии: что решено, что сделано (хеши, пути, ID — дословно), что открыто. "
                  "Без оценок и новых выводов; числа и вердикты — только как в тексте, дословно. По-русски, до 25 строк.")


def _claude():
    c = os.environ.get("CLAUDE_BIN") or shutil.which("claude")
    return c if c and (os.path.isfile(c) or shutil.which(c)) else None


def ask(system: str, text: str, runner=subprocess.run, claude: str | None = None, timeout: int = TIMEOUT_S,
        as_json: bool = False):
    """Ответ модели (str) или None; as_json — весь JSON `--output-format json` (result, usage) как dict. Вход режется до хвоста MAX_IN знаков."""
    claude = claude if claude is not None else _claude()
    if not claude or not text.strip():
        return None
    cmd = [claude, "-p", "--model", MODEL, "--effort", EFFORT, "--system-prompt", system, "--tools", "",
           "--no-session-persistence"]
    if as_json:
        cmd += ["--output-format", "json"]
    try:
        with tempfile.TemporaryDirectory() as tmp:
            r = runner(cmd, input=text[-MAX_IN:].encode("utf-8"), capture_output=True, timeout=timeout, cwd=tmp)
    except (OSError, subprocess.SubprocessError):
        return None
    out = (getattr(r, "stdout", b"") or b"").decode("utf-8", "replace").strip()
    if getattr(r, "returncode", 1) != 0 or not out:
        return None
    if not as_json:
        return out
    try:
        return json.loads(out)
    except ValueError:
        return None


def diagnose(unit: str, tail: str, **kw):
    return ask(DIAG_SYSTEM, f"Юнит: {unit}\n\n{tail}", **kw)


def compact(text: str, **kw):
    return ask(COMPACT_SYSTEM, text, **kw)
