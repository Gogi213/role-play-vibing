"""Конфигурация плагина Role Play Vibing: корень проекта, `.claude/rpv.json`, переменные окружения `RPV_*`.

Всё проектное (роли, модели, потолки расходов, проверки сторожа, профиль хуков) живёт в `<проект>/.claude/rpv.json`;
плагин сам хранит только код и шаблоны. Порядок приоритета: переменная окружения → rpv.json → умолчание (`DEFAULTS`).
Только stdlib. Файл не падает на битом JSON: берутся умолчания, ошибка остаётся в `CONFIG_ERRORS` (её показывает doctor).
"""
from __future__ import annotations

import copy
import json
import os
import re
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = SCRIPTS_DIR.parent
CONFIG_NAME = "rpv.json"

# Роли по умолчанию. Свои — в rpv.json → "roles": добавить/переопределить поля; `null` убирает роль.
#   name — как роль называется в тексте; match — подстроки названия сессии Claude Desktop (нижний регистр);
#   executor — диспетчер запускает её по тикетам (`claude -p`); model/effort/session_scope — для запусков диспетчера;
#   no_haiku — такой роли нельзя ставить `--executor haiku` (результат не отдают дешёвой модели).
DEFAULT_ROLES = {
    "researcher": {"name": "Исследователь", "match": ["исследователь", "researcher"], "executor": True,
                   "effort": "high", "session_scope": "ticket", "no_haiku": True},
    "engineer": {"name": "Инженер", "match": ["инженер", "engineer"], "executor": True,
                 "effort": "high", "session_scope": "ticket"},
    "judge": {"name": "Судья", "match": ["судья", "judge"], "executor": True, "model": "opus",
              "effort": "xhigh", "session_scope": "ticket", "no_haiku": True},
    "ceo": {"name": "CEO", "match": ["ceo"], "executor": False},
}

DEFAULTS = {
    "project_name": "",            # как называть проект в промптах; пусто — имя каталога проекта
    "default_role": "",            # роль интерактивной сессии, если название сессии недоступно (CLI); пусто — не задана
    "roles": DEFAULT_ROLES,
    "ssh": {                       # вторая машина (wait_for `ssh:<путь>` и проверка сторожа ssh_alerts); пусто = выкл.
        "host": "", "key": "", "known_hosts": "", "connect_timeout": 8, "cache_s": 60,
    },
    "dispatcher": {
        "claude_bin": "",          # пусто — `claude` из PATH
        "permission_mode": "bypassPermissions",   # запуск `claude -p` без человека: иначе роль встанет на разрешении
        "poll_interval_s": 15, "max_parallel": 3, "run_timeout_s": 1200,
        "default_model": "sonnet", "haiku_model": "haiku",
        "rotate_tokens": 120000,
        "run_cap_usd": 8, "min_run_cap_usd": 3, "min_retry_budget_usd": 1,
        "hour_cost_usd": 15, "daily_cost_usd": 150,
        "max_runs_per_ticket_hour": 6, "min_gap_s": 60, "summary_hours": 1,
        "budget_presets": {"S": 3, "M": 10, "L": 25}, "default_budget": "M",
        "haiku_kinds": ["file-move", "table-format", "publish"],
        "prompt_extra": "",        # дописывается в конец промпта каждого запуска (правила проекта одной-двумя фразами)
    },
    "watch": {
        "interval_s": 120, "repeat_hours": 2, "long_repeat_hours": 24, "summary_hours": 1,
        "orphan_hours": 2, "dispatch_stale_min": 5,
        # плагины проверок: [{"name": "ssh_alerts", "enabled": false, ...}] — по умолчанию пусто, ничего не включено
        "checks": [],
    },
    "hooks": {"profile": "standard", "disabled": []},
    "guards": {
        "context_warn_tokens": 450000, "context_repeat_every": 10,
        "loop_threshold": 5, "loop_window": 30,
        "stop_guard": {"enabled": True, "max_blocks": 3},
    },
    "delete_guard": {
        "scope": "roles",          # roles — только запуски диспетчера; all — все сессии (профиль strict включает all)
        "local_roots": [],         # доп. каталоги, где можно удалять (корень проекта и scratchpad разрешены всегда)
        "remote_roots": [],        # доп. префиксы на удалённых машинах, например "~/work/"
        "stage_dirs": [],          # каталоги целиком разрешены (например /dev/shm/stage)
        "forbidden_segments": [],  # имена каталогов, которые удалять нельзя ни при каких путях ("backup", "raw")
        "protected_hosts": "",     # регэксп имён/хостов, на которых удаление запрещено всегда
        "protected_ssh_ports": [], # порты ssh/rsync закрытых узлов
    },
}

CONFIG_ERRORS: list = []


def deep_merge(base, over):
    """Рекурсивное слияние словарей; `None` в `over` удаляет ключ; списки и скаляры заменяются целиком."""
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if v is None:
            out.pop(k, None)
        elif isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def find_project_root(start=None) -> Path:
    """Корень проекта: RPV_PROJECT_DIR → CLAUDE_PROJECT_DIR → первый каталог вверх от start/cwd, где лежит
    `.claude/rpv.json` или `.claude/tickets/` → иначе сам start/cwd."""
    for var in ("RPV_PROJECT_DIR", "CLAUDE_PROJECT_DIR"):
        v = os.environ.get(var)
        if v and Path(v).is_dir():
            return Path(v).resolve()
    cur = Path(start or os.getcwd()).resolve()
    for p in (cur, *cur.parents):
        if (p / ".claude" / CONFIG_NAME).is_file() or (p / ".claude" / "tickets").is_dir():
            return p
    return cur


