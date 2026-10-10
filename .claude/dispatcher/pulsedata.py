"""Общее для plan.py и board_push.py: пути плана шагов, время, атомарная запись json (tmp + replace, повтор на Windows)."""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import project as _project  # noqa: E402
import ticket as _ticket  # noqa: E402

TZ = timezone(timedelta(hours=4))  # GMT+4


def plans_dir(root: Path | None = None) -> Path:
    """`<проект>/.claude/pulse/plans` — корень проекта как у остальных скриптов (--project, RPV_PROJECT, поиск вверх)."""
    return (root or _project.resolve_project([])) / ".claude" / "pulse" / "plans"


def questions_dir(root: Path | None = None) -> Path:
    return (root or _project.resolve_project([])) / ".claude" / "pulse" / "questions"


def is_ticket(pid: str) -> bool:
    return bool(re.fullmatch(r"TK-\d+", pid or ""))


VALID_ON = ("pc", "vps", "calc", "col", "you")  # метки машин + «вы»
STEP_STATES = ("todo", "run", "review", "repair", "wait", "bad", "done")  # run делается · review проверяется · repair чинится


def now_dt() -> datetime:
    return datetime.now(TZ)


def iso(dt: datetime | None = None) -> str:
    return (dt or now_dt()).isoformat(timespec="seconds")


def parse(s) -> datetime | None:
    try:
        d = datetime.fromisoformat(str(s))
    except (TypeError, ValueError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=TZ)


def hhmm(s) -> str | None:
    d = parse(s)
    return d.astimezone(TZ).strftime("%H:%M") if d else None


def tk_id(s: str) -> str:
    """«tk044» / «TK-44» → «TK-044»; остальное (orphans-vps, …) — как есть."""
    m = re.fullmatch(r"(?i)tk[-_]?(\d+)", (s or "").strip())
    return f"TK-{int(m.group(1)):03d}" if m else (s or "").strip()


def read_json(path: Path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_json(path: Path, obj, retries: int = 20) -> None:
    """Запись JSON целиком (`ticket.atomic_write_text`: tmp + os.replace с повторами на Windows)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _ticket.atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=1) + "\n", retries)
