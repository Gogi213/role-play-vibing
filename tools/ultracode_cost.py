"""Расход Ультракода в alpha: по прогону, фазе, агенту, часу, виду токенов. Только чтение.
Запуск: python tools/ultracode_cost.py [--out файл.json]. Цены API (USD за 1 млн) — допущение, см. PRICE."""
import argparse, glob, json, os, collections, datetime

SESS = "C:/Users/Георгий/.claude/projects/C--visual-projects-alpha/215df875-21c0-4c0b-b79d-964caf0deaca"
# Sonnet-класс (допущение: цен Sonnet 5.5 в источниках нет): вход 3, выход 15, запись кэша 1ч 6 / 5м 3.75, чтение кэша 0.30
PRICE = {"in": 3.0, "out": 15.0, "cw1h": 6.0, "cw5m": 3.75, "cr": 0.30}


def usage_rows(path):
    """Один ответ модели = одна запись по message.id (потоковые дубли отбрасываются, берём последнюю)."""
    last = {}
    for n, line in enumerate(open(path, encoding="utf-8"), 1):
        try:
            o = json.loads(line)
        except ValueError:
            continue
        if o.get("type") != "assistant":
            continue
        m = o.get("message") or {}
        u = m.get("usage")
        if not u:
            continue
        key = m.get("id") or o.get("uuid")
        last[key] = (n, o.get("timestamp"), m.get("model"), u)
    return list(last.values())


def tok(u):
    cc = u.get("cache_creation") or {}
    w1 = cc.get("ephemeral_1h_input_tokens", 0)
    w5 = cc.get("ephemeral_5m_input_tokens", 0)
    if not cc:
        w5 = u.get("cache_creation_input_tokens", 0)
    return {"in": u.get("input_tokens", 0), "out": u.get("output_tokens", 0),
            "cw1h": w1, "cw5m": w5, "cr": u.get("cache_read_input_tokens", 0)}


def cost(t):
    return sum(t[k] * PRICE[k] for k in PRICE) / 1e6


def add(a, b):
    for k in b:
        a[k] += b[k]


