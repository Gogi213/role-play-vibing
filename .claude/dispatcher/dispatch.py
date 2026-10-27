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
import transcript  # noqa: E402
import worktree_hygiene as WH  # noqa: E402
import blind  # noqa: E402
import downtime  # noqa: E402
import notify  # noqa: E402
import routes as ROUTES  # noqa: E402
import report as REPORT  # noqa: E402

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
    P.set_root(PROJECT_ROOT)  # CP8: настройки проекта — `.claude/rpv.json` (P.setting)
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
# Пинг-понг ревью (аудит-2): сколько раз ревьюер может вернуть работу владельцу (in_review → вернул → снова in_review).
# После MAX_REVIEW_RETURNS возвратов тикет, снова пришедший на ревью, ревьюеру не отдаётся: запись dispatcher, тикет
# закрыт «не сделано» с остатком (последний возврат ревьюера) в логе — не blocked и не вопрос владельцу (TK-233, 13.10:
# потолок ремонта 2; был 3 и needs_owner). Число НАЗНАЧЕНО владельцем 13.10, не измерено.
MAX_REVIEW_RETURNS = 2
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
# Усилие ролей (слово владельца 14.10): Судья — Opus 5.5 high, Инженер — Sonnet 5.5 xhigh, Исследователь — Sonnet 5.5 high.
# Для Судьи и Инженера усилие ЖЁСТКОЕ: поле `effort:` тикета их не меняет (раньше оно перекрывало усилие всех ролей тикета,
# и `effort: medium` молча понижал Судью — замер docs/bench/2026-10-14.md). Для Исследователя поле по-прежнему действует
# (`tickets.py new --effort`), см. effort_for().
ROLE_EFFORT = {"judge": "high", "engineer": "xhigh", "researcher": "high"}
FIXED_EFFORT_ROLES = ("judge", "engineer")  # усилие этих ролей не зависит от поля `effort:` тикета


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
HAIKU_ALLOWED_KINDS = {"file-move", "table-format", "publish", "log-compact"}


# Сессия: каждое пробуждение роли — новая (бабочка); память между запусками — блокнот роли и лог тикета.
# --resume — только повтор после таймаута в том же запуске (TK-056 п.3′). Предел запусков на роль — _role_busy
# (`role_parallel` в rpv.json, по умолчанию 1); на один тикет — один запуск роли.
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
    "host:<ssh-алиас>:<путь к файлу итога задания> --by ЧЧ:ММ` (срок обязателен, GMT+4); формы: host:<ssh-алиас>:<путь | unit:имя>, file:<путь>, ticket:<ID>), и выйди, "
    "не жди в сессии. Трать минимум: "
    "самый короткий путь к результату задачи; траты каждого запуска записываются и сравниваются с "
    "результатом. Сделай следующий шаг и сдай итог командой "
    "`{tickets_cli} result {tid} <{verbs}> --why \"что сделал, что дальше\"` (--why — не длиннее 200 знаков) "
    "ДО истечения лимита (доказательство: {proofs}; неполная команда — отказ с подсказкой): итог обязателен, "
    "частичный прогресс не провал. "
    "Кого будить дальше и status/wait_for ставит таблица маршрутов — --next и шапку руками не правь; "
    "blocked (задача встала) и ask-owner (развилка, вопрос) уходят Судье; владельцу вопрос о содержании "
    "исследования (какие гипотезы, гиперпараметры, метрики, что хотим увидеть) переадресует Судья; CEO не будят. "
    "`{tickets_cli} comment {tid} --author {role} --text \"...\"` — только промежуточная заметка без смены "
    "хода. @упоминания в тексте никого не будят."
)

RUNNING = {}  # tid -> {role, popen, pid, started, attempt, run_file, err_file, out_fh, err_fh, reason}
# popen=None у записей, восстановленных из state.json["active_runs"] после перезапуска диспетчера
# (recover_active_runs) — тогда живость и остановка идут по pid (_pid_alive/_kill), не по Popen.


@dataclass
class Decision:
    role: str
    reason: str
    header_updates: dict = None


# --- состояние ------------------------------------------------------------------------------

def load_state() -> dict:
    return P.read_json(STATE_FILE, {})


def save_state(state: dict) -> None:
    P.write_json(STATE_FILE, state, sort_keys=True)   # с повторами: на Windows читатель держит файл


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
_WAIT_RC = {}     # (алиас, "path", арг) -> текст файла итога задания (`*.rc`, tools/jobrun.sh), когда путь появился — см. wait_rc
WAIT_CHECK_CACHE_S = 60.0  # синхронный путь (--once, тесты): ssh-проверка одного условия не чаще раза в столько секунд
WAIT_ERR_EVERY_S = 600.0  # ошибка ssh по одному условию — строка в dispatch.err.log не чаще раза в 10 мин
_WAIT_ERR_LAST = {}  # ключ условия -> time.time() последней строки
_UNIT_RUNNING = ("active", "activating", "reloading", "deactivating", "refreshing")

