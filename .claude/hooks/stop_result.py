"""Stop: запуск роли диспетчера не кончается без записи в лог тикета (TK-079, В-195).

Роль, запущенная диспетчером (RPV_ROLE + RPV_TICKET), не оставила за запуск ни одной записи своим заголовком —
Stop блокируется с причиной «сообщи итог». Строгий режим RPV_STOP_STRICT=1 требует именно команду итога
(запись `[итог: …]`). Повторный Stop после блокировки (stop_hook_active) пропускается — без вечной петли. Не запуск
диспетчера, нет зеркала запуска в state.json или любой сбой — молча разрешает: хук не должен держать сессию.
"""
import json
import os
import sys


def _env(name):
    return os.environ.get("RPV_" + name) or ""


def main():
    role, tid = _env("ROLE").strip().lower(), _env("TICKET").strip()
    if not role or not tid:
        return
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        data = {}
    if data.get("stop_hook_active"):
        return
    root = os.path.abspath(os.environ.get("CLAUDE_PROJECT_DIR") or _env("PROJECT") or os.getcwd())
    disp = _env("DISPATCHER_DIR") or os.path.join(root, ".claude", "dispatcher")
    try:
        with open(os.path.join(disp, "state.json"), encoding="utf-8") as f:
            run = (json.load(f).get("active_runs") or {}).get(tid)
        if not run or "log_keys_at_launch" not in run:
            return
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dispatcher"))
        import ticket as T
        tkt = T.read_ticket(os.path.join(root, ".claude", "tickets", f"{tid}.md"))
        before = set(run["log_keys_at_launch"])
        strict = _env("STOP_STRICT") == "1"
        for e in tkt.log:
            if T.author_is(e.author, role) and f"{e.ts_raw} {e.author}" not in before:
                if not strict or e.text.lstrip().startswith("[итог:"):
                    return
    except Exception:
        return
    how = f"tickets.py result {tid} <итог> --why \"…\"" if strict else f"tickets.py comment {tid} --author {role} --text \"…\""
    print(json.dumps({"decision": "block",
                      "reason": f"За этот запуск нет записи в логе {tid}. Сообщи итог: {how} — что сделал, что дальше."}))


if __name__ == "__main__":
    main()
