#!/usr/bin/env python3
"""MCP-сервер «rpv-pulse»: stdio, JSON-RPC 2.0 без зависимостей и без модели.

Инструмент `pulse_status` (без аргументов) отдаёт содержимое `<проект>/.claude/pulse/status.json` (structuredContent +
текст) и поле `stale_s` — возраст кадра в секундах. Файл раз в 5 с пишет `board_push.py --loop 5` (плагин,
`.claude/dispatcher/`); если файла нет — ошибка с подсказкой, как поднять. Инструмент `pulse_answer` (id вопроса и
клавиша варианта) — ответ владельца на вопрос табло: то же, что `ask.py answer`.

Корень проекта — `--project <путь>`, иначе RPV_PROJECT, CLAUDE_PROJECT_DIR, иначе поиск вверх от текущего каталога.
Регистрация: `claude mcp add rpv-pulse -- python "<проект>/.claude/board/mcp_server.py" --project "<проект>"`.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "dispatcher"))
import project  # noqa: E402

PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
TOOL = {
    "name": "pulse_status",
    "description": "Ход работ проекта одним JSON: раздел `view2` — готовый вид табло (заголовок, счётчики, процессы, "
                   "вопросы владельцу, машины, лента); `built_at` — когда собран кадр; `stale_s` — его возраст в секундах "
                   "(больше 30 — `board_push.py --loop` не запущен). Источник — .claude/pulse/status.json.",
    "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    "outputSchema": {"type": "object"},
    "annotations": {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
}
TOOL_ANSWER = {
    "name": "pulse_answer",
    "description": "Ответ владельца на вопрос табло (раздел view2.questions): id вопроса и клавиша варианта. Помечает вопрос "
                   "отвеченным и пишет в журнал задачи. Само решение НЕ исполняет.",
    "inputSchema": {"type": "object", "properties": {"id": {"type": "string", "description": "id вопроса, напр. q-TK-044-1"},
                                                      "key": {"type": "string", "description": "клавиша варианта, напр. a"}},
                    "required": ["id", "key"], "additionalProperties": False},
    "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
}

ROOT: Path | None = None


def root() -> Path:
    global ROOT
    if ROOT is None:
        ROOT = project.resolve_project(sys.argv[1:])
        os.environ["RPV_PROJECT"] = str(ROOT)  # ask.py/pulsedata.py ищут проект по нему
    return ROOT


def pulse_answer(qid: str, key: str) -> dict:
    root()
    sys.path.insert(0, str(HERE.parent / "dispatcher"))
    import ask  # печатает только CLI; здесь stdout — канал протокола
    ok, msg, warns = ask.answer_question(str(qid), str(key))
    return {"ok": ok, "message": msg, "warnings": warns}


def pulse_status() -> dict:
    path = root() / ".claude" / "pulse" / "status.json"
    try:
        st = json.loads(path.read_text(encoding="utf-8"))
        mtime = path.stat().st_mtime
    except OSError:
        raise RuntimeError(f"{path} нет: запустите `python .claude/dispatcher/board_push.py --loop 5`") from None
    except ValueError:
        raise RuntimeError(f"{path} не читается: битый JSON (кадр пишется атомарно — повторите запрос)") from None
    if not isinstance(st, dict):
        raise RuntimeError(f"{path}: ожидался объект JSON")
    st = dict(st)
    built = (st.get("view2") or {}).get("built_ts") or mtime
    try:
        st["stale_s"] = max(0, int(time.time() - float(built)))
    except (TypeError, ValueError):
        st["stale_s"] = max(0, int(time.time() - mtime))
    return st


def handle(msg: dict):
    """Ответ на запрос (dict) или None для уведомления."""
    mid, method = msg.get("id"), msg.get("method", "")
    if mid is None:  # уведомление (notifications/initialized и др.) — без ответа
        return None
    params = msg.get("params") or {}
    if method == "initialize":
        want = params.get("protocolVersion")
        res = {"protocolVersion": want if want in PROTOCOLS else PROTOCOLS[0],
               "capabilities": {"tools": {"listChanged": False}},
               "serverInfo": {"name": "rpv-pulse", "version": "1.1.0"}}
    elif method == "ping":
        res = {}
    elif method == "tools/list":
        res = {"tools": [TOOL, TOOL_ANSWER]}
    elif method == "tools/call":
        if params.get("name") not in (TOOL["name"], TOOL_ANSWER["name"]):
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": f"неизвестный инструмент: {params.get('name')}"}}
        try:
            if params["name"] == TOOL_ANSWER["name"]:
                args = params.get("arguments") or {}
                st = pulse_answer(args.get("id", ""), args.get("key", ""))
                res = {"content": [{"type": "text", "text": "\n".join([st["message"]] + [f"! {w}" for w in st["warnings"]])}],
                       "structuredContent": st, "isError": not st["ok"]}
                return {"jsonrpc": "2.0", "id": mid, "result": res}
            st = pulse_status()
            res = {"content": [{"type": "text", "text": json.dumps(st, ensure_ascii=False)}], "structuredContent": st}
        except Exception as e:
            res = {"content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}], "isError": True}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"метод не поддерживается: {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": res}


def main() -> int:
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
            out = handle(msg) if isinstance(msg, dict) else \
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "ожидался объект JSON-RPC"}}
        except ValueError:
            out = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "битый JSON"}}
        if out is not None:
            sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
