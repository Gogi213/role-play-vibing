"""Диспетчер задач (по образцу Paperclip) — вместо постоянных чатов ролей.

Цикл раз в `POLL_INTERVAL` секунд читает `.claude/tickets/*.md` и решает, кого будить:
роль-исполнителя (`claude -p ... --resume <session_id>`) или CEO (строка в `ceo-inbox.md`).
Только stdlib. Подробности формата — `ticket.py`, правила — `README.md`.

Запускается прямо из папки плагина; проект — `--project <путь>`, иначе `RPV_PROJECT` / `CLAUDE_PROJECT_DIR`,
иначе ближайший каталог вверх от текущего с `.claude/roles` (см. `project.py`); не нашли — ошибка с подсказкой,
каталоги не создаются. Состояние — в `<проект>/.claude/dispatcher/`.

Тест: `python -m unittest .claude/dispatcher/test_dispatch.py` (или из каталога — см. README).
"""
from __future__ import annotations

import atexit
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import project as P  # noqa: E402
import config as CFG  # noqa: E402
import hide  # noqa: E402
import ticket as T  # noqa: E402
import transcript  # noqa: E402
import worktree_hygiene as WH  # noqa: E402
import downtime  # noqa: E402
import notify  # noqa: E402
import on_met as ON_MET  # noqa: E402
import waits as W  # noqa: E402
import alarms as AL  # noqa: E402
import signals as SIG  # noqa: E402
import routes as ROUTES  # noqa: E402
import report as REPORT  # noqa: E402
from role_args import (  # noqa: E402,F401 — запуск роли (усилие, MCP, промпт) вынесен в role_args.py; имена для launch_run и тестов
    ROLE_EFFORT, FIXED_EFFORT_ROLES, PROMPT_TEMPLATE, _read_mcp_servers, role_mcp_args, plugin_dir_args, effort_for, _py,
    tickets_cli)
import role_args as RA  # noqa: E402
from limits import (  # noqa: E402,F401 — лимит сессии и ошибки API (распознавание) вынесены в limits.py
    _limit_hit, API_PAUSE, API_AUTH_PAUSE, MAX_API_ERRORS, _api_error, _auth_error, _limit_reset_at, _limit_paused,
    LIMIT_LABEL_SLACK)

# --- конфигурация (константы — тесты подменяют их прямо на модуле) ------------------------

CODE_DIR = Path(__file__).resolve().parent  # где лежит сам диспетчер (папка плагина) — к проекту отношения не имеет

CLAUDE_BIN = os.environ.get("CLAUDE_BIN") or shutil.which("claude") or "claude"  # имя из PATH, без зашитых путей
POLL_INTERVAL = 15.0
# v2 (02.10, аудит ролевой системы): всего параллельно ≤ 3 запусков и по умолчанию не больше ОДНОГО запуска на роль
# (по всем тикетам сразу, см. _role_busy); таймаут запуска 20 мин (было 40 — фоновые помощники в `-p` висели
# до убийства, а цена убитого запуска в учёте — $0). Проект меняет пределы в `.claude/rpv.json`: `max_parallel`,
# `role_parallel` ({"engineer": 3}); на один тикет — по-прежнему один запуск роли.
MAX_PARALLEL = 3


def _parse_role_parallel(spec) -> dict:
    """`role_parallel` из `.claude/rpv.json` — {"engineer": 3, "researcher": 1} → {роль: предел запусков}. Роль без
    записи — предел 1 (см. _role_busy); пара не вида «роль: целое ≥ 1» пропускается с предупреждением в stderr."""
    if not isinstance(spec, dict):
        print("[dispatch] rpv.json role_parallel — не объект {роль: предел}, пропущено", file=sys.stderr)
        return {}
    out = {}
    for role, num in spec.items():
        if str(role).strip() and isinstance(num, int) and not isinstance(num, bool) and num >= 1:
            out[str(role).strip()] = num
        else:
            print(f"[dispatch] rpv.json role_parallel: {role!r}: {num!r} пропущено (нужно целое ≥ 1), предел 1",
                  file=sys.stderr)
    return out


ROLE_PARALLEL = {}  # пределы по ролям без rpv.json; тесты подменяют на модуле


def max_parallel() -> int:
    """Всего запусков ролей одновременно: `max_parallel` из `.claude/rpv.json`, иначе MAX_PARALLEL."""
    v = P.setting("max_parallel")
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 1 else MAX_PARALLEL


def max_runs_per_ticket_hour() -> int:
    """Запусков одного тикета за час: `max_runs_per_ticket_hour` из `.claude/rpv.json`, иначе MAX_RUNS_PER_TICKET_HOUR."""
    v = P.setting("max_runs_per_ticket_hour")
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 1 else MAX_RUNS_PER_TICKET_HOUR


def role_parallel() -> dict:
    """Пределы запусков по ролям: `role_parallel` из `.claude/rpv.json`, иначе ROLE_PARALLEL."""
    v = P.setting("role_parallel")
    return ROLE_PARALLEL if v is None else _parse_role_parallel(v)


RUN_TIMEOUT = 20 * 60.0

# Защита от петли (владелец 27.09, v1.1). MAX_RUNS_PER_TICKET_HOUR/MIN_GAP_S — троттлинг решений (а)-(г):
# тикет просто пропускается этот тик, без ceo-inbox (не ошибка, а пауза); ретраи правила (д) их не считают —
# они и так ограничены одной попыткой. Денежных ограничений НЕТ вовсе: В-149 снял часовой и суточный лимит,
# В-173 (03.10) — на тикет, владелец 03.10 («бюджет до конца убирай») — и потолок запуска; траты только считаются
# (runs.log, state.json).
MAX_RUNS_PER_TICKET_HOUR = 12  # умолчание (слово владельца 15.10; было 6 — упёрлось в простой 50 мин); проект меняет `max_runs_per_ticket_hour` в `.claude/rpv.json` (целое ≥ 1)
MIN_GAP_S = 60.0
# Тормоза цикла (аудит 03.10) — по ЧИСЛУ запусков подряд на (тикет, роль), не по деньгам. Числа НАЗНАЧЕНЫ CEO 03.10,
# не измерены: MAX_SAME_STATUS_RUNS (12) — роль пишет запись, а статус (in_progress; waiting при выполненном wait_for) не
# меняется → на MAX тикет blocked + строка CEO; MAX_IDLE_RUNS (2) — запуски без записи и без смены статуса (холостой ход) → blocked (первый
# холостой — обычный один повтор, см. _finish_role_part).
MAX_SAME_STATUS_RUNS = 12
MAX_IDLE_RUNS = 2
# --- сигналы CEO и уведомления — signals.py; здесь обёртки подставляют идущие запуски --------------------------

from signals import (  # noqa: E402,F401 — сигналы CEO вынесены в signals.py; имена для вызовов из dispatch и tickets
    _ceo_file_write, MECH_KINDS, _mech_log, append_ceo_inbox, SIGNAL_SUMMARY_KINDS, SUMMARY_EVERY_HOURS,
    classify_signal, flush_pending_summary, route_ceo_signal, _first_line, handle_next_ceo, _ceo_handoff_during_run,
    escalate_review_limit, JUDGE_GRACE, blocked_since, blocked_age_text, _awaits_judge, notify_status_for_ceo,
    notify_parse_error, WAIT_NOTICE_EVERY, WAIT_EMPTY_AFTER, _review_returns, NO_REVIEW_MARK, closed_without_review,
    _review_pending, _done_marker, baseline_done_notified, notify_done, haiku_refused_reason, MAX_REVIEW_RETURNS,
    HAIKU_ALLOWED_KINDS)


def notify_wait_for_problem(tkt: T.Ticket, state: dict, now) -> None:
    SIG.notify_wait_for_problem(tkt, state, now, RUNNING)
# Остановка роли CEO (`tickets.py stop`): после снятия дерева процессов ждём смерти процесса не дольше STOP_VERIFY_S
# секунд (число НАЗНАЧЕНО, не измерено: taskkill/killpg возвращаются сразу, 10 с — запас на очередь ОС); не умер —
# строка CEO `stop-failed`, заявка остаётся до следующего тика.
STOP_VERIFY_S = 10.0
STOP_NOTE = ("Прошлый запуск оборван CEO — проверь git status и недописанные правки, начни с новой постановки "
             "(последняя запись CEO в логе тикета).")

# Модель и перерасход (владелец 27.09, v1.2 — пилот Судьи на умолчаниях CLI стоил $6,8 на Fable 5.1
# xhigh): модель и усилие теперь ВСЕГДА явно в команде запуска, не полагаемся на умолчание CLI.
# В-153 (02.10): все роли — Sonnet 5.5.
CLAUDE_MODEL = "claude-sonnet-5-5"


# В-153 уточнение (02.10, v1.6.1): на Sonnet 5.5 — только те, кого рационально; Судья (проверяет всех)
# остаётся на Opus 5.5 (усилие — ROLE_EFFORT: с 14.10 high). Роль вне словаря и executor haiku — см. launch_run.
ROLE_MODEL = {"judge": "claude-opus-5-5", "engineer": CLAUDE_MODEL, "researcher": CLAUDE_MODEL}


