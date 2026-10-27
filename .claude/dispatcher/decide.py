"""Кого будить по тикету: решение `decide` и закрытие тикета после влива PR — вынесено из dispatch.py (TK-011).
Проверку `wait_for` (`check_wait_for`) передаёт вызывающий — обёртка в dispatch.py подставляет текущую (подмена в тестах)."""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import project as P  # noqa: E402
import ticket as T  # noqa: E402
from signals import CLOSED_BY_MERGE_MARK, MAX_REVIEW_RETURNS, _review_returns, closed_without_review, last_move, _done_marker  # noqa: E402


@dataclass
class Decision:
    role: str
    reason: str
    header_updates: dict = None


ACCEPT_PR = re.compile(r"(?:\[итог:\s*accept\]\s*PR\s*#(\d+)@|ПРИНЯТО PR #(\d+) на голове\s+)([0-9a-f]{4,40})")


def _last_accept(tkt: T.Ticket, pr: int) -> tuple:
    """Последняя приёмка Судьи этого PR в логе: (индекс записи, sha), нет — (-1, "")."""
    for i in range(len(tkt.log) - 1, -1, -1):
        e = tkt.log[i]
        m = ACCEPT_PR.match(e.text.strip())
        if m and T.author_is(e.author, "judge") and int(m.group(1) or m.group(2)) == pr:
            return i, m.group(3)
    return -1, ""


def _ceo_order_after_accept(tkt: T.Ticket, pr: int) -> bool:
    """После последней приёмки PR есть запись ceo/владельца (не само закрытие `[итог: done]`) — заказ, на который автор не ответил."""
    idx, _ = _last_accept(tkt, pr)
    return idx >= 0 and any(any(T.author_is(e.author, a) for a in ("ceo", "owner", "владелец"))
                            and not re.match(r"\[итог:\s*done\]", e.text.strip()) for e in tkt.log[idx + 1:])


def _executor_waits_after_accept(tkt: T.Ticket, idx: int) -> bool:
    """После приёмки (запись idx) `merged:`-ожидание поставил исполнитель (`[итог: wait]` не Судьи): ждёт слияния ради шага после
    него (выкладка, выпуск — alpha TK-258, TK-077), его и будить; ожидание, поставленное самой приёмкой, — Судьи."""
    return any(re.match(r"\[итог:\s*wait\]", e.text.strip()) and not T.author_is(e.author, "judge") for e in tkt.log[idx + 1:])


CLOSED_BY_MERGE = re.compile(r"PR #(\d+) влит, приёмка Судьи (?:на голову )?([0-9a-f]{4,40})")


def _closed_by_merge_before(tkt: T.Ticket, pr: int, sha: str) -> bool:
    """Диспетчер уже закрывал тикет по слиянию этого PR на этой принятой голове (повтор приёмки не новый заказ)."""
    for e in tkt.log:
        m = CLOSED_BY_MERGE.search(e.text) if e.author.lower() == "dispatcher" else None
        if m and int(m.group(1)) == pr and sha.lower().startswith(m.group(2).lower()[:7]):
            return True
    return False


def close_after_merge(path: Path, tkt: T.Ticket, now, state: dict = None) -> "T.Ticket | None":
    """alpha #107 П0: `wait_for: merged:` выполнен, приёмка Судьи стоит на голове влитого PR, заказов после неё нет — закрываем
    сами (done, путь из приёмки), роли не будим: автор писал бы `done` ради блокнота, а Судья принимал бы повторно. Заказ CEO
    04:37: только если ожидание поставил Судья (не исполнитель ради шага после слияния) и PR влит в ветку по умолчанию (не в
    промежуточную, как alpha TK-263 в tk262). Иначе None — автора будит wait_for-met, как раньше. TK-018: закрывали этим же PR
    на этой же голове — это повтор, второй сигнал done CEO не уходит (метка в `state`)."""
    if tkt.status != "waiting" or tkt.next_role:
        return None
    parsed = T.parse_wait_for(tkt.header.get("wait_for", ""))
    if not parsed or parsed[0] != "merged":
        return None
    repo, pr = parsed[1], parsed[2]
    idx, sha = _last_accept(tkt, pr)
    if idx < 0 or _ceo_order_after_accept(tkt, pr) or _executor_waits_after_accept(tkt, idx):
        return None
    import merge_rule
    merged = merge_rule.merged_pr(repo, pr)
    if not merged or not merged["head"].lower().startswith(sha.lower()) or merged["base"] != merged["default"]:
        return None  # не влит, не найден, влита другая голова, чем принятая, или не в ветку по умолчанию — решает автор (wait_for-met)
    proof = next((m.group(1) for e in reversed(tkt.log[:idx + 1])
                  for m in [re.search(r"(?:проверка|результат):\s*(\S+)", e.text)] if m), "")
    repeat = _closed_by_merge_before(tkt, pr, sha)
    with T.ticket_lock(path):
        cur = T.read_ticket(path)
        if cur.status != "waiting" or cur.next_role or len(cur.log) != len(tkt.log):
            return None  # пока читали, тикет двинулся (запись роли/CEO)
        T.append_log(path, "dispatcher", f"[итог: done]{f' результат: {proof}' if proof else ''} — PR #{pr} влит, приёмка Судьи "
                     f"{sha[:7]} есть, нового заказа нет: {CLOSED_BY_MERGE_MARK}." + (" Повтор: этим слиянием тикет уже закрыт." if repeat else ""),
                     now=now)
        T.write_header_updates(path, {"status": "done", "next": "", "wait_for": "", "on_met": ""}, now=now)
    closed = T.read_ticket(path)
    if repeat and state is not None:
        state.setdefault("ceo_done_notified", {})[closed.id] = _done_marker(closed)  # CEO о закрытии уже знает
    return closed


