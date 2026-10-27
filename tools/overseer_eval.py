#!/usr/bin/env python3
"""Замер Надзирателя (CP14, `agents/overseer.md`) на истории проекта: ловит ли он эпизоды «деньги и время идут, к цели не
движемся» заранее и сколько ложных тревог. Только stdlib. Данные проекта читаются, в репозиторий не копируются.

    python tools/overseer_eval.py plan   --project <проект> [--tickets TK-087,TK-048 | --group bad|normal|all] [--step-min 15]
                                          [--skip-roles watch,ci,…] [--fit <итоги>] [--per-call 0.01]
    python tools/overseer_eval.py run    --project <проект> --out <итоги> [--tickets …] [--group …] [--step-min 15]
                                          [--jobs 4] [--budget 1.0] [--model claude-haiku-5-5] [--effort xhigh]
    python tools/overseer_eval.py report --out <итоги>
    python tools/overseer_eval.py regrade --project <проект> --out <итоги>   # правило проверки цитаты поправлено — без вызовов
    python tools/overseer_eval.py prompt --project <проект> --ticket TK-087 --window 5   # что увидит Надзиратель

Источники (проект, `--project`):
  - `.claude/tickets/TK-*.md` — шапка (`title`, `status`), описание (цель: слова владельца) и `## Лог`;
  - `.claude/tickets/archive/TK-*-log.md` — старые записи лога; запись — `### <ISO-время> <роль>` и текст;
  - `.claude/dispatcher/runs.log` — запуски: `<ISO> <тикет> <роль> reason=… cost_usd=… resolved_cost=… status=…`.
    Цена запуска — `resolved_cost` (начислено именно этому запуску); нет его — `cost_usd`. Сумма `cost_usd` по запускам
    одной сессии считает её итог много раз (сессия с `--resume` отдаёт итог с начала), поэтому она втрое-вчетверо больше.

Окна. Лог тикета режется на окна по `--step-min` минут от первой записи; окно с новыми записями — один вызов Надзирателя
в момент конца окна. Он видит: цель (описание), строку кода «к этому моменту» (запуски, $, часы от начала, возвраты Судьи —
считает код по runs.log и логу на конец окна, модель чисел не выдаёт), ход тикета (по строке на прежнюю запись), новые
записи окна (последние `--max-kb` КБ, обрезка с пометкой). Описание тикета берётся каким оно лежит сейчас: прежние его
редакции не восстановить.

Ответ — JSON. Находка принимается, только если `quote` есть дословно (пробелы нормализуются) в одной записи лога тикета
до конца окна, вид из четырёх, `ticket` совпадает; иначе «отброшена» с причиной. Числа в `action`, которых не дал код,
помечаются `invented_numbers` (не отбрасывают находку); «дал код» — всё, что было в промпте.

Набор по умолчанию (`--group all`):
  - плохие: TK-048, 049, 063, 065, 071, 076, 079, 087, 100, 120 (эпизоды, разобранные в docs/v2/checkpoints.md);
  - обычные: 20 тикетов, выбранных кодом: статус `done`, от 1 до 6 запусков в runs.log, ни одного возврата Судьи
    (запись судьи `[итог: return]`), не из плохих; кандидаты по номеру, `random.Random(14).sample(…, 20)`.

`--budget` — стоп новых вызовов, когда сумма цен этого запуска достигла лимита (уже идущие `--jobs` вызовов доделываются).
Запуск продолжаемый: готовые окна (не ошибки) повторно не считаются; один `--out` — один набор настроек окон и промпта.
Вызов — `claude -p --agent overseer --model claude-haiku-5-5 --effort xhigh --output-format json` с определением из
`agents/overseer.md` этой рабочей копии (через `--agents`, как у helper_eval), без инструментов, MCP и пользовательских
настроек; цена вызова — `total_cost_usd` ответа. Итоги `--out`: `windows.jsonl` (строка на окно), `prompts/` (что видел
Надзиратель), `summary.md`.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / ".claude" / "dispatcher"))
from eval_common import LOCK, append_jsonl, emit, read_jsonl  # noqa: E402
import overseer as core  # noqa: E402  ядро — общее с живым Надзирателем (`.claude/dispatcher/overseer.py`)
from overseer import (  # noqa: E402
    build_prompt, call_overseer, closed_scale, grade, is_judge_return, load_agent, load_runs, load_ticket,
    make_windows, parse_ts, scale_text, stats_at, ticket_ids, ticket_start)
BAD = ("TK-048", "TK-049", "TK-063", "TK-065", "TK-071", "TK-076", "TK-079", "TK-087", "TK-100", "TK-120")
NORMAL_N, NORMAL_SEED, NORMAL_MAX_RUNS = 20, 14, 6
MECHANICAL = ("watch", "dispatcher", "ci", "merge")  # записи кода, не ролей: для оценки «сколько окон без них»
SUPPRESS_H = 24  # против шума: одна принятая находка по тикету не чаще раза в сутки (docs/v2/checkpoints.md, CP14)



# ------------------------------------------------------------------------------------------------------------
# Набор
# ------------------------------------------------------------------------------------------------------------

def normal_pool(info: list, max_runs: int = NORMAL_MAX_RUNS, exclude=BAD) -> list:
    """Кандидаты в обычные: info = [(тикет, статус, запусков, возвратов Судьи)]. done, 1…max_runs запусков, возвратов нет, не из плохих."""
    return sorted(t for t, st, runs, ret in info if st == "done" and 1 <= runs <= max_runs and ret == 0 and t not in exclude)


def pick_normal(info: list, n: int = NORMAL_N, seed: int = NORMAL_SEED, max_runs: int = NORMAL_MAX_RUNS,
                exclude=BAD) -> list:
    """Обычные тикеты: random.Random(seed).sample(n) из кандидатов по номеру (`normal_pool`)."""
    pool = normal_pool(info, max_runs, exclude)
    return sorted(random.Random(seed).sample(pool, min(n, len(pool))))


def load_all(project: Path) -> tuple:
    tickets = {tid: load_ticket(project, tid) for tid in ticket_ids(project)}
    return tickets, load_runs(project)


def ticket_info(tickets: dict, runs: dict) -> list:
    return [(t.tid, t.status, len(runs.get(t.tid, [])), sum(1 for e in t.entries if is_judge_return(e))) for t in tickets.values()]


def choose(tickets: dict, runs: dict, only=None, group: str = "all") -> list:
    """[(тикет, группа)]: явный список (`only`) или набор по умолчанию."""
    if only:
        missing = [t for t in only if t not in tickets]
        if missing:
            raise SystemExit(f"нет тикетов: {', '.join(missing)}")
        return [(t, "bad" if t in BAD else "other") for t in only]
    info = ticket_info(tickets, runs)
    bad = [(t, "bad") for t in BAD if t in tickets]
    normal = [(t, "normal") for t in pick_normal(info)]
    return {"bad": bad, "normal": normal, "all": bad + normal}[group]


# ------------------------------------------------------------------------------------------------------------
# Вызов claude
# ------------------------------------------------------------------------------------------------------------

# ------------------------------------------------------------------------------------------------------------
# Прогон
# ------------------------------------------------------------------------------------------------------------

def prompt_opts(a) -> dict:
    return {"max_kb": a.max_kb, "ctx_lines": a.ctx_lines, "entry_chars": a.entry_chars, "desc_chars": a.desc_chars,
            "skip_roles": tuple(x for x in a.skip_roles.split(",") if x)}


def window_key(tid: str, step: int, end: datetime) -> str:
    return f"{tid}|{step}|{end.isoformat()}"


def plan_jobs(tickets: dict, runs: dict, chosen: list, a) -> list:
    """Все окна набора с готовым промптом и числами: [{…поля строки итогов…, prompt}]."""
    opts, jobs = prompt_opts(a), []
    scale = closed_scale(tickets, runs)
    for tid, group in chosen:
        t = tickets[tid]
        rs = runs.get(tid, [])
        start = ticket_start(t.entries, rs)
        total = sum(r.cost for r in rs)
        for w in make_windows(t.entries, a.step_min, opts["skip_roles"]):
            st = stats_at(w.end, t.entries, rs, start)
            prompt = build_prompt(t, w, st, scale=scale_text(scale, w.end), **opts)
            jobs.append({"key": window_key(tid, a.step_min, w.end), "ticket": tid, "group": group, "window": w.n,
                         "start": w.start.isoformat(), "end": w.end.isoformat(), "step_min": a.step_min, "n_new": len(w.new),
                         "prompt_chars": len(prompt), "runs": st.runs, "spent": round(st.cost, 4), "hours": round(st.hours, 2),
                         "returns": st.returns, "ticket_total": round(total, 4), "ticket_runs": len(rs),
                         "prompt": prompt, "_until": w.end})
    return jobs


def read_rows(out: Path) -> list:
    return read_jsonl(out / "windows.jsonl", missing_ok=True)


def cmd_run(a) -> int:
    project, out = Path(a.project).resolve(), Path(a.out).resolve()  # resolve(): короткое имя Windows Claude Code считает подозрительным
    (out / "work").mkdir(parents=True, exist_ok=True)
    (out / "prompts").mkdir(exist_ok=True)
    tickets, runs = load_all(project)
    only = [x for x in a.tickets.split(",") if x] if a.tickets else None
    chosen = choose(tickets, runs, only, a.group)
    jobs = plan_jobs(tickets, runs, chosen, a)
    done = {r["key"] for r in read_rows(out) if r["outcome"] != "error"}
    todo = [j for j in jobs if j["key"] not in done]
    agents = out / "agents.json"
    d = load_agent()
    d["model"], d["effort"] = a.model, a.effort
    agents.write_text(json.dumps({"overseer": d}, ensure_ascii=False), encoding="utf-8")
    state = {"spent": 0.0, "calls": 0, "skipped": 0}
    print(f"окон в наборе: {len(jobs)}, готово раньше: {len(jobs) - len(todo)}, к вызову: {len(todo)}, бюджет ${a.budget:.2f}", flush=True)

    def one(j):
        with LOCK:
            if state["spent"] >= a.budget:
                state["skipped"] += 1
                return
        t = tickets[j["ticket"]]
        res = None
        for _ in range(2):  # сбой связи или пустой ответ — один повтор, цена обоих вызовов в сумме
            r = call_overseer(j["prompt"], agents, out / "work", a.model, a.effort, a.per_call, a.timeout)
            res = r if res is None else {**r, "cost": (r["cost"] or 0) + (res["cost"] or 0), "retried": True}
            with LOCK:
                state["spent"] += r["cost"] or 0
                state["calls"] += 1
            if not r["error"]:
                break
        g = grade(res["answer"], j["ticket"], t.entries, j["_until"], j["prompt"]) if not res["error"] else \
            {"outcome": "error", "reason": "ошибка вызова", "kind": "", "quote": "", "action": "", "invented_numbers": []}
        row = {k: v for k, v in j.items() if k not in ("prompt", "_until")}
        row.update(res)
        row.update(g)
        prompt_file(out, row).write_text(j["prompt"], encoding="utf-8")
        append_jsonl(out / "windows.jsonl", row)
        print(f"{j['ticket']} окно {j['window']:>3} {j['end'][5:16]} ${row['cost'] or 0:.4f} {row['dur']:>5}s {row['outcome']}"
              + (f" ({row['kind']})" if row["outcome"] == "accepted" else f" [{row['reason']}]" if row["reason"] else ""), flush=True)

    with ThreadPoolExecutor(max_workers=a.jobs) as ex:
        list(ex.map(one, todo))
    if state["skipped"]:
        print(f"бюджет исчерпан: пропущено окон {state['skipped']} (продолжить — тот же --out и больший --budget)", flush=True)
    print(f"вызовов {state['calls']}, цена этого запуска ${state['spent']:.4f}", flush=True)
    return cmd_report(a)


def prompt_file(out: Path, r: dict) -> Path:
    return out / "prompts" / f"{r['ticket']}-s{r['step_min']}-{r['window']:04d}.txt"


def cmd_regrade(a) -> int:
    """Проверки заново по сохранённым ответам и промптам (правило цитаты поправлено) — без вызовов модели."""
    project, out = Path(a.project).resolve(), Path(a.out).resolve()
    last = {}
    for r in read_rows(out):
        last[r["key"]] = r
    cache = {}
    for r in last.values():
        if r["outcome"] == "error" and not r.get("answer"):
            continue
        if r["ticket"] not in cache:
            cache[r["ticket"]] = load_ticket(project, r["ticket"])
        pf = prompt_file(out, r)
        r.update(grade(r["answer"], r["ticket"], cache[r["ticket"]].entries, parse_ts(r["end"]),
                       pf.read_text(encoding="utf-8") if pf.exists() else ""))
    with (out / "windows.jsonl").open("w", encoding="utf-8") as w:
        for r in last.values():
            w.write(json.dumps(r, ensure_ascii=False) + "\n")
    return cmd_report(a)


# ------------------------------------------------------------------------------------------------------------
# План (без вызовов)
# ------------------------------------------------------------------------------------------------------------

def fit_cost(rows: list) -> tuple:
    """Цена вызова от размера промпта по готовым итогам: (a, b) для cost ≈ a + b·prompt_chars; мало данных — (среднее, 0)."""
    pts = [(r["prompt_chars"], r["cost"]) for r in rows if r.get("cost") is not None and not r.get("error")]
    if not pts:
        return 0.0, 0.0
    mx, my = sum(x for x, _ in pts) / len(pts), sum(y for _, y in pts) / len(pts)
    sxx = sum((x - mx) ** 2 for x, _ in pts)
    if len(pts) < 4 or sxx == 0:
        return my, 0.0
    b = sum((x - mx) * (y - my) for x, y in pts) / sxx
    return my - b * mx, b


def _plan_table(a, chosen: list, tickets: dict, runs: dict, jobs: list) -> list[str]:
    mech = tuple(MECHANICAL)
    md = ["| тикет | группа | статус | запусков | цена $ (resolved_cost) | Σ cost_usd $ | записей | часов | окон (шаг %d) | без механических | шаг 30 | шаг 60 | тыс. знаков промптов |" % a.step_min,
          "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    tot = [0, 0, 0, 0, 0]
    for tid, group in chosen:
        t, rs = tickets[tid], runs.get(tid, [])
        wins = [j for j in jobs if j["ticket"] == tid]
        w_mech = len(make_windows(t.entries, a.step_min, mech))
        w30, w60 = len(make_windows(t.entries, 30)), len(make_windows(t.entries, 60))
        hours = (t.entries[-1].ts - t.entries[0].ts).total_seconds() / 3600 if t.entries else 0
        kb = sum(j["prompt_chars"] for j in wins) / 1000
        md.append(f"| {tid} | {group} | {t.status} | {len(rs)} | {sum(r.cost for r in rs):.2f} | {sum(r.raw for r in rs):.2f} | "
                  f"{len(t.entries)} | {hours:.1f} | {len(wins)} | {w_mech} | {w30} | {w60} | {kb:.0f} |")
        for i, v in enumerate((len(wins), w_mech, w30, w60, kb)):
            tot[i] += v
    md.append(f"| **итого** | | | | | | | | **{tot[0]}** | {tot[1]} | {tot[2]} | {tot[3]} | {tot[4]:.0f} |")
    return md


def _plan_history(a, tickets: dict, runs: dict, jobs: list) -> tuple[list[str], float]:
    """Строки о всей истории проекта и набор в сутки (нужен оценке цены)."""
    allw = [w for t in tickets.values() for w in make_windows(t.entries, a.step_min)]
    ts = [e.ts for t in tickets.values() for e in t.entries]
    days = (max(ts) - min(ts)).total_seconds() / 86400 if ts else 0
    per_day = len(allw) / days if days else 0
    every = [r for rs in runs.values() for r in rs]
    sizes = sorted(len(j["prompt"]) for j in jobs)
    md = ["", f"Все запуски проекта: {len(every)}; цена по resolved_cost ${sum(r.cost for r in every):.0f}; "
              f"если складывать cost_usd (итог сессии с начала, при --resume считается повторно) ${sum(r.raw for r in every):.0f}.",
          f"Вся история: тикетов {len(tickets)}, окон (шаг {a.step_min} мин) {len(allw)} за {days:.1f} суток — {per_day:.0f} в сутки.",
          f"Обычные: кандидатов по правилу (done, 1–{NORMAL_MAX_RUNS} запусков, без возвратов Судьи) "
          f"{len(normal_pool(ticket_info(tickets, runs)))}; выбрано {NORMAL_N}, seed {NORMAL_SEED}."]
    if sizes:
        md.append(f"Промпт набора: средний {sum(sizes) / len(sizes):.0f} знаков, медиана {sizes[len(sizes) // 2]}, максимум {sizes[-1]}.")
    return md, per_day


def _plan_estimates(a, jobs: list, per_day: float) -> list[str]:
    md = []
    if a.fit:
        rows = [r for d in a.fit.split(",") if d for r in read_rows(Path(d).resolve())]
        al, be = fit_cost(rows)
        mean = sum(r["cost"] or 0 for r in rows) / len(rows) if rows else 0
        est_fit = sum(al + be * j["prompt_chars"] for j in jobs)
        md += ["", f"Оценка по {len(rows)} готовым вызовам (средний вызов ${mean:.4f}, размер промптов {min(r['prompt_chars'] for r in rows)}–"
                   f"{max(r['prompt_chars'] for r in rows)} знаков): окон {len(jobs)} × ${mean:.4f} = ${len(jobs) * mean:.2f}; "
                   f"по размеру промпта (${al:.5f} + ${be * 1000:.5f}/1000 знаков) = ${est_fit:.2f}.",
               f"В сутки по всей истории ({per_day:.0f} окон): ${per_day * mean:.2f} по среднему вызову."]
    if a.per_call:
        md.append(f"По заданной цене вызова ${a.per_call}: окон {len(jobs)} → ${len(jobs) * a.per_call:.2f}.")
    return md


def cmd_plan(a) -> int:
    project = Path(a.project).resolve()
    tickets, runs = load_all(project)
    only = [x for x in a.tickets.split(",") if x] if a.tickets else None
    chosen = choose(tickets, runs, only, a.group)
    jobs = plan_jobs(tickets, runs, chosen, a)
    md = _plan_table(a, chosen, tickets, runs, jobs)
    history, per_day = _plan_history(a, tickets, runs, jobs)
    emit("\n".join(md + history + _plan_estimates(a, jobs, per_day)))
    return 0


def cmd_prompt(a) -> int:
    project = Path(a.project).resolve()
    t = load_ticket(project, a.ticket)
    rs = load_runs(project).get(a.ticket, [])
    opts = prompt_opts(a)
    wins = make_windows(t.entries, a.step_min, opts["skip_roles"])
    w = next((w for w in wins if w.n == a.window), None)
    if not w:
        raise SystemExit(f"{a.ticket}: окон {len(wins)}, окна {a.window} нет")
    tickets, runs = load_all(project)
    emit(build_prompt(t, w, stats_at(w.end, t.entries, rs, ticket_start(t.entries, rs)),
                      scale=scale_text(closed_scale(tickets, runs), w.end), **opts))
    return 0


# ------------------------------------------------------------------------------------------------------------
# Сводка
# ------------------------------------------------------------------------------------------------------------

def suppress(rows: list, hours: int = SUPPRESS_H) -> list:
    """Принятые находки тикета (rows по времени) после правила «не чаще раза в `hours` часов»."""
    out, last = [], None
    for r in rows:
        if r["outcome"] != "accepted":
            continue
        t = parse_ts(r["end"])
        if last is None or t - last >= timedelta(hours=hours):
            out.append(r)
            last = t
    return out


def summarize(rows: list) -> dict:
    """Итоги по тикетам и по набору из строк windows.jsonl (последняя строка на окно)."""
    last = {}
    for r in rows:
        last[r["key"]] = r
    by = {}
    for r in sorted(last.values(), key=lambda r: (r["ticket"], r["end"])):
        by.setdefault(r["ticket"], []).append(r)
    tickets = {}
    for tid, rs in by.items():
        acc = [r for r in rs if r["outcome"] == "accepted"]
        first = acc[0] if acc else None
        tickets[tid] = {
            "group": rs[0]["group"], "windows": len(rs), "cost": sum(r["cost"] or 0 for r in rs),
            "accepted": len(acc), "accepted_daily": len(suppress(rs)), "rejected": sum(r["outcome"] == "rejected" for r in rs),
            "errors": sum(r["outcome"] == "error" for r in rs), "total": rs[0]["ticket_total"],
            "first": first, "share": (first["spent"] / first["ticket_total"]) if first and first["ticket_total"] else None}
    return tickets


def _report_tickets(tk: dict) -> list[str]:
    md = ["| тикет | группа | окон | первое окно с принятой находкой | вид | потрачено к тому моменту | доля цены тикета | часов от начала | "
          "принято | раз в сутки | отброшено | ошибок | цена замера $ |", "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for tid, v in sorted(tk.items()):
        f = v["first"]
        first = ([f"окно {f['window']} ({f['end'][5:16]})", f["kind"], f"${f['spent']:.2f} из ${v['total']:.2f}",
                  "—" if v["share"] is None else f"{v['share']:.0%}", f"{f['hours']:.1f}"] if f else ["—", "", "", "", ""])
        md.append("| " + " | ".join([tid, v["group"], str(v["windows"])] + first + [
            str(v["accepted"]), str(v["accepted_daily"]), str(v["rejected"]), str(v["errors"]), f"{v['cost']:.4f}"]) + " |")
    md.append("")
    return md


def _report_groups(tk: dict) -> list[str]:
    md = []
    bad = {t: v for t, v in tk.items() if v["group"] == "bad"}
    norm = {t: v for t, v in tk.items() if v["group"] == "normal"}
    if bad:
        early = [t for t, v in bad.items() if v["share"] is not None and v["share"] <= 0.5]
        md.append(f"Плохие: принятая находка раньше половины цены тикета — {len(early)} из {len(bad)} ({', '.join(early) or '—'}); "
                  f"вообще нашли {sum(1 for v in bad.values() if v['first'])} из {len(bad)}.")
    if norm:
        fa = [t for t, v in norm.items() if v["accepted"]]
        md.append(f"Обычные: тикетов с принятой находкой (ложная тревога) — {len(fa)} из {len(norm)} ({', '.join(fa) or '—'}); "
                  f"принятых находок всего {sum(v['accepted'] for v in norm.values())}, после правила раз в сутки {sum(v['accepted_daily'] for v in norm.values())}.")
    return md


def _report_calls(calls: list) -> list[str]:
    costs = [r["cost"] or 0 for r in calls]
    reasons = {}
    for r in calls:
        if r["outcome"] in ("rejected", "error"):
            reasons[r["reason"]] = reasons.get(r["reason"], 0) + 1
    inv = sum(1 for r in calls if r["outcome"] == "accepted" and r.get("invented_numbers"))
    return ["", f"Вызовов (окон): {len(calls)}; цена замера ${sum(costs):.4f}; цена вызова: средняя ${sum(costs) / len(costs):.4f}, "
                f"минимум ${min(costs):.4f}, максимум ${max(costs):.4f}.",
            f"Исходы: тихо {sum(r['outcome'] == 'quiet' for r in calls)}, принято {sum(r['outcome'] == 'accepted' for r in calls)}, "
            f"отброшено {sum(r['outcome'] == 'rejected' for r in calls)}, ошибок {sum(r['outcome'] == 'error' for r in calls)}"
            + (f" (причины: {json.dumps(reasons, ensure_ascii=False)})" if reasons else "") + f"; принятых, где в action есть число, которого не было в промпте: {inv}.",
            f"Среднее время вызова {sum(r['dur'] for r in calls) / len(calls):.1f} с; токенов на вызов: вход {sum(r.get('in_tok') or 0 for r in calls) / len(calls):.0f}, "
            f"выход {sum(r.get('out_tok') or 0 for r in calls) / len(calls):.0f} (из них мысли {sum(r.get('think_tok') or 0 for r in calls) / len(calls):.0f})."]


def _report_findings(calls: list) -> list[str]:
    finds = [r for r in sorted(calls, key=lambda r: (r["ticket"], r["end"])) if r["outcome"] in ("accepted", "rejected")]
    if not finds:
        return []
    return ["", "## Находки", ""] + [
        f"- {r['ticket']} окно {r['window']} ({r['end'][5:16]}), {r['outcome']}"
        + (f" [{r['reason']}]" if r["outcome"] == "rejected" else "") + f", {r['kind']}: «{r['quote']}» → {r['action']}"
        for r in finds]


def cmd_report(a) -> int:
    out = Path(a.out).resolve()
    rows = read_rows(out)
    if not rows:
        raise SystemExit(f"{out}: нет windows.jsonl")
    calls = list({r["key"]: r for r in rows}.values())
    tk = summarize(rows)
    md = ["# Замер Надзирателя", ""] + _report_tickets(tk) + _report_groups(tk) + _report_calls(calls) + _report_findings(calls)
    (out / "summary.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    emit("\n".join(md))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p, with_project=True):
        if with_project:
            p.add_argument("--project", required=True, help="каталог проекта (читается, не меняется)")
        p.add_argument("--step-min", type=int, default=15)
        p.add_argument("--skip-roles", default="", help="роли, чьи записи не видны и не открывают окно (через запятую)")
        p.add_argument("--max-kb", type=float, default=12, help="новые записи окна, КБ")
        p.add_argument("--ctx-lines", type=int, default=40, help="строк «хода тикета» до окна")
        p.add_argument("--entry-chars", type=int, default=2500)
        p.add_argument("--desc-chars", type=int, default=3000)

    def pick(p):
        p.add_argument("--tickets", default="", help="TK-087,TK-048; без него — набор по умолчанию")
        p.add_argument("--group", choices=("bad", "normal", "all"), default="all")

    pl = sub.add_parser("plan", help="окна и размер промптов набора без вызовов; оценка цены")
    common(pl)
    pick(pl)
    pl.add_argument("--fit", default="", help="каталоги готовых итогов (через запятую): оценка цены по ним")
    pl.add_argument("--per-call", type=float, default=0, help="цена вызова, $: оценка = окон × цена")
    r = sub.add_parser("run")
    common(r)
    pick(r)
    r.add_argument("--out", required=True)
    r.add_argument("--model", default=core.MODEL)
    r.add_argument("--effort", default=core.EFFORT)
    r.add_argument("--jobs", type=int, default=4)
    r.add_argument("--budget", type=float, default=1.0, help="стоп, когда цена этого запуска достигла лимита, $")
    r.add_argument("--per-call", type=float, default=0.25, help="потолок цены одного вызова, $ (--max-budget-usd)")
    r.add_argument("--timeout", type=int, default=300)
    p = sub.add_parser("report")
    p.add_argument("--out", required=True)
    rg = sub.add_parser("regrade", help="проверки заново по сохранённым ответам, без вызовов")
    rg.add_argument("--project", required=True)
    rg.add_argument("--out", required=True)
    pr = sub.add_parser("prompt", help="напечатать промпт окна")
    common(pr)
    pr.add_argument("--ticket", required=True)
    pr.add_argument("--window", type=int, required=True, help="номер окна тикета с записями, с 1")
    a = ap.parse_args(argv)
    return {"plan": cmd_plan, "run": cmd_run, "report": cmd_report, "prompt": cmd_prompt, "regrade": cmd_regrade}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
