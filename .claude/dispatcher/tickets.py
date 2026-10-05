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
    with T.ticket_lock(path):  # запись, `next` и сжатие лога — одним куском (диспетчер правит шапку тем же замком)
        T.append_log(path, args.author, args.text)
        if args.next:
            # v2: единственный будильник другой роли/CEO; `updated` не двигаем (маркеры уведомлений CEO по нему)
            T.write_header_updates(path, {"next": args.next}, stamp_updated=False)
        moved = T.compact_log(path)
    print(f"дописано в {path}" + (f"; next: {args.next}" if args.next else "")
          + (f"; в архив перенесено записей: {moved}" if moved else ""))
    return 0


def _caller_role() -> str:
    """Роль вызывающей сессии: диспетчер ставит `RPV_ROLE` (и `ALPHA_ROLE`) запускам ролей; у CEO и владельца её нет."""
    return (D.P.env("ROLE", "") or "").strip().lower()


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
    try:
        upd = {"status": "waiting", "wait_for": spec}
        if getattr(args, "on_met", None):
            upd["on_met"] = args.on_met.strip()
        T.write_header_updates(path, upd)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 1
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