def zero():
    return {"in": 0, "out": 0, "cw1h": 0, "cw5m": 0, "cr": 0}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out")
    args = ap.parse_args()
    agents = {}  # agentId -> dict
    for p in glob.glob(SESS + "/subagents/**/agent-*.jsonl", recursive=True):
        aid = os.path.basename(p)[6:-6]
        parts = p.replace("\\", "/").split("/")
        wf = parts[-2] if parts[-2].startswith("wf_") else None
        rows = usage_rows(p)
        t = zero(); hours = collections.defaultdict(zero); models = collections.Counter(); maxctx = 0
        ts = []
        for n, ts_, model, u in rows:
            x = tok(u); add(t, x); models[model] += 1
            if ts_:
                ts.append(ts_); add(hours[ts_[:13]], x)
            maxctx = max(maxctx, x["in"] + x["cw1h"] + x["cw5m"] + x["cr"])
        meta = {}
        mp = p[:-6] + ".meta.json"
        if os.path.exists(mp):
            meta = json.load(open(mp, encoding="utf-8"))
        agents[aid] = {"wf": wf, "calls": len(rows), "tok": t, "usd": cost(t), "models": dict(models), "maxctx": maxctx,
                       "first": min(ts) if ts else None, "last": max(ts) if ts else None,
                       "desc": meta.get("description"), "hours": {h: cost(v) for h, v in hours.items()}, "file": p}
    wfs = {}
    for p in glob.glob(SESS + "/workflows/wf_*.json"):
        d = json.load(open(p, encoding="utf-8"))
        rid = d["runId"]
        labels = {}
        for e in d.get("workflowProgress", []):
            if e.get("type") == "workflow_agent" and e.get("agentId"):
                labels[e["agentId"]] = (e.get("label"), e.get("phaseTitle"), e.get("attempt"))
        wfs[rid] = {"name": d.get("workflowName"), "start": d.get("timestamp"), "dur_min": round((d.get("durationMs") or 0) / 60000, 1),
                    "status": d.get("status"), "agentCount": d.get("agentCount"), "totalTokens": d.get("totalTokens"),
                    "toolCalls": d.get("totalToolCalls"), "labels": labels, "file": p}
    # сверка: агенты без прогона
    for aid, a in agents.items():
        w = wfs.get(a["wf"]) if a["wf"] else None
        lab = w["labels"].get(aid) if w else None
        a["label"], a["phase"] = (lab[0], lab[1]) if lab else (None, None)
    main_rows = usage_rows(SESS + ".jsonl")
    mt = zero(); mh = collections.defaultdict(zero); mday = collections.defaultdict(zero); mm = collections.Counter()
    for n, ts_, model, u in main_rows:
        x = tok(u); add(mt, x); mm[model] += 1
        if ts_:
            add(mh[ts_[:13]], x)
    out = {"price_assumption": PRICE, "main": {"calls": len(main_rows), "tok": mt, "usd": cost(mt), "models": dict(mm),
                                              "hours": {h: cost(v) for h, v in sorted(mh.items())}}}
    # по прогону
    byrun = collections.defaultdict(lambda: {"agents": 0, "calls": 0, "tok": zero()})
    byphase = collections.defaultdict(lambda: {"agents": 0, "calls": 0, "tok": zero()})
    for a in agents.values():
        for d, k in ((byrun, a["wf"] or "(вне прогона)"), (byphase, "%s | %s" % (a["wf"], a["phase"]))):
            d[k]["agents"] += 1; d[k]["calls"] += a["calls"]; add(d[k]["tok"], a["tok"])
    out["by_run"] = {k: dict(v, usd=cost(v["tok"]), meta={x: wfs.get(k, {}).get(x) for x in ("name", "start", "dur_min", "status", "agentCount", "totalTokens")})
                     for k, v in byrun.items()}
    out["by_phase"] = {k: dict(v, usd=cost(v["tok"])) for k, v in byphase.items()}
    out["by_agent"] = {k: {x: v[x] for x in ("wf", "label", "phase", "calls", "usd", "maxctx", "first", "last", "desc", "models")} | {"tok": v["tok"]}
                       for k, v in agents.items()}
    hr = collections.defaultdict(float)
    for a in agents.values():
        for h, c in a["hours"].items():
            hr[h] += c
    out["agents_by_hour_usd"] = dict(sorted(hr.items()))
    at = zero()
    for a in agents.values():
        add(at, a["tok"])
    out["agents_total"] = {"n": len(agents), "calls": sum(a["calls"] for a in agents.values()), "tok": at, "usd": cost(at)}
    out["tok_kind_usd"] = {"main": {k: mt[k] * PRICE[k] / 1e6 for k in PRICE}, "agents": {k: at[k] * PRICE[k] / 1e6 for k in PRICE}}
    out["runs_vs_scripts"] = sorted(w for w in wfs)
    if args.out:
        json.dump(out, open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    # печать
    print("ГЛАВНАЯ: ответов %d, $%.0f, токены %s" % (out["main"]["calls"], out["main"]["usd"], mt))
    print("АГЕНТЫ: %d, ответов %d, $%.0f" % (len(agents), out["agents_total"]["calls"], out["agents_total"]["usd"]))
    print("по видам токенов, $ (агенты):", {k: round(v) for k, v in out["tok_kind_usd"]["agents"].items()})
    print("по видам токенов, $ (главная):", {k: round(v) for k, v in out["tok_kind_usd"]["main"].items()})
    print("\nПО ПРОГОНАМ (старт, имя, агентов, ответов, $, мин):")
    for k, v in sorted(out["by_run"].items(), key=lambda kv: str(kv[1]["meta"]["start"])):
        m = v["meta"]
        print(" %s %-28s %-24s ag=%3d calls=%5d $%7.1f %6s мин %s" % (str(m["start"])[5:16], k, str(m["name"])[:24], v["agents"], v["calls"], v["usd"], m["dur_min"], m["status"]))
    print("\nПо часам (UTC), $ агентов:")
    for h, c in out["agents_by_hour_usd"].items():
        print(" ", h, round(c, 1))
    print("\nПо часам главная, $:")
    for h, c in out["main"]["hours"].items():
        print(" ", h, round(c, 1))


if __name__ == "__main__":
    main()