def decide(tkt: T.Ticket, state: dict, now, check_wait_for) -> "Decision | None":
    """Кого будить по тикету. v2 (02.10): @упоминания в тексте больше не будят (аудит: 77 % запусков и 67 %
    денег — запуски по упоминаниям, в том числе «для сведения» и на статусе waiting). Будят: явное поле
    `next:` (пишет `tickets.py comment --next`), `todo` → владелец, `in_progress` → владелец (продолжить),
    `waiting` с выполненным `wait_for` → владелец, `done` с `reviewer` → ревьюер. `waiting` без `wait_for`
    молчит всегда — только явный `next`. `next: ceo` — не запуск роли (см. handle_next_ceo)."""
    if tkt.status == "backlog":
        return None  # перенесено из TASKS.md, ещё не в работе — диспетчер не трогает; см. `tickets.py start`

    # (0) явная передача: `next: <роль>` — один запуск этой роли, поле очищается при запуске (tick)
    nxt = tkt.next_role
    if nxt in P.ROLES:
        return Decision(role=nxt, reason="next", header_updates={"next": ""})

    status = tkt.status
    owner = tkt.owner

    # (а) todo → owner (роль-владелец тикета)
    if status == "todo" and owner in P.ROLES:
        return Decision(role=owner, reason="todo")

    # (а') in_progress без активного запуска → владелец, чтобы продолжить многошаговую задачу (судья
    # 27.09, п.2 «обязательно»: раньше такой тикет замирал после первой сессии — устав ролей обещает
    # продолжение другой сессией, а диспетчер никого не будил). Троттлинг — MIN_GAP_S/MAX_RUNS_PER_TICKET_HOUR
    # в tick(), как у любого решения; уходит через явную смену status (done/waiting/blocked/…).
    if status == "in_progress" and owner in P.ROLES:
        return Decision(role=owner, reason="in_progress-resume")

    # (в) waiting и условие wait_for выполнено → owner
    if status == "waiting" and owner in P.ROLES:
        if check_wait_for(tkt.header.get("wait_for", "")):
            parsed = T.parse_wait_for(tkt.header.get("wait_for", ""))
            if parsed and parsed[0] == "merged" and _ceo_order_after_accept(tkt, parsed[2]):
                return Decision(role=owner, reason="ceo-order-after-accept")  # alpha TK-283: заказ CEO после приёмки без ответа
            return Decision(role=owner, reason="wait_for-met")
        return None

    # (г) done или in_review при заданном reviewer → будит ревьюера (done ставит in_review) — но не когда последняя
    # запись лога уже от самого ревьюера (или dispatcher): это штатный конец состоявшегося ревью, не новый раунд.
    # Раньше проверялось по updated-таймстампу — тот становится новее записи ревьюера, стоит роли проставить
    # status ПОСЛЕ append_log, и (г) будило ревьюера повторно за его же вердикт (судья 27.09, п.3 «обязательно»).
    # in_review (аудит 03.10): ревьюер упал/убит без записи — тикет не висит вечно; повторы режут MAX_IDLE_RUNS и
    # MAX_SAME_STATUS_RUNS.
    if status in ("done", "in_review") and tkt.reviewer in P.ROLES:
        reviewer = tkt.reviewer
        last = last_move(tkt)  # запись merge («PR влит») — не ход: ревьюера не будит (TK-018)
        last_author = last.author if last else ""
        if closed_without_review(tkt):
            return None
        if T.author_is(last_author, reviewer) or last_author.lower() == "dispatcher":
            return None
        if _review_returns(state, tkt.id) >= MAX_REVIEW_RETURNS:
            return None  # пинг-понг: ревьюера не будим, тикет уходит CEO (escalate_review_limit в tick)
        return Decision(role=reviewer, reason="review",
                        header_updates={"status": "in_review"} if status == "done" else None)

    return None
