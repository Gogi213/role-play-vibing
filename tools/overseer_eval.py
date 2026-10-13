#!/usr/bin/env python3
"""Замер Надзирателя (CP14, `agents/overseer.md`) на истории проекта: ловит ли он эпизоды «деньги и время идут, к цели не
движемся» заранее и сколько ложных тревог. Только stdlib. Данные проекта читаются, в репозиторий не копируются.

    python tools/overseer_eval.py plan   --alpha <проект> [--tickets TK-087,TK-048 | --group bad|normal|all] [--step-min 15]
                                          [--skip-roles watch,ci,…] [--fit <итоги>] [--per-call 0.01]
    python tools/overseer_eval.py run    --alpha <проект> --out <итоги> [--tickets …] [--group …] [--step-min 15]
                                          [--jobs 4] [--budget 1.0] [--model claude-haiku-5-5] [--effort high]
    python tools/overseer_eval.py report --out <итоги>
    python tools/overseer_eval.py regrade --alpha <проект> --out <итоги>   # правило проверки цитаты поправлено — без вызовов
    python tools/overseer_eval.py prompt --alpha <проект> --ticket TK-087 --window 5   # что увидит Надзиратель

Источники (проект, `--alpha`):
  - `.claude/tickets/TK-*.md` — шапка (`title`, `status`), описание (цель: слова владельца) и `## Лог`;
  - `.claude/tickets/archive/TK-*-log.md` — старые записи лога; запись — `### <ISO-время> <роль>` и текст;
  - `.claude/dispatcher/runs.log` — запуски: `<ISO> <тикет> <роль> reason=… cost_usd=… resolved_cost=… status=…`.
    Цена запуска — `resolved_cost` (начислено именно этому запуску); нет его — `cost_usd`. Сумма `cost_usd` по запускам
    одной сессии считает её итог много раз (сессия с `--resume` отдаёт итог с начала), поэтому она втрое-вчетверо больше.

Окна. Лог тикета режется на окна по `--step-min` минут от первой записи; окно с новыми записями — один вызов Надзирателя
в момент конца окна. Он видит: цель (описание), строку кода «к этому моменту» (запуски, $, часы от начала, возвраты Судьи —
считает код по runs.log и логу на конец окна, модель чисел не выдаёт), ход тикета (по строке на прежнюю запись), новые
записи окна (последние `--max-kb` КБ, обрезка с пометкой). Описание тикета берётся каким оно лежит сейчас: прежние его
редакции не восстановить.

Ответ — JSON. Находка принимается, только если `quote` есть дословно (пробелы нормализуются) в одной записи лога тикета
до конца окна, вид из четырёх, `ticket` совпадает; иначе «отброшена» с причиной. Числа в `action`, которых не дал код,
помечаются `invented_numbers` (не отбрасывают находку); «дал код» — всё, что было в промпте.

Набор по умолчанию (`--group all`):
  - плохие: TK-048, 049, 063, 065, 071, 076, 079, 087, 100, 120 (эпизоды, разобранные в docs/v2/checkpoints.md);
  - обычные: 20 тикетов, выбранных кодом: статус `done`, от 1 до 6 запусков в runs.log, ни одного возврата Судьи
    (запись судьи `[итог: return]`), не из плохих; кандидаты по номеру, `random.Random(14).sample(…, 20)`.

`--budget` — стоп новых вызовов, когда сумма цен этого запуска достигла лимита (уже идущие `--jobs` вызовов доделываются).
Запуск продолжаемый: готовые окна (не ошибки) повторно не считаются; один `--out` — один набор настроек окон и промпта.
Вызов — `claude -p --agent overseer --model claude-haiku-5-5 --effort high --output-format json` с определением из
`agents/overseer.md` этой рабочей копии (через `--agents`, как у helper_eval), без инструментов, MCP и пользовательских
настроек; цена вызова — `total_cost_usd` ответа. Итоги `--out`: `windows.jsonl` (строка на окно), `prompts/` (что видел
Надзиратель), `summary.md`.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BAD = ("TK-048", "TK-049", "TK-063", "TK-065", "TK-071", "TK-076", "TK-079", "TK-087", "TK-100", "TK-120")
NORMAL_N, NORMAL_SEED, NORMAL_MAX_RUNS = 20, 14, 6
MECHANICAL = ("watch", "dispatcher", "ci", "merge")  # записи кода, не ролей: для оценки «сколько окон без них»
KINDS = ("не-движемся", "ломает-принятое", "вопрос-уже-решён", "не-служит-цели")
DEFAULT_TZ = timezone(timedelta(hours=4))  # запись без пояса — время владельца (GMT+4), как у логов проекта
MIN_QUOTE = 12  # короче этого цитата не доказательство
SUPPRESS_H = 24  # против шума: одна принятая находка по тикету не чаще раза в сутки (docs/v2/checkpoints.md, CP14)
RECENT_H = 6
DAYS_SHOWN = 7

HDR = re.compile(r"^### (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?) (\S+)[ \t]*$", re.M)
RUN_KV = re.compile(r"\b(\w+)=(\S*)")
RETURN = re.compile(r"^\[итог:\s*return\]")


def emit(text: str) -> None:
    """Печать в UTF-8 (консоль Windows по умолчанию не она)."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(text)


