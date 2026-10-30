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
    tickets.py charter sync [--file judge.md]                    # только CEO: уставы из templates/roles/ плагина в .claude/roles/
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

import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ceo_inbox as CI  # noqa: E402
import charter as charter_mod  # noqa: E402
import rpv_config as cfg_mod  # noqa: E402
import signals as SIG  # noqa: E402
import cost as COST  # noqa: E402
import probe as PROBE  # noqa: E402
import hostload as LOAD  # noqa: E402
import stop as STOP  # noqa: E402
import project_paths as CFG  # noqa: E402
import errors as errors_mod  # noqa: E402
import project as P  # noqa: E402
import registry as registry_mod  # noqa: E402
import routes as routes_mod  # noqa: E402
import ticket as T  # noqa: E402
import waits as W  # noqa: E402
import tickets_numbers as numbers_mod  # noqa: E402
import tickets_proof as proof_mod  # noqa: E402
from tickets_cli import build_parser  # noqa: E402

# Проект и тикеты берутся у диспетчера (CFG.configure_project при импорте — по --project/RPV_PROJECT/CLAUDE_PROJECT_DIR/поиску вверх);
# в main() флаг --project переключает их ещё раз. Тесты подменяют TICKETS_DIR прямо на модуле.
TICKETS_DIR = CFG.TICKETS_DIR
REF_MIN = 5  # знаков в цитате --ref; решение «В-<число>» (REF_DECISION) проходит любой длины, «да»/пусто — нет
REF_DECISION = re.compile(r"^В-\d+")
REF_REGISTRY = re.compile(r"^(В-\d+|G-\d{3,})\b")  # номер реестра владельца: есть реестр — номер обязан в нём быть
PROJECT_ROOT = CFG.PROJECT_ROOT
_text_numbers, _artifact_numbers, _has_number = numbers_mod.text_numbers, numbers_mod.artifact_numbers, numbers_mod.has_number


def cmd_inbox(args) -> int:
    """Очередь CEO: новые строки ceo-inbox.md с прошлого чтения (отметка `.ceo-inbox-seen` — общая с хуком сессии);
    `--peek` отметку не двигает."""
    seen_f = CFG.CEO_INBOX.parent / CI.SEEN_NAME
    lines, seen = CI.read(CFG.CEO_INBOX, seen_f)
    for ln in lines[seen:]:
        print(ln)
    if len(lines) == seen:
        print("очередь CEO пуста (новых строк в ceo-inbox.md нет)")
    elif not args.peek:
        CI.mark(seen_f, len(lines))
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
        if args.kind not in SIG.HAIKU_ALLOWED_KINDS:
            print(f"--executor haiku требует --kind из {sorted(SIG.HAIKU_ALLOWED_KINDS)}", file=sys.stderr)
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
    nxt, auto_next, kept = args.next, False, ""
    with T.ticket_lock(path):  # запись, `next` и сжатие лога — одним куском (диспетчер правит шапку тем же замком)
        T.append_log(path, args.author, args.text)
        if not nxt and T.author_is(args.author, "judge"):
            # Судья вернул тикет без `--next`: пока тикет в todo/in_progress/in_review/waiting, ход возвращается
            # владельцу; done, stopped, needs_owner, blocked — не будим. Непустой `next` (заказ CEO, ещё не исполненный) не затираем
            tkt = T.read_ticket(path)
            kept = (tkt.header.get("next") or "").strip()
            if not kept and tkt.status in T.ACTIVE_STATUSES and tkt.owner in ("researcher", "engineer"):
                nxt, auto_next = tkt.owner, True
        if nxt:
            # v2: единственный будильник другой роли/CEO; `updated` не двигаем (маркеры уведомлений CEO по нему)
            T.write_header_updates(path, {"next": nxt}, stamp_updated=False)
            # №15 аудита: blocked + next: роль — запись без запуска; ход роли снимает blocked (иначе тикет висит с будильником)
            if nxt in P.ROLES and T.read_ticket(path).status == "blocked":
                T.write_header_updates(path, {"status": "in_progress"}, stamp_updated=False)
        moved = T.compact_log(path)
    print(f"дописано в {path}" + (f"; next: {nxt}" if nxt else "")
          + (f"; next проставлен автоматически (запись Судьи без --next → владелец тикета: {nxt})" if auto_next else "")
          + (f"; next оставлен: {kept}" if kept else "")
          + (f"; в архив перенесено записей: {moved}" if moved else ""))
    return 0


