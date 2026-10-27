"""Итог запуска в runs.log: результат `claude -p`, токены и контекст — вынесено из dispatch.py (TK-011).
Каталог транскриптов (`CLAUDE_PROJECTS_DIR`) передаёт вызывающий — обёртки в dispatch.py подставляют текущий."""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import project_paths as CFG  # noqa: E402
import project as P  # noqa: E402
import ticket as T  # noqa: E402
import transcript  # noqa: E402
from limits import _api_error as api_error  # noqa: E402


tokens_of = transcript.context_of


def context_tokens_sum(usage: dict) -> int:
    """Сумма input+cache_read+cache_creation по ВСЕМУ usage из JSON `claude -p` — это сумма по всем
    ходам запуска, не контекст одного хода (см. context_tokens_last). Для runs.log (ctx_sum=)."""
    return tokens_of(usage)


def transcript_last_turn_tokens(session_id, projects_dir) -> int:
    """Контекст ПОСЛЕДНЕГО хода сессии по её транскрипту (usage последнего ответа модели); 0 — транскрипта нет."""
    path = transcript.find(projects_dir, session_id)
    return transcript.last_turn_tokens(path) if path else 0


def context_tokens_last(result: dict, projects_dir) -> int:
    """Контекст, который реально понесёт следующий `--resume` — последний ход ЭТОГО запуска, не сумма
    по всем его ходам: usage в JSON claude -p суммирует по ходам, и на многоходовом запуске это
    завышает контекст в разы, рвя ротацию раньше времени (CEO 27.09, живой прогон: ctx_sum=333 886
    при реальном контексте хода ~52 тыс.). usage.iterations[-1] — контекст последнего хода; нет
    iterations (аудит 03.10) — последний ход из транскрипта сессии; нет и его — запасная оценка
    ctx_sum // num_turns (среднее по ходам, занижена)."""
    usage = result.get("usage") or {}
    iterations = usage.get("iterations")
    if isinstance(iterations, list) and iterations:
        return tokens_of(iterations[-1])
    last = transcript_last_turn_tokens(result.get("session_id"), projects_dir)
    if last:
        return last
    total = context_tokens_sum(usage)
    try:
        num_turns = int(result.get("num_turns") or 1) or 1
    except (TypeError, ValueError):
        num_turns = 1
    return total // num_turns


def read_run_result(run_file: Path) -> dict:
    try:
        data = Path(run_file).read_text(encoding="utf-8").strip()
        return json.loads(data) if data else {}
    except Exception:
        return {}


def transcript_usage_since(session_id, started, projects_dir) -> dict:
    """Токены запуска по транскрипту сессии (TK-055): сумма usage ответов модели с отметкой >= started (ISO или
    datetime), по одному на message.id (потоковые дубли — последний). Для запусков без JSON (таймаут/убит): {} —
    сессии/транскрипта нет."""
    try:
        path = transcript.find(projects_dir, session_id)
        if path is None:
            return {}
        t0 = started if isinstance(started, datetime) else P.parse_time(started, strict=True)
        if t0.tzinfo is None:
            t0 = t0.astimezone()
        per = {}
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"usage"' not in line:
                    continue
                try:
                    row = json.loads(line)
                    ts = P.parse_time(row["timestamp"], strict=True)
                except Exception:
                    continue
                msg = row.get("message") or {}
                u = msg.get("usage")
                if ts >= t0 and isinstance(u, dict):
                    per[msg.get("id") or row.get("uuid") or len(per)] = u
        tot = {"input_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "output_tokens": 0}
        for u in per.values():
            for k in tot:
                tot[k] += int(u.get(k) or 0)
        return tot if per else {}
    except Exception:
        return {}


def log_run_summary(projects_dir, tid: str, info: dict, result: dict, now, timed_out: bool, resolved_cost: float,
                    ticket_spent: float, cost_note: bool = False, stopped: bool = False) -> None:
    CFG.RUNS_LOG.parent.mkdir(parents=True, exist_ok=True)
    usage = result.get("usage") or {}
    src = "json"
    if not usage:  # таймаут/убит: токены — из транскрипта сессии с момента старта запуска
        usage = transcript_usage_since(info.get("session_id"), info.get("started"), projects_dir) or {}
        src = "jsonl" if usage else "none"
    status = ("stopped" if stopped else "timeout" if timed_out else "api_error" if api_error(result)
              else ("ok" if result else "no_output"))
    # cost_usd — сырой (кумулятивный за сессию) из JSON; resolved_cost — разница с прошлым итогом ТОЙ ЖЕ
    # session_id (судья TK-002 п.1) — то, что реально начислено этому запуску; cost_note=asis — не было
    # с чем сравнить (новая/ротированная сессия) или разница < 0 — использован сырой итог как есть.
    # ticket_spent — накоплено по ЭТОЙ задаче ПОСЛЕ этого запуска (колонка CEO, роль её не видит).
    line = (f"{T.now_iso(now)} {tid} {info['role']} reason={info.get('reason')} "
            f"attempt={info.get('attempt', 0)} session={result.get('session_id') or info.get('session_id') or '-'} "
            f"cost_usd={result.get('total_cost_usd', '-')} resolved_cost={resolved_cost:.4f} "
            f"cost_note={'asis' if cost_note else 'diff'} ticket_spent={ticket_spent:.4f} "
            f"in_tok={usage.get('input_tokens', '-')} out_tok={usage.get('output_tokens', '-')} "
            f"cr_tok={usage.get('cache_read_input_tokens', '-')} cw_tok={usage.get('cache_creation_input_tokens', '-')} "
            f"tok_src={src} "
            f"ctx_last={context_tokens_last(result, projects_dir)} ctx_sum={context_tokens_sum(usage)} "
            f"terminal_reason={result.get('terminal_reason') or '-'} is_error={result.get('is_error', '-')} "
            f"status={status}\n")
    with open(CFG.RUNS_LOG, "a", encoding="utf-8") as fh:
        fh.write(line)