def claude_bin() -> str:
    """`claude` берётся при вызове, не при импорте: в тестах `CLAUDE_BIN` подменён на несуществующий файл (conftest)."""
    return os.environ.get("CLAUDE_BIN") or shutil.which("claude") or "claude"


# ------------------------------------------------------------------------------------------------------------
# Разбор данных проекта
# ------------------------------------------------------------------------------------------------------------

def parse_ts(s: str) -> datetime:
    t = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=DEFAULT_TZ)


def norm_ws(s: str) -> str:
    return " ".join((s or "").split())


@dataclass
class Entry:
    ts: datetime
    role: str
    text: str
    norm: str = field(default="", repr=False)  # текст с нормализованными пробелами — для проверки цитаты

    def __post_init__(self):
        if not self.norm:
            self.norm = norm_ws(self.text)


@dataclass
class Run:
    ts: datetime
    role: str
    cost: float          # начислено запуску (resolved_cost, иначе cost_usd)
    status: str = ""
    raw: float = 0.0     # cost_usd как записан: итог сессии с начала, в сумме по запускам считает повторно


@dataclass
class Ticket:
    tid: str
    title: str
    status: str
    desc: str
    entries: list


def parse_log(text: str) -> list:
    """Записи лога: `### <ISO> <роль>` и текст до следующего такого заголовка. Что до первого заголовка — не запись."""
    ms = list(HDR.finditer(text))
    out = []
    for i, m in enumerate(ms):
        end = ms[i + 1].start() if i + 1 < len(ms) else len(text)
        out.append(Entry(parse_ts(m.group(1)), m.group(2), text[m.end():end].strip()))
    return out


def split_ticket(text: str) -> tuple:
    """Файл тикета → (шапка, описание, текст лога). Описание — между шапкой и `## Лог`."""
    m = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, re.S)
    head, body = {}, text
    if m:
        for ln in m.group(1).splitlines():
            k, _, v = ln.partition(":")
            head[k.strip()] = v.strip()
        body = m.group(2)
    lg = re.search(r"^## Лог[ \t]*$", body, re.M)
    if not lg:
        return head, body.strip(), ""
    return head, body[:lg.start()].strip(), body[lg.end():]


def merge_entries(*lists) -> list:
    """Записи архива и файла по времени; одинаковые (время, роль, текст) — одна."""
    seen, out = set(), []
    for e in sorted((e for lst in lists for e in lst), key=lambda e: e.ts):
        key = (e.ts, e.role, e.text)
        if key not in seen:
            seen.add(key)
            out.append(e)
    return out


def load_ticket(alpha: Path, tid: str) -> Ticket:
    tdir = alpha / ".claude" / "tickets"
    head, desc, log = split_ticket((tdir / f"{tid}.md").read_text(encoding="utf-8"))
    arch = tdir / "archive" / f"{tid}-log.md"
    old = parse_log(arch.read_text(encoding="utf-8")) if arch.exists() else []
    return Ticket(tid, head.get("title", tid), head.get("status", "?"), desc, merge_entries(old, parse_log(log)))


def ticket_ids(alpha: Path) -> list:
    return sorted(p.stem for p in (alpha / ".claude" / "tickets").glob("TK-*.md"))