def _regression(tid: str, cmd: str) -> bool:
    """V2-3: приёмка Судьи — только при зелёных проверках всех исправленных ошибок (`errors.py`). True — отказ (причина уже
    в stderr, в тикет ничего не пишем). Нет реестра ошибок или исправленных в нём — ничего не запускается. Проверка, что «не
    запустилась», — тоже отказ (не проверено), а повтор сверх потолка зовёт CEO (`SIG.append_ceo_inbox`)."""
    err = errors_mod.gate(PROJECT_ROOT, tid, signal=SIG.append_ceo_inbox)
    if err:
        print(f"{cmd}: {err}", file=sys.stderr)
    return bool(err)


def _result_refusal(args, role: str, tkt) -> str:
    """Отказ шагу: неполная команда (`routes.check`) или `continue` сверх потолка подряд; пусто — можно писать."""
    err = routes_mod.check(role, args.result, args.why, pr=args.pr, sha=args.sha or "", path=args.path or "",
                           form=args.form or "", root=PROJECT_ROOT, owner=tkt.owner or "",
                           why_owner=getattr(args, "why_owner", None) or "")
    if err:
        return err
    if args.result == "continue" and routes_mod.continue_streak(tkt, role) >= routes_mod.CONTINUE_MAX:
        return (f"continue уже {routes_mod.CONTINUE_MAX} раз подряд — сдай pr/done или wait (долгое — фоновый процесс), "
                f"либо blocked")
    return ""


def _result_proof(args, role: str, tkt) -> str:
    """Доказательство в записи «[итог: …]»: PR@голова, артефакт проверки, результат, ждём, повод."""
    on_head = f" голова {(args.sha or '')[:7]}" if args.sha else f" проверка: {args.path}"
    if args.result == "accept" and not args.pr:  # приёмка без PR: артефакт проверки, sha (если дан) — пометка
        on_head = f" проверка: {args.path}" + (f" голова {args.sha[:7]}" if args.sha else "")
    proof = {"pr": f" PR #{args.pr}@{(args.sha or '')[:7]}", "accept": on_head, "return": on_head,
             "done": f" результат: {args.path}", "wait": f" ждём: {args.form}",
             "ask-owner": f" повод: {args.why_owner}" if getattr(args, "why_owner", None) else ""}.get(args.result, "")
    if role == "judge" and tkt.owner == "judge" and args.result in ("done", "accept") and not (args.pr or args.sha):
        head, _ = routes_mod.tracked_at_head(PROJECT_ROOT, args.path)  # TK-225: формальное закрытие по артефакту в git
        proof = f" закрыт по артефакту {args.path}@{head}"
    if args.result == "done" and role == "ceo":
        proof += f" ({SIG.NO_REVIEW_MARK})"
    return proof


def _accept_checks(args) -> tuple[str, str, str]:
    """Сверки приёмки Судьи: решения, числа, хвосты, живая проверка. → (отказ, пометки для лога, хвост для CEO)."""
    err, ack = _decisions_ack(args)
    nerr, nack = _numbers_ack(args, args.id)
    terr, tack, tail_ceo = _tail_ack(args, args.id)
    lerr, lack = _live_ack(args, args.id)
    err = err or nerr or terr or lerr
    perr, pack = ("", "") if err or args.pr else _proof_ack(args.id)  # с PR команды гонит cmd_accept: дважды не гоняем
    return err or perr, ack + nack + tack + lack + pack, tail_ceo


def _accept_in_pr(args, path: Path, role: str, why: str, ack: str) -> int:
    """Приёмка на голове PR: слияние — `cmd_accept`; запись итога — только при успехе (иначе Stop-хук `stop_result.py`
    не видит сдачи шага и блокирует Судью)."""
    rc = cmd_accept(type("A", (), {"id": args.id, "pr": args.pr, "sha": args.sha, "text": why,
                                  "repo": getattr(args, "repo", None),
                                  "decisions": getattr(args, "decisions", None),
                                  "numbers": getattr(args, "numbers", None),
                                  "tail": getattr(args, "tail", None),
                                  "live": getattr(args, "live", None),
                                  "after_merge": getattr(args, "after_merge", None)})())
    if rc == 0:
        with T.ticket_lock(path):
            T.append_log(path, role, f"[итог: accept] PR #{args.pr}@{args.sha[:7]}{ack} — {why}{_after_merge_note(args)}")
    return rc