# Ожидание `host:<алиас>:…` — ssh к алиасу из ~/.ssh/config (любая машина, плагин своих настроек машин не держит). Боевой цикл
# (WAIT_ASYNC) ssh в тике не трогает: первую проверку нового условия и сверку всех ждущих раз в WAIT_RECON_S (один ssh на
# машину) делает поток _wait_poller, поэтому конец задания виден не позже периода сверки.
WAIT_ASYNC = False  # True ставит main() для боевого цикла; тесты и --once — синхронный путь
WAIT_POLL_S = 300.0
_WAIT_WATCH = set()  # ключи (алиас, what, арг), которые ждут тикеты — их опрашивает _wait_poller
_WAIT_NEW = threading.Event()
WAIT_RECON_S = 300.0  # сверка всех ждущих host:… одним ssh на машину
WATCHED_ALIASES = set()  # без rpv.json машин нет; тесты подменяют на модуле


def watched_aliases() -> set:
    """Машины, где сторож гоняет адаптеры заданий (`job_owners_cmd`, `stray_cmd`): `watched_aliases` из
    `.claude/rpv.json` (список ssh-алиасов), иначе WATCHED_ALIASES (пусто)."""
    v = P.setting("watched_aliases")
    return {str(a).strip() for a in v if str(a).strip()} if isinstance(v, list) else WATCHED_ALIASES
# TK-217: детектор «диспетчер слеп» — недоступность машины с ожиданиями дольше порога → тревога вне этой машины (вердикт Судьи 12.10)
BLIND = blind.Tracker(blind.ALERT_S, blind.REPEAT_S)
WAIT_RECON_BLIND_S = 30.0  # пока цель недоступна — сверка раз в 30 с (+ссылка таймаут 20 с), чтобы после возврата связи проснуться ≤ 60 с (стенд 12.10: 45 с таймаут + 60 с период дали 150 с)
_RECON_LAST = 0.0


def _wait_poller() -> None:
    """Боевой цикл: ssh вне тика — первая проверка нового условия из _WAIT_WATCH и сверка раз в WAIT_RECON_S."""
    global _RECON_LAST
    while True:
        for ckey in list(_WAIT_WATCH):
            if ckey in _WAIT_CACHE:
                continue
            try:
                _host_probe(*ckey)
            except Exception as e:
                _wait_err(f"poller:{ckey}", f"{type(e).__name__}: {e}")
        if _WAIT_WATCH and time.time() - _RECON_LAST >= (WAIT_RECON_BLIND_S if BLIND.any_down() else WAIT_RECON_S):
            _RECON_LAST = time.time()
            try:
                _reconcile()
            except Exception as e:
                _wait_err("reconcile", f"{type(e).__name__}: {e}")
        _WAIT_NEW.wait(min(WAIT_POLL_S, WAIT_RECON_BLIND_S if BLIND.any_down() else WAIT_RECON_S))
        _WAIT_NEW.clear()


def _wait_err(key: str, msg: str) -> None:
    now_ts = time.time()
    last = _WAIT_ERR_LAST.get(key)
    if last is not None and (now_ts - last) < WAIT_ERR_EVERY_S:
        return
    _WAIT_ERR_LAST[key] = now_ts
    print(f"[dispatch] {T.now_iso()} wait_for {key}: {msg}", file=sys.stderr, flush=True)


def _ssh_cmd(alias: str, remote_cmd: str) -> list:
    """ssh к алиасу из ~/.ssh/config: пользователь, ключ, known_hosts, прыжковый хост — там же. Алиас — только вида ssh-имени
    (ticket.host_alias_ok): имя с дефисом впереди ssh принял бы за свою опцию."""
    if not T.host_alias_ok(alias):
        raise RuntimeError(f"алиас ssh {alias!r} не годится (буквы, цифры, . _ -; не с дефиса)")
    return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", alias, remote_cmd]


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


def _reads_body(arg: str) -> bool:
    """Содержимое пути нужно диспетчеру: файл хода (`.json`) и файл итога задания (`.rc`) читаются, прочее — `test -e`."""
    return arg.endswith((".json", ".rc"))


def _path_met(key: tuple, body: str) -> bool:
    """Путь по ssh есть и прочитан: файл хода — готов при done >= total; файл итога `.rc` — готов всегда, а текст запоминается
    (wait_rc) — роль увидит, чем кончилось задание; прочий путь — достаточно того, что он есть."""
    if key[2].endswith(".rc"):
        _WAIT_RC[key] = " ".join(body.split())[:300]
        return True
    progress = _progress_done(body) if key[2].endswith(".json") else None
    return True if progress is None else progress


def _host_wait_met(alias: str, what: str, arg: str) -> bool:
    """`host:<алиас>:<путь>` — путь на машине существует (`.json` с done/total — done >= total; `.rc` — файл итога jobrun.sh);
    `host:<алиас>:unit:<имя>` — юнит не работает (`systemctl is-active` ≠ active: задание закончилось или упало).
    Судья 27.09 («можно потом»): без кэша ssh дёргается на каждый ждущий тикет каждые 15 с — результат на
    WAIT_CHECK_CACHE_S; ошибка ssh (код 255, таймаут) = «не выполнено» + строка в dispatch.err.log раз в 10 мин."""
    ckey = (alias, what, arg)
    if WAIT_ASYNC:  # основной поток ssh не трогает: проверку делает _wait_poller (TK-055)
        if ckey not in _WAIT_WATCH:
            _WAIT_WATCH.add(ckey)
            _WAIT_NEW.set()  # первую проверку нового условия поток делает сразу, не через WAIT_POLL_S
        return (_WAIT_CACHE.get(ckey) or (0, False))[1]
    cached = _WAIT_CACHE.get(ckey)
    now_ts = time.time()
    if cached and (now_ts - cached[0]) < WAIT_CHECK_CACHE_S:
        return cached[1]
    return _host_probe(alias, what, arg)


