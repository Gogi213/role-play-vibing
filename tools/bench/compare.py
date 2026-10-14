#!/usr/bin/env python3
"""Сводка замеров: `compare.py bench-ubuntu.json bench-macos.json …` → Markdown в stdout (в CI — в `$GITHUB_STEP_SUMMARY`).
Одна машина — отчёт; несколько — те же строки рядом, а у чисел, расходящихся больше чем в 1,5 раза, — пометка ⚠."""
from __future__ import annotations

import json
import sys
from pathlib import Path

SKEW = 1.5  # во сколько раз числа должны разойтись между машинами, чтобы выделить строку


def g(d, *path, default=None):
    for k in path:
        if isinstance(d, dict) and k in d:
            d = d[k]
        else:
            return default
    return d


def fmt(v, digits=0, unit=""):
    if v is None:
        return "—"
    if isinstance(v, bool):
        return "да" if v else "нет"
    if isinstance(v, (int, float)):
        return (f"{v:,.{digits}f}".replace(",", " ") if digits else f"{round(v):,}".replace(",", " ")) + unit
    return str(v)


def skew_mark(vals: list) -> str:
    nums = [v for v in vals if isinstance(v, (int, float)) and v > 0]
    if len(nums) >= 2 and max(nums) / min(nums) >= SKEW:
        return f" ⚠×{max(nums) / min(nums):.1f}"
    return ""


def table(headers: list[str], rows: list[list]) -> str:
    if not rows:
        return "_нет данных_\n"
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" if i == 0 else "---:" for i in range(len(headers))) + "|"]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out) + "\n"


def row_across(name: str, results: list[dict], getter, digits=0, unit="") -> list:
    vals = [getter(r) for r in results]
    cells = [fmt(v, digits, unit) for v in vals]
    if len(results) > 1:
        cells[-1] += skew_mark(vals)
    return [name] + cells


def render(results: list[dict]) -> str:
    L = [r["label"] for r in results]
    md = ["# Замеры role-play-vibing\n"]
    md.append(table(["", *L], [
        ["ОС", *[g(r, "env", "os", default="") for r in results]],
        ["CPU · память", *[f"{g(r, 'env', 'cpus')} · {g(r, 'env', 'memory')}" for r in results]],
        ["Python · git", *[f"{g(r, 'env', 'python')} · {g(r, 'env', 'git', default='').replace('git version ', '')}" for r in results]],
        ["Claude CLI", *[g(r, "env", "claude", default="—") or "—" for r in results]],
        ["плагин", *[f"{g(r, 'env', 'plugin_version')} @ {g(r, 'env', 'plugin_commit')}" for r in results]],
        ["где", *[g(r, "env", "runner", default="") for r in results]],
        ["всего заняло, с", *[fmt(r.get("total_wall_s")) for r in results]],
    ]))
    if any("micro" in r for r in results):
        md.append(_micro(results))
    if any("stub" in r for r in results):
        md.append(_stub(results))
    if any("e2e" in r for r in results):
        md.append(_e2e(results))
    return "\n".join(md)


