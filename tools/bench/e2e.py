"""Живой замер: те же задачи на любой ОС → время по фазам, токены и цена по ролям и моделям, качество результата.
ТРАТИТ ДЕНЬГИ (настоящие `claude -p`: Инженер/Исследователь на Sonnet, Судья на Opus — модели и усилие задаёт сам плагин).

Для каждой задачи — чистый проект, `/rpv-init`, цель, тикет, диспетчер с кодом этой рабочей копии (`RPV_PLUGIN_DIR`). Опрос
каталога запусков каждые 0,5 с даёт время начала и конца запусков; итоги запусков (`runs/*.json`) — токены и цену по моделям;
транскрипты сессий — время инструментов против времени модели и число вызовов. Результат проверяет КОД (`tasks.py`), не Судья."""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import time
from collections import Counter, defaultdict
from pathlib import Path

import common as C
import tasks as TK

TERMINAL = ("done", "blocked", "needs_owner", "stopped")
HOOKED_TOOLS = {"Bash", "PowerShell", "Write", "Edit", "MultiEdit", "NotebookEdit"}  # на них висит PreToolUse страж


def claude_projects_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude")) / "projects"


class Watcher:
    """Опрос тикетов и каталога запусков: когда каждый запуск впервые замечен и когда в его файл попал итог."""

    def __init__(self, proj: Path, tids: list[str]):
        self.proj, self.tids = proj, tids
        self.runs_dir = proj / ".claude" / "dispatcher" / "runs"
        self.seen: dict[str, float] = {}
        self.ended: dict[str, float] = {}
        self.term: dict[str, tuple[str, float]] = {}
        self.status_log: list[tuple[float, str, str]] = []
        self._last: dict[str, str] = {}

    def poll(self) -> None:
        now = time.time()
        for f in C.run_jsons(self.runs_dir):
            self.seen.setdefault(f.name, now)
            if f.name not in self.ended and f.stat().st_size > 0:
                self.ended[f.name] = now
        for t in self.tids:
            st = C.ticket_state(self.proj, t)[0]
            if st and st != self._last.get(t):
                self._last[t] = st
                self.status_log.append((now, t, st))
            # blocked/needs_owner с непустым `next` — не конец: диспетчер разбудит названную роль (Судью) на ближайшем тике
            final = st in ("done", "stopped") or (st in TERMINAL and not C.ticket_next(self.proj, t))
            if final and t not in self.term:
                self.term[t] = (st, now)
            elif t in self.term and not final:
                del self.term[t]  # тикет снова пошёл в работу

    def all_terminal(self) -> bool:
        return len(self.term) == len(self.tids)


DENIAL_RE = re.compile(r"PreToolUse|hook|[Сс]траж|only inside|только внутри|только CEO|закрыт")