def _wait_in_result(args, path: Path, role: str, why: str, proof: str) -> int:
    """Итог wait: сначала условие (цикл, форма), и только при успехе — запись: отказ ничего не пишет."""
    rc = cmd_wait(type("A", (), {"id": args.id, "spec": args.form, "on_met": None, "by": getattr(args, "by", None),
                                  "allow_met": getattr(args, "allow_met", False), "probe": getattr(args, "probe", None)})())
    if rc == 0:
        with T.ticket_lock(path):
            T.append_log(path, role, f"[итог: wait]{proof} — {why}")
    return rc


def _write_result(args, path: Path, role: str, tkt, why: str, proof: str, tail_ceo: str) -> None:
    """Запись итога в лог, номер PR в шапку, хвост CEO в очередь и ход по таблице маршрутов."""
    with T.ticket_lock(path):
        T.append_log(path, role, f"[итог: {args.result}]{proof} — {why}")
        if args.result == "pr":
            prs = [n for n in re.findall(r"\d+", str(tkt.header.get("pr") or "")) if n != str(args.pr)]
            T.write_header_updates(path, {"pr": ", ".join(prs + [str(args.pr)])}, stamp_updated=False)
    if tail_ceo:
        SIG.append_ceo_inbox(args.id, "хвост", tail_ceo)
    upd = routes_mod.route(role, args.result, tkt.owner, tkt.header.get("reviewer", ""),
                           owner_last=routes_mod.last_owner_result(tkt) if args.result == "accept" else "", executor=tkt.executor)
    T.write_header_updates(path, upd)
    print(f"{args.id}: итог {args.result} → status: {upd['status']}" + (f", next: {upd['next']}" if upd.get("next") else ""))


def cmd_result(args) -> int:
    """Сдать шаг: итог из закрытого списка + зачем + доказательство; кого будить — таблица routes.py (TK-079 п.0).
    Неполная команда — отказ с текстом, что исправить; запись в лог тикета ставит сама команда."""
    path = TICKETS_DIR / f"{args.id}.md"
    if not path.exists():
        print(f"нет тикета {args.id}", file=sys.stderr)
        return 1
    role = _caller_role()
    tkt = T.read_ticket(path)
    err = _result_refusal(args, role, tkt)
    if err:
        print(f"result: {err}", file=sys.stderr)
        return 1
    role = role or "ceo"  # check() пускает сессию без роли только к done (закрыть тикет)
    why = args.why.strip()
    proof = _result_proof(args, role, tkt)
    tail_ceo = ""
    if args.result == "accept":
        err, ack, tail_ceo = _accept_checks(args)
        if err:
            print(f"result: {err}", file=sys.stderr)
            return 1
        proof += ack
        if args.pr:
            return _accept_in_pr(args, path, role, why, ack)
        if _regression(args.id, "result"):  # приёмка в PR проходит ту же проверку в cmd_accept
            return 1
    if args.result == "wait":
        return _wait_in_result(args, path, role, why, proof)
    _write_result(args, path, role, tkt, why, proof, tail_ceo)
    return 0


def _caller_role() -> str:
    """Роль вызывающей сессии: `RPV_ROLE` запуска роли, а стёрта — по дереву процессов (`project.caller_role`); у CEO и
    владельца её нет."""
    return P.caller_role(CFG.PROJECT_ROOT)


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


# «Принято с хвостом» (разбор alpha 14–16.10: TK-240, TK-245, TK-257 — исполнитель сам написал «не сделано / не достигнуто»,
# Судья принял). Приёмка в V2-проекте — с явным ответом про хвосты; «хвостов нет» сверяется с последней сдачей исполнителя.
TAIL_WORDS = re.compile(r"не\s+сделан|не\s+достигнут|не\s+доделан|не\s+выполнен|не\s+реализован|не\s+закончен|не\s+действует|"
                        r"не\s+работает|\bхвост|\bосталось\b|отдельн\w*\s+тикет", re.I)
