#!/usr/bin/env python3
"""Ежедневный отчёт владельцу (CP10, критерии 2–7, 9–13, 16): одно сообщение в день в Telegram, словами результата, не
больше 25 строк. Собирает код, пересказ принятого — помощник `reporter`. Только stdlib.

    report.py [--project <путь>] [--dry-run] [--since <время>] [--until <время>]

Без флагов: собрать отчёт за период, записать `<проект>/.claude/dispatcher/reports/<дата>.md` (всегда; под текстом —
источники, в сообщение они не входят), отправить владельцу (`notify.py`) и напечатать. `--dry-run` — только напечатать: не
шлёт, файла нет. Период — от `last_report_at` в state.json (метку двигает диспетчер, а не этот скрипт: state.json пишет
он один; ручной запуск её не двигает, плановый отчёт покроет и этот период) до сейчас; метки нет — последние сутки.

Часть А — принято проверяющим: тикеты `done`, закрытые за период (`updated` в шапке), чей последний вердикт Судьи в логе —
accept (`[итог: accept]` или «ПРИНЯТО PR»); по целям владельца (`Ссылка на задачу владельца: G-NNN`). Тикеты самого Судьи
и закрытые без него не в счёте. Строку на цель пишет `reporter` (`claude -p --agent reporter`); нет claude, сбой, таймаут,
номер тикета в ответе или число, которого нет в материале, — названия тикетов: отчёт уходит всегда.

Часть Б — измерения «что · сколько · норма», кодом, без модели. Нет источника — «нет данных», числа не выдумываются.
Определения — README, «Ход работы владельцу».
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cfg as cfg_mod  # noqa: E402
import downtime  # noqa: E402
import errors as errors_mod  # noqa: E402
import haiku_aux as HA  # noqa: E402
import hide  # noqa: E402
import notify  # noqa: E402
import project as P  # noqa: E402
import registry  # noqa: E402
import routes  # noqa: E402
import runs as RL  # noqa: E402
import ticket as T  # noqa: E402

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
AGENTS_DIR = PLUGIN_ROOT / "agents"
SEMVER = re.compile(r"\d+\.\d+\.\d+")
MAX_LINES = 25             # строк в сообщении владельцу
CLAUDE_TIMEOUT_S = 240.0   # пересказ reporter; дольше — отчёт уходит с названиями тикетов
BUDGET_USD = 1.0           # потолок цены одного пересказа (`--max-budget-usd`)
SUMMARY_MAX = 240          # знаков в строке пересказа; длиннее — это уже не одна строка, берём названия
SLACK = timedelta(seconds=30)   # запись роли в конце запуска может оказаться чуть позже отметки конца в runs.log
WEEK = timedelta(days=7)

GOAL_REF = re.compile(r"Ссылка на задачу владельца:\s*(\S+)")
VERDICT = re.compile(r"(?:\[итог: (accept|return)\]|ПРИНЯТО PR)")
RESULT = re.compile(r"\[итог: ([\w-]+)\]")
RETURN_BLOCKED = re.compile(r"\[итог: (?:return|blocked)\]")
CONFLICT = re.compile(r"конфликт|rebase|merge conflict", re.I)
NUM = re.compile(r"\d+(?:[.,]\d+)?")
REF_IN_TEXT = re.compile(r"\b(?:TK|G|E)-\d+|В-\d+")


@dataclass
class Period:
    root: Path
    since: datetime
    until: datetime
    tickets: list

    def inside(self, ts) -> bool:
        return bool(ts) and self.since < ts <= self.until


# --- чтение ------------------------------------------------------------------------------------

def _ts(text):
    """Время из строки (ISO или `ГГГГ-ММ-ДД ЧЧ:ММ`); без пояса — местное, как в реестре целей и журналах. Не разобрать — None."""
    try:
        dt = datetime.fromisoformat(str(text).strip())
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.astimezone()


def _span(seconds: float) -> str:
    m = int(seconds // 60)
    if m >= 1440:
        d, rest = divmod(m, 1440)
        return f"{d} дн {rest // 60} ч"
    return f"{m // 60} ч {m % 60:02d} мин" if m >= 60 else f"{m} мин"


def _cut(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n - 1].rstrip() + "…"


def _dispatcher_dir(root: Path) -> Path:
    return root / ".claude" / "dispatcher"


def _read_lines(path: Path):
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None


def load_tickets(root: Path) -> list:
    """Все тикеты проекта; к логу добавлены записи из архива (`tickets/archive/<ID>-log.md`) — старше последних восьми."""
    out = []
    for path in T.list_tickets(root / ".claude" / "tickets"):
        try:
            tkt = T.read_ticket(path)
            arch = path.parent / T.ARCHIVE_DIRNAME / f"{path.stem}-log.md"
            if arch.exists():
                tkt.log = T.parse_text("---\nid: x\n---\n\n## Лог\n" + arch.read_text(encoding="utf-8")).log + tkt.log
            out.append(tkt)
        except Exception:  # noqa: BLE001 — битый тикет отчёт не останавливает
            continue
    return out


def last_verdict(tkt):
    """Последний вердикт Судьи в логе: ("accept"|"return", запись) или None."""
    for e in reversed(tkt.log):
        m = VERDICT.match(e.text.strip())
        if m and T.author_is(e.author, "judge"):
            return (m.group(1) or "accept"), e
    return None


# --- часть А: что принято ----------------------------------------------------------------------

def _plain(title: str) -> str:
    """Название тикета без номеров TK-N: владельцу они не нужны (источники — по запросу, в файле отчёта)."""
    t = " ".join(re.sub(r"\bTK-\d+\b[:,]?", "", title or "").split())
    return re.sub(r"\(\s*\)", "", t).strip(" -—:(),") or (title or "")


def _goal_title(path: Path) -> str:
    return next((ln[2:].strip() for ln in path.read_text(encoding="utf-8").splitlines() if ln.startswith("# ")), "")


def group_results(p: Period) -> list:
    """[(G-NNN или "", название цели, [{id, title, why}])]: принято Судьёй и закрыто за период, по целям; «без цели» — последним."""
    goals = registry.goals(p.root)
    groups = {}
    for t in p.tickets:
        v = last_verdict(t)
        if t.status != "done" or t.owner == "judge" or not v or v[0] != "accept" or not p.inside(_ts(t.header.get("updated"))):
            continue
        m = GOAL_REF.search(t.description)
        gid = m.group(1) if m and m.group(1) in goals else ""
        text = v[1].text.strip()
        why = text.split(" — ", 1)[1].strip() if text.startswith("[итог:") and " — " in text else ""
        groups.setdefault(gid, []).append({"id": t.id, "title": _plain(t.header.get("title") or t.id), "why": why})
    return [(g, _goal_title(goals[g]) if g else "", groups[g]) for g in sorted(groups, key=lambda g: (g == "", g))]


def _facts(groups: list) -> str:
    out = []
    for i, (_, goal, items) in enumerate(groups, 1):
        out.append(f"Группа {i} — " + (f"цель «{goal}»" if goal else "работа без цели (по решению владельца)") + ":")
        out += [f"- {it['title']}" + (f" (проверяющий: {it['why']})" if it["why"] else "") for it in items]
    return "\n".join(out)


def _agent_def(name: str) -> dict:
    """agents/<name>.md → определение для `claude --agents` (как tools/helper_eval.py): модель и усилие — из фронтматтера."""
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", (AGENTS_DIR / f"{name}.md").read_text(encoding="utf-8"), re.S)
    meta = {k.strip(): v.strip().strip('"') for k, _, v in (ln.partition(":") for ln in m.group(1).splitlines())}
    return {"description": meta.get("description", name), "prompt": m.group(2).strip(),
            "model": meta["model"], "effort": meta["effort"]}


def ask_reporter(prompt: str, runner=hide.run, claude: str | None = None):
    """Ответ `claude -p --agent reporter` (весь JSON `--output-format json`: result, total_cost_usd) или None. Запуск из
    временного каталога вне проекта и без RPV_* в окружении (как haiku_aux): ни CLAUDE.md, ни хуков, ни инструментов."""
    claude = claude if claude is not None else HA._claude()
    if not claude:
        return None
    env = {k: v for k, v in os.environ.items() if not k.startswith("RPV_") and k != "CLAUDE_PROJECT_DIR"}
    try:
        agent = _agent_def("reporter")
        with tempfile.TemporaryDirectory() as tmp:
            defs = Path(tmp) / "agents.json"
            defs.write_text(json.dumps({"reporter": agent}, ensure_ascii=False), encoding="utf-8")
            cmd = [claude, "-p", "--agents", str(defs), "--agent", "reporter", "--model", agent["model"], "--effort",
                   agent["effort"], "--output-format", "json", "--no-session-persistence", "--tools", "",
                   "--max-budget-usd", str(BUDGET_USD)]
            r = runner(cmd, input=prompt.encode("utf-8"), capture_output=True, timeout=CLAUDE_TIMEOUT_S, cwd=tmp, env=env)
        out = json.loads((r.stdout or b"").decode("utf-8", "replace"))
    except (OSError, KeyError, AttributeError, ValueError, subprocess.SubprocessError):
        return None
    return out if r.returncode == 0 and isinstance(out, dict) and not out.get("is_error") and out.get("result") else None


def _sane(line, facts: str) -> str:
    """Строка пересказа годна: одна строка, без номеров тикетов/целей/решений и без чисел, которых нет в материале."""
    line = " ".join(str(line or "").split())
    if not line or len(line) > SUMMARY_MAX or REF_IN_TEXT.search(line):
        return ""
    known = {n.replace(",", ".") for n in NUM.findall(facts)}
    return line if all(n.replace(",", ".") in known for n in NUM.findall(line)) else ""


def summarize(groups: list, p: Period, ask=None):
    """({номер группы: (строка, откуда)}, цена пересказа). Строка — от `reporter`; не прошла проверку или его нет — названия тикетов."""
    facts, cost, answer = _facts(groups), None, {}
    if groups:
        out = (ask or ask_reporter)(f"Запуск «ежедневный отчёт». Сейчас: {p.until:%Y-%m-%d %H:%M} (местное). Принятые "
                                    f"проверяющим результаты по целям владельца; на каждую группу — одна строка.\n\n{facts}")
        cost = (out or {}).get("total_cost_usd")
        m = re.search(r"\{.*\}", str((out or {}).get("result") or ""), re.S)
        try:
            answer = json.loads(m.group(0)) if m else {}
        except ValueError:
            answer = {}
        answer = answer if isinstance(answer, dict) else {}
    res = {}
    for i, (_, _, items) in enumerate(groups, 1):
        line = _sane(answer.get(str(i)), facts)
        res[i] = (line, "reporter") if line else (_cut("; ".join(it["title"] for it in items), SUMMARY_MAX), "названия тикетов")
    return res, cost


# --- часть Б: измерения (значение строки; подпись и норма — в MEASURES) -------------------------

def _nodata(why: str) -> str:
    return f"нет данных ({why})"


def m_goal_time(p: Period) -> str:
    """Критерий 3. Время «цель поставлена → принят результат» (`set` → `accepted` в `goals/G-NNN.md`) у целей, принятых за
    период, и как оно соотносится со средним у всех целей, принятых раньше."""
    done = []
    for path in registry.goals(p.root).values():
        h = registry.goal_header(path)
        s, a = _ts(h.get("set")), _ts(h.get("accepted"))
        if h.get("status") == "accepted" and s and a and a >= s:
            done.append((a, (a - s).total_seconds(), _goal_title(path)))
    now = [d for d in done if p.inside(d[0])]
    prev = [d[1] for d in done if d[0] <= p.since]
    if not now:
        return _nodata("цель за период не принималась")
    avg = sum(d[1] for d in now) / len(now)
    val = f"«{_cut(now[0][2], 40)}» — {_span(avg)}" if len(now) == 1 else f"в среднем {_span(avg)} по {len(now)} целям"
    if not prev:
        return val + "; раньше целей не принималось"
    old = sum(prev) / len(prev)
    pct = round(100 * abs(avg - old) / old) if old else 0
    return val + f"; раньше в среднем {_span(old)}" + (f" — {'быстрее' if avg < old else 'медленнее'} на {pct}%" if pct else " — так же")


def m_deadlines(p: Period) -> str:
    """Критерий 4. Среди тикетов с названным владельцу сроком (`due_first`): в срок — принято Судьёй (последний вердикт accept,
    тикет `done`) не позже срока; не в срок — принято позже или срок прошёл, а принятия нет. За всё время; срок впереди — не в счёте."""
    kept = missed = 0
    for t in p.tickets:
        due = _ts(t.header.get("due_first"))
        if not due:
            continue
        v = last_verdict(t)
        if t.status == "done" and v and v[0] == "accept":
            kept, missed = kept + (v[1].ts <= due), missed + (v[1].ts > due)
        elif p.until > due:
            missed += 1
    n = kept + missed
    return f"в срок {kept} из {n} ({round(100 * kept / n)}%)" if n else _nodata("сроки владельцу не называли")


def m_questions(p: Period) -> str:
    """Критерий 5. Вопросы владельцу за период по поводам: записи Судьи `[итог: ask-owner] повод: <повод>` в логах тикетов."""
    if not p.tickets:
        return _nodata("тикетов нет")
    got = Counter()
    for t in p.tickets:
        for e in t.log:
            if p.inside(e.ts) and T.author_is(e.author, "judge") and e.text.strip().startswith("[итог: ask-owner]"):
                m = re.search(r"повод: ([\w-]+)", e.text)
                got[m.group(1) if m else "без повода"] += 1
    by = ", ".join(f"{r} {got[r]}" for r in (*routes.OWNER_REASONS, "без повода") if got[r])
    return f"{sum(got.values())}" + (f" ({by})" if by else "")


def m_repeats(p: Period) -> str:
    """Критерий 6. Повторы уже принятых решений владельца за период: строки «Повторено владельцем · <время>» в блоках
    `.claude/decisions.md` (`registry.py repeat`)."""
    if not registry.decisions_file(p.root).exists():
        return _nodata("реестр решений не заведён")
    refs = [ref for ref, block in registry.decisions(p.root).items() for ln in block.splitlines()
            if (m := re.match(rf"{registry.REPEAT} · (\d{{4}}-\d\d-\d\d \d\d:\d\d)", ln)) and p.inside(_ts(m.group(1)))]
    return f"{len(refs)}" + (f" ({', '.join(sorted(set(refs)))})" if refs else "")


def m_regressions(p: Period) -> str:
    """Критерий 7. Отказы приёмки из-за упавшей проверки исправленной ошибки (`regressions.log`, пишет `errors.gate`) и ошибки,
    заведённые заново: новая запись за период, чьё название похоже (≥ 0,8, как у дубля тикета) на более раннюю. Строки с
    пометкой в четвёртом поле (`infra` — проверка не запустилась, `ok`) — не регресс, не в счёте."""
    lines = _read_lines(_dispatcher_dir(p.root) / "regressions.log")
    if lines is None and not errors_mod.errors_dir(p.root).is_dir():
        return _nodata("реестр ошибок не заведён")
    refused = sum(1 for ln in lines or [] if len(ln.split("\t")) < 4 and p.inside(_ts(ln.split("\t")[0])))
    found = []
    for path in errors_mod.errors(p.root).values():
        h = errors_mod.header(path)
        if ts := _ts(h.get("found")):
            found.append((ts, " ".join(h.get("title", "").lower().split())))
    found.sort()
    again = sum(1 for i, (ts, title) in enumerate(found)
                if p.inside(ts) and any(difflib.SequenceMatcher(None, title, old).ratio() >= T.DUP_RATIO for _, old in found[:i]))
    return f"{refused + again} (отказов приёмки из-за регресса {refused}, заведено заново {again})"


def m_idle(p: Period) -> str:
    """Критерий 10. Минуты простоя при готовой работе по календарным дням периода (`downtime` в state.json, сумма корзин
    `downtime.total_min`; пауза лимита сессии вне счёта). Дня нет в счёте — диспетчер в тот день не работал."""
    try:
        days = json.loads((_dispatcher_dir(p.root) / "state.json").read_text(encoding="utf-8")).get("downtime") or {}
    except (OSError, ValueError):
        days = {}
    got = [(d, downtime.total_min(days[d])) for d in sorted({p.since.date().isoformat(), p.until.date().isoformat()}) if d in days]
    if not got:
        return _nodata("диспетчер не вёл счёт")
    return f"{sum(m for _, m in got):.0f} мин" + (" (" + ", ".join(f"{d[8:]}.{d[5:7]}: {m:.0f}" for d, m in got) + ")" if len(got) > 1 else "")


def read_runs(root: Path):
    """[{ts, tid, role, reason, status, cost}] — запуски ролей из runs.log по времени; нет файла — None. Цена — `resolved_cost`
    (начислено именно этому запуску), а не `cost_usd` (итог сессии с начала: при `--resume` повтора считал бы дважды)."""
    lines = _read_lines(_dispatcher_dir(root) / "runs.log")
    if lines is None:
        return None
    rows = []
    for ln in lines:
        row = RL.split(ln)
        ts = _ts(row[0]) if row else None
        if not ts or row[2] == RL.ON_MET:    # on_met — команда проекта, не запуск модели
            continue
        _, tid, role, kv = row
        rows.append({"ts": ts, "tid": tid, "role": role, "reason": kv.get("reason", ""),
                     "status": kv.get("status", "?"), "cost": RL.costs(kv)[0]})
    return sorted(rows, key=lambda r: r["ts"])


def _rework(t, runs: list) -> set:
    """(тикет, конец) запусков владельца после возврата Судьи: от `[итог: return]` до его следующего итога (кроме `continue`);
    запуск, давший этот итог, тоже переделка."""
    out = set()
    if not t.owner or t.owner == "judge":
        return out
    results = sorted(e.ts for e in t.log if T.author_is(e.author, t.owner) and (m := RESULT.match(e.text.strip()))
                     and m.group(1) != "continue")
    mine = [r["ts"] for r in runs if r["tid"] == t.id and r["role"] == t.owner]
    for e in t.log:
        if T.author_is(e.author, "judge") and e.text.strip().startswith("[итог: return]"):
            end = next((x for x in results if x > e.ts), None)
            for ts in (x for x in mine if x > e.ts):
                out.add((t.id, ts))
                if end is not None and ts >= end:
                    break
    return out


def label_runs(runs: list, tickets: list) -> None:
    """Метка `kind` каждому запуску; запуск попадает в первый подходящий класс:
    сбой — `status` не ok (таймаут, нет вывода, остановлен); холостой — status ok, а роль не оставила записи в логе тикета между
    концом прошлого запуска этой роли по тикету и концом этого (как `watch.triage_stalls`); повтор — `reason=retry`;
    переделка — запуск владельца после возврата Судьи (`_rework`); иначе работа."""
    by_id = {t.id: t for t in tickets}
    rework = set().union(*(_rework(t, runs) for t in tickets))
    last_end = {}
    for r in runs:
        t, key = by_id.get(r["tid"]), (r["tid"], r["role"])
        prev, last_end[key] = last_end.get(key), r["ts"]
        if r["status"] != "ok":
            r["kind"] = "сбои"
        elif t and not any(T.author_is(e.author, r["role"]) and (prev is None or e.ts > prev) and e.ts <= r["ts"] + SLACK
                           for e in t.log):
            r["kind"] = "холостые запуски"
        elif r["reason"] == "retry":
            r["kind"] = "повторы"
        elif (r["tid"], r["ts"]) in rework:
            r["kind"] = "переделки после возврата Судьи"
        else:
            r["kind"] = "работа"


def m_cost(p: Period) -> str:
    """Критерий 11. Расход за период по `runs.log` и доля «механики»: сбои + холостые запуски + повторы + переделки
    (классы — `label_runs`) в расходе; остальное — работа."""
    runs = read_runs(p.root)
    if runs is None:
        return _nodata("runs.log нет")
    label_runs(runs, p.tickets)
    mine = [r for r in runs if p.inside(r["ts"])]
    if not mine:
        return _nodata("запусков за период не было")
    total, by = sum(r["cost"] for r in mine), Counter()
    for r in mine:
        by[r["kind"]] += r["cost"]
    if not total:
        return f"$0.00, запусков: {len(mine)}"
    parts = ", ".join(f"{k} {round(100 * by[k] / total)}%" for k in ("сбои", "холостые запуски", "повторы", "переделки после возврата Судьи") if by[k])
    return f"${total:.2f}, на механику {round(100 * (total - by['работа']) / total)}%" + (f" ({parts})" if parts else "")


def m_jobs(p: Period) -> str:
    """Критерий 12. Задания на машинах за период из `jobs.jsonl` (строка на ожидание: `ts`, `ticket`, `wait_for`, `outcome`
    met|overdue, `rc`): подхвачено вовремя и срок вышел без итога."""
    lines = _read_lines(_dispatcher_dir(p.root) / "jobs.jsonl")
    if lines is None:
        return _nodata("журнала заданий jobs.jsonl нет")
    got = Counter()
    for ln in lines:
        try:
            j = json.loads(ln)
        except ValueError:
            continue
        if isinstance(j, dict) and p.inside(_ts(j.get("ts"))):
            got[j.get("outcome")] += 1
    if not got["met"] + got["overdue"]:
        return "за период заданий не было"
    return f"кончились и подхвачены {got['met']}, срок вышел без итога {got['overdue']}"


def m_conflicts(p: Period) -> str:
    """Критерий 13. Столкновения параллельной работы за период: записи лога со словами «конфликт», «rebase» или «merge conflict»,
    если это итог `return`/`blocked` или запись `ci` (merge_rule: «PR конфликтует с main»). Счёт по тексту — возможны лишние."""
    if not p.tickets:
        return _nodata("тикетов нет")
    return str(sum(1 for t in p.tickets for e in t.log
                   if p.inside(e.ts) and CONFLICT.search(e.text) and (RETURN_BLOCKED.match(e.text.strip()) or T.author_is(e.author, "ci"))))


def m_plugin(p: Period) -> str:
    """Критерий 16. Версии плагина, поставленные за 7 дней: соседние каталоги `…/<плагин>/X.Y.Z` в кэше Claude Code с изменением
    за неделю. Запуск не из кэша (каталог плагина не версия) — версии нигде не копятся."""
    if not SEMVER.fullmatch(PLUGIN_ROOT.name):
        return _nodata("плагин запущен не из кэша Claude Code, версии не копятся")
    new = sorted((d.stat().st_mtime, d.name) for d in PLUGIN_ROOT.parent.iterdir()
                 if d.is_dir() and SEMVER.fullmatch(d.name) and d.stat().st_mtime > (p.until - WEEK).timestamp())
    return f"новых версий: {len(new)}" + (f" ({', '.join(n for _, n in new)})" if new else "") + f"; сейчас {PLUGIN_ROOT.name}"


def m_overseer(p: Period) -> str:
    """CP14, критерии 1 и 11. Находки Надзирателя, переданные Судье за период (правила «дорого» и «всплеск» и находки
    Haiku, кроме повторов в пределах суток), и цена его вызовов — механика — из `overseer.log`."""
    lines = _read_lines(_dispatcher_dir(p.root) / "overseer.log")
    if lines is None:
        return _nodata("Надзиратель не работал: overseer.log нет")
    looks, cost, kinds = 0, 0.0, Counter()
    for ln in lines:
        try:
            j = json.loads(ln)
        except ValueError:
            continue
        if not isinstance(j, dict) or not p.inside(_ts(j.get("ts"))):
            continue
        cost += float(j.get("cost") or 0.0)
        if j.get("outcome") == "cost":
            kinds["дорого"] += 1
            continue
        looks += 1
        if j.get("outcome") == "accepted" and not j.get("suppressed"):
            kinds[j.get("kind") or "?"] += 1
    found = sum(kinds.values())
    return (f"находок Судье {found}" + (f" ({', '.join(f'{k} {v}' for k, v in kinds.most_common())})" if found else "")
            + f", взглядов {looks}, ${cost:.2f}")


# (что, мера, норма) — по строке сообщения; порядок — как в README
MEASURES = (
    ("Время от цели до принятого результата", m_goal_time, "падает от цели к цели"),
    ("Сроки", m_deadlines, "все названные сроки сбываются"),
    ("Вопросы вам", m_questions, "единицы в день, каждый по одному из четырёх поводов"),
    ("Повторы ваших решений", m_repeats, "0"),
    ("Повторы ошибок", m_regressions, "0"),
    ("Простой при готовой работе", m_idle, "около 0"),
    ("Расход", m_cost, "большая часть — работа"),
    ("Задания на машинах", m_jobs, "потерь 0"),
    ("Столкновения параллельной работы", m_conflicts, "около 0"),
    ("Правки плагина за неделю", m_plugin, "около 0"),
    ("Надзиратель", m_overseer, "находки — по делу, цена — центы в день"),
)


def _measure(what: str, fn, norm: str, p: Period) -> str:
    try:
        value = fn(p)
    except Exception as e:  # noqa: BLE001 — одна битая мера не должна лишить владельца отчёта
        value = _nodata(type(e).__name__)
    return f"• {what} · {value} · норма: {norm}"


# --- сборка и доставка -------------------------------------------------------------------------

def _config_changes(p: Period) -> list:
    try:
        return cfg_mod.report_lines(p.root, p.since, p.until)[:5]
    except Exception:  # noqa: BLE001 — битый журнал не лишает владельца отчёта
        return []


def build(p: Period, ask=None):
    """(текст для владельца, подробности для файла отчёта). Текст — не больше MAX_LINES строк."""
    try:
        groups, empty = group_results(p), "Принятых проверяющим результатов за период нет."
    except Exception as e:  # noqa: BLE001 — не прочитали тикеты: отчёт уходит, а «принятого нет» не врём
        groups, empty = [], _nodata(f"принятое не прочитано: {type(e).__name__}")
    sums, cost = summarize(groups, p, ask)
    measures = [_measure(what, fn, norm, p) for what, fn, norm in MEASURES]
    changes = _config_changes(p)                # TK-007: правки настроек CEO с цитатой; нет правок — нет раздела
    room = MAX_LINES - (5 + len(measures) + (len(changes) + 2 if changes else 0))   # шапка, пустые строки, заголовки частей
    items = [f"• «{_cut(goal, 50)}» — {sums[i][0]}" if goal else f"• Без цели — {sums[i][0]}"
             for i, (_, goal, _) in enumerate(groups, 1)] or [empty]
    if len(items) > room:
        items = items[:room - 1] + [f"…и ещё {len(items) - room + 1}"]
    text = "\n".join([f"Отчёт за {p.since:%d.%m %H:%M} — {p.until:%d.%m %H:%M}", "", "Принято проверяющим:", *items, "",
                      "Измерения (что · сколько · норма):", *measures,
                      *(["", "CEO изменил работу команды:", *changes] if changes else [])])
    details = [f"Период: {p.since:%Y-%m-%d %H:%M} — {p.until:%Y-%m-%d %H:%M}", "",
               "Источники принятого (по запросу владельца; в сообщение не входят):"]
    details += [f"- {gid or 'без цели'}: {', '.join(it['id'] for it in its)} — строка: {sums[i][1]}"
                for i, (gid, _, its) in enumerate(groups, 1)] or ["- принятого нет"]
    if cost is not None:
        details.append(f"Пересказ reporter стоил ${float(cost):.4f}")
    return text, "\n".join(details)


def default_since(root: Path, until: datetime) -> datetime:
    """Начало периода: `last_report_at` из state.json, иначе сутки назад."""
    try:
        last = json.loads((_dispatcher_dir(root) / "state.json").read_text(encoding="utf-8")).get("last_report_at")
    except (OSError, ValueError):
        last = None
    since = _ts(last) if last else None
    return since if since and since < until else until - timedelta(days=1)


def due(last: str, now: datetime, at: str = "10:00") -> bool:
    """Пора слать: сегодняшний срок `at` (ЧЧ:ММ, местное) наступил, а прошлый отчёт `last` был раньше него. Диспетчер стоял в срок —
    отчёт уйдёт на первом тике после и один раз (после него `last` уже позже срока); раньше срока не уйдёт никогда."""
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", (at or "").strip())
    h, mi = (int(m.group(1)), int(m.group(2))) if m and int(m.group(1)) < 24 and int(m.group(2)) < 60 else (10, 0)
    slot = now.replace(hour=h, minute=mi, second=0, microsecond=0)
    return now >= slot and T.parse_dt(last) < slot


def deliver(root: Path, text: str, details: str, until: datetime, send=None):
    """Файл отчёта (всегда) и отправка; файл не записался — отправка всё равно идёт. -> (путь файла или None, отправлено)."""
    f = _dispatcher_dir(root) / "reports" / f"{until:%Y-%m-%d}.md"
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        T.atomic_write_text(f, f"{text}\n\n---\n{details}\n")
    except OSError as e:
        print(f"[report] файл отчёта не записан: {e}", file=sys.stderr)
        f = None
    return f, (send or notify.send)(text, root)


def main(argv=None) -> int:
    P.utf8_stdio()
    ap = argparse.ArgumentParser(prog="report.py", description=__doc__.split("\n\n")[0])
    ap.add_argument("--project", default=None)
    ap.add_argument("--dry-run", action="store_true", help="только напечатать: не отправлять, файла не писать")
    ap.add_argument("--since", default=None, help="начало периода (ISO); по умолчанию last_report_at из state.json")
    ap.add_argument("--until", default=None, help="конец периода (ISO); по умолчанию сейчас")
    a = ap.parse_args(argv)
    try:
        root = Path(a.project).expanduser().resolve() if a.project else P.resolve_project()
    except P.ProjectNotFound:
        print(f"[report] ошибка: {P.NOT_FOUND_HINT}", file=sys.stderr)
        return 2
    until = _ts(a.until) if a.until else datetime.now().astimezone()
    since = _ts(a.since) if a.since else (default_since(root, until) if until else None)
    if not until or not since or since >= until:
        print("--since/--until: нужны времена ISO, начало раньше конца", file=sys.stderr)
        return 2
    text, details = build(Period(root, since, until, load_tickets(root)))
    print(text)
    if a.dry_run:
        return 0
    f, ok = deliver(root, text, details, until)
    print(f"\n[{'отправлено владельцу' if ok else 'не отправлено — причина в notify.log'}; файл: {f or 'не записан'}]")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
