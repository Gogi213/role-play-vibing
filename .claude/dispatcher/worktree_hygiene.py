"""Гигиена рабочих копий (TK-104): worktree закрытого тикета (done/stopped) убирается, несохранённое — коммитом в ветку.

Копии живут только в `<проект>/.claude/worktrees/<имя>`; имя привязано к тикету токеном `tk<N>` (tk049-rs, rpv-tk104).
    sweep(project, tickets)  — убрать копии закрытых тикетов (вызывает тик диспетчера)
    findings(project, tickets) — (закрытые, чужие) для doctor, ничего не меняет"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import hide

CLOSED = ("done", "stopped")
TOKEN = re.compile(r"tk0*(\d+)", re.I)


def _git(project: Path, *a, cwd=None, timeout=60):
    return hide.run(["git", *a], cwd=str(cwd or project), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


def list_worktrees(project: Path) -> list:
    """[(путь, ветка|None)] без основного дерева."""
    r = _git(project, "worktree", "list", "--porcelain")
    out, cur = [], {}
    for line in r.stdout.splitlines() + [""]:
        if not line:
            if cur.get("worktree"):
                out.append((Path(cur["worktree"]), (cur.get("branch") or "").removeprefix("refs/heads/") or None))
            cur = {}
        else:
            k, _, v = line.partition(" ")
            cur[k] = v
    return [w for w in out[1:]]


def _inside(project: Path, p: Path) -> bool:
    try:
        p.resolve().relative_to((project / ".claude" / "worktrees").resolve())
        return True
    except ValueError:
        return False


def ticket_of(path: Path) -> str | None:
    m = TOKEN.search(path.name)
    return f"TK-{int(m.group(1)):03d}" if m else None


def ticket_of_branch(branch: str | None) -> str | None:
    m = TOKEN.search(branch or "")
    return f"TK-{int(m.group(1)):03d}" if m else None


def is_closed(path: Path, branch: str | None, status_of: dict) -> bool:
    """Копия закрыта, только если имя папки И ветка (если в ней есть токен тикета) указывают на закрытые тикеты."""
    if status_of.get(ticket_of(path)) not in CLOSED:
        return False
    bt = ticket_of_branch(branch)
    return bt is None or status_of.get(bt) in CLOSED


def findings(project: Path, status_of: dict) -> tuple:
    """status_of: {id тикета: status}. -> (копии закрытых тикетов, копии вне .claude/worktrees)."""
    closed, foreign = [], []
    for path, branch in list_worktrees(project):
        if not _inside(project, path):
            foreign.append(path)
        elif is_closed(path, branch, status_of):
            closed.append(path)
    return closed, foreign


BUILD_IGNORED = ("target/", "node_modules/", "__pycache__/", ".pytest_cache/", ".venv/")


def precious_ignored(path: Path) -> list:
    """Игнорируемое git'ом, кроме артефактов сборки: удаление worktree потеряло бы это без следа."""
    r = _git(path, "status", "--ignored", "--porcelain", cwd=path)
    out = [l[3:] for l in r.stdout.splitlines() if l.startswith("!! ")]
    return [f for f in out if not any(f.startswith(b) or f"/{b}" in f for b in BUILD_IGNORED)]


def remove(project: Path, path: Path, branch: str | None) -> str:
    """Убрать копию; грязное — коммит в ветку (у detached HEAD сохранять некуда — копия остаётся). Ответ — что сделано."""
    keep = precious_ignored(path)
    if keep:
        return f"оставлена {path.name}: игнорируемые файлы {', '.join(keep[:3])}"
    dirty = _git(project, "status", "--porcelain", cwd=path).stdout.strip()
    if dirty:
        if not branch:
            return f"оставлена {path.name}: несохранённое при detached HEAD"
        _git(project, "add", "-A", cwd=path)
        c = _git(project, "-c", "user.name=dispatcher", "-c", "user.email=dispatcher@local", "commit", "-q", "-m",
                 f"WIP: сохранено при закрытии тикета ({path.name})", cwd=path)
        if c.returncode:
            return f"оставлена {path.name}: коммит не удался"
    r = _git(project, "worktree", "remove", "--force", str(path), timeout=300)
    return f"убрана {path.name}" if r.returncode == 0 else f"оставлена {path.name}: {r.stderr.strip()[:80]}"


def sweep(project: Path, status_of: dict) -> list:
    done = []
    branches = dict(list_worktrees(project))
    closed, _ = findings(project, status_of)
    for path in closed:
        done.append(remove(project, path, branches.get(path)))
    if done:
        _git(project, "worktree", "prune")
    return done
