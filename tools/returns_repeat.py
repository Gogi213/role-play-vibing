"""TK-122: повторные возвраты Судьи (серии >=2 подряд), класс требования, чем исправлено, цена кругов, простой.
Запуск: python tools/returns_repeat.py [--json out.json] [--check]
Читает тикеты rpv (+archive) и alpha (.claude/tickets), оба runs.log, git обоих проектов (diff голов).
Серия = возвраты Судьи ([итог: return]) без судейского accept между."""
import json
import re
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROJECTS = {"rpv": Path("C:/visual projects/role-play-vibing"), "alpha": Path("C:/visual projects/alpha")}
EXEC = {"engineer", "researcher"}

# класс требования: первое совпадение по порядку (ключевые слова из текста возврата)
CLASSES = [
    ("конфликт с main", r"конфликт|DIRTY|перебазир|влить main|слить main|влей main|origin/main в|rebase|ушёл вперёд|ушел вперед"),
    ("не закоммичено/не запушено", r"не закоммич|не запуш|без коммит|не в ветке|нет в ветке|push|пуш|коммит.{0,20}(нет|не)|головы нет|нет голов"),
    ("тест/CI красный или нестабильный", r"CI красн|красн\w+ CI|CI.{0,15}(упал|красн|не зелён)|pytest.{0,20}(упал|красн)|тест.{0,25}(падает|упал|красн|нестабил|флак)|flak"),
    ("форма договора/ярлыки", r"ярлык|договор|\[тест|\[чтение|\[запуск|accept отказал|гейт|пересдай|без слов|формат|форм[аеу] "),
    ("продолжай/сдай как есть", r"голова та же|прежн\w+ вердикт|вердикт прежн|в силе|не менялась|продолж|сдай как есть|то же самое|без изменений"),
    ("не сделан пункт договора", r"не сделан|не измерен|не вызван|не закрыт|не записан|не вынесен|доделать|досчитать|дописать|[Дд]альше:|нет (стен|n|[a-zA-Zа-яА-Я]{1,12}[,;:]|\w+ \w+)|[Нн]е хватает|без вердикта|не показан|отсутствует"),
    ("числа/выводы отчёта расходятся с сырьём", r"≠|не сходит|не соответ|противореч|не число|не в форме|пик |знаменател|n_эфф|MDE|интервал|мажор|сверен|csv|calc|подтвержд"),
    ("ошибка в коде/логике", r"баг|ошибк|дефект|не ловит|пропуска|ложн|падает|неверн|не работает|E-\d+|регресс|тест на|добавить тест|фильтр|исправить|поправить|чинить|логик|код"),
]
FAULT = {
    "конфликт с main": "внешняя (main ушёл вперёд)",
    "не закоммичено/не запушено": "исполнитель сдал недоделанное",
    "тест/CI красный или нестабильный": "исполнитель сдал недоделанное",
    "форма договора/ярлыки": "договор плохо написан / требование формы",
    "продолжай/сдай как есть": "исполнитель сдал недоделанное",
    "ошибка в коде/логике": "исполнитель сдал недоделанное",
    "не сделан пункт договора": "исполнитель сдал недоделанное (проверить: был ли пункт в договоре)",
    "числа/выводы отчёта расходятся с сырьём": "исполнитель сдал недоделанное",
    "требование вне договора": "Судья требует новое вне договора",
    "прочее": "не определено",
}
OUT_OF_CONTRACT = r"вне договора|не было в договоре|новое требование|заодно|кроме того|дополнительно|ещё и|также нужно"
REVOKE = r"снят[оа]? требован|требование снято|отзываю|не требуется|не нужно|снимаю|вернул зря|ложный возврат"


def classify(text):
    if re.search(OUT_OF_CONTRACT, text, re.I):
        return "требование вне договора"
    for name, rx in CLASSES:
        if re.search(rx, text, re.I):
            return name
    return "прочее"