# Отрицание хвоста — не хвост (alpha TK-264: «… Хвостов нет» + `--tail -` отклонялось корнем `хвост`). Гасим только законченный
# оборот: «хвост: нет метрик у 431 строки» или «хвост нет метрик» — по-прежнему хвост; «не осталось времени доделать» —
# тоже (гасится только «ничего не осталось», E-001). «Осталось: приёмка» — штатная строка сдачи (устав Инженера: что осталось),
# не хвост, если оборот закончен точкой, «!» или концом строки; «осталось: приёмка и/;/—/(/+ экспорт» — хвост (E-007).
# Так же пустой остаток: «Осталось: ничего», «Осталось: нет», «не осталось ничего» — не хвост в законченном обороте;
# «осталось: ничего, кроме …», «осталось: нет метрик» — хвост (E-011).
_TAIL_END = r"(?=[ \t\r]*(?:[.!]|\n|$))"
TAIL_NEGATION = re.compile(r"хвост\w*\s+нет\b(?!\s+[\w])|\bнет\s+хвост\w*|\bбез\s+хвост\w*|\bничего\s+не\s+осталось\b(?!\s+\w)|"
                           r"\bосталось\s+(?:0|ноль)\b(?![.,]\d)|"
                           r"\bосталось[:\s—–-]+(?:только\s+)?при[её]мк\w*(?:\s+судьи\b)?" + _TAIL_END + r"|"
                           r"\bосталось[:\s—–-]+(?:ничего|нет)\b" + _TAIL_END + r"|"
                           r"\bне\s+осталось\s+ничего\b" + _TAIL_END, re.I)
TAIL_HINT = ("--tail - (хвостов нет) | --tail TK-M[,TK-N] (тикеты на хвост заведены до приёмки) | "
             "--tail ceo:\"что сделать CEO\" (хвост, который ролям не сделать: настройки, CLAUDE.md, решение владельца)")


def _owner_submission(tkt) -> str:
    """Текст последней сдачи исполнителя (`[итог: done|pr]` владельца тикета) — то, что принимает Судья."""
    for e in reversed(tkt.log):
        if T.author_is(e.author, tkt.owner or "") and re.match(r"\[итог:\s*(done|pr)\]", e.text.strip()):
            return e.text
    return ""


# Живая проверка (разбор alpha 14–16.10, П1): дефекты, которые нашёл владелец, видны за секунды на настоящей странице, а
# сдачу проверяли на dry_run и фикстурах. Пункт заказа или договора с меткой `[вживую]` принимается только с файлом, где
# Судья записал, что увидел сам на настоящей машине и настоящих данных (малый срез): `--live <файл>`. Код не может
# проверить, что Судья смотрел, — он требует явной записи; ложь в ней видна в логе. Слова и время файла не проверяются:
# честное «не на dry_run» и пересохранение дали бы ложные отказы и ложные пропуски.
LIVE_MARK = re.compile(r"\[вживую\]", re.I)
LIVE_MIN_CHARS = 40
LIVE_HINT = ("файл в проекте (например docs/reviews/<тикет>-live.md): где открыл (X), что увидел (Y), за сколько (Z с), "
             "на каком срезе настоящих данных; открыть нечем — не принимать, а `blocked`")


def _live_ack(args, tid: str) -> tuple[str, str]:
    """Есть `[вживую]` в описании тикета или в записях исполнителя (договор) — приёмка только с `--live <файл>`. → (отказ, пометка)."""
    path = TICKETS_DIR / f"{tid}.md"
    if not path.exists():
        return "", ""
    tkt = T.read_ticket(path)
    texts = [tkt.description] + [e.text for e in tkt.log if T.author_is(e.author, tkt.owner or "")]
    if not any(LIVE_MARK.search(t or "") for t in texts):
        return "", ""
    raw = (getattr(args, "live", None) or "").strip()
    if not raw:
        return f"в договоре есть пункт [вживую] — приёмка только с --live <файл>: {LIVE_HINT}", ""
    f = Path(raw) if Path(raw).is_absolute() else PROJECT_ROOT / raw
    try:
        rel = f.resolve().relative_to(PROJECT_ROOT.resolve()).as_posix()
    except ValueError:
        return f"--live {raw}: файл вне проекта — {LIVE_HINT}", ""
    if not f.is_file():
        return f"--live {raw}: файла нет — {LIVE_HINT}", ""
    if len(f.read_text(encoding="utf-8", errors="replace").strip()) < LIVE_MIN_CHARS:
        return f"--live {raw}: пусто — {LIVE_HINT}", ""
    return "", f" вживую: {rel}"


