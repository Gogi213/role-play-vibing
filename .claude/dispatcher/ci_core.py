"""Общие примитивы сторожа CI и вливания по правилу: состояние CI, вызов GitHub, привязка PR к тикету, пробуждение владельца.

Вынесено из `ci_watch.py` (TK-011, раунд ревью 1): `merge_rule` тянул это из `ci_watch`, а `ci_watch` запускал `merge_rule` —
цикл импортов. Теперь оба читают `ci_core`; `ci_watch` по-прежнему отдаёт те же имена (`C.save_state`, `C.ticket_for_pr`).
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config as CFG  # noqa: E402
import hide  # noqa: E402
import project as P  # noqa: E402
import ticket as T  # noqa: E402

_TK_RE = re.compile(r"\bTK-?(\d+)\b", re.I)
ACTIVE = ("todo", "in_progress", "in_review", "waiting")


def state_path() -> Path:
    return CFG.STATE_FILE.parent / "ci-state.json"


def load_state() -> dict:
    return P.read_json(state_path(), {})


def save_state(st: dict) -> None:
    P.write_json(state_path(), st)


def gh_api(path: str, method: str = "GET", **fields):
    args = ["gh", "api", "-X", method, path] + [x for k, v in fields.items() for x in ("-f", f"{k}={v}")]
    r = hide.run(args, capture_output=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or b"").decode("utf-8", "replace").strip()[:200])
    return json.loads(r.stdout.decode("utf-8"))


def ticket_for_pr(pr: dict, tickets: list):
    """Тикет по шапке `pr:` (список номеров), иначе по TK-<N> в имени ветки/заголовке. Нет — None."""
    num = str(pr["number"])
    for t in tickets:
        if num in re.findall(r"\d+", str(t.header.get("pr") or "")):
            return t
    m = _TK_RE.search(f"{pr.get('head', {}).get('ref', '')} {pr.get('title', '')}")
    if m:
        want = f"TK-{int(m.group(1)):03d}"
        for t in tickets:
            if t.id == want:
                return t
    return None


def wake(path, tkt, who: str, text: str, repo: str = "", pr_number: int = 0) -> None:
    upd = {"next": who}
    if repo and (tkt.header.get("wait_for") or "").strip() == f"ci:{repo}#{pr_number}":
        upd.update({"wait_for": "", "on_met": ""})
    elif repo and who != "judge" and (tkt.header.get("wait_for") or "").strip() == f"merged:{repo}#{pr_number}":
        # красный CI на принятом PR: ждать влития нечего (PR не влить) — владелец будится, ожидание снято (TK-092, CEO 05:25)
        upd.update({"wait_for": "", "on_met": "", "status": "in_progress"})
    with T.ticket_lock(path):
        T.append_log(path, "ci", text)
        T.write_header_updates(path, upd, stamp_updated=False)
