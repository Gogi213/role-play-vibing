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
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import project as P  # noqa: E402
import ticket as T  # noqa: E402

# --- конфигурация (константы — тесты подменяют их прямо на модуле) ------------------------

CODE_DIR = Path(__file__).resolve().parent  # где лежит сам диспетчер (папка плагина) — к проекту отношения не имеет
# Пути проекта (PROJECT_ROOT, TICKETS_DIR, DISPATCHER_DIR — каталог СОСТОЯНИЯ в проекте и остальные файлы) выставляет
# configure_project(): при импорте — по --project/RPV_PROJECT/CLAUDE_PROJECT_DIR/текущему каталогу, в main() — по флагу.
PROJECT_ROOT = DISPATCHER_DIR = TICKETS_DIR = STATE_FILE = PID_FILE = RUNS_DIR = RUNS_LOG = None
CEO_INBOX = CEO_WAKE_LOG = None
PROJECT_FOUND = False  # проект найден при импорте (флаг/окружение/поиск вверх); False — CLI откажет с подсказкой


def configure_project(root) -> Path:
    """Корень проекта и все пути состояния от него (ничего не создаёт — каталоги появляются при записи)."""
    global PROJECT_ROOT, DISPATCHER_DIR, TICKETS_DIR, STATE_FILE, PID_FILE, RUNS_DIR, RUNS_LOG, CEO_INBOX, CEO_WAKE_LOG
    PROJECT_ROOT = Path(root).expanduser().resolve()
    DISPATCHER_DIR = PROJECT_ROOT / ".claude" / "dispatcher"
    TICKETS_DIR = PROJECT_ROOT / ".claude" / "tickets"
    STATE_FILE = DISPATCHER_DIR / "state.json"
    PID_FILE = DISPATCHER_DIR / "dispatch.pid"  # замок единственного экземпляра диспетчера (см. acquire_instance_lock)
    RUNS_DIR = DISPATCHER_DIR / "runs"
    RUNS_LOG = DISPATCHER_DIR / "runs.log"
    CEO_INBOX = DISPATCHER_DIR / "ceo-inbox.md"
    CEO_WAKE_LOG = DISPATCHER_DIR / "ceo-wake.log"  # короткая копия каждой строки ceo-inbox — CEO держит на ней Monitor
    return PROJECT_ROOT


try:
    configure_project(P.resolve_project())
    PROJECT_FOUND = True
except P.ProjectNotFound:                 # импорт не падает; пути — заглушка от cwd, ничего не создаётся
    configure_project(Path.cwd())


def ensure_project(argv, prog: str) -> bool:
    """Точка входа CLI: проект задан флагом `--project` (переключаем пути) или найден при импорте; иначе —
    подсказка в stderr и False (вызывающий выходит с кодом 2, не создав ни одного каталога)."""
    explicit = P.project_arg(argv)
    if explicit:
        configure_project(explicit)
        return True
    if not PROJECT_FOUND:
        print(f"[{prog}] ошибка: {P.NOT_FOUND_HINT}", file=sys.stderr)
        return False
    return True

CLAUDE_BIN = os.environ.get("CLAUDE_BIN") or shutil.which("claude") or "claude"  # имя из PATH, без зашитых путей
PID_EXPECT_NAME = "claude"  # _pid_alive: подстрока имени образа процесса; тесты подменяют на "python"
POLL_INTERVAL = float(P.env("DISPATCH_INTERVAL", "15"))
# v2 (02.10, аудит ролевой системы): всего параллельно ≤ 3 запусков и не больше ОДНОГО запуска на роль
# (по всем тикетам сразу, см. _role_busy); таймаут запуска 20 мин (было 40 — фоновые помощники в `-p` висели
# до убийства, а цена убитого запуска в учёте — $0).
MAX_PARALLEL = int(P.env("DISPATCH_MAX_PARALLEL", "3"))
RUN_TIMEOUT = float(P.env("DISPATCH_TIMEOUT", str(20 * 60)))

# Защита от петли (владелец 27.09, v1.1). MAX_RUNS_PER_TICKET_HOUR/MIN_GAP_S — троттлинг решений (а)-(г):
# тикет просто пропускается этот тик, без ceo-inbox (не ошибка, а пауза); ретраи правила (д) их не считают —
# они и так ограничены одной попыткой. Денежных ограничений НЕТ вовсе: В-149 снял часовой и суточный лимит,
# В-173 (03.10) — на тикет, владелец 03.10 («бюджет до конца убирай») — и потолок запуска; траты только считаются
# (runs.log, state.json).
MAX_RUNS_PER_TICKET_HOUR = int(P.env("DISPATCH_MAX_RUNS_PER_TICKET_HOUR", "6"))
MIN_GAP_S = float(P.env("DISPATCH_MIN_GAP_S", "60"))
# Тормоза цикла (аудит 03.10) — по ЧИСЛУ запусков подряд на (тикет, роль), не по деньгам. Числа НАЗНАЧЕНЫ CEO 03.10,
# не измерены: MAX_SAME_STATUS_RUNS (12) — роль пишет запись, а статус (in_progress; waiting при выполненном wait_for) не
# меняется → на половине (SAME_STATUS_WARN_RUNS, 0 = MAX // 2 = 6) одна строка CEO «loop-warning», на MAX — тикет
# blocked + строка CEO; MAX_IDLE_RUNS (2) — запуски без записи и без смены статуса (холостой ход) → blocked (первый
# холостой — обычный один повтор, см. _finish_role_part).
MAX_SAME_STATUS_RUNS = int(P.env("DISPATCH_MAX_SAME_STATUS_RUNS", "12"))
SAME_STATUS_WARN_RUNS = int(P.env("DISPATCH_SAME_STATUS_WARN_RUNS", "0"))
MAX_IDLE_RUNS = int(P.env("DISPATCH_MAX_IDLE_RUNS", "2"))
# Пинг-понг ревью (аудит-2): сколько раз ревьюер может вернуть работу владельцу (in_review → вернул → снова in_review).
# После MAX_REVIEW_RETURNS возвратов тикет, снова пришедший на ревью, ревьюеру не отдаётся: запись dispatcher +
# `next: ceo` (одна строка CEO). Число НАЗНАЧЕНО CEO 03.10, не измерено.
MAX_REVIEW_RETURNS = int(P.env("DISPATCH_MAX_REVIEW_RETURNS", "3"))

# Модель и перерасход (владелец 27.09, v1.2 — пилот Судьи на умолчаниях CLI стоил $6,8 на Fable 5.1
# xhigh): модель и усилие теперь ВСЕГДА явно в команде запуска, не полагаемся на умолчание CLI.
# В-153 (02.10): все роли — Sonnet 5.5 с усилием xhigh. Усилие переопределяемо через
# RPV_DISPATCH_EFFORT=judge:xhigh,engineer:high (или одно значение — на все роли).
CLAUDE_MODEL = P.env("DISPATCH_MODEL", "claude-sonnet-5-5")
# v2 (02.10): усилие — по виду задачи (статья «spending your effort»): исследователь и инженер — high, Судья
# (вердикт) — xhigh; на тикете переопределяется полем `effort: low|medium|high|xhigh` в шапке
# (`tickets.py new --effort`), см. effort_for().
ROLE_EFFORT = {"judge": "xhigh", "engineer": "high", "researcher": "high"}