def ts(s):
    d = datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=timezone(timedelta(hours=4)))  # записи без пояса — +04:00


def parse_ticket(path):
    ent, cur = [], None
    for ln in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = re.match(r"### (\S+)\s+(.*)$", ln)
        if m:
            cur = dict(ts=m.group(1), role=m.group(2).strip(), body=[])
            ent.append(cur)
        elif cur is not None:
            cur["body"].append(ln)
    for e in ent:
        e["text"] = "\n".join(e["body"]).strip()
        m = re.match(r"\[итог: ([\w-]+)\]\s*(.*)", e["text"], re.S)
        e["out"], e["msg"] = (m.group(1), m.group(2)) if m else (None, e["text"])
        e["sha"] = (re.findall(r"@([0-9a-f]{7,40})", e["msg"][:300]) or re.findall(r"голова ([0-9a-f]{7,40})", e["msg"][:300]) or [None])[0]
    return ent


def load_tickets():
    """тикет + его архив лога (<ID>-log.md, старые записи): склеено по ID, порядок по времени"""
    parts = defaultdict(dict)
    for proj, root in PROJECTS.items():
        for d in (root / ".claude/tickets", root / ".claude/tickets/archive"):
            for p in sorted(d.glob("*.md")):
                tid = p.stem[:-4] if p.stem.endswith("-log") else p.stem
                try:
                    parts[(proj, tid)][p] = parse_ticket(p)
                except Exception:
                    pass
    out = {}
    for key, files in parts.items():
        seen, ent = set(), []
        for p, es in files.items():
            for e in es:
                k = (e["ts"], e["role"], e["text"][:80])
                if k not in seen:
                    seen.add(k)
                    ent.append(e)
        ent.sort(key=lambda e: ts(e["ts"]))
        out[key] = (next(iter(files)), ent)
    return out


def load_runs():
    runs = defaultdict(list)
    for proj, root in PROJECTS.items():
        for ln in (root / ".claude/dispatcher/runs.log").read_text(encoding="utf-8", errors="replace").splitlines():
            f = ln.split()
            if len(f) < 4:
                continue
            kv = dict(x.split("=", 1) for x in f[3:] if "=" in x)
            try:
                runs[(proj, f[1])].append(dict(t=ts(f[0]), role=f[2], cost=float(kv.get("resolved_cost", 0))))
            except ValueError:
                pass
    return runs


def git_lines(proj, a, b):
    """строки кода (py/sh/js/ts/yml/json, без docs/.claude/tickets/notes), добавлено+удалено между головами; None если нет коммитов"""
    if not a or not b or a == b:
        return None
    r = subprocess.run(["git", "diff", "--numstat", a, b], cwd=PROJECTS[proj], capture_output=True, text=True, encoding="utf-8")
    if r.returncode:
        return None
    code = other = 0
    for ln in r.stdout.splitlines():
        p = ln.split("\t")
        if len(p) < 3 or p[0] == "-":
            continue
        n = int(p[0]) + int(p[1])
        if re.search(r"\.(py|sh|js|ts|yml|yaml|toml|json)$", p[2]) and not re.match(r"(docs/|\.claude/tickets|\.claude/roles/notes)", p[2]):
            code += n
        else:
            other += n
    return code, other


_COMMITS = {}


def commits(proj):
    """один проход git log по проекту: [(время, заголовок, код-строк, прочих-строк)]"""
    if proj not in _COMMITS:
        r = subprocess.run(["git", "log", "--all", "--no-merges", "--numstat", "--format=@@%aI|%s"], cwd=PROJECTS[proj], capture_output=True, text=True, encoding="utf-8", errors="replace")
        out = []
        for ln in r.stdout.splitlines():
            if ln.startswith("@@"):
                t, _, subj = ln[2:].partition("|")
                out.append([ts(t), subj, 0, 0])
                continue
            p = ln.split("	")
            if len(p) < 3 or p[0] == "-" or not out:
                continue
            k = int(p[0]) + int(p[1])
            if re.search(r"\.(py|sh|js|ts|yml|yaml|toml|json)$", p[2]) and not re.match(r"(docs/|\.claude/tickets|\.claude/roles/notes)", p[2]):
                out[-1][2] += k
            else:
                out[-1][3] += k
        _COMMITS[proj] = out
    return _COMMITS[proj]


