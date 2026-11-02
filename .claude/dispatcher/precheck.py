"""TK-091 (В-36, разбор тупки П1/П4/П6): проверки перед сдачей `result pr|done` — то, что роль иначе узнаёт от Судьи уже
возвратом. Два отказа: (1) голова ветки не сливается с main без конфликта (`git merge-tree`); (2) после старта сессии роли
в логе появилась запись `ceo`/`judge`, на которую роль не ответила цитатой. Обе проверки молчат, если проверить нечем
(нет git, нет main, нет головы в репозитории, не задан старт сессии): отказ — только по найденному, не по неизвестному."""
from __future__ import annotations

import re
import subprocess
from datetime import datetime
from pathlib import Path

import hide
import project as P
import ticket as T

BASES = ("main", "master")
GIT_TIMEOUT = 30
QUOTE_MIN = 20  # знаков подряд из записи, по которым видно, что роль её прочла
ORDER_AUTHORS = ("ceo", "judge")
CHECKED = ("pr", "done")


def _git(root: Path, *argv: str, timeout: int = GIT_TIMEOUT):
    try:
        return hide.git(root, *argv, timeout=timeout, text=True)
    except (OSError, subprocess.SubprocessError):
        return None


def _ok(proc) -> bool:
    return proc is not None and proc.returncode == 0


def base_ref(root: Path, fetch: bool = True) -> str:
    """`origin/main` (или `origin/master`), а нет такого — локальная `main`/`master`; пусто — базы нет. Перед выбором один
    быстрый `git fetch origin <ветка>`: устаревшая `origin/main` — ровно то, из-за чего конфликт всплывает уже в PR."""
    for b in BASES:
        for ref in (f"origin/{b}", b):
            if _ok(_git(root, "rev-parse", "--verify", "-q", f"{ref}^{{commit}}")):
                if fetch and ref.startswith("origin/"):
                    _git(root, "fetch", "-q", "origin", b)
                return ref
    return ""


def merge_conflict(root: Path, sha: str, fetch: bool = True) -> str:
    """Текст отказа, если голова `sha` не сливается с main без конфликта; пусто — сливается или проверить нечем."""
    if not sha or not _ok(_git(root, "cat-file", "-e", f"{sha}^{{commit}}")):
        return ""
    base = base_ref(root, fetch)
    if not base:
        return ""
    proc = _git(root, "merge-tree", "--write-tree", "--name-only", base, sha)
    if proc is None or proc.returncode != 1:  # 0 — чисто; иное (старый git без --write-tree и т.п.) — проверить нечем
        return ""
    files = []
    for line in proc.stdout.splitlines()[1:]:
        if not line.strip():
            break
        files.append(line.strip())
    shown = ", ".join(files[:5]) + (f" и ещё {len(files) - 5}" if len(files) > 5 else "")
    return (f"голова {sha[:7]} не сливается с {base} без конфликта ({shown or 'файлы не названы'}) — в своей копии "
            f"`git fetch origin` и `git merge {base}`, разреши конфликты, прогони тесты правки, запушь и сдай заново")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[«»\"'`*_]", "", text or "")).strip().lower()


def quoted(entry_text: str, reply: str) -> bool:
    """Ответ содержит дословный кусок записи (≥ QUOTE_MIN знаков подряд, а запись короче — вся целиком)."""
    e, r = _norm(entry_text), _norm(reply)
    if not e:
        return True
    n = min(len(e), QUOTE_MIN)
    return any(e[i:i + n] in r for i in range(len(e) - n + 1))


def late_entries(tkt, role: str, started: datetime, reply: str = "") -> list:
    """Записи `ceo`/`judge` позже старта сессии роли, на которые она не ответила цитатой: ни следующей своей записью в логе,
    ни текстом итога (`reply`)."""
    cut = started.replace(microsecond=0)
    own = [e for e in tkt.log if T.author_is(e.author, role)]
    out = []
    for e in tkt.log:
        if e.ts <= cut or not any(T.author_is(e.author, a) for a in ORDER_AUTHORS):
            continue
        answers = [o.text for o in own if o.ts >= e.ts and o is not e] + [reply]
        if not any(quoted(e.text, a) for a in answers):
            out.append(e)
    return out


def run_started(state: dict, tid: str, role: str) -> str:
    """Старт запущенной сессии этой роли на тикете — из зеркала диспетчера `active_runs` в `state.json`; пусто, если запуска
    нет (сдача вручную, не из запуска диспетчера)."""
    run = ((state or {}).get("active_runs") or {}).get(tid)
    return str(run.get("started") or "") if isinstance(run, dict) and run.get("role") == role else ""


def late_order(tkt, role: str, started_iso: str, reply: str = "") -> str:
    started = P.parse_time(started_iso) if started_iso else None
    if started is None:
        return ""
    late = late_entries(tkt, role, started, reply)
    if not late:
        return ""
    e = late[-1]
    head = re.sub(r"\s+", " ", e.text).strip()[:100]
    who = "; ".join(f"{x.author} {x.ts_raw}" for x in late)
    return (f"после старта сессии в логе новая запись: {who} — «{head}». Прочитай её и ответь: запись роли "
            f"(`tickets.py comment {tkt.id} --author {role} --text \"…\"`) или --why итога с цитатой оттуда (≥ {QUOTE_MIN} знаков "
            f"подряд), затем сдай заново")


def refusal(args, role: str, tkt, root: Path, started_iso: str = "", fetch: bool = True) -> str:
    """Отказ `result pr|done` роли-исполнителю до записи итога; пусто — можно сдавать."""
    if args.result not in CHECKED or role not in ("engineer", "researcher"):
        return ""
    err = merge_conflict(root, getattr(args, "sha", None) or "", fetch)
    return err or late_order(tkt, role, started_iso, getattr(args, "why", "") or "")
