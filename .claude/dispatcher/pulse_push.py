"""Пульс (TK-113, В-56): проект сам отправляет свой `pulse.json` на сервер страницы — ssh-ключ на клиенте не нужен.

После тика диспетчер шлёт файл `PUT <url>/put/<имя>` с `Authorization: Bearer <ключ записи>` (ключ в заголовке, не в адресе: адрес
оседает в журналах). Не чаще раза в минуту; таймаут короткий, сбой тик не валит и повторяется через минуту. В `notify.log` — только
смена состояния (первый сбой, новая причина, восстановление), чтобы журнал не рос на каждой минуте простоя сети. Ключ `pulse_push`
в `.claude/rpv.json`: `{"url": "https://хост:порт", "name": "rpv", "key": "<ключ записи>"}`; ключа нет — модуль ничего не делает и
в сеть не ходит. Разовая отправка: `python pulse_push.py --project <проект>` (код 0 — принято). Приём — `board/pulse_server.py`."""
from __future__ import annotations

import argparse
import http.client
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import notify as N  # noqa: E402
import project as P  # noqa: E402

MIN_GAP_S = 60
TIMEOUT_S = 4.0  # тик диспетчера ждёт ответа: короче, чем у Telegram (15 с)
NAME = re.compile(r"[A-Za-z0-9_-]{1,40}")  # то же, что принимает сервер
KEY = re.compile(r"[A-Za-z0-9._~+/=-]{16,200}")  # годится в заголовок как есть
URL = re.compile(r"https://[\w.-]+(:\d{1,5})?|http://(127\.0\.0\.1|localhost)(:\d{1,5})?")  # голый http — только на этой же машине (туннель)


def valid(cfg) -> bool:
    """Ключ `pulse_push` в порядке: объект с адресом сервера (https, голый http — только localhost), именем проекта и ключом записи."""
    return (isinstance(cfg, dict) and set(cfg) == {"url", "name", "key"} and all(isinstance(v, str) for v in cfg.values())
            and URL.fullmatch(cfg["url"]) is not None and NAME.fullmatch(cfg["name"]) is not None and KEY.fullmatch(cfg["key"]) is not None)


def config(project):
    """Настройка отправки проекта; нет ключа или он не в порядке — None."""
    cfg = P.setting("pulse_push", None, root=project)
    return cfg if valid(cfg) else None


def source(project) -> Path:
    return Path(project) / ".claude" / "dispatcher" / "pulse.json"


def send(cfg: dict, body: bytes, opener=urllib.request.urlopen) -> str:
    """Отправить `body` на сервер: '' — принято, иначе причина сбоя (без ключа)."""
    req = urllib.request.Request(f"{cfg['url']}/put/{cfg['name']}", data=body, method="PUT",
                                 headers={"Authorization": f"Bearer {cfg['key']}", "Content-Type": "application/json; charset=utf-8"})
    try:
        with opener(req, timeout=TIMEOUT_S) as r:
            return "" if 200 <= getattr(r, "status", 200) < 300 else f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code}"
    except (OSError, ValueError, http.client.HTTPException) as e:  # URLError, ssl, таймаут — OSError
        return f"{type(e).__name__}: {e}"


def tick(project, state: dict, now: datetime, opener=urllib.request.urlopen) -> str:
    """Вызов из тика диспетчера: 'off' (не настроено), 'wait' (рано), 'nofile' (pulse.json нет), 'sent', 'failed'."""
    cfg = config(project)
    if cfg is None:
        return "off"
    st = state.setdefault("pulse_push", {})
    last = P.parse_time(st.get("tried"))
    if last and (now - last).total_seconds() < MIN_GAP_S:
        return "wait"
    st["tried"] = P.now_iso(now)
    try:
        body = source(project).read_bytes()
    except OSError:
        return "nofile"
    err = send(cfg, body, opener)
    if err != st.get("err", ""):
        N._log(project, f"пульс на сервер: {'не отправлено (' + err + ')' if err else 'снова отправляется'}")
    st["err"] = err
    if not err:
        st["ok"] = st["tried"]
    return "failed" if err else "sent"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="pulse_push.py", description="разово отправить pulse.json проекта на сервер страницы")
    ap.add_argument("--project", required=True)
    a = ap.parse_args(argv)
    cfg = config(a.project)
    if cfg is None:
        print("[pulse_push] в .claude/rpv.json нет pulse_push {url, name, key} или он не в порядке", file=sys.stderr)
        return 2
    try:
        body = source(a.project).read_bytes()
    except OSError as e:
        print(f"[pulse_push] pulse.json не читается: {e}", file=sys.stderr)
        return 2
    err = send(cfg, body)
    print(f"[pulse_push] {cfg['name']} -> {cfg['url']}: " + (f"не принято ({err})" if err else "принято"))
    return 1 if err else 0


if __name__ == "__main__":
    sys.exit(main())
