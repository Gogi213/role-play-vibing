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
import hide  # noqa: E402
import ticket as T  # noqa: E402
import worktree_hygiene as WH  # noqa: E402
import bus_link  # noqa: E402
import downtime  # noqa: E402
import haiku_aux as HA  # noqa: E402

# --- конфигурация (константы — тесты подменяют их прямо на модуле) ------------------------

CODE_DIR = Path(__file__).resolve().parent  # где лежит сам диспетчер (папка плагина) — к проекту отношения не имеет
# Пути проекта (PROJECT_ROOT, TICKETS_DIR, DISPATCHER_DIR — каталог СОСТОЯНИЯ в проекте и остальные файлы) выставляет
# configure_project(): при импорте — по --project/RPV_PROJECT/CLAUDE_PROJECT_DIR/текущему каталогу, в main() — по флагу.
PROJECT_ROOT = DISPATCHER_DIR = TICKETS_DIR = STATE_FILE = PID_FILE = RUNS_DIR = RUNS_LOG = None
CEO_INBOX = CEO_WAKE_LOG = STOP_DIR = None
PROJECT_FOUND = False  # проект найден при импорте (флаг/окружение/поиск вверх); False — CLI откажет с подсказкой


def configure_project(root) -> Path:
    """Корень проекта и все пути состояния от него (ничего не создаёт — каталоги появляются при записи)."""
    global PROJECT_ROOT, DISPATCHER_DIR, TICKETS_DIR, STATE_FILE, PID_FILE, RUNS_DIR, RUNS_LOG, CEO_INBOX, CEO_WAKE_LOG, STOP_DIR
    PROJECT_ROOT = Path(root).expanduser().resolve()
    DISPATCHER_DIR = PROJECT_ROOT / ".claude" / "dispatcher"
    TICKETS_DIR = PROJECT_ROOT / ".claude" / "tickets"
    STATE_FILE = DISPATCHER_DIR / "state.json"
    PID_FILE = DISPATCHER_DIR / "dispatch.pid"  # замок единственного экземпляра диспетчера (см. acquire_instance_lock)
    RUNS_DIR = DISPATCHER_DIR / "runs"
    RUNS_LOG = DISPATCHER_DIR / "runs.log"
    CEO_INBOX = DISPATCHER_DIR / "ceo-inbox.md"
    CEO_WAKE_LOG = DISPATCHER_DIR / "ceo-wake.log"  # короткая копия каждой строки ceo-inbox — CEO держит на ней Monitor
    STOP_DIR = DISPATCHER_DIR / "stop"  # заявки `tickets.py stop` (<ID>.json): диспетчер разбирает их на ближайшем тике
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
# v2 (02.10, аудит ролевой системы): всего параллельно ≤ 3 запусков и по умолчанию не больше ОДНОГО запуска на роль
# (по всем тикетам сразу, см. _role_busy); таймаут запуска 20 мин (было 40 — фоновые помощники в `-p` висели
# до убийства, а цена убитого запуска в учёте — $0). Предел на роль настраивается RPV_DISPATCH_ROLE_PARALLEL
# (например engineer:3); на один тикет — по-прежнему один запуск роли.
MAX_PARALLEL = int(P.env("DISPATCH_MAX_PARALLEL", "3"))


def _parse_role_parallel(spec: str) -> dict:
    """«engineer:3,researcher:1» → {роль: предел запусков}. Роль без записи — предел 1 (см. _role_busy); пара не вида
    «роль:целое ≥ 1» игнорируется с предупреждением в stderr."""
    out = {}
    for pair in (spec or "").split(","):
        pair = pair.strip()
        if not pair:
            continue
        role, sep, num = pair.partition(":")
        role, num = role.strip(), num.strip()
        if sep and role and re.fullmatch(r"[0-9]+", num) and int(num) >= 1:
            out[role] = int(num)
        else:
            print(f"[dispatch] RPV_DISPATCH_ROLE_PARALLEL: пара {pair!r} пропущена (нужно роль:целое≥1), предел 1",
                  file=sys.stderr)
    return out


ROLE_PARALLEL = _parse_role_parallel(P.env("DISPATCH_ROLE_PARALLEL", ""))  # тесты подменяют на модуле
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
# меняется → на MAX тикет blocked + строка CEO; MAX_IDLE_RUNS (2) — запуски без записи и без смены статуса (холостой ход) → blocked (первый
# холостой — обычный один повтор, см. _finish_role_part).
MAX_SAME_STATUS_RUNS = int(P.env("DISPATCH_MAX_SAME_STATUS_RUNS", "12"))
MAX_IDLE_RUNS = int(P.env("DISPATCH_MAX_IDLE_RUNS", "2"))
# Пинг-понг ревью (аудит-2): сколько раз ревьюер может вернуть работу владельцу (in_review → вернул → снова in_review).
# После MAX_REVIEW_RETURNS возвратов тикет, снова пришедший на ревью, ревьюеру не отдаётся: запись dispatcher +
# `next: ceo` (одна строка CEO). Число НАЗНАЧЕНО CEO 03.10, не измерено.
MAX_REVIEW_RETURNS = int(P.env("DISPATCH_MAX_REVIEW_RETURNS", "3"))
# Остановка роли CEO (`tickets.py stop`): после снятия дерева процессов ждём смерти процесса не дольше STOP_VERIFY_S
# секунд (число НАЗНАЧЕНО, не измерено: taskkill/killpg возвращаются сразу, 10 с — запас на очередь ОС); не умер —
# строка CEO `stop-failed`, заявка остаётся до следующего тика.
STOP_VERIFY_S = float(P.env("DISPATCH_STOP_VERIFY_S", "10"))
STOP_NOTE = ("Прошлый запуск оборван CEO — проверь git status и недописанные правки, начни с новой постановки "
             "(последняя запись CEO в логе тикета).")

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
CLAUDE_HAIKU_MODEL = P.env("DISPATCH_HAIKU_MODEL", "claude-haiku-5-5")
HAIKU_ALLOWED_KINDS = {"file-move", "table-format", "publish", "log-compact"}

ROLE_KEYS = ("researcher", "engineer", "judge")  # роли, которых диспетчер запускает; ceo — человек/CEO-сессия

# Область сессии на роль (владелец 27.09): "ticket" — сессия на (задача, роль), --resume в пределах
# задачи; "role" — одна долгая сессия роли на ВСЕ задачи (в промпте каждый раз названа текущая задача).
# v2 (02.10): Судья тоже "ticket" — одна сессия на все задачи копила контекст чужих тикетов (аудит).
# Предел запусков на роль действует всегда (_role_busy), а не только при "role"; по умолчанию 1, настраивается
# RPV_DISPATCH_ROLE_PARALLEL; на один тикет — один запуск роли.
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
    "{timeout_min} мин. Не запускай в сессии фоновых помощников и фоновых задач; долгая работа — фоновый процесс "
    "на машине (systemd-run) + status: waiting + wait_for (`{tickets_cli} wait {tid} "
    "host:<алиас>:<путь к файлу хода>.json --by ЧЧ:ММ` (срок обязателен, GMT+4); формы: host:calc|vps|deck:<путь | unit:имя>, file:<путь>, ticket:<ID>), и выйди, "
    "не жди в сессии. Трать минимум: "
    "самый короткий путь к результату задачи; траты каждого запуска записываются и сравниваются с "
    "результатом. Сделай следующий шаг и сдай итог командой "
    "`{tickets_cli} result {tid} <done|pr|accept|return|blocked|ask-owner|wait|continue> --why \"что сделал, что "
    "дальше\"` ДО истечения лимита (доказательство: done — --path, pr — --pr --sha, accept/return — --sha, "
    "wait — --form; неполная команда — отказ с подсказкой): итог обязателен, частичный прогресс не провал. "
    "Кого будить дальше и status/wait_for ставит таблица маршрутов — --next и шапку руками не правь; "
    "blocked (задача встала) уходит Судье, ask-owner — только вопрос о содержании исследования (какие гипотезы, "
    "гиперпараметры, метрики, что хотим увидеть) — владельцу; CEO не будят. "
    "`{tickets_cli} comment {tid} --author {role} --text \"...\"` — только промежуточная заметка без смены "
    "хода. @упоминания в тексте никого не будят. "
    "Табло: нет плана шагов — в начале работы `{plan_cli} set {tid} --title \"...\" --step \"название|кто|где|для чего\" ...` "
    "(3–6 шагов по-людски, `{plan_cli} --help`); в конце запуска — `{plan_cli} step {tid} <N> <run|done|todo>` по факту."
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
    T.atomic_write_text(STATE_FILE, json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True))  # с повторами: на Windows читатель держит файл


# --- wait_for ---------------------------------------------------------------------------------

