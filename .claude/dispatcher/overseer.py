#!/usr/bin/env python3
"""Надзиратель (CP14; критерии 1, 3, 4, 5, 6, 10, 11): ловит то, что тормозам кода не видно, — деньги и время идут, а к цели
не движемся. Только stdlib. Его зовёт диспетчер отдельным коротким процессом не чаще раза в EVERY_S и только когда тикеты
менялись. Находка идёт Судье: запись «надзиратель» в логе тикета и `next: judge` (`to_judge`). CEO и владельца Надзиратель
не будит: CEO узнаёт от Судьи, если тот ответил `blocked` или `ask-owner`; цена и находки — строкой ежедневного отчёта.

Один взгляд (`look`):
  1. Правило кода «дорого»: открытый тикет потратил больше 6×, 12× медианы цены закрытых тикетов проекта (от MIN_CLOSED
     закрытых) — находка Судье на каждую ступень (в замере CP14 на истории проекта оно первым ловило TK-049 — на 12 % его цены).
  2. Haiku 5.5 xhigh (`agents/overseer.md`, без инструментов): по каждому открытому тикету с новыми записями лога (не больше
     MAX_TICKETS самых свежих) — «тихо» или находка из четырёх видов с дословной цитатой. Числа (запуски, $, часы, возвраты
     Судьи, масштаб закрытых тикетов) считает код; цель владельца (G-NNN) и, если в записях вопрос владельцу, реестр решений —
     в промпте. Находку код принимает, только если цитата есть в логе дословно; одна находка по тикету — не чаще раза в
     SUPPRESS_H часов.
  Каждый вызов — строка `overseer.log` (вид, цена): цена Надзирателя идёт в ежедневный отчёт как механика. Состояние —
  `overseer.json` (что уже прочитано, когда была находка, ступень «дорого»).

    python overseer.py --project <проект> [--dry-run]   # --dry-run: находки печатаются, CEO не пишется, состояние не двигается

Замер на истории проекта — `tools/overseer_eval.py` (то же ядро: разбор тикетов, числа, промпт, проверка цитаты).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import registry  # noqa: E402
import runs as RL  # noqa: E402
import ticket as T  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]  # корень плагина: agents/overseer.md
KINDS = ("не-движемся", "ломает-принятое", "вопрос-уже-решён", "не-служит-цели")
DEFAULT_TZ = timezone(timedelta(hours=4))  # запись без пояса — время владельца (GMT+4), как у логов проекта
MIN_QUOTE = 12  # короче этого цитата не доказательство
RECENT_H = 6
DAYS_SHOWN = 7

HDR = re.compile(r"^### (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?) (\S+)[ \t]*$", re.M)
RETURN = re.compile(r"^\[итог:\s*return\]")

# --- живая работа ----------------------------------------------------------------------------------------------
OPEN = ("todo", "in_progress", "in_review", "waiting", "blocked", "needs_owner")
EVERY_S = 15 * 60          # диспетчер зовёт не чаще
SUPPRESS_H = 24            # одна находка по тикету — не чаще раза в сутки (без этого модель повторяет её в каждом окне)
MAX_TICKETS = 12           # тикетов за один взгляд: самые свежие записи и MAX_COSTLY самых дорогих из остальных
MAX_COSTLY = 2             # дорогой дрейфующий тикет не всегда самый свежий
MODEL, EFFORT = "claude-haiku-5-5", "xhigh"  # замер 14.10 (CP14, история проекта): xhigh до половины цены ловит 6 из 10, high — 2
PER_CALL_USD = 0.05        # потолок цены одного вызова (`--max-budget-usd`); замер: средняя $0.0023
TIMEOUT_S = 180
COST_STEPS = (6, 12)       # ступени правила «дорого»: × медиана цены закрытых тикетов проекта
MIN_CLOSED = 5             # меньше закрытых тикетов — масштаба нет, правила «дорого» нет
SPIKE_H, SPIKE_K = 6, 3    # правило «всплеск»: траты тикета за SPIKE_H ч > SPIKE_K × обычных трат тикета за SPIKE_H ч
SPIKE_MIN = 30             # меньше стольких замеров «траты за окно» в истории проекта — правила «всплеск» нет
ACTIVE_H = 24              # тикет без записей и запусков за столько часов — брошен, а не тратит: его не смотрим
STATE_NAME, LOG_NAME = "overseer.json", "overseer.log"
SELF = "надзиратель"       # автор записей Надзирателя в логе тикета
GOAL_ID = re.compile(r"\bG-\d{3,}\b")
ASK = re.compile(r"\[итог:\s*ask-owner\]|needs_owner|владельц\w* вопрос|спрошу владельца|спрашиваю владельца", re.I)


def claude_bin() -> str:
    """`claude` берётся при вызове, не при импорте: в тестах `CLAUDE_BIN` подменён на несуществующий файл (conftest)."""
    return os.environ.get("CLAUDE_BIN") or shutil.which("claude") or "claude"


# ------------------------------------------------------------------------------------------------------------
# Разбор данных проекта
# ------------------------------------------------------------------------------------------------------------

def parse_ts(s: str) -> datetime:
    return T.parse_time(s, DEFAULT_TZ, strict=True)


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


def load_ticket(project: Path, tid: str) -> Ticket:
    tdir = project / ".claude" / "tickets"
    head, desc, log = split_ticket((tdir / f"{tid}.md").read_text(encoding="utf-8"))
    arch = tdir / "archive" / f"{tid}-log.md"
    old = parse_log(arch.read_text(encoding="utf-8")) if arch.exists() else []
    return Ticket(tid, head.get("title", tid), head.get("status", "?"), desc, merge_entries(old, parse_log(log)))


def ticket_ids(project: Path) -> list:
    return sorted(p.stem for p in (project / ".claude" / "tickets").glob("TK-*.md"))


def parse_runs(text: str) -> dict:
    """runs.log → {тикет: [Run по времени]}. `on_met` — команда проекта, не запуск модели. Цена — resolved_cost, иначе cost_usd."""
    out = {}
    for ln in text.splitlines():
        row = RL.split(ln)
        if row is None or row[2] == RL.ON_MET:
            continue
        stamp, tid, role, kv = row
        try:
            ts = parse_ts(stamp)
        except ValueError:
            continue
        cost, raw = RL.costs(kv)
        out.setdefault(tid, []).append(Run(ts, role, cost, kv.get("status", ""), raw))
    for lst in out.values():
        lst.sort(key=lambda r: r.ts)
    return out


def load_runs(project: Path) -> dict:
    p = project / ".claude" / "dispatcher" / "runs.log"
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


def closed_scale(tickets: dict, runs: dict) -> list:
    """Закрытые (`done`) тикеты проекта → [(время закрытия — последняя запись или запуск, $, запусков, часов)] по времени:
    масштаб «сколько обычно стоит тикет» для окна — только тикеты, закрытые до него (без заглядывания вперёд)."""
    out = []
    for t in tickets.values():
        rs = runs.get(t.tid, [])
        if t.status != "done" or not (t.entries or rs):
            continue
        start = ticket_start(t.entries, rs)
        end = max([e.ts for e in t.entries] + [r.ts for r in rs])
        out.append((end, sum(r.cost for r in rs), len(rs), (end - start).total_seconds() / 3600))
    return sorted(out)


def _pct(xs: list, q: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))]


def scale_text(scale: list, now: datetime, min_n: int = 5) -> str:
    """Строка масштаба: медиана и 90-й процентиль закрытых к `now` тикетов; меньше `min_n` — масштаба нет."""
    done = [x for x in scale if x[0] <= now]
    if len(done) < min_n:
        return ""
    cost, n, hours = ([x[i] for x in done] for i in (1, 2, 3))
    return (f"Закрытые тикеты проекта к этому моменту ({len(done)}): медиана — ${_pct(cost, 0.5):.2f}, запусков "
            f"{_pct(n, 0.5)}, часов {_pct(hours, 0.5):.1f}; 90-й процентиль — ${_pct(cost, 0.9):.2f}, запусков "
            f"{_pct(n, 0.9)}, часов {_pct(hours, 0.9):.1f}.")


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
                 entry_chars: int = 2500, desc_chars: int = 3000, skip_roles=(), scale: str = "", extra: str = "") -> str:
    before = [e for e in t.entries if e.ts < w.start and e.role not in skip_roles]
    shown = before[-ctx_lines:] if ctx_lines else []
    blocks, dropped = pick_new(w.new, int(max_kb * 1024), entry_chars)
    out = [f"Тикет {t.tid} — {t.title}", f"Сейчас: {w.end.isoformat()}", "",
           "## Цель (описание тикета)", cut(t.desc, desc_chars), ""] + ([extra, ""] if extra else []) + [
           "## Числа (считал код)", stats_text(stats)] + ([scale] if scale else []) + [""]
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
    res.update(kind=str(obj.get("kind", "")).strip(), quote=str(obj.get("quote", "")), action=str(obj.get("action", "")).strip(),
               decision=str(obj.get("decision", "")).strip())
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
# Живая работа: один взгляд по проекту
# ------------------------------------------------------------------------------------------------------------

def _dispatcher_dir(root: Path) -> Path:
    return Path(root) / ".claude" / "dispatcher"


def load_state(root: Path) -> dict:
    try:
        data = json.loads((_dispatcher_dir(root) / STATE_NAME).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(root: Path, st: dict) -> None:
    d = _dispatcher_dir(root)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / (STATE_NAME + ".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, d / STATE_NAME)


def log_row(root: Path, row: dict) -> None:
    d = _dispatcher_dir(root)
    d.mkdir(parents=True, exist_ok=True)
    with open(d / LOG_NAME, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def median_closed(scale: list, now: datetime):
    """Медиана цены тикетов, закрытых к `now`; меньше MIN_CLOSED — None. Медиана, а не 90-й процентиль: дорогие тикеты,
    закрываясь, поднимают процентиль (замер CP14, история проекта: $23 → $37 → $21 за неделю), медиану — почти нет."""
    done = [x[1] for x in scale if x[0] <= now]
    return _pct(done, 0.5) if len(done) >= MIN_CLOSED else None


def window_spends(runs: dict, now: datetime, hours: float = SPIKE_H) -> list:
    """Замеры «сколько тикет потратил за последние `hours` ч» на момент каждого запуска до `now` — по всем тикетам."""
    out, w = [], timedelta(hours=hours)
    for rs in runs.values():
        done, j, acc = [r for r in rs if r.ts <= now], 0, 0.0
        for r in done:
            acc += r.cost
            while done[j].ts <= r.ts - w:
                acc -= done[j].cost
                j += 1
            out.append(acc)
    return out


def spike_base(runs: dict, now: datetime):
    """Обычные траты тикета за SPIKE_H ч в проекте — медиана замеров `window_spends`; мало замеров — None. Ловит то, что
    накопленные траты не видят: тикет вдруг стал тратить в разы быстрее (замер CP14, история проекта: TK-049 — на 7 % его цены)."""
    xs = window_spends(runs, now)
    return _pct(xs, 0.5) if len(xs) >= SPIKE_MIN else None


def cost_step(spent: float, base) -> int:
    """Ступень правила «дорого»: наибольшее k из COST_STEPS, при котором spent > k × base; нет — 0."""
    return max((k for k in COST_STEPS if base and spent > k * base), default=0)


def goal_context(root: Path, desc: str) -> str:
    """Цели владельца (G-NNN), на которые ссылается описание тикета, — дословно из `.claude/goals/`."""
    parts = []
    for gid in dict.fromkeys(GOAL_ID.findall(desc or "")):
        p = Path(root) / ".claude" / "goals" / f"{gid}.md"
        if p.exists():
            parts.append(f"## Цель владельца {gid}\n" + cut(p.read_text(encoding="utf-8").strip(), 1500))
    return "\n\n".join(parts)


_WORD = re.compile(r"[А-Яа-яЁёA-Za-z]{5,}")


def decisions_context(root: Path, about: str, full: int = 5) -> str:
    """Реестр решений владельца для вида «вопрос-уже-решён»: все решения по строке «В-N · суть» (ранние не выпадают) и
    полный текст `full` решений, больше всех совпавших словами с новыми записями."""
    blocks = registry.decisions(Path(root))
    if not blocks:
        return ""
    words = {w.lower() for w in _WORD.findall(about or "")}
    heads, scored = [], []
    for did, block in blocks.items():
        gist = next((ln[5:].strip() for ln in block.splitlines() if ln.startswith("Суть:")), "")
        heads.append(f"{did} · {gist or norm_ws(block.splitlines()[1] if len(block.splitlines()) > 1 else '')[:120]}")
        hit = len(words & {w.lower() for w in _WORD.findall(block)})
        if hit:
            scored.append((hit, did, block))
    top = [b for _, _, b in sorted(scored, key=lambda x: -x[0])[:full]]
    return ("## Решения владельца (реестр: все по строке)\n" + "\n".join(heads)
            + ("\n\n## Подходящие по словам — полностью\n" + "\n\n".join(top) if top else ""))


def agents_file(work: Path) -> Path:
    """Определение Надзирателя из `agents/overseer.md` плагина — файлом для `claude --agents` (во временном каталоге
    вызова, не в проекте)."""
    p = Path(work) / "overseer-agent.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"overseer": load_agent("overseer")}, ensure_ascii=False), encoding="utf-8")
    return p


JUDGE_ASK = ("Судье: проверь по делу. Работа идёт к цели — `return` с указанием владельцу, что делать дальше; не идёт — "
             "`blocked` с причиной (CEO остановит или перепланирует) или `ask-owner --why-owner деньги`.")


def busy(root: Path, tid: str, tkt_next: str) -> bool:
    """Тикет сейчас нельзя передать Судье: идёт запуск роли (его итог перепишет `next`) или передача уже назначена."""
    try:
        active = json.loads((_dispatcher_dir(root) / "state.json").read_text(encoding="utf-8")).get("active_runs") or {}
    except (OSError, ValueError):
        active = {}
    return tid in active or bool((tkt_next or "").strip())


def to_judge(root: Path, tid: str, text: str, now: datetime) -> bool:
    """Находка — в лог тикета записью «надзиратель» и `next: judge`: Судью диспетчер запускает всегда, даже когда сессии
    CEO нет. Тикет занят (`busy`) — False, попробуем на следующем взгляде."""
    path = Path(root) / ".claude" / "tickets" / f"{tid}.md"
    if not path.exists():
        return True  # тикета больше нет — передавать некому
    tkt = T.read_ticket(path)
    if tkt.status not in OPEN:
        return True
    if busy(root, tid, tkt.header.get("next", "")):
        return False
    T.append_log(path, SELF, f"{text} {JUDGE_ASK}", now=now)
    T.write_header_updates(path, {"next": "judge"}, now=now)
    return True


def _key(tid: str, kind: str) -> str:
    return f"{tid}|{kind}"


def look(root: Path, now: datetime | None = None, call=None, route=None, dry: bool = False) -> list:
    """Один взгляд по проекту → строки итогов (они же в `overseer.log`). `call(prompt)` → ответ как у `call_overseer`;
    `route(тикет, текст)` → передано ли Судье (`to_judge`; занят — повтор на следующем взгляде). Ошибка вызова не двигает
    «прочитано»: тикет посмотрим в следующий раз. `dry` — ни лога, ни состояния, ни записей в тикеты."""
    root = Path(root)
    now = now or datetime.now().astimezone()
    if call is None:
        work = Path(tempfile.gettempdir()) / "rpv-overseer"
        af = agents_file(work)
        call = lambda prompt: call_overseer(prompt, af, work, MODEL, EFFORT, PER_CALL_USD, TIMEOUT_S)  # noqa: E731
    route = route or (lambda tid, text: True if dry else to_judge(root, tid, text, now))
    st = load_state(root)
    seen, found, steps = st.setdefault("seen", {}), st.setdefault("found", {}), st.setdefault("cost_step", {})
    st["pending"] = [p for p in st.get("pending", []) if not route(p["ticket"], p["text"])]  # прошлые, не переданные

    def emit(tid, text):
        if not route(tid, text):
            st["pending"].append({"ticket": tid, "text": text})

    tickets = {tid: load_ticket(root, tid) for tid in ticket_ids(root)}
    runs = load_runs(root)
    scale = closed_scale(tickets, runs)
    base = median_closed(scale, now)
    sbase = spike_base(runs, now)
    recent = now - timedelta(hours=ACTIVE_H)
    rows = []
    for tid, t in sorted(tickets.items()):
        rs = runs.get(tid, [])
        if t.status not in OPEN or not any(r.ts > recent for r in rs):
            continue
        spent = sum(r.cost for r in rs)
        k = cost_step(spent, base)
        if k > steps.get(tid, 0):
            emit(tid, f"Надзиратель, дорого: «{t.title}» ({tid}) потратил ${spent:.2f} — больше {k}× медианы цены закрытых "
                      f"тикетов проекта (${base:.2f}).")
            steps[tid] = k
            rows.append({"ts": now.isoformat(), "ticket": tid, "outcome": "cost", "kind": "дорого", "step": k,
                         "spent": round(spent, 4), "median": round(base, 4), "cost": 0.0})
        last_h = sum(r.cost for r in rs if r.ts > now - timedelta(hours=SPIKE_H))
        prev = found.get(_key(tid, "всплеск"))
        if sbase and last_h > SPIKE_K * sbase and not (prev and now - parse_ts(prev) < timedelta(hours=SUPPRESS_H)):
            emit(tid, f"Надзиратель, всплеск: «{t.title}» ({tid}) за {SPIKE_H} ч потратил ${last_h:.2f} — больше {SPIKE_K}× "
                      f"обычных трат тикета за {SPIKE_H} ч в проекте (${sbase:.2f}).")
            found[_key(tid, "всплеск")] = now.isoformat()
            rows.append({"ts": now.isoformat(), "ticket": tid, "outcome": "cost", "kind": "всплеск",
                         "spent": round(last_h, 4), "median": round(sbase, 4), "cost": 0.0})
    fresh = []
    for tid, t in tickets.items():
        if t.status in OPEN and t.entries:
            last = parse_ts(seen[tid]) if tid in seen else None
            new = [e for e in t.entries if (last is None or e.ts > last) and e.role != SELF]  # свои записи — не работа
            if new and new[-1].ts > recent:
                fresh.append((new[-1].ts, tid, new))
    fresh.sort(key=lambda x: x[0], reverse=True)
    picked = fresh[:MAX_TICKETS - MAX_COSTLY]
    costly = sorted(fresh[len(picked):], key=lambda x: -sum(r.cost for r in runs.get(x[1], [])))[:MAX_COSTLY]
    for _, tid, new in picked + costly:
        t, rs = tickets[tid], runs.get(tid, [])
        extra = goal_context(root, t.desc)
        if t.status == "needs_owner" or any(ASK.search(e.text) for e in new):
            extra = "\n\n".join(x for x in (extra, decisions_context(root, " ".join(e.text for e in new))) if x)
        prompt = build_prompt(t, Window(1, 0, new[0].ts, now, new), stats_at(now, t.entries, rs),
                              scale=scale_text(scale, now), extra=extra)
        res = call(prompt)
        row = {"ts": now.isoformat(), "ticket": tid, "cost": float(res.get("cost") or 0.0), "n_new": len(new)}
        if res.get("error"):
            row.update(outcome="error", reason=str(res.get("answer", ""))[:200])
            rows.append(row)
            continue
        g = grade(res["answer"], tid, t.entries, now, prompt)
        row.update(outcome=g["outcome"], reason=g["reason"], kind=g["kind"], quote=g["quote"], action=g["action"],
                   invented_numbers=g["invented_numbers"])
        seen[tid] = t.entries[-1].ts.isoformat()
        if g["outcome"] == "accepted":
            action = g["action"]
            if g["invented_numbers"]:  # критерий 4: число не из данных кода и лога не уходит дальше
                action = f"(совет Надзирателя не передан: в нём числа не из данных — {', '.join(g['invented_numbers'])})"
            decision = g.get("decision", "")
            if g["kind"] == "вопрос-уже-решён":
                known = registry.decisions(root)
                if decision in known:
                    action = f"решение уже есть: {decision} — {cut(norm_ws(known[decision]), 300)}"
                else:
                    row["decision_unknown"] = decision
            prev = found.get(_key(tid, g["kind"]))
            if prev and now - parse_ts(prev) < timedelta(hours=SUPPRESS_H):
                row["suppressed"] = True
            else:
                emit(tid, f"Надзиратель, {g['kind']}: «{t.title}» ({tid}) — «{norm_ws(g['quote'])}» → {action}")
                found[_key(tid, g["kind"])] = now.isoformat()
        rows.append(row)
    if not dry:
        for row in rows:
            log_row(root, row)
        st["last"] = now.isoformat()
        save_state(root, st)
    return rows


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    ap = argparse.ArgumentParser(description="Надзиратель: один взгляд по проекту")
    ap.add_argument("--project", required=True)
    ap.add_argument("--dry-run", action="store_true", help="находки напечатать; CEO, тикеты и состояние не трогать")
    a = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    root = Path(a.project).expanduser().resolve()
    if a.dry_run:
        rows = look(root, route=lambda tid, text: print(text) or True, dry=True)
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return 0
    look(root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
