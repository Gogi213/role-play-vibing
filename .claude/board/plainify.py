#!/usr/bin/env python3
"""Короткие человеческие строки процессов для табло: `claude -p` (Haiku) в фоне, кэш по хэшу входа.

Включено по умолчанию в `board_push.py` (RPV_PLAIN=0 — выключить). Вход — название процесса и шаги с состояниями (без деталей и номеров), поэтому модель
зовётся только когда меняется состояние шагов. Ответ кэшируется в `<проект>/.claude/pulse/plain-auto.json`; пока ответа нет —
остаётся строка по фактам плана (`summary` из view2 или «N из M шагов готово»). Нет `claude` в PATH (и CLAUDE_BIN), ошибка,
таймаут, негодный ответ — то же самое, без повторов чаще раза в минуту и не больше трёх попыток на вход.
`claude` запускается из временного каталога вне проекта: CLAUDE.md проекта не подтягивается, инструментов и хуков нет.
Только стандартная библиотека.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

MODEL = os.environ.get("RPV_PLAIN_MODEL") or "claude-haiku-5-5"
CALL_TIMEOUT_S = 60
MAX_PER_HOUR = 60
RETRY_S = (60, 300, 1800)  # пауза после 1-го, 2-го, 3-го сбоя; после третьего вход не повторяется
SYSTEM = ("Ты пишешь для табло одну короткую строку по-русски (до 90 знаков) про один процесс команды: что стало готово или что "
          "сейчас делается. Простыми словами, без номеров, имён файлов и жаргона, без кавычек и точки в конце. "
          "Ответ — только эта строка.")


def find_claude() -> str | None:
    c = os.environ.get("CLAUDE_BIN") or shutil.which("claude")
    return c if c and (os.path.isfile(c) or shutil.which(c)) else None


def fallback_line(proc: dict) -> str | None:
    """Строка по фактам плана: «N из M шагов готово»; шагов нет — None."""
    steps = proc.get("steps") or []
    return f"{sum(1 for s in steps if s.get('state') == 'done')} из {len(steps)} шагов готово" if steps else None


def payload(proc: dict) -> dict:
    return {"title": proc.get("title") or "", "steps": [{"title": s.get("title") or "", "state": s.get("state") or "todo"}
                                                        for s in proc.get("steps") or []]}


def key_of(p: dict) -> str:
    return hashlib.sha1(json.dumps(p, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def norm(text: str) -> str | None:
    """Первая строка ответа без кавычек и точки на конце; пусто или длиннее 140 знаков — негодный ответ."""
    lines = [x.strip() for x in str(text or "").strip().splitlines() if x.strip()]
    line = lines[0].strip(" \"'«»`").rstrip(".") if lines else ""
    return line if 0 < len(line) <= 140 else None


class Plain:
    def __init__(self, cache_path, runner=subprocess.run, claude: str | None = None):
        self.cache_path, self.runner = Path(cache_path), runner
        self.claude = claude if claude is not None else find_claude()
        self.lock = threading.Lock()
        self.queue: list = []                       # (хэш, вход) в ожидании
        self.pending: set = set()                   # хэши в очереди или в работе
        self.fails: dict = {}                       # хэш → (число сбоев, не раньше какого времени)
        self.calls: list = []                       # времена вызовов за последний час
        self.cache: dict = {}
        self.thread: threading.Thread | None = None
        try:
            doc = json.loads(self.cache_path.read_text(encoding="utf-8"))
            self.cache = {k: v for k, v in (doc.get("items") or {}).items() if isinstance(v, str)}
        except (OSError, ValueError, AttributeError):
            pass

    def get(self, p: dict) -> str | None:
        """Строка из кэша или None (тогда вход поставлен в очередь — ответ придёт на одном из следующих кадров)."""
        h = key_of(p)
        with self.lock:
            if h in self.cache:
                return self.cache[h]
            f = self.fails.get(h)
            if not self.claude or h in self.pending or (f and (f[0] >= len(RETRY_S) or time.time() < f[1])):
                return None
            self.pending.add(h)
            self.queue.append((h, p))
            if self.thread is None or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._work, daemon=True, name="plainify")
                self.thread.start()
        return None

    def flush(self, timeout: float = 90.0) -> None:
        """Дождаться пустой очереди (разовый запуск; в цикле не нужно)."""
        end = time.time() + timeout
        while time.time() < end and self.pending:
            time.sleep(0.1)

    def _work(self) -> None:
        while True:
            with self.lock:
                if not self.queue:
                    return
                h, p = self.queue.pop(0)
            try:
                out = self._call(p)
            except Exception:  # поток не умирает, вход не застревает в очереди
                out = None
            with self.lock:
                if out:
                    self.cache[h] = out
                    self.fails.pop(h, None)
                    self._save()
                else:
                    n = self.fails.get(h, (0, 0))[0] + 1
                    self.fails[h] = (n, time.time() + RETRY_S[min(n, len(RETRY_S)) - 1])
                self.pending.discard(h)

    def _call(self, p: dict) -> str | None:
        now = time.time()
        self.calls = [t for t in self.calls if now - t < 3600]
        if len(self.calls) >= MAX_PER_HOUR:
            return None
        self.calls.append(now)
        cmd = [self.claude, "-p", "--model", MODEL, "--effort", "low", "--output-format", "json", "--tools", "", "--system-prompt", SYSTEM,
               "--setting-sources", "", "--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence"]
        env = {k: v for k, v in os.environ.items() if "HOST_SESSION" not in k.upper()}
        env["MAX_THINKING_TOKENS"] = "0"
        cwd = Path(tempfile.gettempdir()) / "rpv-plainify"  # вне проекта: его CLAUDE.md не подтягивается
        try:
            cwd.mkdir(parents=True, exist_ok=True)
            r = self.runner(cmd, input=json.dumps(p, ensure_ascii=False), capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=CALL_TIMEOUT_S, cwd=str(cwd), env=env)
            doc = json.loads(r.stdout)
        except (OSError, ValueError, subprocess.SubprocessError):
            return None
        if r.returncode != 0 or not isinstance(doc, dict) or doc.get("is_error"):
            return None
        return norm(doc.get("result"))

    def _save(self) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"items": self.cache}, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self.cache_path)
        except OSError:
            pass


_INSTANCES: dict = {}


def apply(view2: dict, cache_path, wait: bool = False, plain: Plain | None = None) -> dict:
    """Улучшить `summary` процессов view2: строка модели + « — N из M готово». Улучшаются только процессы, у которых сборщик
    уже дал `summary` (план есть, хоть один шаг готов); нет строки модели — `summary` не трогается (факты плана).
    `wait` — дождаться ответов модели (разовый запуск)."""
    plain = plain or _INSTANCES.setdefault(str(cache_path), Plain(cache_path))
    procs = [p for p in view2.get("processes") or [] if p.get("summary")]
    for p in procs:
        plain.get(payload(p))
    if wait:
        plain.flush()
    for p in procs:
        line = plain.cache.get(key_of(payload(p)))
        if line:
            steps = p.get("steps") or []
            p["summary"] = f"{line} — {sum(1 for s in steps if s.get('state') == 'done')} из {len(steps)} готово"
    return view2