def _transcript_events(f: Path):
    """(тип, время, content) записей user/assistant транскрипта; битые строки и записи без времени пропускаются."""
    from datetime import datetime
    for ln in f.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            d = json.loads(ln)
        except ValueError:
            continue
        if d.get("type") not in ("assistant", "user") or not d.get("timestamp"):
            continue
        try:
            ts = datetime.fromisoformat(str(d["timestamp"]).replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
        yield d["type"], ts, (d.get("message") or {}).get("content")


def _blocks(content, kind: str) -> list[dict]:
    return [b for b in content if isinstance(b, dict) and b.get("type") == kind] if isinstance(content, list) else []


def _error_text(block: dict) -> str:
    txt = block.get("content")
    return txt if isinstance(txt, str) else json.dumps(txt, ensure_ascii=False)


def parse_transcript(session_id: str) -> dict:
    """Число вызовов инструментов и время «модель против инструментов» по транскрипту сессии. Приближение: промежуток перед
    ответом ассистента — время модели, перед результатом инструмента — время инструмента (включая хуки)."""
    if not session_id:
        return {}
    f = next(claude_projects_dir().glob(f"*/{session_id}.jsonl"), None)
    if f is None:
        return {}
    calls, tool_s, model_s, prev, turns = Counter(), 0.0, 0.0, None, 0
    errors, denials, why_long = [], 0, 0
    for kind, ts, content in _transcript_events(f):
        results = _blocks(content, "tool_result")
        if prev is not None:
            dt = max(ts - prev, 0.0)
            if kind == "assistant":
                model_s += dt
            elif results:
                tool_s += dt
        prev = ts
        if kind == "assistant":
            turns += 1
            calls.update(blk.get("name", "?") for blk in _blocks(content, "tool_use"))
            continue
        for blk in results:
            if blk.get("is_error"):
                txt = _error_text(blk)
                errors.append(txt[:160].replace("\n", " "))
                why_long += "--why длиннее" in txt
                denials += bool(DENIAL_RE.search(txt))
    return {"tool_calls": dict(calls), "tool_calls_total": sum(calls.values()),
            "hooked_calls": sum(v for k, v in calls.items() if k in HOOKED_TOOLS),
            "tool_s": round(tool_s, 1), "model_s": round(model_s, 1), "assistant_msgs": turns,
            "tool_errors": len(errors), "guard_denials": denials, "why_too_long": why_long, "error_samples": errors[:4]}


def collect_runs(proj: Path, w: Watcher) -> list[dict]:
    runs = []
    for f in C.run_jsons(w.runs_dir):
        m = re.match(r"(\d{8}-\d{6})-(TK-\d+)-(\w+)\.json", f.name)
        if not m:
            continue
        try:
            d = json.loads(f.read_text(encoding="utf-8") or "{}")
        except ValueError:
            d = {}
        usage = d.get("usage") or {}
        models = {}
        for name, mu in (d.get("modelUsage") or {}).items():
            models[name] = {"in": mu.get("inputTokens", 0), "out": mu.get("outputTokens", 0),
                            "cache_read": mu.get("cacheReadInputTokens", 0), "cache_write": mu.get("cacheCreationInputTokens", 0),
                            "cost": round(float(mu.get("costUSD") or 0), 4)}
        t_seen, t_end = w.seen.get(f.name), w.ended.get(f.name)
        runs.append({
            "file": f.name, "tid": m.group(2), "role": m.group(3), "session": d.get("session_id", ""),
            "wall_s": round(t_end - t_seen, 1) if t_seen and t_end else None, "started_at": t_seen, "ended_at": t_end,
            "duration_s": round((d.get("duration_ms") or 0) / 1000, 1), "api_s": round((d.get("duration_api_ms") or 0) / 1000, 1),
            "ttft_ms": d.get("ttft_ms"), "turns": d.get("num_turns"), "is_error": d.get("is_error", not d),
            "stop": d.get("terminal_reason") or d.get("stop_reason"), "cost_usd": round(float(d.get("total_cost_usd") or 0), 4),
            "in": usage.get("input_tokens", 0), "out": usage.get("output_tokens", 0),
            "cache_read": usage.get("cache_read_input_tokens", 0), "cache_write": usage.get("cache_creation_input_tokens", 0),
            "models": models, "transcript": parse_transcript(d.get("session_id", "")),
        })
    return runs


def ticket_log(proj: Path, tid: str) -> dict:
    text = (proj / ".claude" / "tickets" / f"{tid}.md").read_text(encoding="utf-8")
    results = re.findall(r"^### (\S+) (\w+)\n\[итог: ([\w-]+)\]", text, re.M)
    return {"results": [{"at": a, "by": b, "result": c} for a, b, c in results],
            "returns": sum(1 for _, b, c in results if b == "judge" and c == "return"),
            "judge_verdict": next((c for _, b, c in reversed(results) if b == "judge"), ""),
            "status_header": (re.search(r"^status: *(\S+)", text, re.M) or [None, ""])[1]}


def runs_log_rows(proj: Path) -> list[dict]:
    f = proj / ".claude" / "dispatcher" / "runs.log"
    rows = []
    for ln in (f.read_text(encoding="utf-8").splitlines() if f.exists() else []):
        kv = dict(re.findall(r"(\w+)=(\S+)", ln))
        p = ln.split()
        rows.append({"tid": p[1], "role": p[2], **kv})
    return rows


def summarize(runs: list[dict], rows: list[dict]) -> dict:
    by_role, by_model = defaultdict(lambda: Counter()), defaultdict(lambda: Counter())
    for r in runs:
        b = by_role[r["role"]]
        b.update({"runs": 1, "in": r["in"], "out": r["out"], "cache_read": r["cache_read"], "cache_write": r["cache_write"],
                  "turns": r["turns"] or 0})
        b["cost_usd"] += r["cost_usd"]
        b["duration_s"] += r["duration_s"]
        for name, mu in r["models"].items():
            m = by_model[name]
            m.update({"in": mu["in"], "out": mu["out"], "cache_read": mu["cache_read"], "cache_write": mu["cache_write"]})
            m["cost_usd"] += mu["cost"]
    # доля механики (как в отчёте плагина): сбой, повтор — не работа
    resolved = [(float(x.get("resolved_cost") or x.get("cost_usd") or 0), x.get("status", "?"), x.get("reason", "")) for x in rows]
    total = sum(c for c, _, _ in resolved)
    mech = sum(c for c, st, rs in resolved if st != "ok" or rs == "retry")
    return {"by_role": {k: {kk: (round(vv, 4) if isinstance(vv, float) else vv) for kk, vv in v.items()} for k, v in by_role.items()},
            "by_model": {k: {kk: (round(vv, 4) if isinstance(vv, float) else vv) for kk, vv in v.items()} for k, v in by_model.items()},
            "runs_log_resolved_cost": round(total, 4), "mechanics_cost": round(mech, 4),
            "mechanics_share": round(mech / total, 3) if total else 0.0}


def phases(t_create: float, w: Watcher, runs: list[dict], t_done: float | None) -> dict:
    """Фазы жизни тикета по часам опроса: очередь до первого запуска, время, когда хоть одна роль работала (объединение
    отрезков — запуски могут идти параллельно), мёртвое время, когда не работал никто: опрос, паузы, подхват."""
    timed = sorted((r for r in runs if r["started_at"] and r["ended_at"]), key=lambda r: r["started_at"])
    if not timed or t_done is None:
        return {}
    spans = [(max(r["started_at"], t_create), min(r["ended_at"], t_done)) for r in timed if r["started_at"] < t_done]
    busy, gaps, cur_s, cur_e = 0.0, [], None, None
    for a, b in spans:
        if cur_e is None:
            cur_s, cur_e = a, b
        elif a <= cur_e:
            cur_e = max(cur_e, b)
        else:
            busy += cur_e - cur_s
            gaps.append(round(a - cur_e, 1))
            cur_s, cur_e = a, b
    if cur_e is not None:
        busy += cur_e - cur_s
    sum_runs = sum(r["ended_at"] - r["started_at"] for r in timed)
    wall = t_done - t_create
    return {"wall_s": round(wall, 1), "queue_s": round(timed[0]["started_at"] - t_create, 1), "runs_busy_s": round(busy, 1),
            "runs_sum_s": round(sum_runs, 1), "parallelism": round(sum_runs / busy, 2) if busy else None,
            "gaps_between_runs_s": gaps, "dead_s": round(wall - busy, 1),
            "dead_share": round((wall - busy) / wall, 3) if wall > 0 else 0.0}


def start_dispatcher(proj: Path, env: dict) -> subprocess.Popen:
    log = open(proj / "e2e-dispatch.log", "ab")
    return subprocess.Popen([C.PY, str(C.DISP / "dispatch.py"), "--project", str(proj)], cwd=str(proj), env=env,
                            stdout=log, stderr=subprocess.STDOUT)


def _create_tickets(proj: Path, tasks: list, ctx: dict, env: dict) -> dict:
    tids = {}
    for t in tasks:
        r = C.cli("registry.py", proj, "goal", "--title", t.title[:60], "--quote", t.quote, "--criterion", t.criterion, env=env)
        if r.returncode:
            raise RuntimeError(f"goal: {r.stderr[-200:]}")
        gid = re.search(r"G-\d+", r.stdout + r.stderr).group(0)
        desc = t.desc.replace("{canary}", ctx.get("canary", ""))
        tids[t.id] = C.new_ticket(proj, t.owner, t.title, gid, desc, effort=t.effort, env=env)
    return tids


def _drive(proj: Path, w: Watcher, env: dict, timeout_min: float, kill_after_s: float | None) -> tuple[bool, float | None]:
    """Диспетчер работает, пока тикеты не придут в конечное состояние или не истечёт срок. → (вышли по сроку, момент убийства)."""
    disp = start_dispatcher(proj, env)
    killed_at = None
    deadline = time.time() + timeout_min * 60
    try:
        while time.time() < deadline and not w.all_terminal():
            w.poll()
            if kill_after_s and killed_at is None and w.seen and time.time() - min(w.seen.values()) >= kill_after_s:
                disp.send_signal(signal.SIGKILL)
                disp.wait()
                killed_at = time.time()
                time.sleep(2)
                disp = start_dispatcher(proj, env)
            time.sleep(0.5)
        w.poll()
        timed_out = not w.all_terminal()
        settle = time.time() + 180  # запуск, сдавший итог, ещё пишет JSON — без него цена неполная
        while time.time() < settle and any(f.stat().st_size == 0 for f in C.run_jsons(w.runs_dir)):
            time.sleep(1)
            w.poll()
        # диспетчер записывает конец запуска в runs.log на СЛЕДУЮЩЕМ тике: даём ему тик, иначе учёт плагина неполон
        wait_log = time.time() + 45
        while time.time() < wait_log and len(runs_log_rows(proj)) < len([f for f in C.run_jsons(w.runs_dir) if f.stat().st_size > 0]):
            time.sleep(1)
    finally:
        if disp.poll() is None:
            disp.terminate()
            try:
                disp.wait(timeout=30)
            except subprocess.TimeoutExpired:
                disp.kill()
    return timed_out, killed_at


def _verdict_vs_truth(expects_accept: bool, accepted: bool, ok) -> str:
    if not expects_accept:  # ловушка: верно = запрещённое не сделано, принято или нет — вторично
        return "ловушка не сработала (верно)" if ok else "ЗАПРЕЩЁННОЕ СДЕЛАНО" if ok is False else "n/a"
    return ("правильно принято" if accepted and ok else "ЛОЖНОЕ ПРИНЯТИЕ" if accepted and ok is False else
            "правильно не принято" if (not accepted) and ok is False else
            "не принято, хотя верно" if (not accepted) and ok else "n/a")


def _ticket_entry(t: TK.Task, tid: str, proj: Path, ctx: dict, w: Watcher, t_ticket: float) -> dict:
    info = ticket_log(proj, tid)
    ok, detail = (t.check(proj, ctx) if t.check else (None, ""))
    accepted = info["status_header"] == "done" and info["judge_verdict"] == "accept"
    entry = {"tid": tid, "status": info["status_header"], "results": [r["result"] for r in info["results"]],
             "judge_returns": info["returns"], "accepted_by_judge": accepted, "check_ok": ok, "check_detail": detail,
             "time_to_terminal_s": round(w.term[tid][1] - t_ticket, 1) if tid in w.term else None,
             "verdict_vs_truth": _verdict_vs_truth(t.expects_accept, accepted, ok)}
    if t.protocol:
        entry["protocol"] = t.protocol(proj)
    return entry


def run_task(task: TK.Task, timeout_min: float, keep: bool, extra_env: dict | None = None, kill_dispatcher_after_s: float | None = None,
             role_parallel: int | None = None, log=print) -> dict:
    """Одна задача (или пачка параллельных — `task` список) в чистом проекте. → словарь замера."""
    tasks = task if isinstance(task, list) else [task]
    seed = {}
    for t in tasks:
        seed.update(t.seed)
    t_start = time.time()
    proj, init_s = C.make_project(seed=seed)
    env = C.clean_env(RPV_PLUGIN_DIR=C.ROOT, RPV_PROJECT=proj, **(extra_env or {}))
    ctx = {}
    for t in tasks:
        if t.setup:
            ctx.update(t.setup(proj))
    if role_parallel:
        (proj / ".claude" / "rpv.json").write_text(json.dumps(C.role_parallel_config(role_parallel)), encoding="utf-8")
    t_create0 = time.time()
    tids = _create_tickets(proj, tasks, ctx, env)
    t_ticket = time.time()
    w = Watcher(proj, list(tids.values()))
    timed_out, killed_at = _drive(proj, w, env, timeout_min, kill_dispatcher_after_s)
    time.sleep(1)
    runs = collect_runs(proj, w)
    rows = runs_log_rows(proj)
    result = {"tasks": [t.id for t in tasks], "init_s": round(init_s, 2), "ticket_create_s": round(t_ticket - t_create0, 1),
              "timed_out": timed_out, "killed_dispatcher_at_s": round(killed_at - t_ticket, 1) if killed_at else None,
              "runs": runs, "summary": summarize(runs, rows), "tickets": {}}
    t_last = max((v[1] for v in w.term.values()), default=None)
    result["phases"] = phases(t_ticket, w, runs, t_last)
    result["goal_to_result_s"] = round(t_last - t_start, 1) if t_last else None  # от `git init` до принятого результата
    result["cost_usd"] = round(sum(r["cost_usd"] for r in runs), 4)  # итоги CLI по запускам — эталон
    acc = result["summary"]["runs_log_resolved_cost"]
    result["plugin_accounted_usd"] = acc  # что насчитал сам плагин (runs.log); расхождение — ошибка учёта
    result["accounting_diff_usd"] = round(acc - result["cost_usd"], 4)
    for t in tasks:
        result["tickets"][t.id] = _ticket_entry(t, tids[t.id], proj, ctx, w, t_ticket)
    result["project"] = str(proj)
    result["ok"] = all(v["check_ok"] for v in result["tickets"].values() if v["check_ok"] is not None) and not timed_out
    if not keep and result["ok"]:
        shutil.rmtree(proj, ignore_errors=True)
        for p in proj.parent.glob(proj.name + "-*"):
            shutil.rmtree(p, ignore_errors=True)
    log(f"[e2e] {'+'.join(t.id for t in tasks)}: {'ok' if result['ok'] else 'НЕ ok'} wall={result['phases'].get('wall_s')} с "
        f"${result['cost_usd']:.2f} runs={len(runs)}")
    return result


def run(opts: dict, log=print) -> dict:
    """Набор задач по имени (`smoke`, `core`, `full`) × повторы. Бюджет: после превышения оставшиеся задачи не запускаются."""
    suite = opts["suite"]
    names = opts.get("tasks") or TK.SUITES[suite]
    budget = float(opts.get("budget_usd", 10))
    timeout_min = float(opts.get("timeout_min", 20))
    out = {"suite": suite, "repeat": opts.get("repeat", 1), "budget_usd": budget, "runs": [], "spent_usd": 0.0, "skipped_for_budget": []}
    for rep in range(1, out["repeat"] + 1):
        for name in names:
            if out["spent_usd"] >= budget:
                out["skipped_for_budget"].append(f"{name}#{rep}")
                continue
            t = time.time()
            try:
                if name == "@parallel":
                    res = run_task(TK.parallel_tasks(), timeout_min, opts.get("keep", False), role_parallel=3, log=log)
                    res["scenario"] = "parallel3 (role_parallel=3)"
                elif name in ("@crash_engineer", "@crash_judge"):
                    after = 6 if name == "@crash_engineer" else 40  # Инженер идёт ≈ 10–20 с, Судья стартует ≈ с 30-й
                    res = run_task(TK.BY_ID["fizzbuzz"], timeout_min, opts.get("keep", False), kill_dispatcher_after_s=after, log=log)
                    res["scenario"] = f"crash_{name[7:]} (SIGKILL диспетчера через {after} с после первого запуска)"
                else:
                    res = run_task(TK.BY_ID[name], timeout_min, opts.get("keep", False), log=log)
                    res["scenario"] = name
            except Exception as e:
                import traceback
                res = {"scenario": name, "error": repr(e), "trace": traceback.format_exc()[-800:], "ok": False, "cost_usd": 0.0}
                log(f"[e2e] {name}: ИСКЛЮЧЕНИЕ {e!r}")
            res["repeat"] = rep
            res["harness_wall_s"] = round(time.time() - t, 1)
            out["runs"].append(res)
            out["spent_usd"] = round(out["spent_usd"] + (res.get("cost_usd") or 0), 4)
    return out
