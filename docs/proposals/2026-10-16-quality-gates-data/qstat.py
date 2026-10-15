import re, glob, sys, collections, datetime as dt
H = re.compile(r"^### (\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)\S* (\w+)")
R = re.compile(r"\[итог: (\w[\w-]*)\]")
win = {"base 10–13.10": ("2026-10-10T00:00", "2026-10-14T00:00"), "period 14.10–16.10 00:30": ("2026-10-14T00:00", "2026-10-16T00:30")}
files = sorted(set(glob.glob(".claude/tickets/TK-*.md") + glob.glob(".claude/tickets/archive/TK-*.md")))
per = {k: collections.defaultdict(lambda: collections.Counter()) for k in win}
for f in files:
    tid = re.search(r"(TK-\d+)", f).group(1)
    lines = open(f, encoding="utf-8").read().splitlines()
    for i, l in enumerate(lines):
        m = H.match(l)
        if not m: continue
        ts, role = m.group(1)[:16], m.group(2)
        nxt = lines[i+1] if i+1 < len(lines) else ""
        r = R.search(nxt)
        for k, (a, b) in win.items():
            if a <= ts < b and role in ("engineer", "researcher", "judge"):
                c = per[k][tid]
                c["entries"] += 1
                if r:
                    c["res:" + r.group(1)] += 1
                    if role != "judge": c["exec_results"] += 1
for k, d in per.items():
    n = len(d); ret = sum(c["res:return"] for c in d.values()); withret = sum(1 for c in d.values() if c["res:return"])
    acc = sum(c["res:accept"] for c in d.values())
    print(f"{k}: тикетов с записями ролей {n}; с ≥1 возвратом {withret} ({withret*100//max(n,1)} %); возвратов {ret} ({ret/max(n,1):.2f}/тикет); приёмок {acc}")
    if k.startswith("period"):
        for t, c in sorted(d.items(), key=lambda x: int(x[0][3:])):
            print(f"  {t}: возвратов {c['res:return']}, итогов исполнителя {c['exec_results']}, приёмок {c['res:accept']}")