def _micro(R: list[dict]) -> str:
    L = [r["label"] for r in R]
    out = ["\n## Микробенчи (без моделей)\n", "Время — медиана, мс; ⚠ — машины расходятся ≥ 1,5×.\n"]
    names = list(g(R[0], "micro", "cold_start", default={}).keys())
    out.append("### Холодный старт скриптов\n")
    out.append(table(["команда", *[f"{l} p50 мс" for l in L], *[f"{l} p95" for l in L], *[f"{l} CPU мс" for l in L], *[f"{l} RSS МиБ" for l in L]],
                     [[n, *[fmt(g(r, "micro", "cold_start", n, "wall_ms", "p50")) + (skew_mark([g(x, "micro", "cold_start", n, "wall_ms", "p50") for x in R]) if i == len(R) - 1 and len(R) > 1 else "")
                            for i, r in enumerate(R)],
                      *[fmt(g(r, "micro", "cold_start", n, "wall_ms", "p95")) for r in R],
                      *[fmt(g(r, "micro", "cold_start", n, "cpu_ms", "p50")) for r in R],
                      *[fmt(g(r, "micro", "cold_start", n, "rss_mib", "p50"), 1) for r in R]] for n in names]))
    imp = g(R[0], "micro", "import_cost", "top_cumulative_ms")
    if imp:
        out.append("Самые дорогие импорты диспетчера (мс, накопленно): " + ", ".join(f"{n} {ms}" for n, ms in imp[:6]) + "\n")
    out.append("### Операции с тикетами (запуск процесса на операцию, p50 / p95 мс)\n")
    out.append(table(["операция", *[f"{l} p50" for l in L], *[f"{l} p95" for l in L]],
                     [[op, *[fmt(g(r, "micro", "tickets_ops", op, "p50")) for r in R], *[fmt(g(r, "micro", "tickets_ops", op, "p95")) for r in R]]
                      for op in ("new", "comment", "result_done", "status")]))
    out.append("### Хуки (как их запускает Claude Code: `sh -c` из hooks.json)\n")
    hooks = list(g(R[0], "micro", "hooks", default={}).keys())
    out.append(table(["хук", *[f"{l} p50 мс" for l in L], *[f"{l} p95" for l in L], *[f"{l} CPU мс" for l in L], "вывод, Б"],
                     [[h, *[fmt(g(r, "micro", "hooks", h, "wall_ms", "p50")) for r in R], *[fmt(g(r, "micro", "hooks", h, "wall_ms", "p95")) for r in R],
                       *[fmt(g(r, "micro", "hooks", h, "cpu_ms", "p50")) for r in R], fmt(g(R[0], "micro", "hooks", h, "stdout_bytes"))] for h in hooks]))
    p50 = g(R[0], "micro", "hooks", "PreToolUse Bash (ls)", "wall_ms", "p50")
    if p50:
        out.append(f"PreToolUse висит на каждом Bash/Write/Edit роли: при {p50:.0f} мс и 40 вызовах за запуск это ≈ {p50 * 40 / 1000:.1f} с на запуск.\n")
    ctx = g(R[0], "micro", "injected_context")
    if ctx:
        out.append("### Что плагин кладёт в начало каждой сессии роли (байты; токены — в живом прогоне)\n")
        out.append(table(["роль", "вставка хука", "уставы", "всего Б", "слов", "≈ токенов*"],
                         [[k, fmt(v["hook_output_bytes"]), fmt(v["role_files_bytes"]), fmt(v["total_bytes"]), fmt(v["words"]),
                           fmt(v["approx_tokens_cyrillic_2_5_chars"])] for k, v in ctx.items()]))
        out.append("\\* грубо: кириллица ≈ 2 байта на знак и 2,5 знака на токен.\n")
    gm = g(R[0], "micro", "guard_matrix")
    if gm:
        out.append("### Страж: ожидание против факта (чёрный ящик, полный путь hooks.json)\n")
        out.append(table(["", *L], [
            ["случаев · совпало", *[f"{g(r, 'micro', 'guard_matrix', 'cases')} · {g(r, 'micro', 'guard_matrix', 'match')}" for r in R]],
            ["задержка p50 / p95, мс", *[f"{fmt(g(r, 'micro', 'guard_matrix', 'latency_ms', 'p50'))} / {fmt(g(r, 'micro', 'guard_matrix', 'latency_ms', 'p95'))}" for r in R]],
        ]))
        for r in R:
            for m in g(r, "micro", "guard_matrix", "mismatches", default=[]):
                out.append(f"- ❌ **{r['label']}**: «{m['name']}» — ждали {m['expect']}, получили {m['got']}" + (f". Причина стража: {m['detail']}" if m.get("detail") else "") + "\n")
        gaps = g(R[0], "micro", "guard_matrix", "known_gaps", default=[])
        if gaps:
            out.append("Попытки обхода (документированные дыры): " + "; ".join(
                f"{'закрыто' if x['closed'] else '**ПРОПУЩЕНО**'} — {x['name'].replace('дыра: ', '')}" for x in gaps) + "\n")
    ic = g(R[0], "micro", "identity_cli")
    if ic:
        parts = [r["label"] + " " + str(g(r, "micro", "identity_cli", "match")) + "/" + str(g(r, "micro", "identity_cli", "cases")) for r in R]
        out.append("Подмена роли в CLI (по записи диспетчера): " + ", ".join(parts) + ".\n")
    sc = g(R[0], "micro", "scale", default={})
    if sc:
        out.append("### Рост времени от числа закрытых тикетов (p50, мс)\n")
        sizes = list(sc.keys())
        rows = []
        for key, nm in (("parse_all_in_process_ms", "разбор всех тикетов в процессе"), ("dispatch_first_tick_ms", "первый тик диспетчера"),
                        ("dispatch_steady_tick", "тик диспетчера (устоявшийся)"), ("watch_once", "сторож, один круг"),
                        ("tickets_status", "tickets.py status"), ("doctor", "doctor.py"), ("report_dry_run", "report.py --dry-run"),
                        ("overseer_dry_run", "overseer.py --dry-run")):
            for r in R:
                vals = []
                for s in sizes:
                    v = g(r, "micro", "scale", s, key)
                    vals.append(fmt(v.get("wall_ms", {}).get("p50") if isinstance(v, dict) else v))
                rows.append([f"{nm} · {r['label']}", *vals])
        out.append(table(["", *[f"{s} тикетов" for s in sizes]], rows))
        out.append(table(["", *[f"{s}" for s in sizes]], [[f"строк в ceo-inbox после первого тика · {r['label']}", *[fmt(g(r, 'micro', 'scale', s, 'first_tick_ceo_inbox_lines')) for s in sizes]] for r in R]))
    ts = g(R[0], "micro", "test_suite")
    if ts:
        out.append("### Набор тестов плагина\n")
        out.append(table(["", *L], [
            row_across("тестов", R, lambda r: g(r, "micro", "test_suite", "tests")),
            row_across("параллельно (-n auto), с", R, lambda r: g(r, "micro", "test_suite", "parallel_stats", "p50"), 1),
            row_across("последовательно, с", R, lambda r: g(r, "micro", "test_suite", "serial_s"), 1),
            row_across("упавших прогонов", R, lambda r: g(r, "micro", "test_suite", "failed_runs")),
        ]))
        for r in R:
            for fl in g(r, "micro", "test_suite", "failures", default=[]):
                out.append(f"- ❌ {r['label']}: {fl}\n")
    errs = [(r["label"], k, v["error"]) for r in R for k, v in (r.get("micro") or {}).items() if isinstance(v, dict) and "error" in v]
    for lab, k, e in errs:
        out.append(f"- ❌ {lab}: раздел `{k}` упал: {e}\n")
    return "\n".join(out)


