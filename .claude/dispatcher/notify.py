#!/usr/bin/env python3
"""Сообщение владельцу в Telegram (CP5, CP10): тревога «работа стоит» и ежедневный отчёт. Только отправка, только stdlib.

    notify.py [--project <путь>] send "текст"     # одно сообщение владельцу
    notify.py [--project <путь>] test             # проверка связи: «связь есть»

Бот и чат — переменные окружения `RPV_TG_TOKEN` (токен бота от @BotFather) и `RPV_TG_CHAT` (чат владельца); в репозитории
их нет. Не заданы — отправки нет, строка в `<проект>/.claude/dispatcher/notify.log`. Сбой сети — повтор, потом строка в
лог; исключений наружу нет: тревога не должна ронять диспетчер.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import project as P  # noqa: E402

API = "https://api.telegram.org/bot{token}/sendMessage"
TRIES = 3
TIMEOUT_S = 15.0
LIMIT = 4000  # знаков в сообщении (у Telegram 4096)


def configured() -> bool:
    return bool(P.env("TG_TOKEN") and P.env("TG_CHAT"))


def _log(project, line: str, log_dir=None) -> None:
    if not (project or log_dir):
        return
    try:
        d = Path(log_dir) if log_dir else Path(project) / ".claude" / "dispatcher"
        d.mkdir(parents=True, exist_ok=True)
        with (d / "notify.log").open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now().astimezone().isoformat(timespec='seconds')}\t{line}\n")
    except OSError:
        pass


def send(text: str, project=None, opener=urllib.request.urlopen, sleep=time.sleep, log_dir=None) -> bool:
    """True — Telegram принял сообщение. Не настроено, пустой текст, сбой — False и строка в notify.log (каталог
    состояния проекта или `log_dir`)."""
    text = (text or "").strip()
    if not text:
        return False
    if not configured():
        _log(project, f"не отправлено (нет RPV_TG_TOKEN/RPV_TG_CHAT): {text[:120]}", log_dir)
        return False
    body = json.dumps({"chat_id": P.env("TG_CHAT"), "text": text[:LIMIT]}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(API.format(token=P.env("TG_TOKEN")), data=body,
                                 headers={"Content-Type": "application/json; charset=utf-8"})
    err = ""
    for i in range(TRIES):
        try:
            with opener(req, timeout=TIMEOUT_S) as r:
                if json.loads(r.read().decode("utf-8")).get("ok"):
                    _log(project, f"отправлено: {text[:120]}", log_dir)
                    return True
                err = "ответ без ok"
        except urllib.error.HTTPError as e:  # 4xx — токен/чат неверны: повтор не поможет
            err = f"HTTP {e.code}"
            if 400 <= e.code < 500:
                break
        except (OSError, ValueError) as e:
            err = f"{type(e).__name__}: {e}"
        if i + 1 < TRIES:
            sleep(2 * (i + 1))
    _log(project, f"не отправлено ({err}): {text[:120]}", log_dir)
    return False


def main(argv=None) -> int:
    P.utf8_stdio()
    ap = argparse.ArgumentParser(prog="notify.py", description=__doc__.split("\n\n")[0])
    ap.add_argument("--project", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("send")
    s.add_argument("text")
    sub.add_parser("test")
    a = ap.parse_args(argv)
    try:
        project = Path(a.project).resolve() if a.project else P.resolve_project([])
    except P.ProjectNotFound:
        project = None
    if not configured():
        print("Telegram не настроен: задайте RPV_TG_TOKEN и RPV_TG_CHAT", file=sys.stderr)
        return 1
    ok = send(a.text if a.cmd == "send" else "Связь есть: сюда придут отчёт и тревога «работа стоит».", project)
    print("отправлено" if ok else "не отправлено — причина в .claude/dispatcher/notify.log")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
