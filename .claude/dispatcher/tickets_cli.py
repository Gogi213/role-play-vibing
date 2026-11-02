"""Разбор командной строки `tickets.py`: подкоманды и флаги в одном месте (обработчики — в `tickets.py`)."""
from __future__ import annotations

import argparse

import project as P
import signals as SIG
import routes as routes_mod
import ticket as T


def _add_ticket_parsers(sub) -> None:
    """Заведение тикета, запись в лог, приёмка."""
    p_new = sub.add_parser("new")
    p_new.add_argument("--owner", required=True, choices=P.ROLES)
    p_new.add_argument("--title", required=True)
    p_new.add_argument("--reviewer", choices=P.ROLES,
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
    p_new.add_argument("--kind", choices=sorted(SIG.HAIKU_ALLOWED_KINDS), default=None,
                        help="вид задачи для --executor haiku")

    p_comment = sub.add_parser("comment")
    p_comment.add_argument("id")
    p_comment.add_argument("--author", required=True)
    p_comment.add_argument("--text", required=True)
    p_comment.add_argument("--next", choices=[*P.ROLES, "ceo"], default=None,
                            help="разбудить эту роль один раз (ceo — только blocked/нужно решение владельца)")

    p_accept = sub.add_parser("accept", help="Судья: принято, PR N на голове SHA (вливает merge_rule по правилу)")
    p_accept.add_argument("id")
    p_accept.add_argument("--pr", type=int, required=True)
    p_accept.add_argument("--sha", required=True)
    p_accept.add_argument("--text", default="")
    p_accept.add_argument("--repo", default=None, help="<владелец/репо> PR (иначе ci_repo в .claude/rpv.json)")
    p_accept.add_argument("--decisions", default=None, help="решения владельца, с которыми сверено: В-N[,В-M] или - (связанных нет)")
    p_accept.add_argument("--numbers", default=None, help="файл с таблицей «| число | файл:строка |» или - (чисел нет)")
    p_accept.add_argument("--tail", default=None, help="хвосты: - (нет) | TK-M[,TK-N] | ceo:\"что сделать CEO\"")
    p_accept.add_argument("--live", default=None, help="договор с [вживую]: файл живой проверки (что увидит владелец)")
    p_accept.add_argument("--critic", default=None, help="critic_before_accept: отчёт критика — файл в docs/reviews/")
    p_accept.add_argument("--critic-skip", dest="critic_skip", default=None, help="critic_before_accept: обход критика, причина ≥ 10 знаков")
    p_accept.add_argument("--after-merge", dest="after_merge", default=None,
                          help="шаг после слияния (тег, выкладка, выпуск): после влива диспетчер будит автора на него, а не закрывает тикет")


def _add_result_parser(sub) -> None:
    """Итог шага роли."""
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
    p_result.add_argument("--probe", default=None, help="wait: проба до долгого расчёта — запись или файл (с require_probe, срок дальше порога)")
    p_result.add_argument("--repo", default=None, help="accept по PR: <владелец/репо> (иначе ci_repo в .claude/rpv.json)")
    p_result.add_argument("--decisions", default=None,
                          help="accept: решения владельца, с которыми сверена сдача: В-N[,В-M] или - (связанных нет)")
    p_result.add_argument("--numbers", default=None,
                          help="accept: файл с таблицей ключевых чисел «| число | файл:строка |» или - (чисел нет; код проверяет по --path)")
    p_result.add_argument("--tail", default=None,
                          help="accept: хвосты — - (нет; сверяется со сдачей исполнителя) | TK-M[,TK-N] | ceo:\"что сделать CEO\"")
    p_result.add_argument("--live", default=None,
                          help="accept: договор с [вживую] — файл живой проверки на настоящих данных (что увидит владелец)")
    p_result.add_argument("--critic", default=None, help="accept (critic_before_accept): отчёт критика — файл в docs/reviews/")
    p_result.add_argument("--critic-skip", dest="critic_skip", default=None,
                          help="accept (critic_before_accept): обход критика, причина ≥ 10 знаков (считается в отчёт)")
    p_result.add_argument("--after-merge", dest="after_merge", default=None,
                          help="accept по PR: шаг после слияния (тег, выкладка, выпуск) — после влива будится автор, тикет не закрывается")


def _add_control_parsers(sub) -> None:
    """Ход тикета (start, wait, due, stop), согласия владельца, настройки, входящие CEO, статус."""
    p_start = sub.add_parser("start")
    p_start.add_argument("id")

    p_wait = sub.add_parser("wait", help="status: waiting + wait_for (форма проверяется)")
    p_wait.add_argument("--allow-met", dest="allow_met", action="store_true",
                        help="ждать условие, уже выполненное при постановке (по умолчанию отказ)")
    p_wait.add_argument("id")
    p_wait.add_argument("spec", help=T.WAIT_FOR_FORMATS)
    p_wait.add_argument("--by", default=None, help="срок ожидания ЧЧ:ММ|ISO (GMT+4), обязателен для host:/file:/job:")
    p_wait.add_argument("--probe", default=None, help="проба до долгого расчёта — запись или файл (с require_probe, срок дальше порога)")
    p_wait.add_argument("--check", action="store_true",
                        help="только проверка (форма, нагрузка машины, пик/N после OOM) до запуска расчёта; тикет не меняется, --by не нужен")
    p_wait.add_argument("--on-met", default=None,
                        help="команда по закрытии wait_for вместо пробуждения LLM: `python|bash <скрипт под tools/ или .claude/, в git> [арг]`")

    p_oom = sub.add_parser("oom-clear", help="Судья/CEO: снять отметку OOM тикета без нового замера")
    p_oom.add_argument("id")
    p_oom.add_argument("--why", required=True, help="причина снятия (≥ 10 знаков), идёт в лог тикета")

    p_due = sub.add_parser("due", help="только CEO: срок, названный владельцу (первый остаётся в due_first)")
    p_due.add_argument("id")
    p_due.add_argument("--by", required=True, help="ЧЧ:ММ или ISO (GMT+4)")
    p_due.add_argument("--said", default=None, help="как срок сказан владельцу (дословно)")

    p_stop = sub.add_parser("stop", help="только CEO: остановить запущенную роль тикета и дать новую постановку")
    p_stop.add_argument("id")
    p_stop.add_argument("--text", required=True, help="новая постановка — запись CEO в лог тикета")
    p_stop.add_argument("--next", choices=P.ROLES, default=None,
                        help="роль, которая стартует сразу (тикет станет todo); без --next — status: stopped, не будить")

    p_allow = sub.add_parser("allow", help="только CEO: согласие владельца на публикацию по тикету (или --revoke)")
    p_allow.add_argument("id")
    p_allow.add_argument("what", nargs="?", choices=["publish"], help="что разрешено: публикации (выпуск, слияние PR, пуш в main, пакеты)")
    p_allow.add_argument("--ref", default="", help="решение владельца В-N из реестра (`registry.py decide`)")
    p_allow.add_argument("--revoke", action="store_true", help="снять согласие")
    p_alld = sub.add_parser("allow-delete", help="только CEO: согласие владельца на удаление веток на удалённом (на час; main/master — никогда)")
    p_alld.add_argument("branches", nargs="+", help="точные имена веток")
    p_alld.add_argument("--ref", default="", help="решение владельца В-N из реестра (`registry.py decide`)")
    p_alld.add_argument("--revoke", action="store_true", help="снять согласие")

    p_cfg = sub.add_parser("config", help="CEO: set — правка .claude/rpv.json по закрытому списку ключей (Судья проверяет, "
                                          "через 2 ч без приёмки откат); Судья: accept CFG-N")
    p_cfg.add_argument("action", choices=["set", "accept"])
    p_cfg.add_argument("key", help="set: ключ rpv.json; accept: номер правки CFG-N")
    p_cfg.add_argument("value", nargs="?", default=None, help="set: значение (JSON или строка)")
    p_cfg.add_argument("--why", default="", help="зачем правка (Судья сверяет со словом владельца)")
    p_cfg.add_argument("--ref", default="", help="решение владельца В-N из реестра: цитата попадёт в журнал и отчёт")

    p_char = sub.add_parser("charter", help="CEO: sync — перенос уставов из templates/roles/ плагина в .claude/roles/ как есть "
                                            "(строки таблицы зон README остаются проектными); addon <роль|README> — добавка проекта "
                                            ".claude/roles/project/<роль>.md (синк её не трогает); обе — только с --ref В-N")
    p_char.add_argument("action", choices=["sync", "addon"])
    p_char.add_argument("name", nargs="?", default="", help="addon: роль (ceo, engineer, judge, researcher) или README")
    p_char.add_argument("--text", default="", help="addon: текст добавки (дописывается в конец файла)")
    p_char.add_argument("--replace", action="store_true", help="addon: заменить файл добавки целиком")
    p_char.add_argument("--file", action="append", default=None, metavar="ИМЯ.md",
                        help="только этот файл шаблонов (можно несколько раз); без флага — все")
    p_char.add_argument("--why", default="", help="зачем перенос (по умолчанию — версия плагина)")
    p_char.add_argument("--ref", default="", help="решение владельца В-N из реестра: цитата попадёт в журнал и отчёт")

    p_inbox = sub.add_parser("inbox", help="CEO: новые строки ceo-inbox.md с прошлого чтения")
    p_inbox.add_argument("--peek", action="store_true", help="только показать, не считать прочитанным")

    sub.add_parser("status")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tickets.py")
    p.add_argument("--project", default=None,
                   help="корень проекта (иначе RPV_PROJECT, CLAUDE_PROJECT_DIR, ближайший каталог вверх с .claude/roles); "
                        "перед подкомандой")
    sub = p.add_subparsers(dest="cmd", required=True)
    _add_ticket_parsers(sub)
    _add_result_parser(sub)
    _add_control_parsers(sub)
    return p