def model_family(model_id: str) -> str:
    """Семейство из ID модели: opus/sonnet/haiku (подстрока); иначе — сам ID в нижнем регистре."""
    low = str(model_id or "").lower()
    for fam in ("opus", "sonnet", "haiku"):
        if fam in low:
            return fam
    return low


def _expected_model_family(info: dict) -> str:
    """Ожидаемое семейство в modelUsage запуска: executor haiku → «haiku»; иначе — семейство модели
    РОЛИ этого запуска (ROLE_MODEL, v1.6.1: Судья — opus, остальные — CLAUDE_MODEL), не общего CLAUDE_MODEL."""
    if info.get("executor") == "haiku":
        return "haiku"
    return model_family(ROLE_MODEL.get(info.get("role"), CLAUDE_MODEL))


# executor: haiku (судья TK-002 п.5) — механические задачи только: белый список видов (--kind при
# tickets.py new), приёмка результата — кодом (в конкретных скриптах-проверках по виду, не здесь).
# Обход Судьи запрещён (условие г): reviewer: judge или owner: researcher — не Haiku, tickets.py new
# отказывает раньше, чем тикет вообще появится; здесь — вторая защита на случай ручной правки шапки.
CLAUDE_HAIKU_MODEL = "claude-haiku-5-5"


# Сессия: каждое пробуждение роли — новая (бабочка); память между запусками — блокнот роли и лог тикета.
# --resume — только повтор после таймаута в том же запуске (TK-056 п.3′). Предел запусков на роль — _role_busy
# (`role_parallel` в rpv.json, по умолчанию 1); на один тикет — один запуск роли.
# транскрипты сессий claude (`<projects>/<проект>/<session_id>.jsonl`): контекст последнего хода, когда в JSON нет
# `usage.iterations` (см. _context_tokens_last)
CLAUDE_PROJECTS_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude")) / "projects"


RUNNING = {}  # tid -> {role, popen, pid, started, attempt, run_file, err_file, out_fh, err_fh, reason}
# popen=None у записей, восстановленных из state.json["active_runs"] после перезапуска диспетчера
# (recover_active_runs) — тогда живость и остановка идут по pid (_pid_alive/_kill), не по Popen.


@dataclass
class Decision:
    role: str
    reason: str
    header_updates: dict = None


# --- состояние ------------------------------------------------------------------------------

from config import load_state, save_state  # noqa: E402,F401 — файл состояния, пути — CFG


# --- wait_for: само ожидание — waits.py; здесь обёртки подставляют пути проекта --------------------------------

def _where() -> W.Where:
    return W.Where(CFG.PROJECT_ROOT, CFG.DISPATCHER_DIR, CFG.TICKETS_DIR)


def check_wait_for(spec: str) -> bool:
    """Условие `wait_for` выполнено? Формы — `ticket.parse_wait_for` (README, «v4»); см. waits.check_wait_for."""
    return W.check_wait_for(spec, _where())


def record_job_outcome(tid: str, spec: str, outcome: str, rc: str = "", now=None) -> bool:
    return W.record_job_outcome(tid, spec, outcome, rc, now, where=_where())


def note_wait_met(path: Path, tkt: T.Ticket, now) -> T.Ticket:
    return W.note_wait_met(path, tkt, now, _where())


def _blind_tick() -> None:
    W.blind_tick(_where(), _mech_log)


# --- ceo-inbox ---------------------------------------------------------------------------------

# Сигнал CEO — строка в ceo-inbox.md (детали; читает `tickets.py inbox`) и короткая копия в ceo-wake.log (на нём Monitor CEO).
REASON_PRIORITY = {"next": 0, "in_progress-resume": 2}
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


def close_after_merge(path: Path, tkt: T.Ticket, now) -> "T.Ticket | None":
    """alpha #107 П0: `wait_for: merged:` выполнен, приёмка Судьи стоит на голове влитого PR, заказов после неё нет — закрываем
    сами (done, путь из приёмки), роли не будим: автор писал бы `done` ради блокнота, а Судья принимал бы повторно. Заказ CEO
    04:37: только если ожидание поставил Судья (не исполнитель ради шага после слияния) и PR влит в ветку по умолчанию (не в
    промежуточную, как alpha TK-263 в tk262). Иначе None — автора будит wait_for-met, как раньше."""
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
    with T.ticket_lock(path):
        cur = T.read_ticket(path)
        if cur.status != "waiting" or cur.next_role or len(cur.log) != len(tkt.log):
            return None  # пока читали, тикет двинулся (запись роли/CEO)
        T.append_log(path, "dispatcher", f"[итог: done]{f' результат: {proof}' if proof else ''} — PR #{pr} влит, приёмка Судьи "
                     f"на голову {sha[:7]} стоит, нового заказа нет: тикет закрыт без запуска ролей ({NO_REVIEW_MARK}).", now=now)
        T.write_header_updates(path, {"status": "done", "next": "", "wait_for": "", "on_met": ""}, now=now)
    return T.read_ticket(path)


def decide(tkt: T.Ticket, state: dict, now) -> "Decision | None":
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
        last_author = tkt.log[-1].author if tkt.log else ""
        if closed_without_review(tkt):
            return None
        if T.author_is(last_author, reviewer) or last_author.lower() == "dispatcher":
            return None
        if _review_returns(state, tkt.id) >= MAX_REVIEW_RETURNS:
            return None  # пинг-понг: ревьюера не будим, тикет уходит CEO (escalate_review_limit в tick)
        return Decision(role=reviewer, reason="review",
                        header_updates={"status": "in_review"} if status == "done" else None)

    return None


# --- сессия запуска ----------------------------------------------------------------------------

def _resume_store(state: dict, tid: str, role: str) -> dict:
    """session_id/токены контекста на (задача, роль) — для повтора после таймаута."""
    return state.setdefault("ticket_sessions", {}).setdefault(f"{tid}::{role}", {})


_tokens_of = transcript.context_of


def _context_tokens_sum(usage: dict) -> int:
    """Сумма input+cache_read+cache_creation по ВСЕМУ usage из JSON `claude -p` — это сумма по всем
    ходам запуска, не контекст одного хода (см. _context_tokens_last). Для runs.log (ctx_sum=)."""
    return _tokens_of(usage)


def _transcript_last_turn_tokens(session_id) -> int:
    """Контекст ПОСЛЕДНЕГО хода сессии по её транскрипту (usage последнего ответа модели); 0 — транскрипта нет."""
    path = transcript.find(CLAUDE_PROJECTS_DIR, session_id)
    return transcript.last_turn_tokens(path) if path else 0


def _context_tokens_last(result: dict) -> int:
    """Контекст, который реально понесёт следующий `--resume` — последний ход ЭТОГО запуска, не сумма
    по всем его ходам: usage в JSON claude -p суммирует по ходам, и на многоходовом запуске это
    завышает контекст в разы, рвя ротацию раньше времени (CEO 27.09, живой прогон: ctx_sum=333 886
    при реальном контексте хода ~52 тыс.). usage.iterations[-1] — контекст последнего хода; нет
    iterations (аудит 03.10) — последний ход из транскрипта сессии; нет и его — запасная оценка
    ctx_sum // num_turns (среднее по ходам, занижена)."""
    usage = result.get("usage") or {}
    iterations = usage.get("iterations")
    if isinstance(iterations, list) and iterations:
        return _tokens_of(iterations[-1])
    last = _transcript_last_turn_tokens(result.get("session_id"))
    if last:
        return last
    total = _context_tokens_sum(usage)
    try:
        num_turns = int(result.get("num_turns") or 1) or 1
    except (TypeError, ValueError):
        num_turns = 1
    return total // num_turns


def _role_busy(role: str) -> bool:
    """v2 (02.10): активных запусков роли по всем тикетам сразу не больше её предела (по умолчанию 1; раньше — только
    при scope="role"; аудит: одну задачу вели три сессии Исследователя, до 6 запусков параллельно). Предел на роль
    настраивается `role_parallel` в rpv.json; на один тикет —
    по-прежнему один запуск роли (RUNNING по ключу тикета). Занята — запуск ждёт следующего тика."""
    return sum(1 for info in RUNNING.values() if info["role"] == role) >= role_parallel().get(role, 1)


# --- защита от петли и перерасхода (v1.1) --------------------------------------------------

def _record_launch(state: dict, tid: str, now, role: str = "") -> None:
    state.setdefault("launch_role", {})[tid] = role  # MIN_GAP_S — только между запусками одной роли
    hist = state.setdefault("launch_history", {}).setdefault(tid, [])
    hist.append(T.now_iso(now))
    cutoff = now - timedelta(hours=2)  # храним немного с запасом сверх окна MAX_RUNS_PER_TICKET_HOUR
    state["launch_history"][tid] = [t for t in hist if T.parse_dt(t) > cutoff]


def _rate_limited(state: dict, tid: str, now, role: str = "") -> bool:
    """max_runs_per_ticket_hour() / MIN_GAP_S — троттлинг решений (а)-(г); ретраи (д) их не проходят. CP11: пауза
    MIN_GAP_S — между запусками ОДНОЙ роли; передача другой роли (Инженер сдал → Судья) идёт на ближайшем тике
    (на чистом проекте пауза съедала ~40 с из 135); часовой предел считает все роли."""
    hist = [T.parse_dt(t) for t in state.get("launch_history", {}).get(tid, [])]
    if not hist:
        return False
    if len([t for t in hist if (now - t) < timedelta(hours=1)]) >= max_runs_per_ticket_hour():
        return True
    last_role = state.get("launch_role", {}).get(tid, "")
    if role and last_role and role != last_role:
        return False
    return (now - max(hist)).total_seconds() < MIN_GAP_S


