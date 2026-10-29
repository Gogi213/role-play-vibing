"""Порог контекста сессии CEO (TK-052, В-15, G-002): расход подписки растёт с контекстом, автосжатие при окне 1 млн срабатывает
на 97 % — практически никогда.

- контекст ≥ `ceo_context_warn` (350 тыс.) — на каждое сообщение владельца CEO получает вставку «допиши блокнот и попроси /clear»;
- контекст ≥ `ceo_context_stop` (400 тыс.) — хардстоп: сообщение в модель не уходит, владелец видит причину и что делать.

Контекст — вход + запись кэша + чтение кэша последнего ответа ассистента (`transcript.last_turn_tokens`: строки одного
`message.id` — один ответ, подагенты не в счёт). Пороги — `.claude/rpv.json`, ключи `ceo_context_warn` / `ceo_context_stop`
(токены); нет ключа или значение неверное (не целое, ≤ 0, предупреждение не ниже остановки) — оба умолчания. Другие роли модуль
не вызывают; `/clear` и `/compact` проходят всегда — иначе из хардстопа не выйти. Модуль без импортов диспетчера: его читает хук."""
import json
import os
import re

WARN_DEFAULT = 350_000
STOP_DEFAULT = 400_000
NOTEBOOK = ".claude/roles/notes/ceo.md"
ESCAPE = re.compile(r"^\s*/(?:clear|compact)\b", re.I)   # выход из хардстопа

WARN_TEXT = (f"[контекст CEO] контекст {{n}} тыс. ≥ {{warn}}: допиши блокнот {NOTEBOOK} и попроси владельца /clear — "
             "скажи ему это первой строкой ответа. На {stop} тыс. сообщения владельца перестанут доходить до модели.")
STOP_TEXT = ("Контекст сессии CEO {n} тыс. ≥ {stop} тыс. — сообщение не отправлено в модель (расход подписки растёт с "
             f"контекстом). Сделай /clear (блокнот {NOTEBOOK} уже должен быть записан на {{warn}} тыс.) и напиши снова.")


def setting(key, default, root):
    """Ключ `<root>/.claude/rpv.json`; файла или ключа нет, файл не читается — `default`."""
    try:
        with open(os.path.join(root, ".claude", "rpv.json"), encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return default
    return data.get(key, default) if isinstance(data, dict) else default


def _tokens(v):
    return isinstance(v, int) and not isinstance(v, bool) and v > 0


def limits(root):
    """(предупреждение, остановка) в токенах: из настроек проекта, при любой неправильности — оба умолчания."""
    warn, stop = setting("ceo_context_warn", WARN_DEFAULT, root), setting("ceo_context_stop", STOP_DEFAULT, root)
    return (warn, stop) if _tokens(warn) and _tokens(stop) and warn < stop else (WARN_DEFAULT, STOP_DEFAULT)


def verdict(tokens, prompt, warn, stop):
    """`("stop", причина)`, `("warn", вставка)` или None."""
    kw = {"n": tokens // 1000, "warn": warn // 1000, "stop": stop // 1000}
    if tokens >= stop and not ESCAPE.match(prompt or ""):
        return "stop", STOP_TEXT.format(**kw)
    if tokens >= warn:
        return "warn", WARN_TEXT.format(**kw)
    return None
