"""CLI для тикетов диспетчера: `new`, `comment`, `start`, `wait`, `status`. Только stdlib.

Запускается из папки плагина; проект — `--project <путь>` (перед подкомандой), иначе `RPV_PROJECT` /
`CLAUDE_PROJECT_DIR`, иначе ближайший каталог вверх от текущего с `.claude/roles`; не нашли — ошибка с подсказкой,
каталоги не создаются. Тикеты — `<проект>/.claude/tickets/`. Ниже `tickets.py` — это
`python <плагин>/.claude/dispatcher/tickets.py [--project <проект>]`.

    tickets.py new --owner researcher --title "..." [--desc "..."]  # ревьюера нет (v2)
    tickets.py new --owner engineer --title "..." --reviewer judge  # Судья — только явно
    tickets.py new --owner engineer --title "..." --effort medium   # low|medium|high|xhigh
    tickets.py new --owner researcher --title "..." --backlog   # перенос из TASKS.md
    tickets.py new --owner engineer --title "..." --executor haiku --kind file-move
        # белый список kind; --reviewer judge и owner:researcher с haiku — отказ
    tickets.py comment TK-001 --author researcher --text "..." [--next judge]
    tickets.py accept TK-001 --pr 7 --sha <голова>              # только Судья: принято на этой голове → вливает merge_rule
    tickets.py start TK-001                                     # backlog|stopped → todo
    tickets.py wait TK-001 host:calc:<путь>/<job>.json [--on-met "python tools/x.py арг"]  # status: waiting + wait_for
    tickets.py stop TK-001 --text "..." [--next engineer]       # только CEO: снять роль
    tickets.py status                                           # потрачено по задачам

`--next researcher|engineer|judge|ceo` — единственный способ разбудить другую роль (или CEO) записью лога:
пишет `next: <роль>` в шапку, диспетчер запускает роль ОДИН раз и очищает поле. @упоминания в тексте никого
не будят. После записи лог больше 20 КБ ужимается: всё, кроме последних 8 записей, — в `archive/<ID>-log.md`.

`stop <ID> --text "<новая постановка>" [--next <роль>]` — только CEO (запись в лог — от `ceo`; из сессии роли — отказ): заявка
диспетчеру снять запущенную роль всем деревом процессов и дать новую постановку (подробно — README диспетчера, «Остановка
роли»). С `--next` тикет станет `todo` + `next: <роль>` и роль стартует сразу; без — `status: stopped`: диспетчер не будит,
пока CEO не переведёт в `todo` (`start`).

Лимитов денег на тикет нет (В-173, 03.10): `status` показывает только потрачено (учёт из state.json).
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dispatch as D  # noqa: E402
import ticket as T  # noqa: E402

# Проект и тикеты берутся у диспетчера (D.configure_project при импорте — по --project/RPV_PROJECT/CLAUDE_PROJECT_DIR/поиску вверх);
# в main() флаг --project переключает их ещё раз. Тесты подменяют TICKETS_DIR прямо на модуле.
TICKETS_DIR = D.TICKETS_DIR
PROJECT_ROOT = D.PROJECT_ROOT


def bus_emit(tid: str, kind: str, payload: dict) -> None:
    """Событие на шину (TK-045): шина не настроена или недоступна — команда не ломается (busclient.post не бросает)."""
    if not D._bus_configured():
        return
    import busclient
    busclient.post(f"задача.{tid}.{kind}", payload, timeout=3)


def _ceo_fallback_unread(peek: bool, all_lines: bool = False) -> list[str]:
    """Строки файла ceo-inbox.md новее отметки `.ceo-inbox-fallback-seen`: при шине — только помеченные «[запасной путь]»,
    без шины (all_lines) — все. Отметку двигает чтение без --peek."""
    inbox, seen_f = D.CEO_INBOX, D.CEO_INBOX.parent / ".ceo-inbox-fallback-seen"
    try:
        lines = inbox.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    try:
        seen = int(seen_f.read_text(encoding="utf-8").strip() or 0)
    except (OSError, ValueError):
        seen = 0
    if seen > len(lines):
        seen = 0
    new = [ln for ln in lines[seen:] if all_lines or "[запасной путь]" in ln]
    if not peek and len(lines) != seen:
        seen_f.write_text(str(len(lines)), encoding="utf-8")
    return new


def cmd_inbox(args) -> int:
    """Очередь CEO (В-192): читает `ceo` пачкой, срочное (prio=urgent) первым, подтверждает; запасной файл — отдельно.
    Шина не настроена — печатает новые строки ceo-inbox.md."""
    if not D._bus_configured():
        new = _ceo_fallback_unread(args.peek, all_lines=True)
        for ln in new:
            print(ln)
        if not new:
            print("очередь CEO пуста (шина не настроена; сигналы — в ceo-inbox.md)")
        return 0
    import busclient
    events, down = [], None
    try:
        while True:
            batch = busclient.request("/q/ceo?after=0&wait=0", timeout=5)["events"]
            fresh = [e for e in batch if e["seq"] not in {x["seq"] for x in events}]
            if not fresh:
                break
            events += fresh
            if args.peek:
                break
            if not busclient.request("/ack", {"recipient": "ceo", "seqs": [e["seq"] for e in fresh]}, timeout=5).get("acked"):
                break
    except Exception as e:
        down = f"{type(e).__name__}: {e}"
    events.sort(key=lambda e: ((e.get("payload") or {}).get("prio") != "urgent", e["seq"]))
    for e in events:
        pl = e.get("payload") or {}
        tid = e["addr"].split(".")[1] if e["addr"].startswith("задача.") else e["addr"]
        print(f"[{'СРОЧНО' if pl.get('prio') == 'urgent' else 'обычное'}] #{e['seq']} {tid} {pl.get('kind', e['addr'])}: "
              f"{pl.get('note', pl) if pl.get('note') is not None else pl}")
    fb = _ceo_fallback_unread(args.peek)
    for ln in fb:
        print(f"[запасной путь] {ln}")
    if down:
        print(f"шина недоступна ({down}): очередь не прочитана; сигналы за это время — в файле ceo-inbox.md (строки "
              f"«[запасной путь]» выше)")
        return 1
    if not events and not fb:
        print("очередь CEO пуста")
    return 0


def cmd_new(args) -> int:
    # v2 (02.10): ревьюера по умолчанию НЕТ — Судья только по явному `--reviewer judge` (исследования с
    # выводом и необратимое); `--no-reviewer` принимается как no-op (совместимость со старыми командами)
    reviewer = args.reviewer

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

    try:
        path = T.create_ticket(TICKETS_DIR, owner=args.owner, title=args.title, reviewer=reviewer,
                                description=args.desc or "", wait_for=args.wait_for or "",
                                status="backlog" if args.backlog else "todo",
                                executor=args.executor, kind=args.kind, effort=args.effort)
    except ValueError as e:  # неизвестная форма wait_for — файл не создан
        print(e, file=sys.stderr)
        return 1
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
        moved = T.compact_log(path)
    payload = {"author": args.author, "next": nxt or ""}
    if args.text.lstrip().upper().startswith("ВОПРОС ВЛАДЕЛЬЦУ"):
        kind = "вопрос_владельцу"
        payload.update(kind="owner-question", note=D._first_line(args.text), prio="urgent")
    else:
        # next: ceo — сигнал CEO даёт диспетчер (handle_next_ceo, с дедупом); второй путь здесь был бы дублем
        kind = "статус" if not nxt else "сдано"
    bus_emit(args.id, kind, payload)
    print(f"дописано в {path}" + (f"; next: {nxt}" if nxt else "")
          + (f"; next проставлен автоматически (запись Судьи без --next → владелец тикета: {nxt})" if auto_next else "")
          + (f"; в архив перенесено записей: {moved}" if moved else ""))
    return 0


def _caller_role() -> str:
    """Роль вызывающей сессии: диспетчер ставит `RPV_ROLE` (и `ALPHA_ROLE`) запускам ролей; у CEO и владельца её нет."""
    return (D.P.env("ROLE", "") or "").strip().lower()


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
    with T.ticket_lock(path):
        T.append_log(path, "judge", f"ПРИНЯТО PR #{args.pr} на голове {args.sha[:7]}. "
                     + (args.text or "Влить, когда CI зелёный и нет конфликта — сделает merge_rule."))
        T.write_header_updates(path, {"accepted": f"{args.pr}@{args.sha}"}, stamp_updated=False)
    bus_emit(args.id, "статус", {"accepted": f"{args.pr}@{args.sha}"})
    print(f"{args.id}: принято PR #{args.pr}@{args.sha[:7]}")
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
    parsed = T.parse_wait_for(spec)
    if parsed and parsed[0] == "ticket":
        cycle = T.wait_cycle(TICKETS_DIR, args.id, parsed[1])
        if cycle:
            print(f"wait: цикл ожиданий {' -> '.join(cycle)} — каждый ждёт следующего, никто не пойдёт. Ждите "
                  f"результат (file:/host:…), а не тикет, или разорвите цепочку.", file=sys.stderr)
            return 1
    try:
        upd = {"status": "waiting", "wait_for": spec}
        if getattr(args, "on_met", None):
            upd["on_met"] = args.on_met.strip()
        T.write_header_updates(path, upd)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 1
    bus_emit(args.id, "статус", {"status": "waiting", "wait_for": spec})
    print(f"{args.id}: status: waiting, wait_for: {spec}")
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
                        help="ревьюер; по умолчанию его нет (Судья — только явно: --reviewer judge)")
    p_new.add_argument("--no-reviewer", action="store_true",
                        help="no-op (v2: ревьюера по умолчанию нет); оставлено для совместимости")
    p_new.add_argument("--effort", choices=list(T.VALID_EFFORTS), default=None,
                        help="усилие запусков по этому тикету; без поля — исследователь/инженер high, Судья xhigh")
    p_new.add_argument("--desc", default="")
    p_new.add_argument("--wait-for", dest="wait_for", default="")
    p_new.add_argument("--backlog", action="store_true",
                        help="создать сразу в backlog (перенос из TASKS.md) — диспетчер её не трогает до `start`")
    p_new.add_argument("--executor", choices=["haiku"], default=None,
                        help="claude-haiku-4-5 для чисто механических задач — требует --kind")
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
    p_accept.set_defaults(func=cmd_accept)

    p_start = sub.add_parser("start")
    p_start.add_argument("id")
    p_start.set_defaults(func=cmd_start)

    p_wait = sub.add_parser("wait", help="status: waiting + wait_for (форма проверяется)")
    p_wait.add_argument("id")
    p_wait.add_argument("spec", help=T.WAIT_FOR_FORMATS)
    p_wait.add_argument("--on-met", default=None,
                        help="команда по закрытии wait_for вместо пробуждения LLM: `python|bash <скрипт под tools/ или .claude/, в git> [арг]`")
    p_wait.set_defaults(func=cmd_wait)

    p_stop = sub.add_parser("stop", help="только CEO: остановить запущенную роль тикета и дать новую постановку")
    p_stop.add_argument("id")
    p_stop.add_argument("--text", required=True, help="новая постановка — запись CEO в лог тикета")
    p_stop.add_argument("--next", choices=["researcher", "engineer", "judge"], default=None,
                        help="роль, которая стартует сразу (тикет станет todo); без --next — status: stopped, не будить")
    p_stop.set_defaults(func=cmd_stop)

    p_inbox = sub.add_parser("inbox", help="CEO: прочитать и подтвердить очередь «ceo» шины (срочное первым)")
    p_inbox.add_argument("--peek", action="store_true", help="только показать, без ack")
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
