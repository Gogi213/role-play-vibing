"""view2 как у alpha (порт tools/pulse/view2.py): тикеты → процессы, шаги, волны, прогресс/ETA, вопросы, лента.
Машинных данных и серверных заданий (cpu, /data/progress) здесь нет — это alpha-специфика."""
from __future__ import annotations

import re

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pulsedata as P  # noqa: E402
import ticket as T  # noqa: E402

TAGS_ALL = {"pc": {"tag": "ПК", "name": "этот ПК", "color": "purple"}, "vps": {"tag": "VPS", "name": "сервер", "color": "teal"},
        "calc": {"tag": "СЧЁТ", "name": "сервер счёта", "color": "blue"},
        "col": {"tag": "КОЛ", "name": "сборщик", "color": "gray"}, "you": {"tag": "ВЫ", "name": "вы", "color": "amber"}}
MORDER = ("pc", "vps", "calc", "col")
ROLE_LC = {"engineer": "инженер", "researcher": "исследователь", "judge": "судья", "ceo": "CEO"}
EMPTY_FOR = {"text": "", "on": None}
ACTIVE = ("run", "review", "repair")  # «в работе»: делается / проверяется / чинится
COUNT_KEYS = ("done", "run", "review", "repair", "wait", "todo", "bad")
EXECUTORS = ("инженер", "исследователь")  # чьи «делается»-шаги после возврата Судьи становятся «чинится»
ETA_LO, ETA_HI = 0.75, 1.4   # вилка «осталось»: доли суммы eta_min шагов критической цепочки
ETA_MIN_MEASURES = 2         # меньше замеров (шагов цепочки с eta_min) — вилку не показываем
WORK_GAP_MIN = 45            # «прошло»: промежуток между событиями длиннее — засчитывается как столько минут
BOARD_RE = re.compile(r"табло|шкал|дашборд|страниц|диспетчерск", re.I)
BOARD_TITLE_RE = re.compile(r"табло|дашборд|диспетчерск", re.I)
FEED_AGE_S = 21600
WAITING_MAX_AGE_S = 12 * 3600


HUMAN_WORDS = 6
_TECH = re.compile(r"https?://\S+|host:\S+|\S*/\S+|(?<!\w)--?[A-Za-z][\w-]*|\bmd5\b|\bTK-?\d+\S*|\b[ВвTt]-\d+|\bветк\w*\s+\S+|\bИтог\w*", re.I)

FEED_NOT = re.compile(r"ждёт|ждём|ожида|оценк|прогноз|дальше|круг|\b[bB]\d+\w*|PGO|\bFF\b|perf|соло", re.I)
FEED_NUM = re.compile(r"\d+(?:[.,]\d+)?(?:\s*[–…-]+\s*\d+(?:[.,]\d+)?)?")  # «Недавно»: итог с числом, не ожидание и не оценка


def human(text: str, words: int = HUMAN_WORDS) -> str:
    """Текст шага для клетки табло: без скобок, путей, хэшей, номеров тикетов и флагов; не больше `words` слов."""
    s = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", text or "")
    s = _TECH.sub(" ", s)
    s = re.split(r"[:;]|\s—\s", s.strip(" :;—-"), maxsplit=1)[0] if len(s.split()) > words else s
    out = []
    for w in s.split():
        w = w.strip(" ,.;:—-+*=")
        if not w or (re.search(r"\d", w) and re.search(r"[A-Za-zА-Яа-я]", w)) or re.fullmatch(r"[0-9a-f]{7,}", w):
            continue
        out.append(w)
    return " ".join(out[:words]) or "работа идёт"


def _step(n, title, who, on, forr, state, **kw) -> dict:
    d = {"n": n, "title": title, "who": who, "on": on, "for": forr, "state": state, "pct": None, "detail": None,
         "eta_min": None, "started": None, "finished": None, "question": None, "after": None, "wave": 1}
    d.update(kw)
    return d


