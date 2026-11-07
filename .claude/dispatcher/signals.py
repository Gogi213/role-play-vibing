"""Сигналы CEO и уведомления диспетчера: строка в ceo-inbox.md, механика в mechanics.log, сводка, возвраты ревью, «готово»,
претензии к wait_for — вынесено из dispatch.py (TK-011, ось 4). Состояние — `project_paths.save_state`; идущие запуски
(`running`) передаёт вызывающий."""
from __future__ import annotations

import re
from datetime import timedelta
from functools import partial
from pathlib import Path

import ceo_wake as CW
import project_paths as CFG
import project as P
import registry as R
import ticket as T
import turn as TURN

# Пинг-понг ревью (аудит-2): сколько раз ревьюер может вернуть работу владельцу (in_review → вернул → снова in_review).
# После MAX_REVIEW_RETURNS возвратов тикет, снова сданный на ревью, ревьюеру не отдаётся (`review_limit_hit`, первая строка
# `decide`): запись dispatcher с причинами возвратов, статус needs_owner — вопрос владельцу, а не тихое закрытие (TK-090, В-36;
# до этого проверка стояла за `next`, который `result pr` ставит сам, и не срабатывала ни разу). 500 — владелец 07.11 (В-64), не
# измерено; до того 10 (В-60), раньше 2 (назначено 13.10).
MAX_REVIEW_RETURNS = 500
HAIKU_ALLOWED_KINDS = {"file-move", "table-format", "publish", "log-compact"}


WAKE_REASON_KINDS = {"watch-goal-idle"}  # в ceo-wake.log к таким видам дописывается причина (TK-086)


def _ceo_file_write(tid: str, kind: str, note: str, now=None, inbox=None, log=None) -> None:
    """`inbox`/`log` — пути, зафиксированные вызывающим: поток побудки пишет туда же, куда писал бы сразу (E-028)."""
    inbox, log = inbox or CFG.CEO_INBOX, log or CFG.CEO_WAKE_LOG
    inbox.parent.mkdir(parents=True, exist_ok=True)
    with open(inbox, "a", encoding="utf-8") as fh:
        fh.write(f"- {T.now_iso(now)} {tid} [{kind}] {note}\n")
    with open(log, "a", encoding="utf-8") as fh:
        reason = f" — {' '.join(note.split())[:150]}" if kind in WAKE_REASON_KINDS else ""
        fh.write(f"{T.now_iso(now)} {tid} {kind}{reason}\n")


# CP5 (критерий 8): механика CEO не будит — строка в mechanics.log. Владельцу и CEO — одна тревога «работа стоит»
# (_stall_alarm): её считают по тому, стоит ли готовая работа, а не по виду поломки
MECH_KINDS = {"blind", "idle-slo", "нет-хода", "цикл-ожиданий", "stop-failed", "parse-error"}


def _mech_log(tid: str, kind: str, note: str, now=None) -> None:
    try:
        CFG.DISPATCHER_DIR.mkdir(parents=True, exist_ok=True)
        with (CFG.DISPATCHER_DIR / "mechanics.log").open("a", encoding="utf-8") as f:
            f.write(f"{T.now_iso(now)} {tid} [{kind}] {' '.join(str(note).split())}\n")
    except OSError:
        pass


def append_ceo_inbox(tid: str, kind: str, note: str, now=None) -> None:
    """Сигнал CEO: строка в ceo-inbox.md + копия в ceo-wake.log. Вид механики (MECH_KINDS) — только в mechanics.log."""
    if kind in MECH_KINDS:
        _mech_log(tid, kind, note, now)
        return
    _ceo_file_write(tid, kind, note, now)
    CW.wake(tid, kind, note, partial(_ceo_file_write, inbox=CFG.CEO_INBOX, log=CFG.CEO_WAKE_LOG), now)  # событие (CW.EVENT_OF) — одна строка в сессию CEO; прочие виды не будят


