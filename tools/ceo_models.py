"""TK-068 (G-007): Opus vs Sonnet в роли CEO по транскриптам Claude Code.
Запуск: python tools/ceo_models.py --rpv <каталог> --alpha <каталог> [--out отчёт.md] [--sample выборка.tsv] [--seed 7]
Читает *.jsonl: интерактивные сессии CEO (хук «РОЛЬ СЕССИИ: ты — «CEO»», вход не sdk-cli, не побочные цепочки),
режет по смене модели, считает на ход: вес, инструменты, длину ответа, задержку, поправку владельца следующим сообщением."""
import argparse
import json
import random
import re
import statistics as st
from functools import lru_cache
from pathlib import Path

CEO_HDR = re.compile(r"РОЛЬ СЕССИИ: ты — «(?:Alpha - )?CEO»")
SYS_PREFIX = ("<task-notification", "<system-reminder", "<command-", "<local-command", "Caveat:", "[Request interrupted",
              "<user-prompt-submit-hook", "<bash-", "<ide_", "This session is being continued")
# поправка владельца: повтор/возмущение/требование критика; ключи по корням, регистр не важен
FIX_RE = re.compile(
    r"почему|зачем|не то\b|не так\b|неправильн|ошибк|я же (?:сказал|писал|просил)|опять|снова|ты что|что за|"
    r"бля|хуй|хуя|пизд|ёб|еб[аеу]н|заеб|нахуй|херн|хрен|говн|включай критика|критик|не понял|переделай|вернись|стоп\b|"
    r"ты охуел|ну и|не надо",
    re.I)
CRITIC_RE = re.compile(r"критик", re.I)
# нарушение устава, названное владельцем: задача без слова / не отдал роли / правка уставов
VIOL_RE = re.compile(r"не просил|не говорил|без (?:моего )?(?:слова|разрешения)|почему ты (?:сам|не дал|не завёл|не завел)|"
                     r"не дал (?:инженеру|исследователю)|сам (?:сделал|написал|правил)|устав", re.I)
# CEO сам признаёт и исправляет свою ошибку в ответе (без дозапроса владельца)
SELF_RE = re.compile(r"ошибся|моя ошибка|я ошибс|неверно (?:сказал|написал)|поправлюсь|исправляю (?:свою|себя)|"
                     r"было неточно|я неправильно|я не так", re.I)
INBOX_DONE_RE = re.compile(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d) (TK-\d+) \[done\]")


def weight(u):
    i, cw, cr, o = (u.get("input_tokens", 0), u.get("cache_creation_input_tokens", 0),
                    u.get("cache_read_input_tokens", 0), u.get("output_tokens", 0))
    return i + 1.25 * cw + 0.1 * cr + 5 * o


def ctx(u):
    return u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0) + u.get("cache_read_input_tokens", 0)


def family(model):
    m = (model or "").lower()
    for f in ("opus", "sonnet", "haiku", "fable"):
        if f in m:
            return f
    return "прочее"


def ts_of(d):
    from datetime import datetime
    return datetime.fromisoformat(d["timestamp"].replace("Z", "+00:00")).timestamp()


def user_text(d):
    """Текст сообщения владельца или None, если это результат инструмента / системная вставка."""
    if d.get("isMeta") or d.get("isSidechain"):
        return None
    c = d["message"]["content"]
    if isinstance(c, str):
        t = c
    else:
        if any(b.get("type") == "tool_result" for b in c):
            return None
        t = "\n".join(b.get("text", "") for b in c if b.get("type") == "text")
    t = t.strip()
    if not t or t.startswith(SYS_PREFIX):
        return None
    return t


def is_ceo_session(path):
    """(это интерактивный CEO, проект-метка входа). Вход sdk-cli — запуск диспетчера, не интерактив."""
    entry = None
    with open(path, encoding="utf-8", errors="replace") as f:
        for n, line in enumerate(f):
            if entry is None:
                m = re.search(r'"entrypoint":"([^"]+)"', line)
                entry = m.group(1) if m else None
            if entry == "sdk-cli" or (entry is None and n > 400):
                return False
            if entry and "РОЛЬ СЕССИИ" in line and CEO_HDR.search(line):
                return True
    return False


