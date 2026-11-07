"""Ультракод alpha, п.4 (приёмы): что дало находки. Только чтение.
Запуск: python tools/ultracode_techniques.py --out файл.json   (нужен …-cost.json от ultracode_cost.py)"""
import argparse, glob, json, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ultracode_cost import usage_rows, tok, cost
SESS = "C:/Users/Георгий/.claude/projects/C--visual-projects-alpha/215df875-21c0-4c0b-b79d-964caf0deaca/workflows"
AUDITS = {"wf_170e810f-af5": "audit (4 аудитора + скептики)", "wf_295063d9-0e2": "audit2-part2", "wf_a77afef5-1ce": "audit2-part1"}
SEV = re.compile(r"blocker|major", re.I)


def load(w):
    return json.load(open(f"{SESS}/{w}.json", encoding="utf-8"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    br = json.load(open("docs/findings/ultracode-alpha-2026-11-cost.json", encoding="utf-8"))["by_run"]
    out = {"audits": {}, "blind_pumponset": {}, "video": {}}
    tot = {"usd": 0.0, "confirmed": 0, "changes": 0, "kept_major": 0, "refuted": 0, "minor": 0}
    for w, name in AUDITS.items():
        r = load(w)["result"]
        conf = r["confirmed"]
        kept = [f for f in conf if SEV.search(str((f.get("verdict") or {}).get("corrected_severity", f.get("severity"))).split(",")[0].split(" ")[0])]
        row = {"name": name, "usd": round(br[w]["usd"], 1), "confirmed": len(conf), "refuted": len(r["refuted"]),
               "minor": len(r["minor"]), "can_change_results": sum(1 for f in conf if f.get("can_change_results")),
               "major_after_skeptic": len(kept),
               "ids_kept_major": [f["id"] for f in kept]}
        out["audits"][w] = row
        tot["usd"] += row["usd"]; tot["confirmed"] += row["confirmed"]; tot["changes"] += row["can_change_results"]
        tot["kept_major"] += row["major_after_skeptic"]; tot["refuted"] += row["refuted"]; tot["minor"] += row["minor"]
    tot["usd"] = round(tot["usd"], 1)
    tot["usd_per_confirmed"] = round(tot["usd"] / tot["confirmed"], 1)
    tot["usd_per_kept_major"] = round(tot["usd"] / tot["kept_major"], 1)
    tot["skeptic_checked"] = tot["confirmed"] + tot["refuted"]  # скептик проверял каждую серьёзную находку (скрипты: фаза «Проверка»)
    tot["skeptic_downgraded"] = tot["confirmed"] - tot["kept_major"]  # подтверждены, но серьёзность снижена до minor
    out["audits_total"] = tot
    b = load("wf_cc42e25f-104")["result"]
    out["blind_pumponset"] = {"run": "wf_cc42e25f-104", "usd": round(br["wf_cc42e25f-104"]["usd"], 1), "verdicts": [x["verdict"] for x in b],
                              "bugs_suspected": [len(x.get("suspected_core_bugs") or []) for x in b]}
    v = load("wf_6023efed-f2b")["result"]
    out["video"] = {"run": "wf_6023efed-f2b", "usd": round(br["wf_6023efed-f2b"]["usd"], 1), "reads": len(v["reads"]), "syn_chars": len(json.dumps(v["syn"], ensure_ascii=False))}
    # потолок обращений на агента: сколько стоят ответы сверх N-го (оценка экономии для п.5)
    SESSD = os.path.dirname(SESS)
    over = {100: 0.0, 150: 0.0, 200: 0.0}
    n_ag = {100: 0, 150: 0, 200: 0}
    for pth in glob.glob(SESSD + "/subagents/**/agent-*.jsonl", recursive=True):
        rows = usage_rows(pth)
        for cap in over:
            if len(rows) > cap:
                n_ag[cap] += 1
                over[cap] += sum(cost(tok(u)) for _, _, _, u in rows[cap:])
    out["call_cap"] = {str(c): {"agents_over": n_ag[c], "usd_beyond_cap": round(over[c], 1)} for c in over}
    json.dump(out, open(a.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


main()