def _host_probe(alias: str, what: str, arg: str) -> bool:
    ckey = (alias, what, arg)
    now_ts = time.time()
    if what == "unit":
        remote = f"systemctl is-active {shlex.quote(arg)}"
    elif _reads_body(arg):
        remote = f"cat {_remote_test_arg(arg)}"
    else:
        remote = f"test -e {_remote_test_arg(arg)}"
    label = f"host:{alias}:{'unit:' if what == 'unit' else ''}{arg}"
    result = False
    try:
        r = hide.run(_ssh_cmd(alias, remote), capture_output=True, timeout=15)
        _blind_note(alias, r.returncode != 255)
        out = (getattr(r, "stdout", b"") or b"").decode("utf-8", "replace")
        if what == "unit":
            state = (out.strip().splitlines() or [""])[0]
            if r.returncode == 255 or not state:
                _wait_err(label, f"ssh: код {r.returncode}, {_ssh_stderr(r)}")
            else:
                result = state not in _UNIT_RUNNING
        elif r.returncode == 0:
            result = _path_met(ckey, out)
        elif r.returncode != 1:  # 1 — файла нет (штатно); остальное (255…) — сбой ssh/хоста
            _wait_err(label, f"ssh: код {r.returncode}, {_ssh_stderr(r)}")
    except Exception as e:
        _blind_note(alias, False)
        _wait_err(label, f"ssh: {type(e).__name__}: {e}")
    _WAIT_CACHE[ckey] = (now_ts, result)
    return result


def _recon_script(keys: list) -> str:
    parts = []
    for i, (alias, what, arg) in enumerate(keys):
        parts.append(f"echo @@{i}")
        if what == "unit":
            parts.append(f"systemctl is-active {shlex.quote(arg)} 2>&1 | head -1; echo '@@rc 0'")
        else:
            parts.append(f"{'cat' if _reads_body(arg) else 'test -e'} {_remote_test_arg(arg)} 2>/dev/null; echo \"@@rc $?\"")
    return "; ".join(parts)


def _parse_recon(out: str, n: int) -> dict:
    """Ответ _recon_script → {i: {"rc": int|None, "body": str}}."""
    res, cur = {}, None
    for line in out.splitlines():
        line = line.rstrip("\r")
        if line.startswith("@@") and line[2:].isdigit() and int(line[2:]) < n:
            cur = int(line[2:])
            res[cur] = {"rc": None, "body": []}
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
        return state not in _UNIT_RUNNING if state else None
    if v["rc"] == 0:
        return _path_met((alias, what, arg), v["body"])
    return False if v["rc"] == 1 else None


def _reconcile() -> None:
    """Раз в WAIT_RECON_S один ssh на машину проверяет ВСЕ ждущие host:…: пропущенный конец задания стоит не дороже одного шага
    сверки. Машины — параллельно (TK-217: таймаут одной не задерживает другую, иначе 60-секундная сверка при слепоте не держится)."""
    by_alias = {}
    for k in list(_WAIT_WATCH):
        by_alias.setdefault(k[0], []).append(k)
    threads = [threading.Thread(target=_reconcile_alias, args=(alias, sorted(keys)), daemon=True)
               for alias, keys in by_alias.items()]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)


def _reconcile_alias(alias: str, keys: list) -> None:
    try:
        r = hide.run(_ssh_cmd(alias, _recon_script(keys)), capture_output=True, timeout=20 if BLIND.any_down() else 45)
    except Exception as e:
        _blind_note(alias, False)
        _wait_err(f"reconcile:{alias}", f"ssh: {type(e).__name__}: {e}")
        return
    out = (getattr(r, "stdout", b"") or b"").decode("utf-8", "replace")
    if r.returncode == 255 or not out.strip():
        _blind_note(alias, False)
        _wait_err(f"reconcile:{alias}", f"ssh: код {r.returncode}, {_ssh_stderr(r)}")
        return
    _blind_note(alias, True)
    now_ts = time.time()
    for i, v in _parse_recon(out, len(keys)).items():
        if v["rc"] is None:
            continue
        _, what, arg = keys[i]
        result = _recon_result(alias, what, arg, v)
        if result is None:
            continue
        _WAIT_CACHE[keys[i]] = (now_ts, result)


# --- итоги ожиданий внешних заданий (CP6, критерий 12) ----------------------------------------------

JOBS_LOG_NAME = "jobs.jsonl"  # строка на исход ожидания: {"ts", "ticket", "wait_for", "outcome": "met"|"overdue", "rc": текст файла итога | null}