def config_path(root=None) -> Path:
    env = os.environ.get("RPV_CONFIG")
    if env:
        return Path(env)
    return Path(root or find_project_root()) / ".claude" / CONFIG_NAME


def load_config(root=None) -> dict:
    """Умолчания + rpv.json проекта. Битый файл — умолчания и запись в CONFIG_ERRORS."""
    path = config_path(root)
    user = {}
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            if isinstance(data, dict):
                user = data
            else:
                CONFIG_ERRORS.append(f"{path}: корень должен быть объектом JSON")
        except Exception as e:
            CONFIG_ERRORS.append(f"{path}: {type(e).__name__}: {e}")
    cfg = deep_merge(DEFAULTS, user)
    # роли: пустой словарь/`null` у роли убирает её; пользовательские поля накладываются на умолчания
    cfg["roles"] = {k: v for k, v in cfg["roles"].items() if isinstance(v, dict)}
    return cfg


def pick(env_name, value, cast=float):
    """Значение из окружения `env_name` (если задано и приводится), иначе `value`."""
    raw = os.environ.get(env_name)
    if raw is not None and str(raw).strip() != "":
        try:
            return cast(str(raw).strip())
        except (TypeError, ValueError):
            pass
    return cast(value) if cast in (int, float) else value


def role_keys(cfg, executors_only=False) -> tuple:
    return tuple(k for k, v in cfg["roles"].items() if not executors_only or v.get("executor"))


def role_name(cfg, key) -> str:
    return (cfg["roles"].get(key) or {}).get("name") or key


def role_matches(cfg) -> list:
    """[(подстрока названия сессии в нижнем регистре, ключ роли)] в порядке ролей; первое совпадение выигрывает."""
    out = []
    for key, v in cfg["roles"].items():
        for m in (v.get("match") or [key]):
            out.append((str(m).lower(), key))
    return out


def ceo_key(cfg) -> str:
    """Роль-человек: первая без executor (по умолчанию `ceo`); ей адресуется `--next ceo` и ceo-inbox."""
    for k, v in cfg["roles"].items():
        if not v.get("executor"):
            return k
    return "ceo"


def project_name(cfg, root=None) -> str:
    return cfg.get("project_name") or (Path(root or find_project_root()).name or "проект")


def role_env(name="RPV_ROLE", cfg=None):
    """Роль из переменной окружения (любая роль из конфига, включая CEO) или None."""
    cfg = cfg or load_config()
    r = (os.environ.get(name) or "").strip().lower()
    return r if r in cfg["roles"] else None


def is_dispatched() -> bool:
    """Запуск диспетчера: он ставит RPV_DISPATCHED=1 (+ RPV_ROLE, RPV_TICKET). Роль из RPV_ROLE, выставленная
    вручную (`RPV_ROLE=ceo claude`), запуском диспетчера не считается — человек в сессии есть."""
    return os.environ.get("RPV_DISPATCHED") == "1"


def ssh_settings(cfg=None) -> dict:
    """Вторая машина: rpv.json → "ssh" с переопределением RPV_SSH_HOST / RPV_SSH_KEY / RPV_SSH_KNOWN_HOSTS."""
    cfg = cfg or load_config()
    s = dict(cfg.get("ssh") or {})
    for field, env in (("host", "RPV_SSH_HOST"), ("key", "RPV_SSH_KEY"), ("known_hosts", "RPV_SSH_KNOWN_HOSTS")):
        if os.environ.get(env):
            s[field] = os.environ[env]
    return s


def ssh_base_cmd(settings: dict, connect_timeout=None) -> list:
    """`ssh [-i ключ] [-o UserKnownHostsFile=…] -o BatchMode=yes -o ConnectTimeout=N <host>` — без команды.
    Ключ и known_hosts передаются явно, только если заданы (на машинах с нестандартным HOME умолчания ssh ломаются)."""
    cmd = ["ssh"]
    if settings.get("key"):
        cmd += ["-i", str(settings["key"])]
    if settings.get("known_hosts"):
        cmd += ["-o", f"UserKnownHostsFile={settings['known_hosts']}"]
    timeout = connect_timeout if connect_timeout is not None else settings.get("connect_timeout", 8)
    cmd += ["-o", "BatchMode=yes", "-o", f"ConnectTimeout={int(timeout)}", str(settings.get("host") or "")]
    return cmd


def posix(path) -> str:
    """Путь с прямыми слэшами — одинаково понимают bash, PowerShell и Python на Windows."""
    return Path(path).as_posix()


def tickets_cli() -> str:
    """Готовая команда тикетов для текста роли: `"<python>" "<scripts>/tickets.py"` (прямые слэши, в кавычках)."""
    return f'"{posix(sys.executable)}" "{posix(SCRIPTS_DIR / "tickets.py")}"'


TICKET_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,32}")