def git_window(proj, tid, t0, t1):
    """коммиты с тикетом в заголовке между t0 и t1: (код-строк, прочих-строк, коммитов)"""
    rx = re.compile(rf"{tid}($|[^0-9])")
    sel = [c for c in commits(proj) if ts(t0) < c[0] <= ts(t1) and rx.search(c[1])]
    return sum(c[2] for c in sel), sum(c[3] for c in sel), len(sel)


def series_of(entries):
    """серии возвратов: списки (индекс записи) judge-return подряд, accept судьи рвёт серию"""
    ser, cur = [], []
    for i, e in enumerate(entries):
        if e["role"] != "judge":
            continue
        if e["out"] == "return":
            cur.append(i)
        elif e["out"] == "accept":
            if cur:
                ser.append(cur)
            cur = []
    if cur:
        ser.append(cur)
    return ser


def analyse():
    tickets, runs = load_tickets(), load_runs()
    res, n_ret, n_ser1 = [], 0, 0
    for (proj, tid), (path, ent) in tickets.items():
        for s in series_of(ent):
            n_ret += len(s)
            if len(s) < 2:
                n_ser1 += 1
                continue
            rows = []
            for k, i in enumerate(s):
                e = ent[i]
                prev_sha = next((x["sha"] for x in reversed(ent[:i]) if x["role"] in EXEC and x["sha"]), None)
                nxt = next((x for x in ent[i + 1:] if x["role"] in EXEC and x["out"] in ("pr", "done", "blocked", "ask-owner")), None)
                new_sha = nxt["sha"] if nxt else None
                gl = git_lines(proj, prev_sha, new_sha)
                nxt_judge = next((x for x in ent[i + 1:] if x["role"] == "judge" and x["out"] in ("return", "accept")), None)
                cls = classify(e["msg"][:400])
                if nxt is None:
                    fix = "нет сдачи после возврата"
                else:
                    w = git_window(proj, tid, e["ts"], nxt["ts"])
                    c = gl[0] if gl is not None else w[0]
                    o = gl[1] if gl is not None else w[1]
                    if c > 0:
                        fix = f"правка кода ({c} строк)"
                    elif o > 0 or w[2] > 0:
                        fix = f"только текст/отчёт/коммит ({o} строк)"
                    else:
                        fix = "ничего (коммитов нет, сдано то же)"
                if nxt_judge and nxt_judge["out"] == "accept" and re.search(REVOKE, nxt_judge["msg"], re.I):
                    fix = "откат требования Судьёй"
                # цена и простой: сеансы тикета между предыдущим возвратом (или первой записью) и этим возвратом
                t1 = ts(e["ts"])
                t0 = ts(ent[s[k - 1]]["ts"]) if k else None
                cost = sum(r["cost"] for r in runs.get((proj, tid), []) if t0 and t0 < r["t"] <= t1) if t0 else None
                gap = (t1 - t0).total_seconds() / 60 if t0 else None
                rows.append(dict(n=k + 1, ts=e["ts"], cls=cls, fault=FAULT[cls], fix=fix, cost=cost, gap_min=gap, msg=e["msg"][:220].replace("\n", " "), full=e["msg"][:1500]))
            ceil = [x for x in ent if x["role"] == "dispatcher" and "Потолок возвратов" in x["text"]]
            idle = 0.0
            for c in ceil:
                after = next((x for x in ent if ts(x["ts"]) > ts(c["ts"]) and x["role"] != "dispatcher"), None)
                if after:
                    idle += (ts(after["ts"]) - ts(c["ts"])).total_seconds() / 60
            res.append(dict(proj=proj, tid=tid, rows=rows, ceiling=len(ceil), ceiling_idle_min=idle,
                            total_cost=sum(r["cost"] for r in runs.get((proj, tid), []))))
    return res, n_ret, n_ser1


