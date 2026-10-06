#!/usr/bin/env python3
"""Короткие строки для табло: итог процесса одной фразой.

RPV_PLAIN=haiku (по умолчанию, если в PATH есть `claude`) — один вызов `claude -p --model haiku` на изменение
состояния шагов (кэш `.claude/pulse/plain-auto.json`, ключ — хэш названия и шагов); RPV_PLAIN=off или нет claude,
сбой, таймаут — строка по фактам плана («сделано: <шаг> — N из M»). До RPV_PLAIN_MAX вызовов (3) за сборку.
Вызовы пишутся в `.claude/pulse/plainify.log`.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

PROMPT = ("Одной короткой фразой по-русски (до 12 слов, без жаргона) скажи, что сделано и что делается сейчас, "
          "по задаче и шагам. Только фраза.\n")


def facts(p: dict) -> str | None:
    done = [s for s in p.get("steps") or [] if s.get("state") == "done"]
    if not done:
        return None
    return f"сделано: {done[-1]['title']} — {len(done)} из {len(p['steps'])}"


def _key(p: dict) -> str:
    raw = p.get("title", "") + "|" + "|".join(f"{s.get('title')}:{s.get('state')}" for s in p.get("steps") or [])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _haiku(text: str, timeout: float = 40.0) -> str | None:
    exe = shutil.which("claude")
    if not exe:
        return None
    try:
        r = subprocess.run([exe, "-p", "--model", "haiku", PROMPT + text], capture_output=True, text=True,
                           timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    out = (r.stdout or "").strip().splitlines()
    return out[-1].strip()[:140] if r.returncode == 0 and out else None


def apply(procs: list, pulse: Path, budget: int | None = None) -> None:
    """Проставляет p['summary']: из кэша/Haiku, иначе по фактам."""
    mode = os.environ.get("RPV_PLAIN", "haiku").lower()
    budget = int(os.environ.get("RPV_PLAIN_MAX", "3")) if budget is None else budget
    cache_f = Path(pulse) / "plain-auto.json"
    try:
        cache = json.loads(cache_f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cache = {}
    dirty = False
    for p in procs:
        base = p.get("summary")
        if mode == "off" or base is None:
            continue
        k = _key(p)
        if k in cache:
            p["summary"] = cache[k]
        elif budget > 0:
            budget -= 1
            t0 = time.time()
            txt = _haiku(p.get("title", "") + "\n" + "\n".join(f"- {s.get('title')} [{s.get('state')}]" for s in p.get("steps") or []))
            try:
                with open(Path(pulse) / "plainify.log", "a", encoding="utf-8") as f:
                    f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {p.get('id')} {'ok' if txt else 'fail'} {time.time() - t0:.1f}s\n")
            except OSError:
                pass
            if txt:
                cache[k] = p["summary"] = txt
                dirty = True
    if dirty:
        try:
            cache_f.parent.mkdir(parents=True, exist_ok=True)
            tmp = cache_f.with_suffix(".tmp")
            tmp.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
            tmp.replace(cache_f)
        except OSError:
            pass