def _parse_role_map(spec: str, base: dict) -> dict:
    """«role:val,role:val» → правки к base; одно значение без «:» — на все роли base."""
    out = dict(base)
    spec = (spec or "").strip()
    if not spec:
        return out
    if ":" not in spec:
        return {r: spec for r in out}
    for _pair in spec.split(","):
        _role, _, _val = _pair.partition(":")
        if _role.strip() and _val.strip():
            out[_role.strip()] = _val.strip()
    return out


ROLE_EFFORT = _parse_role_map(P.env("DISPATCH_EFFORT", ""), ROLE_EFFORT)

# В-153 уточнение (02.10, v1.6.1): на Sonnet 5.5 — только те, кого рационально; Судья (проверяет всех)
# остаётся на Opus 5.5 xhigh. Переопределение: RPV_DISPATCH_ROLE_MODEL=judge:claude-opus-5-5,engineer:...
# (или одно значение — на все роли). Роль вне словаря и executor haiku — см. launch_run.
ROLE_MODEL = _parse_role_map(P.env("DISPATCH_ROLE_MODEL", ""),
                             {"judge": "claude-opus-5-5", "engineer": CLAUDE_MODEL, "researcher": CLAUDE_MODEL})


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
CLAUDE_HAIKU_MODEL = P.env("DISPATCH_HAIKU_MODEL", "claude-haiku-4-5-20251001")
HAIKU_ALLOWED_KINDS = {"file-move", "table-format", "publish"}

ROLE_KEYS = ("researcher", "engineer", "judge")  # роли, которых диспетчер запускает; ceo — человек/CEO-сессия

# Область сессии на роль (владелец 27.09): "ticket" — сессия на (задача, роль), --resume в пределах
# задачи; "role" — одна долгая сессия роли на ВСЕ задачи (в промпте каждый раз названа текущая задача).
# v2 (02.10): Судья тоже "ticket" — одна сессия на все задачи копила контекст чужих тикетов (аудит).
# Не больше одного запуска на роль теперь действует всегда (_role_busy), а не только при "role".
# Переопределяемо через RPV_DISPATCH_SESSION_SCOPE=judge:role,engineer:ticket (прежнее имя — ALPHA_DISPATCH_…).
SESSION_SCOPE = {"judge": "ticket", "researcher": "ticket", "engineer": "ticket"}
if P.env("DISPATCH_SESSION_SCOPE"):
    for _pair in P.env("DISPATCH_SESSION_SCOPE").split(","):
        _role, _, _scope = _pair.partition(":")
        if _role and _scope:
            SESSION_SCOPE[_role.strip()] = _scope.strip()

# Ротация долгой сессии: если контекст прошлого запуска (input + cache_read + cache_creation, по usage
# из JSON-вывода claude) превысил это число токенов — следующий запуск роли начинает новую сессию (без
# --resume) и получает в промпте напоминание перечитать блокнот и прежние решения по нужной задаче.
# v2: 250 000 → 120 000 — при окне Sonnet 200 тыс. прежний порог не срабатывал (TK-025: одна сессия $36).
ROTATE_TOKENS = int(P.env("DISPATCH_ROTATE_TOKENS", "120000"))
# транскрипты сессий claude (`<projects>/<проект>/<session_id>.jsonl`): контекст последнего хода, когда в JSON нет
# `usage.iterations` (см. _context_tokens_last)
CLAUDE_PROJECTS_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude")) / "projects"

# v2 (02.10): промпт без призыва @-упоминать роли — будит только явный `--next`; читать шапку, описание и
# последние записи лога (старое — в archive/<ID>-log.md); никаких фоновых помощников/задач внутри сессии.
PROMPT_TEMPLATE = (
    "Ты — {role} команды. Устав: .claude/roles/{role}.md, блокнот: .claude/roles/notes/{role}.md. "
    "Задача: .claude/tickets/{tid}.md — прочитай шапку, описание и последние записи «## Лог» (старые записи "
    "лежат в .claude/tickets/archive/{tid}-log.md — grep только при необходимости). Лимит этого запуска — "
    "{timeout_min} мин. Не запускай в сессии фоновых помощников и фоновых задач; долгая работа — фоновый "
    "процесс на машине (systemd-run) + status: waiting + wait_for, и выйди, не жди в сессии. Трать минимум: "
    "самый короткий путь к результату задачи; траты каждого запуска записываются и сравниваются с "
    "результатом. Сделай следующий шаг и запиши итог командой "
    "`{tickets_cli} comment {tid} --author {role} --text \"...\"` (что сделал, что "
    "дальше) ДО истечения лимита — запись с твоим заголовком обязательна, частичный прогресс не провал; "
    "status/wait_for в шапке обнови сама (не «todo», если работа не закончена). Передать работу другой "
    "роли — один раз `--next <researcher|engineer|judge>` в той же команде comment, без копий «для "
    "сведения»; владелец после передачи ставит status: waiting (иначе его запустят снова). `--next ceo` — "
    "только если задача заблокирована или нужно решение владельца. @упоминания в тексте никого не будят."
)

RUNNING = {}  # tid -> {role, popen, pid, started, attempt, run_file, err_file, out_fh, err_fh, reason}
# popen=None у записей, восстановленных из state.json["active_runs"] после перезапуска диспетчера
# (recover_active_runs) — тогда живость и остановка идут по pid (_pid_alive/_pid_kill), не по Popen.


@dataclass
class Decision:
    role: str
    reason: str
    header_updates: dict = None


# --- состояние ------------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(STATE_FILE)


# --- wait_for ---------------------------------------------------------------------------------

def check_wait_for(spec: str) -> bool:
    spec = (spec or "").strip()
    if not spec:
        return False
    if spec.startswith("file:"):
        p = spec[len("file:"):].strip()
        path = Path(p)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        return path.exists()
    if spec.startswith("deck:"):
        return _deck_file_exists(spec[len("deck:"):].strip())
    if spec.startswith("ticket:"):
        return _other_ticket_done(spec[len("ticket:"):].strip())
    return False  # незнакомые формы (в т.ч. старое `mention`) сами не снимаются — тикет ждёт явного `next`


def _other_ticket_done(other_id: str) -> bool:
    """`wait_for: ticket:<ID>` — ждём, пока другой тикет дойдёт до status: done (судья 27.09, «можно потом»:
    зависимости T-XX жили только прозой TASKS.md, диспетчер их не видел)."""
    other_path = TICKETS_DIR / f"{other_id}.md"
    if not other_path.exists():
        return False
    try:
        return T.read_ticket(other_path).status == "done"
    except Exception:
        return False


