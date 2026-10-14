#!/usr/bin/env python3
"""Заглушка `claude -p` для замера самого плагина без моделей: диспетчер запускает её вместо настоящего CLI
(`CLAUDE_BIN`), она делает ход роли по сценарию и печатает JSON итога в том же виде, что и `claude --output-format json`.
Цена и токены в JSON — условные, но проходят тот же учёт диспетчера (`resolved_cost`, `modelUsage`), что и настоящие.

Сценарий — файл `BENCH_STUB_CFG` (JSON): {"default": {...}, "TK-003": {...}}; поля хода:
  delay_s        сколько «думает» (сон до действия); [от, до] — случайно в этих пределах
  engineer       ok | nowrite (вышла без итога) | crash (код 3 без JSON) | hang (спит, пока не убьют)
  judge          accept | return1 (вернуть один раз, потом принять) | ask-owner | blocked
Запуск роли берётся из RPV_ROLE / RPV_TICKET / RPV_PROJECT, как у настоящего."""
from __future__ import annotations

import json
import os
import random
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

DISP = Path(__file__).resolve().parents[2] / ".claude" / "dispatcher"
TICKETS = DISP / "tickets.py"
FAKE = {"in": 1200, "out": 340, "cr": 18000, "cw": 2500}  # условные токены одного хода


def arg(name: str, default: str = "") -> str:
    a = sys.argv
    return a[a.index(name) + 1] if name in a and a.index(name) + 1 < len(a) else default


def scenario(tid: str) -> dict:
    cfg = {}
    p = os.environ.get("BENCH_STUB_CFG")
    if p and Path(p).exists():
        data = json.loads(Path(p).read_text(encoding="utf-8"))
        cfg = {**data.get("default", {}), **data.get(tid, {})}
    return cfg


def tickets(*args) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(TICKETS), *args], capture_output=True, text=True, encoding="utf-8",
                          env=os.environ.copy())


def event(proj: Path, **kw) -> None:
    """Строка в `stub-events.jsonl`: когда запуск роли реально начался и кончился (время на стороне заглушки)."""
    f = proj / ".claude" / "dispatcher" / "stub-events.jsonl"
    f.parent.mkdir(parents=True, exist_ok=True)
    with open(f, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(kw, ensure_ascii=False) + "\n")


def count_prior(proj: Path, tid: str, role: str) -> int:
    """Сколько раз ЭТА роль уже сдавала итог в тикете (для «вернуть один раз»): запись `### <время> <роль>` и под ней `[итог:`."""
    f = proj / ".claude" / "tickets" / f"{tid}.md"
    if not f.exists():
        return 0
    return len(re.findall(rf"^### \S+ {role}\n\[итог:", f.read_text(encoding="utf-8"), re.M))


def main() -> int:
    role, tid = os.environ.get("RPV_ROLE", ""), os.environ.get("RPV_TICKET", "")
    proj = Path(os.environ.get("RPV_PROJECT") or os.getcwd())
    model = arg("--model", "claude-sonnet-5-5")
    sid = arg("--resume") or arg("--session-id") or str(uuid.uuid4())
    cfg = scenario(tid)
    t0 = time.time()
    event(proj, tid=tid, role=role, at="start", t=t0, pid=os.getpid(), model=model, effort=arg("--effort"))
    d = cfg.get("delay_s", 0)
    if isinstance(d, list):  # [от, до]: случайное, но одно и то же для (тикет, роль, номер запуска) — прогон воспроизводим
        d = random.Random(f"{tid}{role}{count_prior(proj, tid, role)}").uniform(d[0], d[1])
    time.sleep(float(d))
    mode = cfg.get(role, {"engineer": "ok", "judge": "accept"}.get(role, "ok"))
    if mode == "crash":
        print("stub: crash", file=sys.stderr)
        event(proj, tid=tid, role=role, at="end", t=time.time(), mode=mode)
        return 3
    if mode == "hang":
        time.sleep(3600)
    if mode != "nowrite":
        if role in ("engineer", "researcher"):
            out = proj / "out" / f"{tid}.txt"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text("готово\n", encoding="utf-8")
            subprocess.run(["git", "add", str(out)], cwd=proj, capture_output=True)
            subprocess.run(["git", "-c", "user.name=stub", "-c", "user.email=stub@example.invalid", "commit", "-qm",
                            f"{tid} stub", "--allow-empty"], cwd=proj, capture_output=True)
            r = tickets("result", tid, "done", "--path", f"out/{tid}.txt", "--why", "заглушка: файл записан")
        elif role == "judge":
            art = f"out/{tid}.txt"
            if mode == "return1" and count_prior(proj, tid, "judge") == 0:
                r = tickets("result", tid, "return", "--path", art, "--why", "заглушка: вернуть один раз")
            elif mode == "ask-owner":
                r = tickets("result", tid, "ask-owner", "--why-owner", "деньги", "--why", "заглушка: нужен бюджет")
            elif mode == "blocked":
                r = tickets("result", tid, "blocked", "--why", "заглушка: встали")
            else:
                r = tickets("result", tid, "accept", "--path", art, "--decisions", "-", "--numbers", "-", "--why",
                            "заглушка: принято")
        else:
            r = None
        if r is not None and r.returncode:
            print(f"stub: result отказ: {r.stderr.strip()}", file=sys.stderr)
    cost_file = proj / ".claude" / "dispatcher" / "stub-cost" / f"{sid}.txt"
    cost_file.parent.mkdir(parents=True, exist_ok=True)
    total = (float(cost_file.read_text()) if cost_file.exists() else 0.0) + 0.0123  # --resume отдаёт итог кумулятивно
    cost_file.write_text(f"{total:.4f}")
    dur = int((time.time() - t0) * 1000)
    event(proj, tid=tid, role=role, at="end", t=time.time(), mode=mode)
    usage = {"input_tokens": FAKE["in"], "output_tokens": FAKE["out"], "cache_read_input_tokens": FAKE["cr"],
             "cache_creation_input_tokens": FAKE["cw"]}
    print(json.dumps({
        "type": "result", "subtype": "success", "is_error": False, "duration_ms": dur, "duration_api_ms": dur,
        "num_turns": 3, "result": "stub", "session_id": sid, "total_cost_usd": round(total, 4), "usage": usage,
        "modelUsage": {model: {"inputTokens": FAKE["in"], "outputTokens": FAKE["out"],
                               "cacheReadInputTokens": FAKE["cr"], "cacheCreationInputTokens": FAKE["cw"],
                               "costUSD": round(total, 4)}},
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