def check_wait_for(spec: str) -> bool:
    """Условие `wait_for` выполнено? Формы — `ticket.parse_wait_for` (README, «v4»). Незнакомая форма (в т.ч. свободный
    текст и старое `mention`) → False, но тихо не остаётся: `notify_wait_for_problem` пишет строку в ceo-inbox."""
    parsed = T.parse_wait_for(spec)
    if parsed is None:
        return False
    if parsed[0] == "file":
        path = Path(parsed[1])
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        return path.exists()
    if parsed[0] == "at":
        return datetime.now().astimezone() >= parsed[1]
    if parsed[0] == "ticket":
        return _other_ticket_done(parsed[1])
    if parsed[0] == "ci":
        import ci_watch
        return ci_watch.ci_done(parsed[1], parsed[2])
    if parsed[0] == "ci-run":
        import ci_watch
        return ci_watch.run_done(parsed[1], parsed[2])
    if parsed[0] == "merged":
        import merge_rule
        return merge_rule.merged_done(parsed[1], parsed[2])
    if parsed[0] == "job":  # состояние пишет сторож жизни (watch.py → lifewatch.py), своего ssh нет
        import lifewatch
        return lifewatch.job_done(DISPATCHER_DIR, parsed[1], parsed[2])
    _, alias, what, arg = parsed
    return _host_wait_met(alias, what, arg)


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


_WAIT_CACHE = {}  # (алиас, "path"|"unit", арг) -> (time.time() отметка, результат) — см. _host_wait_met
WAIT_CHECK_CACHE_S = float(P.env("DISPATCH_DECK_CACHE_S", "60"))
WAIT_ERR_EVERY_S = 600.0  # ошибка ssh по одному условию — строка в dispatch.err.log не чаще раза в 10 мин
_WAIT_ERR_LAST = {}  # ключ условия -> time.time() последней строки
_UNIT_RUNNING = ("active", "activating", "reloading", "deactivating", "refreshing")

# TK-055: wait_for host:… закрывается событием сторожа машины (.claude/bus/watcher.py); ssh — редкая подстраховка в потоке.
WAIT_ASYNC = False  # True ставит main() для боевого цикла; тесты и --once — синхронный путь как раньше
WAIT_POLL_S = float(P.env("DISPATCH_WAIT_POLL_S", "300"))
PROGRESS_DIR = P.env("PROGRESS_DIR", "~/rpv/progress")  # каталог файлов хода заданий на машинах (RPV_PROGRESS_DIR)
WATCH_LIST = PROGRESS_DIR + "/watch.list"  # пути, которые сторож машины проверяет сам (по строке на путь)
_WAIT_WATCH = set()  # ключи (алиас, what, арг), которые ждут тикеты — их опрашивает _wait_poller
_EVENT_MET = {}      # (алиас, "unit"|"path", арг) -> time.time() прихода события
_UNIT_START = {}     # (алиас, "unit", имя) -> InvocationID последнего запуска по событию «юнит.запущен»
_EVENT_VERIFIED = set()  # ключи, чьё «остановлен» несёт InvocationID того же запуска: ssh-проверка не нужна
_EVENT_LOCK = threading.Lock()
_WAIT_NEW = threading.Event()

# Стандарт сигналов: штатно ssh не опрашивает — события сторожа машины закрывают wait_for; ssh — первая проверка нового
# условия и сверка раз в WAIT_RECON_S (она же — единственный путь, пока шина лежит или не настроена, TK-100 №21).
WAIT_RECON_S = float(P.env("DISPATCH_WAIT_RECON_S", "300"))  # сверка всех ждущих host:… одним ssh на машину
WATCHED_ALIASES = {a.strip() for a in P.env("WATCHED_ALIASES", "calc").split(",") if a.strip()}  # машины со сторожем: пропуск события там — тревога
_LINK = None
_RECON_MISS = set()  # ключи, по которым «пропуск» уже записан и тревога уже ушла
_WL_REG = set()  # (алиас, путь), чья регистрация у сторожа подтверждена ответом машины
_RECON_LAST = 0.0


def _unit_base(name: str) -> str:
    return name[:-8] if name.endswith(".service") else name


def record_wait_event(ev: dict) -> None:
    """Событие шины → «условие wait_for выполнено»: машина.<алиас>.юнит.остановлен|упал, задача.*.задание.готово,
    машина.<алиас>.файл.появился (payload.path). Вызывается из потока слушателя шины."""
    addr, pl = ev.get("addr", ""), ev.get("payload") or {}
    if not isinstance(pl, dict):
        return
    parts = addr.split(".")
    host = pl.get("host") or (parts[1] if len(parts) > 1 else "")
    keys = []
    if addr.startswith("машина.") and addr.endswith(".юнит.запущен") and pl.get("unit") and pl.get("invocation"):
        k = (host, "unit", _unit_base(str(pl["unit"])))
        with _EVENT_LOCK:  # новый запуск: прежнее «остановлен» этого имени недействительно
            _UNIT_START[k] = str(pl["invocation"])
            _EVENT_MET.pop(k, None)
            _EVENT_VERIFIED.discard(k)
        return
    if addr.startswith("машина.") and addr.endswith((".юнит.остановлен", ".юнит.упал")) and pl.get("unit"):
        k = (host, "unit", _unit_base(str(pl["unit"])))
        inv, began = str(pl.get("invocation") or ""), _UNIT_START.get(k)
        if inv and began and inv != began:
            return  # остановка не последнего запуска (старый экземпляр с тем же именем) — игнорируем
        if inv and began == inv:
            with _EVENT_LOCK:
                _EVENT_VERIFIED.add(k)
        keys.append(k)
        if addr.endswith(".юнит.упал") and P.env("HAIKU_DIAG") != "0":
            threading.Thread(target=_diagnose_failed_unit, args=(host, str(pl["unit"])), daemon=True).start()
    elif addr.startswith("машина.") and addr.endswith(".файл.появился") and pl.get("path"):
        keys.append((host, "path", str(pl["path"])))
    elif addr.startswith("задача.") and addr.endswith(".задание.готово") and pl.get("job"):
        keys.append((host, "path", f"{PROGRESS_DIR}/{pl['job']}.json"))
    with _EVENT_LOCK:
        for k in keys:
            _EVENT_MET[k] = time.time()


def _diagnose_failed_unit(host: str, unit: str) -> None:
    """TK-087 п.1: упал юнит → Haiku читает хвост journalctl и пишет причину в тикеты, ждущие этот юнит. Сбой — тихо."""
    try:
        base = _unit_base(unit)
        paths = []
        for path in T.list_tickets(TICKETS_DIR):
            parsed = T.parse_wait_for(T.read_ticket(path).wait_for)
            if parsed and parsed[:3] == ("host", host, "unit") and _unit_base(parsed[3]) == base:
                paths.append(path)
        if not paths:
            return
        r = hide.run(_ssh_cmd(host, f"journalctl -u {shlex.quote(base)} -n 80 --no-pager 2>&1 | tail -c 8000"),
                           capture_output=True, timeout=30)
        diag = HA.diagnose(unit, (r.stdout or b"").decode("utf-8", "replace"))
        for path in paths if diag else []:
            T.append_log(path, "haiku", f"диагноз упавшего юнита {unit} на {host} (Haiku, по journalctl):\n{diag}")
    except Exception as e:  # noqa: BLE001 — рутина не должна ронять слушатель шины
        print(f"[dispatch] haiku-diag {host}/{unit}: {e}", file=sys.stderr, flush=True)


def _event_met(alias: str, what: str, arg: str) -> bool:
    key = (alias, what, _unit_base(arg) if what == "unit" else arg)
    with _EVENT_LOCK:
        return key in _EVENT_MET


def _event_ts(alias: str, what: str, arg: str):
    key = (alias, what, _unit_base(arg) if what == "unit" else arg)
    with _EVENT_LOCK:
        return _EVENT_MET.get(key)


def _drop_event(alias: str, what: str, arg: str) -> None:
    with _EVENT_LOCK:
        k = (alias, what, _unit_base(arg) if what == "unit" else arg)
        _EVENT_MET.pop(k, None)
        _EVENT_VERIFIED.discard(k)


def _needs_probe(ckey) -> bool:
    cached = _WAIT_CACHE.get(ckey)
    if cached is None:
        return True  # первая проверка нового условия (заодно регистрация пути у сторожа)
    ev_ts = _event_ts(*ckey)  # событие остановки юнита — одна проверка, что это не прежний экземпляр с тем же именем
    return (ckey[1] == "unit" and ev_ts is not None and cached[0] < ev_ts
            and (ckey[0], "unit", _unit_base(ckey[2])) not in _EVENT_VERIFIED)