def _remote_test_arg(remote_path: str) -> str:
    """`test -e` аргумент: `~`/`~/...` — без кавычек вокруг тильды, иначе remote-шелл не раскроет её
    в $HOME (shlex.quote экранирует и тильду тоже — поймано боевым вызовом v1.1, 27.09)."""
    remote_path = remote_path.strip()
    if remote_path == "~":
        return "~"
    if remote_path.startswith("~/"):
        rest = remote_path[1:]  # оставляем ведущий '~' сырым, остальное — безопасно экранируем
        return "~" + shlex.quote(rest)
    return shlex.quote(remote_path)


_DECK_CACHE = {}  # remote_path -> (time.time() отметка, результат) — см. _deck_file_exists
DECK_CHECK_CACHE_S = float(P.env("DISPATCH_DECK_CACHE_S", "60"))


def _deck_file_exists(remote_path: str) -> bool:
    # Судья 27.09 («можно потом»): без кэша ssh дёргается на каждый ждущий тикет каждые 15 с —
    # кэшируем результат на DECK_CHECK_CACHE_S, как deck_alert() в role_memory.py (15 мин там,
    # здесь короче — это условие продолжения работы, не редкая тревога).
    cached = _DECK_CACHE.get(remote_path)
    now_ts = time.time()
    if cached and (now_ts - cached[0]) < DECK_CHECK_CACHE_S:
        return cached[1]
    # Машина для ssh-проверок — только из окружения, без умолчаний: нет RPV_DECK_HOST (прежнее ALPHA_DECK_HOST) — проверка выключена.
    host = P.env("DECK_HOST")
    if not host:
        return False
    key = P.env("DECK_KEY")
    known_hosts = P.env("DECK_KNOWN_HOSTS")
    cmd = (["ssh"] + (["-i", key] if key else []) + (["-o", f"UserKnownHostsFile={known_hosts}"] if known_hosts else [])
           + ["-o", "BatchMode=yes", "-o", "ConnectTimeout=8", host, f"test -e {_remote_test_arg(remote_path)}"])
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=15)
        result = r.returncode == 0
    except Exception:
        result = False
    _DECK_CACHE[remote_path] = (now_ts, result)
    return result


# --- ceo-inbox ---------------------------------------------------------------------------------

def append_ceo_inbox(tid: str, kind: str, note: str, now=None) -> None:
    CEO_INBOX.parent.mkdir(parents=True, exist_ok=True)
    with open(CEO_INBOX, "a", encoding="utf-8") as fh:
        fh.write(f"- {T.now_iso(now)} {tid} [{kind}] {note}\n")
    # ceo-wake.log — короткая (время, задача, причина) копия для Monitor CEO; ceo-inbox.md остаётся источником деталей
    with open(CEO_WAKE_LOG, "a", encoding="utf-8") as fh:
        fh.write(f"{T.now_iso(now)} {tid} {kind}\n")


# --- таблица правил «вид сигнала → будить / сводка» (судья TK-002 п.3, взамен привратника TypeSafe) --
#
# В-85: классифицирует КОД по виду сигнала (`kind` из append_ceo_inbox), не модель. Судья отверг
# привратник на Jev в предложенном виде — асимметрия цены ошибок (пропуск сигнала стоит часы простоя
# команды, лишнее пробуждение — центы); почти все виды здесь структурные, не свободный текст. Ничего не
# отбрасывается: "summary" копится и уходит одной строкой не реже SUMMARY_EVERY_HOURS — не молчание.
# Неизвестный вид (кто-то добавит новый append_ceo_inbox без обновления таблицы) — по умолчанию "wake",
# безопасная сторона асимметрии.
SIGNAL_SUMMARY_KINDS = {"model"}  # уже само по себе диагностика/лог, не требует немедленной реакции
SUMMARY_EVERY_HOURS = float(P.env("DISPATCH_SUMMARY_HOURS", "1"))


def classify_signal(kind: str) -> str:
    return "summary" if kind in SIGNAL_SUMMARY_KINDS else "wake"