def step_waves(steps: list) -> None:
    """`after` (None — предыдущий шаг; пусто — стартует сразу; ссылки только на более ранние) → `after` и `wave` шага."""
    for i, s in enumerate(steps, 1):
        raw = s.get("after")
        if raw is None:
            raw = [i - 1] if i > 1 else []
        a = sorted({x for x in raw if isinstance(x, int) and not isinstance(x, bool) and 1 <= x < i})
        s["after"] = a
        s["wave"] = 1 + max((steps[x - 1]["wave"] for x in a), default=0)


def proc_waves(ids: list, deps: dict) -> dict:
    """Волна процесса = 1 + максимум волн процессов из `depends`; цикл не роняет (ребро внутрь цикла не считается)."""
    memo: dict = {}

    def w(i, stack=()):
        if i in memo:
            return memo[i]
        if i in stack:
            return 1
        v = 1 + max((w(d, stack + (i,)) for d in deps.get(i, []) if d != i and d in ids), default=0)
        memo[i] = v
        return v

    return {i: w(i) for i in ids}


def plural(n: int, forms: tuple) -> str:
    if 11 <= n % 100 <= 14:
        return forms[2]
    return forms[0] if n % 10 == 1 else forms[1] if 2 <= n % 10 <= 4 else forms[2]


def wave_groups(procs: list) -> list:
    """Группы обзора: «ВОЛНА 1 · 2 процесса параллельно», «ВОЛНА 2 · ждёт волну 1», в конце — «БЕЗ ХОЗЯИНА»."""
    groups: dict = {}
    orph = []
    for p in procs:
        (orph if p["wave"] is None else groups.setdefault(p["wave"], [])).append(p)
    out = []
    for w in sorted(groups):
        ps = groups[w]
        st = "done" if all(p["state"] == "done" for p in ps) else "todo" if all(p["state"] == "todo" for p in ps) else "run"
        parts = [f"ВОЛНА {w}"]
        if len(ps) > 1:
            parts.append(f"{len(ps)} {plural(len(ps), ('процесс', 'процесса', 'процессов'))} параллельно")
        out.append({"n": w, "label": parts, "state": st, "procs": [p["id"] for p in ps]})
    for g in out:
        if g["state"] == "todo":
            before = [h["n"] for h in out if h["n"] < g["n"] and h["state"] != "done"]
            if before:
                g["label"].append(f"ждёт волну {max(before)}")
        elif g["state"] == "done":
            g["label"].append("готова")
        g["label"] = " · ".join(g["label"])
    if orph:
        out.append({"n": None, "label": "БЕЗ ХОЗЯИНА", "state": "bad", "procs": [p["id"] for p in orph]})
    return out


def step_weight(s: dict) -> float:
    if s["state"] == "done":
        return 1.0
    return max(0.0, min(1.0, s["pct"] / 100)) if s.get("pct") is not None else 0.0


def critical_eta(work: list):
    """Критическая цепочка шагов (самый длинный путь по eta_min через шаги и зависимости процессов) → вилка «осталось».
    None — замеров (шагов цепочки с eta_min) меньше ETA_MIN_MEASURES."""
    best: dict = {}
    by_id = {p["id"]: p for p in work}
    sinks = {p["id"]: [s["n"] for s in p["steps"] if not any(s["n"] in x["after"] for x in p["steps"])] for p in work}
    for p in sorted(work, key=lambda p: (p["wave"] or 0, p["n"])):
        entry = [(d, n) for d in p.get("depends") or [] if d in by_id for n in sinks.get(d, [])]
        for s in p["steps"]:
            preds = [(p["id"], a) for a in s["after"]] or entry
            w = float(s["eta_min"]) if s["state"] != "done" and s.get("eta_min") is not None else 0.0
            bp = max(preds, key=lambda k: best[k][0], default=None)
            best[(p["id"], s["n"])] = ((best[bp][0] if bp else 0.0) + w, bp)
    if not best:
        return None
    k = max(best, key=lambda k: best[k][0])
    path = []
    while k:
        path.append(k)
        k = best[k][1]
    path.reverse()
    pick = [(pid, n) for pid, n in path if by_id[pid]["steps"][n - 1]["state"] != "done"]
    meas = [(pid, n) for pid, n in pick if by_id[pid]["steps"][n - 1].get("eta_min") is not None]
    if len(meas) < ETA_MIN_MEASURES:
        return None
    tot = sum(float(by_id[pid]["steps"][n - 1]["eta_min"]) for pid, n in meas)
    return {"lo_min": max(1, round(ETA_LO * tot)), "hi_min": max(1, round(ETA_HI * tot)), "measured": len(meas),
            "chain": [{"process": pid, "n": n} for pid, n in pick]}


