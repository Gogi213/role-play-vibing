"""CLI для тикетов диспетчера: `new`, `comment`, `start`, `wait`, `status`. Только stdlib.

Запускается из папки плагина; проект — `--project <путь>` (перед подкомандой), иначе `RPV_PROJECT` /
`CLAUDE_PROJECT_DIR`, иначе ближайший каталог вверх от текущего с `.claude/roles`; не нашли — ошибка с подсказкой,
каталоги не создаются. Тикеты — `<проект>/.claude/tickets/`. Ниже `tickets.py` — это
`python <плагин>/.claude/dispatcher/tickets.py [--project <проект>]`.

    tickets.py new --owner researcher --title "..." [--desc "..."]  # ревьюер — Судья по умолчанию (проверяет каждый done)
    tickets.py new --owner engineer --title "..." --effort medium   # low|medium|high|xhigh
    tickets.py new --owner researcher --title "..." --backlog   # перенос из TASKS.md
    tickets.py new --owner engineer --title "..." --executor haiku --kind file-move
        # белый список kind; ревьюера у Haiku-задачи нет, --reviewer judge и owner:researcher с haiku — отказ
    tickets.py result TK-001 done --path <файл> --why "..."      # итог роли: done|pr|accept|return|blocked|ask-owner|wait|continue; следующую роль ставит маршрут
    tickets.py comment TK-001 --author researcher --text "..."  # промежуточная заметка без смены хода
    tickets.py accept TK-001 --pr 7 --sha <голова>              # только Судья: принято на этой голове → вливает merge_rule
    tickets.py start TK-001                                     # backlog|stopped → todo
    tickets.py wait TK-001 host:gpu1:<путь>/<job>.json [--on-met "python tools/x.py арг"]  # status: waiting + wait_for
    tickets.py stop TK-001 --text "..." [--next engineer]       # только CEO: снять роль
    tickets.py due TK-001 --by 18:00 [--said "к шести"]          # только CEO: срок, названный владельцу (due / due_first)
    tickets.py allow TK-001 publish --ref В-12                  # только CEO: согласие владельца на публикацию по тикету
    tickets.py allow TK-001 --revoke                            # только CEO: снять согласие
    tickets.py status                                           # потрачено по задачам

Приёмка Судьи (`result … accept`, `accept`) сначала гоняет проверки всех исправленных ошибок (`errors.py`; в worktree
тикета `.claude/worktrees/<ID>`, если он есть, иначе в корне проекта): упала любая — регрессия, отказ, в тикет ничего не пишется.

`--next researcher|engineer|judge|ceo` — единственный способ разбудить другую роль (или CEO) записью лога:
пишет `next: <роль>` в шапку, диспетчер запускает роль ОДИН раз и очищает поле. @упоминания в тексте никого
не будят. После записи лог больше 20 КБ ужимается: всё, кроме последних 8 записей, — в `archive/<ID>-log.md`.

`stop <ID> --text "<новая постановка>" [--next <роль>]` — только CEO (запись в лог — от `ceo`; из сессии роли — отказ): заявка
диспетчеру снять запущенную роль всем деревом процессов и дать новую постановку (подробно — README диспетчера, «Остановка
роли»). С `--next` тикет станет `todo` + `next: <роль>` и роль стартует сразу; без — `status: stopped`: диспетчер не будит,
пока CEO не переведёт в `todo` (`start`).

`allow <ID> publish --ref В-N` — только CEO (из сессии роли — отказ): пишет в шапку `allow: publish (В-N)` и запись `ceo` в лог.
`--ref` — решение владельца, уже занесённое в реестр (`registry.py decide`): слово владельца сначала в реестр, потом согласие.
Пока поля нет, страж (`delete_guard.py`) закрывает запуску роли по этому тикету публикации: выпуск и слияние PR (`gh release`,
`gh pr merge`), `git push` в main/master и теги, публикацию пакетов и образов, запись в GitHub API на слияние и выпуск
(`gh api`, `curl`/`wget`/`Invoke-WebRequest` с записью на `api.github.com`). `--revoke` снимает согласие.

Лимитов денег на тикет нет (В-173, 03.10): `status` показывает только потрачено (учёт из state.json).
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dispatch as D  # noqa: E402
import errors as errors_mod  # noqa: E402
import registry as registry_mod  # noqa: E402
import routes as routes_mod  # noqa: E402
import ticket as T  # noqa: E402

# Проект и тикеты берутся у диспетчера (D.configure_project при импорте — по --project/RPV_PROJECT/CLAUDE_PROJECT_DIR/поиску вверх);
# в main() флаг --project переключает их ещё раз. Тесты подменяют TICKETS_DIR прямо на модуле.
TICKETS_DIR = D.TICKETS_DIR
REF_MIN = 5  # знаков в цитате --ref; решение «В-<число>» (REF_DECISION) проходит любой длины, «да»/пусто — нет
REF_DECISION = re.compile(r"^В-\d+")
REF_REGISTRY = re.compile(r"^(В-\d+|G-\d{3,})\b")  # номер реестра владельца: есть реестр — номер обязан в нём быть
PROJECT_ROOT = D.PROJECT_ROOT


def cmd_inbox(args) -> int:
    """Очередь CEO: новые строки ceo-inbox.md с прошлого чтения (отметка `.ceo-inbox-fallback-seen` — имя осталось от шины);
    `--peek` отметку не двигает."""
    inbox, seen_f = D.CEO_INBOX, D.CEO_INBOX.parent / ".ceo-inbox-fallback-seen"
    try:
        lines = inbox.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    try:
        seen = int(seen_f.read_text(encoding="utf-8").strip() or 0)
    except (OSError, ValueError):
        seen = 0
    if seen > len(lines):
        seen = 0
    for ln in lines[seen:]:
        print(ln)
    if len(lines) == seen:
        print("очередь CEO пуста (новых строк в ceo-inbox.md нет)")
    elif not args.peek:
        seen_f.write_text(str(len(lines)), encoding="utf-8")
    return 0


def cmd_new(args) -> int:
    # TK-233 п.2: незаказанной работе — отказ. Тикет заводится только со ссылкой на задачу владельца (цитата или В-##)
    ref = " ".join((getattr(args, "ref", None) or "").split())
    if len(ref) < REF_MIN and not REF_DECISION.match(ref):
        print(f"new: нужна --ref «ссылка на задачу владельца»: его цитата (≥ {REF_MIN} знаков) или решение В-##. "
              f"Работы, которую владелец не заказывал, не заводим", file=sys.stderr)
        return 1
    # CP2 (критерий 1): реестр владельца есть — ссылка только на его номер, цель G-NNN или решение В-N; слова владельца
    # сначала записывает `secretary`. Без реестра (`.claude/decisions.md` нет) — цитата или В-## как раньше
    if registry_mod.decisions_file(PROJECT_ROOT).exists() and not REF_REGISTRY.match(ref):
        print("new: --ref — только номер реестра владельца: цель G-NNN или решение В-N (`registry.py list`). Слова "
              "владельца сначала в реестр — помощник `secretary` (`registry.py decide|goal`)", file=sys.stderr)
        return 1
    # V2-2: номер реестра владельца (В-N, G-NNN) — только существующий; цель — только открытая. Решения без
    # реестра (.claude/decisions.md нет — номера ведутся в другом месте проекта) проходят как раньше
    num = REF_REGISTRY.match(ref)
    if num and (num.group(1).startswith("G-") or registry_mod.decisions_file(PROJECT_ROOT).exists()) \
            and not registry_mod.ref_exists(PROJECT_ROOT, num.group(1)):
        print(f"new: --ref {num.group(1)} — такого {'открытой цели' if num.group(1).startswith('G-') else 'решения'} "
              f"в реестре владельца нет (`registry.py list`). Сначала запись словами владельца — `registry.py "
              f"decide|goal`", file=sys.stderr)
        return 1
    # V2-1: Судья проверяет каждый результат — ревьюер по умолчанию judge. Без него: механическая задача Haiku
    # (приёмка кодом) и тикет самого Судьи (себя он не проверяет). Снять Судью с обычной задачи может только владелец
    if getattr(args, "no_reviewer", False) and args.executor != "haiku":
        print("new: --no-reviewer не принимается — Судья проверяет каждый результат, снять его может только владелец. "
              "Механическая задача без Судьи — `--executor haiku --kind …` (приёмка кодом)", file=sys.stderr)
        return 1
    reviewer = args.reviewer or ("" if args.owner == "judge" else routes_mod.default_reviewer(args.executor))

    if args.executor == "haiku":
        # судья TK-002 п.5г: обход проверки Судьи запрещён — отказ до создания файла, не постфактум
        if args.kind not in D.HAIKU_ALLOWED_KINDS:
            print(f"--executor haiku требует --kind из {sorted(D.HAIKU_ALLOWED_KINDS)}", file=sys.stderr)
            return 1
        if reviewer == "judge":
            print("--executor haiku нельзя вместе с --reviewer judge — числа/вердикты не на Haiku",
                  file=sys.stderr)
            return 1
        if args.owner == "researcher":
            print("--executor haiku нельзя для owner: researcher — исследовательский результат не на Haiku",
                  file=sys.stderr)
            return 1
    elif args.kind:
        print("--kind без --executor haiku не имеет смысла", file=sys.stderr)
        return 1

    if not getattr(args, "force", False) and (dup := T.similar_recent_ticket(TICKETS_DIR, args.title)):
        print(f"new: похожий тикет за последний час уже есть — {dup}. Дубль? Допиши в него или создай с `--force`.",
              file=sys.stderr)
        return 1
    try:
        due = T.parse_by(args.due) if getattr(args, "due", None) else None  # непонятый срок — отказ до создания файла
        path = T.create_ticket(TICKETS_DIR, owner=args.owner, title=args.title, reviewer=reviewer,
                                description=f"Ссылка на задачу владельца: {ref}\n\n{args.desc or ''}".rstrip(),
                                wait_for=args.wait_for or "",
                                status="backlog" if args.backlog else "todo",
                                executor=args.executor, kind=args.kind, effort=args.effort)
    except ValueError as e:  # неизвестная форма wait_for или срока — файл не создан
        print(e, file=sys.stderr)
        return 1
    if due:
        _set_due(path, due, getattr(args, "said", None) or "")
    try:
        print(path.relative_to(PROJECT_ROOT))
    except ValueError:
        print(path)
    return 0


def cmd_comment(args) -> int:
    path = TICKETS_DIR / f"{args.id}.md"
    if not path.exists():
        print(f"нет тикета {args.id}", file=sys.stderr)
        return 1
    nxt, auto_next = args.next, False
    with T.ticket_lock(path):  # запись, `next` и сжатие лога — одним куском (диспетчер правит шапку тем же замком)
        T.append_log(path, args.author, args.text)
        if not nxt and T.author_is(args.author, "judge"):
            # Судья вернул тикет без `--next`: пока тикет в todo/in_progress/in_review/waiting, ход возвращается
            # владельцу; done, stopped, needs_owner, blocked — не будим
            tkt = T.read_ticket(path)
            if tkt.status in ("todo", "in_progress", "in_review", "waiting") and tkt.owner in ("researcher", "engineer"):
                nxt, auto_next = tkt.owner, True
        if nxt:
            # v2: единственный будильник другой роли/CEO; `updated` не двигаем (маркеры уведомлений CEO по нему)
            T.write_header_updates(path, {"next": nxt}, stamp_updated=False)
            # №15 аудита: blocked + next: роль — запись без запуска; ход роли снимает blocked (иначе тикет висит с будильником)
            if nxt in ("researcher", "engineer", "judge") and T.read_ticket(path).status == "blocked":
                T.write_header_updates(path, {"status": "in_progress"}, stamp_updated=False)
        moved = T.compact_log(path)
    print(f"дописано в {path}" + (f"; next: {nxt}" if nxt else "")
          + (f"; next проставлен автоматически (запись Судьи без --next → владелец тикета: {nxt})" if auto_next else "")
          + (f"; в архив перенесено записей: {moved}" if moved else ""))
    return 0


def _regression(tid: str, cmd: str) -> bool:
    """V2-3: приёмка Судьи — только при зелёных проверках всех исправленных ошибок (`errors.py`). True — отказ (причина уже
    в stderr, в тикет ничего не пишем). Нет реестра ошибок или исправленных в нём — ничего не запускается."""
    err = errors_mod.gate(PROJECT_ROOT, tid)
    if err:
        print(f"{cmd}: {err}", file=sys.stderr)
    return bool(err)


def cmd_result(args) -> int:
    """Сдать шаг: итог из закрытого списка + зачем + доказательство; кого будить — таблица routes.py (TK-079 п.0).
    Неполная команда — отказ с текстом, что исправить; запись в лог тикета ставит сама команда."""
    import routes
    path = TICKETS_DIR / f"{args.id}.md"
    if not path.exists():
        print(f"нет тикета {args.id}", file=sys.stderr)
        return 1
    role = _caller_role()
    tkt = T.read_ticket(path)
    err = routes.check(role, args.result, args.why, pr=args.pr, sha=args.sha or "", path=args.path or "",
                       form=args.form or "", root=PROJECT_ROOT, owner=tkt.owner or "",
                       why_owner=getattr(args, "why_owner", None) or "")
    if err:
        print(f"result: {err}", file=sys.stderr)
        return 1
    if args.result == "continue" and routes.continue_streak(tkt, role) >= routes.CONTINUE_MAX:
        print(f"result: continue уже {routes.CONTINUE_MAX} раз подряд — сдай pr/done или wait (долгое — фоновый процесс), "
              f"либо blocked", file=sys.stderr)
        return 1
    role = role or "ceo"  # check() пускает сессию без роли только к done (закрыть тикет)
    why = args.why.strip()
    on_head = f" голова {(args.sha or '')[:7]}" if args.sha else f" проверка: {args.path}"
    proof = {"pr": f" PR #{args.pr}@{(args.sha or '')[:7]}", "accept": on_head, "return": on_head,
             "done": f" результат: {args.path}", "wait": f" ждём: {args.form}",
             "ask-owner": f" повод: {args.why_owner}" if getattr(args, "why_owner", None) else ""}.get(args.result, "")
    if role == "judge" and tkt.owner == "judge" and args.result in ("done", "accept") and not (args.pr or args.sha):
        head, _ = routes.tracked_at_head(PROJECT_ROOT, args.path)  # TK-225: формальное закрытие по артефакту в git
        proof = f" закрыт по артефакту {args.path}@{head}"
    ack = ""
    if args.result == "accept":
        err, ack = _decisions_ack(args)
        nerr, nack = _numbers_ack(args, args.id)
        if err or nerr:
            print(f"result: {err or nerr}", file=sys.stderr)
            return 1
        ack += nack
        proof += ack
    if args.result == "accept" and not (args.pr or args.sha) and _regression(args.id, "result"):
        return 1  # приёмка в PR проходит ту же проверку в cmd_accept
    if args.result == "accept" and (args.pr or args.sha):
        rc = cmd_accept(type("A", (), {"id": args.id, "pr": args.pr, "sha": args.sha, "text": why,
                                      "repo": getattr(args, "repo", None),
                                      "decisions": getattr(args, "decisions", None),
                                      "numbers": getattr(args, "numbers", None)})())
        if rc == 0:  # запись итога — иначе Stop-хук (stop_result.py) не видит сдачи шага и блокирует Судью
            with T.ticket_lock(path):
                T.append_log(path, role, f"[итог: accept] PR #{args.pr}@{args.sha[:7]}{ack} — {why}")
        return rc
    if args.result == "wait":  # сначала условие (цикл, форма), и только при успехе — запись: отказ ничего не пишет
        rc = cmd_wait(type("A", (), {"id": args.id, "spec": args.form, "on_met": None, "by": getattr(args, "by", None),
                                      "allow_met": getattr(args, "allow_met", False)})())
        if rc == 0:
            with T.ticket_lock(path):
                T.append_log(path, role, f"[итог: wait]{proof} — {why}")
        return rc
    if args.result == "done" and role == "ceo":
        proof += f" ({D.NO_REVIEW_MARK})"
    with T.ticket_lock(path):
        T.append_log(path, role, f"[итог: {args.result}]{proof} — {why}")
        if args.result == "pr":
            prs = [n for n in re.findall(r"\d+", str(tkt.header.get("pr") or "")) if n != str(args.pr)]
            T.write_header_updates(path, {"pr": ", ".join(prs + [str(args.pr)])}, stamp_updated=False)
    upd = routes.route(role, args.result, tkt.owner, tkt.header.get("reviewer", ""),
                       owner_last=routes.last_owner_result(tkt) if args.result == "accept" else "", executor=tkt.executor)
    T.write_header_updates(path, upd)
    print(f"{args.id}: итог {args.result} → status: {upd['status']}" + (f", next: {upd['next']}" if upd.get("next") else ""))
    return 0


def _caller_role() -> str:
    """Роль вызывающей сессии: `RPV_ROLE` запуска роли, а стёрта — по дереву процессов (`project.caller_role`); у CEO и
    владельца её нет."""
    return D.P.caller_role(D.PROJECT_ROOT)


def _decisions_ack(args) -> tuple[str, str]:
    """CP2 (критерий 6): приёмка Судьи при заведённом реестре — с перечнем решений владельца, с которыми сдача сверена:
    `--decisions В-3,В-7` (номера есть в реестре) или `--decisions -` (связанных нет). → (отказ, пометка для лога)."""
    if not registry_mod.decisions_file(PROJECT_ROOT).exists():
        return "", ""
    raw = (getattr(args, "decisions", None) or "").strip()
    if raw in ("-", "—"):
        return "", " решения: связанных нет"
    refs = [r for r in re.split(r"[,\s]+", raw) if r]
    if not refs:
        return ("приёмка — только со сверкой с решениями владельца: --decisions В-N[,В-M] (с чем сверено) или "
                "--decisions - (связанных нет); противоречит решению — не принимать, а вернуть"), ""
    missing = [r for r in refs if not (REF_DECISION.fullmatch(r) and registry_mod.ref_exists(PROJECT_ROOT, r))]
    if missing:
        return f"--decisions: {', '.join(missing)} — таких решений в реестре нет (`registry.py list`)", ""
    return "", f" решения: сверено {', '.join(refs)}"


NUMBER = re.compile(r"[-+−]?\d+(?:[.,]\d+)?")
NUMBER_ROW = re.compile(r"^\s*\|([^|]+)\|([^|]+)\|")
# `--numbers -` («чисел в сдаче нет») код проверяет сам: в текстовом артефакте приёмки по --path ищет числа
NUMBERS_TEXT_EXT = {".md", ".txt", ".csv", ".tsv", ".json", ".log"}
NUMBERS_SCAN_FILES = 50          # каталог-артефакт: не больше стольких текстовых файлов
NUMBERS_SCAN_BYTES = 1_000_000   # и не больше стольких байт с каждого
# что числом не считается (метки, а не величины); каждое совпадение заменяется пробелом до поиска чисел
_NOT_NUMBERS = tuple(re.compile(p, f) for p, f in (
    (r"https?://\S+", 0),                                                                   # адреса
    (r"(?<![0-9a-f])[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?![0-9a-f])", 0),   # uuid
    (r"(?<![\w./\\-])[\w./\\-]{0,200}\w\.[A-Za-z][A-Za-z0-9]{0,4}\b(?::\d+(?:[-–]\d+)?)?", 0),   # файл.расш[:строка]
    (r"(?<!\d)\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:[.,]\d+)?)?(?:Z|[+-]\d{2}(?::?\d{2})?)?)?", 0),  # ISO
    (r"\d{1,2}\s+(?:января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)"
     r"(?:\s+\d{4})?(?:\s*г\.?)?", re.I),                                                   # 14 октября 2026
    (r"(?<![\d.,])\d{1,2}\.\d{1,2}\.\d{4}(?!\d)", 0),                                        # ДД.ММ.ГГГГ
    (r"(?<![\d.,])(?:0[1-9]|[12]\d|3[01])\.(?:0[1-9]|1[0-2])(?:\.(?:\d{4}|\d{2}))?(?!\d|[.,]\d)", 0),   # ДД.ММ[.ГГ]
    (r"(?<![\w:.,])\d{1,2}:\d{2}(?::\d{2})?(?!\d)", 0),                                      # ЧЧ:ММ[:СС]
    (r"(?<![\w.])[vV]?\d+(?:\.\d+){2,}(?:-[A-Za-z][0-9A-Za-z.]*)?", 0),                      # версия X.Y.Z
    (r"(?<!\w)[A-ZА-ЯЁ]{1,3}-\d+(?:\.\d+)*", 0),                                             # TK-236, В-12, G-001, E-3, UTF-8
    (r"(?<!\w)[Pp][Rr]\s*№?\s*#?\d+|[#№]\s*\d+", 0),                                              # PR #7, #7, №7
    (r"(?<!\w)(?:пп?|пункт\w*|§)\.?\s*\d+(?:[.,]\d+)*", 0),                                   # п.4, пункт 4
    (r"(?<!\w)(?=[0-9a-f]*[a-f])(?=[0-9a-f]*\d)[0-9a-f]{7,40}(?!\w)", 0),                    # sha (7–40 hex)
    (r"^[ \t]*(?:[-*+>][ \t]*)*\d+(?:\.\d+)*[.)](?=\s|$)", re.M),                            # «1.», «2)» в начале строки
    (r"^[ \t]*#{1,6}[ \t]+\d+(?:\.\d+)*[.)]?(?=\s|$)", re.M),                                # ## 1
))
_FREE_NUMBER = re.compile(r"(?<![\w.,])\d+(?:[.,]\d+)*")  # «CP9», «v2», «sha256» — метки, а не числа


def _text_numbers(text: str) -> list[str]:
    """Числа в тексте сдачи — всё, что не дата, время, номер (TK-/В-/G-/E-/PR #/п.), версия X.Y.Z, sha, файл:строка,
    адрес, номер пункта списка и заголовка. Метка с буквой слева («CP9», «v2») числом не считается."""
    for rx in _NOT_NUMBERS:
        text = rx.sub(" ", text)
    return [m.group(0) for m in _FREE_NUMBER.finditer(text)]


def _artifact_numbers(target: Path | None) -> list[str]:
    """Числа в текстовом артефакте (md, txt, csv, tsv, json, log) или в текстовых файлах каталога (до
    NUMBERS_SCAN_FILES файлов); не текст — пусто: код читать нечем, это держит проверка Судьи."""
    if target is None:
        return []
    if target.is_file():
        files = [target]
    else:
        files = []
        for dirpath, dirnames, names in os.walk(target):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            files += [Path(dirpath) / n for n in sorted(names) if Path(n).suffix.lower() in NUMBERS_TEXT_EXT]
            if len(files) >= NUMBERS_SCAN_FILES:
                break
    found: list[str] = []
    for f in files[:NUMBERS_SCAN_FILES]:
        if f.suffix.lower() not in NUMBERS_TEXT_EXT:
            continue
        try:
            found += _text_numbers(f.read_bytes()[:NUMBERS_SCAN_BYTES].decode("utf-8", errors="replace"))
        except OSError:
            continue
    return found


def _has_number(text: str, num: str) -> bool:
    """Число `num` есть в `text` целым числом (3 — не в 13 и не в 0,3); запятая и точка — одно."""
    text = text.replace("−", "-")
    return any(re.search(r"(?<![\d.,])" + re.escape(v) + r"(?!\d|[.,]\d)", text)
               for v in {num, num.replace(",", "."), num.replace(".", ",")})


def _numbers_ack(args, tid: str) -> tuple[str, str]:
    """CP9 (критерий 4): приёмка Судьи в V2-проекте — с таблицей ключевых чисел сдачи `| число | файл:строка |`
    (`--numbers <файл>`) или `--numbers -` (чисел нет). Номер строки обязателен: число обязано быть в названной строке ±2
    своего источника, иначе оно «из головы», отказ. `--numbers -` при приёмке по --path (без PR) код проверяет сам: в текстовом
    артефакте есть числа — отказ. Пути — от worktree тикета, если он есть, иначе от корня проекта."""
    if not registry_mod.decisions_file(PROJECT_ROOT).exists():
        return "", ""
    raw = (getattr(args, "numbers", None) or "").strip()
    root = PROJECT_ROOT.resolve()
    wt = root / ".claude" / "worktrees" / tid

    def find(rel: str, dirs: bool = False):
        for base in ((wt, root) if wt.is_dir() else (root,)):
            f = (Path(rel) if Path(rel).is_absolute() else base / rel).resolve()
            if (f.is_file() or (dirs and f.is_dir())) and f.is_relative_to(root):
                return f
        return None

    if raw in ("-", "—"):
        art = (getattr(args, "path", None) or "").strip()
        if art and not (getattr(args, "pr", None) or getattr(args, "sha", None)):  # приёмка по PR (код) — без этой проверки
            nums = list(dict.fromkeys(_artifact_numbers(find(art, dirs=True))))
            if nums:
                return (f"в сдаче есть числа (первые: {', '.join(nums[:5])}) — нужна таблица --numbers <файл с таблицей "
                        f"«| число | файл:строка |»>; --numbers - только когда чисел в {art} нет"), ""
        return "", " числа: нет"
    if not raw:
        return ("приёмка — с таблицей ключевых чисел: --numbers <файл с таблицей «| число | файл:строка |»> или "
                "--numbers - (чисел в сдаче нет); число без источника владельцу не уходит"), ""
    table = find(raw)
    if not table:
        return f"--numbers {raw}: файла нет в проекте", ""
    rows = []
    for ln in table.read_text(encoding="utf-8", errors="replace").splitlines():
        m = NUMBER_ROW.match(ln)
        num = NUMBER.search(m.group(1)) if m else None
        if num:
            rows.append((num.group(0).replace("−", "-"), m.group(2).strip().strip("`").strip()))
    if not rows:
        return f"--numbers {raw}: нет строк таблицы «| число | файл:строка |»", ""
    bad = []
    for num, src in rows:
        at = re.fullmatch(r"(.+?):(\d+)", src)
        f = find(at.group(1) if at else src)
        if not f:
            bad.append(f"{num} — источника {src} нет")
            continue
        lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        if not at:  # номер строки обязателен: короткое «3» иначе найдётся в любом месте файла
            hit = [str(i) for i, ln in enumerate(lines, 1) if _has_number(ln, num)][:3]
            bad.append(f"{num} — у {src} нет номера строки, пиши файл:строка"
                       + (f" (число есть в строках {', '.join(hit)})" if hit else " (такого числа в файле нет)"))
            continue
        n = int(at.group(2))
        if not 1 <= n <= len(lines):
            bad.append(f"{num} — в {at.group(1)} нет строки {n} (строк: {len(lines)})")
        elif not _has_number("\n".join(lines[max(n - 3, 0):n + 2]), num):
            bad.append(f"{num} — нет в {src} (строки {max(n - 2, 1)}–{min(n + 2, len(lines))})")
    if bad:
        return "числа не сходятся с источником (число «из головы» владельцу не уходит): " + "; ".join(bad[:8]), ""
    return "", f" числа: сверено {len(rows)} ({raw})"


def _set_due(path: Path, due, said: str = "") -> None:
    """Срок, названный владельцу: `due` — текущий, `due_first` — первый названный (по нему меряется «сбылось»)."""
    iso = due.isoformat(timespec="minutes")
    with T.ticket_lock(path):
        tkt = T.read_ticket(path)
        upd = {"due": iso}
        if not (tkt.header.get("due_first") or "").strip():
            upd["due_first"] = iso
        T.write_header_updates(path, upd, stamp_updated=False)
        T.append_log(path, "ceo", f"Срок владельцу: {iso}" + (f" — «{' '.join(said.split())}»" if said.strip() else ""))


def cmd_due(args) -> int:
    """CEO: срок, названный владельцу (критерий 4). Перенос — новая запись, первый срок остаётся в `due_first`."""
    role = _caller_role()
    if role and role != "ceo":
        print(f"due — только CEO (срок называет владельцу он); эта сессия — роль {role}", file=sys.stderr)
        return 1
    path = TICKETS_DIR / f"{args.id}.md"
    if not path.exists():
        print(f"нет тикета {args.id}", file=sys.stderr)
        return 1
    try:
        due = T.parse_by(args.by)
    except ValueError as e:
        print(f"due: {e}", file=sys.stderr)
        return 1
    _set_due(path, due, args.said or "")
    print(f"{args.id}: срок владельцу {due.isoformat(timespec='minutes')}")
    return 0


def cmd_accept(args) -> int:
    """Судья: машиночитаемый вердикт «принято» на конкретной голове PR (TK-079 п.2). Вливает программа (merge_rule), когда
    на этой голове CI зелёный и PR без конфликтов; сменится голова — вердикт не действует, нужен новый."""
    role = _caller_role()
    if role and role != "judge":
        print("accept: только Судья", file=sys.stderr)
        return 1
    path = TICKETS_DIR / f"{args.id}.md"
    if not path.exists():
        print(f"нет тикета {args.id}", file=sys.stderr)
        return 1
    if not re.fullmatch(r"[0-9a-f]{7,40}", args.sha):
        print("accept: --sha — хеш головы PR (7–40 hex)", file=sys.stderr)
        return 1
    import merge_rule
    import routes
    slug = (getattr(args, "repo", None) or D.P.setting("ci_repo", "") or "").strip()  # без догадки по cwd: PR живут не в этом репо
    if not slug:
        print("accept: репозиторий не задан — --repo <владелец/репо> или ci_repo в .claude/rpv.json", file=sys.stderr)
        return 1
    err = merge_rule.check_pr(slug, int(args.pr), args.sha)
    if err:
        print(f"accept: {err}", file=sys.stderr)
        return 1
    derr, ack = _decisions_ack(args)
    nerr, nack = _numbers_ack(args, args.id)
    if derr or nerr:
        print(f"accept: {derr or nerr}", file=sys.stderr)
        return 1
    ack += nack
    if _regression(args.id, "accept"):
        return 1
    tkt = T.read_ticket(path)
    prs = {int(n) for n in re.findall(r"\d+", str(tkt.header.get("pr") or ""))} | {int(args.pr)}
    if (routes.last_owner_result(tkt) == "done" and prs <= merge_rule.landed_prs(tkt) | {int(args.pr)}
            and merge_rule.merged_done(slug, int(args.pr))):
        # финальная приёмка: PR уже влит (все PR тикета влиты), владелец сдавал done — ждать нечего, круга не будет (TK-090 а)
        with T.ticket_lock(path):
            T.append_log(path, "judge", f"ПРИНЯТО PR #{args.pr} на голове {args.sha[:7]} (уже влит).{ack} {args.text or 'Тикет закрыт.'}")
            acc = merge_rule.parse_accepted(T.read_ticket(path).header.get("accepted"))
            acc.pop(int(args.pr), None)
            T.write_header_updates(path, {"accepted": merge_rule.format_accepted(acc), "status": "done", "wait_for": "",
                                         "on_met": "", "next": ""})
        print(f"{args.id}: принято PR #{args.pr}@{args.sha[:7]} (влит) → status: done")
        return 0
    with T.ticket_lock(path):
        T.append_log(path, "judge", f"ПРИНЯТО PR #{args.pr} на голове {args.sha[:7]}.{ack} "
                     + (args.text or "Влить, когда CI зелёный и нет конфликта — сделает merge_rule."))
        acc = merge_rule.parse_accepted(T.read_ticket(path).header.get("accepted"))
        acc[int(args.pr)] = args.sha  # вердикты по другим PR тикета не трогаем
        # ждём влития: после merged диспетчер разбудит владельца (done или следующий шаг); Судью повторно не будим
        T.write_header_updates(path, {"accepted": merge_rule.format_accepted(acc), "status": "waiting",
                                     "wait_for": f"merged:{slug}#{args.pr}", "on_met": "", "next": ""})
    print(f"{args.id}: принято PR #{args.pr}@{args.sha[:7]} → status: waiting, wait_for: merged:{slug}#{args.pr}")
    return 0


def cmd_start(args) -> int:
    """backlog → todo: задача, перенесённая из TASKS.md, берётся в работу — диспетчер начинает её видеть. stopped → todo:
    CEO возвращает остановленную (`stop` без `--next`) задачу в работу."""
    path = TICKETS_DIR / f"{args.id}.md"
    if not path.exists():
        print(f"нет тикета {args.id}", file=sys.stderr)
        return 1
    with T.ticket_lock(path):
        tkt = T.read_ticket(path)
        if tkt.status not in ("backlog", "stopped"):
            print(f"{args.id}: status={tkt.status!r}, не backlog/stopped — не трогаю", file=sys.stderr)
            return 1
        was = tkt.status
        T.write_header_updates(path, {"status": "todo"})
    print(f"{args.id}: {was} → todo")
    return 0


def cmd_wait(args) -> int:
    """`status: waiting` + `wait_for: <форма>` одной командой с проверкой формы: неизвестная форма — отказ с подсказкой
    (диспетчер такой `waiting` снять не умеет — тикет ждал бы вечно)."""
    path = TICKETS_DIR / f"{args.id}.md"
    if not path.exists():
        print(f"нет тикета {args.id}", file=sys.stderr)
        return 1
    spec = args.spec.strip()
    if not spec:
        print(f"wait: форма пуста. Допустимо: {T.WAIT_FOR_FORMATS}", file=sys.stderr)
        return 1
    if why := T.file_wait_problem(spec):  # роль ставит ожидание сама: отказ при постановке, а не вечное «нет»
        print(f"wait: {why}", file=sys.stderr)
        return 1
    by = (getattr(args, "by", None) or "").strip()
    if T.wait_needs_by(spec) and not by:
        print("wait: нужен срок `--by ЧЧ:ММ|ISO` (GMT+4) — когда ждать конца самое позднее; прошёл срок (+15 мин) и цель "
              "не выполнена — сторож разбудит тебя (TK-117). Оцени по ходу (done/total, скорость), не «с запасом на всю ночь»", file=sys.stderr)
        return 1
    if by:
        try:
            by_dt = T.parse_by(by)
        except ValueError as e:
            print(f"wait: {e}", file=sys.stderr)
            return 1
    parsed = T.parse_wait_for(spec)
    # TK-218 п.2: условие, уже выполненное при постановке, не ожидание — роль проснулась бы по старому файлу
    # (маркер прошлого прогона). Осознанно ждать готовое — `--allow-met`.
    if parsed and parsed[0] in ("file", "host", "ticket") and not getattr(args, "allow_met", False):
        try:
            met = D.check_wait_for(spec)
        except Exception:  # нет ssh/алиаса в окружении — проверка недоступна, не мешаем
            met = False
        if met:
            print(f"wait: {spec} уже выполнено на момент постановки (файл/тикет существует) — это маркер прошлого "
                  f"прогона или чужой результат: ждать нечего. Удали старый маркер или возьми новый путь; "
                  f"осознанно ждать готовое — `--allow-met`.", file=sys.stderr)
            return 1
    if parsed and parsed[0] == "ticket":
        cycle = T.wait_cycle(TICKETS_DIR, args.id, parsed[1])
        if cycle:
            print(f"wait: цикл ожиданий {' -> '.join(cycle)} — каждый ждёт следующего, никто не пойдёт. Ждите "
                  f"результат (file:/host:…), а не тикет, или разорвите цепочку.", file=sys.stderr)
            return 1
    try:
        upd = {"status": "waiting", "wait_for": spec, "wait_by": by_dt.isoformat(timespec="seconds") if by else ""}
        if getattr(args, "on_met", None):
            upd["on_met"] = args.on_met.strip()
        T.write_header_updates(path, upd)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 1
    print(f"{args.id}: status: waiting, wait_for: {spec}" + (f", срок {upd['wait_by']}" if by else ""))
    return 0


def cmd_stop(args) -> int:
    """CEO: остановить запущенную роль тикета и дать новую постановку. Только заявка диспетчеру (`stop/<ID>.json`): снятие
    процесса, след, статус и запись CEO в лог делает диспетчер на ближайшем тике (README, «Остановка роли»)."""
    role = _caller_role()
    if role and role != "ceo":
        print(f"stop — только CEO; эта сессия — роль {role}", file=sys.stderr)
        return 1
    path = TICKETS_DIR / f"{args.id}.md"
    if not path.exists():
        print(f"нет тикета {args.id}", file=sys.stderr)
        return 1
    text = (args.text or "").strip()
    if not text:
        print("stop: --text пуст — нужна новая постановка", file=sys.stderr)
        return 1
    D.write_stop_request(path.stem, args.next or "", text)
    print(f"{args.id}: заявка на остановку принята — на ближайшем тике диспетчер снимет запущенную роль; дальше: "
          + (f"todo, next: {args.next}" if args.next else "status: stopped (не будить)"))
    return 0


def cmd_allow(args) -> int:
    """CEO: согласие владельца на публикацию по тикету (CP2, критерий 14). Поле шапки `allow: publish (В-N)` читает страж
    (`delete_guard.publish_consent`): без него запуску роли закрыты выпуск, слияние PR, пуш в main/master и теги, публикация
    пакетов. Основание — решение владельца из реестра, занесённое ДО согласия; `--revoke` снимает поле."""
    role = _caller_role()
    if role and role != "ceo":
        print(f"allow — только CEO; эта сессия — роль {role}. Нужна публикация — `result <ID> ask-owner --why-owner необратимое`",
              file=sys.stderr)
        return 1
    path = TICKETS_DIR / f"{args.id}.md"
    if not path.exists():
        print(f"нет тикета {args.id}", file=sys.stderr)
        return 1
    if args.revoke:
        with T.ticket_lock(path):
            if not T.read_ticket(path).header.get("allow"):
                print(f"{args.id}: согласия на публикацию не было — снимать нечего")
                return 0
            T.write_header_updates(path, {"allow": ""}, stamp_updated=False)
            T.append_log(path, "ceo", "согласие на публикацию снято")
        print(f"{args.id}: согласие на публикацию снято")
        return 0
    ref = (args.ref or "").strip()
    if not args.what:
        print("allow: нужно слово `publish` — `allow <ID> publish --ref В-N`", file=sys.stderr)
        return 1
    if not re.fullmatch(r"В-\d+", ref) or not registry_mod.ref_exists(PROJECT_ROOT, ref):
        print(f"allow: --ref {ref or '(пусто)'} — нужно решение владельца В-N, уже занесённое в реестр (`registry.py list`). "
              f"Сначала слово владельца дословно — `registry.py decide`, потом согласие", file=sys.stderr)
        return 1
    with T.ticket_lock(path):
        T.write_header_updates(path, {"allow": f"publish ({ref})"}, stamp_updated=False)
        T.append_log(path, "ceo", f"согласие владельца на публикацию по этому тикету ({ref}): выпуск, слияние PR, пуш в "
                     f"основную ветку и теги, публикация пакетов")
    print(f"{args.id}: allow: publish ({ref})")
    return 0


def cmd_status(args) -> int:
    state = D.load_state()
    rows = []
    for path in T.list_tickets(TICKETS_DIR):
        try:
            tkt = T.read_ticket(path)
        except Exception as e:
            rows.append((path.stem, f"<ошибка разбора: {e}>", "", "", "", "", ""))
            continue
        tid = tkt.id
        spent_col = f"${D.ticket_cost_spent(state, tid):.2f}"  # учёт, не лимит (В-173)
        rows.append((tid, tkt.header.get("title", "")[:40], tkt.owner, tkt.status,
                     tkt.reviewer, spent_col, tkt.header.get("updated", "")))
    if not rows:
        print("тикетов нет")
        return 0
    header = ("ид", "заголовок", "владелец", "статус", "ревьюер", "потрачено", "обновлён")
    widths = [max(len(str(r[i])) for r in rows + [header]) for i in range(7)]
    fmt = "  ".join("{:<%d}" % w for w in widths)
    print(fmt.format(*header))
    for r in rows:
        print(fmt.format(*r))
    now = datetime.now().astimezone()
    for path in T.list_tickets(TICKETS_DIR):  # ожидания по времени: «ждёт до …, осталось N ч»
        try:
            tkt = T.read_ticket(path)
        except Exception:
            continue
        left = T.at_left_text(tkt.header.get("wait_for", "")) if tkt.status == "waiting" else ""
        if left:
            print(f"{tkt.id}: {left}")
        if by_left := T.by_left_text(tkt, now):
            print(f"{tkt.id}: {by_left}")
    print(f"потрачено: за сутки ${state.get('daily_cost', {}).get(D._today(now), 0.0):.2f}, "
          f"за последний час ${D._rolling_hour_cost(state, now):.2f}")
    return 0


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    p = argparse.ArgumentParser(prog="tickets.py")
    p.add_argument("--project", default=None,
                   help="корень проекта (иначе RPV_PROJECT, CLAUDE_PROJECT_DIR, ближайший каталог вверх с .claude/roles); "
                        "перед подкомандой")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_new = sub.add_parser("new")
    p_new.add_argument("--owner", required=True, choices=["researcher", "engineer", "judge"])
    p_new.add_argument("--title", required=True)
    p_new.add_argument("--reviewer", choices=["researcher", "engineer", "judge"],
                        help="ревьюер; по умолчанию Судья (у Haiku-задач и тикетов Судьи — никого)")
    p_new.add_argument("--no-reviewer", action="store_true",
                        help="только с --executor haiku (там и так без Судьи); иначе отказ — Судью снимает владелец")
    p_new.add_argument("--effort", choices=list(T.VALID_EFFORTS), default=None,
                        help="усилие запусков по этому тикету; без поля — исследователь/инженер high, Судья xhigh")
    p_new.add_argument("--ref", default="", help="обязательно: цель G-NNN или решение В-N из реестра владельца "
                                                  "(без реестра — цитата владельца или В-##)")
    p_new.add_argument("--due", default=None, help="срок, названный владельцу: ЧЧ:ММ или ISO (GMT+4)")
    p_new.add_argument("--said", default=None, help="как срок сказан владельцу (дословно)")
    p_new.add_argument("--desc", default="")
    p_new.add_argument("--wait-for", dest="wait_for", default="")
    p_new.add_argument("--force", action="store_true", help="создать, даже если за час есть тикет с похожим заголовком")
    p_new.add_argument("--backlog", action="store_true",
                        help="создать сразу в backlog (перенос из TASKS.md) — диспетчер её не трогает до `start`")
    p_new.add_argument("--executor", choices=["haiku"], default=None,
                        help="claude-haiku-5-5 для чисто механических задач — требует --kind")
    p_new.add_argument("--kind", choices=sorted(D.HAIKU_ALLOWED_KINDS), default=None,
                        help="вид задачи для --executor haiku")
    p_new.set_defaults(func=cmd_new)

    p_comment = sub.add_parser("comment")
    p_comment.add_argument("id")
    p_comment.add_argument("--author", required=True)
    p_comment.add_argument("--text", required=True)
    p_comment.add_argument("--next", choices=["researcher", "engineer", "judge", "ceo"], default=None,
                            help="разбудить эту роль один раз (ceo — только blocked/нужно решение владельца)")
    p_comment.set_defaults(func=cmd_comment)

    p_accept = sub.add_parser("accept", help="Судья: принято, PR N на голове SHA (вливает merge_rule по правилу)")
    p_accept.add_argument("id")
    p_accept.add_argument("--pr", type=int, required=True)
    p_accept.add_argument("--sha", required=True)
    p_accept.add_argument("--text", default="")
    p_accept.add_argument("--repo", default=None, help="<владелец/репо> PR (иначе ci_repo в .claude/rpv.json)")
    p_accept.add_argument("--decisions", default=None, help="решения владельца, с которыми сверено: В-N[,В-M] или - (связанных нет)")
    p_accept.add_argument("--numbers", default=None, help="файл с таблицей «| число | файл:строка |» или - (чисел нет)")
    p_accept.set_defaults(func=cmd_accept)

    p_result = sub.add_parser("result", help="сдать шаг: итог из списка + --why + доказательство; кого будить — таблица маршрутов")
    p_result.add_argument("id")
    p_result.add_argument("result", help="done | pr | accept | return | blocked | ask-owner | wait | continue")
    p_result.add_argument("--why", default="", help="зачем/почему, ≤ 200 знаков (обязательно)")
    p_result.add_argument("--pr", type=int, default=None, help="pr, accept: номер PR")
    p_result.add_argument("--sha", default=None, help="pr, accept, return: голова PR (7–40 hex)")
    p_result.add_argument("--path", default=None, help="done: файл/каталог результата")
    p_result.add_argument("--form", default=None, help="wait: форма wait_for")
    p_result.add_argument("--why-owner", dest="why_owner", default=None, choices=list(routes_mod.OWNER_REASONS),
                          help="ask-owner Судьи: повод вопроса владельцу (деньги|направление|необратимое|бизнес-правило)")
    p_result.add_argument("--by", default=None, help="wait: срок ЧЧ:ММ|ISO (GMT+4), обязателен для host:/file:/job:")
    p_result.add_argument("--allow-met", dest="allow_met", action="store_true", help="wait: условие уже выполнено — всё равно ждать")
    p_result.add_argument("--repo", default=None, help="accept по PR: <владелец/репо> (иначе ci_repo в .claude/rpv.json)")
    p_result.add_argument("--decisions", default=None,
                          help="accept: решения владельца, с которыми сверена сдача: В-N[,В-M] или - (связанных нет)")
    p_result.add_argument("--numbers", default=None,
                          help="accept: файл с таблицей ключевых чисел «| число | файл:строка |» или - (чисел нет; код проверяет по --path)")
    p_result.set_defaults(func=cmd_result)

    p_start = sub.add_parser("start")
    p_start.add_argument("id")
    p_start.set_defaults(func=cmd_start)

    p_wait = sub.add_parser("wait", help="status: waiting + wait_for (форма проверяется)")
    p_wait.add_argument("--allow-met", dest="allow_met", action="store_true",
                        help="ждать условие, уже выполненное при постановке (по умолчанию отказ)")
    p_wait.add_argument("id")
    p_wait.add_argument("spec", help=T.WAIT_FOR_FORMATS)
    p_wait.add_argument("--by", default=None, help="срок ожидания ЧЧ:ММ|ISO (GMT+4), обязателен для host:/file:/job:")
    p_wait.add_argument("--on-met", default=None,
                        help="команда по закрытии wait_for вместо пробуждения LLM: `python|bash <скрипт под tools/ или .claude/, в git> [арг]`")
    p_wait.set_defaults(func=cmd_wait)

    p_due = sub.add_parser("due", help="только CEO: срок, названный владельцу (первый остаётся в due_first)")
    p_due.add_argument("id")
    p_due.add_argument("--by", required=True, help="ЧЧ:ММ или ISO (GMT+4)")
    p_due.add_argument("--said", default=None, help="как срок сказан владельцу (дословно)")
    p_due.set_defaults(func=cmd_due)

    p_stop = sub.add_parser("stop", help="только CEO: остановить запущенную роль тикета и дать новую постановку")
    p_stop.add_argument("id")
    p_stop.add_argument("--text", required=True, help="новая постановка — запись CEO в лог тикета")
    p_stop.add_argument("--next", choices=["researcher", "engineer", "judge"], default=None,
                        help="роль, которая стартует сразу (тикет станет todo); без --next — status: stopped, не будить")
    p_stop.set_defaults(func=cmd_stop)

    p_allow = sub.add_parser("allow", help="только CEO: согласие владельца на публикацию по тикету (или --revoke)")
    p_allow.add_argument("id")
    p_allow.add_argument("what", nargs="?", choices=["publish"], help="что разрешено: публикации (выпуск, слияние PR, пуш в main, пакеты)")
    p_allow.add_argument("--ref", default="", help="решение владельца В-N из реестра (`registry.py decide`)")
    p_allow.add_argument("--revoke", action="store_true", help="снять согласие")
    p_allow.set_defaults(func=cmd_allow)

    p_inbox = sub.add_parser("inbox", help="CEO: новые строки ceo-inbox.md с прошлого чтения")
    p_inbox.add_argument("--peek", action="store_true", help="только показать, не считать прочитанным")
    p_inbox.set_defaults(func=cmd_inbox)

    p_status = sub.add_parser("status")
    p_status.set_defaults(func=cmd_status)

    args = p.parse_args(argv)
    if not D.ensure_project(["--project", args.project] if args.project else [], "tickets"):
        return 2                                 # нет проекта — ошибка с подсказкой, тикет не создаётся
    if args.project:
        global TICKETS_DIR, PROJECT_ROOT
        TICKETS_DIR, PROJECT_ROOT = D.TICKETS_DIR, D.PROJECT_ROOT
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
