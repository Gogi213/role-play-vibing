#!/usr/bin/env python3
"""A/B Судьи: можно ли удешевить проверяющего (модель и усилие), не пропустив плохую работу. ТРАТИТ ДЕНЬГИ.

Сдачу Инженера не запускаем (она недетерминирована и стоит денег): проект готовится сразу в состоянии «Инженер сдал» — работа
закоммичена, тикет в `in_review`. Часть сдач верная, часть с подсаженным дефектом (контракт тикета нарушен, а собственные тесты
Инженера зелёные). Один и тот же набор сдач проходит каждый вариант Судьи; настоящий диспетчер будит Судью, берётся его первый
вердикт и сразу останавливается — переделку Инженером не запускаем.

    python tools/bench/judge_ab.py --variants opus-xhigh,opus-high,sonnet-xhigh --repeat 1 --budget-usd 10

Тикеты без поля `effort:` — иначе оно перекрывает усилие Судьи (ставится ВСЕМ ролям тикета). Варианты:
  opus-xhigh   — умолчание плагина (тикет без effort)      opus-high / opus-medium — то же с меньшим усилием
  sonnet-xhigh / sonnet-high — Судья на Sonnet 5.5"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402
import e2e as E  # noqa: E402
import tasks as TK  # noqa: E402

BENCH = C.ROOT / "tools" / "bench"
VARIANTS = {
    "opus-xhigh": ("claude-opus-5-5", "xhigh"),
    "opus-high": ("claude-opus-5-5", "high"),
    "opus-medium": ("claude-opus-5-5", "medium"),
    "sonnet-xhigh": ("claude-sonnet-5-5", "xhigh"),
    "sonnet-high": ("claude-sonnet-5-5", "high"),
}

# --- сдачи ----------------------------------------------------------------------------------------

FIZZ_OK = textwrap.dedent('''\
    def fizzbuzz(n):
        out = []
        for i in range(1, n + 1):
            out.append("FizzBuzz" if i % 15 == 0 else "Fizz" if i % 3 == 0 else "Buzz" if i % 5 == 0 else str(i))
        return out
''')
FIZZ_TESTS_OK = textwrap.dedent('''\
    import unittest
    from fizzbuzz import fizzbuzz


    class T(unittest.TestCase):
        def test_small(self):
            self.assertEqual(fizzbuzz(5), ["1", "2", "Fizz", "4", "Buzz"])

        def test_fifteen(self):
            self.assertEqual(fizzbuzz(15)[-1], "FizzBuzz")

        def test_zero(self):
            self.assertEqual(fizzbuzz(0), [])

        def test_len(self):
            self.assertEqual(len(fizzbuzz(30)), 30)
''')
# дефект D1: числа остались числами, а не строками; тесты написаны под неверное поведение и зелёные
FIZZ_INTS = FIZZ_OK.replace("else str(i))", "else i)")
FIZZ_TESTS_INTS = FIZZ_TESTS_OK.replace('["1", "2", "Fizz", "4", "Buzz"]', '[1, 2, "Fizz", 4, "Buzz"]')

TOKENS = textwrap.dedent('''\
    import re


    def words(text):
        return re.findall(r"[^\\W_]+", text.lower())
''')
TOKENS_TESTS = textwrap.dedent('''\
    import unittest
    from tokens import words


    class T(unittest.TestCase):
        def test_case(self):
            self.assertEqual(words("А б"), ["а", "б"])

        def test_empty(self):
            self.assertEqual(words(""), [])

        def test_digits(self):
            self.assertEqual(words("a1,b2"), ["a1", "b2"])
''')
WORDSTAT_OK = textwrap.dedent('''\
    import sys
    from collections import Counter
    from tokens import words

    path, top = sys.argv[1], int(sys.argv[sys.argv.index("--top") + 1])
    counts = Counter(words(open(path, encoding="utf-8").read()))
    for word, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:top]:
        print(f"{word}\\t{n}")
''')
# дефект D2: при равных числах слова идут в порядке появления, а не по алфавиту; собственные тесты связей не проверяют
WORDSTAT_TIES = WORDSTAT_OK.replace("sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:top]", "counts.most_common(top)")

MEDIAN_FIXED = TK.STATS_BUGGY.replace("    return s[len(s) // 2]", textwrap.indent(textwrap.dedent('''\
    if not s:
        raise ValueError("пустой список")
    m = len(s) // 2
    return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2'''), "    "))
MEDIAN_TEST = textwrap.dedent('''\
    import unittest
    from stats_util import median


    class MedianEven(unittest.TestCase):
        def test_even(self):
            self.assertEqual(median([1, 2, 3, 4]), 2.5)
            self.assertEqual(median([10, 0]), 5.0)

        def test_odd_and_single(self):
            self.assertEqual(median([3, 1, 2]), 2)
            self.assertEqual(median([5]), 5)

        def test_empty(self):
            with self.assertRaises(ValueError):
                median([])
''')


def commit(proj: Path, msg: str) -> str:
    C.sh(["git", "add", "-A"], proj)
    C.sh(["git"] + C.GIT_ID + ["commit", "-qm", msg], proj)
    return C.sh(["git", "rev-parse", "--short", "HEAD"], proj).stdout.strip()


def write(proj: Path, files: dict) -> None:
    for name, text in files.items():
        (proj / name).write_text(text, encoding="utf-8")


@dataclass
class Case:
    id: str
    task: TK.Task
    expect: str                # accept | return — что обязан вынести Судья по контракту тикета
    defect: str                # что не так (пусто у верных)
    build: object              # build(proj, eng_env, tid) — работа Инженера и его `result done`


def _done(proj, env, tid, path, why):
    r = C.cli("tickets.py", proj, "result", tid, "done", "--path", path, "--why", why, env=env)
    if r.returncode:
        raise RuntimeError(f"result done: {r.stderr[-200:]}")


def b_hello(committed):
    def build(proj, env, tid):
        write(proj, {"hello.py": "print('привет')\n"})
        if committed:
            commit(proj, "hello.py")
        _done(proj, env, tid, "hello.py", "hello.py печатает «привет»" + (", закоммичен" if committed else ""))
    return build


def b_fizz(ints):
    def build(proj, env, tid):
        write(proj, {"fizzbuzz.py": FIZZ_INTS if ints else FIZZ_OK, "test_fizzbuzz.py": FIZZ_TESTS_INTS if ints else FIZZ_TESTS_OK})
        commit(proj, "fizzbuzz с тестами")
        _done(proj, env, tid, "fizzbuzz.py", "fizzbuzz.py и test_fizzbuzz.py закоммичены, 4 теста зелёные")
    return build


def b_wordstat(ties):
    def build(proj, env, tid):
        write(proj, {"wordstat.py": WORDSTAT_TIES if ties else WORDSTAT_OK, "tokens.py": TOKENS, "test_tokens.py": TOKENS_TESTS})
        commit(proj, "wordstat, tokens, тесты")
        _done(proj, env, tid, "wordstat.py", "wordstat.py, tokens.py и 3 теста закоммичены, unittest зелёный")
    return build


def b_bugfix(registered):
    def build(proj, env, tid):
        write(proj, {"test_median_even.py": MEDIAN_TEST})
        before = commit(proj, "тест: median чётной длины (падает)")
        if registered:
            r = C.cli("errors.py", proj, "add", "--title", "median неверна для чётной длины", "--ticket", tid, env=env)
            if r.returncode:
                raise RuntimeError(f"errors add: {r.stderr[-200:]}")
        write(proj, {"stats_util.py": MEDIAN_FIXED})
        commit(proj, "fix: median")
        why = (f"E-001 исправлена; проверка: python -m unittest test_median_even (падала на {before}, зелёная на HEAD)"
               if registered else "median исправлена, тесты зелёные")
        _done(proj, env, tid, "stats_util.py", why)
    return build


CASES = [
    Case("G1-hello", TK.HELLO, "accept", "", b_hello(True)),
    Case("G2-fizzbuzz", TK.FIZZBUZZ, "accept", "", b_fizz(False)),
    Case("G3-bugfix-с-реестром", TK.BUGFIX, "accept", "", b_bugfix(True)),
    Case("G4-wordstat", TK.WORDSTAT, "accept", "", b_wordstat(False)),
    Case("D1-fizzbuzz-числа-вместо-строк", TK.FIZZBUZZ, "return", "числа не строками; свои тесты написаны под это", b_fizz(True)),
    Case("D2-wordstat-связи-не-по-алфавиту", TK.WORDSTAT, "return", "при равных числах порядок появления, а не алфавит", b_wordstat(True)),
    Case("D3-bugfix-без-записи-в-реестр", TK.BUGFIX, "return", "код верный, но ошибка не занесена в реестр (п.4 договора)", b_bugfix(False)),
    Case("D4-hello-не-закоммичен", TK.HELLO, "return", "файл есть, но не закоммичен (договор: закоммичен)", b_hello(False)),
]
BY_ID = {c.id: c for c in CASES}


def contract_ok(case: Case, proj: Path) -> bool:
    """Верна ли сдача по договору тикета — независимо от Судьи (те же проверки кода, что в e2e, плюс то, что договор требует отдельно)."""
    ok, _ = case.task.check(proj, {})
    if case.task.id == "hello":
        ok = ok and bool(C.sh(["git", "ls-files", "hello.py"], proj).stdout.strip())  # «закоммичен»
    if case.task.id == "bugfix":
        ok = ok and case.task.protocol(proj)["errors_registered"] >= 1                # п.4 договора
    return bool(ok)


# --- запуск ---------------------------------------------------------------------------------------

JUDGE_ENTRY = re.compile(r"^### (\S+) judge\n(.*?)(?=^### |\Z)", re.M | re.S)


def prepare(case: Case, extra_env: dict | None = None) -> tuple[Path, str, dict]:
    """Проект в состоянии «Инженер сдал»: работа закоммичена, тикет `in_review`, судья ещё не запущен."""
    proj, _ = C.make_project(seed=case.task.seed)
    env = C.clean_env(RPV_PLUGIN_DIR=C.ROOT, RPV_PROJECT=proj, **(extra_env or {}))
    t = case.task
    r = C.cli("registry.py", proj, "goal", "--title", t.title[:60], "--quote", t.quote, "--criterion", t.criterion, env=env)
    gid = re.search(r"G-\d+", r.stdout + r.stderr).group(0)
    tid = C.new_ticket(proj, "engineer", t.title, gid, t.desc, effort=None, env=env)   # без effort: усилие Судьи — умолчание/вариант
    case.build(proj, dict(env, RPV_ROLE="engineer", RPV_TICKET=tid), tid)
    if C.ticket_state(proj, tid)[0] != "in_review":
        raise RuntimeError(f"тикет не в in_review: {C.ticket_state(proj, tid)}")
    return proj, tid, env


def run_case(variant: str, case: Case, rep: int, timeout_min: float = 10) -> dict:
    model, effort = VARIANTS[variant]
    t0 = time.time()
    proj, tid, env = prepare(case, {"BENCH_JUDGE_MODEL": model, "BENCH_JUDGE_EFFORT": effort})
    w = E.Watcher(proj, [tid])
    log = open(proj / "ab-dispatch.log", "ab")
    disp = subprocess.Popen([C.PY, str(BENCH / "bench_dispatch.py"), "--project", str(proj)], cwd=str(proj), env=env,
                            stdout=log, stderr=subprocess.STDOUT)
    verdict, entry = "", ""
    try:
        deadline = time.time() + timeout_min * 60
        while time.time() < deadline and not verdict:
            w.poll()
            text = (proj / ".claude" / "tickets" / f"{tid}.md").read_text(encoding="utf-8")
            m = next((x for x in JUDGE_ENTRY.finditer(text) if "[итог:" in x.group(2)), None)
            if m:
                verdict = re.search(r"\[итог: ([\w-]+)\]", m.group(2)).group(1)
                entry = m.group(2).strip()
            time.sleep(0.5)
    finally:
        if disp.poll() is None:  # вердикт получен — дальше диспетчер разбудил бы Инженера на переделку: не нужно
            disp.terminate()
            try:
                disp.wait(timeout=20)
            except subprocess.TimeoutExpired:
                disp.kill()
    settle = time.time() + 240
    while time.time() < settle and any(f.stat().st_size == 0 for f in E.run_jsons(w.runs_dir)):
        time.sleep(1)
        w.poll()
    runs = E.collect_runs(proj, w)
    judge = [r for r in runs if r["role"] == "judge"]
    extra = [r for r in runs if r["role"] != "judge"]
    j = judge[0] if judge else {}
    if verdict == "accept":
        label = "верно" if case.expect == "accept" else "ЛОЖНОЕ ПРИНЯТИЕ"
    elif case.expect == "accept":
        label = "ЛОЖНЫЙ ОТКАЗ" if verdict else "нет вердикта"
    else:
        label = "верно" if verdict == "return" else ("не принял (не return)" if verdict else "нет вердикта")
    tr = j.get("transcript") or {}
    res = {"variant": variant, "model": model, "effort": effort, "case": case.id, "rep": rep, "expect": case.expect,
           "defect": case.defect, "contract_ok": contract_ok(case, proj), "verdict": verdict or None, "label": label,
           "judge_runs": len(judge), "extra_runs_after_verdict": len(extra), "cost_usd": round(sum(r["cost_usd"] for r in runs), 4),
           "judge_cost_usd": j.get("cost_usd"), "turns": j.get("turns"), "duration_s": j.get("duration_s"),
           "out_tokens": j.get("out"), "cache_read": j.get("cache_read"), "cache_write": j.get("cache_write"),
           "models_used": sorted((j.get("models") or {}).keys()), "tool_calls": tr.get("tool_calls_total"),
           "tool_errors": tr.get("tool_errors"), "entry": entry[:600], "wall_s": round(time.time() - t0, 1)}
    shutil.rmtree(proj, ignore_errors=True)
    for p in proj.parent.glob(proj.name + "-*"):
        shutil.rmtree(p, ignore_errors=True)
    return res


def run(variants: list[str], repeat: int, budget: float, workers: int, cases: list[str] | None = None, log=print) -> dict:
    jobs = [(v, c, r) for r in range(1, repeat + 1) for c in (cases or [x.id for x in CASES]) for v in variants]
    spent, lock, results = [0.0], threading.Lock(), []
    skipped = []

    def work(job):
        v, cid, rep = job
        with lock:
            if spent[0] >= budget:
                skipped.append(f"{v}/{cid}#{rep}")
                return
        try:
            res = run_case(v, BY_ID[cid], rep)
        except Exception as e:
            import traceback
            res = {"variant": v, "case": cid, "rep": rep, "label": "ИСКЛЮЧЕНИЕ", "error": repr(e), "trace": traceback.format_exc()[-500:], "cost_usd": 0.0}
        with lock:
            spent[0] += res.get("cost_usd") or 0
            results.append(res)
        log(f"[judge-ab] {v:13s} {cid:34s} #{rep}: {res['label']:22s} вердикт={res.get('verdict')} ${res.get('cost_usd', 0):.2f} (всего ${spent[0]:.2f})")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(work, jobs))
    return {"variants": {v: VARIANTS[v] for v in variants}, "repeat": repeat, "budget_usd": budget, "spent_usd": round(spent[0], 4),
            "skipped_for_budget": skipped, "results": results, "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "env": C.env_info()}


# --- отчёт ----------------------------------------------------------------------------------------

def render(data: dict) -> str:
    res = [r for r in data["results"] if r.get("label") != "ИСКЛЮЧЕНИЕ"]
    variants = list(data["variants"])
    out = ["# A/B Судьи: модель и усилие против цены и качества\n",
           f"Сдач: {len(set(r['case'] for r in res))} (верных по договору {sum(1 for c in CASES if c.expect == 'accept')}, "
           f"с дефектом {sum(1 for c in CASES if c.expect == 'return')}), повторов {data['repeat']}, потрачено **${data['spent_usd']:.2f}** "
           f"из ${data['budget_usd']}.\n"]
    rows = []
    base = None
    for v in variants:
        rs = [r for r in res if r["variant"] == v]
        if not rs:
            continue
        n = len(rs)
        fa = sum(r["label"] == "ЛОЖНОЕ ПРИНЯТИЕ" for r in rs)
        fr = sum(r["label"] in ("ЛОЖНЫЙ ОТКАЗ", "нет вердикта") and r["expect"] == "accept" for r in rs)
        ok = sum(r["label"] == "верно" for r in rs)
        bad = [r for r in rs if r["expect"] == "return"]
        caught = sum(r["verdict"] == "return" for r in bad)
        cost = sum(r["judge_cost_usd"] or 0 for r in rs) / n
        if v == variants[0]:
            base = cost
        rows.append([v, f"{data['variants'][v][0].replace('claude-', '')} / {data['variants'][v][1]}", n, f"{ok}/{n}", fa, fr,
                     f"{caught}/{len(bad)}", f"${cost:.3f}", f"{100 * (cost / base - 1):+.0f}%" if base else "—",
                     round(sum(r["turns"] or 0 for r in rs) / n, 1), round(sum(r["duration_s"] or 0 for r in rs) / n),
                     round(sum(r["out_tokens"] or 0 for r in rs) / n)])
    out.append(_table(["вариант", "модель / усилие", "запусков", "верно", "ЛОЖНЫХ ПРИНЯТИЙ", "ложных отказов", "дефектов пойман (return)",
                       "цена запуска", "к первому", "ходов", "время, с", "выход, ток."], rows))
    out.append("\n## По сдачам: вердикт и цена запуска\n")
    cases = [c.id for c in CASES if any(r["case"] == c.id for r in res)]
    mrows = []
    for cid in cases:
        row = [cid, BY_ID[cid].expect]
        for v in variants:
            rs = [r for r in res if r["case"] == cid and r["variant"] == v]
            row.append(" · ".join(f"{('✓' if r['label'] == 'верно' else '✗ ' + (r['verdict'] or '?'))} ${r['judge_cost_usd'] or 0:.2f}" for r in rs) or "—")
        mrows.append(row)
    out.append(_table(["сдача", "надо", *variants], mrows))
    misses = [r for r in res if r["label"] != "верно"]
    if misses:
        out.append("\n## Промахи и что написал Судья\n")
        for r in misses:
            out.append(f"- **{r['variant']} · {r['case']} #{r['rep']}**: {r['label']} (вердикт {r['verdict']}). " + r["entry"][:380].replace("\n", " ") + "\n")
    errs = [r for r in data["results"] if r.get("label") == "ИСКЛЮЧЕНИЕ"]
    for r in errs:
        out.append(f"- ❌ {r['variant']}/{r['case']}: {r['error']}\n")
    if data["skipped_for_budget"]:
        out.append(f"\nНе запущены из-за потолка: {len(data['skipped_for_budget'])}\n")
    return "\n".join(out)


def _table(headers: list[str], rows: list[list]) -> str:
    if not rows:
        return "_нет данных_\n"
    s = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" if i == 0 else "---:" for i in range(len(headers))) + "|"]
    s += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(s) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--variants", default="opus-xhigh,opus-high,opus-medium,sonnet-xhigh", help="через запятую; первый — база для «к первому»")
    ap.add_argument("--cases", help="через запятую: только эти сдачи")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--budget-usd", type=float, default=10.0)
    ap.add_argument("--workers", type=int, default=4, help="сколько Судей одновременно (у каждого свой проект и диспетчер)")
    ap.add_argument("--out", default="bench-out")
    a = ap.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    variants = [v.strip() for v in a.variants.split(",") if v.strip()]
    bad = [v for v in variants if v not in VARIANTS]
    if bad:
        ap.error(f"неизвестные варианты: {bad}; есть {list(VARIANTS)}")
    t0 = time.time()
    data = run(variants, a.repeat, a.budget_usd, a.workers, a.cases.split(",") if a.cases else None,
               log=lambda s: print(f"{time.time() - t0:6.0f}s {s}", flush=True))
    d = Path(a.out)
    d.mkdir(parents=True, exist_ok=True)
    (d / "judge-ab.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    (d / "judge-ab.md").write_text(render(data), encoding="utf-8")
    print(f"готово: {d / 'judge-ab.json'}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