def work_minutes(stamps: list, now_ts: float, gap: float = WORK_GAP_MIN):
    """Рабочее время: промежутки между событиями > gap мин засчитываются как gap (и последний — до «сейчас»)."""
    ts = sorted(set(stamps))
    if not ts:
        return None
    tot = sum(min(gap, (b - a) / 60) for a, b in zip(ts, ts[1:]))
    return int(round(tot + min(gap, max(0.0, (now_ts - ts[-1]) / 60))))


def progress_block(work: list, stamps: list, now_ts: float):
    """Общая полоса: вес шага 1, у шага с pct — частично; процессы «без хозяина» не в счёте (это не работа к цели)."""
    total = sum(len(p["steps"]) for p in work)
    if not total:
        return None
    done = sum(step_weight(s) for p in work for s in p["steps"])
    pct = int(100 * done / total + 0.5)
    if done < total:
        pct = min(pct, 99)
    return {"pct": pct, "done": round(done, 2), "total": total, "eta": critical_eta(work),
            "spent_min": work_minutes(stamps, now_ts)}


def judge_returned(t) -> bool:
    """Последняя запись Судьи — «вернуть/возврат», а тикет с тех пор на ревью не сдан снова (status != in_review) и не закрыт."""
    if t is None or t.status in ("in_review", "done"):
        return False
    last = next((e for e in reversed(t.log) if _is_judge(e.author)), None)
    return bool(last and re.search(r"вернуть|возврат", last.text, re.I))


def adjust_states(steps: list, t, returned: bool) -> None:
    """Три активных состояния из «идёт»: шаг судьи → проверяется; тикет in_review → проверяется; после возврата Судьи
    шаг исполнителя → чинится. Явные review/repair из плана остаются как есть."""
    in_review = t is not None and t.status == "in_review"
    for s in steps:
        if s["state"] != "run":
            continue
        if s["who"] == "судья" or (in_review and s["who"] not in ("автомат", "вы")):
            s["state"] = "review"
        elif returned and s["who"] in EXECUTORS:
            s["state"] = "repair"
    if in_review and not any(s["state"] in ACTIVE + ("wait", "bad") for s in steps):
        todo = [s for s in steps if s["state"] == "todo"]
        pick = next((s for s in todo if s["who"] == "судья"), todo[0] if todo else None)
        if pick:
            pick["state"] = "review"


def _pstate(steps: list) -> str:
    st = {s["state"] for s in steps}
    for k in ("bad", "wait", "repair", "review", "run"):
        if k in st:
            return k
    return "done" if st == {"done"} else "todo"


def _step_now(steps: list) -> int:
    for pick in (ACTIVE + ("wait",), ("bad",), ("todo",)):
        for s in steps:
            if s["state"] in pick:
                return s["n"]
    return steps[-1]["n"] if steps else 0




def _is_judge(author: str) -> bool:
    return (author or "").strip().lower() in ("judge", "судья")


def _clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _t_updated(t, default: float) -> float:
    d = P.parse(t.header.get("updated"))
    return d.timestamp() if d else default


def _wait_human(t) -> tuple:
    wf = (t.header.get("wait_for") or "").strip().lower()
    m = re.match(r"(?:host:)?(calc|vps)\b", wf)
    if t.status == "waiting" and wf.startswith("at:"):
        left = T.at_left_text(t.header.get("wait_for") or "")
        if left:
            return left, "pc"
    if t.status == "needs_owner":
        return "ждёт вашего слова", "you"
    if t.next_role == "ceo" or wf.startswith(("ceo", "mention")) or (t.status == "waiting" and not wf):
        return "ждёт решения CEO", "pc"
    if m:
        return ("ждёт конца счёта", "calc") if m.group(1) == "calc" else ("ждёт конца сборки", "vps")
    if wf.startswith("ticket:"):
        return "ждёт другую задачу", "pc"
    if t.status == "in_review":
        return "ждёт проверки Судьи", "pc"
    if t.next_role or t.status in ("todo", "in_progress"):
        return "в очереди", "pc"
    return human(wf or "ждёт"), "pc"


