"""Пульс (В-38), шаг 2: одно закреплённое сообщение в Telegram. Диспетчер правит его (editMessageText), новых не шлёт.

Первые две строки — шапка, как на странице («Всё идёт» / «Нужен ваш ответ» / «Застряло: <проект>» и пояснение), дальше строка на проект (`pulse_view.tg_text`). Правка — только при смене текста и не чаще раза в
минуту; `message_id` лежит в `state.json` (`pulse_tg`); сообщения нет — создаётся и закрепляется (сбой закрепления повторяется до
PIN_TRIES раз, пересозданное сообщение закрепляется заново). Запись «отправлено» в
notify.log — только при ответе API `ok` (TK-092). Включает ключ `pulse_telegram` в `.claude/rpv.json`; проекты — `pulse_projects`.
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import notify as N  # noqa: E402
import project as P  # noqa: E402
import pulse_view as V  # noqa: E402

API = "https://api.telegram.org/bot{token}/{method}"
MIN_GAP_S = 60  # не чаще раза в минуту: и чтение проектов, и правка
TIMEOUT_S = 15.0
PIN_TRIES = 3  # закрепить не вышло (нет прав) — повтор раз в минуту, но не бесконечно


def call(method: str, payload: dict, opener=urllib.request.urlopen) -> tuple:
    """(result|None, ошибка|''): ответ API с ok — result; иначе описание сбоя (сеть, HTTP, ok=false)."""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(API.format(token=P.env("TG_TOKEN"), method=method), data=body,
                                 headers={"Content-Type": "application/json; charset=utf-8"})
    try:
        with opener(req, timeout=TIMEOUT_S) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return None, json.loads(e.read().decode("utf-8")).get("description") or f"HTTP {e.code}"
        except (OSError, ValueError):
            return None, f"HTTP {e.code}"
    except (OSError, ValueError) as e:
        return None, f"{type(e).__name__}: {e}"
    return (data.get("result") or {}, "") if data.get("ok") else (None, str(data.get("description") or "ответ без ok"))


def _pin(project, st: dict, opener) -> None:
    """Закрепить сообщение, если ещё не закреплено и попытки не вышли; `pinned` ставит только ответ ok."""
    if st.get("pinned") or st.get("pin_tries", 0) >= PIN_TRIES:
        return
    st["pin_tries"] = st.get("pin_tries", 0) + 1
    _, err = call("pinChatMessage", {"chat_id": P.env("TG_CHAT"), "message_id": st["message_id"], "disable_notification": True}, opener)
    if err:
        N._log(project, f"не закреплено ({err}), попытка {st['pin_tries']} из {PIN_TRIES}")
    else:
        st["pinned"] = True


def sync(project, state: dict, views: list, now: datetime, opener=urllib.request.urlopen) -> str:
    """Привести сообщение к `views`: 'sent' (создано и закреплено), 'edited', 'same' (текст тот же), 'wait' (рано),
    'off' (Telegram не настроен), 'failed' (сбой, повтор через минуту)."""
    if not N.configured():
        return "off"
    st = state.setdefault("pulse_tg", {})
    last = P.parse_time(st.get("tried"))
    if last and (now - last).total_seconds() < MIN_GAP_S:
        return "wait"
    st["tried"] = P.now_iso(now)
    text = V.tg_text(views)[:N.LIMIT]
    chat = P.env("TG_CHAT")
    if st.get("message_id") and st.get("text") == text:
        _pin(project, st, opener)
        return "same"
    if st.get("message_id"):
        _, err = call("editMessageText", {"chat_id": chat, "message_id": st["message_id"], "text": text}, opener)
        if not err or "not modified" in err:
            st["text"] = text
            if not err:
                N._log(project, f"отправлено: правка пульса: {text[:120]}")
            _pin(project, st, opener)
            return "edited" if not err else "same"
        N._log(project, f"не отправлено (правка пульса: {err}): {text[:120]}")
        if "not found" in err or "can't be edited" in err:
            for key in ("message_id", "pinned", "pin_tries"):
                st.pop(key, None)  # сообщение удалено владельцем — следующая попытка создаст и закрепит новое
        return "failed"
    sent, err = call("sendMessage", {"chat_id": chat, "text": text, "disable_notification": True}, opener)
    if err or not sent.get("message_id"):
        N._log(project, f"не отправлено (пульс: {err or 'нет message_id'}): {text[:120]}")
        return "failed"
    st.update(message_id=sent["message_id"], text=text, pinned=False, pin_tries=0)
    N._log(project, f"отправлено: пульс создан: {text[:120]}")
    _pin(project, st, opener)
    return "sent"


def tick(project, state: dict, now: datetime, opener=urllib.request.urlopen, fetch=urllib.request.urlopen) -> str:
    """Вызов из тика диспетчера: ключ `pulse_telegram: true` и заданные RPV_TG_TOKEN/RPV_TG_CHAT — иначе 'off' и ничего не читается.
    Проектов нет — в сообщении «Проектов нет…», как на странице."""
    if P.setting("pulse_telegram", False, root=project) is not True or not N.configured():
        return "off"
    last = P.parse_time((state.get("pulse_tg") or {}).get("tried"))
    if last and (now - last).total_seconds() < MIN_GAP_S:
        return "wait"  # рано — проекты (в т. ч. удалённые по URL) не читаем
    return sync(project, state, V.load_all(project, now, fetch), now, opener)
