#!/usr/bin/env python3
"""Реестр владельца: решения (В-N) и цели (G-NNN). Пишет только CEO — сам или помощником `secretary`, словами
владельца дословно; запуски ролей (`RPV_ROLE`) только читают: команда им отказывает, страж закрывает файлы. Только stdlib.

    registry.py [--project <путь>] decide --quote "<дословно>" --gist "<одна строка>" [--question "<вопрос CEO>"] [--replaces В-N] [--at <время>]
    registry.py decide --quote - --gist "..." <<'EOF'      # цитата с кавычками и переносами — из stdin как есть
    <слова владельца>
    EOF
    registry.py goal --title "..." --quote "<дословно>" [--criterion "..."]... [--budget "..."] [--due "..."] [--at <время>]
    registry.py accept-goal G-NNN [--at <время>]     # владелец принял результат цели: отметка времени принятия
    registry.py drop-goal G-NNN --quote "<дословно>"  # владелец снял цель
    registry.py repeat В-N --quote -                 # владелец повторил уже принятое — отметка в блоке, нового В-N нет
    registry.py show В-N|G-NNN
    registry.py list [--open]

Слово владельца короче 15 знаков («да», «1 - да») — ответ на вопрос CEO: без `--question` (вопрос дословно) решение не пишется,
суть короткого ответа не проверить. Из сессии CEO `decide` пускает только помощник `secretary` (страж, TK-007).

Файлы: `<проект>/.claude/decisions.md` (блок на решение, новое — в конец) и `<проект>/.claude/goals/G-NNN.md` (шапка
`id/status/set/accepted` + цитата, критерии, бюджет). Номер ставит команда: следующий после наибольшего в реестре.
Время — `ГГГГ-ММ-ДД ЧЧ:ММ` (по умолчанию сейчас, местное). Ссылка тикета `--ref` — номер отсюда или цитата владельца.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import project as P  # noqa: E402

DISPATCH_ROLES = P.ROLES
DECISION = re.compile(r"^## В-(\d+) · (.+)$", re.M)
GOAL_ID = re.compile(r"^G-(\d{3,})$")
REF = re.compile(r"^(?:В-\d+|G-\d{3,})$")
NOT_NAMED = "не названо — спросить владельца"
SHORT_QUOTE = 15       # слово владельца короче — ответ на вопрос CEO: без вопроса суть не проверить
QUESTION = "Вопрос CEO: "


def decisions_file(root: Path) -> Path:
    return root / ".claude" / "decisions.md"


def goals_dir(root: Path) -> Path:
    return root / ".claude" / "goals"


def _now(at: str | None) -> str:
    if at:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", at.strip()):
            raise ValueError(f"--at {at!r}: нужно ГГГГ-ММ-ДД ЧЧ:ММ")
        return at.strip()
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def _quote_block(quote: str) -> list[str]:
    lines = (quote or "").strip("\n").splitlines()
    return ["> " + ln if ln.strip() else ">" for ln in lines]


def decisions(root: Path) -> dict[str, str]:
    """{"В-N": блок текста} в порядке файла."""
    f = decisions_file(root)
    text = f.read_text(encoding="utf-8") if f.exists() else ""
    marks = list(DECISION.finditer(text))
    return {f"В-{m.group(1)}": text[m.start():(marks[i + 1].start() if i + 1 < len(marks) else len(text))].rstrip()
            for i, m in enumerate(marks)}


def goal_header(path: Path) -> dict:
    m = re.match(r"^---\n(.*?)\n---\n", path.read_text(encoding="utf-8"), re.S)
    out = {}
    for ln in (m.group(1).splitlines() if m else []):
        k, _, v = ln.partition(":")
        out[k.strip()] = v.strip()
    return out


def goals(root: Path) -> dict[str, Path]:
    d = goals_dir(root)
    return {p.stem: p for p in sorted(d.glob("G-*.md")) if GOAL_ID.match(p.stem)} if d.is_dir() else {}


def ref_exists(root: Path, ref: str) -> bool:
    """`--ref` тикета — номер реестра (В-N, G-NNN), который есть; открытая цель или любое решение."""
    ref = (ref or "").strip()
    if ref.startswith("В-"):
        return ref in decisions(root)
    if ref.startswith("G-"):
        p = goals(root).get(ref)
        return bool(p) and goal_header(p).get("status") == "open"
    return False


def _refuse_role() -> str:
    role = P.caller_role()  # RPV_ROLE, а стёрта — по дереву процессов: запуск роли CEO не притворится
    return (f"реестр владельца пишет только CEO; запуск роли {role} — только читает (`show`, `list`). Нужна запись — "
            f"`tickets.py result <ID> ask-owner`") if role in DISPATCH_ROLES else ""


def _read_quote(a) -> None:
    """`--quote -` — цитата из stdin как есть (кавычки и переносы владельца в аргументе оболочки ломаются). Читаем байты и
    декодируем как UTF-8 (BOM снимаем): текстовый stdin на Windows берёт кодировку локали (cp1251, cp866) и портит кириллицу.
    Не UTF-8 — ошибка, а не молчаливая порча цитаты (реестр хранит слова владельца дословно)."""
    if getattr(a, "quote", None) != "-":
        return
    raw = getattr(sys.stdin, "buffer", None)
    if raw is None:  # stdin подменён текстовым потоком без байтов (тест, встраивание)
        a.quote = sys.stdin.read().strip("\n")
        return
    try:
        a.quote = raw.read().decode("utf-8-sig").strip("\n")
    except UnicodeDecodeError as e:
        raise ValueError("цитата из stdin не в UTF-8 — запись не сделана (кириллица порвалась бы). Передайте stdin в UTF-8: "
                         "в bash/Git Bash — как есть, в PowerShell — `[Console]::InputEncoding = [Text.UTF8Encoding]::new($false)`; "
                         "или короткую цитату — аргументом `--quote \"…\"`") from e


def _need_quote(quote: str) -> str:
    return "" if len(" ".join((quote or "").split())) >= 2 else "нужна --quote — слова владельца дословно"


def _need_question(quote: str, question: str | None) -> str:
    short = len(" ".join((quote or "").split())) < SHORT_QUOTE
    return (f"слово владельца короче {SHORT_QUOTE} знаков — это ответ на вопрос CEO; без вопроса суть не проверить. "
            "Добавь --question \"<вопрос CEO владельцу дословно>\"") if short and not (question or "").strip() else ""


def question_of(block: str) -> str:
    """Вопрос CEO, на который владелец ответил (строка `Вопрос CEO: …` блока решения); нет — пусто."""
    return next((ln[len(QUESTION):].strip() for ln in block.splitlines() if ln.startswith(QUESTION)), "")


def cmd_decide(a, root: Path) -> int:
    if err := _refuse_role() or _need_quote(a.quote) or _need_question(a.quote, a.question):
        print(err, file=sys.stderr)
        return 1
    have = decisions(root)
    if a.replaces and a.replaces not in have:
        print(f"--replaces {a.replaces}: такого решения в реестре нет", file=sys.stderr)
        return 1
    n = max((int(k[2:]) for k in have), default=0) + 1
    f = decisions_file(root)
    f.parent.mkdir(parents=True, exist_ok=True)
    prev = f.read_text(encoding="utf-8") if f.exists() else ""
    question = " ".join((a.question or "").split())
    block = [f"## В-{n} · {_now(a.at)}", *_quote_block(a.quote), *([QUESTION + question] if question else []),
             f"Суть: {' '.join(a.gist.split())}", f"Заменяет: {a.replaces or '—'}"]
    lead = ("# Решения владельца\n\n" if not prev.strip() else
            "" if prev.endswith("\n\n") else "\n" if prev.endswith("\n") else "\n\n")
    with f.open("a", encoding="utf-8") as w:
        w.write(lead + "\n".join(block) + "\n")
    print(f"В-{n}")
    return 0


def cmd_goal(a, root: Path) -> int:
    if err := _refuse_role() or _need_quote(a.quote):
        print(err, file=sys.stderr)
        return 1
    if not (a.title or "").strip():
        print("нужен --title", file=sys.stderr)
        return 1
    n = max((int(k[2:]) for k in goals(root)), default=0) + 1
    gid = f"G-{n:03d}"
    crit = [c.strip() for c in (a.criterion or []) if c.strip()] or [NOT_NAMED]
    text = "\n".join([
        "---", f"id: {gid}", "status: open", f"set: {_now(a.at)}", "accepted:", "---",
        f"# {' '.join(a.title.split())}", "",
        "## Что нужно владельцу", *_quote_block(a.quote), "",
        "## Критерии приёмки", *[f"- {c}" for c in crit], "",
        "## Бюджет и срок", f"Бюджет: {(a.budget or '').strip() or 'не назван'}",
        f"Срок: {(a.due or '').strip() or 'не назван'}", ""])
    d = goals_dir(root)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{gid}.md").write_text(text, encoding="utf-8")
    print(gid)
    return 0


def _set_goal(root: Path, gid: str, updates: dict, note: str = "") -> int:
    if err := _refuse_role():
        print(err, file=sys.stderr)
        return 1
    p = goals(root).get(gid)
    if not p:
        print(f"нет цели {gid}", file=sys.stderr)
        return 1
    text = p.read_text(encoding="utf-8")
    for k, v in updates.items():
        text = re.sub(rf"^{k}:.*$", f"{k}: {v}", text, count=1, flags=re.M)
    p.write_text(text.rstrip("\n") + (f"\n\n{note}\n" if note else "\n"), encoding="utf-8")
    print(f"{gid}: {', '.join(f'{k} {v}' for k, v in updates.items())}")
    return 0


def cmd_accept_goal(a, root: Path) -> int:
    return _set_goal(root, a.id, {"status": "accepted", "accepted": _now(a.at)})


def cmd_drop_goal(a, root: Path) -> int:
    if err := _need_quote(a.quote):
        print(err, file=sys.stderr)
        return 1
    return _set_goal(root, a.id, {"status": "dropped"},
                     "## Снята владельцем\n" + "\n".join(_quote_block(a.quote)))


def cmd_show(a, root: Path) -> int:
    if a.ref.startswith("В-"):
        block = decisions(root).get(a.ref)
    else:
        p = goals(root).get(a.ref)
        block = p.read_text(encoding="utf-8").rstrip() if p else None
    if block is None:
        print(f"нет {a.ref}", file=sys.stderr)
        return 1
    print(block)
    return 0


REPEAT = "Повторено владельцем"


def repeats(root: Path) -> dict[str, int]:
    """{"В-N": сколько раз владелец повторил уже принятое} — мера критерия 6 (должно быть 0)."""
    return {k: n for k, block in decisions(root).items()
            if (n := sum(ln.startswith(REPEAT) for ln in block.splitlines()))}


def cmd_repeat(a, root: Path) -> int:
    """Владелец повторил уже принятое решение: строка «Повторено владельцем · время: «цитата»» в его блок, нового В-N нет."""
    if err := _refuse_role() or _need_quote(a.quote):
        print(err, file=sys.stderr)
        return 1
    f = decisions_file(root)
    text = f.read_text(encoding="utf-8") if f.exists() else ""
    marks = list(DECISION.finditer(text))
    i = next((i for i, m in enumerate(marks) if f"В-{m.group(1)}" == a.ref), None)
    if i is None:
        print(f"нет {a.ref} в реестре решений", file=sys.stderr)
        return 1
    end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
    block, after = text[marks[i].start():end].rstrip("\n"), text[end:]
    line = f"{REPEAT} · {_now(a.at)}: «{' '.join(a.quote.split())}»"
    f.write_text(text[:marks[i].start()] + block + "\n" + line + "\n" + ("\n" + after.lstrip("\n") if after.strip() else ""),
                 encoding="utf-8")
    print(f"{a.ref}: повтор №{repeats(root).get(a.ref, 0)}")
    return 0


def cmd_list(a, root: Path) -> int:
    rep = repeats(root)
    for k, block in decisions(root).items():
        gist = next((ln[6:] for ln in block.splitlines() if ln.startswith("Суть: ")), "")
        if not a.open:
            print(f"{k} — {gist}" + (f" (повторено владельцем: {rep[k]})" if k in rep else ""))
    for gid, p in goals(root).items():
        h = goal_header(p)
        if a.open and h.get("status") != "open":
            continue
        title = next((ln[2:] for ln in p.read_text(encoding="utf-8").splitlines() if ln.startswith("# ")), "")
        print(f"{gid} [{h.get('status', '?')}] {title}")
    return 0


def main(argv=None) -> int:
    P.utf8_stdio()
    ap = argparse.ArgumentParser(prog="registry.py", description=__doc__.split("\n\n")[0])
    ap.add_argument("--project", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("decide", help="решение владельца → В-N в конец .claude/decisions.md")
    d.add_argument("--quote", required=True, help="слова владельца дословно; «-» — из stdin")
    d.add_argument("--gist", required=True, help="суть одной строкой")
    d.add_argument("--question", default=None, help=f"вопрос CEO дословно, если слово владельца — ответ на него (обязателен при цитате короче {SHORT_QUOTE} знаков)")
    d.add_argument("--replaces", default=None, help="В-N, которое это решение меняет или отменяет")
    d.add_argument("--at", default=None, help="ГГГГ-ММ-ДД ЧЧ:ММ (по умолчанию сейчас)")
    d.set_defaults(func=cmd_decide)
    g = sub.add_parser("goal", help="цель владельца → .claude/goals/G-NNN.md")
    g.add_argument("--title", required=True)
    g.add_argument("--quote", required=True, help="слова владельца дословно; «-» — из stdin")
    g.add_argument("--criterion", action="append", help="критерий приёмки, названный владельцем (повторяемый)")
    g.add_argument("--budget", default=None, help="бюджет словами владельца")
    g.add_argument("--due", default=None, help="срок словами владельца")
    g.add_argument("--at", default=None, help="ГГГГ-ММ-ДД ЧЧ:ММ (по умолчанию сейчас)")
    g.set_defaults(func=cmd_goal)
    ac = sub.add_parser("accept-goal", help="владелец принял результат цели")
    ac.add_argument("id")
    ac.add_argument("--at", default=None)
    ac.set_defaults(func=cmd_accept_goal)
    dr = sub.add_parser("drop-goal", help="владелец снял цель")
    dr.add_argument("id")
    dr.add_argument("--quote", required=True)
    dr.set_defaults(func=cmd_drop_goal)
    s = sub.add_parser("show")
    s.add_argument("ref")
    s.set_defaults(func=cmd_show)
    rp = sub.add_parser("repeat", help="владелец повторил уже принятое решение (мера: должно быть 0)")
    rp.add_argument("ref", help="В-N")
    rp.add_argument("--quote", required=True, help="слова владельца дословно; «-» — из stdin")
    rp.add_argument("--at", default=None)
    rp.set_defaults(func=cmd_repeat)
    li = sub.add_parser("list")
    li.add_argument("--open", action="store_true", help="только открытые цели")
    li.set_defaults(func=cmd_list)
    a = ap.parse_args(argv)
    try:
        root = Path(a.project).expanduser().resolve() if a.project else P.resolve_project()
    except P.ProjectNotFound:
        print(f"[registry] ошибка: {P.NOT_FOUND_HINT}", file=sys.stderr)
        return 2
    try:
        _read_quote(a)
        return a.func(a, root)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
