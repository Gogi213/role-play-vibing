"""Критик перед приёмкой (TK-071, G-010/G-011, В-29): Судья перед `accept` зовёт агента `critic` с договором и результатом
тикета, сохраняет отчёт в docs/reviews/ и передаёт `--critic <файл>`; код проверяет, что отчёт есть, свежий и что на каждое
«не держится» Судья ответил. Ключ `critic_before_accept` в `.claude/rpv.json` (умолчание — выключен): выключен — приёмка как
раньше. Обход — `--critic-skip "причина"`: пишется в лог приёмки и в `critic-runs.log`, ежедневный отчёт считает обходы.

Только stdlib; корень проекта приходит параметром.
"""
from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime
from pathlib import Path

import hide
import project as P

KEY = "critic_before_accept"  # читается литералом в critic_ack (test_configuration_doc ищет `setting("ключ"`)
LOG_NAME = "critic-runs.log"
REVIEWS = ("docs", "reviews")
MIN_SKIP_WHY = 10
# пункт отчёта критика (agents/critic.md): N. «утверждение» — **держится** | **не держится** | **не хватает**
# Форма номера: «1.», «1)», «**1.**», «`1.`», «### 1.», «- 1.»; вердикт — в той же строке или в следующей непустой.
_VERDICTS = ("не держится", "держится", "не хватает")
_NUMBER = re.compile(r"^[\s#>*`-]*(\d+)[*`]*[.)][*`]*\s")
_VERDICT_BOLD = re.compile(r"\*\*\s*(не держится|держится|не хватает)\s*\*\*", re.I)
_VERDICT_LEAD = re.compile(r"^[\s>*`—–-]*\*{0,2}\s*(не держится|держится|не хватает)(?![а-яё])", re.I)
_SEVERITY = {"держится": 0, "не хватает": 1, "не держится": 2}
ANSWER = re.compile(r"^[\s>*`-]*Ответ Судьи(?:\s+на(?:\s+пункт)?)?\s+(\d+)\s*[*`]*\s*[:—–-]\s*(\S.{4,})", re.M | re.I)
_HEADER_TICKET = re.compile(r"^[\s#>*`-]*Тикет\s*:\s*\**\s*(TK-\d+)", re.M | re.I)
_HEADER_HEAD = re.compile(r"^[\s#>*`-]*Голова\s*:\s*\**\s*`?([0-9a-f]{7,40})", re.M | re.I)
_ACCEPTED = re.compile(r"^\W*принял", re.I)  # «принял правку» — Судья согласен с критиком: тикет назад (return), не accept
HINT = ("Судья зовёт агента critic (Agent, subagent_type role-play-vibing:critic, обычный передний) с договором и результатом "
        "тикета и сохраняет ответ в docs/reviews/<тикет>-critic.md; «не держится» по пункту N — строка «Ответ Судьи на N: …» в "
        "том же файле; обход — --critic-skip \"причина\" (считается в отчёт)")


def log_path(root: Path) -> Path:
    return Path(root) / ".claude" / "dispatcher" / LOG_NAME


def head_time(root: Path, sha: str) -> float | None:
    """Время коммита `sha` (эпоха) в git проекта; нет коммита или git — None."""
    try:
        out = hide.git(root, "show", "-s", "--format=%ct", sha, timeout=20, text=True)
        return float(out.stdout.strip()) if out.returncode == 0 and out.stdout.strip() else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _find(root: Path, raw: str) -> Path | None:
    """Отчёт в проекте: путь от корня (или абсолютный внутри корня); нет файла — None."""
    root = root.resolve()
    f = (Path(raw) if Path(raw).is_absolute() else root / raw).resolve()
    return f if f.is_file() and f.is_relative_to(root) else None


def parse_report(text: str) -> tuple[dict[str, str], set[str], list[str]]:
    """({номер пункта: вердикт критика}, номера, на которые Судья ответил, повторённые номера)."""
    items: dict[str, str] = {}
    dups: list[str] = []
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        num = _NUMBER.match(ln)
        if not num:
            continue
        found = _VERDICT_BOLD.findall(ln)
        if not found:  # вердикт на следующей непустой строке
            nxt = next((x for x in lines[i + 1:i + 4] if x.strip()), "")
            lead = _VERDICT_LEAD.match(nxt)
            found = [lead.group(1)] if lead else []
        if not found:
            continue
        n, verdict = num.group(1), " ".join(found[-1].lower().split())
        if n in items:
            dups.append(n)
        if n not in items or _SEVERITY[verdict] > _SEVERITY[items[n]]:
            items[n] = verdict
    return items, {m.group(1) for m in ANSWER.finditer(text)}, sorted(set(dups), key=int)


def _answers(text: str) -> dict[str, str]:
    return {m.group(1): m.group(2).strip() for m in ANSWER.finditer(text)}


def _empty_answer(ans: str) -> bool:
    """Ответ Судьи без содержания: «ок ок.», «ок», меньше трёх разных слов или 15 знаков."""
    words = {w for w in re.findall(r"\w+", ans.lower())}
    return len(words) < 3 or len(ans.strip()) < 15