def flush_pending_summary(state: dict, now) -> None:
    pending = state.get("pending_summary") or []
    if pending:
        line = f"- {T.now_iso(now)} * [summary] {len(pending)} сигнал(ов): " + " | ".join(pending)
        CEO_INBOX.parent.mkdir(parents=True, exist_ok=True)
        with open(CEO_INBOX, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        with open(CEO_WAKE_LOG, "a", encoding="utf-8") as fh:
            fh.write(f"{T.now_iso(now)} * summary({len(pending)})\n")
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
    append_ceo_inbox(tkt.id, "next-ceo", f"{who}{_first_line(last.text if last else '')}", now)
    T.write_header_updates(path, {"next": ""}, now=now, stamp_updated=False)


def escalate_review_limit(path: Path, tkt: T.Ticket, state: dict, now) -> T.Ticket:
    """Пинг-понг ревью: тикет вернулся на ревью (done/in_review при reviewer) после MAX_REVIEW_RETURNS возвратов —
    ревьюера не будим; запись dispatcher + `next: ceo` (одну строку CEO пишет handle_next_ceo этого же тика), статус
    in_review. Только когда последняя запись — владельца (он отправил работу на ревью): запись CEO/ревьюера/dispatcher
    не повод. Явный `next` (CEO будит ревьюера или другую роль) не перебиваем. Возвращает свежий тикет."""
    if tkt.status not in ("done", "in_review") or tkt.reviewer not in ROLE_KEYS:
        return tkt
    returns = _review_returns(state, tkt.id)
    if returns < MAX_REVIEW_RETURNS or tkt.next_role in ROLE_KEYS or tkt.next_role == "ceo":
        return tkt
    last_author = tkt.log[-1].author if tkt.log else ""
    if not T.author_is(last_author, tkt.owner):
        return tkt
    T.append_log(path, "dispatcher",
                 f"Ревьюер ({tkt.reviewer}) вернул работу {returns} раз подряд — на новый круг не будим. Решение за CEO: "
                 f"ещё один круг (`tickets.py comment {tkt.id} --author ceo --text \"...\" --next {tkt.reviewer}`) "
                 "либо принять/закрыть самому.", now=now)
    T.write_header_updates(path, {"status": "in_review", "next": "ceo"}, now=now)
    return T.read_ticket(path)


def notify_status_for_ceo(tkt: T.Ticket, state: dict, now) -> None:
    if tkt.status not in ("blocked", "needs_owner"):
        return
    notified = state.setdefault("ceo_status_notified", {})
    marker = f"{tkt.status}@{tkt.header.get('updated', '')}"
    if notified.get(tkt.id) == marker:
        return
    reason = (tkt.log[-1].text.splitlines()[0][:200] if tkt.log else "")
    append_ceo_inbox(tkt.id, tkt.status, reason, now)
    notified[tkt.id] = marker


def notify_parse_error(tid: str, err_text: str, state: dict, now) -> None:
    """Дедуп по (тикет, текст ошибки) — судья 27.09, п.5 «обязательно»: без дедупа сломанный вручную
    тикет пишет строку в ceo-inbox.md/ceo-wake.log КАЖДЫЙ тик (симуляция: 240/час при POLL_INTERVAL=15с)."""
    notified = state.setdefault("ceo_parse_error_notified", {})
    if notified.get(tid) == err_text:
        return
    append_ceo_inbox(tid, "parse-error", err_text, now)
    notified[tid] = err_text


def _review_returns(state: dict, tid: str) -> int:
    return int((state or {}).get("review_returns", {}).get(tid, 0))


def _review_pending(tkt: T.Ticket, state: dict = None) -> bool:
    """done при заданном reviewer, но последняя запись лога не от ревьюера (и не dispatcher) — правило (г)
    ещё запустит ревью: задача не закончена. На пределе возвратов (MAX_REVIEW_RETURNS) ревьюера уже не будят —
    ревью не «ожидается», решает CEO."""
    if tkt.status != "done" or tkt.reviewer not in ROLE_KEYS:
        return False
    if state is not None and _review_returns(state, tkt.id) >= MAX_REVIEW_RETURNS:
        return False
    last_author = tkt.log[-1].author.lower() if tkt.log else None
    return last_author not in (tkt.reviewer.lower(), "dispatcher")


def _done_marker(tkt: T.Ticket) -> str:
    return f"done@{tkt.header.get('updated', '')}"


def baseline_done_notified(state: dict) -> None:
    """Первый тик после перехода на v2: уже закрытые тикеты — «известные», иначе CEO получит по строке `done`
    на каждый тикет истории. Ключ `ceo_done_notified` есть → ничего не делаем."""
    if "ceo_done_notified" in state:
        return
    notified = state.setdefault("ceo_done_notified", {})
    for path in T.list_tickets(TICKETS_DIR):
        try:
            tkt = T.read_ticket(path)
        except Exception:
            continue
        if tkt.status == "done":
            notified[tkt.id] = _done_marker(tkt)


def notify_done(tkt: T.Ticket, state: dict, now) -> None:
    """v2 (02.10): `done` — одна строка CEO, когда работа реально закончена: ревьюера нет (теперь норма —
    `tickets.py new` не ставит reviewer по умолчанию) либо ревью уже состоялось. Если ждёт ревью — молчим,
    строка придёт после вердикта. Дедуп по (задача, `updated`)."""
    if tkt.status != "done" or _review_pending(tkt, state):
        return
    notified = state.setdefault("ceo_done_notified", {})
    marker = _done_marker(tkt)
    if notified.get(tkt.id) == marker:
        return
    title = (tkt.header.get("title") or "")[:80]
    last = tkt.log[-1] if tkt.log else None
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
REASON_PRIORITY = {"next": 0, "in_progress-resume": 2}


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
    if nxt in ROLE_KEYS:
        return Decision(role=nxt, reason="next", header_updates={"next": ""})

    status = tkt.status
    owner = tkt.owner

    # (а) todo → owner (роль-владелец тикета)
    if status == "todo" and owner in ROLE_KEYS:
        return Decision(role=owner, reason="todo")

    # (а') in_progress без активного запуска → владелец, чтобы продолжить многошаговую задачу (судья
    # 27.09, п.2 «обязательно»: раньше такой тикет замирал после первой сессии — устав ролей обещает
    # продолжение другой сессией, а диспетчер никого не будил). Троттлинг — MIN_GAP_S/MAX_RUNS_PER_TICKET_HOUR
    # в tick(), как у любого решения; уходит через явную смену status (done/waiting/blocked/…).
    if status == "in_progress" and owner in ROLE_KEYS:
        return Decision(role=owner, reason="in_progress-resume")

    # (в) waiting и условие wait_for выполнено → owner
    if status == "waiting" and owner in ROLE_KEYS:
        if check_wait_for(tkt.header.get("wait_for", "")):
            return Decision(role=owner, reason="wait_for-met")
        return None

    # (г) done или in_review при заданном reviewer → будит ревьюера (done ставит in_review) — но не когда последняя
    # запись лога уже от самого ревьюера (или dispatcher): это штатный конец состоявшегося ревью, не новый раунд.
    # Раньше проверялось по updated-таймстампу — тот становится новее записи ревьюера, стоит роли проставить
    # status ПОСЛЕ append_log, и (г) будило ревьюера повторно за его же вердикт (судья 27.09, п.3 «обязательно»).
    # in_review (аудит 03.10): ревьюер упал/убит без записи — тикет не висит вечно; повторы режут MAX_IDLE_RUNS и
    # MAX_SAME_STATUS_RUNS.
    if status in ("done", "in_review") and tkt.reviewer in ROLE_KEYS:
        reviewer = tkt.reviewer
        last_author = tkt.log[-1].author if tkt.log else ""
        if T.author_is(last_author, reviewer) or last_author.lower() == "dispatcher":
            return None
        if _review_returns(state, tkt.id) >= MAX_REVIEW_RETURNS:
            return None  # пинг-понг: ревьюера не будим, тикет уходит CEO (escalate_review_limit в tick)
        return Decision(role=reviewer, reason="review",
                        header_updates={"status": "in_review"} if status == "done" else None)

    return None


# --- область сессии (SESSION_SCOPE) ------------------------------------------------------------

def _resume_store(state: dict, tid: str, role: str) -> dict:
    """session_id/токены контекста: при scope="role" — общие на роль, при "ticket" — на (задача, роль)."""
    if SESSION_SCOPE.get(role, "ticket") == "role":
        return state.setdefault("role_sessions", {}).setdefault(role, {})
    return state.setdefault("ticket_sessions", {}).setdefault(f"{tid}::{role}", {})


def _tokens_of(d: dict) -> int:
    d = d or {}
    return (int(d.get("input_tokens") or 0) + int(d.get("cache_read_input_tokens") or 0) +
            int(d.get("cache_creation_input_tokens") or 0))


def _context_tokens_sum(usage: dict) -> int:
    """Сумма input+cache_read+cache_creation по ВСЕМУ usage из JSON `claude -p` — это сумма по всем
    ходам запуска, не контекст одного хода (см. _context_tokens_last). Для runs.log (ctx_sum=)."""
    return _tokens_of(usage)


def _transcript_last_turn_tokens(session_id) -> int:
    """Контекст ПОСЛЕДНЕГО хода сессии по её транскрипту (usage последнего ответа модели); 0 — транскрипта нет."""
    if not session_id or not re.fullmatch(r"[A-Za-z0-9_-]+", str(session_id)):
        return 0
    try:
        path = next(CLAUDE_PROJECTS_DIR.glob(f"*/{session_id}.jsonl"), None)
        if path is None:
            return 0
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            start = max(0, fh.tell() - 600_000)
            fh.seek(start)
            lines = fh.read().decode("utf-8", "replace").splitlines()
        if start:
            lines = lines[1:]  # первая строка хвоста может быть оборвана
        for line in reversed(lines):
            if '"usage"' not in line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if row.get("isSidechain"):
                continue
            n = _tokens_of((row.get("message") or {}).get("usage"))
            if n:
                return n
    except Exception:
        pass
    return 0


def _context_tokens_last(result: dict) -> int:
    """Контекст, который реально понесёт следующий `--resume` — последний ход ЭТОГО запуска, не сумма
    по всем его ходам: usage в JSON claude -p суммирует по ходам, и на многоходовом запуске это
    завышает контекст в разы, рвя ротацию раньше времени (CEO 27.09, живой прогон: ctx_sum=333 886
    при реальном контексте хода ~52 тыс.). usage.iterations[-1] — контекст последнего хода; нет
    iterations (аудит 03.10) — последний ход из транскрипта сессии; нет и его — запасная оценка
    ctx_sum // num_turns (среднее по ходам, занижена: см. _context_tokens_for_store)."""
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


def _context_tokens_for_store(result: dict, previous: int, session_id=None) -> int:
    """Что записать в `last_context_tokens` сессии после запуска (аудит 03.10): нет JSON (таймаут/убит) или usage
    пуст (ответ-ошибка) — НЕ обнулять; аудит-2: если id сессии известен (`session_id` — та, что продолжал запуск, либо
    из JSON), последний ход берётся из её транскрипта (убитая сессия успела записать ходы), а не старое число;
    транскрипта нет — остаётся последнее известное. Есть ход (iterations/транскрипт) — он; иначе запасная
    оценка-среднее, но не ниже известного (контекст сессии без сжатия только растёт)."""
    usage = (result or {}).get("usage") or {}
    if not _tokens_of(usage):
        last = _transcript_last_turn_tokens(session_id or (result or {}).get("session_id"))
        return last if last else previous
    iterations = usage.get("iterations")
    if isinstance(iterations, list) and iterations and _tokens_of(iterations[-1]):
        return _tokens_of(iterations[-1])
    last = _transcript_last_turn_tokens(result.get("session_id"))
    if last:
        return last
    return max(previous, _context_tokens_last(result))


def _role_busy(role: str) -> bool:
    """v2 (02.10): не больше ОДНОГО активного запуска на роль по всем тикетам сразу (раньше — только при
    scope="role"; аудит: одну задачу вели три сессии Исследователя, до 6 запусков параллельно). Занята —
    запуск ждёт следующего тика."""
    return any(info["role"] == role for info in RUNNING.values())


# --- защита от петли и перерасхода (v1.1) --------------------------------------------------

def _record_launch(state: dict, tid: str, now) -> None:
    hist = state.setdefault("launch_history", {}).setdefault(tid, [])
    hist.append(T.now_iso(now))
    cutoff = now - timedelta(hours=2)  # храним немного с запасом сверх окна MAX_RUNS_PER_TICKET_HOUR
    state["launch_history"][tid] = [t for t in hist if T.parse_dt(t) > cutoff]


def _rate_limited(state: dict, tid: str, now) -> bool:
    """MAX_RUNS_PER_TICKET_HOUR / MIN_GAP_S — троттлинг решений (а)-(г); ретраи (д) их не проходят."""
    hist = [T.parse_dt(t) for t in state.get("launch_history", {}).get(tid, [])]
    if not hist:
        return False
    if len([t for t in hist if (now - t) < timedelta(hours=1)]) >= MAX_RUNS_PER_TICKET_HOUR:
        return True
    return (now - max(hist)).total_seconds() < MIN_GAP_S


def _today(now) -> str:
    return now.strftime("%Y-%m-%d")


def _add_cost(state: dict, now, cost) -> None:
    if not cost:
        return
    daily = state.setdefault("daily_cost", {})
    day = _today(now)
    daily[day] = round(daily.get(day, 0.0) + float(cost), 6)


def ticket_cost_spent(state: dict, tid: str) -> float:
    return state.get("ticket_cost", {}).get(tid, 0.0)


def add_ticket_cost(state: dict, tid: str, cost) -> None:
    if not cost:
        return
    costs = state.setdefault("ticket_cost", {})
    costs[tid] = round(costs.get(tid, 0.0) + float(cost), 6)


def _record_cost_event(state: dict, now, cost) -> None:
    """Учёт: скользящее окно по ВСЕМ ролям — история (время, сумма), обрезаем с запасом; показатель «за час»
    в `tickets.py status`, никого не блокирует."""
    if not cost:
        return
    hist = state.setdefault("cost_history", [])
    hist.append([T.now_iso(now), float(cost)])
    cutoff = now - timedelta(hours=2)
    state["cost_history"] = [e for e in hist if T.parse_dt(e[0]) > cutoff]


def _rolling_hour_cost(state: dict, now) -> float:
    cutoff = now - timedelta(hours=1)
    return sum(c for t, c in state.get("cost_history", []) if T.parse_dt(t) > cutoff)


def _model_usage_number(v) -> float:
    """costUSD (или похожее поле) из одной записи modelUsage; неизвестная форма — 1.0 (сам факт есть)."""
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, dict):
        for k in ("costUSD", "cost_usd", "cost", "totalCostUsd", "total_cost_usd"):
            if k in v:
                try:
                    return float(v[k])
                except (TypeError, ValueError):
                    return 0.0
        return 1.0
    return 0.0


