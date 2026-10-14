#!/usr/bin/env python3
"""Замер плагина на этой машине: микробенчи, оркестрация на заглушке, живые задачи с моделями. Одинаково на Linux и macOS.

    python tools/bench/run.py                       # бесплатное: микробенчи + оркестрация на заглушке (≈ 12–15 мин)
    python tools/bench/run.py --live smoke          # + живая задача hello (≈ $0.5–1)
    python tools/bench/run.py --live core --repeat 2 --budget-usd 20
    python tools/bench/run.py --only micro --quick  # быстрая проверка самого харнесса

Живые наборы: smoke (1 задача), core (5), full (core + ловушка стража + параллель + падение диспетчера). Результат —
`<out>/bench-<метка>.json` и `.md`; несколько машин сводит `compare.py`. Живой замер тратит подписку/ключ: сначала `--budget-usd`."""
from __future__ import annotations

import argparse
import json
import platform
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--only", choices=["micro", "stub", "live"], action="append", help="только эти части (можно несколько раз)")
    ap.add_argument("--live", choices=["smoke", "core", "full"], help="набор живых задач (платно)")
    ap.add_argument("--tasks", help="через запятую: только эти задачи живого набора (guard_trap,@parallel,@crash_engineer …) вместо всего набора")
    ap.add_argument("--repeat", type=int, default=1, help="повторов живого набора")
    ap.add_argument("--budget-usd", type=float, default=10.0, help="потолок трат живого набора: дальше задачи не запускаются")
    ap.add_argument("--timeout-min", type=float, default=20.0, help="потолок одной живой задачи")
    ap.add_argument("--out", default="bench-out")
    ap.add_argument("--label", default="")
    ap.add_argument("--quick", action="store_true", help="мало повторов — проверка харнесса, не замер")
    ap.add_argument("--skip-tests", action="store_true", help="не гонять набор тестов плагина")
    ap.add_argument("--keep", action="store_true", help="не удалять проекты живых задач")
    a = ap.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    parts = set(a.only or ["micro", "stub"] + (["live"] if a.live else []))
    if "live" in parts and not a.live:
        ap.error("--only live требует --live smoke|core|full")
    info = C.env_info()
    label = a.label or f"{platform.system().lower()}-{platform.machine()}"
    out = {"label": label, "env": info, "args": vars(a), "started": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    t0 = time.time()
    log = lambda s: print(f"{time.time() - t0:7.0f}s {s}", flush=True)
    log(f"машина: {info['os']} {info['machine']} {info['cpus']} CPU {info['memory']}; python {info['python']}; claude {info['claude'] or '—'}")

    if "micro" in parts:
        import micro
        opts = {"n": 5 if a.quick else 15, "sizes": (10, 100) if a.quick else (10, 100, 500, 2000),
                "skip_tests": a.skip_tests or a.quick, "suite_repeats": 1 if a.quick else 2}
        out["micro"] = micro.run(opts, log)
    if "stub" in parts:
        import orchestration as O
        stub = {}
        plan = [("crash_recovery", O.crash_recovery), ("failure_modes", O.failure_modes), ("return_cycle", O.return_cycle),
                ("handoff_latency", lambda: O.handoff_latency(n=3 if a.quick else 8)),
                ("burst_default", lambda: O.burst(n=6 if a.quick else 12)),
                ("burst_role_parallel_3", lambda: O.burst(n=6 if a.quick else 12, role_parallel=3)),
                ("idle_burn", lambda: O.idle_burn(seconds=20 if a.quick else 60, tickets=20 if a.quick else 100)),
                ("telegram_delivery", O.telegram_delivery)]  # без BENCH_TG_TOKEN/BENCH_TG_CHAT — пропуск
        for name, fn in plan:
            t = time.time()
            log(f"[stub] {name} …")
            try:
                stub[name] = fn()
            except Exception as e:
                import traceback
                stub[name] = {"error": repr(e), "trace": traceback.format_exc()[-600:]}
            log(f"[stub] {name}: {time.time() - t:.0f} с")
        out["stub"] = stub
    if "live" in parts:
        import e2e
        log(f"[e2e] набор {a.live}, повторов {a.repeat}, потолок ${a.budget_usd}")
        out["e2e"] = e2e.run({"suite": a.live, "repeat": a.repeat, "budget_usd": a.budget_usd, "timeout_min": a.timeout_min,
                              "keep": a.keep, "tasks": a.tasks.split(",") if a.tasks else None}, log)
    out["total_wall_s"] = round(time.time() - t0, 1)
    d = Path(a.out)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"bench-{label}.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    import compare
    (d / f"bench-{label}.md").write_text(compare.render([out]), encoding="utf-8")
    log(f"готово: {d / f'bench-{label}.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