def report(res, n_ret, n_ser1):
    L = []
    rows = [r for t in res for r in t["rows"]]
    L.append(f"тикетов с серией >=2: {len(res)} (rpv {sum(t['proj']=='rpv' for t in res)}, alpha {sum(t['proj']=='alpha' for t in res)}); возвратов в сериях: {len(rows)}; всего возвратов Судьи: {n_ret}; одиночных серий: {n_ser1}")
    L.append("\nДлины серий: " + str(sorted(Counter(len(t["rows"]) for t in res).items())))
    for name, sel in (("все возвраты в сериях", rows), ("круги 2 и далее", [r for r in rows if r["n"] >= 2])):
        c = Counter(r["cls"] for r in sel)
        L.append(f"\nКлассы — {name} (n={len(sel)}):")
        L += [f"  {k}: {v} ({100*v/len(sel):.0f}%)" for k, v in c.most_common()]
        f = Counter(r["fault"] for r in sel)
        L.append("Причина: " + "; ".join(f"{k}: {v}" for k, v in f.most_common()))
        fx = Counter(re.sub(r" \(\d+ строк\)", "", r["fix"]) for r in sel)
        L.append("Чем исправлено: " + "; ".join(f"{k}: {v}" for k, v in fx.most_common()))
    code = sorted(int(re.search(r"\((\d+)", r["fix"]).group(1)) for r in rows if r["n"] >= 2 and r["fix"].startswith("правка кода"))
    if code:
        L.append(f"Строк кода при правке (круги 2+): n={len(code)}, медиана {code[len(code)//2]}, p25 {code[len(code)//4]}, p75 {code[3*len(code)//4]}, макс {code[-1]}")
    c2 = sorted(r["cost"] for r in rows if r["n"] >= 2 and r["cost"] is not None)
    g2 = sorted(r["gap_min"] for r in rows if r["n"] >= 2 and r["gap_min"] is not None)
    if c2:
        L.append(f"Цена кругов 2+ ($): n={len(c2)}, сумма {sum(c2):.1f}, медиана {c2[len(c2)//2]:.2f}, p75 {c2[3*len(c2)//4]:.2f}, макс {c2[-1]:.2f}")
        L.append(f"Время между возвратами (мин): медиана {g2[len(g2)//2]:.0f}, p75 {g2[3*len(g2)//4]:.0f}, макс {g2[-1]:.0f}, сумма {sum(g2)/60:.1f} ч")
    L.append(f"Потолок возвратов сработал в {sum(1 for t in res if t['ceiling'])} тикетах серий; простой после потолка {sum(t['ceiling_idle_min'] for t in res)/60:.1f} ч")
    L.append("\nТикеты с сериями (проект тикет: длина | классы | цена кругов 2+ $ | всего по тикету $ | потолок):")
    for t in sorted(res, key=lambda t: -len(t["rows"])):
        cs = sum(r["cost"] for r in t["rows"] if r["cost"])
        L.append(f"  {t['proj']} {t['tid']}: {len(t['rows'])} | {', '.join(r['cls'] for r in t['rows'])} | {cs:.2f} | {t['total_cost']:.2f} | {t['ceiling']}")
    return "\n".join(L)


FORMAL = ("форма договора/ярлыки", "конфликт с main", "не закоммичено/не запушено", "тест/CI красный или нестабильный", "продолжай/сдай как есть")


def words(s):
    s = re.sub(r"docs/\S+|[0-9a-f]{7,40}", " ", s.lower())
    return {w for w in re.findall(r"[a-zа-яё0-9_\-\.]{5,}", s)}


