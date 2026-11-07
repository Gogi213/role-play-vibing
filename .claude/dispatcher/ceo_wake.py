#!/usr/bin/env python3
"""Побудка CEO событием (TK-085, В-33/В-35): будильников нет — CEO будят тогда, когда это нужно.

Диспетчер на событие поднимает один headless-запуск `claude -p` (Haiku, только ListAgents и SendMessage) и шлёт в сессию CEO
одну строку. События — четыре (EVENT_OF): нужно решение владельца; сломалось или сорван срок; шаг цели принят, следующий не
определён; цель закрыта. Обычный `done` тикета — не событие. Сессию CEO называет `/ceo`:

    ceo_wake.py [--project <путь>] set --id <id сессии> [--name <имя>]    # `/ceo`: кто CEO
    ceo_wake.py [--project <путь>] show

Сессию ищут по имени: `ListAgents` id не показывает, `SendMessage` адресует по имени (TK-110). Доставкой считается только ответ
запуска `DELIVERED`; сессия не найдена, `SendMessage` отказал или запуск не вышел — строка в `ceo-inbox.md` и сообщение владельцу
в Telegram, исключений наружу нет. Запуск идёт в фоне одной очередью: события, пришедшие, пока он занят, уходят одним запуском.
Режим разрешений запуска — ключ `ceo_wake_mode` (умолчание `bypassPermissions`): он обязан совпадать с режимом сессии CEO, иначе
сообщение повисает на одобрении и не будит. Отключить — `ceo_wake: false`."""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import threading
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
DELIVERED = "DELIVERED"  # запуск пишет это, когда SendMessage вернул успех; без маркера доставки нет
FAILED = "FAILED"
BACKGROUND = True  # тесты ставят False: запуск идёт в том же потоке

