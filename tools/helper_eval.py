#!/usr/bin/env python3
"""Замер помощников CEO (`agents/secretary.md`, `inbox.md`, `reporter.md`) на разных моделях: один и тот же набор
случаев, механические проверки, цена готовой задачи. Только stdlib.

    python tools/helper_eval.py run --data <каталог набора> --out <каталог итогов> \\
        [--models claude-haiku-5-5,claude-sonnet-5-5] [--effort xhigh] [--tasks secretary,inbox,reporter] \\
        [--only s-611,r-666] [--jobs 6] [--budget 1.0]
    python tools/helper_eval.py regrade --data <каталог набора> --out <каталог итогов>  # эталон поправлен — без запусков
    python tools/helper_eval.py report --out <каталог итогов>
    python tools/helper_eval.py pair --a <итоги A> --b <итоги B> [--model …]  # две Haiku: совпали — берём, иначе к CEO

Набор (`--data`) — `cases.jsonl` и `fixtures/<имя>/` (проект-заготовка: `.claude/tickets`, `.claude/decisions.md`…).
Набор собирается из данных проекта и в репозиторий не кладётся. Случай — строка JSON: `id`, `task` (имя помощника),
`fixture`, `now` и поля задачи (см. `PROMPTS`) с эталоном (`gold_*`). Запуск — `claude -p --agent <помощник>` в копии
заготовки: определение помощника берётся из `agents/<помощник>.md`, модель подменяется на испытуемую.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLAUDE = shutil.which("claude") or "claude"
WRITERS = {"secretary"}  # помощники, которые пишут в проект: каждому запуску — своя копия заготовки

PROMPTS = {
    "secretary": lambda c, w: (f"Сейчас: {c['now']} (GMT+4). Команда реестра: `{registry_cmd(w)}`. "
                               f"Владелец написал (дословно, между линиями):\n---\n{c['text']}"
                               f"\n---\nПеред этим CEO писал владельцу (контекст, не слова владельца):\n---\n{c['prev']}"
                               "\n---\nОформи по своему уставу."),
    "inbox": lambda c, w: (f"Сейчас: {c['now']} (GMT+4). Разбери строки {c['lines'][0]}–{c['lines'][1]} файла "
                        ".claude/dispatcher/ceo-inbox.md."),
    "reporter": lambda c, w: (f"Сейчас: {c['now']} (GMT+4). Вопрос владельца (дословно): «{c['question']}». "
                           "Собери ответ по своему уставу."),
}


def registry_cmd(work: Path) -> str:
    """Команда реестра для проекта-копии — как её даёт CEO хук role_context (python, путь плагина, --project)."""
    exe = "python" if shutil.which("python") else sys.executable
    return f'{exe} "{(ROOT / ".claude" / "dispatcher" / "registry.py").as_posix()}" --project "{work.as_posix()}"'


# ------------------------------------------------------------------------------------------------------------
# Определение помощника
# ------------------------------------------------------------------------------------------------------------

def load_agent(name: str) -> dict:
    """agents/<name>.md → {description, prompt, tools, model, effort} (фронтматтер YAML-подобный, плоский)."""
    text = (ROOT / "agents" / f"{name}.md").read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    if not m:
        raise SystemExit(f"agents/{name}.md: нет фронтматтера")
    meta = {}
    for ln in m.group(1).splitlines():
        k, _, v = ln.partition(":")
        meta[k.strip()] = v.strip().strip('"')
    out = {"description": meta.get("description", name), "prompt": m.group(2).strip()}
    if meta.get("tools"):
        out["tools"] = [t.strip() for t in meta["tools"].split(",") if t.strip()]
    for k in ("model", "effort"):
        if meta.get(k):
            out[k] = meta[k]
    return out


# ------------------------------------------------------------------------------------------------------------
# Проверки
# ------------------------------------------------------------------------------------------------------------

def norm(s: str) -> str:
    return " ".join((s or "").replace("ё", "е").split())


_NUM = re.compile(r"(?<![A-Za-zА-Яа-яЁё_-])\d+(?:[.,]\d+)?")
_ID = re.compile(r"\b(?:TK|G|В|Г|П)-\d+\b")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?(?:[+-]\d{2}:\d{2})?)?|\b\d{1,2}:\d{2}\b|\b\d{2}\.\d{2}(?:\.\d{2,4})?\b")


def numbers(s: str) -> list[str]:
    return _NUM.findall(_DATE.sub(" ", _ID.sub(" ", s or "")))


def quote_pieces(q: str) -> list[str]:
    """Куски цитаты, каждый обязан быть в словах владельца: «…» по отдельности, разрывы «…»/«...», перенос как « / »."""
    q = norm(q)
    parts = re.findall(r"«([^»]+)»", q) or [q.strip("«»\"„“ ")]
    out = []
    for part in parts:
        out += [p.strip(" \"„“") for p in re.split(r"…|\.\.\.| / ", part) if len(p.strip(" \"„“")) >= 3]
    return out


def answer_fields(text: str) -> dict:
    f = {}
    for ln in (text or "").splitlines():
        m = re.match(r"^\s*[`*\-]*\s*(тип|запись|цитата|суть|связано)\s*[:：]\s*(.*?)\s*`*$", ln, re.I)
        if m and m.group(1).lower() not in f:
            f[m.group(1).lower()] = m.group(2)
    return f


def grade_secretary(c: dict, ans: str, work: Path, fixture: Path) -> dict:
    f = answer_fields(ans)
    t = (f.get("тип") or "").lower()
    types = sorted({k for k in ("решение", "цель") if k in t})
    seeds = (fixture / ".claude" / "decisions.md").read_text(encoding="utf-8")
    dec_p = work / ".claude" / "decisions.md"
    dec = dec_p.read_text(encoding="utf-8") if dec_p.exists() else ""
    new_dec = dec[len(seeds):] if dec.startswith(seeds) else dec  # переписанный целиком реестр — всё «новое»
    seeds_intact = dec.startswith(seeds)
    old_goals = {p.name for p in (fixture / ".claude" / "goals").glob("*.md")}
    goals_dir = work / ".claude" / "goals"
    new_goals = [p for p in sorted(goals_dir.glob("*.md")) if p.name not in old_goals] if goals_dir.exists() else []
    new_blocks = re.findall(r"^## В-(\d+)", new_dec, re.M)
    written = sorted(({"решение"} if new_blocks else set()) | ({"цель"} if new_goals else set()))
    records = new_dec + "".join(p.read_text(encoding="utf-8") for p in new_goals)
    quotes = [ln.lstrip("> ").strip() for ln in records.splitlines() if ln.startswith(">")]
    if (f.get("цитата") or "").strip() not in ("", "—", "-"):
        quotes.append(f["цитата"])
    owner = norm(c["text"]).replace(" / ", " ")
    bad_quotes = [q for q in quotes for p in quote_pieces(q) if p not in owner]
    body = "\n".join(ln for ln in records.splitlines()
                     if not re.match(r"^(## В-|id:|set:|status:|accepted:|---)", ln))
    allowed = c["text"] + "\n" + c["prev"] + "\n" + seeds
    invented = [n for n in numbers(body) if n not in allowed]
    rel = c.get("gold_related")
    rel_hit = None if not rel else bool(re.search(rf"\b{re.escape(rel)}\b", (f.get("связано") or "") + "\n" + new_dec))
    seed_max = max((int(n) for n in re.findall(r"^## В-(\d+)", seeds, re.M)), default=0)
    goal_max = max((int(n[2:5]) for n in old_goals if re.match(r"G-\d{3}", n)), default=0)
    numbering = ([int(b) for b in new_blocks] == list(range(seed_max + 1, seed_max + 1 + len(new_blocks)))
                 and [p.name for p in new_goals] == [f"G-{goal_max + i + 1:03d}.md" for i in range(len(new_goals))])
    return {
        "type_ok": types in [sorted(g) for g in c["gold_types"]],
        "types": types,
        "files_match_answer": written == types,
        "quotes_verbatim": not bad_quotes,
        "bad_quotes": bad_quotes[:3],
        "no_invented_numbers": not invented,
        "invented": invented[:5],
        "related_hit": rel_hit,
        "numbering_ok": numbering,
        "registry_intact": seeds_intact,
        "format_ok": all(k in f for k in ("тип", "запись", "цитата", "суть", "связано")),
    }


def _ids(s: str) -> set[str]:
    return set(re.findall(r"TK-\d+", s or ""))


def _section(ans: str, head: str, stop: tuple) -> str:
    out, on = [], False
    for ln in ans.splitlines():
        low = ln.strip().lstrip("*#- ").lower()
        if low.startswith(head):
            on = True
            out.append(ln.split(":", 1)[1] if ":" in ln else "")
            continue
        if on and any(low.startswith(s) for s in stop):
            break
        if on:
            out.append(ln)
    return "\n".join(out)


def grade_inbox(c: dict, ans: str, work: Path, fixture: Path) -> dict:
    stops = ("закрыто", "идёт", "идет", "механика", "неактуально", "к сведению")
    act = _ids(_section(ans, "требует действия", stops))
    done = _ids(_section(ans, "закрыто", ("идёт", "идет", "механика", "неактуально", "требует")))
    done |= _ids(_section(ans, "неактуально", ("закрыто", "идёт", "идет", "механика", "требует")))  # done со старым сигналом
    gold_a, gold_d = set(c["gold_action"]), set(c["gold_done"])
    invented = sorted(_ids(ans) - set(c["gold_ids"]))
    return {
        "action_exact": act == gold_a,
        "action_missed": sorted(gold_a - act),
        "action_extra": sorted(act - gold_a),
        "done_recall": round(len(done & gold_d) / len(gold_d), 2) if gold_d else None,
        "no_invented_ids": not invented,
        "invented": invented[:5],
        "lines_ok": len([ln for ln in ans.splitlines() if ln.strip()]) <= 15,
    }


def grade_reporter(c: dict, ans: str, work: Path, fixture: Path) -> dict:
    tdir = fixture / ".claude" / "tickets"

    def ticket_text(tid):
        p = tdir / f"{tid}.md"
        a = tdir / "archive" / f"{tid}-log.md"
        return (p.read_text(encoding="utf-8") if p.exists() else "") + (a.read_text(encoding="utf-8") if a.exists() else "")

    lines = [ln for ln in ans.splitlines() if ln.strip()]
    cited = _ids(ans)
    missing = sorted(t for t in cited if not (tdir / f"{t}.md").exists())
    nums, unsupported = 0, []
    for ln in lines:
        ids = _ids(ln)
        src = "".join(ticket_text(t) for t in ids)
        for n in numbers(ln):
            nums += 1
            if not ids or n not in src:
                unsupported.append(n)
    flagged = [t for t in c["gold_flag"] if t in cited]
    return {
        "flags_recall": round(len(flagged) / len(c["gold_flag"]), 2),
        "numbers": nums,
        "numbers_sourced": round(1 - len(unsupported) / nums, 2) if nums else 1.0,
        "unsupported": unsupported[:6],
        "no_missing_ids": not missing,
        "lines_ok": len(lines) <= 15,
    }


GRADERS = {"secretary": grade_secretary, "inbox": grade_inbox, "reporter": grade_reporter}


# ------------------------------------------------------------------------------------------------------------
# Прогон
# ------------------------------------------------------------------------------------------------------------

_LOCK = threading.Lock()


def work_dir(c: dict, model: str, out: Path) -> Path:
    slug = model.replace("claude-", "")
    return out / "work" / slug / (c["id"] if c["task"] in WRITERS else f"_{c['fixture']}")


def run_case(c: dict, model: str, effort: str, data: Path, out: Path, budget: float) -> dict:
    task, slug = c["task"], model.replace("claude-", "")
    fixture = data / "fixtures" / c["fixture"]
    if task in WRITERS:
        work = work_dir(c, model, out)
        if work.exists():
            shutil.rmtree(work)
        shutil.copytree(fixture, work)
    else:
        work = work_dir(c, model, out)
        with _LOCK:
            if not work.exists():
                shutil.copytree(fixture, work)
    agents = out / f"agents-{slug}.json"
    with _LOCK:
        if not agents.exists():
            defs = {}
            for name in PROMPTS:
                d = load_agent(name)
                d["model"], d["effort"] = model, effort
                defs[name] = d
            agents.write_text(json.dumps(defs, ensure_ascii=False), encoding="utf-8")
    tools = load_agent(task).get("tools", [])
    cmd = [CLAUDE, "-p", "--agents", str(agents), "--agent", task, "--model", model, "--effort", effort,
           "--output-format", "json", "--no-session-persistence", "--max-budget-usd", str(budget),
           # копия заготовки во временном каталоге; `.claude/` Claude Code без полного режима не даёт писать молча
           "--permission-mode", "bypassPermissions", "--allowedTools", ",".join(tools)]
    t0 = time.time()
    try:
        r = subprocess.run(cmd, input=PROMPTS[task](c, work), capture_output=True, text=True, encoding="utf-8",
                           cwd=str(work), timeout=900)
        raw = r.stdout
    except subprocess.TimeoutExpired:
        raw = ""
    dur = round(time.time() - t0, 1)
    try:
        res = json.loads(raw)
    except ValueError:
        res = {"is_error": True, "result": raw[-2000:]}
    ans = res.get("result") or ""
    row = {"id": c["id"], "task": task, "model": model, "effort": effort, "cost": res.get("total_cost_usd"),
           "turns": res.get("num_turns"), "dur": dur, "error": bool(res.get("is_error")) or not ans,
           "answer": ans}
    try:
        row["grade"] = GRADERS[task](c, ans, work, fixture)
    except Exception as e:  # проверка не должна ронять прогон
        row["grade"] = {"grader_error": f"{type(e).__name__}: {e}"}
    with _LOCK:
        with (out / "results.jsonl").open("a", encoding="utf-8") as w:
            w.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"{c['id']:<14} {slug:<12} ${row['cost'] or 0:.4f} {dur:>6}s {'ОШИБКА' if row['error'] else ''}", flush=True)
    return row


def cmd_regrade(a) -> int:
    """Проверки заново по сохранённым ответам и рабочим копиям: эталон или проверка поправлены — запуски не нужны."""
    data, out = Path(a.data).resolve(), Path(a.out).resolve()
    cases = {c["id"]: c for c in (json.loads(l) for l in (data / "cases.jsonl").open(encoding="utf-8") if l.strip())}
    rows = {}
    for l in (out / "results.jsonl").open(encoding="utf-8"):
        r = json.loads(l)
        rows[(r["id"], r["model"])] = r
    with (out / "results.jsonl").open("w", encoding="utf-8") as w:
        for r in rows.values():
            c = cases.get(r["id"])
            if c:
                try:
                    r["grade"] = GRADERS[c["task"]](c, r["answer"], work_dir(c, r["model"], out),
                                                    data / "fixtures" / c["fixture"])
                except Exception as e:
                    r["grade"] = {"grader_error": f"{type(e).__name__}: {e}"}
            w.write(json.dumps(r, ensure_ascii=False) + "\n")
    return cmd_report(a)


def _load(out: Path, model: str) -> dict:
    rows = {}
    for l in (out / "results.jsonl").open(encoding="utf-8"):
        r = json.loads(l)
        if r["model"] == model:
            rows[r["id"]] = r
    return rows


def _pair_key(r: dict):
    """Что сверяют два независимых прогона: secretary — типы записи, inbox — тикеты «требует действия»."""
    if r["task"] == "secretary":
        return tuple(r["grade"].get("types") or ())
    if r["task"] == "inbox":
        return tuple(sorted(_ids(_section(r["answer"], "требует действия",
                                          ("закрыто", "идёт", "идет", "механика", "неактуально", "к сведению")))))
    return None


def _pair_ok(r: dict):
    g = r["grade"]
    return g.get("type_ok") if r["task"] == "secretary" else g.get("action_exact")


def cmd_pair(a) -> int:
    """Две модели (или два прогона одной) независимо: совпали — ответ берётся, разошлись — к CEO. Сколько расходятся,
    верно ли там, где совпали, и сколько ошибок одиночного прогона пара отдала бы CEO."""
    ra, rb = _load(Path(a.a).resolve(), a.model), _load(Path(a.b).resolve(), a.model_b or a.model)
    md = ["# Пара прогонов", "", f"A — `{a.a}`, B — `{a.b}`", "",
          "| помощник | случаев | совпали | верно из совпавших | к CEO | ошибок A | из них к CEO | цена пары, $ на случай |",
          "|---|---|---|---|---|---|---|---|"]
    for task in ("secretary", "inbox"):
        ids = sorted(i for i in ra if i in rb and ra[i]["task"] == task and not ra[i]["error"] and not rb[i]["error"])
        if not ids:
            continue
        same = [i for i in ids if _pair_key(ra[i]) == _pair_key(rb[i])]
        right = [i for i in same if _pair_ok(ra[i])]
        wrong_a = [i for i in ids if not _pair_ok(ra[i])]
        caught = [i for i in wrong_a if i not in same]
        cost = sum((ra[i]["cost"] or 0) + (rb[i]["cost"] or 0) for i in ids) / len(ids)
        md.append(f"| {task} | {len(ids)} | {len(same)} | {len(right)}/{len(same)} | {len(ids) - len(same)} | "
                  f"{len(wrong_a)} | {len(caught)} | {cost:.4f} |")
        md += [f"  - разошлись: `{i}` A={_pair_key(ra[i])} B={_pair_key(rb[i])}" for i in ids if i not in same]
        md += [f"  - совпали, но мимо эталона: `{i}` {_pair_key(ra[i])}" for i in same if not _pair_ok(ra[i])]
    sys.stdout.reconfigure(encoding="utf-8")
    print("\n".join(md))
    return 0


def cmd_run(a) -> int:
    # resolve(): короткое имя Windows (`ABCD~1`) Claude Code считает подозрительным и не даёт писать без вопроса
    data, out = Path(a.data).resolve(), Path(a.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    cases = [json.loads(l) for l in (data / "cases.jsonl").open(encoding="utf-8") if l.strip()]
    tasks = set(a.tasks.split(","))
    only = set(a.only.split(",")) if a.only else None
    cases = [c for c in cases if c["task"] in tasks and (only is None or c["id"] in only)]
    done = set()
    if (out / "results.jsonl").exists():  # продолжение: готовые пары (случай, модель) не повторяем
        for l in (out / "results.jsonl").open(encoding="utf-8"):
            r = json.loads(l)
            if not r.get("error"):
                done.add((r["id"], r["model"]))
    jobs = [(c, m) for m in a.models.split(",") for c in cases if (c["id"], m) not in done]
    print(f"запусков: {len(jobs)} (готово раньше: {len(done)})", flush=True)
    with ThreadPoolExecutor(max_workers=a.jobs) as ex:
        list(ex.map(lambda cm: run_case(cm[0], cm[1], a.effort, data, out, a.budget), jobs))
    return cmd_report(a)


# ------------------------------------------------------------------------------------------------------------
# Сводка
# ------------------------------------------------------------------------------------------------------------

METRICS = {
    "secretary": ["type_ok", "files_match_answer", "quotes_verbatim", "no_invented_numbers", "related_hit",
                  "numbering_ok", "registry_intact", "format_ok"],
    "inbox": ["action_exact", "done_recall", "no_invented_ids", "lines_ok"],
    "reporter": ["flags_recall", "numbers_sourced", "no_missing_ids", "lines_ok"],
}


def cmd_report(a) -> int:
    out = Path(a.out)
    rows = {}
    for l in (out / "results.jsonl").open(encoding="utf-8"):
        r = json.loads(l)
        rows[(r["id"], r["model"])] = r  # последний запуск пары
    rows = list(rows.values())
    models = sorted({r["model"] for r in rows})
    md = ["# Замер помощников CEO", ""]
    for task, mets in METRICS.items():
        tr = [r for r in rows if r["task"] == task]
        if not tr:
            continue
        md += [f"## {task}", "", "| метрика | " + " | ".join(models) + " |", "|---|" + "---|" * len(models)]
        for met in ["случаев", "ошибок запуска"] + mets + ["цена, $ всего", "цена, $ на случай", "время, с на случай"]:
            cells = []
            for m in models:
                mr = [r for r in tr if r["model"] == m]
                ok = [r for r in mr if not r["error"]]
                if met == "случаев":
                    v = str(len(mr))
                elif met == "ошибок запуска":
                    v = str(len(mr) - len(ok))
                elif met.startswith("цена, $ всего"):
                    v = f"{sum(r['cost'] or 0 for r in mr):.3f}"
                elif met.startswith("цена, $ на"):
                    v = f"{sum(r['cost'] or 0 for r in mr) / max(len(mr), 1):.4f}"
                elif met.startswith("время"):
                    v = f"{sum(r['dur'] for r in mr) / max(len(mr), 1):.0f}"
                else:
                    vals = [r["grade"].get(met) for r in ok if r["grade"].get(met) is not None]
                    v = "—" if not vals else (f"{sum(vals) / len(vals):.2f}" if isinstance(vals[0], float)
                                              else f"{sum(bool(x) for x in vals)}/{len(vals)}")
                cells.append(v)
            md.append(f"| {met} | " + " | ".join(cells) + " |")
        md.append("")
    md += ["## Промахи", ""]
    for r in sorted(rows, key=lambda r: (r["task"], r["id"], r["model"])):
        g = r["grade"]
        bad = {k: v for k, v in g.items() if v is False or (isinstance(v, list) and v and k not in ("types",))}
        if r["error"] or bad:
            md.append(f"- `{r['id']}` {r['model'].replace('claude-', '')}: "
                      + ("ошибка запуска; " if r["error"] else "") + json.dumps(bad, ensure_ascii=False))
    (out / "summary.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    print("\n".join(md))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--data", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--models", default="claude-haiku-5-5,claude-sonnet-5-5")
    r.add_argument("--effort", default="xhigh")
    r.add_argument("--tasks", default=",".join(PROMPTS))
    r.add_argument("--only", default="")
    r.add_argument("--jobs", type=int, default=6)
    r.add_argument("--budget", type=float, default=1.0)
    g = sub.add_parser("regrade")
    g.add_argument("--data", required=True)
    g.add_argument("--out", required=True)
    p = sub.add_parser("report")
    p.add_argument("--out", required=True)
    pr = sub.add_parser("pair", help="два независимых прогона: совпали — берём, разошлись — к CEO")
    pr.add_argument("--a", required=True, help="каталог итогов прогона A")
    pr.add_argument("--b", required=True, help="каталог итогов прогона B")
    pr.add_argument("--model", default="claude-haiku-5-5")
    pr.add_argument("--model-b", dest="model_b", default=None, help="модель прогона B (по умолчанию та же)")
    a = ap.parse_args(argv)
    return {"run": cmd_run, "regrade": cmd_regrade, "report": cmd_report, "pair": cmd_pair}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
