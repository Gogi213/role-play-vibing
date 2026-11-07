"""Сверка возвратов Судьи с заказом: были ли требуемые Судьёй пункты в тексте тикета до возврата.

Вход: docs/findings/returns-repeat-2026-11.json (tools/returns_repeat.py).
Для каждого возврата берём значимые слова требования (≥5 букв, числа, коды) и ищем их в тексте
тикета (шапка, описание, записи НЕ судьи) до времени возврата. Доля найденных слов — «покрытие».
Покрытие ≥ 0.5 — требование было в заказе/договоре; < 0.3 — новое; между — неясно (смотреть вручную).
"""
import glob
import json
import re
import statistics
import sys

sys.stdout.reconfigure(encoding="utf-8")
ROOTS = {"rpv": ".claude/tickets", "alpha": "C:/visual projects/alpha/.claude/tickets"}
STOP = set("вернуть голова проверка пункт пункты сделан сделано нужно надо также после перед когда только которые который тикет судья отчёт отчета коммит коммита дальше docs reviews research verdict return"
           .split())


def words(s):
    s = re.sub(r"docs/\S+|[0-9a-f]{7,40}", " ", s.lower())
    return {w for w in re.findall(r"[a-zа-яё0-9_\-\.]{4,}", s) if w not in STOP}


def pre_text(proj, tid, ts):
    txt = ""
    for f in glob.glob(f"{ROOTS[proj]}/**/{tid}*.md", recursive=True):
        txt += open(f, encoding="utf-8").read() + "\n"
    parts = re.split(r"\n### ", txt)
    out = [parts[0]]
    for e in parts[1:]:
        m = re.match(r"(\d{4}-\d\d-\d\dT[\d:]+)\S* (\S+)", e)
        if m and m.group(1) <= ts[:19] and m.group(2).lower() != "judge":
            out.append(e)
    return "\n".join(out)


def main():
    data = json.load(open("docs/findings/returns-repeat-2026-11.json", encoding="utf-8"))
    res = []
    for t in data:
        for r in t["rows"]:
            req = words(r["msg"])
            if len(req) < 4:
                continue
            have = words(pre_text(t["proj"], t["tid"], r["ts"]))
            cov = len(req & have) / len(req)
            res.append({"proj": t["proj"], "tid": t["tid"], "n": r["n"], "ts": r["ts"], "cls": r["cls"], "cov": round(cov, 2)})
    json.dump(res, open("docs/findings/returns-repeat-order-2026-11.json", "w", encoding="utf-8"), ensure_ascii=False)
    print("возвратов с оценкой покрытия:", len(res), "медиана покрытия:", statistics.median(x["cov"] for x in res))
    by = {}
    for x in res:
        by.setdefault(x["cls"], []).append(x["cov"])
    for c, v in sorted(by.items(), key=lambda kv: -len(kv[1])):
        hi = sum(1 for c_ in v if c_ >= 0.5)
        lo = sum(1 for c_ in v if c_ < 0.3)
        print(f"  {c}: n={len(v)} медиана={statistics.median(v):.2f} в заказе(>=0.5)={hi} ({hi*100//len(v)}%) новое(<0.3)={lo} ({lo*100//len(v)}%)")


if __name__ == "__main__":
    main()
