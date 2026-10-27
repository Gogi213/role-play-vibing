#!/usr/bin/env python3
"""Рутина на Haiku 5.5 (TK-087): диагноз упавшего юнита и конспект длинного текста (лог тикета, сессия роли).

`claude -p` из временного каталога вне проекта (без CLAUDE.md, хуков, инструментов). Сбой/таймаут/нет claude — None, вызывающий
идёт прежним путём. Числа и вердикты исследования Haiku не выдаёт: в промптах это запрещено явно.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import hide

MODEL = "claude-haiku-5-5"
EFFORT = "xhigh"  # решение владельца 08.10: Haiku 5.5 везде на xhigh
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


def agent_meta(path: Path) -> tuple:
    """`agents/<имя>.md` → (плоский фронтматтер {ключ: значение}, текст промпта). Нет фронтматтера — ValueError."""
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", path.read_text(encoding="utf-8"), re.S)
    if not m:
        raise ValueError(f"{path.parent.name}/{path.name}: нет фронтматтера")
    meta = {k.strip(): v.strip().strip('"') for k, _, v in (ln.partition(":") for ln in m.group(1).splitlines())}
    return meta, m.group(2).strip()


def ask(system: str, text: str, runner=hide.run, claude: str | None = None, timeout: int = TIMEOUT_S):
    """Ответ модели (str) или None. Вход режется до хвоста MAX_IN знаков."""
    claude = claude if claude is not None else _claude()
    if not claude or not text.strip():
        return None
    cmd = [claude, "-p", "--model", MODEL, "--effort", EFFORT, "--system-prompt", system, "--tools", "",
           "--no-session-persistence"]
    env = {k: v for k, v in os.environ.items() if not k.startswith("RPV_") and k != "CLAUDE_PROJECT_DIR"}  # TK-110 Ж-1
    try:
        with tempfile.TemporaryDirectory() as tmp:
            r = runner(cmd, input=text[-MAX_IN:].encode("utf-8"), capture_output=True, timeout=timeout, cwd=tmp, env=env)
    except (OSError, subprocess.SubprocessError):
        return None
    out = (getattr(r, "stdout", b"") or b"").decode("utf-8", "replace").strip()
    if getattr(r, "returncode", 1) != 0 or not out:
        return None
    return out


def diagnose(unit: str, tail: str, **kw):
    return ask(DIAG_SYSTEM, f"Юнит: {unit}\n\n{tail}", **kw)


def compact(text: str, **kw):
    return ask(COMPACT_SYSTEM, text, **kw)