def read_turns(path):
    """Ходы сессии: от сообщения владельца до следующего. Сообщения ассистента — по id (блоки одного сообщения склеены)."""
    msgs, order = {}, []
    events = []  # ("u", ts, text) | ("a", id)
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            t = d.get("type")
            if t == "user" and "message" in d:
                tx = user_text(d)
                if tx is not None:
                    events.append(("u", ts_of(d), tx))
            elif t == "assistant" and not d.get("isSidechain") and "message" in d:
                m = d["message"]
                if m.get("model") in (None, "<synthetic>"):
                    continue
                e = msgs.get(m["id"])
                if e is None:
                    e = msgs[m["id"]] = {"model": m["model"], "usage": m.get("usage", {}), "ts": ts_of(d), "end": ts_of(d),
                                         "tools": 0, "chars": 0, "text": ""}
                    events.append(("a", m["id"]))
                e["end"] = ts_of(d)
                for b in m.get("content", []):
                    if b.get("type") == "tool_use":
                        e["tools"] += 1
                    elif b.get("type") == "text":
                        e["chars"] += len(b.get("text", ""))
                        e["text"] += b.get("text", "")
    turns, cur = [], None
    for ev in events:
        if ev[0] == "u":
            cur = {"user_ts": ev[1], "user": ev[2], "msgs": []}
            turns.append(cur)
        elif cur is not None:
            cur["msgs"].append(msgs[ev[1]])
    out = []
    for i, t in enumerate(turns):
        if not t["msgs"]:
            continue
        fams = {family(m["model"]) for m in t["msgs"]}
        out.append({
            "user": t["user"], "user_ts": t["user_ts"], "model": fams.pop() if len(fams) == 1 else "смешанный",
            "weight": sum(weight(m["usage"]) for m in t["msgs"]), "tools": sum(m["tools"] for m in t["msgs"]),
            "chars": sum(m["chars"] for m in t["msgs"]), "n_msgs": len(t["msgs"]),
            "first_latency": t["msgs"][0]["ts"] - t["user_ts"], "duration": t["msgs"][-1]["end"] - t["user_ts"],
            "ctx_last": ctx(t["msgs"][-1]["usage"]), "self_fix": bool(SELF_RE.search(" ".join(m["text"] for m in t["msgs"]))),
            "ctx_prev": ctx(turns[i - 1]["msgs"][-1]["usage"]) if i and turns[i - 1]["msgs"] else None, "next_user": turns[i + 1]["user"] if i + 1 < len(turns) else None,
        })
    return out


def segments(path):
    """Отрезки сессии с одной моделью: [(семейство, число сообщений, вес, рост контекста на сообщение)]."""
    seen, seq = set(), []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("type") != "assistant" or d.get("isSidechain") or "message" not in d:
                continue
            m = d["message"]
            if m.get("model") in (None, "<synthetic>") or m["id"] in seen:
                continue
            seen.add(m["id"])
            seq.append((family(m["model"]), weight(m.get("usage", {})), ctx(m.get("usage", {}))))
    out = []
    for fam, w, c in seq:
        if out and out[-1]["family"] == fam:
            out[-1]["n"] += 1
            out[-1]["weight"] += w
            out[-1]["ctx_end"] = c
        else:
            out.append({"family": fam, "n": 1, "weight": w, "ctx_start": c, "ctx_end": c})
    return out


@lru_cache(maxsize=None)
def _has(rx, text):
    return bool(text and rx.search(text))


def corrected(turn):
    return _has(FIX_RE, turn["next_user"])


def critic_asked(turn):
    """Следующее сообщение владельца требует критика (Г4: ошибка, исправляемая обращением к критику)."""
    return _has(CRITIC_RE, turn["next_user"])


def violated(turn):
    """Следующее сообщение владельца называет нарушение порядка (кодом по ключам; точность — по ручной выборке)."""
    return _has(VIOL_RE, turn["next_user"])


def ctx_growth(turn):
    """Прирост контекста за ход (токены); None, если предыдущего хода нет или контекст сбросили (сжатие/clear)."""
    if turn["ctx_prev"] is None or turn["ctx_last"] < turn["ctx_prev"]:
        return None
    return turn["ctx_last"] - turn["ctx_prev"]


def inbox_delays(path):
    """Задержки по строкам «<время> TK-N [done]» из ceo-inbox, прочитанным CEO (результат инструмента).
    [(семейство модели следующего ответа, строка→прочитано, с; прочитано→следующий ответ, с)]; строка считается один раз."""
    seen, recs, out = set(), [], []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("isSidechain") or "message" not in d:
                continue
            if d.get("type") == "assistant":
                m = d["message"]
                if m.get("model") not in (None, "<synthetic>"):
                    recs.append(("a", ts_of(d), family(m["model"]), None))
            elif d.get("type") == "user" and "[done]" in line:
                c = d["message"]["content"]
                body = c if isinstance(c, str) else " ".join(str(b.get("content", "")) for b in c if b.get("type") == "tool_result")
                new = [(m.group(1), m.group(2)) for m in INBOX_DONE_RE.finditer(body) if m.groups() not in seen]
                seen.update(new)
                if new:
                    recs.append(("r", ts_of(d), None, new))
    from datetime import datetime
    for i, r in enumerate(recs):
        if r[0] != "r":
            continue
        nxt = next((x for x in recs[i + 1:] if x[0] == "a"), None)
        if nxt is None:
            continue
        for stamp, _tk in r[3]:
            written = datetime.fromisoformat(stamp).timestamp()
            if 0 <= r[1] - written < 86400:
                out.append((nxt[2], r[1] - written, nxt[1] - r[1]))
    return out


