#!/usr/bin/env python3
"""Рутина на Haiku 5.5 (TK-087): диагноз упавшего юнита и конспект длинного текста (лог тикета, сессия роли).

`claude -p` из временного каталога вне проекта (без CLAUDE.md, хуков, инструментов). Сбой/таймаут/нет claude — None, вызывающий
идёт прежним путём. Числа и вердикты исследования Haiku не выдаёт: в промптах это запрещено явно.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import hide
import project as P

ROOT = Path(__file__).resolve().parents[2]  # корень плагина: agents/<имя>.md
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


def clean_env() -> dict:
    """Окружение помощника: без переменных роли и проекта (TK-110 Ж-1) — ни хуков, ни CLAUDE.md проекта."""
    return {k: v for k, v in os.environ.items() if not k.startswith("RPV_") and k != "CLAUDE_PROJECT_DIR"}


def run_claude(cmd: list, prompt: str, timeout: float, cwd=None, runner=hide.run):
    """Единственный запуск `claude -p` служебной модели (помощник, отчёт, Надзиратель): скрытое окно, окружение без RPV_*,
    вход — байты, вывод — байты. Не запустился или завис — исключение вызывающему."""
    return runner(cmd, input=prompt.encode("utf-8"), capture_output=True, timeout=timeout, cwd=cwd, env=clean_env())


def agent_meta(path: Path) -> tuple:
    """`agents/<имя>.md` → (плоский фронтматтер {ключ: значение}, текст промпта). Нет фронтматтера — ValueError."""
    text = path.read_text(encoding="utf-8")
    fm = P.frontmatter(text)
    if not fm:
        raise ValueError(f"{path.parent.name}/{path.name}: нет фронтматтера")
    return {k: v.strip('"') for k, v in fm[0].items()}, text[fm[1]:].strip()


def agent_def(path: Path, with_tools: bool = False) -> dict:
    """`agents/<имя>.md` → определение для `claude --agents`: description, prompt, model/effort (если заданы) и, с with_tools,
    tools списком (пустой `tools:` — без инструментов). Нет фронтматтера — ValueError."""
    meta, prompt = agent_meta(path)
    d = {"description": meta.get("description", path.stem), "prompt": prompt}
    if with_tools:
        d["tools"] = [t.strip() for t in meta.get("tools", "").split(",") if t.strip()]
    for k in ("model", "effort"):
        if meta.get(k):
            d[k] = meta[k]
    return d


def load_agent(name: str) -> dict:
    """`agents/<name>.md` плагина → определение с tools (`agent_def`); нет фронтматтера — SystemExit с текстом ошибки."""
    try:
        return agent_def(ROOT / "agents" / f"{name}.md", with_tools=True)
    except ValueError as e:
        raise SystemExit(str(e))


def agent_cmd(claude: str, agents_file, agent: str, model: str, effort: str, budget, extra: tuple = (),
              tools: str | None = "") -> list:
    """Командная строка `claude -p --agent <agent>` без сессии; по умолчанию без инструментов (Надзиратель, отчёт), tools=None —
    флага `--tools` нет (инструменты задаёт определение агента, как у помощников в tools/helper_eval); extra — флаги вызывающего."""
    cmd = [claude, "-p", "--agents", str(agents_file), "--agent", agent, "--model", model, "--effort", effort,
           "--output-format", "json", "--no-session-persistence", "--max-budget-usd", str(budget)]
    if tools is not None:
        cmd += ["--tools", tools]
    return cmd + list(extra)


def ask(system: str, text: str, runner=hide.run, claude: str | None = None, timeout: int = TIMEOUT_S):
    """Ответ модели (str) или None. Вход режется до хвоста MAX_IN знаков."""
    claude = claude if claude is not None else _claude()
    if not claude or not text.strip():
        return None
    cmd = [claude, "-p", "--model", MODEL, "--effort", EFFORT, "--system-prompt", system, "--tools", "",
           "--no-session-persistence"]
    try:
        with tempfile.TemporaryDirectory() as tmp:
            r = run_claude(cmd, text[-MAX_IN:], timeout, cwd=tmp, runner=runner)
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