def _wait_poller() -> None:
    """Штатно ssh не опрашивает: события шины (сторож машины) закрывают wait_for; здесь — первая проверка нового условия
    ключей из _WAIT_WATCH и сверка раз в WAIT_RECON_S."""
    global _RECON_LAST
    while True:
        for ckey in list(_WAIT_WATCH):
            if not _needs_probe(ckey):
                continue
            try:
                _host_probe(*ckey)
            except Exception as e:
                _wait_err(f"poller:{ckey}", f"{type(e).__name__}: {e}")
        if _WAIT_WATCH and time.time() - _RECON_LAST >= WAIT_RECON_S:
            _RECON_LAST = time.time()
            try:
                _reconcile()
            except Exception as e:
                _wait_err("reconcile", f"{type(e).__name__}: {e}")
        _WAIT_NEW.wait(min(WAIT_POLL_S, WAIT_RECON_S))
        _WAIT_NEW.clear()


def _wait_err(key: str, msg: str) -> None:
    now_ts = time.time()
    last = _WAIT_ERR_LAST.get(key)
    if last is not None and (now_ts - last) < WAIT_ERR_EVERY_S:
        return
    _WAIT_ERR_LAST[key] = now_ts
    print(f"[dispatch] {T.now_iso()} wait_for {key}: {msg}", file=sys.stderr, flush=True)


def _ssh_cmd(alias: str, remote_cmd: str) -> list:
    # Хост — только из окружения (RPV_CALC_HOST / RPV_VPS_HOST / RPV_DECK_HOST, прежние ALPHA_*); не задан — ошибка
    # (условие «не выполнено» + строка в dispatch.err.log). Ключ и known_hosts — RPV_DECK_KEY / RPV_DECK_KNOWN_HOSTS.
    host = P.env(f"{alias.upper()}_HOST")
    if not host:
        raise RuntimeError(f"хост алиаса {alias!r} не задан: RPV_{alias.upper()}_HOST")
    key = P.env("DECK_KEY")
    known_hosts = P.env("DECK_KNOWN_HOSTS")
    return (["ssh"] + (["-i", key] if key else []) + (["-o", f"UserKnownHostsFile={known_hosts}"] if known_hosts else [])
            + ["-o", "BatchMode=yes", "-o", "ConnectTimeout=8", host, remote_cmd])


def _progress_done(text: str):
    """Содержимое `.json` — файл хода (числа `done`/`total`)? → done >= total (и total > 0, чтобы
    не сработать на заготовке с нулями); иначе None — это не файл хода, достаточно того, что он существует."""
    try:
        obj = json.loads(text)
        done, total = obj["done"], obj["total"]
        if isinstance(done, bool) or isinstance(total, bool):
            return None
        done, total = float(done), float(total)
    except (ValueError, KeyError, TypeError):
        return None
    return total > 0 and done >= total


def _host_wait_met(alias: str, what: str, arg: str) -> bool:
    """`host:<алиас>:<путь>` — путь на машине существует (`.json` с done/total — done >= total); `host:<алиас>:unit:<имя>` —
    юнит не работает (`systemctl is-active` ≠ active: задание закончилось или упало).
    Судья 27.09 («можно потом»): без кэша ssh дёргается на каждый ждущий тикет каждые 15 с — результат на
    WAIT_CHECK_CACHE_S; ошибка ssh (код 255, таймаут) = «не выполнено» + строка в dispatch.err.log раз в 10 мин."""
    ckey = (alias, what, arg)
    ev_ts = _event_ts(alias, what, arg)
    if ev_ts is not None:
        if what != "unit" or (alias, what, _unit_base(arg)) in _EVENT_VERIFIED:
            return True  # путь, либо остановка того же InvocationID, что и запуск: ssh не нужен (TK-072)
        # событие юнита может быть от прежнего экземпляра с тем же именем: засчитываем, только если проверка
        # ПОСЛЕ прихода события видела юнит не работающим (работает — _host_probe сбросит событие)
        cached = _WAIT_CACHE.get(ckey)
        if not (cached and cached[0] >= ev_ts):
            if WAIT_ASYNC:
                _WAIT_WATCH.add(ckey)
                _WAIT_NEW.set()
                return False
            _host_probe(alias, what, arg)
            cached = _WAIT_CACHE.get(ckey)
        return bool(cached and cached[1] and _event_met(alias, what, arg))
    if WAIT_ASYNC:  # основной поток ssh не трогает: проверку делает _wait_poller редким шагом (TK-055)
        if ckey not in _WAIT_WATCH:
            _WAIT_WATCH.add(ckey)
            _WAIT_NEW.set()  # первую проверку нового условия поток делает сразу, не через WAIT_POLL_S
        return (_WAIT_CACHE.get(ckey) or (0, False))[1]
    cached = _WAIT_CACHE.get(ckey)
    now_ts = time.time()
    if cached and (now_ts - cached[0]) < WAIT_CHECK_CACHE_S:
        return cached[1]
    return _host_probe(alias, what, arg)


SSH_CALLS_LOG_NAME = "ssh-calls.log"  # строка на ssh-вызов диспетчера: мерка «сутки без ssh-опроса»


def _log_ssh_call(alias: str, what: str, arg: str, reason: str) -> None:
    try:
        with open(DISPATCHER_DIR / SSH_CALLS_LOG_NAME, "a", encoding="utf-8") as f:
            f.write("\t".join((T.now_iso(), alias, what, arg, reason)) + "\n")
    except OSError:
        pass


def _is_progress_json(arg: str) -> bool:
    """Файл хода (`<RPV_PROGRESS_DIR>/<job>.json`, done/total) сторож ведёт сам; прочие пути, в т.ч. .json вне каталога, регистрируются.
    `~` в PROGRESS_DIR — домашний каталог машины: сравниваем по хвосту пути."""
    if not arg.endswith(".json"):
        return False
    d = arg.rsplit("/", 1)[0]
    pd = PROGRESS_DIR.rstrip("/")
    return d == pd or (pd.startswith("~/") and d.endswith(pd[1:]))


def _host_probe(alias: str, what: str, arg: str) -> bool:
    ckey = (alias, what, arg)
    now_ts = time.time()
    if what == "unit":
        remote = f"systemctl is-active {shlex.quote(arg)}"
    elif arg.endswith(".json"):
        remote = f"cat {_remote_test_arg(arg)}"
    else:
        remote = f"test -e {_remote_test_arg(arg)}"
    if what == "path" and WAIT_ASYNC and arg.startswith("/") and not _is_progress_json(arg):
        # заодно регистрируем путь в списке сторожа машины: дальше появление файла придёт событием без ssh;
        # «@@WL» в ответе = регистрация подтверждена (нет — повторит сверка _reconcile)
        remote = (f"{{ grep -qxF {shlex.quote(arg)} {WATCH_LIST} 2>/dev/null || echo {shlex.quote(arg)} >> {WATCH_LIST}; }} "
                  f">/dev/null 2>&1 && echo @@WL; {remote}")
    label = f"host:{alias}:{'unit:' if what == 'unit' else ''}{arg}"
    result = False
    _log_ssh_call(alias, what, arg, "первая" if ckey not in _WAIT_CACHE else "событие-юнита")
    try:
        r = hide.run(_ssh_cmd(alias, remote), capture_output=True, timeout=15)
        out = (getattr(r, "stdout", b"") or b"").decode("utf-8", "replace")
        if out.startswith("@@WL\n") or out.strip() == "@@WL":
            _WL_REG.add((alias, arg))
            out = out[len("@@WL"):].lstrip("\r\n")
        if what == "unit":
            state = (out.strip().splitlines() or [""])[0]
            if r.returncode == 255 or not state:
                _wait_err(label, f"ssh: код {r.returncode}, {_ssh_stderr(r)}")
            else:
                result = state not in _UNIT_RUNNING
                if not result:  # юнит работает — «остановлен» от прежнего экземпляра с этим именем недействительно
                    _drop_event(alias, what, arg)
        elif r.returncode == 0:
            progress = _progress_done(out) if arg.endswith(".json") else None
            result = True if progress is None else progress
        elif r.returncode != 1:  # 1 — файла нет (штатно); остальное (255…) — сбой ssh/хоста
            _wait_err(label, f"ssh: код {r.returncode}, {_ssh_stderr(r)}")
    except Exception as e:
        _wait_err(label, f"ssh: {type(e).__name__}: {e}")
    _WAIT_CACHE[ckey] = (now_ts, result)
    return result


def _recon_script(keys: list) -> str:
    parts = []
    for i, (alias, what, arg) in enumerate(keys):
        q = shlex.quote(arg)
        parts.append(f"echo @@{i}")
        if what == "unit":
            parts.append(f"systemctl is-active {q} 2>&1 | head -1; echo '@@rc 0'")
            continue
        if arg.startswith("/") and not _is_progress_json(arg):
            parts.append(f"{{ grep -qxF {q} {WATCH_LIST} 2>/dev/null || echo {q} >> {WATCH_LIST}; }} >/dev/null 2>&1 && echo @@reg")
        parts.append(f"{'cat' if arg.endswith('.json') else 'test -e'} {_remote_test_arg(arg)} 2>/dev/null; echo \"@@rc $?\"")
    return "; ".join(parts)


