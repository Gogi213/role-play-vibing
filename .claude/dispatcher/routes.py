"""Маршруты итога шага (TK-079 п.0, В-195): роль сдаёт шаг командой `tickets.py result <ID> <итог> --why …`, а кого будить
дальше решает эта таблица, не текст записи. Итогов восемь: done, pr, accept, return, blocked, ask-owner, wait, continue."""
from __future__ import annotations

import re
from pathlib import Path

RESULTS = ("done", "pr", "accept", "return", "blocked", "ask-owner", "wait", "continue")
WHY_MAX = 200
_SHA = re.compile(r"[0-9a-f]{7,40}")

# итог → (кто вправе, обязательное доказательство)
RULES = {
    "done": ({"researcher", "engineer", "ceo"}, "path"),  # ceo — сессия без роли: закрыть тикет (TK-090 б)
    "pr": ({"engineer", "researcher"}, "pr+sha"),
    "accept": ({"judge"}, "pr+sha|path"),
    "return": ({"judge"}, "sha|path"),
    "blocked": ({"researcher", "engineer", "judge"}, ""),
    "ask-owner": ({"researcher", "engineer", "judge"}, ""),
    "wait": ({"researcher", "engineer", "judge"}, "form"),
    "continue": ({"researcher", "engineer"}, ""),  # TK-090 в: длинная работа кусками — следующий запуск той же роли
}
CONTINUE_MAX = 5  # подряд идущих continue у одной роли в тикете; дальше — wait/blocked/pr/done
HINT = {
    "path": "--path <файл или каталог с результатом, существует на диске>",
    "pr+sha": "--pr <номер> --sha <голова PR, 7–40 hex>",
    "sha": "--sha <проверенная голова, 7–40 hex>",
    "pr+sha|path": "--pr <номер> --sha <голова PR, 7–40 hex> (работа в PR) либо --path <артефакт проверки> (без PR)",
    "sha|path": "--sha <проверенная голова, 7–40 hex> (работа в PR) либо --path <артефакт проверки> (без PR)",
    "form": "--form <форма wait_for>",
}


def check(role: str, result: str, why: str, pr=None, sha: str = "", path: str = "", form: str = "",
          root: Path = Path(".")) -> str:
    """Пустая строка — команда годна; иначе — что исправить (текст отказа в сессии роли)."""
    if result not in RESULTS:
        return f"неизвестный итог {result!r}; допустимо: {', '.join(RESULTS)}"
    who, need = RULES[result]
    if not role and result == "done":
        role = "ceo"
    if role not in who:
        return f"итог {result} — не для роли {role or '(не роль)'}; вправе: {', '.join(sorted(who))}"
    why = (why or "").strip()
    if not why:
        return "нужен --why «зачем/почему» (≤ 200 знаков)"
    if len(why) > WHY_MAX:
        return f"--why длиннее {WHY_MAX} знаков ({len(why)}) — сократи"

    def _exists(p: str) -> bool:
        if not (p or "").strip():
            return False
        try:  # TK-109 п.3: не '.', '/', каталог проекта и не путь вне проекта
            base = root.resolve()
            f = (Path(p) if Path(p).is_absolute() else root / p).resolve()
            return f != base and f.exists() and f.is_relative_to(base)
        except (OSError, ValueError):
            return False

    if "|" in need:  # Судья: работа в PR (голова) либо без PR (артефакт проверки)
        pr_form = need.partition("|")[0]
        if pr or sha:
            need = pr_form
        elif _exists(path):
            return ""
        else:
            return f"итог {result} требует {HINT[need]}"
    if "pr" in need and not (isinstance(pr, int) and pr > 0):
        return f"итог {result} требует {HINT[need]}"
    if "sha" in need and not _SHA.fullmatch((sha or "").lower()):
        return f"итог {result} требует {HINT[need]}"
    if need == "path" and not _exists(path):
        return f"итог done требует {HINT['path']}"
    if need == "form" and not (form or "").strip():
        return f"итог wait требует {HINT['form']}"
    if need == "form":
        import ticket as T
        if T.parse_wait_for(form.strip()) is None:
            return f"wait: форма не понята. Допустимо: {T.WAIT_FOR_FORMATS}"
    return ""


def T_author_is(author: str, role: str) -> bool:
    import ticket as T
    return T.author_is(author, role)


def last_owner_result(tkt) -> str:
    """Последний итог владельца тикета из лога («[итог: X]…»); нет — пусто."""
    for e in reversed(tkt.log):
        m = re.match(r"\[итог: ([\w-]+)\]", e.text.strip())
        if m and T_author_is(e.author, tkt.owner or ""):
            return m.group(1)
    return ""


def continue_streak(tkt, role: str) -> int:
    """Сколько итогов `continue` этой роли подряд в конце лога (запись другого итога обрывает серию)."""
    n = 0
    for e in reversed(tkt.log):
        m = re.match(r"\[итог: ([\w-]+)\]", e.text.strip())
        if not m or not T_author_is(e.author, role):
            continue
        if m.group(1) != "continue":
            break
        n += 1
    return n


def route(role: str, result: str, owner: str, reviewer: str = "", owner_last: str = "") -> dict:
    """Правки шапки по итогу. accept и wait правит своя команда (accepted / wait_for), здесь — только ход."""
    rev = (reviewer or "").strip()
    if result == "continue":
        return {"status": "in_progress", "next": role}
    if result == "done":
        if role == "ceo":  # ceo закрывает сам
            return {"status": "done", "next": ""}
        if rev and rev != role and role != "judge":
            return {"status": "in_review", "next": rev}
        return {"status": "done", "next": ""}
    if result == "pr":
        return {"status": "in_review", "next": rev or "judge"}
    if result == "accept":  # без PR; с PR — cmd_accept. Закрываем, только если владелец сдавал done; иначе проверка
        if owner_last == "done":  # шага — владелец продолжает
            return {"status": "done", "next": ""}
        return {"status": "in_progress", "next": owner}
    if result == "return":
        return {"status": "in_progress", "next": owner}
    if result == "blocked":  # TK-094: задача встала → Судье (методика, развилки, гейты); Судья сам встал → владельцу
        return {"status": "blocked", "next": "" if role == "judge" else "judge"}
    if result == "ask-owner":  # TK-094: только о содержании исследования; строка «ждёт вас», CEO не будится
        return {"status": "needs_owner", "next": ""}
    return {}  # wait: статус waiting + wait_for ставит cmd_wait
