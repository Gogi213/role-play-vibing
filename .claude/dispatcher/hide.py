"""Служебные процессы плагина без видимых окон (TK-105 п.3, п.5, п.6). На не-Windows — обычный subprocess."""
from __future__ import annotations

import os
import subprocess

CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_CONSOLE = 0x00000010
CREATE_NEW_PROCESS_GROUP = 0x00000200
DEFAULT_TIMEOUT_S = 120.0   # run() без своего таймаута не висит часами (сирота ssh/git); по таймауту процесс убит


def hidden() -> dict:
    """kwargs для коротких служебных процессов: без консоли вовсе (им она не нужна)."""
    return {"creationflags": CREATE_NO_WINDOW} if os.name == "nt" else {}


def hidden_console() -> dict:
    """kwargs для сессии роли: СКРЫТАЯ консоль, а не её отсутствие — внуки (bash, ssh, git) наследуют эту консоль, а при
    CREATE_NO_WINDOW у родителя каждый получает свою новую видимую."""
    if os.name != "nt":
        return {"start_new_session": True}
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0  # SW_HIDE
    return {"creationflags": CREATE_NEW_CONSOLE | CREATE_NEW_PROCESS_GROUP, "startupinfo": si}


def run(cmd, **kw) -> subprocess.CompletedProcess:
    """subprocess.run: скрытое окно и таймаут по умолчанию; по таймауту процесс убит (TimeoutExpired — вызывающему)."""
    for k, v in hidden().items():
        kw.setdefault(k, v)
    kw.setdefault("timeout", DEFAULT_TIMEOUT_S)
    return subprocess.run(cmd, **kw)
