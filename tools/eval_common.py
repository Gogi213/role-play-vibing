"""Общее для стендов замера служебных моделей: `tools/helper_eval.py` (помощники CEO) и `tools/overseer_eval.py` (Надзиратель).
Печать в UTF-8, замок записи результатов, чтение/дозапись `*.jsonl`, разбор JSON-ответа `claude -p --output-format json`."""
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

LOCK = threading.Lock()  # дозапись результатов из потоков прогона


def emit(text: str) -> None:
    """Печать в UTF-8 (консоль Windows по умолчанию не она)."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(text)


def read_jsonl(path: Path, missing_ok: bool = False) -> list:
    """Строки `*.jsonl` как словари; нет файла — ошибка, с missing_ok — пусто."""
    if missing_ok and not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def append_jsonl(path: Path, row: dict) -> None:
    """Дописать строку под общим замком."""
    with LOCK, path.open("a", encoding="utf-8") as w:
        w.write(json.dumps(row, ensure_ascii=False) + "\n")


def parse_claude_json(raw: str, tail: int = 2000) -> dict:
    """Ответ `claude -p --output-format json`; не JSON — `{"is_error": True, "result": хвост сырого вывода}`."""
    try:
        return json.loads(raw)
    except ValueError:
        return {"is_error": True, "result": raw[-tail:]}
