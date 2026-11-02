#!/usr/bin/env python3
"""Побудка CEO событием (TK-085, В-33/В-35): будильников нет — CEO будят тогда, когда это нужно.

Диспетчер на событие поднимает один headless-запуск `claude -p` (Haiku, только ListAgents и SendMessage) и шлёт в сессию CEO
одну строку. События — четыре (EVENT_OF): нужно решение владельца; сломалось или сорван срок; шаг цели принят, следующий не
определён; цель закрыта. Обычный `done` тикета — не событие. Сессию CEO называет `/ceo`:

    ceo_wake.py [--project <путь>] set --id <id сессии> [--name <имя>]    # `/ceo`: кто CEO
    ceo_wake.py [--project <путь>] show

Сессия не найдена или запуск не вышел — строка в `ceo-inbox.md` и сообщение владельцу в Telegram, исключений наружу нет.
Режим разрешений запуска — ключ `ceo_wake_mode` (умолчание `bypassPermissions`): он обязан совпадать с режимом сессии CEO, иначе
сообщение повисает на одобрении и не будит. Отключить — `ceo_wake: false`."""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import haiku_aux  # noqa: E402
import notify  # noqa: E402
import project as P  # noqa: E402
import project_paths as CFG  # noqa: E402

SESSION_NAME = "ceo-session.json"
MODEL = haiku_aux.MODEL
TIMEOUT_S = 90
DEFAULT_MODE = "bypassPermissions"
NOT_FOUND = "NOT_FOUND"

# вид сигнала диспетчера → событие. Остальные виды CEO не будят: они только строки очереди (`tickets.py inbox`)
EVENT_OF = {
    "needs_owner": "нужно решение владельца",
    "blocked": "сломалось",
    "auth": "сломалось",
    "due": "сорван срок",
    "следующий-не-определён": "шаг цели принят, следующий не определён",  # вид шлёт goal_steps.advance (TK-087)
    "goal-done": "цель закрыта",
}


def session_file() -> Path:
    return CFG.DISPATCHER_DIR / SESSION_NAME


def read_session() -> dict:
    try:
        d = json.loads(session_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) and (d.get("id") or d.get("name")) else {}


def write_session(sid: str, name: str = "") -> None:
    session_file().parent.mkdir(parents=True, exist_ok=True)
    session_file().write_text(json.dumps({"id": sid.strip(), "name": name.strip(), "at": P.now_iso()}, ensure_ascii=False),
                              encoding="utf-8")


def _prompt(session: dict, line: str) -> str:
    who = " или ".join(f"«{v}»" for v in (session.get("name"), session.get("id")) if v)
    return (f"Вызови ListAgents. Найди сессию, у которой имя или идентификатор совпадает с {who}. Нет такой — выведи ровно "
            f"{NOT_FOUND} и остановись. Есть — отправь ей через SendMessage ровно одну строку, дословно:\n{line}\n"
            "Больше ничего не делай и не пиши.")


def _launch(session: dict, line: str, runner) -> tuple[bool, str]:
    """(доставлено, причина). Один вызов `claude -p`; не запустился, завис, не нашёл сессию — (False, причина)."""
    claude = haiku_aux._claude()
    if not claude:
        return False, "claude не найден"
    mode = str(P.setting("ceo_wake_mode", DEFAULT_MODE))
    cmd = [claude, "-p", "--model", MODEL, "--permission-mode", mode, "--allowedTools", "ListAgents,SendMessage"]
    try:
        with tempfile.TemporaryDirectory(prefix="rpv-wake-") as cwd:  # вне проекта: без CLAUDE.md и хуков
            r = haiku_aux.run_claude(cmd, _prompt(session, line), TIMEOUT_S, cwd=cwd, runner=runner)
    except Exception as e:  # noqa: BLE001 — побудка не роняет диспетчер
        return False, f"{type(e).__name__}: {e}"
    out = (r.stdout or b"").decode("utf-8", "replace")
    if r.returncode != 0:
        return False, f"код {r.returncode}: {((r.stderr or b'').decode('utf-8', 'replace') or out).strip()[:120]}"
    if NOT_FOUND in out:
        return False, "сессия CEO не найдена в ListAgents"
    return True, ""


def wake(tid: str, kind: str, note: str, write_inbox, now=None, runner=None, send=None) -> bool:
    """Событие `kind` → одна строка в сессию CEO. True — запуск ушёл и сессия нашлась. Не событие — False, запусков нет.
    `write_inbox(тикет, вид, текст, now)` — запись в `ceo-inbox.md` (подставляет signals, без побудки)."""
    event = EVENT_OF.get(kind)
    if not event or P.setting("ceo_wake", True) is False:
        return False
    line = f"[{event}] {tid} — {' '.join(str(note).split())[:160]}. Прочитай `tickets.py inbox`."
    session = read_session()
    if session:
        ok, why = _launch(session, line, runner or haiku_aux.hide.run)
        if ok:
            return True
        write_inbox(tid, "ceo-не-разбужен", f"{why}; событие: {line}", now)
    else:
        why = "сессия CEO не названа (`/ceo` не записал ceo-session.json)"
        try:
            with (CFG.DISPATCHER_DIR / "mechanics.log").open("a", encoding="utf-8") as f:
                f.write(f"{P.now_iso(now)} {tid} [ceo-не-разбужен] {why}; событие: {line}\n")
        except OSError:
            pass
    try:
        (send or notify.send)(f"CEO не разбужен ({why}). {line}", log_dir=CFG.DISPATCHER_DIR)
    except Exception:  # noqa: BLE001
        pass
    return False


def main(argv=None) -> int:
    P.utf8_stdio()
    ap = argparse.ArgumentParser(prog="ceo_wake.py", description=__doc__.split("\n\n")[0])
    ap.add_argument("--project", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("set")
    s.add_argument("--id", default="")
    s.add_argument("--name", default="")
    sub.add_parser("show")
    a = ap.parse_args(argv)
    try:
        root = Path(a.project).resolve() if a.project else P.resolve_project([])
    except P.ProjectNotFound as e:
        print(e, file=sys.stderr)
        return 2
    CFG.configure_project(root)
    if a.cmd == "set":
        if not (a.id.strip() or a.name.strip()):
            print("set: нужен --id или --name", file=sys.stderr)
            return 2
        write_session(a.id, a.name)
    print(json.dumps(read_session(), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