from cost import (  # noqa: E402,F401 — учёт стоимости вынесен в cost.py; имена для вызовов из dispatch и tickets
    _today, _add_cost, ticket_cost_spent, add_ticket_cost, _record_cost_event, _rolling_hour_cost,
    _model_usage_number, _model_usage_diff, resolve_run_cost)


def _model_usage_warning(model_usage_diff: dict, expected: str = None) -> str:
    """п.1: «проверь, что в JSON modelUsage только opus» (с В-153 — только семейство модели запуска;
    в _finish_run `expected` = `_expected_model_family(info)` — семейство модели РОЛИ, v1.6.1;
    по умолчанию — model_family(CLAUDE_MODEL)) — автоматическая, не разовая проверка, и
    только по РАЗНИЦЕ этого запуска (см. _model_usage_diff), не по кумулятивной истории сессии.
    `expected` — "haiku" для executor: haiku (судья TK-002 п.5в: диспетчер проверяет по разнице
    modelUsage, что запуск реально был на Haiku, не тихо на другой модели)."""
    if not isinstance(model_usage_diff, dict) or not model_usage_diff:
        return None
    if expected is None:
        expected = model_family(CLAUDE_MODEL)
    bad = [m for m in model_usage_diff if expected not in str(m).lower()]
    if bad:
        return f"modelUsage этого запуска содержит модели без «{expected}» ({', '.join(bad)})"
    return None


from procs import (  # noqa: E402,F401 — процессы вынесены в procs.py; здесь имена для вызовов из dispatch
    _proc_start, _pid_alive, PID_CHECK_TRIES, PID_CHECK_TIMEOUT_S, _pid_state, _ps_field, _proc_state, _proc_comm, _pid_alive_posix, _kill, acquire_instance_lock, release_instance_lock, _proc_alive, _kill_proc)


def _stop_run(info: dict) -> bool:
    """Снять запущенную роль всем деревом процессов и проверить, что она умерла (ждём до STOP_VERIFY_S). True — умерла.
    Уже мёртвый процесс не трогаем: его pid ОС могла отдать чужому."""
    pid = info.get("pid") or (info["popen"].pid if info.get("popen") is not None else None)
    if _proc_alive(info):
        _kill(pid, tree=True)
    deadline = time.time() + STOP_VERIFY_S
    while _proc_alive(info):
        if time.time() >= deadline:
            return False
        time.sleep(0.1)
    return True


# --- запуск роли ------------------------------------------------------------------------------

def _popen(cmd, **kwargs):
    """Точка подмены для тестов (вместо многословного CLAUDE_BIN) — оборачивает subprocess.Popen."""
    return subprocess.Popen(cmd, **kwargs)
















def build_prompt(role: str, tid: str, extra_note: str = None) -> str:
    return RA.build_prompt(role, tid, RUN_TIMEOUT, extra_note)


def launch_run(ticket_path, role: str, state: dict, now, reason: str, attempt: int = 0,
               extra_note: str = None) -> None:
    ticket_path = Path(ticket_path)
    tid = ticket_path.stem
    CFG.RUNS_DIR.mkdir(parents=True, exist_ok=True)
    ts = now.strftime("%Y%m%d-%H%M%S")
    run_file = CFG.RUNS_DIR / f"{ts}-{tid}-{role}.json"
    err_file = CFG.RUNS_DIR / f"{ts}-{tid}-{role}.err.log"

    store = _resume_store(state, tid, role)
    sid = store.get("session_id")
    state.get("on_met_chain", {}).pop(tid, None)  # запуск роли обрывает цепочку on_met
    if state.get("stopped_runs", {}).pop(tid, None):
        # прошлый запуск этого тикета оборван CEO (`tickets.py stop`): сессия НОВАЯ (без --resume), в промпте — пометка
        sid = None
        extra_note = " ".join(x for x in (extra_note, STOP_NOTE) if x)
    if sid and not attempt:
        # каждое пробуждение — новая сессия; --resume только у повтора после таймаута (attempt > 0)
        sid = None
        rotate_note = (f"Начинаем новую сессию: прочитай блокнот .claude/roles/notes/{role}.md и прежние решения "
                       f"docs/research/reviews/ по нужной задаче.")
        extra_note = " ".join(x for x in (extra_note, rotate_note) if x)

    if reason == "next":
        next_note = ("Запуск по явной передаче (`--next`): прочитай последнюю запись лога, сделай "
                     "запрошенное и запиши итог.")
        extra_note = " ".join(x for x in (extra_note, next_note) if x)
    if reason == "ceo-order-after-accept":
        order_note = ("PR влит, но после приёмки Судьи в логе есть заказ CEO: ответь на него записью — выполнен (чем, "
                      "коммит/PR) или нет (что осталось); не закрывай тикет молча.")
        extra_note = " ".join(x for x in (extra_note, order_note) if x)
    prompt = build_prompt(role, tid, extra_note)
    try:
        launch_tkt = T.read_ticket(ticket_path)
        status_at_launch = launch_tkt.status
        if role == launch_tkt.owner and status_at_launch == "todo":
            # TK-070 п.6: «в работе» ставит диспетчер при старте владельца — роль не обязана менять todo (раньше: повтор → blocked)
            T.write_header_updates(ticket_path, {"status": "in_progress"}, now=now)
            status_at_launch = "in_progress"  # старт ≠ смена статуса ролью: холостой ход считается как раньше
        executor = launch_tkt.executor
        effort = effort_for(role, launch_tkt)
        # v2: «роль оставила запись» — новый заголовок записи ЭТОЙ роли (ключи на старте), а не рост лога
        log_keys_at_launch = T.role_entry_keys(launch_tkt, role)
    except Exception:
        status_at_launch, executor, effort, log_keys_at_launch = None, "", effort_for(role), []
    # executor: haiku (судья TK-002 п.5) — заведомо проверенный на whitelist/обход тикетом (tickets.py
    # new и haiku_refused_reason() в tick()); здесь только сама подмена модели.
    model = CLAUDE_HAIKU_MODEL if executor == "haiku" else ROLE_MODEL.get(role, CLAUDE_MODEL)
    if executor == "haiku":
        effort = "xhigh"  # решение владельца 08.10: Haiku 5.5 везде на xhigh
    cmd = [CLAUDE_BIN, "-p", prompt, "--output-format", "json", "--permission-mode", "bypassPermissions",
           "--model", model, "--effort", effort] + role_mcp_args(run_file.stem) + plugin_dir_args()
    if sid:
        cmd += ["--resume", sid]
    else:
        sid = str(uuid.uuid4())  # id известен заранее: после таймаута повтор идёт --resume этой же сессии (TK-056 п.3′)
        cmd += ["--session-id", sid]

    env = dict(os.environ)
    # Судья 27.09, п.4 «обязательно»: без этого дочерний claude наследует CLAUDE_CODE_HOST_SESSION_ID
    # сессии CEO (диспетчер сам запущен из неё) — role_context.py/role_memory.py принимают роль за CEO
    # (тревоги/inbox/«молчание» ломаются на все роли). RPV_ROLE сама по себе не спасает: find_title()
    # срабатывает раньше при непустом host_id, если сама переменная не снята.
    for _k in list(env):
        if "HOST_SESSION" in _k.upper():
            env.pop(_k, None)
    # хуки (role_context/role_memory) ведут состояние и конспект прошлой сессии по тикету
    env["RPV_ROLE"] = role
    env["RPV_TICKET"] = tid
    env["RPV_PROJECT"] = str(CFG.PROJECT_ROOT)  # tickets.py роли берёт проект отсюда (и из cwd запуска)

    out_fh = open(run_file, "w", encoding="utf-8")
    err_fh = open(err_file, "w", encoding="utf-8")
    # зеркало в state.json — ДО Popen (pid пока неизвестен): диспетчер убит между запуском роли и записью зеркала —
    # после рестарта роль не сирота, её находят по session_id (recover_active_runs)
    mirror = {
        "role": role, "pid": None, "started": T.now_iso(now), "attempt": attempt,
        "run_file": str(run_file), "err_file": str(err_file), "reason": reason,
        "status_at_launch": status_at_launch, "executor": executor,
        "log_keys_at_launch": log_keys_at_launch, "effort": effort, "session_id": sid,
    }
    state.setdefault("active_runs", {})[tid] = mirror
    save_state(state)
    # своя группа процессов (на Windows параметр без действия): остановка CEO снимает роль вместе с потомками
    try:
        popen = _popen(cmd, cwd=str(CFG.PROJECT_ROOT), env=env, stdout=out_fh, stderr=err_fh, text=True,
                       **hide.hidden_console())
    except BaseException:
        state.get("active_runs", {}).pop(tid, None)
        save_state(state)
        raise
    mirror["pid"] = popen.pid
    _drop_ceo_handoff(state, tid)  # любой запуск позже передачи CEO — ход состоялся, метка отработала
    pstart = _proc_start(popen.pid)
    mirror["pstart"] = pstart
    RUNNING[tid] = {
        "role": role, "popen": popen, "pid": popen.pid, "pstart": pstart, "started": now, "attempt": attempt,
        "run_file": run_file, "err_file": err_file, "out_fh": out_fh, "err_fh": err_fh, "reason": reason,
        "status_at_launch": status_at_launch, "executor": executor,
        "log_keys_at_launch": log_keys_at_launch, "effort": effort, "session_id": sid,
    }
    # last_woken — приоритет очереди запусков внутри роли (кто дольше не запускался — тот первый, см. tick);
    # всегда на (задачу, роль)
    sess_entry = state.setdefault("sessions", {}).setdefault(f"{tid}::{role}", {})
    sess_entry["last_woken"] = T.now_iso(now)
    _record_launch(state, tid, now, role)
    # зеркало в state.json (pid, задача, роль, старт) — переживает перезапуск диспетчера (recover_active_runs)
    save_state(state)