def median(v):
    v = sorted(v)
    return v[len(v) // 2] if v else 0


def report_tables(res, n_ret):
    L = []
    rows = [r for t in res for r in t["rows"]]
    r2 = [r for r in rows if r["n"] >= 2]
    tot = sum(r["cost"] or 0 for r in r2)
    L.append("\nТаблица по классам (круги 2+): класс | все в сериях | круги 2+ | $ | доля $ | медиана $ | медиана мин | код/текст/ничего")
    for cls in [c for c, _ in Counter(r["cls"] for r in rows).most_common()]:
        a, b = [r for r in rows if r["cls"] == cls], [r for r in r2 if r["cls"] == cls]
        s = sum(r["cost"] or 0 for r in b)
        fx = Counter(r["fix"].split(" (")[0] for r in b)
        L.append(f"  {cls} | {len(a)} ({100*len(a)/len(rows):.0f}%) | {len(b)} | {s:.1f} | {100*s/tot:.0f}% | {median([r['cost'] for r in b if r['cost'] is not None]):.2f} | {median([r['gap_min'] for r in b if r['gap_min'] is not None]):.0f} | {fx['правка кода']}/{fx['только текст/отчёт/коммит']}/{fx['ничего']}")
    for cls, g in (("формальные (форма, main, коммит, CI, «продолжай»)", FORMAL),):
        a = [r for r in rows if r["cls"] in g]
        b = [r for r in r2 if r["cls"] in g]
        s = sum(r["cost"] or 0 for r in b)
        L.append(f"Формальные: возвратов в сериях {len(a)} из {len(rows)} ({100*len(a)/len(rows):.0f}%), от всех {n_ret}: {100*len(a)/n_ret:.0f}%; кругов 2+ {len(b)} из {len(r2)} ({100*len(b)/len(r2):.0f}%); $ {s:.1f} из {tot:.1f} ({100*s/tot:.0f}%)")
    for pj in ("rpv", "alpha"):
        sel = [r for t in res if t["proj"] == pj for r in t["rows"]]
        c = Counter(r["cls"] for r in sel)
        f = sum(v for k, v in c.items() if k in FORMAL)
        L.append(f"{pj}: n={len(sel)}; " + "; ".join(f"{k} {100*v/len(sel):.0f}%" for k, v in c.most_common(5)) + f"; формальные {100*f/len(sel):.0f}%")
    L.append("Деньги в хвосте: " + ", ".join(
        f"серии >={m}: {sum(1 for t in res if len(t['rows'])>=m)} тикетов, ${sum(r['cost'] or 0 for t in res if len(t['rows'])>=m for r in t['rows'] if r['n']>=2):.0f} ({100*sum(r['cost'] or 0 for t in res if len(t['rows'])>=m for r in t['rows'] if r['n']>=2)/tot:.0f}%)" for m in (4, 6)))
    sh, same = [], 0
    for t in res:
        for a, b in zip(t["rows"], t["rows"][1:]):
            wa, wb = words(a["full"]), words(b["full"])
            if len(wb) >= 4:
                x = len(wa & wb) / len(wb)
                sh.append(x)
                same += x >= 0.5
    L.append(f"Соседние пары возвратов: {len(sh)}; делят >=50% слов нового: {same} ({100*same/len(sh):.0f}%); медиана доли слов нового, уже бывших в прежнем: {median(sh):.2f}")
    return "\n".join(L)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    res, n_ret, n_ser1 = analyse()
    print(report(res, n_ret, n_ser1))
    print(report_tables(res, n_ret))
    if "--json" in sys.argv:
        Path(sys.argv[sys.argv.index("--json") + 1]).write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    if "--check" in sys.argv:
        sys.exit(0 if res and all(len(t["rows"]) >= 2 for t in res) else 1)
