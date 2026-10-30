"""TK-067 (G-006): потери команды по логам проекта — фазы тикета, побудки без нового факта, возвраты Судьи, отказы и петли.
Запуск: python tools/losses.py <путь проекта> [--since 2026-10-23] [--until 2026-10-31] [--loop 20]
Читает <проект>/.claude/: dispatcher/runs.log, dispatcher/guard.log, dispatcher/ceo-inbox.md, tickets/TK-*.md и tickets/archive/*-log.md."""
import argparse
import bisect
import re
import statistics as st
from collections import Counter, defaultdict
from pathlib import Path

HEAD = re.compile(r"^### (\S+) (\S+)\s*$")
RES = re.compile(r"^\[итог: ([a-z-]+)\]\s*(.*)")
PRNUM = re.compile(r"PR #(\d+)")
KV = re.compile(r"(\w+)=(\S+)")
WAIT_FORM = re.compile(r"ждём:\s*(\S+)")
# возвраты Судьи: причина по ключевым словам текста возврата (первое совпадение); не совпало — «не размечено»
RETURN_REASONS = (
    ("ресурсы (память/OOM/закачка)", re.compile(r"oom|памят|ядр|закач|runtimemax", re.I)),
    ("доработка/пересчёт по существу", re.compile(r"вернуть|нужно:|доделать|чинить|поправить|переделать|пересчёт|пересчит|подать|условия:|ложно|до пересчёта|без пересчёта", re.I)),
    ("нет/не вошло по договору", re.compile(r"\bне вош|\bнет\b|не сделан|не хватает|не показан|не приложен|нет синтетик", re.I)),
    ("расхождение/неверно", re.compile(r"расхожд|не сходит|неверн|ошибк|сломан|не совпад|падает|красн", re.I)),
    ("тест/проверка", re.compile(r"тест|проверк|покрыт", re.I)),
    ("формальность (хвосты, форма)", re.compile(r"хвост|формат|форма|ярлык|ссылк|путь", re.I)),
)


def num(s, default=0.0):
    try:
        return float(s)
    except (TypeError, ValueError):
        return default


def in_period(ts, since, until):
    return (not since or ts >= since) and (not until or ts < until)


def read(path):
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def parse_runs(path, since="", until=""):
    out = []
    for ln in read(path).splitlines():
        p = ln.split(None, 3)
        if len(p) < 4 or not in_period(p[0], since, until):
            continue
        kv = dict(KV.findall(p[3]))
        cost = num(kv.get("resolved_cost"), num(kv.get("cost_usd")))
        out.append({"ts": p[0], "tk": p[1], "role": p[2], "reason": kv.get("reason", "?"), "cost": cost,
                    "status": kv.get("status", "?")})
    return out


def parse_logs(claude_dir):
    """{тикет: [(ts, роль, текст)]} по логу тикета и его архиву."""
    logs = defaultdict(list)
    files = list((claude_dir / "tickets").glob("*.md")) + list((claude_dir / "tickets" / "archive").glob("*-log.md"))
    for f in files:
        tid = f.name.removesuffix("-log.md").removesuffix(".md")
        cur = None
        for ln in read(f).splitlines():
            m = HEAD.match(ln)
            if m:
                cur = [m.group(1), m.group(2), []]
                logs[tid].append(cur)
            elif cur is not None:
                cur[2].append(ln)
    return {t: sorted(((a, b, "\n".join(c).strip()) for a, b, c in v), key=lambda e: e[0]) for t, v in logs.items()}


def results(entry):
    """(итог, остальной текст) по первой строке [итог: …] записи или (None, '')."""
    for ln in entry[2].splitlines():
        m = RES.match(ln.strip())
        if m:
            return m.group(1), m.group(2)
    return None, ""


def pct(a, b):
    return f"{100 * a / b:.0f}%" if b else "—"


def minutes(a, b):
    from datetime import datetime
    return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() / 60


def dist(vals):
    if not vals:
        return "нет данных"
    v = sorted(vals)
    return f"n={len(v)}, медиана {st.median(v):.0f} мин, p90 {v[min(len(v) - 1, int(0.9 * len(v)))]:.0f} мин"


def phases(logs, since, until):
    """pr→приёмка и приёмка→слияние по номеру PR из записей лога (роль merge — слияние)."""
    pr_t, acc_t, mer_t, ex = {}, {}, {}, defaultdict(list)
    for tid, ents in logs.items():
        for e in ents:
            kind, rest = results(e)
            m = PRNUM.search(rest or e[2][:300])
            if not m or not in_period(e[0], since, until):
                continue
            n = (tid, m.group(1))
            if kind == "pr":
                pr_t.setdefault(n, e[0])
            elif kind == "accept" and e[1] == "judge":
                acc_t.setdefault(n, e[0])
            elif e[1] == "merge":
                mer_t.setdefault(n, e[0])
    a = [(minutes(pr_t[k], acc_t[k]), k) for k in pr_t if k in acc_t and acc_t[k] >= pr_t[k]]
    b = [(minutes(acc_t[k], mer_t[k]), k) for k in acc_t if k in mer_t and mer_t[k] >= acc_t[k]]
    return a, b