def _stub(R: list[dict]) -> str:
    L = [r["label"] for r in R]
    out = ["\n## Оркестрация плагина без моделей (заглушка вместо `claude`, боевые константы)\n",
           "Всё, что здесь измерено, — вклад плагина: на живом прогоне это мёртвое время между ходами моделей.\n"]
    rows = [
        row_across("заведён → запущен Инженер, p50 / max, с", R, lambda r: f"{fmt(g(r, 'stub', 'handoff_latency', 'queue_s', 'p50'), 1)} / {fmt(g(r, 'stub', 'handoff_latency', 'queue_s', 'max'), 1)}"),
        row_across("Инженер сдал → запущен Судья, p50 / max, с", R, lambda r: f"{fmt(g(r, 'stub', 'handoff_latency', 'handoff_s', 'p50'), 1)} / {fmt(g(r, 'stub', 'handoff_latency', 'handoff_s', 'max'), 1)}"),
        row_across("тикет целиком при ходах 4–24 с, p50, с", R, lambda r: g(r, "stub", "handoff_latency", "ticket_total_s", "p50"), 1),
        row_across("круг «вернуть → переделать → принять», с", R, lambda r: g(r, "stub", "return_cycle", "total_s"), 1),
        row_across("падение диспетчера: до `done` после перезапуска, с", R, lambda r: g(r, "stub", "crash_recovery", "recovery_to_done_s"), 1),
        row_across("…ход Инженера шёл раз / запуск посчитан дважды", R, lambda r: f"{g(r, 'stub', 'crash_recovery', 'engineer_starts')} / {fmt(g(r, 'stub', 'crash_recovery', 'double_charged'))}"),
    ]
    out.append(table(["", *L], rows))
    out.append("### Сбои ролей\n")
    frows = []
    for sc in ("engineer_crash", "engineer_silent", "judge_blocked", "judge_asks_owner"):
        frows.append([sc, *[f"{g(r, 'stub', 'failure_modes', sc, 'final_status')} за {fmt(g(r, 'stub', 'failure_modes', sc, 'to_final_s'), 1)} с, запусков {g(r, 'stub', 'failure_modes', sc, 'runs')}; "
                            f"CEO {fmt(g(r, 'stub', 'failure_modes', sc, 'ceo_inbox_delay_s'), 1)} с"
                            + (f"; Telegram {fmt(g(r, 'stub', 'failure_modes', sc, 'telegram_attempt_delay_s'), 1)} с" if sc == "judge_asks_owner" else "") for r in R]])
    out.append(table(["сценарий", *L], frows))
    out.append("### Пачка тикетов разом (ход заглушки 3 с)\n")
    out.append(table(["", *[f"{l}: умолч." for l in L], *[f"{l}: role_parallel=3" for l in L]], [
        ["тикетов · довели до `done`", *[f"{g(r, 'stub', k, 'tickets')} · {g(r, 'stub', k, 'done')}" for k in ("burst_default", "burst_role_parallel_3") for r in R]],
        ["время пачки, с (идеал при той же параллели)", *[f"{fmt(g(r, 'stub', k, 'makespan_s'), 1)} ({fmt(g(r, 'stub', k, 'ideal_makespan_s'), 1)})" for k in ("burst_default", "burst_role_parallel_3") for r in R]],
        ["пик одновременных запусков", *[fmt(g(r, "stub", k, "peak_concurrent_runs")) for k in ("burst_default", "burst_role_parallel_3") for r in R]],
        ["заведение тикетов, с", *[fmt(g(r, "stub", k, "ticket_create_s"), 1) for k in ("burst_default", "burst_role_parallel_3") for r in R]],
    ]))
    out.append("### Простой (100 закрытых тикетов, диспетчер + сторож, 60 с)\n")
    out.append(table(["", *L], [
        row_across("диспетчер: CPU, % ядра", R, lambda r: g(r, "stub", "idle_burn", "dispatcher", "cpu_pct_of_core"), 2),
        row_across("диспетчер: RSS, МиБ (дрейф за минуту)", R, lambda r: f"{fmt(g(r, 'stub', 'idle_burn', 'dispatcher', 'rss_mib'), 1)} ({fmt(g(r, 'stub', 'idle_burn', 'dispatcher', 'rss_drift_mib'), 2)})"),
        row_across("сторож: CPU, % ядра", R, lambda r: g(r, "stub", "idle_burn", "watch", "cpu_pct_of_core"), 2),
        row_across("сторож: RSS, МиБ (дрейф)", R, lambda r: f"{fmt(g(r, 'stub', 'idle_burn', 'watch', 'rss_mib'), 1)} ({fmt(g(r, 'stub', 'idle_burn', 'watch', 'rss_drift_mib'), 2)})"),
        row_across("рост каталога состояния за минуту, Б", R, lambda r: g(r, "stub", "idle_burn", "state_dir_growth_bytes")),
    ]))
    for r in R:
        for k, v in (r.get("stub") or {}).items():
            if isinstance(v, dict) and "error" in v:
                out.append(f"- ❌ {r['label']}: `{k}` упал: {v['error']}\n")
    return "\n".join(out)