def _parse_recon(out: str, n: int) -> dict:
    """Ответ _recon_script → {i: {"reg": bool, "rc": int|None, "body": str}}."""
    res, cur = {}, None
    for line in out.splitlines():
        line = line.rstrip("\r")
        if line.startswith("@@") and line[2:].isdigit() and int(line[2:]) < n:
            cur = int(line[2:])
            res[cur] = {"reg": False, "rc": None, "body": []}
        elif cur is not None and line == "@@reg":
            res[cur]["reg"] = True
        elif cur is not None and line.startswith("@@rc "):
            try:
                res[cur]["rc"] = int(line[5:])
            except ValueError:
                pass
        elif cur is not None:
            res[cur]["body"].append(line)
    for v in res.values():
        v["body"] = "\n".join(v["body"])
    return res


def _recon_result(alias: str, what: str, arg: str, v: dict):
    """Ответ сверки по одному ключу → выполнено ли условие; None — ответ неясен, кэш не трогаем."""
    if what == "unit":
        state = (v["body"].strip().splitlines() or [""])[0]
        if not state:
            return None
        result = state not in _UNIT_RUNNING
        if not result:
            _drop_event(alias, what, arg)
        return result
    if v["rc"] == 0:
        progress = _progress_done(v["body"]) if arg.endswith(".json") else None
        return True if progress is None else progress
    return False if v["rc"] == 1 else None


def _recon_note_miss(key, result: bool) -> None:
    """Сверка закрыла условие, а события не было — на машине со сторожем тревога `recon-miss` (раз на ключ)."""
    alias, what, arg = key
    prev = _WAIT_CACHE.get(key)
    if (result and not (prev and prev[1]) and alias in WATCHED_ALIASES and _LINK is not None and _LINK.down_since is None
            and _event_ts(*key) is None and key not in _RECON_MISS):
        _RECON_MISS.add(key)  # запасной путь сработал, события не было — сторож не справился
        _log_ssh_call(alias, what, arg, "пропуск")
        append_ceo_inbox("*", "recon-miss", f"сверка закрыла host:{alias}:{'unit:' if what == 'unit' else ''}{arg} без события шины — проверить сторож машины")


def _reconcile() -> None:
    """Страховка: раз в WAIT_RECON_S один ssh на машину проверяет ВСЕ ждущие host:… и повторяет регистрацию путей
    у сторожа, не подтверждённую ранее. Пропущенное событие или сорванная регистрация стоят не дороже одного шага сверки."""
    by_alias = {}
    for k in list(_WAIT_WATCH):
        by_alias.setdefault(k[0], []).append(k)
    for alias, keys in by_alias.items():
        keys.sort()
        _log_ssh_call(alias, "сверка", f"{len(keys)} ключей", "сверка")
        try:
            r = hide.run(_ssh_cmd(alias, _recon_script(keys)), capture_output=True, timeout=45)
        except Exception as e:
            _wait_err(f"reconcile:{alias}", f"ssh: {type(e).__name__}: {e}")
            continue
        out = (getattr(r, "stdout", b"") or b"").decode("utf-8", "replace")
        if r.returncode == 255 or not out.strip():
            _wait_err(f"reconcile:{alias}", f"ssh: код {r.returncode}, {_ssh_stderr(r)}")
            continue
        now_ts = time.time()
        for i, v in _parse_recon(out, len(keys)).items():
            if v["rc"] is None:
                continue
            _, what, arg = keys[i]
            if v["reg"]:
                _WL_REG.add((alias, arg))
            result = _recon_result(alias, what, arg, v)
            if result is None:
                continue
            _recon_note_miss(keys[i], result)
            _WAIT_CACHE[keys[i]] = (now_ts, result)


def _ssh_stderr(r) -> str:
    return ((getattr(r, "stderr", b"") or b"").decode("utf-8", "replace").strip().splitlines() or ["—"])[-1][:200]


# --- ceo-inbox ---------------------------------------------------------------------------------

# Стандарт сигналов (В-192): при настроенной шине единственный путь сигнала к CEO — событие `задача.<ID|общее>.к_ceo`
# в очередь `ceo` с приоритетом (urgent — читать первым); CEO читает её `tickets.py inbox`. Файлы ceo-inbox.md и
# ceo-wake.log при шине — ТОЛЬКО запасной путь, когда шина не приняла событие (строка помечена «[запасной путь]»);
# без шины (RPV_BUS_URL не задан) файлы — основной путь, как раньше. Неизвестный вид — urgent (безопасная сторона).
NORMAL_KINDS = {"done", "next-ceo", "wait-for", "model", "watch-summary", "summary", "bus-up"}


def signal_prio(kind: str) -> str:
    return "normal" if kind in NORMAL_KINDS else "urgent"


def _ceo_file_write(tid: str, kind: str, note: str, now=None, fallback: bool = False, wake_only: bool = False) -> None:
    CEO_INBOX.parent.mkdir(parents=True, exist_ok=True)
    if not wake_only:
        with open(CEO_INBOX, "a", encoding="utf-8") as fh:
            fh.write(f"- {T.now_iso(now)} {tid} [{kind}]{' [запасной путь]' if fallback else ''} {note}\n")
    # ceo-wake.log — короткая (время, задача, причина) копия для Monitor CEO; детали — в ceo-inbox.md или в очереди шины
    with open(CEO_WAKE_LOG, "a", encoding="utf-8") as fh:
        fh.write(f"{T.now_iso(now)} {tid} {kind}{' (очередь шины: tickets.py inbox)' if wake_only else ''}\n")


def ceo_queue_wake(addr: str, seq: int) -> None:
    """Будильник на событие очереди ceo, пришедшее мимо append_ceo_inbox (вопрос владельцу, падение юнита, тревога простоя)."""
    CEO_WAKE_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(CEO_WAKE_LOG, "a", encoding="utf-8") as fh:
        fh.write(f"{T.now_iso()} {addr} #{seq} (очередь шины: tickets.py inbox){chr(10)}")


def _bus_configured() -> bool:
    try:
        import busclient
        return not os.environ.get("RPV_BUS_DISABLE") and bool(busclient.config()[0])
    except Exception:
        return False


def append_ceo_inbox(tid: str, kind: str, note: str, now=None) -> None:
    """Сигнал CEO: шина (очередь `ceo`, ack на стороне CEO); шина не настроена — файл; не приняла — запасной файл."""
    if not _bus_configured():
        _ceo_file_write(tid, kind, note, now)
        return
    import busclient
    addr = f"задача.{'общее' if tid == '*' else tid}.к_ceo"
    payload = {"kind": kind, "note": note, "prio": signal_prio(kind), "ts": T.now_iso(now)}
    if busclient.post(addr, payload, timeout=3, spool=False) is None:
        _ceo_file_write(tid, kind, note, now, fallback=True)
        return
    _ceo_file_write(tid, kind, note, now, wake_only=True)  # будильник Monitor CEO; детали — в очереди (`tickets.py inbox`)


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
    # метка — на диск раньше, чем уйдёт `next`: убит диспетчер между ними — метка есть, `next` остался и доставится
    # повторно (строка CEO может задвоиться, но передача не теряется и ожидание роли не станет «без условия»)
    state.setdefault("ceo_handoffs", {})[tkt.id] = T.now_iso(now)  # next съеден тиком при живой роли — её waiting не «без условия»
    save_state(state)
    append_ceo_inbox(tkt.id, "next-ceo", f"{who}{_first_line(last.text if last else '')}", now)
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


