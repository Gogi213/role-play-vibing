"""Ход роли вместо строки CEO (TK-127, В-63): «стоит молча» диспетчер сам отдаёт роли, которая может действовать, — CEO не будят.

Пять видов: `next-ceo` и `ждёт-ceo` (роль передала ход CEO) → Судья решает за CEO (владельцу — только деньги, направление,
необратимое, бизнес-правило через `--why-owner`, В-10); `нет-хода` → владелец тикета; `цикл-ожиданий` → Судья решает, кто идёт
первым; `хвост` (принято с `--tail ceo:`) → Судья заводит тикетом (В-35). Передача от самого Судьи остаётся за CEO: передавать
некому. Запись `dispatcher` «Ход тебе (вид): почему» в логе тикета + `next: <роль>`; строка в очереди CEO — «к сведению».
Предел — MAX_PER_KIND записей на вид в одном тикете (считает лог: работает и из команды accept, и из тика), дальше — как раньше.
Выключатель — ключ `role_turns: false` в `.claude/rpv.json`."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import project as P  # noqa: E402
import ticket as T  # noqa: E402

MARK = "Ход тебе"
MAX_PER_KIND = 3
JUDGE_KINDS = {  # вид → что Судье делать (владельца-роль у `нет-хода` берём из тикета)
    "next-ceo": "решай за CEO сам; владельцу — только `result ask-owner --why-owner деньги|направление|необратимое|бизнес-правило`",
    "ждёт-ceo": "CEO не ответил — реши за него сам; владельцу — только `result ask-owner --why-owner деньги|направление|необратимое|бизнес-правило`",
    "цикл-ожиданий": "каждый тикет цикла ждёт следующего — реши, кто идёт первым, и сними `wait_for` с остальных",
    "хвост": "заведи тикет роли, которая это может (`tickets.py new --owner <роль> --ref <ref тикета> --title \"Хвост …\"`); "
             "владельцу — только `result ask-owner --why-owner деньги|направление|необратимое|бизнес-правило`",
}
KINDS = (*JUDGE_KINDS, "нет-хода")


def enabled() -> bool:
    return P.setting("role_turns", True) is not False


def _passed_by(tkt: T.Ticket) -> str:
    """Автор последней записи не от dispatcher: кто передал ход."""
    for e in reversed(tkt.log):
        if not T.author_is(e.author, "dispatcher"):
            return e.author
    return ""


def turns_given(tkt: T.Ticket, kind: str) -> int:
    key = f"{MARK} ({kind}):"
    return sum(1 for e in tkt.log if T.author_is(e.author, "dispatcher") and e.text.lstrip().startswith(key))


def target(kind: str, tkt: T.Ticket) -> str:
    """Роль, которой отдаём ход по этому виду; пусто — отдавать некому или нельзя, действует прежний путь (строка CEO)."""
    if not enabled() or kind not in KINDS:
        return ""
    role = tkt.owner if kind == "нет-хода" else "judge"
    if role not in P.ROLES:
        return ""
    if kind in ("next-ceo", "ждёт-ceo") and T.author_is(_passed_by(tkt), role):
        return ""
    if kind != "нет-хода" and turns_given(tkt, kind) >= MAX_PER_KIND:
        return ""
    return role


def note(kind: str, why: str) -> str:
    return f"{MARK} ({kind}): {why}" + (f" Что делать: {JUDGE_KINDS[kind]}." if kind in JUDGE_KINDS else "")


def give_turn(path: Path, tkt: T.Ticket, kind: str, why: str, now=None) -> str:
    """Ход роли: запись dispatcher «почему ход тебе» и `next`. Возвращает роль; пусто — ход не отдан."""
    role = target(kind, tkt)
    if not role:
        return ""
    T.append_log(path, "dispatcher", note(kind, why), now=now)
    T.write_header_updates(path, {"next": role}, now=now)
    return role
