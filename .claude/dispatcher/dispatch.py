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
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import project as P  # noqa: E402
import project_paths as CFG  # noqa: E402
import hide  # noqa: E402
import plugin_version as PV  # noqa: E402
import ticket as T  # noqa: E402
import worktree_hygiene as WH  # noqa: E402
import downtime  # noqa: E402
import on_met as ON_MET  # noqa: E402
import waits as W  # noqa: E402
import alarms as AL  # noqa: E402
import signals as SIG  # noqa: E402
import stop as STOP  # noqa: E402
from role_args import (  # noqa: E402,F401 — запуск роли (усилие, MCP, промпт) вынесен в role_args.py; имена для launch_run и тестов
    role_mcp_args, plugin_dir_args, effort_for)
import role_args as RA  # noqa: E402
from limits import (  # noqa: E402,F401 — лимит сессии и ошибки API (распознавание) вынесены в limits.py
    _limit_hit, _api_error, _limit_reset_at, _limit_paused)

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
    _mech_log, append_ceo_inbox, route_ceo_signal, handle_next_ceo, escalate_review_limit, notify_status_for_ceo,
    notify_parse_error, baseline_done_notified, notify_done, haiku_refused_reason)


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
# остаётся на Opus 5.5 (усилие — role_args.ROLE_EFFORT: с 14.10 high). Роль вне словаря и executor haiku — см. launch_run.
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
# `usage.iterations` (см. runlog.context_tokens_last)
CLAUDE_PROJECTS_DIR = Path(PV.claude_dir()) / "projects"


RUNNING = {}  # tid -> {role, popen, pid, started, attempt, run_file, err_file, out_fh, err_fh, reason}
# popen=None у записей, восстановленных из state.json["active_runs"] после перезапуска диспетчера
# (recover_active_runs) — тогда живость и остановка идут по pid (_pid_alive/_kill), не по Popen.




# --- состояние ------------------------------------------------------------------------------

from project_paths import load_state, save_state  # noqa: E402,F401 — файл состояния, пути — CFG


# --- wait_for: само ожидание — waits.py; здесь обёртки подставляют пути проекта --------------------------------

def check_wait_for(spec: str) -> bool:
    """Условие `wait_for` выполнено? Формы — `ticket.parse_wait_for` (README, «v4»); см. waits.check_wait_for."""
    return W.check_wait_for(spec, W.project_where())


def _blind_tick() -> None:
    W.blind_tick(W.project_where(), _mech_log)


# --- ceo-inbox ---------------------------------------------------------------------------------

# Сигнал CEO — строка в ceo-inbox.md (детали; читает `tickets.py inbox`) и короткая копия в ceo-wake.log (на нём Monitor CEO).
REASON_PRIORITY = {"next": 0, "in_progress-resume": 2}










from decide import (  # noqa: E402,F401 — решение «кого будить» вынесено в decide.py; здесь имена и обёртка с подставляемым check_wait_for
    Decision, close_after_merge)
import decide as DEC  # noqa: E402


def _wait_alive(tkt: T.Ticket) -> bool:
    """TK-054: сторож видит задание ожидающего тикета живым; флаг `hold_alive_waits: false` — прежнее поведение."""
    if not P.setting("hold_alive_waits", True):
        return False
    import lifewatch
    return lifewatch.wait_alive(W.project_where().dispatcher_dir, tkt.id, (tkt.header.get("wait_for") or "").strip(), time.time())


def decide(tkt: T.Ticket, state: dict, now) -> "Decision | None":
    return DEC.decide(tkt, state, now, check_wait_for, _wait_alive)


# --- сессия запуска ----------------------------------------------------------------------------

def _resume_store(state: dict, tid: str, role: str) -> dict:
    """session_id/токены контекста на (задача, роль) — для повтора после таймаута."""
    return state.setdefault("ticket_sessions", {}).setdefault(f"{tid}::{role}", {})










import runlog as RL  # noqa: E402
from runlog import read_run_result as _read_run_result  # noqa: E402


def _log_run_summary(*args, **kwargs) -> None:
    RL.log_run_summary(CLAUDE_PROJECTS_DIR, *args, **kwargs)


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
    _add_cost, ticket_cost_spent, add_ticket_cost, _record_cost_event, resolve_run_cost)


def _extra_models(info: dict, critic_on: bool) -> tuple:
    """Судья с включённым критиком (`critic_before_accept`) зовёт агента critic на Sonnet: это не чужая модель (TK-073)."""
    return ("sonnet",) if critic_on and info.get("role") == "judge" else ()


def _model_usage_warning(model_usage_diff: dict, expected: str = None, also: tuple = ()) -> str:
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
    bad = [m for m in model_usage_diff if not any(f in str(m).lower() for f in (expected, *also))]
    if bad:
        return f"modelUsage этого запуска содержит модели без «{expected}» ({', '.join(bad)})"
    return None