def escalate_review_limit(path: Path, tkt: T.Ticket, state: dict, now) -> T.Ticket:
    """Пинг-понг ревью: тикет вернулся на ревью (done/in_review при reviewer) после MAX_REVIEW_RETURNS возвратов —
    ревьюера не будим; запись dispatcher, статус needs_owner (строка «ждёт вас» на Диспетчерской, CEO не будится). Только когда последняя запись — владельца (он отправил работу на ревью): запись CEO/ревьюера/dispatcher
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
                 f"Ревьюер ({tkt.reviewer}) вернул работу {returns} раз подряд (спор ревью, не вопрос об исследовании) — на новый круг не будим. Решение за владельцем: "
                 f"ещё один круг (`tickets.py comment {tkt.id} --author ceo --text \"...\" --next {tkt.reviewer}`) "
                 "либо принять/закрыть самому.", now=now)
    T.write_header_updates(path, {"status": "needs_owner", "next": ""}, now=now)
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


WAIT_NOTICE_EVERY = timedelta(days=1)    # одна и та же претензия к wait_for тикета — не чаще раза в сутки
WAIT_EMPTY_AFTER = timedelta(minutes=30)  # waiting с пустым wait_for молчит 30 мин (роль как раз правит шапку)


def notify_wait_for_problem(tkt: T.Ticket, state: dict, now) -> None:
    """`waiting`, который диспетчер не умеет снять, не молчит (разбор 04.10: роли писали в wait_for что попало — тикет ждал
    вечно, ни ошибки, ни строки): непустой `wait_for` неизвестной формы или ПУСТОЙ дольше 30 мин (от `updated`) → строка
    CEO в ceo-inbox, раз в сутки на тикет. Не трогаем: тикет в запуске, ждущий `next` (уйдёт на этом же тике)."""
    if tkt.status != "waiting" or tkt.id in RUNNING or tkt.next_role:
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


def closed_without_review(tkt: T.Ticket) -> bool:
    """done закрыт без нового круга ревью (TK-090 а/б): последняя запись — CEO (закрыл сам или оставил комментарий к
    закрытому) либо итог done с пометкой «закрыто без ревью». Иначе (г) будило бы Судью за каждую такую запись."""
    if tkt.status != "done" or not tkt.log:
        return False
    last = tkt.log[-1]
    return T.author_is(last.author, "ceo") or (last.text.lstrip().startswith("[итог: done]") and NO_REVIEW_MARK in last.text)


def _review_pending(tkt: T.Ticket, state: dict = None) -> bool:
    """done при заданном reviewer, но последняя запись лога не от ревьюера (и не dispatcher) — правило (г)
    ещё запустит ревью: задача не закончена. На пределе возвратов (MAX_REVIEW_RETURNS) ревьюера уже не будят —
    ревью не «ожидается», решает CEO."""
    if tkt.status != "done" or tkt.reviewer not in ROLE_KEYS or closed_without_review(tkt):
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
        if closed_without_review(tkt):
            return None
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
    """v2 (02.10): активных запусков роли по всем тикетам сразу не больше её предела (по умолчанию 1; раньше — только
    при scope="role"; аудит: одну задачу вели три сессии Исследователя, до 6 запусков параллельно). Предел на роль
    настраивается RPV_DISPATCH_ROLE_PARALLEL; на один тикет —
    по-прежнему один запуск роли (RUNNING по ключу тикета). Занята — запуск ждёт следующего тика."""
    return sum(1 for info in RUNNING.values() if info["role"] == role) >= ROLE_PARALLEL.get(role, 1)


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


def _proc_start(pid):
    """Метка времени старта процесса (строка) — вместе с pid однозначно называет процесс, переиспользованный pid даёт
    другую метку. Windows — GetProcessTimes (ctypes), Linux — поле 22 `/proc/<pid>/stat`, иначе `ps -o lstart=`.
    None — процесса нет или метку узнать нельзя."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.OpenProcess.restype = wintypes.HANDLE
            h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return None
            try:
                c, e, k, u = (wintypes.FILETIME() for _ in range(4))
                if not k32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e), ctypes.byref(k), ctypes.byref(u)):
                    return None
                return str((c.dwHighDateTime << 32) | c.dwLowDateTime)
            finally:
                k32.CloseHandle(h)
        except Exception:
            return None
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as fh:
            return "t" + fh.read().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        pass
    got = _ps_field(pid, "lstart")
    return "s" + " ".join(got.split()) if got else None


def _pid_alive(pid, expect_name: str = None, start: str = None) -> bool:
    """Жив ли pid. `start` — метка старта, записанная при запуске (`_proc_start`): сверка pid + старт вместо имени
    образа (роль под npm-установкой — `node`, не `claude`); не совпала — pid занят другим процессом, мёртв; метку узнать
    нельзя — только «процесс есть». Дальше — прежнее: """
    if start:
        now_start = _proc_start(pid)
        if now_start is not None:
            return now_start == start and _pid_alive(pid, "")
        return _pid_alive_name(pid, "")  # метку узнать нельзя (сбой ps) — процесс есть, имя образа не судья
    return _pid_alive_name(pid, expect_name)


PID_CHECK_TRIES = 3          # tasklist на нагруженной машине отвечает позже 5 с — повтор, не «мёртв»
PID_CHECK_TIMEOUT_S = 10


def _pid_alive_name(pid, expect_name: str = None) -> bool:
    """Жив ли pid. Сбой проверки (таймаут tasklist) — не «мёртв»: см. `_pid_state`; здесь «неизвестно» = жив, чтобы
    нагруженная машина не объявляла живую службу мёртвой (В-209 Д-1)."""
    return _pid_state(pid, expect_name) is not False


def _pid_state(pid, expect_name: str = None):
    """True — жив и похож на наш `claude`; False — процесса нет / чужой образ; None — узнать не удалось (таймаут,
    сбой tasklist после повторов). Подстрочный поиск pid ловил чужие совпадения (123 ⊂ 1234) — сравнение PID-колонки
    через `/FO CSV` + имя образа."""
    expect_name = PID_EXPECT_NAME if expect_name is None else expect_name
    if not pid:
        return False
    if os.name == "nt":
        for attempt in range(PID_CHECK_TRIES):
            try:
                out = hide.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                               capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=PID_CHECK_TIMEOUT_S)
            except Exception:
                continue
            if out.returncode:
                continue
            for line in (out.stdout or "").splitlines():
                fields = [f.strip().strip('"') for f in line.split(",")]
                if len(fields) >= 2 and fields[1] == str(pid):
                    return (expect_name or "").lower() in fields[0].lower()
            return False
        return None
    return _pid_alive_posix(pid, expect_name)


def _ps_field(pid, field: str):
    """Поле процесса из `ps -o <field>= -p <pid>` (macOS/BSD, где нет /proc; stdlib). Строка (может быть пустой),
    None — ps нет или процесса нет."""
    try:
        out = hide.run(["ps", "-o", f"{field}=", "-p", str(int(pid))], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=5,
                             env=dict(os.environ, LC_ALL="C", TZ="UTC0"))  # lstart зависит от локали и пояса
    except Exception:
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def _proc_state(pid):
    """Буква состояния процесса: Linux — `/proc/<pid>/status` (R, S, D, T, Z, X…), иначе (macOS) — `ps -o stat=`;
    None — узнать нельзя."""
    try:
        with open(f"/proc/{pid}/status", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith("State:"):
                    return line.split(":", 1)[1].strip()[:1] or None
        return None
    except OSError:
        pass
    return (_ps_field(pid, "stat") or "")[:1] or None


def _proc_comm(pid):
    """Имя образа процесса: `/proc/<pid>/comm` или `ps -o comm=` (macOS: путь к исполняемому — берём имя файла).
    None — узнать нельзя."""
    try:
        with open(f"/proc/{pid}/comm", encoding="utf-8", errors="replace") as fh:
            return fh.read().strip()
    except OSError:
        pass
    comm = _ps_field(pid, "comm")
    return os.path.basename(comm) if comm else None


def _pid_alive_posix(pid, expect_name: str = None) -> bool:
    """Живость pid на Linux/macOS: `kill -0` отвечает и на зомби (завершился, но родитель не вызвал wait) — такой
    процесс мёртв, иначе подхват «живого» прогона после перезапуска ждёт вечно (состояние Z или X). Имя образа
    сверяется всегда, когда его можно узнать (без /proc — через ps): чужой процесс с переиспользованным pid — не наш."""
    try:
        os.kill(pid, 0)
    except PermissionError:
        pass  # процесс есть, но чужого пользователя — имя проверим ниже
    except Exception:
        return False
    if _proc_state(pid) in ("Z", "X"):
        return False
    if not expect_name:
        return True
    comm = _proc_comm(pid)
    return True if comm is None else expect_name.lower() in comm.lower()  # имя узнать нельзя — не валим живость


def _pid_kill(pid) -> None:
    if not pid:
        return
    if os.name == "nt":
        try:
            hide.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, timeout=5)
        except Exception:
            pass
        return
    try:
        os.kill(pid, 15)
    except Exception:
        pass