def wilson(k, n, z=1.96):
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return (c - h, c + h)


def boot_diff(by_session_a, by_session_b, stat, iters=2000, seed=1):
    """Разница stat(A)-stat(B), 95% интервал бутстрепом по сессиям (ходы одной сессии зависимы)."""
    rnd = random.Random(seed)
    if not by_session_a or not by_session_b:
        return None
    vals = []
    for _ in range(iters):
        a = [x for s in rnd.choices(by_session_a, k=len(by_session_a)) for x in s]
        b = [x for s in rnd.choices(by_session_b, k=len(by_session_b)) for x in s]
        if a and b:
            vals.append(stat(a) - stat(b))
    vals.sort()
    return (vals[int(0.025 * len(vals))], vals[int(0.975 * len(vals))]) if vals else None


def collect(dirs):
    """{(проект, семейство): {сессия: [ходы]}}, список сессий CEO, число пропущенных."""
    data, sessions = {}, []
    for proj, d in dirs.items():
        for p in sorted(Path(d).glob("*.jsonl")):
            if not is_ceo_session(p):
                continue
            sessions.append((proj, p.name))
            for t in read_turns(p):
                data.setdefault((proj, t["model"]), {}).setdefault(p.name, []).append(t)
    return data, sessions


def summarize(turns):
    n = len(turns)
    k = sum(corrected(t) for t in turns if t["next_user"] is not None)
    nn = sum(1 for t in turns if t["next_user"] is not None)
    return {
        "n": n,
        "w_med": st.median(t["weight"] for t in turns), "w_mean": st.mean(t["weight"] for t in turns),
        "tools": st.mean(t["tools"] for t in turns), "chars_med": st.median(t["chars"] for t in turns),
        "lat_med": st.median(t["first_latency"] for t in turns), "dur_med": st.median(t["duration"] for t in turns),
        "fix": k, "fix_n": nn, "fix_ci": wilson(k, nn), "critic": sum(critic_asked(t) for t in turns),
        "ctx_med": st.median(t["ctx_last"] for t in turns),
        "growth_med": st.median(g) if (g := [x for x in map(ctx_growth, turns) if x is not None]) else 0,
        "viol": sum(violated(t) for t in turns if t["next_user"] is not None),
        "self": sum(t["self_fix"] for t in turns),
    }