def ceo_delay(claude_dir, logs, since, until):
    """Приближение: от строки [done] во входящих CEO до ближайшей следующей записи роли ceo в любом тикете."""
    ceo_ts = sorted(e[0] for ents in logs.values() for e in ents if e[1] == "ceo")
    out = []
    for ln in read(claude_dir / "dispatcher" / "ceo-inbox.md").splitlines():
        m = re.match(r"- (\S+) (\S+) \[done\]", ln)
        if not m or not in_period(m.group(1), since, until):
            continue
        nxt = next((t for t in ceo_ts if t > m.group(1)), None)
        if nxt:
            out.append((minutes(m.group(1), nxt), (m.group(2), m.group(1))))
    return out


def wasted_wakes(runs, logs):
    """Побудка без нового факта: запуск по next/wait_for-met/in_progress-resume/retry, после которого
    (а) в логе нет записи этой роли, или (б) запись — [итог: wait] на ту же цель, что и прошлый её wait.
    Возвращает [(запуск, вид)]."""
    out = []
    last_end = {}
    for r in sorted(runs, key=lambda r: r["ts"]):
        prev_end = last_end.get(r["tk"], "")
        last_end[r["tk"]] = r["ts"]
        if r["reason"] not in ("next", "wait_for-met", "in_progress-resume", "retry"):
            continue
        ents = logs.get(r["tk"], [])
        own = [e for e in ents if e[1] == r["role"]]
        win = [e for e in own if prev_end < e[0] <= _plus(r["ts"], 60)]
        if r["status"] != "ok":
            out.append((r, "запуск не удался: " + r["status"]))
            continue
        if not win:
            out.append((r, "нет записи роли"))
            continue
        kind, rest = results(win[-1])
        if kind != "wait":
            continue
        before = [e for e in own if e[0] <= prev_end]
        k0, rest0 = results(before[-1]) if before else (None, "")
        f1, f0 = WAIT_FORM.search(rest), WAIT_FORM.search(rest0 or "")
        if k0 == "wait" and f1 and f0 and f1.group(1) == f0.group(1):
            out.append((r, "повторное ожидание той же цели"))
    return out


def _plus(ts, sec):
    from datetime import datetime, timedelta
    return (datetime.fromisoformat(ts) + timedelta(seconds=sec)).isoformat()


def returns(logs, since, until):
    """Возвраты Судьи [итог: return]: [(тикет, ts, причина, текст)]; причина автоматически, иначе «не размечено»."""
    out = []
    for tid, ents in logs.items():
        for e in ents:
            kind, rest = results(e)
            if e[1] == "judge" and kind == "return" and in_period(e[0], since, until):
                body = re.sub(r"^(проверка|результат):\s*\S+\s*[—-]?\s*", "", rest)
                why = next((name for name, rx in RETURN_REASONS if rx.search(body)), "не размечено")
                out.append((tid, e[0], why, rest))
    return out


def refusals(claude_dir, since, until):
    """Отказы охранника команд (guard.log: ts, инструмент, команда, deny, причина): по (команда, начало причины)."""
    c = Counter()
    ex = {}
    for ln in read(claude_dir / "dispatcher" / "guard.log").splitlines():
        p = ln.split("\t")
        if len(p) >= 5 and p[3] == "deny" and in_period(p[0], since, until):
            key = (p[2], p[4][:60])
            c[key] += 1
            ex.setdefault(key, p[0])
    return c, ex


def refusal_runs(claude_dir, runs, since, until, window=60):
    """Запуски с отказом охранника: отказ (guard.log без тикета) относится к ближайшему по времени запуску,
    закончившемуся после него не позже чем через window мин. При параллельных запусках привязка приблизительна.
    Возвращает {индекс запуска: число отказов}."""
    ends = sorted(range(len(runs)), key=lambda i: runs[i]["ts"])
    stamps = [runs[i]["ts"] for i in ends]
    hit = Counter()
    for ln in read(claude_dir / "dispatcher" / "guard.log").splitlines():
        p = ln.split("	")
        if len(p) < 5 or p[3] != "deny" or not in_period(p[0], since, until):
            continue
        k = bisect.bisect_left(stamps, p[0])
        if k < len(stamps) and minutes(p[0], stamps[k]) <= window:
            hit[ends[k]] += 1
    return hit