def _num(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def parse_runs(text: str) -> dict:
    """runs.log → {тикет: [Run по времени]}. `on_met` — команда проекта, не запуск модели. Цена — resolved_cost, иначе cost_usd."""
    out = {}
    for ln in text.splitlines():
        p = ln.split()
        if len(p) < 3 or p[2] == "on_met":
            continue
        try:
            ts = parse_ts(p[0])
        except ValueError:
            continue
        kv = dict(RUN_KV.findall(ln))
        raw = _num(kv.get("cost_usd")) or 0.0
        cost = _num(kv.get("resolved_cost"))
        out.setdefault(p[1], []).append(Run(ts, p[2], raw if cost is None else cost, kv.get("status", ""), raw))
    for lst in out.values():
        lst.sort(key=lambda r: r.ts)
    return out


def load_runs(alpha: Path) -> dict:
    p = alpha / ".claude" / "dispatcher" / "runs.log"
    return parse_runs(p.read_text(encoding="utf-8")) if p.exists() else {}


def is_judge_return(e: Entry) -> bool:
    return e.role == "judge" and bool(RETURN.match(e.text))


# ------------------------------------------------------------------------------------------------------------
# Окна и числа к моменту окна
# ------------------------------------------------------------------------------------------------------------

@dataclass
class Window:
    n: int          # номер среди окон тикета с записями, с 1
    index: int      # номер ячейки сетки от первой записи тикета, с 0
    start: datetime
    end: datetime
    new: list       # записи окна


def make_windows(entries: list, step_min: int, skip_roles=()) -> list:
    """Окна по step_min минут от первой записи тикета; только те, где есть записи ролей не из skip_roles."""
    if not entries:
        return []
    t0, step = entries[0].ts, timedelta(minutes=step_min)
    cells = {}
    for e in entries:
        if e.role not in skip_roles:
            cells.setdefault((e.ts - t0) // step, []).append(e)
    return [Window(n, k, t0 + k * step, t0 + (k + 1) * step, cells[k]) for n, k in enumerate(sorted(cells), 1)]


@dataclass
class Stats:
    runs: int
    cost: float
    hours: float
    returns: int
    runs_recent: int
    cost_recent: float
    days: list  # [(дата, запусков, $)] по суткам, последние DAYS_SHOWN


def ticket_start(entries: list, runs: list):
    firsts = [x[0].ts for x in (entries, runs) if x]
    return min(firsts) if firsts else None


def stats_at(now: datetime, entries: list, runs: list, start=None) -> Stats:
    """Числа на момент now: запуски и $ из runs.log, часы от начала, возвраты Судьи из лога. Всё, что позже now, не считается."""
    start = start or ticket_start(entries, runs) or now
    done = [r for r in runs if r.ts <= now]
    recent = [r for r in done if r.ts > now - timedelta(hours=RECENT_H)]
    per_day = {}
    for r in done:
        d = per_day.setdefault(r.ts.date().isoformat(), [0, 0.0])
        d[0] += 1
        d[1] += r.cost
    return Stats(runs=len(done), cost=sum(r.cost for r in done), hours=max((now - start).total_seconds(), 0) / 3600,
                 returns=sum(1 for e in entries if e.ts <= now and is_judge_return(e)),
                 runs_recent=len(recent), cost_recent=sum(r.cost for r in recent),
                 days=[(d, v[0], v[1]) for d, v in sorted(per_day.items())][-DAYS_SHOWN:])


def stats_text(s: Stats) -> str:
    out = [f"к этому моменту: запусков {s.runs}, потрачено ${s.cost:.2f}, часов от начала {s.hours:.1f}, "
           f"возвратов Судьи {s.returns}.",
           f"За последние {RECENT_H} ч: запусков {s.runs_recent}, ${s.cost_recent:.2f}."]
    if s.days:
        out.append("По суткам: " + "; ".join(f"{d} — запусков {n}, ${c:.2f}" for d, n, c in s.days) + ".")
    return "\n".join(out)


# ------------------------------------------------------------------------------------------------------------
# Что видит Надзиратель
# ------------------------------------------------------------------------------------------------------------

def cut(text: str, limit: int) -> str:
    """Длинный текст: начало и конец, посередине пометка кода."""
    if len(text) <= limit:
        return text
    head, tail = int(limit * 0.6), limit - int(limit * 0.6)
    return f"{text[:head]} ⟦обрезано: показано {limit} из {len(text)} знаков⟧ {text[-tail:]}"


def gist(text: str, width: int) -> str:
    t = norm_ws(text)
    return t if len(t) <= width else t[:width] + " ⟦обрезано⟧"


def entry_block(e: Entry, entry_chars: int) -> str:
    return f"### {e.ts.isoformat()} {e.role}\n{cut(e.text, entry_chars)}"


def pick_new(new: list, max_bytes: int, entry_chars: int) -> tuple:
    """Новые записи окна: с конца, пока влезают в max_bytes. → (блоки по времени, сколько старых записей пропущено)."""
    blocks, size = [], 0
    for e in reversed(new):
        b = entry_block(e, entry_chars)
        if blocks and size + len(b.encode()) > max_bytes:
            break
        blocks.append(b)
        size += len(b.encode())
    return blocks[::-1], len(new) - len(blocks)


def build_prompt(t: Ticket, w: Window, stats: Stats, *, max_kb: float = 12, ctx_lines: int = 40, ctx_width: int = 180,
                 entry_chars: int = 2500, desc_chars: int = 3000, skip_roles=()) -> str:
    before = [e for e in t.entries if e.ts < w.start and e.role not in skip_roles]
    shown = before[-ctx_lines:] if ctx_lines else []
    blocks, dropped = pick_new(w.new, int(max_kb * 1024), entry_chars)
    out = [f"Тикет {t.tid} — {t.title}", f"Сейчас: {w.end.isoformat()}", "",
           "## Цель (описание тикета)", cut(t.desc, desc_chars), "",
           "## Числа (считал код)", stats_text(stats), ""]
    if shown:
        head = "## Ход тикета до этого окна (по строке на запись" + (
            f"; показаны последние {len(shown)} из {len(before)})" if len(before) > len(shown) else ")")
        out += [head] + [f"{e.ts.strftime('%m-%dT%H:%M')} {e.role}: {gist(e.text, ctx_width)}" for e in shown] + [""]
    else:
        out += ["## Ход тикета до этого окна", "Записей до этого окна нет.", ""]
    out.append(f"## Новые записи ({w.start.isoformat()} — {w.end.isoformat()})")
    if dropped:
        out.append(f"⟦пропущено старых записей окна: {dropped}; показаны последние {len(blocks)} в пределах {max_kb:g} КБ⟧")
    out += blocks
    return "\n".join(out) + "\n"


# ------------------------------------------------------------------------------------------------------------
# Разбор ответа и проверка цитаты
# ------------------------------------------------------------------------------------------------------------

def parse_answer(raw: str):
    """Первый JSON-объект с ключом verdict в ответе (ограждения и лишний текст вокруг не мешают); нет — None."""
    s = raw or ""
    dec = json.JSONDecoder()
    for m in re.finditer(r"\{", s):
        try:
            obj, _ = dec.raw_decode(s[m.start():])
        except ValueError:
            continue
        if isinstance(obj, dict) and "verdict" in obj:
            return obj
    return None


def quote_found(quote: str, entries: list, until: datetime) -> bool:
    """Цитата есть дословно (пробелы нормализуются) в одной записи лога до момента until."""
    q = norm_ws(quote)
    return len(q) >= MIN_QUOTE and any(q in e.norm for e in entries if e.ts <= until)


_NUMBER = re.compile(r"(?<![\w-])\d+(?:[.,]\d+)?")
_ID = re.compile(r"\b(?:TK|G|В|Г|П|PR)[ -]?#?\d+\b")


def _numbers(s: str) -> list:
    """Числа текста; «1,50», «1.5» и «1.50» — одно число, «33.0» и «33» — тоже."""
    out = []
    for n in _NUMBER.findall(_ID.sub(" ", s or "")):
        n = n.replace(",", ".")
        out.append(n.rstrip("0").rstrip(".") if "." in n else n)
    return out


def invented_numbers(action: str, shown: str) -> list:
    """Числа в action, которых нет в том, что видел Надзиратель (числа кода, цель, записи лога): запятая и точка — одно и то же."""
    allowed = set(_numbers(shown))
    return sorted({n for n in _numbers(action) if n not in allowed})


def grade(raw: str, tid: str, entries: list, until: datetime, shown: str = "") -> dict:
    """Ответ модели → {outcome: quiet|accepted|rejected|error, reason, kind, quote, action, invented_numbers}."""
    obj = parse_answer(raw)
    res = {"outcome": "error", "reason": "", "kind": "", "quote": "", "action": "", "invented_numbers": []}
    if obj is None:
        res["reason"] = "ответ не JSON"
        return res
    verdict = str(obj.get("verdict", "")).strip().lower()
    if verdict == "тихо":
        res["outcome"] = "quiet"
        return res
    if verdict != "находка":
        res["reason"] = f"verdict «{verdict}» не из двух"
        return res
    res.update(kind=str(obj.get("kind", "")).strip(), quote=str(obj.get("quote", "")), action=str(obj.get("action", "")).strip())
    res["outcome"] = "rejected"
    if res["kind"] not in KINDS:
        res["reason"] = "вид не из четырёх"
    elif str(obj.get("ticket", "")).strip() != tid:
        res["reason"] = "тикет не тот"
    elif len(norm_ws(res["quote"])) < MIN_QUOTE:
        res["reason"] = "нет цитаты"
    elif not quote_found(res["quote"], entries, until):
        res["reason"] = "цитаты нет в логе дословно"
    elif not res["action"]:
        res["reason"] = "нет действия"
    else:
        res["outcome"] = "accepted"
        res["invented_numbers"] = invented_numbers(res["action"], shown)
    return res


# ------------------------------------------------------------------------------------------------------------
# Набор
# ------------------------------------------------------------------------------------------------------------

def normal_pool(info: list, max_runs: int = NORMAL_MAX_RUNS, exclude=BAD) -> list:
    """Кандидаты в обычные: info = [(тикет, статус, запусков, возвратов Судьи)]. done, 1…max_runs запусков, возвратов нет, не из плохих."""
    return sorted(t for t, st, runs, ret in info if st == "done" and 1 <= runs <= max_runs and ret == 0 and t not in exclude)


def pick_normal(info: list, n: int = NORMAL_N, seed: int = NORMAL_SEED, max_runs: int = NORMAL_MAX_RUNS,
                exclude=BAD) -> list:
    """Обычные тикеты: random.Random(seed).sample(n) из кандидатов по номеру (`normal_pool`)."""
    pool = normal_pool(info, max_runs, exclude)
    return sorted(random.Random(seed).sample(pool, min(n, len(pool))))


def load_all(alpha: Path) -> tuple:
    tickets = {tid: load_ticket(alpha, tid) for tid in ticket_ids(alpha)}
    return tickets, load_runs(alpha)


def ticket_info(tickets: dict, runs: dict) -> list:
    return [(t.tid, t.status, len(runs.get(t.tid, [])), sum(1 for e in t.entries if is_judge_return(e))) for t in tickets.values()]


def choose(tickets: dict, runs: dict, only=None, group: str = "all") -> list:
    """[(тикет, группа)]: явный список (`only`) или набор по умолчанию."""
    if only:
        missing = [t for t in only if t not in tickets]
        if missing:
            raise SystemExit(f"нет тикетов: {', '.join(missing)}")
        return [(t, "bad" if t in BAD else "other") for t in only]
    info = ticket_info(tickets, runs)
    bad = [(t, "bad") for t in BAD if t in tickets]
    normal = [(t, "normal") for t in pick_normal(info)]
    return {"bad": bad, "normal": normal, "all": bad + normal}[group]


# ------------------------------------------------------------------------------------------------------------
# Вызов claude
# ------------------------------------------------------------------------------------------------------------

_LOCK = threading.Lock()


def load_agent(name: str = "overseer") -> dict:
    """agents/<name>.md → определение для `claude --agents` (как tools/helper_eval.py). Пустой `tools:` — без инструментов."""
    text = (ROOT / "agents" / f"{name}.md").read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    if not m:
        raise SystemExit(f"agents/{name}.md: нет фронтматтера")
    meta = {}
    for ln in m.group(1).splitlines():
        k, _, v = ln.partition(":")
        meta[k.strip()] = v.strip().strip('"')
    d = {"description": meta.get("description", name), "prompt": m.group(2).strip(),
         "tools": [t.strip() for t in meta.get("tools", "").split(",") if t.strip()]}
    for k in ("model", "effort"):
        if meta.get(k):
            d[k] = meta[k]
    return d


def call_overseer(prompt: str, agents_file: Path, workdir: Path, model: str, effort: str, per_call: float, timeout: int) -> dict:
    """Один вызов: `claude -p --agent overseer` без инструментов, без MCP, настроек пользователя и навыков — только устав и промпт."""
    cmd = [claude_bin(), "-p", "--agents", str(agents_file), "--agent", "overseer", "--model", model, "--effort", effort,
           "--output-format", "json", "--no-session-persistence", "--max-budget-usd", str(per_call),
           "--tools", "", "--strict-mcp-config", "--disable-slash-commands", "--setting-sources", "project"]
    t0 = time.time()
    raw = ""
    try:
        r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8", cwd=str(workdir), timeout=timeout)
        raw = r.stdout
    except (subprocess.TimeoutExpired, OSError) as e:
        res = {"is_error": True, "result": f"{type(e).__name__}: {e}"}
    else:
        try:
            res = json.loads(raw)
        except ValueError:
            res = {"is_error": True, "result": (raw or r.stderr)[-1000:]}
    u = res.get("usage") or {}
    ans = res.get("result") or ""
    return {"answer": ans, "cost": res.get("total_cost_usd"), "dur": round(time.time() - t0, 1),
            "error": bool(res.get("is_error")) or not ans,
            "in_tok": (u.get("input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0),
            "out_tok": u.get("output_tokens"), "think_tok": (u.get("output_tokens_details") or {}).get("thinking_tokens"),
            "models": sorted(res.get("modelUsage") or {})}  # кто на самом деле отвечал


# ------------------------------------------------------------------------------------------------------------
# Прогон
# ------------------------------------------------------------------------------------------------------------

def prompt_opts(a) -> dict:
    return {"max_kb": a.max_kb, "ctx_lines": a.ctx_lines, "entry_chars": a.entry_chars, "desc_chars": a.desc_chars,
            "skip_roles": tuple(x for x in a.skip_roles.split(",") if x)}


def window_key(tid: str, step: int, end: datetime) -> str:
    return f"{tid}|{step}|{end.isoformat()}"


def plan_jobs(tickets: dict, runs: dict, chosen: list, a) -> list:
    """Все окна набора с готовым промптом и числами: [{…поля строки итогов…, prompt}]."""
    opts, jobs = prompt_opts(a), []
    for tid, group in chosen:
        t = tickets[tid]
        rs = runs.get(tid, [])
        start = ticket_start(t.entries, rs)
        total = sum(r.cost for r in rs)
        for w in make_windows(t.entries, a.step_min, opts["skip_roles"]):
            st = stats_at(w.end, t.entries, rs, start)
            prompt = build_prompt(t, w, st, **opts)
            jobs.append({"key": window_key(tid, a.step_min, w.end), "ticket": tid, "group": group, "window": w.n,
                         "start": w.start.isoformat(), "end": w.end.isoformat(), "step_min": a.step_min, "n_new": len(w.new),
                         "prompt_chars": len(prompt), "runs": st.runs, "spent": round(st.cost, 4), "hours": round(st.hours, 2),
                         "returns": st.returns, "ticket_total": round(total, 4), "ticket_runs": len(rs),
                         "prompt": prompt, "_until": w.end})
    return jobs


def read_rows(out: Path) -> list:
    p = out / "windows.jsonl"
    return [json.loads(l) for l in p.open(encoding="utf-8") if l.strip()] if p.exists() else []


def cmd_run(a) -> int:
    alpha, out = Path(a.alpha).resolve(), Path(a.out).resolve()  # resolve(): короткое имя Windows Claude Code считает подозрительным
    (out / "work").mkdir(parents=True, exist_ok=True)
    (out / "prompts").mkdir(exist_ok=True)
    tickets, runs = load_all(alpha)
    only = [x for x in a.tickets.split(",") if x] if a.tickets else None
    chosen = choose(tickets, runs, only, a.group)
    jobs = plan_jobs(tickets, runs, chosen, a)
    done = {r["key"] for r in read_rows(out) if r["outcome"] != "error"}
    todo = [j for j in jobs if j["key"] not in done]
    agents = out / "agents.json"
    d = load_agent()
    d["model"], d["effort"] = a.model, a.effort
    agents.write_text(json.dumps({"overseer": d}, ensure_ascii=False), encoding="utf-8")
    state = {"spent": 0.0, "calls": 0, "skipped": 0}
    print(f"окон в наборе: {len(jobs)}, готово раньше: {len(jobs) - len(todo)}, к вызову: {len(todo)}, бюджет ${a.budget:.2f}", flush=True)

    def one(j):
        with _LOCK:
            if state["spent"] >= a.budget:
                state["skipped"] += 1
                return
        t = tickets[j["ticket"]]
        res = None
        for _ in range(2):  # сбой связи или пустой ответ — один повтор, цена обоих вызовов в сумме
            r = call_overseer(j["prompt"], agents, out / "work", a.model, a.effort, a.per_call, a.timeout)
            res = r if res is None else {**r, "cost": (r["cost"] or 0) + (res["cost"] or 0), "retried": True}
            with _LOCK:
                state["spent"] += r["cost"] or 0
                state["calls"] += 1
            if not r["error"]:
                break
        g = grade(res["answer"], j["ticket"], t.entries, j["_until"], j["prompt"]) if not res["error"] else \
            {"outcome": "error", "reason": "ошибка вызова", "kind": "", "quote": "", "action": "", "invented_numbers": []}
        row = {k: v for k, v in j.items() if k not in ("prompt", "_until")}
        row.update(res)
        row.update(g)
        prompt_file(out, row).write_text(j["prompt"], encoding="utf-8")
        with _LOCK:
            with (out / "windows.jsonl").open("a", encoding="utf-8") as w:
                w.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"{j['ticket']} окно {j['window']:>3} {j['end'][5:16]} ${row['cost'] or 0:.4f} {row['dur']:>5}s {row['outcome']}"
              + (f" ({row['kind']})" if row["outcome"] == "accepted" else f" [{row['reason']}]" if row["reason"] else ""), flush=True)

    with ThreadPoolExecutor(max_workers=a.jobs) as ex:
        list(ex.map(one, todo))
    if state["skipped"]:
        print(f"бюджет исчерпан: пропущено окон {state['skipped']} (продолжить — тот же --out и больший --budget)", flush=True)
    print(f"вызовов {state['calls']}, цена этого запуска ${state['spent']:.4f}", flush=True)
    return cmd_report(a)


def prompt_file(out: Path, r: dict) -> Path:
    return out / "prompts" / f"{r['ticket']}-s{r['step_min']}-{r['window']:04d}.txt"


def cmd_regrade(a) -> int:
    """Проверки заново по сохранённым ответам и промптам (правило цитаты поправлено) — без вызовов модели."""
    alpha, out = Path(a.alpha).resolve(), Path(a.out).resolve()
    last = {}
    for r in read_rows(out):
        last[r["key"]] = r
    cache = {}
    for r in last.values():
        if r["outcome"] == "error" and not r.get("answer"):
            continue
        if r["ticket"] not in cache:
            cache[r["ticket"]] = load_ticket(alpha, r["ticket"])
        pf = prompt_file(out, r)
        r.update(grade(r["answer"], r["ticket"], cache[r["ticket"]].entries, parse_ts(r["end"]),
                       pf.read_text(encoding="utf-8") if pf.exists() else ""))
    with (out / "windows.jsonl").open("w", encoding="utf-8") as w:
        for r in last.values():
            w.write(json.dumps(r, ensure_ascii=False) + "\n")
    return cmd_report(a)


# ------------------------------------------------------------------------------------------------------------
# План (без вызовов)
# ------------------------------------------------------------------------------------------------------------

def fit_cost(rows: list) -> tuple:
    """Цена вызова от размера промпта по готовым итогам: (a, b) для cost ≈ a + b·prompt_chars; мало данных — (среднее, 0)."""
    pts = [(r["prompt_chars"], r["cost"]) for r in rows if r.get("cost") is not None and not r.get("error")]
    if not pts:
        return 0.0, 0.0
    mx, my = sum(x for x, _ in pts) / len(pts), sum(y for _, y in pts) / len(pts)
    sxx = sum((x - mx) ** 2 for x, _ in pts)
    if len(pts) < 4 or sxx == 0:
        return my, 0.0
    b = sum((x - mx) * (y - my) for x, y in pts) / sxx
    return my - b * mx, b


def cmd_plan(a) -> int:
    alpha = Path(a.alpha).resolve()
    tickets, runs = load_all(alpha)
    only = [x for x in a.tickets.split(",") if x] if a.tickets else None
    chosen = choose(tickets, runs, only, a.group)
    jobs = plan_jobs(tickets, runs, chosen, a)
    mech = tuple(MECHANICAL)
    md = ["| тикет | группа | статус | запусков | цена $ (resolved_cost) | Σ cost_usd $ | записей | часов | окон (шаг %d) | без механических | шаг 30 | шаг 60 | тыс. знаков промптов |" % a.step_min,
          "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    tot = [0, 0, 0, 0, 0]
    for tid, group in chosen:
        t, rs = tickets[tid], runs.get(tid, [])
        wins = [j for j in jobs if j["ticket"] == tid]
        w_mech = len(make_windows(t.entries, a.step_min, mech))
        w30, w60 = len(make_windows(t.entries, 30)), len(make_windows(t.entries, 60))
        hours = (t.entries[-1].ts - t.entries[0].ts).total_seconds() / 3600 if t.entries else 0
        kb = sum(j["prompt_chars"] for j in wins) / 1000
        md.append(f"| {tid} | {group} | {t.status} | {len(rs)} | {sum(r.cost for r in rs):.2f} | {sum(r.raw for r in rs):.2f} | "
                  f"{len(t.entries)} | {hours:.1f} | {len(wins)} | {w_mech} | {w30} | {w60} | {kb:.0f} |")
        for i, v in enumerate((len(wins), w_mech, w30, w60, kb)):
            tot[i] += v
    md.append(f"| **итого** | | | | | | | | **{tot[0]}** | {tot[1]} | {tot[2]} | {tot[3]} | {tot[4]:.0f} |")
    allw = [w for t in tickets.values() for w in make_windows(t.entries, a.step_min)]
    ts = [e.ts for t in tickets.values() for e in t.entries]
    days = (max(ts) - min(ts)).total_seconds() / 86400 if ts else 0
    per_day = len(allw) / days if days else 0
    every = [r for rs in runs.values() for r in rs]
    sizes = sorted(len(j["prompt"]) for j in jobs)
    md += ["", f"Все запуски проекта: {len(every)}; цена по resolved_cost ${sum(r.cost for r in every):.0f}; "
               f"если складывать cost_usd (итог сессии с начала, при --resume считается повторно) ${sum(r.raw for r in every):.0f}.",
           f"Вся история: тикетов {len(tickets)}, окон (шаг {a.step_min} мин) {len(allw)} за {days:.1f} суток — {per_day:.0f} в сутки.",
           f"Обычные: кандидатов по правилу (done, 1–{NORMAL_MAX_RUNS} запусков, без возвратов Судьи) "
           f"{len(normal_pool(ticket_info(tickets, runs)))}; выбрано {NORMAL_N}, seed {NORMAL_SEED}."]
    if sizes:
        md.append(f"Промпт набора: средний {sum(sizes) / len(sizes):.0f} знаков, медиана {sizes[len(sizes) // 2]}, максимум {sizes[-1]}.")
    if a.fit:
        rows = [r for d in a.fit.split(",") if d for r in read_rows(Path(d).resolve())]
        al, be = fit_cost(rows)
        mean = sum(r["cost"] or 0 for r in rows) / len(rows) if rows else 0
        est_fit = sum(al + be * j["prompt_chars"] for j in jobs)
        md += ["", f"Оценка по {len(rows)} готовым вызовам (средний вызов ${mean:.4f}, размер промптов {min(r['prompt_chars'] for r in rows)}–"
                   f"{max(r['prompt_chars'] for r in rows)} знаков): окон {len(jobs)} × ${mean:.4f} = ${len(jobs) * mean:.2f}; "
                   f"по размеру промпта (${al:.5f} + ${be * 1000:.5f}/1000 знаков) = ${est_fit:.2f}.",
               f"В сутки по всей истории ({per_day:.0f} окон): ${per_day * mean:.2f} по среднему вызову."]
    if a.per_call:
        md.append(f"По заданной цене вызова ${a.per_call}: окон {len(jobs)} → ${len(jobs) * a.per_call:.2f}.")
    emit("\n".join(md))
    return 0


def cmd_prompt(a) -> int:
    alpha = Path(a.alpha).resolve()
    t = load_ticket(alpha, a.ticket)
    rs = load_runs(alpha).get(a.ticket, [])
    opts = prompt_opts(a)
    wins = make_windows(t.entries, a.step_min, opts["skip_roles"])
    w = next((w for w in wins if w.n == a.window), None)
    if not w:
        raise SystemExit(f"{a.ticket}: окон {len(wins)}, окна {a.window} нет")
    emit(build_prompt(t, w, stats_at(w.end, t.entries, rs, ticket_start(t.entries, rs)), **opts))
    return 0


# ------------------------------------------------------------------------------------------------------------
# Сводка
# ------------------------------------------------------------------------------------------------------------

def suppress(rows: list, hours: int = SUPPRESS_H) -> list:
    """Принятые находки тикета (rows по времени) после правила «не чаще раза в `hours` часов»."""
    out, last = [], None
    for r in rows:
        if r["outcome"] != "accepted":
            continue
        t = parse_ts(r["end"])
        if last is None or t - last >= timedelta(hours=hours):
            out.append(r)
            last = t
    return out


def summarize(rows: list) -> dict:
    """Итоги по тикетам и по набору из строк windows.jsonl (последняя строка на окно)."""
    last = {}
    for r in rows:
        last[r["key"]] = r
    by = {}
    for r in sorted(last.values(), key=lambda r: (r["ticket"], r["end"])):
        by.setdefault(r["ticket"], []).append(r)
    tickets = {}
    for tid, rs in by.items():
        acc = [r for r in rs if r["outcome"] == "accepted"]
        first = acc[0] if acc else None
        tickets[tid] = {
            "group": rs[0]["group"], "windows": len(rs), "cost": sum(r["cost"] or 0 for r in rs),
            "accepted": len(acc), "accepted_daily": len(suppress(rs)), "rejected": sum(r["outcome"] == "rejected" for r in rs),
            "errors": sum(r["outcome"] == "error" for r in rs), "total": rs[0]["ticket_total"],
            "first": first, "share": (first["spent"] / first["ticket_total"]) if first and first["ticket_total"] else None}
    return tickets


def cmd_report(a) -> int:
    out = Path(a.out).resolve()
    rows = read_rows(out)
    if not rows:
        raise SystemExit(f"{out}: нет windows.jsonl")
    last = {r["key"]: r for r in rows}
    calls = [r for r in last.values()]
    costs = [r["cost"] or 0 for r in calls]
    tk = summarize(rows)
    md = ["# Замер Надзирателя", "",
          "| тикет | группа | окон | первое окно с принятой находкой | вид | потрачено к тому моменту | доля цены тикета | часов от начала | "
          "принято | раз в сутки | отброшено | ошибок | цена замера $ |", "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for tid, v in sorted(tk.items()):
        f = v["first"]
        first = ([f"окно {f['window']} ({f['end'][5:16]})", f["kind"], f"${f['spent']:.2f} из ${v['total']:.2f}",
                  "—" if v["share"] is None else f"{v['share']:.0%}", f"{f['hours']:.1f}"] if f else ["—", "", "", "", ""])
        md.append("| " + " | ".join([tid, v["group"], str(v["windows"])] + first + [
            str(v["accepted"]), str(v["accepted_daily"]), str(v["rejected"]), str(v["errors"]), f"{v['cost']:.4f}"]) + " |")
    md.append("")
    bad = {t: v for t, v in tk.items() if v["group"] == "bad"}
    norm = {t: v for t, v in tk.items() if v["group"] == "normal"}
    if bad:
        early = [t for t, v in bad.items() if v["share"] is not None and v["share"] <= 0.5]
        md.append(f"Плохие: принятая находка раньше половины цены тикета — {len(early)} из {len(bad)} ({', '.join(early) or '—'}); "
                  f"вообще нашли {sum(1 for v in bad.values() if v['first'])} из {len(bad)}.")
    if norm:
        fa = [t for t, v in norm.items() if v["accepted"]]
        md.append(f"Обычные: тикетов с принятой находкой (ложная тревога) — {len(fa)} из {len(norm)} ({', '.join(fa) or '—'}); "
                  f"принятых находок всего {sum(v['accepted'] for v in norm.values())}, после правила раз в сутки {sum(v['accepted_daily'] for v in norm.values())}.")
    reasons = {}
    for r in calls:
        if r["outcome"] in ("rejected", "error"):
            reasons[r["reason"]] = reasons.get(r["reason"], 0) + 1
    inv = sum(1 for r in calls if r["outcome"] == "accepted" and r.get("invented_numbers"))
    md += ["", f"Вызовов (окон): {len(calls)}; цена замера ${sum(costs):.4f}; цена вызова: средняя ${sum(costs) / len(costs):.4f}, "
               f"минимум ${min(costs):.4f}, максимум ${max(costs):.4f}.",
           f"Исходы: тихо {sum(r['outcome'] == 'quiet' for r in calls)}, принято {sum(r['outcome'] == 'accepted' for r in calls)}, "
           f"отброшено {sum(r['outcome'] == 'rejected' for r in calls)}, ошибок {sum(r['outcome'] == 'error' for r in calls)}"
           + (f" (причины: {json.dumps(reasons, ensure_ascii=False)})" if reasons else "") + f"; принятых, где в action есть число, которого не было в промпте: {inv}.",
           f"Среднее время вызова {sum(r['dur'] for r in calls) / len(calls):.1f} с; токенов на вызов: вход {sum(r.get('in_tok') or 0 for r in calls) / len(calls):.0f}, "
           f"выход {sum(r.get('out_tok') or 0 for r in calls) / len(calls):.0f} (из них мысли {sum(r.get('think_tok') or 0 for r in calls) / len(calls):.0f})."]
    finds = [r for r in sorted(calls, key=lambda r: (r["ticket"], r["end"])) if r["outcome"] in ("accepted", "rejected")]
    if finds:
        md += ["", "## Находки", ""]
        for r in finds:
            md.append(f"- {r['ticket']} окно {r['window']} ({r['end'][5:16]}), {r['outcome']}"
                      + (f" [{r['reason']}]" if r["outcome"] == "rejected" else "") + f", {r['kind']}: «{r['quote']}» → {r['action']}")
    (out / "summary.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    emit("\n".join(md))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p, alpha=True):
        if alpha:
            p.add_argument("--alpha", required=True, help="каталог проекта (читается, не меняется)")
        p.add_argument("--step-min", type=int, default=15)
        p.add_argument("--skip-roles", default="", help="роли, чьи записи не видны и не открывают окно (через запятую)")
        p.add_argument("--max-kb", type=float, default=12, help="новые записи окна, КБ")
        p.add_argument("--ctx-lines", type=int, default=40, help="строк «хода тикета» до окна")
        p.add_argument("--entry-chars", type=int, default=2500)
        p.add_argument("--desc-chars", type=int, default=3000)

    def pick(p):
        p.add_argument("--tickets", default="", help="TK-087,TK-048; без него — набор по умолчанию")
        p.add_argument("--group", choices=("bad", "normal", "all"), default="all")

    pl = sub.add_parser("plan", help="окна и размер промптов набора без вызовов; оценка цены")
    common(pl)
    pick(pl)
    pl.add_argument("--fit", default="", help="каталоги готовых итогов (через запятую): оценка цены по ним")
    pl.add_argument("--per-call", type=float, default=0, help="цена вызова, $: оценка = окон × цена")
    r = sub.add_parser("run")
    common(r)
    pick(r)
    r.add_argument("--out", required=True)
    r.add_argument("--model", default="claude-haiku-5-5")
    r.add_argument("--effort", default="high")
    r.add_argument("--jobs", type=int, default=4)
    r.add_argument("--budget", type=float, default=1.0, help="стоп, когда цена этого запуска достигла лимита, $")
    r.add_argument("--per-call", type=float, default=0.25, help="потолок цены одного вызова, $ (--max-budget-usd)")
    r.add_argument("--timeout", type=int, default=300)
    p = sub.add_parser("report")
    p.add_argument("--out", required=True)
    rg = sub.add_parser("regrade", help="проверки заново по сохранённым ответам, без вызовов")
    rg.add_argument("--alpha", required=True)
    rg.add_argument("--out", required=True)
    pr = sub.add_parser("prompt", help="напечатать промпт окна")
    common(pr)
    pr.add_argument("--ticket", required=True)
    pr.add_argument("--window", type=int, required=True, help="номер окна тикета с записями, с 1")
    a = ap.parse_args(argv)
    return {"plan": cmd_plan, "run": cmd_run, "report": cmd_report, "prompt": cmd_prompt, "regrade": cmd_regrade}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