def _e2e(R: list[dict]) -> str:
    out = ["\n## Живой прогон (настоящие модели)\n"]
    for r in R:
        e = r.get("e2e")
        if not e:
            continue
        out.append(f"**{r['label']}**: набор `{e['suite']}`, повторов {e['repeat']}, потрачено **${e['spent_usd']:.2f}** из потолка ${e['budget_usd']}"
                   + (f"; не запущены из-за потолка: {', '.join(e['skipped_for_budget'])}" if e["skipped_for_budget"] else "") + "\n")
    scen = []
    for r in R:
        for run in g(r, "e2e", "runs", default=[]):
            if run.get("scenario") not in scen:
                scen.append(run.get("scenario"))
    out.append("### Задача × машина: время, цена, качество\n")
    rows = []
    for sc in scen:
        for r in R:
            runs = [x for x in g(r, "e2e", "runs", default=[]) if x.get("scenario") == sc]
            if not runs:
                continue
            walls = [g(x, "phases", "wall_s") for x in runs if g(x, "phases", "wall_s")]
            costs = [x.get("cost_usd", 0) for x in runs]
            dead = [g(x, "phases", "dead_share") for x in runs if g(x, "phases", "dead_share") is not None]
            ok = sum(1 for x in runs if x.get("ok"))
            truth = [v.get("verdict_vs_truth") for x in runs for v in (x.get("tickets") or {}).values()]
            rows.append([sc, r["label"], f"{ok}/{len(runs)}", fmt(sorted(walls)[len(walls) // 2] if walls else None, 0),
                         fmt(sum(costs) / len(costs), 2), fmt(100 * sum(dead) / len(dead) if dead else None, 0, "%"),
                         "; ".join(sorted({t for t in truth if t and t != "n/a"})) or "—"])
    out.append(table(["задача", "машина", "результат верен", "время, с (мед.)", "цена, $ (ср.)", "мёртвое время", "Судья против проверки кодом"], rows))
    out.append("### Токены и цена по ролям и моделям (сумма за все запуски набора)\n")
    out.append("Токены — как их считает CLI; на машине с чужими навыками и MCP (облачная песочница) кэш-чтение раздуто их описаниями, "
               "так что между ОС сравнивать только прогоны в одинаково чистом окружении (CI).\n")
    mrows = []
    for r in R:
        agg_role, agg_model = {}, {}
        for run in g(r, "e2e", "runs", default=[]):
            for role, v in (g(run, "summary", "by_role", default={}) or {}).items():
                a = agg_role.setdefault(role, {})
                for k, x in v.items():
                    a[k] = a.get(k, 0) + x
            for model, v in (g(run, "summary", "by_model", default={}) or {}).items():
                a = agg_model.setdefault(model, {})
                for k, x in v.items():
                    a[k] = a.get(k, 0) + x
        for role, v in sorted(agg_role.items()):
            mrows.append([r["label"], f"роль {role}", fmt(v.get("runs")), fmt(v.get("in")), fmt(v.get("out")), fmt(v.get("cache_read")),
                          fmt(v.get("cache_write")), fmt(v.get("duration_s")), fmt(v.get("cost_usd"), 3)])
        for model, v in sorted(agg_model.items()):
            mrows.append([r["label"], f"модель {model}", "—", fmt(v.get("in")), fmt(v.get("out")), fmt(v.get("cache_read")),
                          fmt(v.get("cache_write")), "—", fmt(v.get("cost_usd"), 3)])
    out.append(table(["машина", "срез", "запусков", "вход", "выход", "кэш-чтение", "кэш-запись", "время в запусках, с", "$"], mrows))
    out.append("### Куда уходит время запуска и сколько вызовов инструментов\n")
    trows = []
    for r in R:
        agg = {}
        for run in g(r, "e2e", "runs", default=[]):
            for x in run.get("runs", []):
                t = x.get("transcript") or {}
                a = agg.setdefault(x["role"], {"model_s": 0, "tool_s": 0, "calls": 0, "hooked": 0, "n": 0})
                a["model_s"] += t.get("model_s", 0)
                a["tool_s"] += t.get("tool_s", 0)
                a["calls"] += t.get("tool_calls_total", 0)
                a["hooked"] += t.get("hooked_calls", 0)
                a["n"] += 1
        hook_ms = g(r, "micro", "hooks", "PreToolUse Bash (ls)", "wall_ms", "p50")
        for role, a in sorted(agg.items()):
            est = f"≈ {a['hooked'] * hook_ms / 1000:.0f} с" if hook_ms else "—"
            trows.append([r["label"], role, a["n"], fmt(a["model_s"]), fmt(a["tool_s"]), fmt(a["calls"]), fmt(a["hooked"]), est])
    out.append(table(["машина", "роль", "запусков", "модель, с", "инструменты, с", "вызовов", "из них под стражем", "оценка цены стража"], trows))
    out.append("### Фазы тикета (по часам опроса, с)\n")
    prow = []
    for sc in scen:
        for r in R:
            for x in [y for y in g(r, "e2e", "runs", default=[]) if y.get("scenario") == sc]:
                ph = x.get("phases") or {}
                prow.append([sc, r["label"], f"#{x.get('repeat')}", fmt(ph.get("wall_s"), 1), fmt(ph.get("queue_s"), 1), fmt(ph.get("runs_busy_s"), 1),
                             ", ".join(fmt(v, 0) for v in ph.get("gaps_between_runs_s", [])) or "—", fmt(ph.get("dead_s"), 1),
                             fmt(x.get("goal_to_result_s"), 1), f"{x.get('cost_usd', 0):.2f}",
                             " → ".join(g(x, "tickets", t, "status", default="?") for t in (x.get("tickets") or {})) + ("  ⏱ТАЙМАУТ" if x.get("timed_out") else "")])
    out.append(table(["задача", "машина", "повтор", "всего", "до первого запуска", "в запусках", "паузы между запусками", "мёртвое", "от git init", "$", "статус"], prow))
    bad = []
    for r in R:
        for x in g(r, "e2e", "runs", default=[]):
            if x.get("error"):
                bad.append(f"- ❌ {r['label']} {x.get('scenario')}: исключение {x['error']}")
            for t, v in (x.get("tickets") or {}).items():
                if v.get("check_ok") is False:
                    bad.append(f"- ❌ {r['label']} {x.get('scenario')} ({t}): проверка кодом не прошла — {v.get('check_detail')}; статус {v.get('status')}, Судья: {v.get('verdict_vs_truth')}")
                if abs(x.get("accounting_diff_usd") or 0) > 0.005:
                    bad.append(f"- ⚠️ {r['label']} {x.get('scenario')}: плагин насчитал ${x.get('plugin_accounted_usd')} против ${x.get('cost_usd')} по итогам запусков — учёт расходится")
                if v.get("protocol"):
                    bad.append(f"- ℹ️ {r['label']} {x.get('scenario')}: устав — {v['protocol']}")
    if bad:
        out.append("\n".join(bad) + "\n")
    return "\n".join(out)


def main(argv=None) -> int:
    paths = [Path(p) for p in (argv if argv is not None else sys.argv[1:])]
    if not paths:
        print(__doc__)
        return 2
    results = [json.loads(p.read_text(encoding="utf-8")) for p in paths]
    sys.stdout.reconfigure(encoding="utf-8")
    print(render(results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