def _proof_ack(tid: str) -> tuple[str, str]:
    """Ярлыки доказательства договора (TK-050): приёмка сама перезапускает его команды, код не 0 — отказ (`tickets_proof`)."""
    path = TICKETS_DIR / f"{tid}.md"
    return proof_mod.proof_ack(tid, T.read_ticket(path), PROJECT_ROOT) if path.exists() else ("", "")


def _tail_ack(args, tid: str) -> tuple[str, str, str]:
    """Приёмка Судьи в V2-проекте — с ответом про хвосты (`--tail`). «Не сделано / не достигнуто» в сдаче исполнителя при
    `--tail -` — отказ: недоделанное либо `return`, либо хвост заведён тикетом, либо передан CEO. → (отказ, пометка, текст CEO)."""
    if not registry_mod.decisions_file(PROJECT_ROOT).exists():
        return "", "", ""
    raw = (getattr(args, "tail", None) or "").strip()
    if not raw:
        return f"приёмка — с ответом про хвосты: {TAIL_HINT}; не сделано то, что в заказе или договоре, — не принимать, а вернуть", "", ""
    if raw in ("-", "—"):
        path = TICKETS_DIR / f"{tid}.md"
        sub = _owner_submission(T.read_ticket(path)) if path.exists() else ""
        sub = TAIL_NEGATION.sub(lambda m: " " * len(m.group()), sub)  # длина та же: цитата в отказе берётся из этого же текста
        hit = TAIL_WORDS.search(sub)
        if hit:
            return (f"--tail -: в сдаче исполнителя «{sub[max(0, hit.start() - 40):hit.end() + 60].strip()}» — это хвост. "
                    f"Не сделано из заказа или договора — `return`; иначе {TAIL_HINT}"), "", ""
        return "", " хвосты: нет", ""
    if raw.lower().startswith("ceo:"):
        what = " ".join(raw[4:].split()).strip("\"' ")
        if len(what) < 5:
            return f"--tail ceo:\"…\" — напиши, что сделать CEO ({TAIL_HINT})", "", ""
        return "", f" хвост CEO: {what[:120]}", what
    ids = [x for x in re.split(r"[,\s]+", raw) if x]
    bad = [x for x in ids if not re.fullmatch(r"TK-\d+", x) or x == tid
           or not any((d / f"{x}.md").exists() for d in (TICKETS_DIR, TICKETS_DIR / "archive"))]
    if bad:
        return f"--tail: {', '.join(bad)} — нужен номер другого, уже заведённого тикета на хвост ({TAIL_HINT})", "", ""
    return "", f" хвосты: {', '.join(ids)}", ""


def _numbers_ack(args, tid: str) -> tuple[str, str]:
    """Таблица ключевых чисел приёмки (`--numbers`): сверка — `tickets_numbers.numbers_ack`, корень проекта — тот, что у модуля."""
    return numbers_mod.numbers_ack(args, tid, PROJECT_ROOT)



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