def _model_usage_diff(current: dict, previous: dict) -> dict:
    """Модели, реально задействованные В ЭТОМ прогоне — не за всю историю сессии (судья TK-002 п.1в):
    --resume отдаёт modelUsage кумулятивно, и историческая примесь (например Fable из самого первого,
    домодельного вызова сессии) иначе вечно всплывает как «не-opus», хотя в этом прогоне её не было —
    поймано 27.09 на живой сессии судьи (T-38/TK-002, claude-fable-5-1 не рос ни разу после в1.2)."""
    current = current or {}
    previous = previous or {}
    diff = {}
    for model, val in current.items():
        cur_n = _model_usage_number(val)
        prev_n = _model_usage_number(previous.get(model)) if model in previous else 0.0
        if model not in previous or (cur_n - prev_n) > 0:
            diff[model] = val
    return diff


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


def resolve_run_cost(state: dict, result: dict):
    """(стоимость ИМЕННО этого запуска, разница modelUsage, нужна_ли_пометка «как есть» в runs.log).
    Условия судьи TK-002 п.1: --resume отдаёт total_cost_usd/modelUsage КУМУЛЯТИВНО по всей истории
    session_id, не по этому запуску (живой пример 27.09: сессия судьи сходила с $6,80 → $8,29 → $8,74
    кумулятивных; реальные траты запусков — $1,49 и $0,45 — записывались как $8,29 и $8,74, отсюда
    ложная часовая пауза $19,39/ч при реальных ≈ $4,30/ч). (а) разница — по session_id, не по
    хранилищу роли/задачи (переживает ротацию иначе — новый id начинает с нуля сам по себе, так как
    для него нет прошлого итога). (б) нет прошлого итога ИЛИ разница < 0 → берём итог как есть,
    помечаем. Вызывающий должен получить `result` только когда `total_cost_usd` присутствует —
    отсутствие JSON целиком (таймаут/убит) обрабатывается отдельно, см. _finish_run."""
    raw_cost = float(result.get("total_cost_usd"))
    raw_usage = result.get("modelUsage") or {}
    session_id = result.get("session_id")
    if not session_id:
        return raw_cost, raw_usage, True
    seen_costs = state.setdefault("session_cost_seen", {})
    seen_usage = state.setdefault("session_model_usage_seen", {})
    prev_cost = seen_costs.get(session_id)
    prev_usage = seen_usage.get(session_id, {})
    if prev_cost is None or (raw_cost - prev_cost) < 0:
        cost, note = raw_cost, True
    else:
        cost, note = raw_cost - prev_cost, False
    usage_diff = _model_usage_diff(raw_usage, prev_usage)
    seen_costs[session_id] = raw_cost
    seen_usage[session_id] = raw_usage
    return cost, usage_diff, note