def make(tickets: dict, plans: dict, allq: list, now: float) -> dict:
    """tickets {id: Ticket}; plans {id: план}; allq — вопросы. Машинных данных и серверных заданий нет (alpha-специфика)."""
    now_dt = P.now_dt()
    allq = sorted([q for q in allq if isinstance(q, dict) and q.get("id")], key=lambda q: q.get("since") or "")
    openq = [q for q in allq if not q.get("answered_at")]
    qopen_ids = {q["id"] for q in openq}

    def mins_since(iso):
        d = P.parse(iso)
        return max(0, int((now_dt - d).total_seconds() // 60)) if d else None

    def when(iso):
        d = P.parse(iso)
        if not d:
            return ""
        d = d.astimezone(P.TZ)
        return d.strftime("%H:%M") if d.date() == now_dt.date() else d.strftime("%d.%m %H:%M")

    def ptitle(tid):
        tk = tickets.get(tid)
        return (_clip((tk.header.get("title", "") or "").strip(), 60) if tk else "") or tid

    def plan_steps(plan) -> list:
        steps = []
        for i, s in enumerate(plan["steps"], 1):
            q = s.get("question")
            af = s.get("after")
            steps.append(_step(i, s.get("title", ""), s.get("who", ""), s.get("on", "pc"), s.get("for") or "",
                               s.get("state", "todo"), detail=s.get("detail"), started=P.hhmm(s.get("started_at")),
                               finished=P.hhmm(s.get("finished_at")), question=q if q in qopen_ids else None,
                               after=af if isinstance(af, list) else None))
        return steps

    def log_steps(t) -> list:
        """План не написан — прошлые шаги берём из лога тикета (по записи на шаг, последние 3)."""
        out = []
        for e in (t.log or [])[-3:]:
            who = (e.author or "").lower()
            txt = _clip(re.split(r"(?<=[.!?])\s", " ".join((e.text or "").split()), 1)[0], 60)
            if not txt or who not in ROLE_LC:
                continue
            out.append(_step(0, txt, ROLE_LC[who], "pc", "", "done", finished=e.ts.astimezone(P.TZ).strftime("%H:%M") if e.ts else None))
        return out

    def synth_steps(t) -> tuple:
        wt, wmid = _wait_human(t)
        prev = log_steps(t)
        if prev:
            cur, mids = synth_steps_now(t, wt, wmid)
            steps = prev + cur
            for i, s in enumerate(steps, 1):
                s["n"] = i
            return steps, mids
        return synth_steps_now(t, wt, wmid)

    def synth_steps_now(t, wt, wmid) -> tuple:
        if t.status == "in_review":
            return [_step(1, "проверка", "судья", "pc", "", "run")], ["pc"]
        if t.status == "in_progress":
            role = ROLE_LC.get((t.owner or "").lower(), t.owner or "")
            return [_step(1, "работа идёт", role, "pc", "", "run")], ["pc"]
        owner = t.status == "needs_owner"
        return [_step(1, wt, "вы" if owner else ("CEO" if "CEO" in wt else "автомат"), "you" if owner else wmid,
                      "", "wait" if t.status in ("waiting", "needs_owner") else "todo")], [wmid]

    procs = []
    for tid, t in tickets.items():
        if t.status in ("done", "stopped"):
            continue
        plan = plans.get(tid)
        if not (plan or t.status in ("todo", "in_progress", "in_review", "needs_owner")
                or (t.status == "waiting" and now - _t_updated(t, now) < WAITING_MAX_AGE_S)):
            continue
        if plan:
            steps = plan_steps(plan)
            flow, forr, title = plan.get("flow") or [], plan.get("for") or EMPTY_FOR, plan.get("title") or ptitle(tid)
        else:
            steps, mids = synth_steps(t)
            flow = [{"text": "идёт на", "on": m} for m in dict.fromkeys(mids)] or [{"text": "в очереди на", "on": "pc"}]
            forr, title = EMPTY_FOR, ptitle(tid)
        adjust_states(steps, t, judge_returned(t))
        procs.append({"id": tid, "title": title, "flow": flow, "for": forr, "steps": steps, "has_plan": bool(plan),
                      "plan": bool(plan), "role": (t.header.get("owner") or "").strip()})

    for p in procs:
        step_waves(p["steps"])
    real = [p["id"] for p in procs]
    deps = {}
    for pid in real:
        raw = tickets[pid].header.get("depends") or ""
        deps[pid] = [d for d in dict.fromkeys(P.tk_id(x) for x in re.findall(r"(?i)tk[-_]?\d+", raw)) if d in real and d != pid]
    pw = proc_waves(real, deps)

    def summary_of(p):
        if BOARD_RE.search(p["title"]):
            return None
        done = [s for s in p["steps"] if s["state"] == "done"]
        if not done:
            return None
        return f"сделано: {done[-1]['title']} — {len(done)} из {len(p['steps'])}"

    for p in procs:
        st = p["steps"]
        run = next((s for s in st if s["state"] in ACTIVE), None)
        pq = [q for q in openq if q.get("process") == p["id"]]
        p.update(state=_pstate(st), step_now=_step_now(st), steps_total=len(st),
                 eta_min=run["eta_min"] if run else None,
                 wait_min=max((mins_since(q.get("since")) or 0 for q in pq), default=None) if pq else None,
                 wave=pw.get(p["id"]), depends=deps.get(p["id"], []))
        p["summary"] = summary_of(p)
    procs.sort(key=lambda p: (p.get("wave") or 0, p["state"] == "done", p["id"]))
    for i, p in enumerate(procs, 1):
        p["n"] = i
    waves = wave_groups(procs)

    stamps = []
    for p in procs:
        for e in tickets[p["id"]].log:
            stamps.append(e.ts.timestamp())
        for s in (plans.get(p["id"]) or {}).get("steps", []):
            for k in ("started_at", "finished_at"):
                d = P.parse(s.get(k))
                if d:
                    stamps.append(d.timestamp())
        for q in allq:
            if q.get("process") == p["id"]:
                for k in ("since", "answered_at"):
                    d = P.parse(q.get(k))
                    if d:
                        stamps.append(d.timestamp())
    progress = progress_block([p for p in procs if p.get("plan") and p["state"] != "done"], stamps, now)

    questions = []
    for i, q in enumerate(openq, 1):
        keys = {o.get("key") for o in q.get("options") or []}
        questions.append({"id": q["id"], "n": i, "process": q.get("process"), "from": q.get("from"), "on": q.get("on"),
                          "text": q.get("text"), "options": q.get("options") or [],
                          "default": q.get("default") if q.get("default") in keys else None,
                          "since": when(q.get("since")), "wait_min": mins_since(q.get("since"))})

    used = {s["on"] for p in procs for s in p["steps"]} | {f.get("on") for p in procs for f in p["flow"]}
    mviews = []
    for mid in MORDER:
        if mid not in used:
            continue
        runs = [(p, s) for p in procs for s in p["steps"] if s["on"] == mid and s["state"] in ACTIVE]
        load = list(dict.fromkeys(f"{s['who']} · {p['title']}" for p, s in runs)) or ["свободен" if mid == "pc" else "свободна"]
        if any(q.get("on") == mid for q in openq):
            now_ = {"state": "wait", "text": "ждёт вас"}
        elif runs:
            now_ = {"state": runs[0][1]["state"], "text": runs[0][1]["title"]}
        else:
            now_ = {"state": "idle", "text": "простаивает"}
        mviews.append({"id": mid, "state": "ok", "load": "; ".join(load), "now": now_, "orphans": 0, "cpu": None,
                       "mem": None, "disk_mb_s": None})

    def on_at(tid, ts):
        for s in (plans.get(tid) or {}).get("steps", []):
            a, b = P.parse(s.get("started_at")), P.parse(s.get("finished_at")) or now_dt
            if a and ts and a <= ts <= b and s.get("on") in P.VALID_ON:
                return [s["on"]]
        return ["pc"]

    def news_time(d):
        d = d.astimezone(P.TZ)
        return d.strftime("%H:%M") if d.date() == now_dt.date() else d.strftime("%d.%m %H:%M")

    feed = []
    ptit = {p["id"]: p["title"] for p in procs}
    for tid, plan in plans.items():
        if BOARD_TITLE_RE.search(plan.get("title") or ptitle(tid)):
            continue
        for st in plan["steps"]:
            fin = P.parse(st.get("finished_at"))
            det = str(st.get("detail") or "").strip()
            if st.get("state") != "done" or not fin or now - fin.timestamp() > FEED_AGE_S or len(FEED_NUM.findall(det)) != 1 or FEED_NOT.search(det) or FEED_NOT.search(st.get("title") or ""):
                continue
            feed.append((fin.timestamp(), {"time": news_time(fin), "state": "done", "on": on_at(tid, fin), "to": None,
                                           "text": f"{st.get('title', '')}: {det}", "_tid": tid, "_title": ptit.get(tid) or plan.get("title") or ptitle(tid)}))
    for q in allq:
        a = P.parse(q.get("since"))
        if a and now - a.timestamp() <= FEED_AGE_S:
            feed.append((a.timestamp(), {"time": news_time(a), "state": "wait", "on": [q.get("on", "pc")], "to": "you",
                                         "text": _clip(f"вопрос: {q['text']}", 70)}))
        b = P.parse(q.get("answered_at"))
        if b and now - b.timestamp() <= FEED_AGE_S:
            feed.append((b.timestamp(), {"time": news_time(b), "state": "done", "on": [q.get("on", "pc")], "to": None,
                                         "text": f"ответ владельца: {q.get('answer_label')}"}))
    for tid, t in tickets.items():
        if BOARD_TITLE_RE.search(ptitle(tid)):
            continue
        for e in (t.log or [])[-4:]:
            who = (e.author or "").lower()
            if not e.ts or who not in ROLE_LC or now - e.ts.timestamp() > FEED_AGE_S:
                continue
            txt = _clip(re.split(r"(?<=[.!?])\s", " ".join((e.text or "").split()), 1)[0], 70)
            feed.append((e.ts.timestamp(), {"time": news_time(e.ts), "state": "done", "on": ["pc"], "to": None,
                                            "text": f"{ROLE_LC[who]}: {txt}", "_tid": tid, "_title": ptitle(tid)}))
        if t.status == "done" and _t_updated(t, 0) and now - _t_updated(t, 0) <= FEED_AGE_S:
            ts = _t_updated(t, 0)
            feed.append((ts, {"time": news_time(P.datetime.fromtimestamp(ts, P.TZ)), "state": "done",
                              "on": ["pc"], "to": None, "text": "задача закрыта", "_tid": tid, "_title": ptitle(tid)}))
    feed.sort(key=lambda x: x[0])
    per, picked = {}, []
    for _, f in reversed(feed):
        k = f.get("_tid") or id(f)
        per[k] = per.get(k, 0) + 1
        if per[k] <= 2:
            picked.append(f)
    last = picked[:6][::-1]
    for d in last:
        tid, title = d.get("_tid"), d.pop("_title", None)
        if tid:
            d["text"] = f"{title} — {d['text']}"
    for d in last:
        d.pop("_tid", None)

    cnt = {k: 0 for k in COUNT_KEYS}
    for p in procs:
        for s in p["steps"]:
            cnt[s["state"]] = cnt.get(s["state"], 0) + 1
    if cnt["bad"]:
        head_ = {"state": "bad", "text": f"проблема: {cnt['bad']}"}
    elif questions:
        head_ = {"state": "wait", "text": f"ждёт вас: {len(questions)}"}
    else:
        head_ = {"state": "ok", "text": "всё идёт" if sum(cnt[k] for k in ACTIVE) else "сейчас ничего не идёт"}
    for p in procs:
        p.pop("has_plan", None)
    tags = {m: TAGS_ALL[m] for m in MORDER + ("you",) if m in used or m == "you"}
    return {"time": now_dt.strftime("%H:%M"), "built_ts": round(now, 1), "tick_s": 5, "headline": head_,
            "counters": cnt, "progress": progress, "tags": tags, "machines": mviews, "waves": waves, "processes": procs,
            "questions": questions, "feed": last}