def wait_rc(spec: str) -> str:
    """Текст файла итога задания (`host:<алиас>:<путь>.rc`), если диспетчер его прочитал; иначе пусто."""
    parsed = T.parse_wait_for(spec)
    if not parsed or parsed[0] != "host" or parsed[2] != "path":
        return ""
    return _WAIT_RC.get((parsed[1], "path", parsed[3]), "")


def _rc_failed(rc: str) -> bool:
    """Файл итога (`SERVICE_RESULT=… EXIT_STATUS=…`) говорит о неудаче: результат не success или код не 0. Чужой вид текста — нет."""
    kv = dict(re.findall(r"(\w+)=(\S*)", rc))
    return kv.get("SERVICE_RESULT", "success") != "success" or kv.get("EXIT_STATUS", "0") not in ("0", "")


def record_job_outcome(tid: str, spec: str, outcome: str, rc: str = "", now=None) -> bool:
    """Строка в jobs.jsonl на исход ожидания внешнего задания (ежедневный отчёт считает по ней «кончилось / потеряно»):
    `met` — условие закрылось, владелец будится; `overdue` — срок `--by` вышел (+15 мин), сторож разбудил владельца.
    Одна строка на (тикет, wait_for, исход): True — записано сейчас, False — такая уже есть."""
    path = DISPATCHER_DIR / JOBS_LOG_NAME
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if (row.get("ticket"), row.get("wait_for"), row.get("outcome")) == (tid, spec, outcome):
                return False
    except OSError:
        pass
    row = {"ts": T.now_iso(now), "ticket": tid, "wait_for": spec, "outcome": outcome, "rc": rc or None}
    try:
        DISPATCHER_DIR.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError as e:  # счёт не должен ронять тик
        print(f"[dispatch] {JOBS_LOG_NAME}: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
    return True


def note_wait_met(path: Path, tkt: T.Ticket, now) -> T.Ticket:
    """waiting с условием внешнего задания (host:, file:, job:) закрылось: исход `met` в jobs.jsonl и, если машина отдала файл
    итога, запись в лог тикета — роль, проснувшись, видит, чем кончилось задание. Свежий тикет, если запись добавлена."""
    spec = (tkt.header.get("wait_for") or "").strip()
    if not T.wait_needs_by(spec) or not check_wait_for(spec):
        return tkt
    rc = wait_rc(spec)
    if not record_job_outcome(tkt.id, spec, "met", rc, now) or not rc:
        return tkt
    T.append_log(path, "dispatcher", f"ожидание `{spec}` закончилось; итог задания: {rc}"
                 + (" — задание НЕ успешно, разберись до продолжения" if _rc_failed(rc) else ""), now=now)
    return T.read_ticket(path)


# --- слепота (TK-217) --------------------------------------------------------------------------

def _blind_note(alias: str, ok: bool) -> None:
    BLIND.record(f"ssh:{alias}", ok, time.time())


def _blind_sound() -> None:
    try:
        import winsound
        for _ in range(3):
            winsound.Beep(1000, 400)
    except Exception:
        pass  # не Windows/нет звука — остаются файл и ceo-wake.log


def _blind_tick() -> None:
    """Раз в тик: тревога по каждой цели отдельно (ALERT-BLIND.txt + звук + ceo-wake.log). Канал не использует ни одна из машин
    с ожиданиями. Возврат цели — строка «зрение вернулось» и немедленная сверка."""
    global _RECON_LAST
    now = time.time()
    active = {f"ssh:{k[0]}" for k in list(_WAIT_WATCH)}
    acts = BLIND.evaluate(now, active)
    for act, tgt, secs in acts:
        text = (f"диспетчер слеп: {tgt} недоступна {secs} с" if act != "recovered"
                else f"зрение вернулось: {tgt} снова отвечает (простой {secs} с)")
        _mech_log("*", "blind", text)  # CP5: механика — в лог; стоит работа — скажет одна тревога «работа стоит»
        if act == "recovered":
            _RECON_LAST = 0.0  # сверка сразу
            _WAIT_NEW.set()
    down = BLIND.alerted_targets()  # файл синхронен набору каждый тик: цель ушла из active — файл снимается
    path = DISPATCHER_DIR / "ALERT-BLIND.txt"
    try:
        if down:
            lines = [f"{t}: недоступна с {T.now_iso(datetime.fromtimestamp(since).astimezone())}" for t, since in sorted(down.items())]
            waits = ", ".join(sorted(t.id for t in map(T.read_ticket, T.list_tickets(TICKETS_DIR)) if t.status == "waiting"))
            path.write_text("ДИСПЕТЧЕР СЛЕП (TK-217)\n" + "\n".join(lines) + f"\nждут (waiting): {waits or '—'}\n", encoding="utf-8")
        elif path.exists():
            path.unlink()
    except OSError:
        pass
    if any(a in ("alert", "repeat") for a, _, _ in acts):
        threading.Thread(target=_blind_sound, daemon=True, name="blind-sound").start()


def _ssh_stderr(r) -> str:
    return ((getattr(r, "stderr", b"") or b"").decode("utf-8", "replace").strip().splitlines() or ["—"])[-1][:200]


# --- ceo-inbox ---------------------------------------------------------------------------------

# Сигнал CEO — строка в ceo-inbox.md (детали; читает `tickets.py inbox`) и короткая копия в ceo-wake.log (на нём Monitor CEO).
def _ceo_file_write(tid: str, kind: str, note: str, now=None) -> None:
    CEO_INBOX.parent.mkdir(parents=True, exist_ok=True)
    with open(CEO_INBOX, "a", encoding="utf-8") as fh:
        fh.write(f"- {T.now_iso(now)} {tid} [{kind}] {note}\n")
    with open(CEO_WAKE_LOG, "a", encoding="utf-8") as fh:
        fh.write(f"{T.now_iso(now)} {tid} {kind}\n")


# CP5 (критерий 8): механика CEO не будит — строка в mechanics.log. Владельцу и CEO — одна тревога «работа стоит»
# (_stall_alarm): её считают по тому, стоит ли готовая работа, а не по виду поломки
MECH_KINDS = {"blind", "idle-slo", "нет-хода", "цикл-ожиданий", "stop-failed", "parse-error"}


def _mech_log(tid: str, kind: str, note: str, now=None) -> None:
    try:
        DISPATCHER_DIR.mkdir(parents=True, exist_ok=True)
        with (DISPATCHER_DIR / "mechanics.log").open("a", encoding="utf-8") as f:
            f.write(f"{T.now_iso(now)} {tid} [{kind}] {' '.join(str(note).split())}\n")
    except OSError:
        pass


def append_ceo_inbox(tid: str, kind: str, note: str, now=None) -> None:
    """Сигнал CEO: строка в ceo-inbox.md + копия в ceo-wake.log. Вид механики (MECH_KINDS) — только в mechanics.log."""
    if kind in MECH_KINDS:
        _mech_log(tid, kind, note, now)
        return
    _ceo_file_write(tid, kind, note, now)


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
    ревьюера не будим; запись dispatcher с остатком, статус done («не сделано»). Только когда последняя запись — владельца (он отправил работу на ревью): запись CEO/ревьюера/dispatcher
    не повод. Явный `next` (CEO будит ревьюера или другую роль) не перебиваем. Возвращает свежий тикет."""
    if tkt.status not in ("done", "in_review") or tkt.reviewer not in P.ROLES:
        return tkt
    returns = _review_returns(state, tkt.id)
    if returns < MAX_REVIEW_RETURNS or tkt.next_role in P.ROLES or tkt.next_role == "ceo":
        return tkt
    last_author = tkt.log[-1].author if tkt.log else ""
    if not T.author_is(last_author, tkt.owner):
        return tkt
    rest = next((e.text.strip() for e in reversed(tkt.log) if T.author_is(e.author, tkt.reviewer)), "")
    T.append_log(path, "dispatcher",
                 f"Не сделано: ревьюер ({tkt.reviewer}) вернул работу {returns} раз подряд (потолок ремонта) — тикет закрыт "
                 f"без нового круга. Остаток (последний возврат ревьюера): {rest[:1500] or '(записи ревьюера нет)'}. "
                 f"Взять остаток — новый тикет со ссылкой на задачу владельца (`new --ref`).", now=now)
    T.write_header_updates(path, {"status": "done", "next": ""}, now=now)
    return T.read_ticket(path)


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
    if tkt.status != "done" or tkt.reviewer not in P.ROLES or closed_without_review(tkt):
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
    last = tkt.log[-1] if tkt.log else None
    if tkt.id in notified and last and T.author_is(last.author, "ceo") and last.text.lstrip().startswith("[итог: done]"):
        notified[tkt.id] = marker  # CEO повторно закрыл уже сообщённый done (гонка с закрытием по слиянию) — второй строки нет
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
        return _pid_state(pid, "") is not False  # метку узнать нельзя (сбой ps) — процесс есть, имя образа не судья
    return _pid_state(pid, expect_name) is not False  # «неизвестно» (таймаут tasklist) = жив: В-209 Д-1


PID_CHECK_TRIES = 3          # tasklist на нагруженной машине отвечает позже 5 с — повтор, не «мёртв»
PID_CHECK_TIMEOUT_S = 10


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


def _kill(pid, tree: bool = False, force: bool = False) -> None:
    """Единственный способ снять процесс. tree — со всеми потомками: Windows `taskkill /T`, POSIX — SIGKILL группе роли
    (запуск со `start_new_session=True`, группа = pid; не лидер своей группы — только он сам: killpg по такой группе
    снял бы и сам диспетчер). force — SIGKILL вместо SIGTERM (Windows: taskkill всегда `/F`)."""
    if not pid:
        return
    try:
        if os.name == "nt":
            hide.run(["taskkill", *(["/T"] if tree else []), "/F", "/PID", str(pid)], capture_output=True, timeout=15)
        elif tree and os.getpgid(pid) == pid:
            os.killpg(pid, getattr(signal, "SIGKILL", 9))
        else:
            os.kill(pid, getattr(signal, "SIGKILL", 9) if tree or force else 15)
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
        _kill(info.get("pid"))


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


def _read_mcp_servers(path: Path, project_key: str | None = None) -> dict:
    """`mcpServers` из json-файла (`~/.claude.json`, `.mcp.json`); с `project_key` — ещё и записи проекта
    (`projects[<путь>].mcpServers`, они перекрывают пользовательские). Нет файла/битый json — пусто."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out = dict(data.get("mcpServers") or {}) if isinstance(data, dict) else {}
    if project_key and isinstance(data, dict):
        projects = data.get("projects") or {}
        for k, v in projects.items():
            if isinstance(v, dict) and os.path.normcase(os.path.normpath(k)) == os.path.normcase(os.path.normpath(project_key)):
                out.update(v.get("mcpServers") or {})
    return out


def role_mcp_args(run_stem: str) -> list[str]:
    """TK-171: MCP-серверы роли — `role_mcp` из `.claude/rpv.json`. Нет ключа/`all` — флагов нет, роль видит всё, что видит обычный
    `claude` (поведение до 1.8.26); `none` — `--strict-mcp-config` без конфига, ни одного сервера (TK-140: свой
    Playwright на каждый запуск ≈ 600 МБ); список имён — временный json только с этими серверами
    (user `~/.claude.json`, проектные записи оттуда, `<проект>/.mcp.json`; позднее перекрывает раннее) и
    `--strict-mcp-config --mcp-config <json>`. Имени нет ни в одном источнике — предупреждение в stderr, сервер пропущен."""
    mode = P.setting("role_mcp", "all")
    word = mode.strip().lower() if isinstance(mode, str) else ""
    if word in ("all", "none"):
        return [] if word == "all" else ["--strict-mcp-config"]
    names = [str(n).strip() for n in (mode if isinstance(mode, list) else str(mode).split(",")) if str(n).strip()]
    servers = _read_mcp_servers(Path.home() / ".claude.json", str(PROJECT_ROOT))
    servers.update(_read_mcp_servers(PROJECT_ROOT / ".mcp.json"))
    picked = {n: servers[n] for n in names if n in servers}
    for n in names:
        if n not in picked:
            print(f"[dispatch] rpv.json role_mcp: сервер {n!r} не найден ни в ~/.claude.json, ни в .mcp.json — пропущен",
                  file=sys.stderr, flush=True)
    cfg = RUNS_DIR / f"{run_stem}.mcp.json"
    cfg.write_text(json.dumps({"mcpServers": picked}, ensure_ascii=False, indent=2), encoding="utf-8")
    return ["--strict-mcp-config", "--mcp-config", str(cfg)]


def plugin_dir_args() -> list[str]:
    """CP11: `RPV_PLUGIN_DIR` — роли идут с плагином из этой папки (`claude --plugin-dir`, перекрывает установленный
    плагин того же имени). Нужна проверке рабочей копии на чистом проекте (`tools/e2e_clean_project.py`); не задана —
    флага нет, роли видят установленный плагин."""
    d = (P.env("PLUGIN_DIR", "") or "").strip()
    return ["--plugin-dir", d] if d else []


def effort_for(role: str, tkt=None) -> str:
    """Усилие запуска. Судья и Инженер — всегда ROLE_EFFORT (поле `effort:` тикета для них не действует: владелец 14.10).
    Остальные роли — поле `effort:` тикета, иначе умолчание роли (high)."""
    if role in FIXED_EFFORT_ROLES:
        return ROLE_EFFORT[role]
    if tkt is not None and getattr(tkt, "effort", ""):
        return tkt.effort
    return ROLE_EFFORT.get(role, "high")


def _py() -> str:
    """Интерпретатор для команд, которые диспетчер выдаёт ролям: на Mac/Linux слова `python` может не быть (TK-110 В-1); есть в PATH — прежнее `python` (кавычки вокруг пути ломают PowerShell)."""
    return "python " if shutil.which("python") else shlex.quote(Path(sys.executable).as_posix()) + " "


def tickets_cli() -> str:
    """Команда tickets.py из папки плагина: проект роль берёт из RPV_PROJECT (его ставит launch_run) и своего cwd."""
    return _py() + shlex.quote((CODE_DIR / "tickets.py").as_posix())


def build_prompt(role: str, tid: str, extra_note: str = None) -> str:
    prompt = PROMPT_TEMPLATE.format(role=role, tid=tid, timeout_min=int(RUN_TIMEOUT // 60),
                                    tickets_cli=tickets_cli(), verbs="|".join(ROUTES.verbs_for(role)),
                                    proofs=ROUTES.proofs_for(role))
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
    store = _resume_store(state, tid, role)
    if result.get("session_id"):
        store["session_id"] = result["session_id"]
    elif (timed_out and info.get("session_id") and not store.get("session_id")
          and transcript.find(CLAUDE_PROJECTS_DIR, info["session_id"]) is not None):
        store["session_id"] = info["session_id"]  # убитая новая сессия: транскрипт есть — повтор продолжит её, не с нуля

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

LIMIT_LABEL_SLACK = timedelta(minutes=2)


def _limit_hit(result: dict) -> bool:
    text = str((result or {}).get("result") or "")
    return (result or {}).get("api_error_status") == 429 or "hit your session limit" in text


API_PAUSE = timedelta(minutes=5)  # ошибка API (обрыв сети, 5xx): запуски на паузе, по образцу лимита сессии
API_AUTH_PAUSE = timedelta(minutes=30)  # вход истёк: повтор бессмыслен, пока CEO не войдёт заново
MAX_API_ERRORS = 3  # подряд ошибок API у (задача, роль) → blocked с настоящей причиной


def _api_error(result: dict) -> bool:
    """Запуск оборвала ошибка API (обрыв ECONNRESET, 5xx, вход истёк), кроме 429 — это лимит (`_limit_hit`)."""
    r = result or {}
    if _limit_hit(r):
        return False
    return r.get("terminal_reason") == "api_error" or (bool(r.get("is_error")) and r.get("api_error_status") is not None)


def _auth_error(result: dict) -> bool:
    r = result or {}
    return r.get("api_error_status") == 401 or "Failed to authenticate" in str(r.get("result") or "")


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
    path = TICKETS_DIR / f"{tid}.md"
    if _auth_error(result):
        append_ceo_inbox(tid, "auth", f"{role}: вход Claude истёк — {text}. Войти заново (`claude /login`)", now)
    elif n >= MAX_API_ERRORS and path.exists():
        counts.pop(key, None)
        _block_ticket(path, tid, role, f"ошибка API x{n}: {text}", state, now,
                      f"Запусков роли {role} подряд оборвала ошибка API: {n} — {text}. Задача заблокирована, нужен CEO.")
    save_state(state)


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
    STOP_DIR.mkdir(parents=True, exist_ok=True)
    path = STOP_DIR / f"{tid}.json"
    P.write_json(path, {"at": T.now_iso(now), "next": next_role or "", "text": text})
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
ON_MET_TIMEOUT_S = 120.0
ON_MET_MAX_CHAIN = 3          # подряд on_met без запуска роли на тикет; 4-й раз — будим владельца
ON_MET_INTERPRETERS = ("python", "python3", "bash")
ON_MET_DIRS = ("tools", ".claude")


def _git_tracked(rel: str) -> bool:
    return hide.git(PROJECT_ROOT, "ls-files", "--error-unmatch", "--", rel, timeout=20).returncode == 0


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
    env = dict(os.environ, RPV_TICKET=tid)
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
            _kill(proc.pid, tree=True)
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
    cycle = T.wait_cycle(TICKETS_DIR, tkt.id, parsed[1])
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
STALLED = []  # заголовки тикетов, стоящих в этом тике (инвариант хода, цикл ожиданий, ожидание без условия, не взят)


def _stall_title(tkt: T.Ticket) -> str:
    return " ".join((tkt.header.get("title") or tkt.id).split())[:70]


def _guarded(label: str, fn, *args) -> None:
    """Побочная работа тика (тревога, отчёт, Надзиратель, вопрос владельцу) не должна ронять тик."""
    try:
        fn(*args)
    except Exception as e:
        print(f"[dispatch] {label}: {type(e).__name__}: {e}", file=sys.stderr, flush=True)


def _stall_alarm(state: dict, now, stalled: list) -> None:
    """CP5 (критерии 8, 9, 10): одна тревога владельцу — в Telegram (`notify.py`) и строкой CEO — когда готовая работа
    стоит STALL_ALARM_MIN минут подряд; одна на эпизод. Пошла снова — короткое «снова идёт»."""
    st = state.get("stall") or {}
    if not stalled:
        if st.get("sent"):
            mins = (now - T.parse_dt(st["since"])).total_seconds() / 60
            text = f"Работа снова идёт (стояла {mins:.0f} мин)."
            notify.send(text, log_dir=DISPATCHER_DIR)
            append_ceo_inbox("*", "работа-идёт", text, now)
        state["stall"] = {}
        return
    st.setdefault("since", T.now_iso(now))
    mins = (now - T.parse_dt(st["since"])).total_seconds() / 60
    if mins >= STALL_ALARM_MIN and not st.get("sent"):
        names = sorted(set(stalled))
        text = (f"Работа стоит {mins:.0f} мин: {len(names)} задач(и) ждут, их никто не делает — "
                + "; ".join(f"«{n}»" for n in names[:3]) + (f" и ещё {len(names) - 3}" if len(names) > 3 else "")
                + ". CEO разбирается; если сессии CEO нет — откройте её и скажите «почему стоит работа».")
        notify.send(text, log_dir=DISPATCHER_DIR)
        append_ceo_inbox("*", "работа-стоит", text, now)
        st["sent"] = True
    state["stall"] = st


REPORT_AT = "10:00"  # CP10: местное время ежедневного отчёта владельцу, ЧЧ:ММ; проект — `report_at` в rpv.json
_REPORT_PROC = None  # процесс отчёта (report.py): короткоживущий, тик его не ждёт


def _daily_report(state: dict, now) -> None:
    """CP10 (критерии 2, 9): раз в сутки в REPORT_AT — отчёт владельцу отдельным коротким процессом (`report.py`): медленный
    `claude -p` тик не держит. Метка `last_report_at` в state.json ставится до запуска: упавший отчёт не повторяется каждый
    тик. Диспетчер стоял в REPORT_AT — отчёт уходит на первом тике после и один раз. Первый тик проекта только ставит метку:
    отчёта за время, когда диспетчера не было, не бывает."""
    global _REPORT_PROC
    if _REPORT_PROC is not None and _REPORT_PROC.poll() is None:
        return
    last = state.get("last_report_at")
    if not last:
        state["last_report_at"] = T.now_iso(now)
        return
    if not REPORT.due(last, now, str(P.setting("report_at", REPORT_AT))):
        return
    state["last_report_at"] = T.now_iso(now)
    cmd = [sys.executable, str(CODE_DIR / "report.py"), "--project", str(PROJECT_ROOT), "--since", last,
           "--until", T.now_iso(now)]
    DISPATCHER_DIR.mkdir(parents=True, exist_ok=True)
    with open(DISPATCHER_DIR / "report.log", "a", encoding="utf-8") as log:
        _REPORT_PROC = _popen(cmd, cwd=str(PROJECT_ROOT), stdout=log, stderr=log, **hide.hidden())


OWNER_ASK = re.compile(r"^\[итог:\s*ask-owner\]")


def _owner_questions(state: dict, now) -> None:
    """Критерии 5 и 9: вопрос Судьи владельцу (тикет `needs_owner`) — сразу одним сообщением в Telegram, а не только строкой
    CEO: сессии CEO может не быть. Повод и сам вопрос; отвечают CEO. Один раз на вопрос (тикет и время записи вопроса)."""
    sent = state.setdefault("owner_asked", {})
    for path in T.list_tickets(TICKETS_DIR):
        try:
            tkt = T.read_ticket(path)
        except Exception:
            continue
        if tkt.status != "needs_owner":
            continue
        q = next((e for e in reversed(tkt.log) if T.author_is(e.author, "judge") and OWNER_ASK.match(e.text.strip())), None)
        if q is None or sent.get(tkt.id) == q.ts_raw:
            continue
        why = re.search(r"повод: ([\w-]+)", q.text)
        body = re.sub(r"^\[итог:\s*ask-owner\]\s*(?:повод: [\w-]+)?\s*[—-]?\s*", "", q.text.strip())
        title = " ".join((tkt.header.get("title") or tkt.id).split())[:80]
        notify.send(f"Вопрос вам ({why.group(1) if why else 'без повода'}): «{title}» — {' '.join(body.split())[:400]} "
                    f"Ответьте CEO.", log_dir=DISPATCHER_DIR)
        sent[tkt.id] = q.ts_raw


OVERSEER_EVERY_S = 15 * 60.0  # CP14: Надзиратель — не чаще раза в 15 мин и только если тикеты менялись
_OVERSEER_PROC = None  # процесс Надзирателя (overseer.py): короткоживущий, тик его не ждёт


def _overseer(state: dict, now) -> None:
    """CP14 (критерии 1, 3, 4, 10): раз в OVERSEER_EVERY_S, если с прошлого взгляда менялся хоть один тикет, — один взгляд
    Надзирателя отдельным процессом (`overseer.py`: правила «дорого» и «всплеск» и Haiku по новым записям; находки — Судье записью тикета).
    Первый тик только ставит метку `overseer_at`; прошлый взгляд ещё идёт — ждём его."""
    global _OVERSEER_PROC
    if _OVERSEER_PROC is not None and _OVERSEER_PROC.poll() is None:
        return
    last = state.get("overseer_at")
    if not last:
        state["overseer_at"] = T.now_iso(now)
        return
    if (now - T.parse_dt(last)).total_seconds() < OVERSEER_EVERY_S:
        return
    changed = max((f.stat().st_mtime for f in TICKETS_DIR.glob("TK-*.md")), default=0.0)
    if changed <= T.parse_dt(last).timestamp():
        return
    state["overseer_at"] = T.now_iso(now)
    cmd = [sys.executable, str(CODE_DIR / "overseer.py"), "--project", str(PROJECT_ROOT)]
    DISPATCHER_DIR.mkdir(parents=True, exist_ok=True)
    with open(DISPATCHER_DIR / "overseer.run.log", "a", encoding="utf-8") as log:
        _OVERSEER_PROC = _popen(cmd, cwd=str(PROJECT_ROOT), stdout=log, stderr=log, **hide.hidden())


def _account_downtime(state: dict, now, **flags) -> None:
    try:
        downtime.account(state, now, poll_s=POLL_INTERVAL, slo_min=IDLE_SLO_MIN, **flags)  # минуты простоя — в отчёт (CP10)
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
    unblock_limit_victims(now)
    paused = _limit_paused(state, now)  # TK-070 п.2: пока лимит сессии не сброшен — новых запусков нет, тикеты не трогаем
    candidates = []  # (path, ticket, decision) — кого можно запустить; порядок и лимиты — ниже
    STALLED.clear()
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
    print(f"[dispatch] v2 loop every {POLL_INTERVAL}s, MAX_PARALLEL={max_parallel()}, run timeout "
          f"{RUN_TIMEOUT / 60:.0f} min, CLAUDE_BIN={CLAUDE_BIN}")
    global WAIT_ASYNC
    WAIT_ASYNC = True
    threading.Thread(target=_wait_poller, daemon=True, name="wait-poller").start()
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