def _pid_alive(pid, expect_name: str = None) -> bool:
    """Жив ли pid — и похож ли на наш `claude` (судья 27.09, «можно потом»): подстрочный поиск pid в
    `tasklist` ловил чужие совпадения (123 ⊂ 1234), а pid мог переиспользоваться ОС после перезагрузки
    — точное сравнение PID-колонки через `/FO CSV` + проверка имени образа снижают оба риска (не
    устраняют полностью: другой процесс `claude.exe` с тем же pid теоретически всё ещё возможен)."""
    expect_name = PID_EXPECT_NAME if expect_name is None else expect_name
    if not pid:
        return False
    if os.name == "nt":
        try:
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                                  capture_output=True, text=True, timeout=5)
            for line in (out.stdout or "").splitlines():
                fields = [f.strip().strip('"') for f in line.split(",")]
                if len(fields) >= 2 and fields[1] == str(pid):
                    return (expect_name or "").lower() in fields[0].lower()
            return False
        except Exception:
            return False
    return _pid_alive_posix(pid, expect_name)


def _proc_state(pid):
    """Буква состояния процесса по `/proc/<pid>/status` (Linux: R, S, D, T, Z, X…) или None, если /proc недоступен."""
    try:
        with open(f"/proc/{pid}/status", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith("State:"):
                    return line.split(":", 1)[1].strip()[:1] or None
    except OSError:
        return None
    return None


def _pid_alive_posix(pid, expect_name: str = None) -> bool:
    """Живость pid на Linux/macOS: `kill -0` отвечает и на зомби (завершился, но родитель не вызвал wait) — такой
    процесс мёртв, иначе подхват «живого» прогона после перезапуска ждёт вечно (состояние Z или X в /proc)."""
    try:
        os.kill(pid, 0)
    except Exception:
        return False
    if _proc_state(pid) in ("Z", "X"):
        return False
    if not expect_name:
        return True
    try:
        with open(f"/proc/{pid}/comm", encoding="utf-8", errors="replace") as fh:
            return expect_name.lower() in fh.read().lower()
    except OSError:
        return True  # /proc недоступен (не Linux) — не валим проверку живости из-за этого


def _pid_kill(pid) -> None:
    if not pid:
        return
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, timeout=5)
        except Exception:
            pass
        return
    try:
        os.kill(pid, 15)
    except Exception:
        pass