def _after_merge_note(args) -> str:
    """Пометка приёмки «после слияния: <шаг>» (TK-027): по ней закрытие по слиянию (`decide.close_after_merge`) не закрывает
    тикет, а будит автора на этот шаг. Одна строка — разбор ищет шаг до конца строки."""
    step = " ".join((getattr(args, "after_merge", None) or "").split())
    return f" после слияния: {step}" if step else ""


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
    slug = (getattr(args, "repo", None) or P.setting("ci_repo", "") or "").strip()  # без догадки по cwd: PR живут не в этом репо
    if not slug:
        print("accept: репозиторий не задан — --repo <владелец/репо> или ci_repo в .claude/rpv.json", file=sys.stderr)
        return 1
    err = merge_rule.check_pr(slug, int(args.pr), args.sha)
    if err:
        print(f"accept: {err}", file=sys.stderr)
        return 1
    derr, ack = _decisions_ack(args)
    nerr, nack = _numbers_ack(args, args.id)
    terr, tack, tail_ceo = _tail_ack(args, args.id)
    lerr, lack = _live_ack(args, args.id)
    perr, pack = ("", "") if derr or nerr or terr or lerr else _proof_ack(args.id)
    if derr or nerr or terr or lerr or perr:
        print(f"accept: {derr or nerr or terr or lerr or perr}", file=sys.stderr)
        return 1
    ack += nack + tack + lack + pack
    if _regression(args.id, "accept"):
        return 1
    if tail_ceo:
        SIG.append_ceo_inbox(args.id, "хвост", tail_ceo)
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
                     + (args.text or "Влить, когда CI зелёный и нет конфликта — сделает merge_rule.") + _after_merge_note(args))
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
            met = W.check_wait_for(spec, W.project_where())
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
    probe_err, probe_upd, probe_log = PROBE.gate(T.read_ticket(path), spec, by_dt if by else None,
                                                 getattr(args, "probe", None), PROJECT_ROOT)  # TK-069 (G-008)
    if probe_err:
        print(f"wait: {probe_err}", file=sys.stderr)
        return 1
    load_err, load_upd, load_log = LOAD.gate(T.read_ticket(path), spec, getattr(args, "probe", None), PROJECT_ROOT)  # TK-070 (G-009)
    if load_err:
        print(f"wait: {load_err}", file=sys.stderr)
        return 1
    try:
        upd = {"status": "waiting", "wait_for": spec, "wait_by": by_dt.isoformat(timespec="seconds") if by else "", **probe_upd, **load_upd}
        if getattr(args, "on_met", None):
            upd["on_met"] = args.on_met.strip()
        T.write_header_updates(path, upd)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 1
    if probe_log:
        T.append_log(path, _caller_role() or "ceo", probe_log)
    if load_log:
        T.append_log(path, _caller_role() or "ceo", load_log)
    if load_upd:
        LOAD.clear_oom(args.id)  # новый замер принят — отметка OOM снята
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
    STOP.write_stop_request(path.stem, args.next or "", text)
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


BRANCH_DELETE_TTL_S = 3600  # срок согласия на удаление ветки: час (читает страж по полю `until`)
BRANCH_CONSENT_FILE = "branch-delete.json"
DEFAULT_BRANCHES = ("main", "master")


def cmd_allow_delete(args) -> int:
    """CEO: согласие владельца на удаление веток на удалённом (`git push --delete`, `:ветка`) — без него страж отказывает всем,
    и CEO тоже. Основание — решение владельца В-N из реестра, занесённое ДО согласия. Согласие — на названные ветки и на час
    (`until` в `dispatcher/branch-delete.json`, читает страж); main/master не удаляются никогда; `--revoke` снимает."""
    role = _caller_role()
    if role and role != "ceo":
        print(f"allow-delete — только CEO; эта сессия — роль {role}. Нужно удалить ветку — `result <ID> blocked --why \"...\"`",
              file=sys.stderr)
        return 1
    names = []
    for raw in args.branches:
        name = raw.strip().removeprefix("refs/heads/")
        if not name or name.startswith(("-", ":", "refs/")) or ".." in name or any(c in name for c in "*?[]{}\\$`'\" \t"):
            print(f"allow-delete: ветка {raw!r} — нужно точное имя (без маски, пробелов и «:»)", file=sys.stderr)
            return 1
        if name in DEFAULT_BRANCHES:
            print(f"allow-delete: {name} — основную ветку удалять нельзя никому", file=sys.stderr)
            return 1
        names.append(name)
    path = CFG.DISPATCHER_DIR / BRANCH_CONSENT_FILE
    now = datetime.now().astimezone()
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        data = data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        data = {}                                        # битый файл не копим: начинаем заново
    data = {b: e for b, e in data.items()
            if isinstance(e, dict) and e.get("until") and _consent_alive(e["until"], now)}   # просроченное убираем
    if args.revoke:
        gone = [n for n in names if data.pop(n, None) is not None]
        _write_consents(path, data)
        print("снято: " + (", ".join(gone) if gone else "ничего (согласия не было)"))
        return 0
    ref = (args.ref or "").strip()
    if not re.fullmatch(r"В-\d+", ref) or not registry_mod.ref_exists(PROJECT_ROOT, ref):
        print(f"allow-delete: --ref {ref or '(пусто)'} — нужно решение владельца В-N, уже занесённое в реестр (`registry.py list`). "
              f"Сначала слово владельца дословно — `registry.py decide`, потом согласие", file=sys.stderr)
        return 1
    until = (now + timedelta(seconds=BRANCH_DELETE_TTL_S)).isoformat(timespec="seconds")
    for n in names:
        data[n] = {"ref": ref, "until": until}
    _write_consents(path, data)
    print(f"удаление на удалённом разрешено до {until} ({ref}): " + ", ".join(names))
    return 0


