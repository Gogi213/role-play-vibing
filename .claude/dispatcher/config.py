"""Пути проекта диспетчера: корень, каталог состояния, тикеты, журналы. Выставляет `configure_project`.

Читают как `CFG.<имя>` (атрибут модуля, не копия): `dispatch`, `watch`, `ci_watch`, `merge_rule`, `tickets`; тесты подменяют
прямо на этом модуле. Только stdlib.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import project as P  # noqa: E402

# Пути проекта (PROJECT_ROOT, TICKETS_DIR, DISPATCHER_DIR — каталог СОСТОЯНИЯ в проекте и остальные файлы) выставляет
# configure_project(): при импорте — по --project/RPV_PROJECT/CLAUDE_PROJECT_DIR/текущему каталогу, в main() — по флагу.
PROJECT_ROOT = DISPATCHER_DIR = TICKETS_DIR = STATE_FILE = PID_FILE = RUNS_DIR = RUNS_LOG = None
CEO_INBOX = CEO_WAKE_LOG = STOP_DIR = None
PROJECT_FOUND = False  # проект найден при импорте (флаг/окружение/поиск вверх); False — CLI откажет с подсказкой


def configure_project(root) -> Path:
    """Корень проекта и все пути состояния от него (ничего не создаёт — каталоги появляются при записи)."""
    global PROJECT_ROOT, DISPATCHER_DIR, TICKETS_DIR, STATE_FILE, PID_FILE, RUNS_DIR, RUNS_LOG, CEO_INBOX, CEO_WAKE_LOG, STOP_DIR
    PROJECT_ROOT = Path(root).expanduser().resolve()
    P.set_root(PROJECT_ROOT)  # CP8: настройки проекта — `.claude/rpv.json` (P.setting)
    DISPATCHER_DIR = PROJECT_ROOT / ".claude" / "dispatcher"
    TICKETS_DIR = PROJECT_ROOT / ".claude" / "tickets"
    STATE_FILE = DISPATCHER_DIR / "state.json"
    PID_FILE = DISPATCHER_DIR / "dispatch.pid"  # замок единственного экземпляра диспетчера (см. acquire_instance_lock)
    RUNS_DIR = DISPATCHER_DIR / "runs"
    RUNS_LOG = DISPATCHER_DIR / "runs.log"
    CEO_INBOX = DISPATCHER_DIR / "ceo-inbox.md"
    CEO_WAKE_LOG = DISPATCHER_DIR / "ceo-wake.log"  # короткая копия каждой строки ceo-inbox — CEO держит на ней Monitor
    STOP_DIR = DISPATCHER_DIR / "stop"  # заявки `tickets.py stop` (<ID>.json): диспетчер разбирает их на ближайшем тике
    return PROJECT_ROOT


try:
    configure_project(P.resolve_project())
    PROJECT_FOUND = True
except P.ProjectNotFound:                 # импорт не падает; пути — заглушка от cwd, ничего не создаётся
    configure_project(Path.cwd())


def ensure_project(argv, prog: str) -> bool:
    """Точка входа CLI: проект задан флагом `--project` (переключаем пути) или найден при импорте; иначе —
    подсказка в stderr и False (вызывающий выходит с кодом 2, не создав ни одного каталога)."""
    explicit = P.project_arg(argv)
    if explicit:
        configure_project(explicit)
        return True
    if not PROJECT_FOUND:
        print(f"[{prog}] ошибка: {P.NOT_FOUND_HINT}", file=sys.stderr)
        return False
    return True


def load_state() -> dict:
    return P.read_json(STATE_FILE, {})


def save_state(state: dict) -> None:
    P.write_json(STATE_FILE, state, sort_keys=True)   # с повторами: на Windows читатель держит файл