def _record(root: Path, row: dict) -> None:
    """Строка в critic-runs.log; повтор той же приёмки (тот же тикет, вид, цель и голова) не дублируется."""
    path = log_path(root)
    key = {k: row.get(k) for k in ("tid", "kind", "file", "sha", "why")}
    try:
        old = path.read_text(encoding="utf-8").splitlines()[-100:] if path.exists() else []
        for ln in old:
            try:
                j = json.loads(ln)
            except ValueError:
                continue
            if isinstance(j, dict) and {k: j.get(k) for k in key} == key:
                return
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": P.now_iso(), **row}, ensure_ascii=False) + "\n")
    except OSError:
        pass  # журнал — учёт, не ворота: приёмку из-за него не рушим


def critic_ack(args, tid: str, root: Path, sha: str = "", head_ts=head_time) -> tuple[str, str]:
    """Ворота критика на `accept`. → (отказ, пометка для лога). Ключ выключен — ("", "")."""
    if not P.setting("critic_before_accept", False, root=root):
        return "", ""
    raw = (getattr(args, "critic", None) or "").strip()
    skip = " ".join((getattr(args, "critic_skip", None) or "").split())
    if raw and skip:
        return "--critic и --critic-skip вместе нельзя: либо отчёт критика, либо обход с причиной", ""
    if skip:
        if len(skip) < MIN_SKIP_WHY:
            return f"--critic-skip: причина обхода — не короче {MIN_SKIP_WHY} знаков", ""
        _record(root, {"tid": tid, "kind": "skip", "file": "", "sha": sha[:12], "why": skip[:200]})
        return "", f" критик: обход ({skip[:80]})"
    if not raw:
        return f"включён ключ {KEY}: приёмка — с отчётом критика --critic <файл в docs/reviews/>: {HINT}", ""
    f = _find(root, raw)
    if not f:
        return f"--critic {raw}: файла нет в проекте — {HINT}", ""
    rel = f.relative_to(root.resolve())
    if rel.parts[:2] != REVIEWS:
        return f"--critic {raw}: отчёт критика — в docs/reviews/", ""
    text = f.read_text(encoding="utf-8", errors="replace")
    items, answered, dups = parse_report(text)
    if not items:
        return f"--critic {raw}: в файле нет пунктов «N. … **держится|не держится|не хватает**» — {HINT}", ""
    if dups:
        return f"--critic {raw}: номер пункта повторён ({', '.join(dups)}) — отчёт читается неоднозначно; критик пишет каждый номер один раз", ""
    bold = sum(1 for _ in re.finditer(r"\*\*\s*не держится\s*\*\*", text, re.I))
    parsed = sum(v == "не держится" for v in items.values())
    if bold > parsed:
        return (f"--critic {raw}: «не держится» в файле {bold}, разобрано пунктов {parsed} — часть пунктов записана не в форме "
                f"«N. … **не держится**» (разбор их не видит); перепиши отчёт"), ""
    if tid:
        t = _HEADER_TICKET.search(text)
        if not t or t.group(1).upper() != tid.upper():
            return f"--critic {raw}: в файле нет строки «Тикет: {tid}» (или она про другой тикет) — отчёт критика привязан к тикету", ""
    if sha:
        h = _HEADER_HEAD.search(text)
        if not h or not (sha.lower().startswith(h.group(1).lower()) or h.group(1).lower().startswith(sha.lower())):
            return f"--critic {raw}: в файле нет строки «Голова: {sha[:7]}…» (или она про другую голову) — критик прошёл по другому коду", ""
    if sha:
        ts = head_ts(root, sha)
        if ts is None:
            return f"--critic {raw}: голова {sha[:7]} не найдена в git проекта (git fetch) — свежесть отчёта не проверить", ""
        if f.stat().st_mtime < ts:
            return f"--critic {raw}: отчёт старше головы {sha[:7]} — критик прошёл по прежнему коду; зови заново", ""
    open_ = sorted((n for n, v in items.items() if v in ("не держится", "не хватает") and n not in answered), key=int)
    if open_:
        return (f"--critic {raw}: «не держится» / «не хватает» без ответа Судьи по пунктам {', '.join(open_)} — допиши в файл "
                f"«Ответ Судьи на N: …» (возражение с причиной по существу)"), ""
    answers = _answers(text)
    bare = sorted((n for n, a in answers.items() if n in items and _empty_answer(a)), key=int)
    if bare:
        return f"--critic {raw}: ответ Судьи по пунктам {', '.join(bare)} без содержания («ок ок.») — напиши причину по существу", ""
    agreed = sorted((n for n, a in answers.items() if n in items and items[n] != "держится" and _ACCEPTED.match(a)), key=int)
    if agreed:
        return (f"--critic {raw}: Судья принял замечание критика по пунктам {', '.join(agreed)} — значит правка нужна: "
                f"верни тикет (`result return`), а не accept"), ""
    held = parsed
    _record(root, {"tid": tid, "kind": "report", "file": rel.as_posix(), "sha": sha[:12], "why": "",
                   "items": len(items), "not_held": held, "answered": len(answered & set(items))})
    return "", f" критик: {rel.as_posix()} (пунктов {len(items)}, не держится {held})"


def skips(root: Path, since: datetime, until: datetime) -> tuple[int, int]:
    """(обходов, проходов с отчётом) в critic-runs.log за период (since, until]."""
    n_skip = n_report = 0
    try:
        lines = log_path(root).read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0, 0
    for ln in lines:
        try:
            j = json.loads(ln)
            ts = P.parse_time(j.get("ts"))
        except (ValueError, AttributeError):
            continue
        if ts and since < ts <= until:
            n_skip += j.get("kind") == "skip"
            n_report += j.get("kind") == "report"
    return n_skip, n_report
