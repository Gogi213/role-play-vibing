"""Ультракод alpha: лента времени главной сессии — работа / ожидание агентов / простой; чем будили главную. Только чтение.
Запуск: python tools/ultracode_idle.py --out файл.json"""
import argparse, collections, datetime as dt, glob, json, re
SESS = "C:/Users/Георгий/.claude/projects/C--visual-projects-alpha/215df875-21c0-4c0b-b79d-964caf0deaca"
MAIN_GAP = 120  # секунд: событий главной нет дольше — она «ждёт»


def ts(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    ev, human, notif, kinds = [], [], [], collections.Counter()
    for line in open(SESS + ".jsonl", encoding="utf-8"):
        try:
            o = json.loads(line)
        except ValueError:
            continue
        t = o.get("timestamp")
        if not t:
            continue
        t = ts(t)
        ev.append(t)
        if o.get("type") == "user":
            c = (o.get("message") or {}).get("content")
            txt = c if isinstance(c, str) else " ".join(x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") == "text") if isinstance(c, list) else ""
            if "<task-notification" in txt or "task-notification" in str(o.get("origin")):
                notif.append(t)
            elif (o.get("origin") or {}).get("kind") == "human":
                human.append((t, txt[:120]))
        if o.get("type") == "queue-operation" and o.get("operation") == "enqueue":
            kinds[("notif" if "task-notification" in str(o.get("content")) else "human")] += 1
    ev.sort()
    ag = []
    for p in glob.glob(SESS + "/subagents/**/agent-*.jsonl", recursive=True):
        tt = []
        for line in open(p, encoding="utf-8"):
            m = re.search(r'"timestamp":"([^"]+)"', line)
            if m:
                tt.append(ts(m.group(1)))
        if tt:
            ag.append((min(tt), max(tt)))
    t0, t1 = ev[0], ev[-1]
    # секундная сетка по минутам
    n = int((t1 - t0) // 60) + 1
    agent_n = [0] * n
    for s, e in ag:
        for i in range(int((s - t0) // 60), int((e - t0) // 60) + 1):
            agent_n[i] += 1
    main_act = [False] * n
    for t in ev:
        main_act[int((t - t0) // 60)] = True
    # главная активна в минуту, если событие в этой или соседней минуте
    cls = []
    for i in range(n):
        m = main_act[i] or (i and main_act[i - 1])
        cls.append("работа" if m and not agent_n[i] else "работа+агенты" if m else "ожидание агентов" if agent_n[i] else "простой")
    tot = collections.Counter(cls)
    # простои подряд
    idle, i = [], 0
    while i < n:
        if cls[i] == "простой":
            j = i
            while j < n and cls[j] == "простой":
                j += 1
            idle.append((t0 + i * 60, t0 + j * 60, j - i))
            i = j
        else:
            i += 1
    idle.sort(key=lambda x: -x[2])
    fmt = lambda x: dt.datetime.fromtimestamp(x, dt.timezone(dt.timedelta(hours=4))).strftime("%d.%m %H:%M")
    # что делал владелец в простоях
    def h_in(s, e):
        return [x for x in human if s - 60 <= x[0] <= e + 60]
    top = [{"c": fmt(s), "по": fmt(e), "мин": m, "реплик_владельца_у_границ": len(h_in(s, e))} for s, e, m in idle[:15]]
    # паузы между репликами владельца и ответ
    hs = sorted(h[0] for h in human)
    out = {"минут_всего": n, "старт": fmt(t0), "конец": fmt(t1), "классы_мин": dict(tot),
           "доли": {k: round(100 * v / n, 1) for k, v in tot.items()},
           "простоев_подряд_>=10мин": sum(1 for x in idle if x[2] >= 10), "минут_в_них": sum(x[2] for x in idle if x[2] >= 10),
           "простои_топ": top, "реплик_владельца": len(human), "уведомлений_о_завершении": len(notif),
           "очередь_enqueue": dict(kinds), "агентов": len(ag), "макс_одновременно": max(agent_n)}
    json.dump(out, open(a.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    main()
