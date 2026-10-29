"""Настройки плагина `.claude/rpv.json` — правка только командой (TK-007, В-7: CEO менять может, но без самодеятельности).

    tickets.py config set <ключ> <значение> --why "..." [--ref В-N]   # только CEO: ключ из закрытого списка, значение проверено
    tickets.py config accept CFG-N                                    # только Судья: правка принята, откат отменён

Прямую запись файла страж отказывает (`delete_guard`); команда пишет файл, строку в журнал `dispatcher/config-log.json` и
заводит Судье тикет-проверку. Правка, которую Судья не принял, сама откатывается через `ROLLBACK_AFTER_S` (2 ч): сторож
вызывает `rollback_due` каждый тик. Журнал читает ежедневный отчёт (раздел «CEO изменил работу команды»).
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path

import project as P
import registry as registry_mod

ROLLBACK_AFTER_S = 2 * 3600
LOG_NAME = "config-log.json"
ABSENT = None                                    # «ключа не было» в журнале: откат удаляет ключ


def _text(v) -> bool:
    return isinstance(v, str) and bool(v.strip())


def _int_min(v, lo=1) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= lo


def _str_list(v) -> bool:
    return isinstance(v, list) and bool(v) and all(_text(x) for x in v)


def _role_parallel(v) -> bool:
    return isinstance(v, dict) and bool(v) and all(k in P.ROLES and _int_min(n) for k, n in v.items())


def _role_mcp(v) -> bool:
    return v in ("all", "none") or _str_list(v)


# ключ -> (проверка значения, допустимое словами); список — из docs/configuration.md, новый ключ добавляют и туда
KEYS = {
    "protected_paths": (_str_list, "список шаблонов путей"),
    "report_at": (lambda v: isinstance(v, str) and re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", v) is not None, "время ЧЧ:ММ"),
    "max_parallel": (_int_min, "целое ≥ 1"),
    "max_runs_per_ticket_hour": (_int_min, "целое ≥ 1"),
    "role_parallel": (_role_parallel, "объект {роль: целое ≥ 1}, роли researcher|engineer|judge"),
    "role_mcp": (_role_mcp, "all, none или список имён серверов"),
    "ci_repo": (lambda v: isinstance(v, str) and re.fullmatch(r"[\w.-]+/[\w.-]+", v) is not None, "владелец/репо"),
    "watched_aliases": (_str_list, "список ssh-алиасов"),
    "job_state_cmd": (_text, "непустая строка"),
    "job_owners_cmd": (_text, "непустая строка"),
    "stray_cmd": (_text, "непустая строка"),
    "guard_remote_roots": (_str_list, "список каталогов"),
    "guard_stage": (_text, "непустая строка"),
    "guard_forbidden_hosts": (_str_list, "список хостов"),
    "guard_push_scope": (lambda v: isinstance(v, bool), "true или false"),
    "hold_alive_waits": (lambda v: isinstance(v, bool), "true или false"),
    "ceo_context_warn": (_int_min, "целое ≥ 1, токены контекста; ниже ceo_context_stop"),
    "ceo_context_stop": (_int_min, "целое ≥ 1, токены контекста; выше ceo_context_warn"),
}


def log_path(root: Path) -> Path:
    return Path(root) / ".claude" / "dispatcher" / LOG_NAME


def rpv_path(root: Path) -> Path:
    return Path(root) / ".claude" / "rpv.json"


def read_log(root: Path) -> list:
    return P.read_log(log_path(root))


def _read_rpv(root: Path):
    """(объект, ошибка): нет файла — ({}, None); не объект JSON — ошибка (чужой файл не затираем)."""
    p = rpv_path(root)
    if not p.exists():
        return {}, None
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as e:
        return {}, f"{p.name} не читается ({type(e).__name__})"
    return (data, None) if isinstance(data, dict) else ({}, f"{p.name} — не объект JSON")


def parse_value(raw: str):
    """Значение из командной строки: JSON, если разбирается (`3`, `["a"]`, `{"engineer": 2}`), иначе строка как есть."""
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def quote_of(root: Path, ref: str) -> str:
    """Слова владельца дословно из блока решения В-N (строки `> …` до «Суть:»)."""
    block = registry_mod.decisions(root).get(ref, "")
    lines = [ln[1:].strip() for ln in block.splitlines() if ln.startswith(">")]
    return " ".join(x for x in lines if x)


def set_value(root: Path, key: str, raw: str, why: str, ref: str = "", now=None):
    """→ (запись журнала, None) или (None, причина отказа). Файл не трогается, пока всё не проверено."""
    now = now or datetime.now().astimezone()
    if key not in KEYS:
        return None, f"ключ {key!r} не из списка: {', '.join(KEYS)}"
    check, words = KEYS[key]
    value = parse_value(raw)
    if not check(value):
        return None, f"значение {raw!r} для {key} не подходит: нужно {words}"
    if not (why or "").strip():
        return None, "нужно --why «зачем»: Судья сверяет его со словом владельца"
    quote = question = ""
    if ref:
        if not re.fullmatch(r"В-\d+", ref) or not registry_mod.ref_exists(root, ref):
            return None, f"--ref {ref}: такого решения нет в реестре (`registry.py list`)"
        quote = quote_of(root, ref)
        question = registry_mod.question_of(registry_mod.decisions(root).get(ref, ""))
    cur, err = _read_rpv(root)
    if err:
        return None, err
    log = read_log(root)
    old = cur.get(key, ABSENT)
    if old == value:
        return None, f"{key} уже равен {json.dumps(value, ensure_ascii=False)} — менять нечего"
    for e in log:                                # новая правка того же ключа наследует «прежнее» непринятой: откат вернёт его
        if e.get("key") == key and e.get("state") == "pending":
            old = e.get("old")
            e["state"] = "superseded"
    cur[key] = value
    P.write_json(rpv_path(root), cur)
    entry = {"id": f"CFG-{len(log) + 1}", "at": now.isoformat(timespec="seconds"), "key": key, "old": old, "new": value,
             "why": " ".join(why.split()), "ref": ref, "quote": quote, "question": question, "state": "pending",
             "deadline": (now + timedelta(seconds=ROLLBACK_AFTER_S)).isoformat(timespec="seconds"), "ticket": ""}
    log.append(entry)
    P.write_json(log_path(root), log)
    return entry, None


def attach_ticket(root: Path, entry_id: str, ticket_id: str) -> None:
    log = read_log(root)
    for e in log:
        if e.get("id") == entry_id:
            e["ticket"] = ticket_id
    P.write_json(log_path(root), log)


def quote_text(e: dict) -> str:
    """«Цитата»; слово владельца было ответом — и вопрос CEO, на который он ответил."""
    return f"«{e['quote'] or '—'}»" + (f" (ответ на вопрос CEO: «{e['question']}»)" if e.get("question") else "")


def judge_ticket_text(e: dict) -> str:
    old = "не было" if e["old"] is ABSENT else json.dumps(e["old"], ensure_ascii=False)
    return (f"Проверка правки настроек {e['id']} (CEO, {e['at']}): {e['key']}: {old} → "
            f"{json.dumps(e['new'], ensure_ascii=False)}. Зачем (CEO): {e['why']}. "
            f"Слово владельца {e['ref'] or '(не названо)'}: {quote_text(e)}. Сверь суть правки со словом владельца: "
            f"правка не добавляет того, чего в цитате нет. Принять — `tickets.py config accept {e['id']}`; не принять — "
            f"ничего не делать: к {e['deadline']} значение вернётся само.")


def accept(root: Path, entry_id: str):
    """→ (запись, None) или (None, причина)."""
    log = read_log(root)
    for e in log:
        if e.get("id") == entry_id:
            if e.get("state") != "pending":
                return None, f"{entry_id}: уже {e.get('state')}"
            e["state"] = "accepted"
            P.write_json(log_path(root), log)
            return e, None
    return None, f"нет правки {entry_id}"


def rollback_due(root: Path, now=None) -> list:
    """Откатить непринятые правки, чей срок вышел. Значение по ключу уже другое (правили ещё раз) — только отметка."""
    now = now or datetime.now().astimezone()
    log, done = read_log(root), []
    for e in log:
        if e.get("state") != "pending":
            continue
        try:
            late = P.parse_time(e["deadline"], strict=True) <= now
        except (KeyError, ValueError):
            continue
        if not late:
            continue
        cur, err = _read_rpv(root)
        if err:
            continue                                 # файл сломан — не затираем, повторим на следующем тике
        if cur.get(e["key"], ABSENT) == e["new"]:
            if e["old"] is ABSENT:
                cur.pop(e["key"], None)
            else:
                cur[e["key"]] = e["old"]
            P.write_json(rpv_path(root), cur)
            e["state"] = "rolled_back"
        else:
            e["state"] = "superseded"
        e["closed_at"] = now.isoformat(timespec="seconds")
        done.append(e)
    if done:
        P.write_json(log_path(root), log)
    return done


def report_lines(root: Path, since, until) -> list:
    """Строки раздела отчёта «CEO изменил работу команды» за период (since, until]; правок нет — пусто (раздела нет)."""
    lines = []
    for e in read_log(root):
        try:
            at = P.parse_time(e["at"], strict=True)
        except (KeyError, ValueError):
            continue
        if not (since < at <= until):
            continue
        old = "не было" if e.get("old") is ABSENT else json.dumps(e["old"], ensure_ascii=False)
        state = {"accepted": "принято Судьёй", "rolled_back": "откатилось: Судья не принял",
                 "pending": "ждёт Судью", "superseded": "заменено новой правкой"}.get(e.get("state"), str(e.get("state")))
        ref = f" {e['ref']}: {quote_text(e)}" if e.get("ref") else " слово владельца не названо"
        lines.append(f"• {at:%d.%m %H:%M} {e['key']}: {old} → {json.dumps(e['new'], ensure_ascii=False)} "
                     f"({e['why']};{ref}) — {state}")
    return lines
