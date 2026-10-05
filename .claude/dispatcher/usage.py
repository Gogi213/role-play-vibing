"""Сводка токенов по runs.log (TK-055): вход / кэш-чтение / кэш-запись / выход по тикету, роли или причине пробуждения.
  python .claude/dispatcher/usage.py [--by ticket|role|reason|day] [--since 2026-10-05] [--top N]
Старые строки (до TK-055) без cr/cw: кэш-чтение+запись = ctx_sum - in_tok, раздельно не известны (колонка cache_all)."""
import argparse
import re
from collections import defaultdict
from pathlib import Path

RUNS_LOG = Path(__file__).resolve().parent / "runs.log"
_KV = re.compile(r"(\w+)=(\S+)")


def parse(line: str):
    parts = line.split()
    if len(parts) < 4:
        return None
    kv = dict(_KV.findall(line))

    def num(k):
        try:
            return int(kv[k])
        except (KeyError, ValueError):
            return None
    inp, out, cr, cw, ctx = num("in_tok"), num("out_tok"), num("cr_tok"), num("cw_tok"), num("ctx_sum")
    inp, out = inp or 0, out or 0
    if cr is None and cw is None:
        cache_all = max((ctx or 0) - inp, 0)
        cr = cw = None
    else:
        cr, cw = cr or 0, cw or 0
        cache_all = cr + cw
    return {"ts": parts[0], "tid": parts[1], "role": parts[2], "reason": kv.get("reason", "?"),
            "status": kv.get("status", "?"), "in": inp, "out": out, "cr": cr, "cw": cw, "cache_all": cache_all}


def summarize(rows, by: str):
    keyf = {"ticket": lambda r: r["tid"], "role": lambda r: r["role"], "reason": lambda r: r["reason"],
            "day": lambda r: r["ts"][:10]}[by]
    agg = defaultdict(lambda: {"runs": 0, "in": 0, "cr": 0, "cw": 0, "cache_all": 0, "out": 0, "timeout": 0})
    for r in rows:
        a = agg[keyf(r)]
        a["runs"] += 1
        a["in"] += r["in"]
        a["out"] += r["out"]
        a["cache_all"] += r["cache_all"]
        a["cr"] += r["cr"] or 0
        a["cw"] += r["cw"] or 0
        a["timeout"] += r["status"] == "timeout"
    return agg


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--by", default="ticket", choices=["ticket", "role", "reason", "day"])
    ap.add_argument("--since", default="")
    ap.add_argument("--top", type=int, default=0)
    a = ap.parse_args(argv)
    rows = [r for r in map(parse, RUNS_LOG.read_text(encoding="utf-8").splitlines()) if r and r["ts"] >= a.since]
    agg = summarize(rows, a.by)
    items = sorted(agg.items(), key=lambda kv: -(kv[1]["cache_all"] + kv[1]["in"]))
    if a.top:
        items = items[:a.top]
    print(f"{a.by:<14}{'запусков':>9}{'timeout':>8}{'вход':>12}{'кэш-чт':>14}{'кэш-зап':>13}{'кэш всего':>15}{'выход':>12}")
    for k, v in items:
        print(f"{k:<14}{v['runs']:>9}{v['timeout']:>8}{v['in']:>12,}{v['cr']:>14,}{v['cw']:>13,}{v['cache_all']:>15,}{v['out']:>12,}")
    t = {k: sum(v[k] for v in agg.values()) for k in ("runs", "in", "cr", "cw", "cache_all", "out", "timeout")}
    print(f"{'ИТОГО':<14}{t['runs']:>9}{t['timeout']:>8}{t['in']:>12,}{t['cr']:>14,}{t['cw']:>13,}{t['cache_all']:>15,}{t['out']:>12,}")


if __name__ == "__main__":
    main()