# --- таблица правил «вид сигнала → будить / сводка» (судья TK-002 п.3, взамен привратника TypeSafe) --
#
# В-85: классифицирует КОД по виду сигнала (`kind` из append_ceo_inbox), не модель. Судья отверг
# привратник на Jev в предложенном виде — асимметрия цены ошибок (пропуск сигнала стоит часы простоя
# команды, лишнее пробуждение — центы); почти все виды здесь структурные, не свободный текст. Ничего не
# отбрасывается: "summary" копится и уходит одной строкой не реже SUMMARY_EVERY_HOURS — не молчание.
# Неизвестный вид (кто-то добавит новый append_ceo_inbox без обновления таблицы) — по умолчанию "wake",
# безопасная сторона асимметрии.
SIGNAL_SUMMARY_KINDS = {"model"}  # уже само по себе диагностика/лог, не требует немедленной реакции
SUMMARY_EVERY_HOURS = 1.0


def classify_signal(kind: str) -> str:
    return "summary" if kind in SIGNAL_SUMMARY_KINDS else "wake"


def flush_pending_summary(state: dict, now) -> None:
    pending = state.get("pending_summary") or []
    if pending:
        append_ceo_inbox("*", "summary", f"{len(pending)} сигнал(ов): " + " | ".join(pending), now)
    state["pending_summary"] = []
    state["last_summary_flush"] = T.now_iso(now)


def route_ceo_signal(tid: str, kind: str, note: str, state: dict, now) -> None:
    """append_ceo_inbox() для "wake"-видов; "summary"-виды копятся и уходят пачкой по SUMMARY_EVERY_HOURS."""
    if classify_signal(kind) != "summary":
        append_ceo_inbox(tid, kind, note, now)
        return
    pending = state.setdefault("pending_summary", [])
    pending.append(f"{T.now_iso(now)} {tid} [{kind}] {note[:150]}")
    last_flush = state.get("last_summary_flush")
    last_flush_dt = T.parse_dt(last_flush) if last_flush else None
    if last_flush_dt is None or (now - last_flush_dt) >= timedelta(hours=SUMMARY_EVERY_HOURS):
        flush_pending_summary(state, now)


def _first_line(text: str, limit: int = 200) -> str:
    return ((text or "").strip().splitlines() or [""])[0][:limit]


def handle_next_ceo(path: Path, tkt: T.Ticket, state: dict, now) -> None:
    """v2 (02.10): `next: ceo` в шапке (`tickets.py comment --next ceo`) — единственный способ позвать CEO
    из записи лога; @ceo в тексте больше ничего не значит. Одна строка CEO, `next` очищается — повторов нет."""
    if tkt.next_role != "ceo":
        return
    last = tkt.log[-1] if tkt.log else None
    who = f"{last.author}: " if last else ""
    line = _first_line(last.text if last else "")
    if TURN.target("next-ceo", tkt) and not review_limit_hit(tkt, state, ignore_next=True):
        # TK-127: ход берёт Судья, CEO мяч не держит — метки передачи нет, строка CEO «к сведению»; на потолке возвратов Судью
        # не зовём (ещё круг пинг-понга) — путь прежний: строка CEO, `next` съеден, потолок срабатывает следом
        append_ceo_inbox(tkt.id, "next-ceo", f"{who}{line} (к сведению: ход отдан Судье)", now)
        TURN.give_turn(path, tkt, "next-ceo", f"{who}передал ход CEO: «{line}».", now)
        return
    # метка — на диск раньше, чем уйдёт `next`: убит диспетчер между ними — метка есть, `next` остался и доставится
    # повторно (строка CEO может задвоиться, но передача не теряется и ожидание роли не станет «без условия»)
    state.setdefault("ceo_handoffs", {})[tkt.id] = T.now_iso(now)  # next съеден тиком при живой роли — её waiting не «без условия»
    CFG.save_state(state)
    append_ceo_inbox(tkt.id, "next-ceo", f"{who}{line}", now)
    T.write_header_updates(path, {"next": ""}, now=now, stamp_updated=False)


def _ceo_handoff_during_run(state: dict, tid: str, info: dict) -> bool:
    """Роль передала CEO (`--next ceo`) во время запуска: тик уже доставил сигнал и очистил поле — ожидание валидно."""
    at = (state.get("ceo_handoffs") or {}).get(tid)  # метка живёт до ответа CEO (_ceo_handoff_pending)
    try:
        started = info["started"]
        started = T.parse_dt(started) if isinstance(started, str) else started
        return bool(at) and T.parse_dt(at) >= started.replace(microsecond=0)
    except (ValueError, TypeError, KeyError):
        return False