from procs import (  # noqa: E402,F401 — процессы вынесены в procs.py; здесь имена для вызовов из dispatch
    _proc_start, _pid_alive, _kill, acquire_instance_lock, release_instance_lock, _proc_alive, _kill_proc)


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
    stopped = bool(state.get("stopped_runs", {}).pop(tid, None))
    rotated = bool(sid and not attempt and not stopped)
    if stopped or rotated:
        sid = None
    extra_note = RA.launch_notes(extra_note, role, reason, stopped, rotated, STOP_NOTE)
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

    env = RA.role_env(role, tid)

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








import finish as FIN  # noqa: E402
from finish import (  # noqa: E402,F401 — что делать с тикетом после запуска вынесено в finish.py; имена для _finish_run и тестов
    handle_api_error as _handle_api_error)


def _finish_role_part(tid: str, info: dict, state: dict, now, timed_out: bool, result: dict) -> None:
    FIN.finish_role_part(tid, info, state, now, timed_out, result, FIN.Env(
        launch_run, check_wait_for, CLAUDE_PROJECTS_DIR, MAX_IDLE_RUNS, MAX_SAME_STATUS_RUNS))









# --- лимит сессии (TK-070 п.2): ответ 429 «You've hit your session limit · resets 5am (Asia/Tbilisi)» — не холостой ход ---

















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
    model_warn = _model_usage_warning(model_usage_diff, expected_model,
                                      _extra_models(info, bool(P.setting("critic_before_accept", False))))
    if model_warn:
        route_ceo_signal(tid, "model", model_warn, state, now)
    if info.get("role") == "judge" and P.setting("critic_before_accept", False):  # TK-073: запуск критика — в журнал с ценой
        import critic_gate
        critic_gate.record_run(CFG.PROJECT_ROOT, tid, Path(info["run_file"]).name, model_usage_diff, result.get("subagent_stats"))

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


from stop import (  # noqa: E402,F401 — остановка и подхват запусков вынесены в stop.py; обёртки подставляют идущие запуски
    _reset_same_status)


def _poll_running(state: dict, now) -> None:
    STOP.poll_running(state, now, RUNNING, _proc_alive, _kill_proc, _finish_run, RUN_TIMEOUT)


def recover_active_runs(state: dict, now) -> None:
    STOP.recover_active_runs(state, now, RUNNING, _finish_run, _proc_start, _pid_alive)


def process_stop_requests(state: dict, now) -> None:
    STOP.process_stop_requests(state, now, RUNNING, _stop_run, _finish_run, append_ceo_inbox)


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
import handoff as HO  # noqa: E402
from handoff import (  # noqa: E402,F401 — ход у открытого тикета вынесен в handoff.py; имена для tick и тестов
    _waits_without_condition, _drop_ceo_handoff, enforce_move_invariant, notify_wait_cycle)


def _expire_ceo_handoff(tkt: T.Ticket, state: dict) -> None:
    HO.expire_ceo_handoff(tkt, state, RUNNING)

LIMIT_PAUSE_MAX = timedelta(hours=1)  # пауза 429 не длиннее часа: дальше пробный запуск (429 бесплатен), новая метка — новая пауза
















STALL_ALARM_MIN = 15.0  # CP5: столько минут подряд стоит готовая работа — тревога
# --- побочная работа тика — alarms.py; здесь обёртки подставляют _popen и append_ceo_inbox ----------------------

from alarms import STALLED, _stall_title, _guarded, owner_questions as _owner_questions  # noqa: E402,F401


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


def _collect_candidates(state: dict, now) -> tuple:
    """Обход тикетов: сигналы CEO, ожидания, решение «кого будить»; (кандидаты [(путь, тикет, решение)], ждёт без условия)."""
    candidates = []  # (path, ticket, decision) — кого можно запустить; порядок и лимиты — в _launch_candidates
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
                tkt = close_after_merge(path, tkt, now, state) or tkt
            _expire_ceo_handoff(tkt, state)
            notify_status_for_ceo(tkt, state, now)
            notify_done(tkt, state, now)
            notify_wait_for_problem(tkt, state, now)

            tid = tkt.id
            if tid in RUNNING:
                continue
            if tkt.status == "waiting" and not tkt.next_role:
                tkt = W.note_wait_met(path, tkt, now, W.project_where())  # CP6: задание кончилось — исход в jobs.jsonl, итог (.rc) — в лог тикета
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
    return candidates, waiting_nocond


def _launch_candidates(candidates: list, state: dict, now, paused) -> tuple:
    """Запуск кандидатов по очереди приоритета с пределами (параллельность, роль, часовой троттлинг); (запущено, придержано)."""
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
    return launched, throttled


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
    candidates, waiting_nocond = _collect_candidates(state, now)
    launched, throttled = _launch_candidates(candidates, state, now, paused)

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