def acquire_instance_lock(pid_file, expect_name: str = "py"):
    """Замок единственного экземпляра. Проверка-и-запись идут под короткой мьютекс-файлом (`<pid_file>.mx`, O_EXCL):
    без него второй претендент видел файл замка уже созданным, но ещё пустым (pid не записан), считал его битым,
    удалял и брал себе — оба диспетчера «владели» замком (TK-093, гонка в test_watch_round). Файл есть и в нём живой
    чужой процесс — (False, сообщение); процесса нет (упал, зомби) или файл битый — замок забирается; свой pid
    (его записал запускатель) — ок. Pid пишется целиком через временный файл и `os.replace`."""
    pid_file = Path(pid_file)
    me = os.getpid()
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    mx = pid_file.with_name(pid_file.name + ".mx")
    deadline = time.time() + 40
    while True:
        try:
            os.close(os.open(str(mx), os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            break
        except FileExistsError:
            try:
                if time.time() - mx.stat().st_mtime > 30:   # мьютекс держит умерший процесс (секция — миллисекунды)
                    mx.unlink()
                    continue
            except OSError:
                continue
            if time.time() > deadline:
                return False, f"не удалось взять замок {pid_file}: занят мьютекс {mx.name}"
            time.sleep(0.01)
    try:
        try:
            other = int(pid_file.read_text(encoding="utf-8").strip() or 0)
        except (OSError, ValueError):
            other = 0
        if other == me:
            return True, ""
        if other and _pid_alive(other, expect_name):
            return False, f"уже запущен (pid {other}, файл {pid_file.name}) — второй экземпляр не нужен, выхожу"
        tmp = pid_file.with_name(f"{pid_file.name}.{me}.tmp")
        try:
            tmp.write_text(str(me), encoding="utf-8")
            os.replace(tmp, pid_file)   # осиротевший/битый замок забираем, свой пишем целиком
        except OSError as e:
            return False, f"не удалось взять замок {pid_file}: {e}"
        return True, ""
    finally:
        try:
            mx.unlink()
        except OSError:
            pass


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
    return _pid_alive(info.get("pid"), start=info.get("pstart"))


def _kill_proc(info: dict) -> None:
    if info.get("popen") is not None:
        try:
            info["popen"].kill()
            info["popen"].wait(timeout=10)
        except Exception:
            pass
    else:
        _pid_kill(info.get("pid"))


def _kill_tree_nt(pid) -> None:
    """Windows: `taskkill /T /F /PID` — процесс и все его потомки."""
    try:
        hide.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True, timeout=15)
    except Exception:
        pass


def _kill_tree_posix(pid) -> None:
    """Linux/macOS: SIGKILL всей группе процессов роли — запуск идёт со `start_new_session=True`, группа = pid. Процесс не
    лидер своей группы (запущен до этой правки и сидит в группе диспетчера) — только он сам: killpg по такой группе
    снял бы и сам диспетчер."""
    sig = getattr(signal, "SIGKILL", 9)
    try:
        if os.getpgid(pid) == pid:
            os.killpg(pid, sig)
        else:
            os.kill(pid, sig)
    except Exception:
        pass


def _kill_tree(pid) -> None:
    if not pid:
        return
    (_kill_tree_nt if os.name == "nt" else _kill_tree_posix)(pid)


def _stop_run(info: dict) -> bool:
    """Снять запущенную роль всем деревом процессов и проверить, что она умерла (ждём до STOP_VERIFY_S). True — умерла.
    Уже мёртвый процесс не трогаем: его pid ОС могла отдать чужому."""
    pid = info.get("pid") or (info["popen"].pid if info.get("popen") is not None else None)
    if _proc_alive(info):
        _kill_tree(pid)
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


def effort_for(role: str, tkt=None) -> str:
    """v2: усилие запуска — поле `effort:` тикета, иначе умолчание роли (ROLE_EFFORT: исследователь/инженер
    high, Судья xhigh; env RPV_DISPATCH_EFFORT переопределяет умолчания ролей, не поле тикета)."""
    if tkt is not None and getattr(tkt, "effort", ""):
        return tkt.effort
    return ROLE_EFFORT.get(role, "high")


def _py() -> str:
    """Интерпретатор для команд, которые диспетчер выдаёт ролям: на Mac/Linux слова `python` может не быть (TK-110 В-1); есть в PATH — прежнее `python` (кавычки вокруг пути ломают PowerShell)."""
    return "python " if shutil.which("python") else shlex.quote(Path(sys.executable).as_posix()) + " "


def tickets_cli() -> str:
    """Команда tickets.py из папки плагина: проект роль берёт из RPV_PROJECT (его ставит launch_run) и своего cwd."""
    return _py() + shlex.quote((CODE_DIR / "tickets.py").as_posix())


def plan_cli() -> str:
    """plan.py из папки плагина (шаги ролей для веб-табло): проект роль берёт из RPV_PROJECT, как и tickets.py."""
    return _py() + shlex.quote((CODE_DIR / "plan.py").as_posix())


def build_prompt(role: str, tid: str, extra_note: str = None) -> str:
    prompt = PROMPT_TEMPLATE.format(role=role, tid=tid, timeout_min=int(RUN_TIMEOUT // 60),
                                    tickets_cli=tickets_cli(), plan_cli=plan_cli())
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
    state.get("on_met_chain", {}).pop(tid, None)  # запуск роли обрывает цепочку on_met
    if state.get("stopped_runs", {}).pop(tid, None):
        # прошлый запуск этого тикета оборван CEO (`tickets.py stop`): сессия НОВАЯ (без --resume), в промпте — пометка
        sid = None
        extra_note = " ".join(x for x in (extra_note, STOP_NOTE) if x)
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
           "--model", model, "--effort", effort,
           # TK-140: роли без MCP (свой Playwright/pulse на каждый запуск ≈600 МБ); без --mcp-config = ни одного сервера
           "--strict-mcp-config"]
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
    # хуки (role_context/role_memory) ведут состояние и конспект прошлой сессии по тикету; обе пары имён — RPV_* новые,
    # ALPHA_* для хуков, которые ещё читают прежние
    env["RPV_ROLE"] = env["ALPHA_ROLE"] = role
    env["RPV_TICKET"] = env["ALPHA_TICKET"] = tid
    env["RPV_PROJECT"] = str(PROJECT_ROOT)  # tickets.py роли берёт проект отсюда (и из cwd запуска)

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
        popen = _popen(cmd, cwd=str(PROJECT_ROOT), env=env, stdout=out_fh, stderr=err_fh, text=True,
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
    # всегда на (задачу, роль), не зависит от SESSION_SCOPE
    sess_entry = state.setdefault("sessions", {}).setdefault(f"{tid}::{role}", {})
    sess_entry["last_woken"] = T.now_iso(now)
    _record_launch(state, tid, now)
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
    if not session_id or not re.fullmatch(r"[A-Za-z0-9_-]+", str(session_id)):
        return {}
    try:
        path = next(CLAUDE_PROJECTS_DIR.glob(f"*/{session_id}.jsonl"), None)
        if path is None:
            return {}
        t0 = started if isinstance(started, datetime) else datetime.fromisoformat(str(started))
        if t0.tzinfo is None:
            t0 = t0.astimezone()
        per = {}
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"usage"' not in line:
                    continue
                try:
                    row = json.loads(line)
                    ts = datetime.fromisoformat(str(row["timestamp"]).replace("Z", "+00:00"))
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
    RUNS_LOG.parent.mkdir(parents=True, exist_ok=True)
    usage = result.get("usage") or {}
    src = "json"
    if not usage:  # таймаут/убит: токены — из транскрипта сессии с момента старта запуска
        usage = _transcript_usage_since(info.get("session_id"), info.get("started")) or {}
        src = "jsonl" if usage else "none"
    status = "stopped" if stopped else "timeout" if timed_out else ("ok" if result else "no_output")
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
    store = _resume_store(state, tid, role)  # session_id/токены — по SESSION_SCOPE[role]
    sid_used = result.get("session_id") or store.get("session_id")  # сессия этого запуска (убит — id прежней, --resume)
    if result.get("session_id"):
        store["session_id"] = result["session_id"]
    elif timed_out and info.get("session_id") and not store.get("session_id") and             next(CLAUDE_PROJECTS_DIR.glob(f"*/{info['session_id']}.jsonl"), None) is not None:
        store["session_id"] = info["session_id"]  # убитая новая сессия: транскрипт есть — повтор продолжит её, не с нуля
        sid_used = info["session_id"]
    # контекст последнего хода; нет JSON/usage (таймаут, ответ-ошибка) — прежнее значение, не ноль
    store["last_context_tokens"] = _context_tokens_for_store(result, store.get("last_context_tokens", 0), sid_used)

    path = TICKETS_DIR / f"{tid}.md"
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

LIMIT_LABEL_SLACK = timedelta(minutes=2)


def _limit_hit(result: dict) -> bool:
    text = str((result or {}).get("result") or "")
    return (result or {}).get("api_error_status") == 429 or "hit your session limit" in text


def _limit_reset_at(result: dict, now, ref=None) -> datetime:
    """Ближайшее «resets 5am (TZ)» / «resets 3:30pm»; не разобрали — через час (проверим снова, пауза продлится).
    ref — момент ответа (конец запуска; по умолчанию now): метка раньше него — завтра, но метка в пределах
    LIMIT_LABEL_SLACK до ответа (минутное округление) — уже прошла: вернём момент <= now, паузы не будет."""
    m = re.search(r"resets\s+(\d{1,2})(?::(\d{2}))?\s*([ap]m)(?:\s*\(([^)]+)\))?", str((result or {}).get("result") or ""), re.I)
    if not m:
        return now + timedelta(hours=1)
    hour, minute = int(m.group(1)) % 12, int(m.group(2) or 0)
    if m.group(3).lower() == "pm":
        hour += 12
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(m.group(4)) if m.group(4) else now.tzinfo
    except Exception:
        tz = now.tzinfo
    local = (ref or now).astimezone(tz)
    at = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if at <= local - LIMIT_LABEL_SLACK:
        at += timedelta(days=1)
    return at + timedelta(minutes=1)


def _limit_paused(state: dict, now) -> bool:
    until = state.get("limit_pause_until")
    if not until:
        return False
    try:
        return now < T.parse_dt(until)
    except ValueError:
        return False


def _last_run_was_limit(tid: str) -> bool:
    runs = sorted(RUNS_DIR.glob(f"*-{tid}-*.json")) if RUNS_DIR.exists() else []
    return bool(runs) and _limit_hit(_read_run_result(runs[-1]))


def unblock_limit_victims(now) -> None:
    """blocked из-за холостых запусков, чей последний запуск — 429 лимита сессии: вернуть в in_progress (тикет не виноват)."""
    for path in T.list_tickets(TICKETS_DIR):
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
    try:
        pairs = []  # (pid, ppid) совпавших процессов
        if os.name == "nt":
            ps = (f"Get-CimInstance Win32_Process | Where-Object {{ $_.CommandLine -like '*{session_id}*' -and "
                  f"$_.CommandLine -notlike '*Get-CimInstance*' -and $_.ProcessId -ne {os.getpid()} }} | "
                  "ForEach-Object { \"$($_.ProcessId) $($_.ParentProcessId)\" }")
            out = hide.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=30).stdout
            for ln in out.splitlines():
                f = ln.split()
                if len(f) == 2 and all(x.isdigit() for x in f):
                    pairs.append((int(f[0]), int(f[1])))
        else:
            out = hide.run(["ps", "-ww", "-eo", "pid=,ppid=,args="], capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=10).stdout
            for ln in out.splitlines():
                f = ln.split(None, 2)
                if len(f) == 3 and f[0].isdigit() and f[1].isdigit() and str(session_id) in f[2]:
                    pairs.append((int(f[0]), int(f[1])))
        pairs = [(p, pp) for p, pp in pairs if p != os.getpid()]
        found = {p for p, _ in pairs}
        roots = [p for p, pp in pairs if pp not in found]  # обёртка (cmd/node/sh) выше дочернего — берём корень дерева
        return min(roots or found) if found else None
    except Exception:
        return None


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
    STOP_DIR.mkdir(parents=True, exist_ok=True)
    path = STOP_DIR / f"{tid}.json"
    T.atomic_write_text(path, json.dumps({"at": T.now_iso(now), "next": next_role or "", "text": text},
                                         ensure_ascii=False, indent=2))
    return path


def _stop_requests() -> list:
    out = []
    for path in sorted(STOP_DIR.glob("*.json")) if STOP_DIR.exists() else []:
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
    for role in ROLE_KEYS:
        key = f"{tid}::{role}"
        state.setdefault("idle_runs", {}).pop(key, None)
        _reset_same_status(state, key)
        if key in state.get("sessions", {}):
            state["sessions"][key]["retries"] = 0
        store = state.get("ticket_sessions", {}).get(key)
        if store:
            store.pop("session_id", None)
            store.pop("last_context_tokens", None)
    state.get("launch_history", {}).pop(tid, None)


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
    if nxt in ROLE_KEYS:
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
        path = TICKETS_DIR / f"{tid}.md"
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


# --- on_met: продолжение по коду после wait_for (TK-056 п.2; семантика — запись Судьи 05.10 23:34) -------------
ON_MET_TIMEOUT_S = float(P.env("DISPATCH_ON_MET_TIMEOUT_S", "120"))
ON_MET_MAX_CHAIN = 3          # подряд on_met без запуска роли на тикет; 4-й раз — будим владельца
ON_MET_INTERPRETERS = ("python", "python3", "bash")
ON_MET_DIRS = ("tools", ".claude")


def _git_tracked(rel: str) -> bool:
    r = hide.run(["git", "-C", str(PROJECT_ROOT), "ls-files", "--error-unmatch", "--", rel],
                       capture_output=True, timeout=20)
    return r.returncode == 0


def _on_met_argv(spec: str):
    """(argv, None) | (None, причина отказа): argv[0] — python/bash, argv[1] — отслеживаемый git скрипт под tools/ или .claude/."""
    try:
        argv = shlex.split(spec, posix=True)
    except ValueError as e:
        return None, f"разбор команды: {e}"
    if len(argv) < 2 or argv[0] not in ON_MET_INTERPRETERS:
        return None, f"argv[0] должен быть из {ON_MET_INTERPRETERS}, дальше — скрипт"
    rel = argv[1]
    parts = rel.split("/")
    if "\\" in rel or rel.startswith("/") or ".." in parts or parts[0] not in ON_MET_DIRS:
        return None, f"скрипт `{rel}` вне tools/ и .claude/ (пути — с прямыми слешами)"
    try:
        tracked = _git_tracked(rel)
    except Exception as e:
        return None, f"git ls-files: {type(e).__name__}: {e}"
    if not tracked:
        return None, f"скрипт `{rel}` не отслеживается git"
    return argv, None


def _tail(b, limit: int = 1024) -> str:
    text = b.decode("utf-8", "replace") if isinstance(b, (bytes, bytearray)) else (b or "")
    return text.strip()[-limit:]


def _log_on_met_run(tid: str, now, status: str, dur: float) -> None:
    RUNS_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(RUNS_LOG, "a", encoding="utf-8") as fh:
        fh.write(f"{T.now_iso(now)} {tid} on_met reason=on_met attempt=0 session=- cost_usd=- resolved_cost=0.0000 "
                 f"cost_note=asis ticket_spent=0.0000 in_tok=- out_tok=- cr_tok=- cw_tok=- tok_src=none ctx_last=0 "
                 f"ctx_sum=0 dur_s={dur:.1f} status={status}\n")


def run_on_met(path: Path, tkt: T.Ticket, state: dict, now) -> bool:
    """waiting + wait_for выполнен + on_met задан → команда вместо пробуждения LLM. True — тикет обработан этим тиком
    (кандидата в запуск роли не делаем); False — on_met отклонён/сброшен, решает обычное `wait_for-met`."""
    tid = tkt.id
    spec = (tkt.header.get("on_met") or "").strip()
    old_wait = (tkt.header.get("wait_for") or "").strip()
    T.write_header_updates(path, {"on_met": ""}, now=now, stamp_updated=False)  # до запуска: падение не даёт повтора
    chain = state.setdefault("on_met_chain", {})
    if chain.get(tid, 0) >= ON_MET_MAX_CHAIN:
        T.append_log(path, "dispatcher", f"on_met не запущен: {ON_MET_MAX_CHAIN} подряд без запуска роли — будим владельца. "
                     f"Команда: {spec}", now=now)
        chain.pop(tid, None)
        return False
    argv, why = _on_met_argv(spec)
    if argv is None:
        T.append_log(path, "dispatcher", f"on_met отклонён ({why}): {spec} — будим владельца", now=now)
        return False
    chain[tid] = chain.get(tid, 0) + 1
    T.append_log(path, "dispatcher", f"запущен on_met: {argv}", now=now)
    env = dict(os.environ, RPV_TICKET=tid, ALPHA_TICKET=tid)
    t0 = time.time()
    out = err = b""
    code, status = None, "ok"
    try:
        proc = subprocess.Popen(argv, cwd=str(PROJECT_ROOT), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                **hide.hidden())
        try:
            out, err = proc.communicate(timeout=ON_MET_TIMEOUT_S)
            code = proc.returncode
        except subprocess.TimeoutExpired:
            _kill_tree(proc.pid)
            status = "timeout"
            try:
                out, err = proc.communicate(timeout=10)
            except Exception:
                pass
    except Exception as e:
        status, err = "fail", f"{type(e).__name__}: {e}".encode()
    dur = time.time() - t0
    if status == "ok" and code != 0:
        status = "fail"
    _log_on_met_run(tid, now, status, dur)
    head = f"on_met {argv}: код {code}, {dur:.1f} с"
    if status != "ok":
        tmo = f", таймаут {int(ON_MET_TIMEOUT_S)} с, дерево убито" if status == "timeout" else ""
        T.append_log(path, "dispatcher", f"{head}{tmo} — будим владельца.\nstdout: {_tail(out)}\nstderr: {_tail(err)}", now=now)
        chain.pop(tid, None)
        return False
    T.append_log(path, "dispatcher", f"{head}\nstdout: {_tail(out)}", now=now)
    after = T.read_ticket(path)
    new_wait = (after.header.get("wait_for") or "").strip()
    if after.status in ("in_review", "done") or after.next_role == "judge":
        T.write_header_updates(path, {"status": "in_progress", "next": ""}, now=now)
        T.append_log(path, "dispatcher", "on_met не закрывает тикет и не зовёт Судью: возврат в in_progress, будим владельца", now=now)
        chain.pop(tid, None)
        return True
    if after.status == "waiting" and new_wait and new_wait != old_wait:
        return True  # цепочка: следующий этап ждёт своё условие, LLM не нужен
    if after.status == "waiting":
        T.write_header_updates(path, {"status": "in_progress", "wait_for": ""}, now=now)
    chain.pop(tid, None)
    return True


IDLE_SLO_MIN = float(P.env("IDLE_SLO_MIN", "10"))
IDLE_GRACE_S = 600.0
INVARIANT_GRACE_S = float(os.environ.get("RPV_DISPATCH_INVARIANT_GRACE_S") or 600.0)  # TK-076 п.6: открытый тикет без хода дольше этого — владельца будим сами
LIMIT_PAUSE_MAX = timedelta(hours=1)  # пауза 429 не длиннее часа: дальше пробный запуск (429 бесплатен), новая метка — новая пауза


def _waits_without_condition(tkt: T.Ticket, now) -> bool:
    if tkt.status != "waiting" or tkt.owner not in ROLE_KEYS or tkt.next_role:
        return False
    if (tkt.header.get("wait_for") or "").strip():
        return False
    try:
        return (now - T.parse_dt(tkt.header.get("updated", ""))).total_seconds() > IDLE_GRACE_S
    except Exception:
        return False


def _open_owner_question(tid: str) -> bool:
    """Есть вопрос владельцу (ask.py) по этому тикету без ответа — ожидание ответа и есть ход."""
    qdir = TICKETS_DIR.parent / "pulse" / "questions"
    for p in qdir.glob(f"q-{tid}-*.json"):
        try:
            q = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(q, dict) and q.get("process") == tid and not q.get("answered_at"):
            return True
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
    """TK-076 п.6: у открытого тикета есть ход — роль запущена / next / годный wait_for / вопрос владельцу. Здесь —
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
    if _open_owner_question(tkt.id):
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
    if tkt.reviewer in ROLE_KEYS and _review_returns(state or {}, tkt.id) < MAX_REVIEW_RETURNS:  # предел — вопрос CEO
        return "in_review: последняя запись — ревьюера, статус не сменён, next не задан"
    return None


def enforce_move_invariant(path: Path, tkt: T.Ticket, state: dict, now) -> "T.Ticket":
    """Нарушение инварианта: будим владельца (status → in_progress, запись диспетчера) + строка CEO (раз на эпизод)."""
    reason = _no_move_reason(tkt, now, state)
    if not reason:
        return tkt
    key = f"{tkt.id}|{tkt.header.get('updated', '')}"
    sig = state.setdefault("invariant_signaled", {})
    if sig.get(tkt.id) != key:
        sig[tkt.id] = key
        append_ceo_inbox(tkt.id, "нет-хода", f"{reason} дольше {int(INVARIANT_GRACE_S // 60)} мин — "
                         + (f"владелец {tkt.owner} разбужен" if tkt.owner in ROLE_KEYS else "владельца-роли нет, нужен CEO"), now)
    if tkt.owner not in ROLE_KEYS:
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
    cycle = T.wait_cycle(TICKETS_DIR, tkt.id, parsed[1])
    if not cycle or tkt.id != min(cycle[:-1]):
        return
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


def _alert_idle(day: str, rec: dict, now) -> None:
    note = (f"простой за {day}: {downtime.total_min(rec):.0f} мин при SLO {IDLE_SLO_MIN:.0f} "
            f"(готовая работа без исполнителя {rec['idle_s'] / 60:.0f}, ожидание без условия {rec['wait_s'] / 60:.0f}, "
            f"молчание диспетчера {rec['stall_s'] / 60:.0f}, тормоз запусков {rec.get('throttle_s', 0) / 60:.0f}; "
            f"сумма корзин)")
    try:
        import busclient
        if not os.environ.get("RPV_BUS_DISABLE") and busclient.config()[0]:
            busclient.post("служба.простой.превышен", {"note": note}, f"idle-slo-{day}")  # недоступна — spool, дошлётся
            return
    except Exception:
        pass
    append_ceo_inbox("*", "idle-slo", note, now)  # шина выключена — файл


def _account_downtime(state: dict, now, **flags) -> None:
    try:
        day, rec, breached = downtime.account(state, now, poll_s=POLL_INTERVAL, slo_min=IDLE_SLO_MIN, **flags)
        try:
            import busclient
            state["bus_url"] = "" if os.environ.get("RPV_BUS_DISABLE") else busclient.config()[0]
        except Exception:
            state["bus_url"] = ""
        if breached:
            _alert_idle(day, rec, now)
    except Exception as e:  # счётчик не должен ронять тик
        print(f"[dispatch] downtime: {type(e).__name__}: {e}", file=sys.stderr, flush=True)


def sweep_closed_worktrees() -> None:
    """TK-104: копии закрытых тикетов убираются (грязное — коммитом в ветку); ошибка уборки тик не роняет."""
    try:
        status = {}
        for p in T.list_tickets(TICKETS_DIR):
            try:
                t = T.read_ticket(p)
                status[t.id] = t.status
            except Exception:
                continue
        for line in WH.sweep(PROJECT_ROOT, status):
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
    try:  # TK-109 п.16: недошедшие записи ответов владельца с табло
        import ask
        ask.flush_pending()
    except Exception as e:
        print(f"[dispatch] досылка ответов: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
    unblock_limit_victims(now)
    paused = _limit_paused(state, now)  # TK-070 п.2: пока лимит сессии не сброшен — новых запусков нет, тикеты не трогаем
    candidates = []  # (path, ticket, decision) — кого можно запустить; порядок и лимиты — ниже
    waiting_nocond = False  # TK-076 п.4: роль ждёт без условия (ни wait_for, ни next) дольше IDLE_GRACE_S
    for path in T.list_tickets(TICKETS_DIR):
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
            _expire_ceo_handoff(tkt, state)
            notify_status_for_ceo(tkt, state, now)
            notify_done(tkt, state, now)
            notify_wait_for_problem(tkt, state, now)

            tid = tkt.id
            if tid in RUNNING:
                continue
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
        if paused or len(RUNNING) >= MAX_PARALLEL:
            break
        if tid in RUNNING or path.stem in RUNNING:
            continue  # на один тикет — один запуск роли (RUNNING по stem файла; tkt.id из шапки мог разойтись с ним)
        if _role_busy(decision.role):
            continue  # у роли уже предел запусков (на любых задачах) — ждёт следующего тика
        if _rate_limited(state, tid, now):
            throttled += 1
            continue  # MAX_RUNS_PER_TICKET_HOUR/MIN_GAP_S — пауза, не ошибка; попробуем следующим тиком
        if decision.header_updates:
            _apply_header_updates(path, decision.header_updates, now)
        launch_run(path, decision.role, state, now, reason=decision.reason)
        launched += 1

    idle_now = not RUNNING and not launched and not paused
    _account_downtime(state, now, ready_unserved=(len(candidates) - launched - throttled > 0 and idle_now),
                      throttled=(throttled > 0 and idle_now), limit_paused=(bool(paused) and bool(candidates) and not RUNNING),
                      work_present=bool(candidates) or bool(RUNNING), waiting_nocond=waiting_nocond)
    state["last_tick"] = T.now_iso(now)  # судья TK-002 п.2а: сторож проверяет диспетчер жив по этому
    save_state(state)
    return launched


def _start_bus_link():
    """Шина событий: включена, только если задан RPV_BUS_URL (и нет RPV_BUS_DISABLE); любая ошибка — работаем по таймеру."""
    try:
        import busclient
        if os.environ.get("RPV_BUS_DISABLE") or not busclient.config()[0]:
            return None
        link = bus_link.Link(lambda kind, note: append_ceo_inbox(
            kind.split(".")[1] if kind.startswith("задача.") else "bus", kind, note), on_event=record_wait_event,
            ceo_wake=ceo_queue_wake if os.environ.get("RPV_CEO_WAKE") == "1" else None)  # TK-094: CEO-будильник по запросу
        link.start()
        return link
    except Exception as e:
        print(f"[dispatch] шина не запущена: {type(e).__name__}: {e}", file=sys.stderr)
        return None


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
    link = _start_bus_link()
    global WAIT_ASYNC, _LINK
    _LINK = link
    WAIT_ASYNC = True
    threading.Thread(target=_wait_poller, daemon=True, name="wait-poller").start()
    while True:
        acks = link.take_ack() if link else set()  # события, полученные ДО этого тика — подтверждаем после него
        try:
            tick()
            if link:
                if not bus_link.ack("dispatcher", acks):
                    link.give_back(acks)  # ack не дошёл — повтор на следующем тике (TK-055)
                link.maybe_snapshot([T.read_ticket(p) for p in T.list_tickets(TICKETS_DIR)])
        except Exception as e:
            print(f"[dispatch] tick error: {type(e).__name__}: {e}", file=sys.stderr)
            if link:
                link.give_back(acks)  # тик упал — события не подтверждены, следующий тик подтвердит
        if link:
            link.wake.wait(POLL_INTERVAL)
            link.wake.clear()
        else:
            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    if not sys.stdout.isatty():  # демон с перенаправленным выводом: чужой Ctrl+C общей консоли его не убивает (TK-072)
        import signal
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    sys.exit(main())