def _read_run_result(run_file: Path) -> dict:
    try:
        data = Path(run_file).read_text(encoding="utf-8").strip()
        return json.loads(data) if data else {}
    except Exception:
        return {}


def _transcript_usage_since(session_id, started) -> dict:
    """Токены запуска по транскрипту сессии (TK-055): сумма usage ответов модели с отметкой >= started (ISO или
    datetime), по одному на message.id (потоковые дубли — последний). Для запусков без JSON (таймаут/убит): {} —
    сессии/транскрипта нет."""
    try:
        path = transcript.find(CLAUDE_PROJECTS_DIR, session_id)
        if path is None:
            return {}
        t0 = started if isinstance(started, datetime) else P.parse_time(started, strict=True)
        if t0.tzinfo is None:
            t0 = t0.astimezone()
        per = {}
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"usage"' not in line:
                    continue
                try:
                    row = json.loads(line)
                    ts = P.parse_time(row["timestamp"], strict=True)
                except Exception:
                    continue
                msg = row.get("message") or {}
                u = msg.get("usage")
                if ts >= t0 and isinstance(u, dict):
                    per[msg.get("id") or row.get("uuid") or len(per)] = u
        tot = {"input_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "output_tokens": 0}
        for u in per.values():
            for k in tot:
                tot[k] += int(u.get(k) or 0)
        return tot if per else {}
    except Exception:
        return {}


def _log_run_summary(tid: str, info: dict, result: dict, now, timed_out: bool, resolved_cost: float,
                      ticket_spent: float, cost_note: bool = False, stopped: bool = False) -> None:
    CFG.RUNS_LOG.parent.mkdir(parents=True, exist_ok=True)
    usage = result.get("usage") or {}
    src = "json"
    if not usage:  # таймаут/убит: токены — из транскрипта сессии с момента старта запуска
        usage = _transcript_usage_since(info.get("session_id"), info.get("started")) or {}
        src = "jsonl" if usage else "none"
    status = ("stopped" if stopped else "timeout" if timed_out else "api_error" if _api_error(result)
              else ("ok" if result else "no_output"))
    # cost_usd — сырой (кумулятивный за сессию) из JSON; resolved_cost — разница с прошлым итогом ТОЙ ЖЕ
    # session_id (судья TK-002 п.1) — то, что реально начислено этому запуску; cost_note=asis — не было
    # с чем сравнить (новая/ротированная сессия) или разница < 0 — использован сырой итог как есть.
    # ticket_spent — накоплено по ЭТОЙ задаче ПОСЛЕ этого запуска (колонка CEO, роль её не видит).
    line = (f"{T.now_iso(now)} {tid} {info['role']} reason={info.get('reason')} "
            f"attempt={info.get('attempt', 0)} session={result.get('session_id') or info.get('session_id') or '-'} "
            f"cost_usd={result.get('total_cost_usd', '-')} resolved_cost={resolved_cost:.4f} "
            f"cost_note={'asis' if cost_note else 'diff'} ticket_spent={ticket_spent:.4f} "
            f"in_tok={usage.get('input_tokens', '-')} out_tok={usage.get('output_tokens', '-')} "
            f"cr_tok={usage.get('cache_read_input_tokens', '-')} cw_tok={usage.get('cache_creation_input_tokens', '-')} "
            f"tok_src={src} "
            f"ctx_last={_context_tokens_last(result)} ctx_sum={_context_tokens_sum(usage)} "
            f"terminal_reason={result.get('terminal_reason') or '-'} is_error={result.get('is_error', '-')} "
            f"status={status}\n")
    with open(CFG.RUNS_LOG, "a", encoding="utf-8") as fh:
        fh.write(line)


def _role_logged(tkt: T.Ticket, role: str, info: dict) -> bool:
    """v2 (02.10): роль оставила НОВУЮ запись лога — заголовок `### <ts> <роль>`, которого не было на старте
    запуска (`log_keys_at_launch`). Не «лог вырос»: запись CEO/dispatcher/другой роли не считается, правка
    текста тоже; от компакции лога (смещений) не зависит. Запуск из старого state.json без снимка ключей
    (диспетчер перезапущен посреди запуска) — запасной путь: запись этой роли с временем не раньше старта."""
    keys = T.role_entry_keys(tkt, role)
    before = info.get("log_keys_at_launch")
    if before is not None:
        return bool(Counter(keys) - Counter(before))  # Counter: две одинаковые записи в одну секунду — две
    started = info.get("started")
    if started is None:
        return bool(keys)
    return any(e.ts >= started for e in tkt.log if T.author_is(e.author, role))


def _block_ticket(path: Path, tid: str, role: str, why: str, state: dict, now, log_text: str) -> None:
    T.write_header_updates(path, {"status": "blocked"}, now=now)
    # роль без "@" намеренно: @упоминания никого не будят (v2), а запись dispatcher не должна выглядеть
    # просьбой к роли
    T.append_log(path, "dispatcher", log_text, now=now)
    append_ceo_inbox(tid, "blocked", f"{role}: {why}", now)
    # одна строка CEO: маркер статуса, чтобы notify_status_for_ceo следующим тиком не написал вторую; счётчики
    # тормозов цикла сброшены — CEO сам решит, что дальше
    state.setdefault("ceo_status_notified", {})[tid] = f"blocked@{T.now_iso(now)}"
    state.setdefault("idle_runs", {}).pop(f"{tid}::{role}", None)
    _reset_same_status(state, f"{tid}::{role}")


def _reset_same_status(state: dict, key: str) -> None:
    state.setdefault("same_status_runs", {}).pop(key, None)


def _same_status_loop(tkt: T.Ticket, logged: bool, status_changed: bool) -> bool:
    """Запуск оставил запись, а статус тот же: in_progress, либо waiting при УЖЕ выполненном wait_for (иначе ждать
    — штатно). Подряд MAX_SAME_STATUS_RUNS таких запусков — петля (аудит 03.10)."""
    if not logged or status_changed:
        return False
    if tkt.status == "in_progress":
        return True
    if tkt.status == "waiting":
        return check_wait_for(tkt.header.get("wait_for", ""))
    return False


def _finish_role_part(tid: str, info: dict, state: dict, now, timed_out: bool, result: dict) -> None:
    """Что делать с тикетом после запуска (деньги/модель уже учтены в _finish_run)."""
    role = info["role"]
    key = f"{tid}::{role}"
    sess = state.setdefault("sessions", {}).setdefault(key, {})  # retries — всегда на (задачу, роль)
    store = _resume_store(state, tid, role)
    if result.get("session_id"):
        store["session_id"] = result["session_id"]
    elif (timed_out and info.get("session_id") and not store.get("session_id")
          and transcript.find(CLAUDE_PROJECTS_DIR, info["session_id"]) is not None):
        store["session_id"] = info["session_id"]  # убитая новая сессия: транскрипт есть — повтор продолжит её, не с нуля

    path = CFG.TICKETS_DIR / f"{tid}.md"
    if not path.exists():
        save_state(state)
        return
    tkt = T.read_ticket(path)
    handed_to_ceo = _ceo_handoff_during_run(state, tid, info)
    if (role == tkt.owner and tkt.status == "waiting" and not (tkt.header.get("wait_for") or "").strip()
            and not tkt.next_role and not handed_to_ceo):
        # TK-070 п.3: ожидание без условия пробуждения — отказ (иначе тикет молчит до эскалации); вернуть в работу
        T.write_header_updates(path, {"status": "in_progress"}, now=now)
        T.append_log(path, "dispatcher", "status: waiting без wait_for и без next — отказ: ожидание без условия "
                     "пробуждения не принимается; тикет возвращён в in_progress. Задай условие "
                     "`tickets.py wait <ID> <форма>` (формы — README диспетчера) или передай `--next <роль>`.", now=now)
        tkt = T.read_ticket(path)

    # аудит 03.10: запуск ЛЮБОЙ роли (владелец, ревьюер, адресат `--next`) без новой записи — провал: один повтор,
    # затем blocked (раньше чужой холостой запуск молчал, и тикет `in_review` висел вечно).
    logged = _role_logged(tkt, role, info)  # судья 27.09, п.7: таймаут сам по себе — не провал, если запись успела
    stuck_todo = role == tkt.owner and logged and tkt.status == "todo"  # отчиталась, но не увела статус с todo
    status_changed = tkt.status != info.get("status_at_launch")
    if role == tkt.reviewer and info.get("status_at_launch") == "in_review" and logged:
        # запуск ревьюера на ревью: вернул владельцу (todo/in_progress либо `--next` другой роли) — счётчик +1, иначе
        # (принял, заблокировал, передал CEO) ревью состоялось — серия кончилась
        returns = state.setdefault("review_returns", {})
        if tkt.status in ("todo", "in_progress") or (tkt.next_role in P.ROLES and tkt.next_role != role):
            returns[tid] = returns.get(tid, 0) + 1
        else:
            returns.pop(tid, None)
    idle_runs = state.setdefault("idle_runs", {})
    same_runs = state.setdefault("same_status_runs", {})

    # холостой ход: нет записи и статус не сменён — считаем подряд (число, не деньги); первый — обычный повтор ниже
    if (not logged) and (not status_changed):
        idle_runs[key] = idle_runs.get(key, 0) + 1
        if idle_runs[key] >= MAX_IDLE_RUNS:
            n = idle_runs[key]
            _block_ticket(path, tid, role, f"холостой ход ×{n}", state, now,
                          f"Запусков роли {role} подряд без записи и без смены статуса: {n} — холостой ход, "
                          "задача заблокирована, нужен CEO.")
            sess["retries"] = 0
            save_state(state)
            return
    else:
        idle_runs.pop(key, None)

    # тормоз цикла: запись есть, статус не меняется N запусков подряд
    if _same_status_loop(tkt, logged, status_changed):
        same_runs[key] = same_runs.get(key, 0) + 1
        if same_runs[key] >= MAX_SAME_STATUS_RUNS:
            n = same_runs[key]
            _block_ticket(path, tid, role, f"{n} запусков подряд: запись есть, статус {tkt.status} не меняется",
                          state, now,
                          f"Роль {role}: {n} запусков подряд оставляют запись, а статус остаётся {tkt.status} — "
                          "петля, задача заблокирована, нужен CEO.")
            sess["retries"] = 0
            save_state(state)
            return
    elif status_changed or tkt.status not in ("in_progress", "waiting"):
        _reset_same_status(state, key)

    if logged and not stuck_todo:
        sess["retries"] = 0
        save_state(state)
        return

    # (д) запуск завершился без пригодного результата — один повтор, затем blocked
    if info.get("attempt", 0) < 1:
        if not logged:
            note = ("Предыдущий запуск не оставил новую запись в «## Лог» — обязательно допиши итог "
                    "командой tickets.py result (done|pr|accept|return|blocked|ask-owner|wait, --why); status и next поставит маршрут." if not timed_out else
                    "Предыдущий запуск не уложился в таймаут — сократи шаг и обязательно запиши итог.")
        else:
            note = ("Запись в «## Лог» есть, но status остался todo — обязательно смени статус (например "
                    "in_progress/waiting/done), иначе задача возьмётся в работу заново.")
        launch_run(path, role, state, now, reason="retry", attempt=info.get("attempt", 0) + 1, extra_note=note)
        sess["retries"] = sess.get("retries", 0) + 1
    else:
        why = ("статус остался todo дважды подряд" if stuck_todo else
               "дважды не уложился в таймаут" if timed_out else
               "дважды не оставил запись в «## Лог»")
        _block_ticket(path, tid, role, why, state, now,
                      f"Запуск роли {role} — {why} — задача заблокирована, нужен CEO.")
        sess["retries"] = 0
    save_state(state)


# --- лимит сессии (TK-070 п.2): ответ 429 «You've hit your session limit · resets 5am (Asia/Tbilisi)» — не холостой ход ---











def _handle_api_error(tid: str, info: dict, state: dict, now, result: dict) -> None:
    """Ошибка API — не холостой ход и не провал роли: пауза запусков, счётчик подряд; N подряд — blocked с причиной;
    вход истёк — сразу сигнал CEO (повтор не поможет)."""
    role = info["role"]
    key = f"{tid}::{role}"
    text = _first_line(str(result.get("result") or ""), 160) or f"api_error_status={result.get('api_error_status')}"
    counts = state.setdefault("api_errors", {})
    counts[key] = n = counts.get(key, 0) + 1
    until = now + (API_AUTH_PAUSE if _auth_error(result) else API_PAUSE)
    cur = state.get("limit_pause_until")
    if not cur or T.parse_dt(cur) < until:
        state["limit_pause_until"] = T.now_iso(until)
    state.setdefault("idle_runs", {}).pop(key, None)
    _reset_same_status(state, key)
    state.setdefault("sessions", {}).setdefault(key, {})["retries"] = 0
    print(f"[dispatch] {T.now_iso(now)} ошибка API на {tid} ({role}) x{n}: {text}; запуски на паузе до {T.now_iso(until)}",
          file=sys.stderr, flush=True)
    path = CFG.TICKETS_DIR / f"{tid}.md"
    if _auth_error(result):
        append_ceo_inbox(tid, "auth", f"{role}: вход Claude истёк — {text}. Войти заново (`claude /login`)", now)
    elif n >= MAX_API_ERRORS and path.exists():
        counts.pop(key, None)
        _block_ticket(path, tid, role, f"ошибка API x{n}: {text}", state, now,
                      f"Запусков роли {role} подряд оборвала ошибка API: {n} — {text}. Задача заблокирована, нужен CEO.")
    save_state(state)






def _last_run_was_limit(tid: str) -> bool:
    runs = sorted(CFG.RUNS_DIR.glob(f"*-{tid}-*.json")) if CFG.RUNS_DIR.exists() else []
    return bool(runs) and _limit_hit(_read_run_result(runs[-1]))


def unblock_limit_victims(now) -> None:
    """blocked из-за холостых запусков, чей последний запуск — 429 лимита сессии: вернуть в in_progress (тикет не виноват)."""
    for path in T.list_tickets(CFG.TICKETS_DIR):
        try:
            tkt = T.read_ticket(path)
            if tkt.status != "blocked" or tkt.id in RUNNING or not _last_run_was_limit(tkt.id):
                continue
            tail = (tkt.log_raw or "")[-1500:]
            if "холост" not in tail and "не оставил запись" not in tail:
                continue
            T.write_header_updates(path, {"status": "in_progress"}, now=now)
            T.append_log(path, "dispatcher", "блок снят автоматически: последний запуск оборвал лимит сессии (429), "
                         "тикет не виноват — роль запустится после сброса лимита.", now=now)
        except Exception:
            continue


def _finish_run(tid: str, info: dict, state: dict, now, timed_out: bool, stopped: bool = False) -> None:
    for fh in (info.get("out_fh"), info.get("err_fh")):
        try:
            fh.close()
        except Exception:
            pass
    state.setdefault("active_runs", {}).pop(tid, None)
    result = _read_run_result(info["run_file"])

    # Стоимость ЭТОГО запуска — разница с прошлым кумулятивным итогом ТОЙ ЖЕ session_id (судья TK-002
    # п.1: --resume отдаёт total_cost_usd/modelUsage кумулятивно за всю историю сессии, не за этот
    # запуск — живой пример 27.09: сессия судьи $6,80 → $8,29 → $8,74 кумулятивных при реальных тратах
    # запусков $1,49 и $0,45, отсюда ложная часовая пауза $19,39/ч). Нет JSON вовсе (убит по таймауту/
    # вручную) — п.1(д): трата теряется НЕДОУЧЁТОМ на этот раз (не досчитываем потолком запуска, как в
    # v1.3 — так считали бы дважды: и потолком сейчас, и разницей на следующем resume той же сессии),
    # суточный/часовой итог в этом случае не точен — известное ограничение, не пытаемся угадать число.
    if result.get("total_cost_usd") is not None:
        resolved_cost, model_usage_diff, cost_note = resolve_run_cost(state, result)
    else:
        resolved_cost, model_usage_diff, cost_note = 0.0, {}, True
    _add_cost(state, now, resolved_cost)
    add_ticket_cost(state, tid, resolved_cost)
    _record_cost_event(state, now, resolved_cost)
    _log_run_summary(tid, info, result, now, timed_out, resolved_cost, ticket_cost_spent(state, tid), cost_note,
                     stopped=stopped)

    expected_model = _expected_model_family(info)
    model_warn = _model_usage_warning(model_usage_diff, expected_model)
    if model_warn:
        route_ceo_signal(tid, "model", model_warn, state, now)

    if _limit_hit(result) and not stopped:
        try:
            ref = datetime.fromtimestamp(Path(info["run_file"]).stat().st_mtime).astimezone()
        except OSError:
            ref = now
        until = min(_limit_reset_at(result, now, ref), now + LIMIT_PAUSE_MAX)
        if until > now:
            state["limit_pause_until"] = T.now_iso(until)
        key = f"{tid}::{info['role']}"
        state.setdefault("idle_runs", {}).pop(key, None)
        _reset_same_status(state, key)
        state.setdefault("sessions", {}).setdefault(key, {})["retries"] = 0
        print(f"[dispatch] {T.now_iso(now)} лимит сессии (429) на {tid}: запуски на паузе до {T.now_iso(until)}",
              file=sys.stderr, flush=True)
        save_state(state)
        return  # лимит — не провал запуска: ни повтора, ни холостого хода, ни blocked
    if stopped:
        return  # остановка CEO — не провал: ни повтора, ни «не оставил запись» (след и счётчики — _apply_stop)
    if _api_error(result):
        _handle_api_error(tid, info, state, now, result)
        return  # ошибка API (обрыв сети, 5xx, вход) — не холостой ход: пауза, N подряд — blocked с причиной
    state.get("api_errors", {}).pop(f"{tid}::{info['role']}", None)
    _finish_role_part(tid, info, state, now, timed_out, result)


def _poll_running(state: dict, now) -> None:
    for tid in list(RUNNING):
        info = RUNNING[tid]
        if _proc_alive(info):
            if (now - info["started"]).total_seconds() > RUN_TIMEOUT:
                _kill_proc(info)
                del RUNNING[tid]
                _finish_run(tid, info, state, now, timed_out=True)
            continue
        del RUNNING[tid]
        _finish_run(tid, info, state, now, timed_out=False)


def _find_pid_by_session(session_id) -> "int | None":
    """Роль, запущенная перед убийством диспетчера, но не успевшая попасть в зеркало с pid: ищем процесс по
    `--session-id`/`--resume <id>` в командной строке (id известен до запуска)."""
    if not session_id or not re.fullmatch(r"[A-Za-z0-9_-]+", str(session_id)):
        return None
    sid = str(session_id).lower()
    pairs = [(p, pp) for p, pp, cmd in P.scan_processes()
             if p != os.getpid() and sid in cmd.lower() and "get-ciminstance" not in cmd.lower()]  # сам сканер — не роль
    found = {p for p, _ in pairs}
    roots = [p for p, pp in pairs if pp not in found]  # обёртка (cmd/node/sh) выше дочернего — берём корень дерева
    return min(roots or found) if found else None


def recover_active_runs(state: dict, now) -> None:
    """После перезапуска диспетчера — подхватить зеркало state.json["active_runs"]: живой pid не
    запускаем повторно (просто продолжаем отслеживать по pid), уже закончившийся — обрабатываем как
    обычное завершение прогона (лог/ретрай/blocked), раз диспетчер это пропустил, пока не работал.
    Зеркало от диспетчера до v2 снимка `log_keys_at_launch` не содержит — тогда «новая запись» ищется
    по времени старта (см. _role_logged)."""
    for tid, saved in list(state.get("active_runs", {}).items()):
        if tid in RUNNING:
            continue  # уже отслеживаем в этом процессе (это не перезапуск)
        info = {
            "role": saved.get("role"), "popen": None, "pid": saved.get("pid"), "pstart": saved.get("pstart"),
            "started": T.parse_dt(saved["started"]), "attempt": saved.get("attempt", 0),
            "run_file": Path(saved["run_file"]), "err_file": Path(saved.get("err_file") or ""),
            "out_fh": None, "err_fh": None, "reason": saved.get("reason", "recovered"),
            "status_at_launch": saved.get("status_at_launch"),
            "executor": saved.get("executor", ""), "log_keys_at_launch": saved.get("log_keys_at_launch"),
            "effort": saved.get("effort", ""),
        }
        by_session = False
        if not saved.get("pid"):  # зеркало-намерение: диспетчер убит до записи pid
            by_session = True
            found = _find_pid_by_session(saved.get("session_id"))
            if not found:
                state.get("active_runs", {}).pop(tid, None)
                try:
                    has_output = info["run_file"].stat().st_size > 0
                except OSError:
                    has_output = False
                if has_output:  # роль отработала, пока диспетчер лежал: итог прогона не терять
                    print(f"[dispatch] {T.now_iso(now)} подхват {tid}: процесса нет, вывод есть — разбираю как завершённый",
                          file=sys.stderr, flush=True)
                    _finish_run(tid, info, state, now, timed_out=False)
                else:
                    print(f"[dispatch] {T.now_iso(now)} подхват {tid}: запуск не состоялся (процесса с session_id нет, "
                          "вывода нет) — зеркало снято", file=sys.stderr, flush=True)
                continue
            saved["pid"] = info["pid"] = found
            saved["pstart"] = info["pstart"] = _proc_start(found)  # метки старта не было — берём текущую: «процесс есть»
            print(f"[dispatch] {T.now_iso(now)} подхват {tid}: pid {found} найден по session_id (зеркало было без pid)",
                  file=sys.stderr, flush=True)
        want, got = saved.get("pstart"), _proc_start(saved.get("pid"))
        alive = _pid_alive(saved.get("pid"), expect_name="" if by_session else None, start=want)
        how = (f"старт записан {want}, сейчас {got}" if want
               else "старт не записан (запуск прежней версии) — по имени образа")
        print(f"[dispatch] {T.now_iso(now)} подхват {tid}: pid {saved.get('pid')}, {how}: "
              f"{'жив — слежу' if alive else 'не найден или занят другим процессом — разбираю как завершённый'}",
              file=sys.stderr, flush=True)
        if alive:
            RUNNING[tid] = info
        else:
            state.get("active_runs", {}).pop(tid, None)
            _finish_run(tid, info, state, now, timed_out=False)


# --- остановка роли посреди запуска: `tickets.py stop` (CEO, 03.10) ----------------------------------------------

def write_stop_request(tid: str, next_role: str, text: str, now=None) -> Path:
    """`tickets.py stop`: заявка `stop/<ID>.json` — время, роль для `--next` (или пусто) и текст новой постановки. Диспетчер
    разбирает её на ближайшем тике (process_stop_requests); новая заявка по тому же тикету заменяет прежнюю."""
    CFG.STOP_DIR.mkdir(parents=True, exist_ok=True)
    path = CFG.STOP_DIR / f"{tid}.json"
    P.write_json(path, {"at": T.now_iso(now), "next": next_role or "", "text": text})
    return path


def _stop_requests() -> list:
    out = []
    for path in sorted(CFG.STOP_DIR.glob("*.json")) if CFG.STOP_DIR.exists() else []:
        try:
            req = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(req, dict):
            out.append((path, req))
    return out


def _reset_loop_state(state: dict, tid: str) -> None:
    """Остановка CEO — не провал и новая постановка: счётчики холостых и «запись есть, статус тот же» запусков, повторов,
    ротации (сессия и контекст ЭТОГО тикета у всех ролей) и часовой лимит запусков по тикету — с нуля."""
    for role in P.ROLES:
        key = f"{tid}::{role}"
        state.setdefault("idle_runs", {}).pop(key, None)
        _reset_same_status(state, key)
        if key in state.get("sessions", {}):
            state["sessions"][key]["retries"] = 0
        store = state.get("ticket_sessions", {}).get(key)
        if store:
            store.pop("session_id", None)
    state.get("launch_history", {}).pop(tid, None)
    state.get("launch_role", {}).pop(tid, None)


def _apply_stop(path: Path, tid: str, req: dict, info, state: dict, now) -> None:
    """Запись тикета после остановки: след диспетчера → запись CEO с постановкой (последняя в логе — её читает следующий
    запуск) → статус (`--next`: todo + next, роль стартует в этом же тике; иначе stopped — не будит, пока CEO не переведёт
    в todo). Был снят запуск — следующий запуск тикета начнёт новую сессию (пометка `stopped_runs`, см. launch_run)."""
    hhmm = now.strftime("%H:%M")
    if info is not None:
        T.append_log(path, "dispatcher", f"остановлен CEO в {hhmm}: запуск роли {info['role']} (pid {info.get('pid')}) снят "
                                          "вместе с дочерними процессами; следующий запуск — новая сессия.", now=now)
    else:
        T.append_log(path, "dispatcher", f"остановка CEO в {hhmm}: запущенной роли не было — статус применён.", now=now)
    try:
        at = T.parse_dt(req["at"])
    except Exception:
        at = now
    T.append_log(path, "ceo", str(req.get("text") or "").strip() or "(постановка без текста)", now=at)
    nxt = req.get("next")
    if nxt in P.ROLES:
        T.write_header_updates(path, {"status": "todo", "next": nxt}, now=now)
    else:
        T.write_header_updates(path, {"status": "stopped", "next": ""}, now=now)
    _reset_loop_state(state, tid)
    if info is not None:
        state.setdefault("stopped_runs", {})[tid] = T.now_iso(now)


def process_stop_requests(state: dict, now) -> None:
    """Заявки `tickets.py stop`: запущенную роль тикета снять деревом процессов, проверить смерть, записать след и статус.
    Не умерла — одна на заявку строка CEO `stop-failed`, заявка и запуск остаются до следующего тика. Тикета нет или он
    не читается — заявку не трогаем (битый тикет заметит основной цикл; чужой корень заявку не съест)."""
    for req_path, req in _stop_requests():
        tid = req_path.stem
        path = CFG.TICKETS_DIR / f"{tid}.md"
        try:
            T.read_ticket(path)
        except Exception:
            continue
        info = RUNNING.get(tid)
        if info is not None:
            if not _stop_run(info):
                notified = state.setdefault("ceo_stop_failed_notified", {})
                if notified.get(tid) != req.get("at"):
                    append_ceo_inbox(tid, "stop-failed", f"{info['role']}: pid {info.get('pid')} не умер после снятия "
                                     "деревом процессов — заявка остаётся, снимите вручную", now)
                    notified[tid] = req.get("at")
                continue
            RUNNING.pop(tid, None)
            _finish_run(tid, info, state, now, timed_out=False, stopped=True)
        _apply_stop(path, tid, req, info, state, now)
        try:
            req_path.unlink()
        except OSError:
            pass


# --- тик / цикл -------------------------------------------------------------------------------

def _apply_header_updates(path: Path, updates: dict, now) -> None:
    """Правка шапки перед запуском; одно лишь очищение `next` поле `updated` не двигает (иначе у blocked/
    needs_owner менялся бы маркер уведомления CEO)."""
    only_next = set(updates) <= {"next"}
    T.write_header_updates(path, updates, now=now, stamp_updated=not only_next)


def _candidate_sort_key(state: dict, tkt: T.Ticket, decision: Decision):
    last = state.get("sessions", {}).get(f"{tkt.id}::{decision.role}", {}).get("last_woken") or ""
    return (REASON_PRIORITY.get(decision.reason, 1), last, tkt.id)


def run_on_met(path: Path, tkt: T.Ticket, state: dict, now) -> bool:
    """`on_met.run_on_met` с путями текущего проекта."""
    return ON_MET.run_on_met(path, tkt, state, now, CFG.PROJECT_ROOT, CFG.RUNS_LOG)


IDLE_SLO_MIN = 10.0
IDLE_GRACE_S = 600.0
INVARIANT_GRACE_S = 600.0  # TK-076 п.6: открытый тикет без хода дольше этого — владельца будим сами
LIMIT_PAUSE_MAX = timedelta(hours=1)  # пауза 429 не длиннее часа: дальше пробный запуск (429 бесплатен), новая метка — новая пауза


def _waits_without_condition(tkt: T.Ticket, now) -> bool:
    if tkt.status != "waiting" or tkt.owner not in P.ROLES or tkt.next_role:
        return False
    if (tkt.header.get("wait_for") or "").strip():
        return False
    try:
        return (now - T.parse_dt(tkt.header.get("updated", ""))).total_seconds() > IDLE_GRACE_S
    except Exception:
        return False


def _drop_ceo_handoff(state: dict, tid: str) -> None:
    (state.get("ceo_handoffs") or {}).pop(tid, None)
    (state.get("ceo_handoff_reminded") or {}).pop(tid, None)


def _expire_ceo_handoff(tkt: T.Ticket, state: dict) -> None:
    """Метка передачи CEO кончается на тике, как только тикет вышел из ожидания (waiting/in_review): CEO вернул его
    шапкой, done/stopped/blocked. Запуск любой роли и запись CEO снимают её в launch_run / _ceo_handoff_pending."""
    at = (state.get("ceo_handoffs") or {}).get(tkt.id)
    if not at or tkt.id in RUNNING or tkt.status in ("waiting", "in_review"):
        return
    # `--next ceo` и смена статуса роли — две команды: тик между ними видит старый статус при свежей метке. Статус
    # сменили после метки (updated новее) — это ход CEO/роли; иначе метку не трогаем, роль ещё допишет `waiting`.
    try:
        if tkt.status in ("todo", "in_progress") and T.parse_dt(tkt.header.get("updated", "")) <= T.parse_dt(at):
            return
    except (ValueError, TypeError):
        pass
    _drop_ceo_handoff(state, tkt.id)


def _ceo_handoff_pending(tkt: T.Ticket, state: dict, now) -> bool:
    """Передача CEO (`--next ceo`) — ход, пока CEO не ответил: метка живёт до первой записи CEO после неё (или смены
    статуса на не-ожидание). Без ответа дольше INVARIANT_GRACE_S — одна повторная строка CEO, владельца не будим."""
    handoffs = state.get("ceo_handoffs") or {}
    at = handoffs.get(tkt.id)
    if not at:
        return False
    try:
        at_dt = T.parse_dt(at)
    except (ValueError, TypeError):
        handoffs.pop(tkt.id, None)
        return False
    if tkt.status not in ("waiting", "in_review") or any(
            T.author_is(e.author, "ceo") and e.ts >= at_dt.replace(microsecond=0) for e in tkt.log):
        handoffs.pop(tkt.id, None)
        return False
    if (now - at_dt).total_seconds() > INVARIANT_GRACE_S:
        reminded = state.setdefault("ceo_handoff_reminded", {})
        if reminded.get(tkt.id) != at:
            reminded[tkt.id] = at
            append_ceo_inbox(tkt.id, "ждёт-ceo", f"передача CEO без ответа дольше {int(INVARIANT_GRACE_S // 60)} мин "
                             f"(владельца не будим, ход за CEO)", now)
    return True


def _no_move_reason(tkt: T.Ticket, now, state: "dict | None" = None) -> "str | None":
    """TK-076 п.6: у открытого тикета есть ход — роль запущена / next / годный wait_for / передача CEO. Здесь —
    тикет, которого decide() не будит, а хода нет дольше INVARIANT_GRACE_S (от `updated`): причина или None."""
    if tkt.next_role or tkt.status not in ("waiting", "in_review"):
        return None
    try:
        last = T.parse_dt(tkt.header.get("updated", ""))
        # `tickets.py comment` без --next `updated` не двигает: запись CEO и смена статуса — две команды, грейс считаем и от записи
        stamps = [e.ts for e in tkt.log[-1:] if e.ts <= now.replace(microsecond=0)]
        if (now - max([last] + stamps)).total_seconds() <= INVARIANT_GRACE_S:
            return None
    except Exception:
        return None
    if state is not None and _ceo_handoff_pending(tkt, state, now):
        return None
    if tkt.status == "waiting":
        spec = (tkt.header.get("wait_for") or "").strip()
        if not spec:
            return "waiting без wait_for и без next"
        if T.parse_wait_for(spec) is None:
            return f"wait_for не понят ({spec[:80]})"
        return None
    if tkt.reviewer in P.ROLES and _review_returns(state or {}, tkt.id) < MAX_REVIEW_RETURNS:  # предел — вопрос CEO
        return "in_review: последняя запись — ревьюера, статус не сменён, next не задан"
    return None


def enforce_move_invariant(path: Path, tkt: T.Ticket, state: dict, now) -> "T.Ticket":
    """Нарушение инварианта: будим владельца (status → in_progress, запись диспетчера) + строка CEO (раз на эпизод)."""
    reason = _no_move_reason(tkt, now, state)
    if not reason:
        return tkt
    STALLED.append(_stall_title(tkt))
    key = f"{tkt.id}|{tkt.header.get('updated', '')}"
    sig = state.setdefault("invariant_signaled", {})
    if sig.get(tkt.id) != key:
        sig[tkt.id] = key
        append_ceo_inbox(tkt.id, "нет-хода", f"{reason} дольше {int(INVARIANT_GRACE_S // 60)} мин — "
                         + (f"владелец {tkt.owner} разбужен" if tkt.owner in P.ROLES else "владельца-роли нет, нужен CEO"), now)
    if tkt.owner not in P.ROLES:
        return tkt
    T.write_header_updates(path, {"status": "in_progress"}, now=now)
    T.append_log(path, "dispatcher", f"инвариант «у открытого тикета есть ход»: {reason} — владелец разбужен "
                 f"(status: in_progress). Следующий ход обязателен: работа / `--next <роль>` / "
                 f"`tickets.py wait <ID> <форма>` / вопрос владельцу.", now=now)
    return T.read_ticket(path)


def notify_wait_cycle(tkt: T.Ticket, state: dict, now) -> None:
    """TK-076 п.7: уже существующий цикл ожиданий ticket:<ID> → строка CEO (раз в сутки, от тикета с наименьшим ID)."""
    if tkt.status != "waiting":
        return
    parsed = T.parse_wait_for(tkt.header.get("wait_for", ""))
    if not parsed or parsed[0] != "ticket":
        return
    cycle = T.wait_cycle(CFG.TICKETS_DIR, tkt.id, parsed[1])
    if not cycle or tkt.id != min(cycle[:-1]):
        return
    STALLED.append(_stall_title(tkt))
    notified = state.setdefault("ceo_wait_cycle_notified", {})
    prev = notified.get(tkt.id)
    if prev:
        try:
            if now - T.parse_dt(prev) < WAIT_NOTICE_EVERY:
                return
        except ValueError:
            pass
    append_ceo_inbox(tkt.id, "цикл-ожиданий", " -> ".join(cycle) + " — каждый ждёт следующего, никто не пойдёт; разорвать", now)
    notified[tkt.id] = T.now_iso(now)


STALL_ALARM_MIN = 15.0  # CP5: столько минут подряд стоит готовая работа — тревога
# --- побочная работа тика — alarms.py; здесь обёртки подставляют _popen и append_ceo_inbox ----------------------

from alarms import STALLED, _stall_title, _guarded  # noqa: E402,F401
from alarms import owner_questions as _owner_questions  # noqa: E402,F401


def _stall_alarm(state: dict, now, stalled: list) -> None:
    AL.stall_alarm(state, now, stalled, STALL_ALARM_MIN, append_ceo_inbox)


def _daily_report(state: dict, now) -> None:
    AL.daily_report(state, now, _popen)


def _overseer(state: dict, now) -> None:
    AL.overseer(state, now, _popen)


def _account_downtime(state: dict, now, **flags) -> None:
    try:
        downtime.account(state, now, poll_s=POLL_INTERVAL, slo_min=IDLE_SLO_MIN, **flags)  # минуты простоя — в отчёт (CP10)
    except Exception as e:  # счётчик не должен ронять тик
        print(f"[dispatch] downtime: {type(e).__name__}: {e}", file=sys.stderr, flush=True)


def sweep_closed_worktrees() -> None:
    """TK-104: копии закрытых тикетов убираются (грязное — коммитом в ветку); ошибка уборки тик не роняет."""
    try:
        status = {}
        for p in T.list_tickets(CFG.TICKETS_DIR):
            try:
                t = T.read_ticket(p)
                status[t.id] = t.status
            except Exception:
                continue
        for line in WH.sweep(CFG.PROJECT_ROOT, status):
            print(f"[dispatch] worktree: {line}", flush=True)
    except Exception as e:
        print(f"[dispatch] worktree sweep: {type(e).__name__}: {e}", file=sys.stderr, flush=True)


def tick(now=None) -> int:
    now = now or datetime.now().astimezone()
    state = load_state()
    recover_active_runs(state, now)  # диспетчер мог перезапуститься — живые/умершие прогоны из state.json
    process_stop_requests(state, now)  # `tickets.py stop` (CEO): снять запущенную роль до разбора завершённых — не провал
    _poll_running(state, now)
    baseline_done_notified(state)  # v2: историю `done` CEO не пересказываем (один раз, ключ в state.json)
    save_state(state)

    sweep_closed_worktrees()
    unblock_limit_victims(now)
    paused = _limit_paused(state, now)  # TK-070 п.2: пока лимит сессии не сброшен — новых запусков нет, тикеты не трогаем
    candidates = []  # (path, ticket, decision) — кого можно запустить; порядок и лимиты — ниже
    STALLED.clear()
    waiting_nocond = False  # TK-076 п.4: роль ждёт без условия (ни wait_for, ни next) дольше IDLE_GRACE_S
    for path in T.list_tickets(CFG.TICKETS_DIR):
        try:
            tkt = T.read_ticket(path)
        except Exception as e:
            notify_parse_error(path.stem, f"{type(e).__name__}: {e}", state, now)
            continue

        try:  # TK-109 п.15: сбой на одном тикете не останавливает обработку остальных
            # CEO получает строку только по: `next: ceo`, blocked/needs_owner, done.
            # @ceo (и любые @упоминания) в тексте записей — просто текст.
            if tkt.id not in RUNNING and path.stem not in RUNNING:
                # аудит-3: пока владелец ещё работает, его промежуточная запись — не «сдал на ревью»; эскалация — после запуска
                tkt = escalate_review_limit(path, tkt, state, now)
            handle_next_ceo(path, tkt, state, now)
            if tkt.id not in RUNNING and path.stem not in RUNNING:
                tkt = close_after_merge(path, tkt, now) or tkt
            _expire_ceo_handoff(tkt, state)
            notify_status_for_ceo(tkt, state, now)
            notify_done(tkt, state, now)
            notify_wait_for_problem(tkt, state, now)

            tid = tkt.id
            if tid in RUNNING:
                continue
            if tkt.status == "waiting" and not tkt.next_role:
                tkt = note_wait_met(path, tkt, now)  # CP6: задание кончилось — исход в jobs.jsonl, итог (.rc) — в лог тикета
            if (tkt.status == "waiting" and not tkt.next_role and (tkt.header.get("on_met") or "").strip()
                    and check_wait_for(tkt.header.get("wait_for", ""))):
                try:
                    if run_on_met(path, tkt, state, now):
                        save_state(state)
                        continue
                except Exception as e:  # on_met не должен ронять тик; on_met уже очищен — дальше обычный путь
                    print(f"[dispatch] on_met {tid}: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
                tkt = T.read_ticket(path)
            notify_wait_cycle(tkt, state, now)
            decision = decide(tkt, state, now)
            if decision is None:
                if _waits_without_condition(tkt, now):
                    waiting_nocond = True
                    STALLED.append(_stall_title(tkt))
                tkt = enforce_move_invariant(path, tkt, state, now)
                decision = decide(tkt, state, now)
                if decision is None:
                    continue
            haiku_reason = haiku_refused_reason(tkt)
            if haiku_reason:
                T.write_header_updates(path, {"status": "blocked"}, now=now)
                T.append_log(path, "dispatcher", f"{haiku_reason} — задача заблокирована, нужен CEO.", now=now)
                route_ceo_signal(tid, "blocked", haiku_reason, state, now)
                continue
            candidates.append((path, tkt, decision))
        except Exception as e:
            print(f"[dispatch] тикет {path.stem}: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
            notify_parse_error(path.stem, f"сбой тика: {type(e).__name__}: {e}", state, now)

    launched = 0
    throttled = 0
    for path, tkt, decision in sorted(candidates, key=lambda c: _candidate_sort_key(state, c[1], c[2])):
        tid = tkt.id
        if paused or len(RUNNING) >= max_parallel():
            break
        if tid in RUNNING or path.stem in RUNNING:
            continue  # на один тикет — один запуск роли (RUNNING по stem файла; tkt.id из шапки мог разойтись с ним)
        if _role_busy(decision.role):
            continue  # у роли уже предел запусков (на любых задачах) — ждёт следующего тика
        if _rate_limited(state, tid, now, decision.role):
            throttled += 1
            continue  # MAX_RUNS_PER_TICKET_HOUR/MIN_GAP_S — пауза, не ошибка; попробуем следующим тиком
        if decision.header_updates:
            _apply_header_updates(path, decision.header_updates, now)
        launch_run(path, decision.role, state, now, reason=decision.reason)
        launched += 1

    idle_now = not RUNNING and not launched and not paused
    if idle_now:  # готовая работа не взята, и никто не работает — стоит
        STALLED.extend(_stall_title(t) for p_, t, _ in candidates if t.id not in RUNNING and p_.stem not in RUNNING)
    _guarded("тревога", _stall_alarm, state, now, STALLED)
    _account_downtime(state, now, ready_unserved=(len(candidates) - launched - throttled > 0 and idle_now),
                      throttled=(throttled > 0 and idle_now), limit_paused=(bool(paused) and bool(candidates) and not RUNNING),
                      work_present=bool(candidates) or bool(RUNNING), waiting_nocond=waiting_nocond)
    _guarded("отчёт", _daily_report, state, now)
    _guarded("надзиратель", _overseer, state, now)
    _guarded("вопрос владельцу", _owner_questions, state, now)
    state["last_tick"] = T.now_iso(now)  # судья TK-002 п.2а: сторож проверяет диспетчер жив по этому
    save_state(state)
    return launched


USAGE = """Диспетчер задач (v2, 02.10). Запускается из папки плагина; проект — --project <путь>, иначе RPV_PROJECT /
CLAUDE_PROJECT_DIR, иначе ближайший каталог вверх с .claude/roles. Состояние — <проект>/.claude/dispatcher/.
  python <плагин>/.claude/dispatcher/dispatch.py --project <проект>          # цикл раз в 15 с — БОЕВОЙ
  python <плагин>/.claude/dispatcher/dispatch.py --project <проект> --once   # один тик (тоже боевой: может запустить роли)
  python <плагин>/.claude/dispatcher/dispatch.py --help                      # эта справка (ничего не запускает)
Правила — README.md рядом с диспетчером."""


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--help" in argv or "-h" in argv:  # раньше любой аргумент запускал боевой цикл
        print(USAGE)
        return 0
    if not CFG.ensure_project(argv, "dispatch"):     # нет проекта — ошибка с подсказкой, каталоги не создаём
        return 2
    CFG.DISPATCHER_DIR.mkdir(parents=True, exist_ok=True)
    CFG.TICKETS_DIR.mkdir(parents=True, exist_ok=True)
    ok, why = acquire_instance_lock(CFG.PID_FILE)
    if not ok:                                   # второй диспетчер (в т. ч. ручной --once при живом цикле) — не стартуем
        print(f"[dispatch] {why}", file=sys.stderr)
        return 1
    atexit.register(release_instance_lock, CFG.PID_FILE)
    if "--once" in argv:
        n = tick()
        print(f"[dispatch] once: launched={n} running={len(RUNNING)}")
        return 0
    print(f"[dispatch] v2 loop every {POLL_INTERVAL}s, MAX_PARALLEL={max_parallel()}, run timeout "
          f"{RUN_TIMEOUT / 60:.0f} min, CLAUDE_BIN={CLAUDE_BIN}")
    W.WAIT_ASYNC = True
    threading.Thread(target=W._wait_poller, daemon=True, name="wait-poller").start()
    while True:
        try:
            tick()
            _blind_tick()
        except Exception as e:
            print(f"[dispatch] tick error: {type(e).__name__}: {e}", file=sys.stderr)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    if not sys.stdout.isatty():  # демон с перенаправленным выводом: чужой Ctrl+C общей консоли его не убивает (TK-072)
        import signal
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    sys.exit(main())
