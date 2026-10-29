"""Ярлыки доказательства в договоре и их перезапуск при приёмке Судьи (TK-050, В-14).

Пункт договора несёт ярлык — на чём проверен: `[чтение кода]` · `[тест: <команда>]` · `[запуск: <команда>]` · `[вживую]`
(последний разбирает `tickets._live_ack`). Команда — в тех же рамках, что у проверок ошибок (`errors.check_argv`: python|bash
скрипт из git или `-m pytest|unittest <тест из git>`); в ярлыке — до `]` или в обратных кавычках: `[тест: `python -m pytest a.py -k "x[1]"`]`.
Приёмка перезапускает команды сама, в копии тикета `.claude/worktrees/<ID>` (нет копии — в корне проекта): код выхода не 0 —
отказ; ярлык «тест» или «запуск» без команды — тоже отказ. Прогон идёт при каждой приёмке заново, поэтому последний прогон — всегда
тот, что решает: более свежий провал отменяет прежний успех. Договор — последняя запись исполнителя, где есть ярлык доказательства;
договор без ярлыков (старый) приёмку не меняет. Команда — только на тесты самой правки: полный прогон проекта (pytest без пути, по
корню или по всему `.claude`) приёмка отказывает (TK-056, В-21) — его подтверждает зелёный CI PR, а срок одной проверки короче. Только stdlib.
"""
from __future__ import annotations

import posixpath
import re
import shlex
from pathlib import Path

import errors as errors_mod
import ticket as T

LABEL = re.compile(r"\[(тест|запуск|чтение(?:\s+кода)?)(?:\s*:\s*(?:`([^`\n]*)`|([^\]\n]*)))?\s*\]", re.I)
NEED_COMMAND = ("тест", "запуск")
HINT = ("пункт договора с ярлыком тест/запуск несёт команду проверки: `[тест: python -m pytest <файл>]`, "
        "`[запуск: python tools/<скрипт>.py]` (допустимые команды — как у `errors.py fix`); без неё — ярлык `[чтение кода]`")
FULL_RUN_HINT = ("полный прогон в договор не пишется — его подтверждает зелёный CI PR; укажи тесты самой правки: "
                 "`[тест: python -m pytest .claude/dispatcher/test_<правка>.py]`")
WHOLE_PROJECT = (".", ".claude")   # цели pytest, которые охватывают все тесты проекта


def full_run(cmd: str) -> bool:
    """Команда — `python -m pytest` без пути, по корню проекта или по всему `.claude` (полный прогон, не тесты правки)."""
    try:
        argv = shlex.split(cmd)
    except ValueError:
        return False
    if len(argv) < 3 or argv[0] not in ("python", "python3") or argv[1:3] != ["-m", "pytest"]:
        return False
    targets, _ = errors_mod.command_targets(argv, "pytest")
    return not targets or any(posixpath.normpath(t.split("::")[0]) in WHOLE_PROJECT for t in targets)


def contract_labels(tkt) -> list[tuple[str, str]]:
    """Ярлыки последней записи исполнителя, где они есть: [(тип, команда или "")]. Записи `[итог: …]` — не договор."""
    for e in reversed(tkt.log):
        text = e.text.strip()
        if not T.author_is(e.author, tkt.owner or "") or re.match(r"\[итог:", text):
            continue
        found = [(m.group(1).lower().split()[0], " ".join((m.group(2) if m.group(2) is not None else m.group(3) or "").split()))
                 for m in LABEL.finditer(text)]
        if found:
            return found
    return []


def proof_ack(tid: str, tkt, root: Path) -> tuple[str, str]:
    """Приёмка: перезапуск команд договора. → (отказ, пометка для лога); договор без ярлыков — ("", "")."""
    labels = contract_labels(tkt)
    if not labels:
        return "", ""
    bare = [k for k, cmd in labels if k in NEED_COMMAND and not cmd]
    if bare:
        return f"в договоре ярлык [{bare[0]}] без команды проверки: {HINT}", ""
    wt = root / ".claude" / "worktrees" / tid
    cwd = wt if wt.is_dir() else root
    cmds = list(dict.fromkeys(cmd for k, cmd in labels if cmd))
    for cmd in cmds:
        if full_run(cmd):
            return f"команда договора `{cmd}` — полный прогон проекта: {FULL_RUN_HINT}", ""
        argv, why = errors_mod.check_argv(cmd, cwd)
        if argv is None:
            return f"команда договора `{cmd}` не допущена: {why}", ""
        ok, note = errors_mod.run_check(argv, cwd, errors_mod.CHECK_TIMEOUT_S)
        if ok is None:
            return (f"команда договора `{cmd}` не запустилась (прогон в {cwd}): {note}\nНе проверено, не вина работы: "
                    "поставь `wait <ID> at:<время ISO, +10–15 мин>` и повтори приёмку; запись в тикете не сделана"), ""
        if not ok:
            return (f"команда договора `{cmd}` упала (прогон в {cwd}): {note.replace(chr(10), chr(10) + '    ')}\n"
                    "Принять нельзя: верни работу автору (`result <ID> return`); запись в тикете не сделана"), ""
    return "", f" проверки договора: {len(cmds)} кмд, код 0" if cmds else ""
