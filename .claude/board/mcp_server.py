#!/usr/bin/env python3
"""MCP-сервер экрана хода работ: stdio, JSON-RPC 2.0 без зависимостей и без модели.

Один инструмент `pulse_status` (без аргументов) → содержимое `.claude/pulse/status.json` (structuredContent + текст).
Статус пишет board_push.py (раз в 5 с).
Запуск (регистрирует владелец): python "<репозиторий>/.claude/board/mcp_server.py"
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "dispatcher"))
import project  # noqa: E402
ROOT = project.resolve_project()
STATUS = ROOT / ".claude" / "pulse" / "status.json"

PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
TOOL = {
    "name": "pulse_status",
    "description": "Ход работ проекта одним JSON. Раздел `view` — готовый человеческий вид (что идёт и где, что "
                   "дальше, вопросы владельцу, на какой машине какая задача или «без задачи», что было); `plain` — человеческие "
                   "строки (переводчик на Haiku, кэш .claude/pulse/plain-auto.json; ручное — plain.json);`tickets`/`machines`/`events` — сырые данные (тикеты, ЦП/ОЗУ/диск, "
                   "процессы, сборки, лента); время GMT+4. Источник — .claude/pulse/status.json, его раз в 5 с пишет board_push.py.",
    "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    "outputSchema": {"type": "object"},
    "annotations": {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
}


TOOL_ANSWER = {
    "name": "pulse_answer",
    "description": "Ответ владельца на вопрос табло (раздел view2.questions): id вопроса и клавиша варианта. Помечает вопрос "
                   "отвеченным, пишет в журнал задачи и в ceo-inbox (CEO записывает решение). Само решение НЕ исполняет.",
    "inputSchema": {"type": "object", "properties": {"id": {"type": "string", "description": "id вопроса, напр. q-TK-044-1"},
                                                      "key": {"type": "string", "description": "клавиша варианта, напр. a"}},
                    "required": ["id", "key"], "additionalProperties": False},
    "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
}


def pulse_answer(qid: str, key: str) -> dict:
    import ask  # печатает только CLI; здесь stdout — канал протокола
    ok, msg, warns = ask.answer_question(str(qid), str(key))
    return {"ok": ok, "message": msg, "warnings": warns}


def pulse_status() -> dict:
    try:
        st = json.loads(STATUS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise RuntimeError("status.json нет: запустите board_push.py --loop 5")
    st["stale_s"] = max(0, int(time.time() - STATUS.stat().st_mtime))
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
                res = {"content": [{"type": "text", "text": "\n".join([st["message"]] + [f"⚠ {w}" for w in st["warnings"]])}],
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