AUTO_AUTHORS = ("merge",)  # записи программы о слиянии: не ход роли и не заказ — «последняя запись» их пропускает (TK-018)
REVIEW_AUTO_AUTHORS = AUTO_AUTHORS + ("ci",)  # записи программ (слияние, CI) после сдачи — не ход роли и не заказ
REVIEW_CAP_MARK = "Потолок возвратов:"  # начало записи dispatcher при потолке: по нему `alarms.owner_questions` шлёт вопрос владельцу


def review_limit_hit(tkt: T.Ticket, state: dict, ignore_next: bool = False) -> bool:
    """Потолок возвратов: тикет снова сдан на ревью (done/in_review при reviewer) после MAX_REVIEW_RETURNS возвратов, последний
    ход — владельца (записи `merge`/`ci` не в счёт), а `next` пуст или стоит на ревьюере (так его ставит `result pr`). Ход CEO или
    ревьюера — не повод; чужой `next` (владелец позвал CEO или другую роль) потолок не стирает: сработает, когда его съедят
    (`ignore_next` — спросить заранее, сработает ли потолок после того, как `next` съедят)."""
    if tkt.status not in ("done", "in_review") or tkt.reviewer not in P.ROLES or _review_returns(state, tkt.id) < MAX_REVIEW_RETURNS:
        return False
    if not ignore_next and tkt.next_role not in ("", tkt.reviewer):
        return False
    last = next((e for e in reversed(tkt.log) if e.author.lower() not in REVIEW_AUTO_AUTHORS), None)
    return bool(last) and T.author_is(last.author, tkt.owner)


def _return_reasons(tkt: T.Ticket, returns: int) -> list:
    """Причины последних `returns` возвратов: записи `[итог: return]` ревьюера; нет таких — записи ревьюера."""
    mine = [e for e in tkt.log if T.author_is(e.author, tkt.reviewer)]
    found = [e for e in mine if re.match(r"\[итог:\s*return\]", e.text.strip())] or mine
    return [" ".join(e.text.split())[:300] for e in found[-returns:]]


def escalate_review_limit(path: Path, tkt: T.Ticket, state: dict, now) -> T.Ticket:
    """Пинг-понг ревью: `review_limit_hit` — ревьюера не будим; запись dispatcher с причинами возвратов (начало `REVIEW_CAP_MARK` —
    её читает `alarms.owner_questions`: вопрос уходит владельцу в Telegram), статус needs_owner, строка CEO с причинами (маркер
    статуса ставим сами, иначе `notify_status_for_ceo` напишет вторую и обрежет причины). Счётчик сбрасывается: ответ владельца —
    новая серия. Возвращает свежий тикет."""
    if not review_limit_hit(tkt, state):
        return tkt
    returns = _review_returns(state, tkt.id)
    found = _return_reasons(tkt, returns)
    reasons = "; ".join(f"{i}) {r}" for i, r in enumerate(found, 1)) or "(записей ревьюера нет)"
    T.append_log(path, "dispatcher",
                 f"{REVIEW_CAP_MARK} ревьюер ({tkt.reviewer}) вернул работу {returns} раз подряд, она снова сдана — очередной круг не "
                 f"запускаю. Причины возвратов: {reasons}. Вопрос владельцу: продолжать (и как), сузить заказ или закрыть.", now=now)
    T.write_header_updates(path, {"status": "needs_owner", "next": ""}, now=now)
    state.setdefault("review_returns", {}).pop(tkt.id, None)
    fresh = T.read_ticket(path)
    append_ceo_inbox(tkt.id, "needs_owner", f"потолок возвратов ({returns}): " + "; ".join(r[:100] for r in found), now)
    state.setdefault("ceo_status_notified", {})[tkt.id] = f"needs_owner@{fresh.header.get('updated', '')}"
    CFG.save_state(state)
    return fresh