def report(data, sessions):
    rows = []
    for (proj, fam), by_s in sorted(data.items()):
        turns = [t for ts in by_s.values() for t in ts]
        s = summarize(turns)
        rows.append((proj, fam, len(by_s), s))
    lines = ["| проект | модель | сессий | ходов | вес/ход мед. | вес/ход средн. | инстр./ход | ответ, знаков мед. | "
             "до 1-го ответа, с мед. | ход целиком, с мед. | контекст, ток. мед. | прирост контекста/ход мед. | поправок / ходов | доля [95% ДИ] | про критика | "
             "названо нарушение порядка | CEO сам признал ошибку |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for proj, fam, ns, s in rows:
        lines.append(f"| {proj} | {fam} | {ns} | {s['n']} | {s['w_med']:.0f} | {s['w_mean']:.0f} | {s['tools']:.1f} | "
                     f"{s['chars_med']:.0f} | {s['lat_med']:.0f} | {s['dur_med']:.0f} | {s['ctx_med']:.0f} | {s['growth_med']:.0f} | "
                     f"{s['fix']}/{s['fix_n']} | {s['fix']/max(s['fix_n'],1):.1%} [{s['fix_ci'][0]:.1%}–{s['fix_ci'][1]:.1%}] | {s['critic']} | "
                     f"{s['viol']} | {s['self']} |")
    lines.append("")
    lines.append("Разность Opus − Sonnet (95% ДИ бутстрепом по сессиям):")
    for proj in sorted({p for p, _ in data}):
        a, b = data.get((proj, "opus"), {}), data.get((proj, "sonnet"), {})
        for name, stat in (("вес/ход, медиана", lambda xs: st.median(t["weight"] for t in xs)),
                           ("инструментов/ход", lambda xs: st.mean(t["tools"] for t in xs)),
                           ("ответ, знаков (медиана)", lambda xs: st.median(t["chars"] for t in xs)),
                           ("до 1-го ответа, с (медиана)", lambda xs: st.median(t["first_latency"] for t in xs)),
                           ("ход целиком, с (медиана)", lambda xs: st.median(t["duration"] for t in xs)),
                           ("доля поправок", lambda xs: sum(corrected(t) for t in xs) / len(xs)),
                           ("доля «включай критика»", lambda xs: sum(critic_asked(t) for t in xs) / len(xs)),
                           ("прирост контекста/ход, медиана", lambda xs: st.median([g for g in map(ctx_growth, xs) if g is not None] or [0])),
                           ("доля «названо нарушение порядка»", lambda xs: sum(violated(t) for t in xs) / len(xs)),
                           ("доля «сам признал ошибку»", lambda xs: sum(t["self_fix"] for t in xs) / len(xs))):
            ci = boot_diff(list(a.values()), list(b.values()), stat)
            lines.append(f"- {proj} · {name}: " + (f"[{ci[0]:.3g}; {ci[1]:.3g}]" + (" — ноль внутри (шум)" if ci[0] <= 0 <= ci[1] else " — ноль вне интервала")
                                                    if ci else "нет данных у одной из моделей"))
    return "\n".join(lines)


SECRET_RE = re.compile(r"\d{8,}:[A-Za-z0-9_-]{30,}|sk-[A-Za-z0-9_-]{20,}|gh[pous]_[A-Za-z0-9]{30,}")


def mask(text):
    """Заменяет похожее на токены (бот Telegram, sk-…, ghp_…) — выборка уходит в публичный репозиторий."""
    return SECRET_RE.sub("<токен скрыт>", text)


def sample(data, n, seed, fam_list=("opus", "sonnet")):
    rnd = random.Random(seed)
    rows = []
    for fam in fam_list:
        pool = [(proj, sid, t) for (proj, f), by_s in data.items() if f == fam for sid, ts in by_s.items() for t in ts
                if t["next_user"] is not None]
        flagged = [x for x in pool if corrected(x[2])]
        free = [x for x in pool if not corrected(x[2])]
        pick = rnd.sample(flagged, min(n // 2, len(flagged))) + rnd.sample(free, min(n - n // 2, len(free)))
        for proj, sid, t in pick:
            rows.append((fam, proj, sid[:8], int(corrected(t)), mask(t["user"])[:140].replace("\n", " "),
                         mask(t["next_user"] or "")[:200].replace("\n", " ")))
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--rpv", required=True)
    ap.add_argument("--alpha", required=True)
    ap.add_argument("--out")
    ap.add_argument("--sample")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--segments", action="store_true", help="добавить таблицу отрезков по сменам модели")
    a = ap.parse_args(argv)
    data, sessions = collect({"rpv": a.rpv, "alpha": a.alpha})
    txt = f"Интерактивных сессий CEO: {len(sessions)} (rpv {sum(p == 'rpv' for p, _ in sessions)}, alpha {sum(p == 'alpha' for p, _ in sessions)})\n\n" + report(data, sessions)
    if a.segments:
        segs = {}
        for proj, name in sessions:
            d = a.rpv if proj == "rpv" else a.alpha
            for s in segments(Path(d) / name):
                segs.setdefault((proj, s["family"]), []).append(s)
        txt += "\n\nОтрезки с одной моделью (проект, модель: отрезков, сообщений, вес/сообщение мед.):\n" + "\n".join(
            f"- {p} · {f}: {len(v)}, {sum(s['n'] for s in v)}, {st.median(s['weight'] / s['n'] for s in v):.0f}"
            for (p, f), v in sorted(segs.items()))
    delays = {}
    for proj, name in sessions:
        for fam, w, r in inbox_delays(Path(a.rpv if proj == "rpv" else a.alpha) / name):
            delays.setdefault((proj, fam), []).append((w, r))
    txt += ("\n\nЗадержка по [done] из ceo-inbox (проект, модель: n, строка→прочитано мед. с, прочитано→следующий ответ мед. с):\n"
            + "\n".join(
        f"- {p} · {f}: {len(v)}, {st.median(x[0] for x in v):.0f}, {st.median(x[1] for x in v):.1f}" for (p, f), v in sorted(delays.items())))
    print(txt)
    if a.out:
        Path(a.out).write_text(txt, encoding="utf-8")
    if a.sample:
        Path(a.sample).write_text("\n".join("\t".join(map(str, r)) for r in sample(data, 20, a.seed)), encoding="utf-8")


if __name__ == "__main__":
    main()
