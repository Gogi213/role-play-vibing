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
# пункт отчёта критика (agents/critic.md): `N. «утверждение» — **держится** | **не держится** | **не хватает**`
ITEM = re.compile(r"^\s*(?:[-*]\s+)?(\d+)\.\s.*?\*\*(не держится|держится|не хватает)\*\*", re.M)
ANSWER = re.compile(r"^\s*(?:[-*]\s+)?Ответ Судьи(?:\s+на(?:\s+пункт)?)?\s+(\d+)\s*[:—–-]\s*(\S.{4,})", re.M)
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


def parse_report(text: str) -> tuple[dict[str, str], set[str]]:
    """({номер пункта: вердикт критика}, номера пунктов, на которые Судья ответил)."""
    return {m.group(1): m.group(2) for m in ITEM.finditer(text)}, {m.group(1) for m in ANSWER.finditer(text)}


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
    items, answered = parse_report(f.read_text(encoding="utf-8", errors="replace"))
    if not items:
        return f"--critic {raw}: в файле нет пунктов «N. … **держится|не держится|не хватает**» — {HINT}", ""
    if sha:
        ts = head_ts(root, sha)
        if ts is None:
            return f"--critic {raw}: голова {sha[:7]} не найдена в git проекта (git fetch) — свежесть отчёта не проверить", ""
        if f.stat().st_mtime < ts:
            return f"--critic {raw}: отчёт старше головы {sha[:7]} — критик прошёл по прежнему коду; зови заново", ""
    open_ = sorted((n for n, v in items.items() if v == "не держится" and n not in answered), key=int)
    if open_:
        return (f"--critic {raw}: «не держится» без ответа Судьи по пунктам {', '.join(open_)} — допиши в файл "
                f"«Ответ Судьи на N: …» (принял правку / возражение с причиной)"), ""
    held = sum(v == "не держится" for v in items.values())
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