def _consent_alive(until: str, now) -> bool:
    stamp = T.parse_time(until)
    return stamp is not None and stamp > now


def _write_consents(path: Path, data: dict) -> None:
    P.write_json(path, data)


def cmd_config(args) -> int:
    """CEO: правка `.claude/rpv.json` — ключ из закрытого списка, значение проверено (`rpv_config.KEYS`); правка пишется в журнал,
    Судье заводится проверка, не принятая за 2 ч правка откатывается сама. Судья: `config accept CFG-N`."""
    role = _caller_role()
    if args.action == "accept":
        if role != "judge":
            print(f"config accept — только Судья; эта сессия — {role or 'CEO'}", file=sys.stderr)
            return 1
        entry, err = cfg_mod.accept(PROJECT_ROOT, args.key)
        if err:
            print(f"config accept: {err}", file=sys.stderr)
            return 1
        print(f"{entry['id']}: принято, откат отменён")
        return 0
    if role and role != "ceo":
        print(f"config set — только CEO; эта сессия — роль {role}", file=sys.stderr)
        return 1
    if args.value is None:
        print("config set: нужно значение — `config set <ключ> <значение> --why \"...\"`", file=sys.stderr)
        return 1
    entry, err = cfg_mod.set_value(PROJECT_ROOT, args.key, args.value, args.why or "", (args.ref or "").strip())
    if err:
        print(f"config set: {err}", file=sys.stderr)
        return 1
    try:
        path = T.create_ticket(TICKETS_DIR, owner="judge", title=f"Проверка правки настроек {entry['id']}: {entry['key']}",
                               reviewer="", description=cfg_mod.judge_ticket_text(entry), status="todo")
        cfg_mod.attach_ticket(PROJECT_ROOT, entry["id"], path.stem)
    except ValueError as e:                      # правка записана и откатится сама; Судью не разбудили — говорим
        print(f"config set: тикет Судье не заведён ({e}); откат сам к {entry['deadline']}", file=sys.stderr)
    print(f"{entry['id']}: {entry['key']} записан; без приёмки Судьи вернётся к {entry['deadline']}")
    return 0


def cmd_charter(args) -> int:
    """CEO: перенос уставов из шаблонов плагина (`charter.py`): руками уставы не правятся (страж, TK-007)."""
    return charter_mod.run(args, _caller_role(), PROJECT_ROOT)


def cmd_status(args) -> int:
    state = CFG.load_state()
    rows = []
    for path in T.list_tickets(TICKETS_DIR):
        try:
            tkt = T.read_ticket(path)
        except Exception as e:
            rows.append((path.stem, f"<ошибка разбора: {e}>", "", "", "", "", ""))
            continue
        tid = tkt.id
        spent_col = f"${COST.ticket_cost_spent(state, tid):.2f}"  # учёт, не лимит (В-173)
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
    print(f"потрачено: за сутки ${state.get('daily_cost', {}).get(COST._today(now), 0.0):.2f}, "
          f"за последний час ${COST._rolling_hour_cost(state, now):.2f}")
    return 0


def _handlers() -> dict:
    """Подкоманда → обработчик; собирается при вызове, чтобы тесты могли подменять обработчики на модуле."""
    return {"new": cmd_new, "comment": cmd_comment, "accept": cmd_accept, "result": cmd_result, "start": cmd_start,
            "wait": cmd_wait, "due": cmd_due, "stop": cmd_stop, "allow": cmd_allow, "allow-delete": cmd_allow_delete,
            "config": cmd_config, "charter": cmd_charter, "inbox": cmd_inbox, "status": cmd_status}


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    args = build_parser().parse_args(argv)
    if not CFG.ensure_project(["--project", args.project] if args.project else [], "tickets"):
        return 2                                 # нет проекта — ошибка с подсказкой, тикет не создаётся
    if args.project:
        global TICKETS_DIR, PROJECT_ROOT
        TICKETS_DIR, PROJECT_ROOT = CFG.TICKETS_DIR, CFG.PROJECT_ROOT
    return _handlers()[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
