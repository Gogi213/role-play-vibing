"""G-013: сколько раз [итог: pr] сдан с головой, которой не было в remote.

Запуск: python tools/unpushed_heads.py <каталог .claude проекта> [...] [--json]

Признаки залипа (по каждой записи «[итог: pr] PR #N@sha»):
  no_ci_watch  — слабый: в ci_watch.run.log нет «PR #N sha» и PR внутри окна журнала (≥ минимального
                 номера PR в нём; журнал без времени и ротируется). В залипы не входит, считается отдельно;
  marker       — позднее запись ceo/judge/engineer: «коммит <sha> … не запушен|detached HEAD» именно про этот sha;
  slow         — запись ci с этим sha позже 10 мин: считается отдельно (медленный CI ≠ не запушен).
Минуты простоя: от записи pr до записи ci с этим sha; если её нет — до следующей записи тикета.
Токены и ключи не читаются и не выводятся: берутся только sha, номера, время.
"""
import json
import re
import sys
from datetime import datetime
from pathlib import Path

SLOW_MIN = 10
ENTRY = re.compile(r"^### (\S+) (\S+)\s*$", re.M)
PR = re.compile(r"\[итог: pr\]\s*PR #(\d+)@([0-9a-f]{7,40})")
MARKER = re.compile(r"(?:коммит|голов\w*)\s+([0-9a-f]{7,40}).{0,160}(?:не запушен|detached HEAD)", re.I)
MARKER_BY = {"ceo", "judge", "engineer"}


def _ts(s):
    return datetime.fromisoformat(s)


def entries(text):
    """[(время, автор, тело)] из раздела журнала тикета."""
    ms = list(ENTRY.finditer(text))
    out = []
    for i, m in enumerate(ms):
        end = ms[i + 1].start() if i + 1 < len(ms) else len(text)
        try:
            out.append((_ts(m.group(1)), m.group(2), text[m.end():end]))
        except ValueError:
            continue
    return out


def ticket_files(claude):
    t = Path(claude) / "tickets"
    return sorted(t.glob("TK-*.md")) + sorted((t / "archive").glob("TK-*-log.md"))


def watch_seen(claude):
    p = Path(claude) / "dispatcher" / "ci_watch.run.log"
    seen = set()
    if p.exists():
        for m in re.finditer(r"PR #(\d+) ([0-9a-f]{7,40})", p.read_text("utf-8", "replace")):
            seen.add((m.group(1), m.group(2)[:7]))
    return seen


def window_min(claude):
    p = Path(claude) / "dispatcher" / "ci_watch.run.log"
    nums = [int(n) for n in re.findall(r"PR #(\d+) [0-9a-f]{7}", p.read_text("utf-8", "replace"))] if p.exists() else []
    return min(nums) if nums else 10 ** 9


def marked(body, sha):
    """Запись называет именно эту голову не запушенной (или пишет detached HEAD рядом с ней)."""
    return any(m.group(1)[:7] == sha[:7] for m in MARKER.finditer(body))


def analyse(claude):
    seen = watch_seen(claude)
    win = window_min(claude)
    by_ticket = {}
    for f in ticket_files(claude):
        tid = re.match(r"(TK-\d+)", f.name).group(1)
        by_ticket.setdefault(tid, []).extend(entries(f.read_text("utf-8", "replace")))
    rows = []
    for tid, es in sorted(by_ticket.items()):
        es.sort(key=lambda e: e[0])
        for i, (ts, who, body) in enumerate(es):
            for m in PR.finditer(body):
                pr, sha = m.group(1), m.group(2)[:7]
                later = es[i + 1:]
                ci = next((e for e in later if e[1] == "ci" and sha in e[2]), None)
                nxt = next((e for e in later if e[0] > ts), None)
                end = ci or nxt
                mins = round((end[0] - ts).total_seconds() / 60, 1) if end else 0.0
                sig = []
                weak = (pr, sha) not in seen and int(pr) >= win and not ci
                if any(e[1] in MARKER_BY and marked(e[2], sha) for e in later):
                    sig.append("marker")
                slow = bool(ci) and mins > SLOW_MIN
                rows.append({"ticket": tid, "pr": pr, "sha": sha, "at": ts.isoformat(),
                             "signals": sig, "stuck": bool(sig), "weak": weak, "slow": slow, "minutes": mins if sig else 0.0})
    return rows


def summary(rows):
    stuck = [r for r in rows if r["stuck"]]
    return {"submissions": len(rows), "stuck": len(stuck),
            "minutes": round(sum(r["minutes"] for r in stuck), 1),
            "slow_ci": sum(1 for r in rows if r["slow"] and not r["stuck"]),
            "weak_no_ci_watch": sum(1 for r in rows if r["weak"] and not r["stuck"]),
            "share": round(len(stuck) / len(rows), 3) if rows else 0.0,
            "tickets": sorted({r["ticket"] for r in stuck})}


def main(argv):
    paths = [a for a in argv if not a.startswith("--")]
    if not paths:
        print(__doc__)
        return 2
    res = {p: analyse(p) for p in paths}
    if "--json" in argv:
        print(json.dumps({p: {"summary": summary(r), "stuck": [x for x in r if x["stuck"]]}
                          for p, r in res.items()}, ensure_ascii=False, indent=1))
        return 0
    for p, r in res.items():
        print(p, summary(r))
        for x in r:
            if x["stuck"]:
                print(" ", x["ticket"], "PR#" + x["pr"], x["sha"], x["at"], x["signals"], x["minutes"])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