def report(root, since="", until="", loop=20):
    cd = Path(root) / ".claude"
    runs = parse_runs(cd / "dispatcher" / "runs.log", since, until)
    logs = parse_logs(cd)
    total = sum(r["cost"] for r in runs)
    L = [f"# Потери: {Path(root).resolve().name}, {since or 'с начала'} … {until or 'сейчас'}",
         f"Запусков {len(runs)}, расход ${total:.2f} (resolved_cost, иначе cost_usd)", ""]
    byrole = defaultdict(lambda: [0, 0.0])
    for r in runs:
        byrole[r["role"]][0] += 1
        byrole[r["role"]][1] += r["cost"]
    L.append("## Расход по ролям")
    for role, (n, c) in sorted(byrole.items(), key=lambda x: -x[1][1]):
        L.append(f"- {role}: {n} запусков, ${c:.2f} ({pct(c, total)})")
    a, b = phases(logs, since, until)
    L += ["", "## 1. Фазы тикета",
          f"- PR→приёмка Судьи: {dist([x for x, _ in a])}",
          f"- приёмка→слияние: {dist([x for x, _ in b])}",
          "- PR→CI: источника времени в логах нет (ci-state.json без меток) — не считается"]
    L += [f"  пример PR→приёмка: {k[0]} PR #{k[1]} — {x:.0f} мин" for x, k in sorted(a, reverse=True)[:2]]
    L += [f"  пример приёмка→слияние: {k[0]} PR #{k[1]} — {x:.0f} мин" for x, k in sorted(b, reverse=True)[:2]]
    d = ceo_delay(cd, logs, since, until)
    L += [f"- задержка CEO ([done] во входящих → следующая запись CEO, приближение): {dist([x for x, _ in d])}"]
    L += [f"  пример: {k[0]} [done] {k[1]} — {x:.0f} мин" for x, k in sorted(d, reverse=True)[:2]]
    w = wasted_wakes(runs, logs)
    wc = sum(r["cost"] for r, _ in w)
    L += ["", "## 2. Побудки без нового факта",
          f"Определение: запуск по next/wait_for-met/in_progress-resume/retry, после которого нет записи роли в тикете, "
          f"или запись — [итог: wait] на ту же цель, что и прошлая, или запуск не удался.",
          f"- всего {len(w)} из {len(runs)} запусков, ${wc:.2f} ({pct(wc, total)} расхода)"]
    by = defaultdict(lambda: [0, 0.0])
    for r, kind in w:
        for key in (f"роль {r['role']}", f"повод {r['reason']}", f"вид: {kind}"):
            by[key][0] += 1
            by[key][1] += r["cost"]
    L += [f"  - {k}: {n} шт., ${c:.2f}" for k, (n, c) in sorted(by.items(), key=lambda x: -x[1][1])]
    L += [f"  пример: {r['tk']} {r['role']} {r['ts']} ({kind}, ${r['cost']:.2f})"
          for r, kind in sorted(w, key=lambda x: -x[0]["cost"])[:2]]
    rt = returns(logs, since, until)
    L += ["", "## 3. Возвраты Судьи", f"- всего {len(rt)}"]
    cnt = Counter(x[2] for x in rt)
    L += [f"  - {k}: {n} ({pct(n, len(rt))})" for k, n in cnt.most_common()]
    L += [f"  пример: {t} {ts} — {txt[:90]}" for t, ts, _, txt in rt[:2]]
    c, ex = refusals(cd, since, until)
    L += ["", "## 4. Отказы команд и петли", f"- отказов охранника: {sum(c.values())}"]
    L += [f"  - {k[0]}: {k[1]} — {n} (первый {ex[k]})" for k, n in c.most_common(5)]
    hit = refusal_runs(cd, runs, since, until)
    hc = sum(runs[i]["cost"] for i in hit)
    L += [f"- запусков с отказом охранника: {len(hit)} из {len(runs)}, ${hc:.2f} ({pct(hc, total)} расхода; "
          f"расход запусков целиком, не стоимость самих отказов — верхняя оценка)"]
    L += [f"  пример: {runs[i]['tk']} {runs[i]['role']} {runs[i]['ts']} — {n} отказов, ${runs[i]['cost']:.2f}"
          for i, n in sorted(hit.items(), key=lambda x: -x[1])[:2]]
    per = Counter(r["tk"] for r in runs)
    cost_tk = defaultdict(float)
    for r in runs:
        cost_tk[r["tk"]] += r["cost"]
    loops = [(t, n) for t, n in per.most_common() if n > loop]
    L += [f"- расход тикетов-петель (>{loop} запусков; не потеря, а весь их расход): {len(loops)} тикетов, "
          f"${sum(cost_tk[t] for t, _ in loops):.2f} ({pct(sum(cost_tk[t] for t, _ in loops), total)})"]
    L += [f"  - {t}: {n} запусков, ${cost_tk[t]:.2f}" for t, n in loops[:5]]
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("project")
    ap.add_argument("--since", default="")
    ap.add_argument("--until", default="")
    ap.add_argument("--loop", type=int, default=20)
    a = ap.parse_args(argv)
    print(report(a.project, a.since, a.until, a.loop))


if __name__ == "__main__":
    main()