# вид сигнала диспетчера → событие. Остальные виды CEO не будят: они только строки очереди (`tickets.py inbox`)
EVENT_OF = {
    "needs_owner": "нужно решение владельца",
    "blocked": "сломалось",
    "auth": "сломалось",
    "due": "сорван срок",
    "следующий-не-определён": "шаг цели принят, следующий не определён",  # вид шлёт goal_steps.advance (TK-087)
    "watch-goal-idle": "шаг цели принят, следующий не определён",  # у цели принят шаг, тикета на неё нет (TK-086)
    "goal-done": "цель закрыта",
    "watch-unreleased": "влито, не выпущено",  # сторож `unreleased`: очередь пуста, в основной ветке есть невыпущенное (TK-062, TK-110)
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
    """Имя не передано — остаётся прежнее: оно переживает `/clear` (новый id сессии, то же имя в `ListAgents`)."""
    name = " ".join(name.split()) or str(read_session().get("name") or "")
    session_file().parent.mkdir(parents=True, exist_ok=True)
    session_file().write_text(json.dumps({"id": sid.strip(), "name": name, "at": P.now_iso()}, ensure_ascii=False),
                              encoding="utf-8")


def _prompt(session: dict, lines: list) -> str:
    name, sid = session.get("name"), session.get("id")
    who = f"с именем {json.dumps(name, ensure_ascii=False)}" if name else f"с идентификатором {json.dumps(sid, ensure_ascii=False)}"
    return (f"Вызови ListAgents. Найди сессию {who}. Нет такой — выведи ровно {NOT_FOUND} и остановись. Есть — отправь ей через "
            f"SendMessage (to — имя сессии) ровно одно сообщение, дословно:\n" + "\n".join(lines) + "\n"
            f"SendMessage вернул успех — выведи ровно {DELIVERED}. Вернул отказ или ошибку — выведи {FAILED} и причину. "
            "Больше ничего не делай и не пиши.")


def _launch(session: dict, lines: list, runner) -> tuple[bool, str]:
    """(доставлено, причина). Один вызов `claude -p`; доставка — только маркер DELIVERED в ответе."""
    claude = haiku_aux._claude()
    if not claude:
        return False, "claude не найден"
    mode = str(P.setting("ceo_wake_mode", DEFAULT_MODE))
    cmd = [claude, "-p", "--model", MODEL, "--permission-mode", mode, "--allowedTools", "ListAgents,SendMessage"]
    try:
        with tempfile.TemporaryDirectory(prefix="rpv-wake-") as cwd:  # вне проекта: без CLAUDE.md и хуков
            r = haiku_aux.run_claude(cmd, _prompt(session, lines), TIMEOUT_S, cwd=cwd, runner=runner)
    except Exception as e:  # noqa: BLE001 — побудка не роняет диспетчер
        return False, f"{type(e).__name__}: {e}"
    out = (r.stdout or b"").decode("utf-8", "replace")
    if r.returncode != 0:
        return False, f"код {r.returncode}: {((r.stderr or b'').decode('utf-8', 'replace') or out).strip()[:120]}"
    if NOT_FOUND in out:
        return False, "сессия CEO не найдена в ListAgents"
    if FAILED in out:
        return False, f"SendMessage отказал: {' '.join(out.split())[:120]}"
    if DELIVERED not in out:
        return False, f"нет подтверждения доставки: {' '.join(out.split())[:120] or 'пустой ответ'}"
    return True, ""


_LOCK = threading.Lock()
_QUEUE: list = []  # события, ждущие запуска: (tid, строка, write_inbox, now, runner, send)
_WORKER: "threading.Thread | None" = None


def _fail(batch: list, why: str, inbox: bool = True) -> None:
    """Отказ: по строке в очередь CEO на событие (кроме «сессия не названа» — та идёт в mechanics.log) и одно сообщение владельцу."""
    for tid, line, write_inbox, now, _runner, _send in batch:
        if inbox:
            write_inbox(tid, "ceo-не-разбужен", f"{why}; событие: {line}", now)
    send = batch[0][5] or notify.send
    try:
        send(f"CEO не разбужен ({why}). " + " | ".join(item[1] for item in batch), log_dir=CFG.DISPATCHER_DIR)
    except Exception:  # noqa: BLE001
        pass


def _deliver(batch: list) -> bool:
    session = read_session()
    if not session:
        why = "сессия CEO не названа (`/ceo` не записал ceo-session.json)"
        try:
            with (CFG.DISPATCHER_DIR / "mechanics.log").open("a", encoding="utf-8") as f:
                for tid, line, _w, now, _r, _s in batch:
                    f.write(f"{P.now_iso(now)} {tid} [ceo-не-разбужен] {why}; событие: {line}\n")
        except OSError:
            pass
        _fail(batch, why, inbox=False)
        return False
    ok, why = _launch(session, [item[1] for item in batch], batch[0][4] or haiku_aux.hide.run)
    if not ok:
        _fail(batch, why)
    return ok


def _drain() -> None:
    global _WORKER
    while True:
        with _LOCK:
            batch = list(_QUEUE)
            _QUEUE.clear()
            if not batch:
                _WORKER = None
                return
        try:
            _deliver(batch)
        except Exception:  # noqa: BLE001 — побудка не роняет диспетчер
            pass


def wait_idle(timeout: float = 5.0) -> None:
    """Дождаться, пока очередь побудки отправлена (тесты, выход из однократных запусков)."""
    worker = _WORKER
    if worker is not None:
        worker.join(timeout)


def wake(tid: str, kind: str, note: str, write_inbox, now=None, runner=None, send=None) -> bool:
    """Событие `kind` → одна строка в сессию CEO. Не событие — False, запусков нет. Иначе событие встаёт в очередь, и вызов
    возвращается сразу (запуск до 90 с не держит тик); True — событие принято. Исход доставки — не здесь: отказ пишется в
    `ceo-inbox.md` (`write_inbox(тикет, вид, текст, now)` — подставляет signals, без побудки) и уходит владельцу в Telegram."""
    global _WORKER
    event = EVENT_OF.get(kind)
    if not event or P.setting("ceo_wake", True) is False:
        return False
    line = f"[{event}] {tid} — {' '.join(str(note).split())[:160]}. Прочитай `tickets.py inbox`."
    item = (tid, line, write_inbox, now, runner, send)
    if not BACKGROUND:
        return _deliver([item])
    with _LOCK:
        _QUEUE.append(item)
        if _WORKER is None:
            _WORKER = threading.Thread(target=_drain, name="ceo-wake", daemon=False)
            _WORKER.start()
    return True


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
    session = read_session()
    print(json.dumps(session, ensure_ascii=False))
    if a.cmd == "set" and not session.get("name"):
        print("ПРЕДУПРЕЖДЕНИЕ: имя сессии не записано — побудка не найдёт эту сессию (ListAgents id не показывает). "
              "Возьми имя из первой строки ListAgents («This session is <имя>») и повтори set с --name.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
