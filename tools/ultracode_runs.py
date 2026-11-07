"""Ультракод alpha: по прогону — имя, время, цена, начало результата; реплики владельца из главной. Только чтение.
Запуск: python tools/ultracode_runs.py --out файл.json"""
import argparse, glob, json, os
GROUPS = {
    "Пампы": "e7d9e801 cc42e25f 77affd8d 1571306d 3303296c e1f2a18f 59ed7cc7 daf88851 f11b12a4 0d56026b d7c93a02 72af6ee7 59f7ac06",
    "Данные и план статарба": "5e6d75ae 87997c9e 8cd5f6cc e3b20a00",
    "Аудиты": "170e810f 295063d9 a77afef5",
    "Правки по аудитам и волны": "5fbe7101 1ef2794c d49143c7 dfe7f9c5",
    "Схождения": "ee184859 ed51a2df 6ef18a76 53d3afc1 295a375b a226d344 6023efed 97a3b6d9",
    "Оборванные и пустые": "9880e983 76c14898 0c487f8f 68156710 15f6c1f0 aca0fb5c b5d1ee09 6a5b5ed1 4b3c4026 2ea5ea76",
    "Вне прогона": "(вне_прогона)",
}
USED = ["Данные и план статарба", "Аудиты", "Правки по аудитам и волны", "Схождения"]
LOST = ["Пампы", "Оборванные и пустые"]
SESS = "C:/Users/Георгий/.claude/projects/C--visual-projects-alpha/215df875-21c0-4c0b-b79d-964caf0deaca"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    cost = json.load(open("docs/findings/ultracode-alpha-2026-11-cost.json", encoding="utf-8"))
    runs = {}
    for p in sorted(glob.glob(SESS + "/workflows/wf_*.json")):
        d = json.load(open(p, encoding="utf-8"))
        wid = os.path.basename(p)[:-5]
        r = d.get("result")
        rs = r if isinstance(r, str) else json.dumps(r, ensure_ascii=False)
        runs[wid] = {"name": d.get("workflowName"), "keys": list(d.keys()), "result_len": len(rs or ""),
                     "result_head": (rs or "")[:600], "mtime": os.path.getmtime(p)}
    owner = []
    for n, line in enumerate(open(SESS + ".jsonl", encoding="utf-8"), 1):
        try:
            o = json.loads(line)
        except ValueError:
            continue
        if o.get("type") != "user":
            continue
        c = (o.get("message") or {}).get("content")
        if isinstance(c, list):
            c = " ".join(x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") == "text")
        if not isinstance(c, str) or not c.strip() or c.startswith("<") or "tool_use_id" in c:
            continue
        owner.append({"line": n, "ts": o.get("timestamp"), "text": c[:700]})
    groups, seen = {}, set()
    for g, ids in GROUPS.items():
        keys = [k for k in cost["by_run"] if any(k == i.replace("_", " ") or k.startswith("wf_" + i) for i in ids.split())]
        assert not seen & set(keys), g
        seen |= set(keys)
        groups[g] = {"runs": len(keys), "usd": sum(cost["by_run"][k]["usd"] for k in keys), "ids": keys}
    assert seen == set(cost["by_run"]), set(cost["by_run"]) - seen
    total = sum(g["usd"] for g in groups.values())
    summary = {"total_usd": total, "used_usd": sum(groups[g]["usd"] for g in USED),
               "lost_usd": sum(groups[g]["usd"] for g in LOST), "out_of_run_usd": groups["Вне прогона"]["usd"]}
    for k in ("used", "lost", "out_of_run"):
        summary[k + "_pct"] = 100 * summary[k + "_usd"] / total
    json.dump({"runs": runs, "owner": owner, "cost_keys": list(cost.keys()), "groups": groups, "groups_summary": summary}, open(a.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(len(runs), len(owner))
    for g, v in groups.items():
        print(f"{g}: {v['runs']} прогонов, ${v['usd']:.1f}")
    print({k: round(v, 1) for k, v in summary.items()})


main()
