"""Ультракод alpha, п.3 (вред): повторы, механика агентами, долгие агенты, обход плагина. Только чтение.
Запуск: python tools/ultracode_harm.py --out файл.json   (нужен …-cost.json от ultracode_cost.py)"""
import argparse, json, subprocess
ALPHA = "C:/visual projects/alpha"
SINCE = "2026-11-04T22:44:00Z"
REPEATS = {  # причина — по главной сессии alpha (стр. указаны в отчёте)
    "audit2-part1": {"fail_subscription": ["wf_76c14898-44d"], "session_end": ["wf_0c487f8f-d4e"], "ok": ["wf_a77afef5-1ce"]},
    "w4-selection-r3": {"fail_subscription": ["wf_9880e983-503"], "session_end": ["wf_68156710-484"], "ok": ["wf_d49143c7-91f"]},
    "conv": {"owner_correction": ["wf_15f6c1f0-233", "wf_aca0fb5c-83d", "wf_b5d1ee09-036"], "ok": ["wf_6ef18a76-09b"]},
}
MECH = {"закачка 9 бирж": ["wf_5e6d75ae-e11"], "закачка Gate": ["wf_e3b20a00-8e4"], "история стакана (10 агентов, оборвано)": ["wf_2ea5ea76-098"]}


def git(*a):
    return subprocess.run(["git", "-C", ALPHA, *a], capture_output=True, text=True, encoding="utf-8").stdout


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    c = json.load(open("docs/findings/ultracode-alpha-2026-11-cost.json", encoding="utf-8"))
    br, ag = c["by_run"], c["by_agent"]
    usd = lambda w: round(br[w]["usd"], 1) if w in br else 0.0
    out = {"repeats": {}, "mechanical": {}, "long_agents": {}, "bypass": {}}
    waste = 0.0
    for k, g in REPEATS.items():
        row = {why: {"runs": ws, "usd": round(sum(usd(w) for w in ws), 1)} for why, ws in g.items()}
        w = sum(v["usd"] for why, v in row.items() if why != "ok")
        row["wasted_usd"] = round(w, 1)
        waste += w
        out["repeats"][k] = row
    out["repeats_wasted_usd"] = round(waste, 1)
    for k, ws in MECH.items():
        out["mechanical"][k] = {"runs": ws, "usd": round(sum(usd(w) for w in ws), 1)}
    out["mechanical"]["PGO-сборка (агент aebcd4a7b1343103e)"] = {"usd": round(ag["aebcd4a7b1343103e"]["usd"], 1)}
    big = [(i, v) for i, v in ag.items() if v["calls"] >= 150]
    out["long_agents"] = {"count": len(big), "usd": round(sum(v["usd"] for _, v in big), 1),
                          "top": sorted(({"id": i, "wf": v["wf"], "calls": v["calls"], "usd": round(v["usd"], 1), "maxctx": v["maxctx"],
                                          "first": v["first"], "last": v["last"], "desc": (v["desc"] or "")[:60]} for i, v in big),
                                        key=lambda x: -x["usd"])[:8]}
    # обход плагина: коммиты alpha с 04.11 22:44Z по префиксу, строки runs.log после старта
    log = git("log", "--all", "--since=" + SINCE, "--format=%s").splitlines()
    pref = {}
    for s in log:
        p = s.split("]")[0] + "]" if s.startswith("[") else "(без префикса роли)"
        pref[p] = pref.get(p, 0) + 1
    runs = [l for l in open(ALPHA + "/.claude/dispatcher/runs.log", encoding="utf-8") if l[:10] >= "2026-11-05"]
    last = open(ALPHA + "/.claude/dispatcher/runs.log", encoding="utf-8").read().splitlines()[-1][:25]
    out["bypass"] = {"commits_since_start": len(log), "by_prefix": pref,
                     "runs_log_lines_from_2026-11-05": len(runs), "runs_log_last": last}
    json.dump(out, open(a.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("ok")


main()