JUDGE_GRACE = timedelta(minutes=25)  # CEO 04:37: Судье на ответ по blocked исполнителя — 25 мин, потом сигнал CEO (alpha: ~22 из 61 решал CEO)


def blocked_since(tkt: T.Ticket):
    """С какого момента тикет в нынешнем статусе: `updated` шапки, нет — время последней записи лога, нет — None."""
    try:
        if tkt.header.get("updated"):
            return T.parse_dt(tkt.header["updated"])
    except ValueError:
        pass
    return tkt.log[-1].ts if tkt.log else None


def blocked_age_text(tkt: T.Ticket, now) -> str:
    """«26 ч» / «40 мин» — сколько тикет стоит в статусе; пусто, если момент неизвестен."""
    since = blocked_since(tkt)
    if since is None:
        return ""
    minutes = max(0, int((now - since).total_seconds() // 60))
    return f"{minutes // 60} ч" if minutes >= 60 else f"{minutes} мин"


def _awaits_judge(tkt: T.Ticket, now) -> bool:
    """blocked исполнителя (`result blocked|ask-owner`) идёт Судье (routes.py) — CEO о нём не знает, пока Судья не ответил
    своим blocked/ask-owner (как с Надзирателем, 2.0.3; alpha #107 П1, #109 п.2). Судья ответил — последняя запись его.
    Не ответил за JUDGE_GRACE (или момент неизвестен) — тикет больше не «ждёт Судью», сигнал CEO уходит."""
    if tkt.status != "blocked":
        return False
    last = tkt.log[-1] if tkt.log else None
    waits = tkt.next_role == "judge" or bool(
        last and any(T.author_is(last.author, r) for r in ("engineer", "researcher"))
        and re.match(r"\[итог:\s*(blocked|ask-owner)\]", last.text.strip()))
    since = blocked_since(tkt)
    return waits and since is not None and now - since < JUDGE_GRACE


def notify_status_for_ceo(tkt: T.Ticket, state: dict, now) -> None:
    if tkt.status not in ("blocked", "needs_owner") or _awaits_judge(tkt, now):
        return
    notified = state.setdefault("ceo_status_notified", {})
    marker = f"{tkt.status}@{tkt.header.get('updated', '')}"
    if notified.get(tkt.id) == marker:
        return
    reason = (tkt.log[-1].text.splitlines()[0][:200] if tkt.log else "")
    append_ceo_inbox(tkt.id, tkt.status, reason, now)
    notified[tkt.id] = marker


def notify_due_missed(tkt: T.Ticket, state: dict, now) -> None:
    """Срок, названный владельцу (`tickets.py due`), сорван: тикет не закрыт, а срок прошёл — одно событие CEO на срок
    (дедуп по (тикет, срок): перенос срока даёт новое)."""
    due = (tkt.header.get("due") or "").strip()
    if not due or tkt.status in ("done", "stopped"):
        return
    try:
        if now < T.parse_dt(due):
            return
    except ValueError:
        return
    notified = state.setdefault("ceo_due_notified", {})
    if notified.get(tkt.id) == due:
        return
    append_ceo_inbox(tkt.id, "due", f"срок {due} сорван, тикет в статусе {tkt.status}", now)
    notified[tkt.id] = due


def notify_goal_closed(state: dict, now) -> None:
    """Цель закрыта (принята владельцем или снята) — одно событие CEO на цель. Первый обход только запоминает уже закрытые
    цели (старые не будят); дальше — каждая цель, перешедшая из открытых в закрытые."""
    closed = {gid: R.goal_header(p).get("status") for gid, p in R.goals(CFG.PROJECT_ROOT).items()
              if R.goal_header(p).get("status") in ("accepted", "dropped")}
    seen = state.get("goals_closed_seen")
    state["goals_closed_seen"] = sorted(closed)
    if seen is None:
        return
    for gid in sorted(set(closed) - set(seen)):
        how = "принята владельцем" if closed[gid] == "accepted" else "снята владельцем"
        append_ceo_inbox(gid, "goal-done", f"цель {gid} {how}: скажи владельцу итог", now)


def notify_parse_error(tid: str, err_text: str, state: dict, now) -> None:
    """Дедуп по (тикет, текст ошибки) — судья 27.09, п.5 «обязательно»: без дедупа сломанный вручную
    тикет пишет строку в ceo-inbox.md/ceo-wake.log КАЖДЫЙ тик (симуляция: 240/час при POLL_INTERVAL=15с)."""
    notified = state.setdefault("ceo_parse_error_notified", {})
    if notified.get(tid) == err_text:
        return
    append_ceo_inbox(tid, "parse-error", err_text, now)
    notified[tid] = err_text


WAIT_NOTICE_EVERY = timedelta(days=1)    # одна и та же претензия к wait_for тикета — не чаще раза в сутки
WAIT_EMPTY_AFTER = timedelta(minutes=30)  # waiting с пустым wait_for молчит 30 мин (роль как раз правит шапку)


def notify_wait_for_problem(tkt: T.Ticket, state: dict, now, running) -> None:
    """`waiting`, который диспетчер не умеет снять, не молчит (разбор 04.10: роли писали в wait_for что попало — тикет ждал
    вечно, ни ошибки, ни строки): непустой `wait_for` неизвестной формы или ПУСТОЙ дольше 30 мин (от `updated`) → строка
    CEO в ceo-inbox, раз в сутки на тикет. Не трогаем: тикет в запуске, ждущий `next` (уйдёт на этом же тике)."""
    if tkt.status != "waiting" or tkt.id in running or tkt.next_role:
        return
    spec = (tkt.header.get("wait_for") or "").strip()
    if spec:
        if T.parse_wait_for(spec) is not None:
            why = T.file_wait_problem(spec)
            if not why:
                return
            note = f"wait_for не сработает: {why}"
        else:
            note = f"wait_for не понят: {spec[:150]} — допустимо: {T.WAIT_FOR_FORMATS}"
    else:
        try:
            idle = now - T.parse_dt(tkt.header.get("updated", ""))
        except ValueError:
            return
        if idle < WAIT_EMPTY_AFTER:
            return
        note = (f"ждёт, но не сказано чего: waiting при пустом wait_for уже {int(idle.total_seconds() // 60)} мин — "
                f"задать (`tickets.py wait {tkt.id} <форма>`: {T.WAIT_FOR_FORMATS}) или сменить статус")
    notified = state.setdefault("ceo_wait_for_notified", {})
    prev = notified.get(tkt.id) or {}
    if prev.get("spec") == spec and prev.get("at"):
        try:
            if now - T.parse_dt(prev["at"]) < WAIT_NOTICE_EVERY:
                return
        except ValueError:
            pass
    append_ceo_inbox(tkt.id, "wait-for", note, now)
    notified[tkt.id] = {"spec": spec, "at": T.now_iso(now)}


def _review_returns(state: dict, tid: str) -> int:
    return int((state or {}).get("review_returns", {}).get(tid, 0))


NO_REVIEW_MARK = "закрыто без ревью"  # в записи «[итог: done]…» (tickets.py result: CEO или все PR уже приняты и влиты)


CLOSED_BY_MERGE_MARK = "тикет закрыт без запуска ролей"  # в записи «[итог: done]…» диспетчера: PR влит, приёмка Судьи на голове есть (TK-018)


def last_move(tkt: T.Ticket):
    """Последняя запись лога не от программы слияния (`merge`): «PR влит» после закрытия — не новый ход, ревьюера не будит."""
    return next((e for e in reversed(tkt.log) if e.author.lower() not in AUTO_AUTHORS), None)


def closed_without_review(tkt: T.Ticket) -> bool:
    """done закрыт без нового круга ревью (TK-090 а/б): последняя запись — CEO (закрыл сам или оставил комментарий к
    закрытому) либо итог done с пометкой «закрыто без ревью» (CEO) / «закрыт без запуска ролей» (диспетчер после слияния).
    Иначе (г) будило бы Судью за каждую такую запись."""
    last = last_move(tkt)
    if tkt.status != "done" or last is None:
        return False
    return T.author_is(last.author, "ceo") or (last.text.lstrip().startswith("[итог: done]")
                                                and (NO_REVIEW_MARK in last.text or CLOSED_BY_MERGE_MARK in last.text))


def _review_pending(tkt: T.Ticket, state: dict = None) -> bool:
    """done при заданном reviewer, но последняя запись лога не от ревьюера (и не dispatcher) — правило (г)
    ещё запустит ревью: задача не закончена. На пределе возвратов (MAX_REVIEW_RETURNS) ревьюера уже не будят —
    ревью не «ожидается», решает CEO."""
    if tkt.status != "done" or tkt.reviewer not in P.ROLES or closed_without_review(tkt):
        return False
    if state is not None and _review_returns(state, tkt.id) >= MAX_REVIEW_RETURNS:
        return False
    last = last_move(tkt)
    return (last.author.lower() if last else None) not in (tkt.reviewer.lower(), "dispatcher")


def _done_marker(tkt: T.Ticket) -> str:
    return f"done@{tkt.header.get('updated', '')}"


def baseline_done_notified(state: dict) -> None:
    """Первый тик после перехода на v2: уже закрытые тикеты — «известные», иначе CEO получит по строке `done`
    на каждый тикет истории. Ключ `ceo_done_notified` есть → ничего не делаем."""
    if "ceo_done_notified" in state:
        return
    notified = state.setdefault("ceo_done_notified", {})
    for path in T.list_tickets(CFG.TICKETS_DIR):
        try:
            tkt = T.read_ticket(path)
        except Exception:
            continue
        if tkt.status == "done":
            notified[tkt.id] = _done_marker(tkt)


def notify_done(tkt: T.Ticket, state: dict, now) -> None:
    """v2 (02.10): `done` — одна строка CEO, когда работа реально закончена: ревьюера нет (V2-1: механическая задача
    Haiku, тикет Судьи или закрыл CEO) либо ревью уже состоялось. Если ждёт ревью — молчим, строка придёт после
    вердикта. Дедуп по (задача, `updated`)."""
    notified = state.setdefault("ceo_done_notified", {})
    if tkt.status != "done":
        notified.pop(tkt.id, None)  # вышел из done (переоткрыт) — следующее закрытие снова сообщается
        return
    if _review_pending(tkt, state):
        return
    marker = _done_marker(tkt)
    if notified.get(tkt.id) == marker:
        return
    last = last_move(tkt)
    if tkt.id in notified and last and T.author_is(last.author, "ceo") and last.text.lstrip().startswith("[итог: done]"):
        notified[tkt.id] = marker  # CEO повторно закрыл уже сообщённый done (гонка с закрытием по слиянию) — второй строки нет
        return
    title = (tkt.header.get("title") or "")[:80]
    tail = f" — {last.author}: {_first_line(last.text, 120)}" if last else ""
    append_ceo_inbox(tkt.id, "done", f"{title}{tail}", now)
    notified[tkt.id] = marker


def haiku_refused_reason(tkt: T.Ticket) -> str:
    """None — можно запускать на Haiku; иначе причина отказа. Судья TK-002 п.5: (а) белый список видов
    (kind), (г) обход проверки Судьи запрещён — reviewer: judge или owner: researcher не бывают Haiku,
    даже если tickets.py new это пропустил (ручная правка шапки) — вторая защита, уже в диспетчере."""
    if tkt.executor != "haiku":
        return None
    if tkt.kind not in HAIKU_ALLOWED_KINDS:
        return f"executor: haiku требует kind из {sorted(HAIKU_ALLOWED_KINDS)}, у тикета kind={tkt.kind or '(пусто)'}"
    if tkt.reviewer.lower() == "judge":
        return "executor: haiku нельзя вместе с reviewer: judge — числа/вердикты не на Haiku"
    if tkt.owner == "researcher":
        return "executor: haiku нельзя для owner: researcher — исследовательский результат не на Haiku"
    return None


# --- решение --------------------------------------------------------------------------------

# Приоритет кандидатов на запуск (tick): явный `next` — раньше статусных правил; продолжение in_progress —
# последним. Внутри одного приоритета — кто дольше не запускался (иначе тикет с in_progress-resume
# вытеснял бы остальные: на роль теперь один запуск).




