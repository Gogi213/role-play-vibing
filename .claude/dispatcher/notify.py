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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import project as P  # noqa: E402

API = "https://api.telegram.org/bot{token}/sendMessage"
ME = "https://api.telegram.org/bot{token}/getMe"   # проверка токена без сообщения (doctor)
TRIES = 3
TIMEOUT_S = 15.0
LIMIT = 4000  # знаков в сообщении (у Telegram 4096)
RETRY_S = 300.0  # не доставленное при настроенном Telegram повторяется не чаще раза в 5 мин: тик не ждёт сеть каждый раз


def configured() -> bool:
    return bool(P.env("TG_TOKEN") and P.env("TG_CHAT"))


def _log(project, line: str, log_dir=None) -> None:
    if not (project or log_dir):
        return
    try:
        d = Path(log_dir) if log_dir else Path(project) / ".claude" / "dispatcher"
        d.mkdir(parents=True, exist_ok=True)
        with (d / "notify.log").open("a", encoding="utf-8") as f:
            f.write(f"{P.now_iso()}\t{line}\n")
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


def delivered(text: str, **kw) -> bool:
    """Сообщение дошло или слать некуда: `send()` True, либо Telegram не настроен вовсе (повторять незачем). Настроен, но
    сбой (401, сеть) — False: «отправлено» не ставим, повтор позже (TK-092: двое суток 401 при «отправлено»)."""
    return send(text, **kw) or not configured()


def episode(st: dict, flag: str, firing: bool, cleared: bool, down, up, log_dir=None, ceo_down=None, ceo_up=None) -> None:
    """Одна тревога на эпизод (TK-010): `firing` и флага `st[flag]` нет — `down()` уходит владельцу (и в `ceo_down(text)`, если
    задана строка CEO), флаг встаёт; `cleared` и флаг есть — `up()` уходит так же, флаг снимается. Пока ни то ни другое —
    ничего. Текст строится функцией только в момент отправки: поле, которого нет вне эпизода, тревоге не мешает."""
    try_key = flag + "_try"   # время последней недоставленной попытки: CEO строку получает один раз, повтор — не чаще RETRY_S
    now = time.time()
    if firing and not st.get(flag):
        step, ceo, want = down, ceo_down, True
    elif cleared and st.get(flag):
        step, ceo, want = up, ceo_up, False
    else:
        if not firing:
            st.pop(try_key, None)
        return
    if now - st.get(try_key, -RETRY_S) < RETRY_S:
        return
    text = step()
    if ceo and try_key not in st:
        ceo(text)
    if delivered(text, log_dir=log_dir):
        st[flag] = want
        st.pop(try_key, None)
    else:
        st[try_key] = now


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
