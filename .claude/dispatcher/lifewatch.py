"""Сторож жизни ожиданий (TK-092): форма `wait_for: job:<алиас>:<id>` и состояние задания через адаптер проекта.

Плагин не знает, чем проект запускает задания: команда состояния — настройка `RPV_JOB_STATE_CMD` (шаблон с `{id}`),
выполняется по ssh на машине алиаса. Договор адаптера: первая строка вывода — `running|queued|done|failed|missing`,
остальные строки — хвост лога задания. Другое первой строкой или код ssh 255 — `ssh-error` (не проверить).
Снятое владельцем задание (cancel-метка планировщика, rc=143 при снятии) адаптер отдаёт как `done`, не `failed` — это не падение
(TK-101 п.2); так же и в `RPV_JOB_OWNERS_CMD`. Пересдача под тем же именем юнита или живое задание того же тикета гасят «упало» (watch.py).
Сторож (watch.py) пишет состояние в `job-state.json`; диспетчер условие `job:` по нему и проверяет — без своего ssh."""
from __future__ import annotations

import json
import shlex
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import project as P  # noqa: E402

JOB_STATES = ("running", "queued", "done", "failed", "missing")
TAIL_LINES = 30
STATE_NAME = "job-state.json"


def probe_job(alias: str, jid: str, ssh_cmd, timeout: float = 25.0):
    """(состояние, хвост лога). `ssh_cmd(alias, remote)` — как `dispatch._ssh_cmd`; адаптер не задан — `ssh-error`."""
    tpl = P.env("JOB_STATE_CMD")
    if not tpl or "{id}" not in tpl:
        return "ssh-error", ""
    try:
        r = subprocess.run(ssh_cmd(alias, tpl.replace("{id}", shlex.quote(jid))), capture_output=True, timeout=timeout)
    except Exception:
        return "ssh-error", ""
    lines = (r.stdout or b"").decode("utf-8", "replace").splitlines()
    if r.returncode == 255 or not lines or lines[0].strip() not in JOB_STATES:
        return "ssh-error", ""
    return lines[0].strip(), "\n".join(lines[1:][-TAIL_LINES:])


def _path(dispatcher_dir) -> Path:
    return Path(dispatcher_dir) / STATE_NAME


def save_states(dispatcher_dir, states: dict) -> None:
    """`{"<алиас>:<id>": "<состояние>"}` — только то, что сторож проверил в этом цикле."""
    p = _path(dispatcher_dir)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(states, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    tmp.replace(p)


def job_done(dispatcher_dir, alias: str, jid: str) -> bool:
    try:
        return json.loads(_path(dispatcher_dir).read_text(encoding="utf-8")).get(f"{alias}:{jid}") == "done"
    except (OSError, ValueError):
        return False


def reason(jid: str, tail: str) -> str:
    """Одна строка причины по хвосту лога — Haiku (`haiku_aux.diagnose`, TK-087), только разбор. Нет помощника,
    claude или ответа — пусто: пробуждение владельца от разбора не зависит."""
    if not tail.strip():
        return ""
    try:
        import haiku_aux
        out = haiku_aux.diagnose(jid, tail)
    except Exception:
        return ""
    out = (out or "").strip()
    return out.splitlines()[0][:300] if out else ""


# --- сопоставление «задание → тикет» (одно на сторож и табло) -------------------------------------------------
OWNERS_NAME = "job-owners.json"


def fetch_owners(alias: str, ssh_cmd, timeout: float = 25.0):
    """Адаптер `RPV_JOB_OWNERS_CMD` (по ssh на машине алиаса): строка на задание, поля через TAB — id, тикет (или пусто),
    юнит, состояние running|queued|done|failed. Список dict или None (адаптер не задан / не ответил)."""
    cmd = P.env("JOB_OWNERS_CMD")
    if not cmd:
        return None
    try:
        r = subprocess.run(ssh_cmd(alias, cmd), capture_output=True, timeout=timeout)
    except Exception:
        return None
    if r.returncode != 0:
        return None
    out = []
    for ln in (r.stdout or b"").decode("utf-8", "replace").splitlines():
        f = ln.split("\t")
        if len(f) >= 4 and f[3].strip() in JOB_STATES:
            out.append({"id": f[0].strip(), "ticket": f[1].strip(), "unit": f[2].strip(), "state": f[3].strip()})
    return out


def save_owners(dispatcher_dir, owners: dict) -> None:
    """`{алиас: [задания]}` — это читает и табло проекта (кто хозяин юнита)."""
    p = Path(dispatcher_dir) / OWNERS_NAME
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(owners, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    tmp.replace(p)


# --- прогоны мимо планировщика поверх чужих (п.5 CEO 05:45) -----------------------------------------------------
def fetch_strays(alias: str, ssh_cmd, timeout: float = 25.0):
    """Адаптер `RPV_STRAY_CMD` (по ssh на машине алиаса): строка на прогон мимо планировщика поверх чужого задания,
    поля через TAB — тикет-нарушитель (или пусто), описание. Список (тикет, описание) или None (адаптер не задан / не ответил)."""
    cmd = P.env("STRAY_CMD")
    if not cmd:
        return None
    try:
        r = subprocess.run(ssh_cmd(alias, cmd), capture_output=True, timeout=timeout)
    except Exception:
        return None
    if r.returncode != 0:
        return None
    out = []
    for ln in (r.stdout or b"").decode("utf-8", "replace").splitlines():
        if ln.strip():
            tid, _, desc = ln.partition("\t")
            out.append((tid.strip(), (desc or tid).strip()))
    return out
