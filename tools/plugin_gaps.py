"""Плагин rpv: простои по записям лога тикетов — промежутки без единой записи во всех тикетах. Только чтение.
Запуск: python tools/plugin_gaps.py --from 2026-11-05 --to 2026-11-07"""
import argparse, datetime as dt, glob, re
a = argparse.ArgumentParser(); a.add_argument("--from", dest="f", required=True); a.add_argument("--to", dest="t", required=True)
x = a.parse_args()
lo = dt.datetime.fromisoformat(x.f + "T00:00:00+04:00"); hi = dt.datetime.fromisoformat(x.t + "T23:59:59+04:00")
ts = []
for p in glob.glob(".claude/tickets/*.md") + glob.glob(".claude/tickets/archive/*.md"):
    for m in re.finditer(r"^### (\d{4}-\d\d-\d\dT[\d:]+[+-]\d\d:\d\d)", open(p, encoding="utf-8").read(), re.M):
        d = dt.datetime.fromisoformat(m.group(1))
        if lo <= d <= hi:
            ts.append(d)
ts.sort()
gaps = [(b - a_).total_seconds() / 60 for a_, b in zip(ts, ts[1:])]
big = sorted(((g, a_, b) for g, (a_, b) in zip(gaps, zip(ts, ts[1:])) if g >= 30), reverse=True)
print("записей", len(ts), "промежутков>=30мин", len(big), "минут в них", round(sum(g for g, *_ in big)), "из", round((ts[-1] - ts[0]).total_seconds() / 60))
for g, a_, b in big[:12]:
    print(round(g), a_.strftime("%d.%m %H:%M"), "->", b.strftime("%d.%m %H:%M"))