def acquire_instance_lock(pid_file, expect_name: str = "py"):
    """Замок единственного экземпляра: pid-файл создаётся атомарно (O_EXCL). Файл есть и в нём живой чужой процесс —
    (False, сообщение); процесса нет (упал, зомби) или файл битый — замок забирается; свой pid (его записал запускатель) —
    ок. Второй диспетчер/сторож иначе запускал бы роли повторно (двойные запуски и расход)."""
    pid_file = Path(pid_file)
    me = os.getpid()
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(5):
        try:
            fd = os.open(str(pid_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                other = int(pid_file.read_text(encoding="utf-8").strip() or 0)
            except (OSError, ValueError):
                other = 0
            if other == me:
                return True, ""
            if other and _pid_alive(other, expect_name):
                return False, f"уже запущен (pid {other}, файл {pid_file.name}) — второй экземпляр не нужен, выхожу"
            try:
                pid_file.unlink()       # процесса нет — замок осиротел, забираем
            except FileNotFoundError:
                pass
            except OSError as e:
                return False, f"не удалось забрать осиротевший замок {pid_file}: {e}"
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(str(me))
        return True, ""
    return False, f"не удалось взять замок {pid_file}"


def release_instance_lock(pid_file) -> None:
    """Снять замок, если он наш (чужой pid не трогаем)."""
    pid_file = Path(pid_file)
    try:
        if int(pid_file.read_text(encoding="utf-8").strip() or 0) == os.getpid():
            pid_file.unlink()
    except (OSError, ValueError):
        pass


def _proc_alive(info: dict) -> bool:
    if info.get("popen") is not None:
        return info["popen"].poll() is None
    return _pid_alive(info.get("pid"))


def _kill_proc(info: dict) -> None:
    if info.get("popen") is not None:
        try:
            info["popen"].kill()
            info["popen"].wait(timeout=10)
        except Exception:
            pass
    else:
        _pid_kill(info.get("pid"))


# --- запуск роли ------------------------------------------------------------------------------

def _popen(cmd, **kwargs):
    """Точка подмены для тестов (вместо многословного CLAUDE_BIN) — оборачивает subprocess.Popen."""
    return subprocess.Popen(cmd, **kwargs)


def effort_for(role: str, tkt=None) -> str:
    """v2: усилие запуска — поле `effort:` тикета, иначе умолчание роли (ROLE_EFFORT: исследователь/инженер
    high, Судья xhigh; env RPV_DISPATCH_EFFORT переопределяет умолчания ролей, не поле тикета)."""
    if tkt is not None and getattr(tkt, "effort", ""):
        return tkt.effort
    return ROLE_EFFORT.get(role, "high")


def tickets_cli() -> str:
    """Команда tickets.py из папки плагина: проект роль берёт из RPV_PROJECT (его ставит launch_run) и своего cwd."""
    return "python " + shlex.quote((CODE_DIR / "tickets.py").as_posix())


def build_prompt(role: str, tid: str, extra_note: str = None) -> str:
    prompt = PROMPT_TEMPLATE.format(role=role, tid=tid, timeout_min=int(RUN_TIMEOUT // 60),
                                    tickets_cli=tickets_cli())
    if extra_note:
        prompt += " " + extra_note
    return prompt


def launch_run(ticket_path, role: str, state: dict, now, reason: str, attempt: int = 0,
               extra_note: str = None) -> None:
    ticket_path = Path(ticket_path)
    tid = ticket_path.stem
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    ts = now.strftime("%Y%m%d-%H%M%S")
    run_file = RUNS_DIR / f"{ts}-{tid}-{role}.json"
    err_file = RUNS_DIR / f"{ts}-{tid}-{role}.err.log"

    store = _resume_store(state, tid, role)
    sid = store.get("session_id")
    if sid and store.get("last_context_tokens", 0) > ROTATE_TOKENS:
        # ротация: контекст прошлой сессии этой роли слишком большой — начинаем новую
        sid = None
        rotate_note = (f"Начинаем новую сессию (контекст прошлой превысил {ROTATE_TOKENS} токенов): "
                        f"прочитай блокнот .claude/roles/notes/{role}.md и прежние решения "
                        f"docs/research/reviews/ по нужной задаче.")
        extra_note = " ".join(x for x in (extra_note, rotate_note) if x)

    if reason == "next":
        next_note = ("Запуск по явной передаче (`--next`): прочитай последнюю запись лога, сделай "
                     "запрошенное и запиши итог.")
        extra_note = " ".join(x for x in (extra_note, next_note) if x)
    prompt = build_prompt(role, tid, extra_note)
    try:
        launch_tkt = T.read_ticket(ticket_path)
        status_at_launch = launch_tkt.status
        executor = launch_tkt.executor
        effort = effort_for(role, launch_tkt)
        # v2: «роль оставила запись» — новый заголовок записи ЭТОЙ роли (ключи на старте), а не рост лога
        log_keys_at_launch = T.role_entry_keys(launch_tkt, role)
    except Exception:
        status_at_launch, executor, effort, log_keys_at_launch = None, "", effort_for(role), []
    # executor: haiku (судья TK-002 п.5) — заведомо проверенный на whitelist/обход тикетом (tickets.py
    # new и haiku_refused_reason() в tick()); здесь только сама подмена модели.
    model = CLAUDE_HAIKU_MODEL if executor == "haiku" else ROLE_MODEL.get(role, CLAUDE_MODEL)
    cmd = [CLAUDE_BIN, "-p", prompt, "--output-format", "json", "--permission-mode", "bypassPermissions",
           "--model", model, "--effort", effort]
    if sid:
        cmd += ["--resume", sid]

    env = dict(os.environ)
    # Судья 27.09, п.4 «обязательно»: без этого дочерний claude наследует CLAUDE_CODE_HOST_SESSION_ID
    # сессии CEO (диспетчер сам запущен из неё) — role_context.py/role_memory.py принимают роль за CEO
    # (тревоги/inbox/«молчание» ломаются на все роли). RPV_ROLE сама по себе не спасает: find_title()
    # срабатывает раньше при непустом host_id, если сама переменная не снята.
    for _k in list(env):
        if "HOST_SESSION" in _k.upper():
            env.pop(_k, None)
    # хуки (role_context/role_memory) ведут состояние и конспект прошлой сессии по тикету; обе пары имён — RPV_* новые,
    # ALPHA_* для хуков, которые ещё читают прежние
    env["RPV_ROLE"] = env["ALPHA_ROLE"] = role
    env["RPV_TICKET"] = env["ALPHA_TICKET"] = tid
    env["RPV_PROJECT"] = str(PROJECT_ROOT)  # tickets.py роли берёт проект отсюда (и из cwd запуска)

    out_fh = open(run_file, "w", encoding="utf-8")
    err_fh = open(err_file, "w", encoding="utf-8")
    popen = _popen(cmd, cwd=str(PROJECT_ROOT), env=env, stdout=out_fh, stderr=err_fh, text=True)
    RUNNING[tid] = {
        "role": role, "popen": popen, "pid": popen.pid, "started": now, "attempt": attempt,
        "run_file": run_file, "err_file": err_file, "out_fh": out_fh, "err_fh": err_fh, "reason": reason,
        "status_at_launch": status_at_launch, "executor": executor,
        "log_keys_at_launch": log_keys_at_launch, "effort": effort,
    }
    # last_woken — приоритет очереди запусков внутри роли (кто дольше не запускался — тот первый, см. tick);
    # всегда на (задачу, роль), не зависит от SESSION_SCOPE
    sess_entry = state.setdefault("sessions", {}).setdefault(f"{tid}::{role}", {})
    sess_entry["last_woken"] = T.now_iso(now)
    _record_launch(state, tid, now)
    # зеркало в state.json (pid, задача, роль, старт) — переживает перезапуск диспетчера (recover_active_runs)
    state.setdefault("active_runs", {})[tid] = {
        "role": role, "pid": popen.pid, "started": T.now_iso(now), "attempt": attempt,
        "run_file": str(run_file), "err_file": str(err_file), "reason": reason,
        "status_at_launch": status_at_launch, "executor": executor,
        "log_keys_at_launch": log_keys_at_launch, "effort": effort,
    }
    save_state(state)


def _read_run_result(run_file: Path) -> dict:
    try:
        data = Path(run_file).read_text(encoding="utf-8").strip()
        return json.loads(data) if data else {}
    except Exception:
        return {}


def _log_run_summary(tid: str, info: dict, result: dict, now, timed_out: bool, resolved_cost: float,
                      ticket_spent: float, cost_note: bool = False) -> None:
    RUNS_LOG.parent.mkdir(parents=True, exist_ok=True)
    usage = result.get("usage") or {}
    status = "timeout" if timed_out else ("ok" if result else "no_output")
    # cost_usd — сырой (кумулятивный за сессию) из JSON; resolved_cost — разница с прошлым итогом ТОЙ ЖЕ
    # session_id (судья TK-002 п.1) — то, что реально начислено этому запуску; cost_note=asis — не было
    # с чем сравнить (новая/ротированная сессия) или разница < 0 — использован сырой итог как есть.
    # ticket_spent — накоплено по ЭТОЙ задаче ПОСЛЕ этого запуска (колонка CEO, роль её не видит).
    line = (f"{T.now_iso(now)} {tid} {info['role']} reason={info.get('reason')} "
            f"attempt={info.get('attempt', 0)} session={result.get('session_id', '-')} "
            f"cost_usd={result.get('total_cost_usd', '-')} resolved_cost={resolved_cost:.4f} "
            f"cost_note={'asis' if cost_note else 'diff'} ticket_spent={ticket_spent:.4f} "
            f"in_tok={usage.get('input_tokens', '-')} out_tok={usage.get('output_tokens', '-')} "
            f"ctx_last={_context_tokens_last(result)} ctx_sum={_context_tokens_sum(usage)} status={status}\n")
    with open(RUNS_LOG, "a", encoding="utf-8") as fh:
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


def same_status_warn_at() -> int:
    """С какого запуска подряд (запись есть, статус тот же) писать CEO предупреждение: половина порога блока."""
    return SAME_STATUS_WARN_RUNS or max(1, MAX_SAME_STATUS_RUNS // 2)


def _reset_same_status(state: dict, key: str) -> None:
    state.setdefault("same_status_runs", {}).pop(key, None)
    state.setdefault("same_status_warned", {}).pop(key, None)


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
    store = _resume_store(state, tid, role)  # session_id/токены — по SESSION_SCOPE[role]
    sid_used = result.get("session_id") or store.get("session_id")  # сессия этого запуска (убит — id прежней, --resume)
    if result.get("session_id"):
        store["session_id"] = result["session_id"]
    # контекст последнего хода; нет JSON/usage (таймаут, ответ-ошибка) — прежнее значение, не ноль
    store["last_context_tokens"] = _context_tokens_for_store(result, store.get("last_context_tokens", 0), sid_used)

    path = TICKETS_DIR / f"{tid}.md"
    if not path.exists():
        save_state(state)
        return
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
        if tkt.status in ("todo", "in_progress") or (tkt.next_role in ROLE_KEYS and tkt.next_role != role):
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
        warned = state.setdefault("same_status_warned", {})
        if same_runs[key] >= same_status_warn_at() and key not in warned:   # на половине порога — одна строка CEO
            warned[key] = same_runs[key]
            append_ceo_inbox(tid, "loop-warning",
                             f"{role}: {same_runs[key]} запусков подряд оставляют запись, а статус {tkt.status} не "
                             f"меняется; на {MAX_SAME_STATUS_RUNS}-м тикет будет заблокирован", now)
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
                    "командой tickets.py comment и обнови status." if not timed_out else
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


def _finish_run(tid: str, info: dict, state: dict, now, timed_out: bool) -> None:
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
    _log_run_summary(tid, info, result, now, timed_out, resolved_cost, ticket_cost_spent(state, tid), cost_note)

    expected_model = _expected_model_family(info)
    model_warn = _model_usage_warning(model_usage_diff, expected_model)
    if model_warn:
        route_ceo_signal(tid, "model", model_warn, state, now)

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
            "role": saved.get("role"), "popen": None, "pid": saved.get("pid"),
            "started": T.parse_dt(saved["started"]), "attempt": saved.get("attempt", 0),
            "run_file": Path(saved["run_file"]), "err_file": Path(saved.get("err_file") or ""),
            "out_fh": None, "err_fh": None, "reason": saved.get("reason", "recovered"),
            "status_at_launch": saved.get("status_at_launch"),
            "executor": saved.get("executor", ""), "log_keys_at_launch": saved.get("log_keys_at_launch"),
            "effort": saved.get("effort", ""),
        }
        if _pid_alive(saved.get("pid")):
            RUNNING[tid] = info
        else:
            state.get("active_runs", {}).pop(tid, None)
            _finish_run(tid, info, state, now, timed_out=False)


# --- тик / цикл -------------------------------------------------------------------------------

def _apply_header_updates(path: Path, updates: dict, now) -> None:
    """Правка шапки перед запуском; одно лишь очищение `next` поле `updated` не двигает (иначе у blocked/
    needs_owner менялся бы маркер уведомления CEO)."""
    only_next = set(updates) <= {"next"}
    T.write_header_updates(path, updates, now=now, stamp_updated=not only_next)


def _candidate_sort_key(state: dict, tkt: T.Ticket, decision: Decision):
    last = state.get("sessions", {}).get(f"{tkt.id}::{decision.role}", {}).get("last_woken") or ""
    return (REASON_PRIORITY.get(decision.reason, 1), last, tkt.id)


def tick(now=None) -> int:
    now = now or datetime.now().astimezone()
    state = load_state()
    recover_active_runs(state, now)  # диспетчер мог перезапуститься — живые/умершие прогоны из state.json
    _poll_running(state, now)
    baseline_done_notified(state)  # v2: историю `done` CEO не пересказываем (один раз, ключ в state.json)
    save_state(state)

    candidates = []  # (path, ticket, decision) — кого можно запустить; порядок и лимиты — ниже
    for path in T.list_tickets(TICKETS_DIR):
        try:
            tkt = T.read_ticket(path)
        except Exception as e:
            notify_parse_error(path.stem, f"{type(e).__name__}: {e}", state, now)
            continue

        # CEO получает строку только по: `next: ceo`, blocked/needs_owner, done.
        # @ceo (и любые @упоминания) в тексте записей — просто текст.
        if tkt.id not in RUNNING and path.stem not in RUNNING:
            # аудит-3: пока владелец ещё работает, его промежуточная запись — не «сдал на ревью»; эскалация — после запуска
            tkt = escalate_review_limit(path, tkt, state, now)
        handle_next_ceo(path, tkt, state, now)
        notify_status_for_ceo(tkt, state, now)
        notify_done(tkt, state, now)

        tid = tkt.id
        if tid in RUNNING:
            continue
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

    launched = 0
    for path, tkt, decision in sorted(candidates, key=lambda c: _candidate_sort_key(state, c[1], c[2])):
        tid = tkt.id
        if len(RUNNING) >= MAX_PARALLEL:
            break
        if _role_busy(decision.role):
            continue  # у роли уже идёт запуск (на любой задаче) — ждёт следующего тика
        if _rate_limited(state, tid, now):
            continue  # MAX_RUNS_PER_TICKET_HOUR/MIN_GAP_S — пауза, не ошибка; попробуем следующим тиком
        if decision.header_updates:
            _apply_header_updates(path, decision.header_updates, now)
        launch_run(path, decision.role, state, now, reason=decision.reason)
        launched += 1

    state["last_tick"] = T.now_iso(now)  # судья TK-002 п.2а: сторож проверяет диспетчер жив по этому
    save_state(state)
    return launched


USAGE = """Диспетчер задач (v2, 02.10). Запускается из папки плагина; проект — --project <путь>, иначе RPV_PROJECT /
CLAUDE_PROJECT_DIR, иначе ближайший каталог вверх с .claude/roles. Состояние — <проект>/.claude/dispatcher/.
  python <плагин>/.claude/dispatcher/dispatch.py --project <проект>          # цикл раз в RPV_DISPATCH_INTERVAL (15 с) — БОЕВОЙ
  python <плагин>/.claude/dispatcher/dispatch.py --project <проект> --once   # один тик (тоже боевой: может запустить роли)
  python <плагин>/.claude/dispatcher/dispatch.py --help                      # эта справка (ничего не запускает)
Правила — README.md рядом с диспетчером."""


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--help" in argv or "-h" in argv:  # раньше любой аргумент запускал боевой цикл
        print(USAGE)
        return 0
    if not ensure_project(argv, "dispatch"):     # нет проекта — ошибка с подсказкой, каталоги не создаём
        return 2
    DISPATCHER_DIR.mkdir(parents=True, exist_ok=True)
    TICKETS_DIR.mkdir(parents=True, exist_ok=True)
    ok, why = acquire_instance_lock(PID_FILE)
    if not ok:                                   # второй диспетчер (в т. ч. ручной --once при живом цикле) — не стартуем
        print(f"[dispatch] {why}", file=sys.stderr)
        return 1
    atexit.register(release_instance_lock, PID_FILE)
    if "--once" in argv:
        n = tick()
        print(f"[dispatch] once: launched={n} running={len(RUNNING)}")
        return 0
    print(f"[dispatch] v2 loop every {POLL_INTERVAL}s, MAX_PARALLEL={MAX_PARALLEL}, run timeout "
          f"{RUN_TIMEOUT / 60:.0f} min, CLAUDE_BIN={CLAUDE_BIN}")
    while True:
        try:
            tick()
        except Exception as e:
            print(f"[dispatch] tick error: {type(e).__name__}: {e}", file=sys.stderr)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    sys.exit(main())
