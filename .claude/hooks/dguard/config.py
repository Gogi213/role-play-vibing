"""Настройки стража: корни своей папки, защищённые пути и удалённые машины из `.claude/rpv.json`; `configure` их ставит."""
import json
import os
import re


def _env(name, default=""):
    """Переменная `RPV_<имя>`."""
    return os.environ.get("RPV_" + name) or default


def norm(p):
    return p.strip().strip("'\"").replace("\\", "/").lower()


def project_roots(project_dir):
    """Корни своей папки: каталог проекта (`c:/proj/` и путь Git Bash `/c/proj/`), строчными, с `/` на конце."""
    p = str(project_dir).replace("\\", "/").lower().rstrip("/") + "/"
    m = re.match(r"^([a-z]):/(.*)$", p)
    return (p, f"/{m.group(1)}/{m.group(2)}") if m else (p,)


def memory_roots(project_dir):
    """Автопамять проекта относительно домашнего каталога: `.claude/projects/<путь проекта через «-»>/memory/`."""
    enc = re.sub(r"[^a-z0-9]", "-", str(project_dir).lower())
    return (f".claude/projects/{enc}/memory/",)


def _load_rpv(project_dir):
    """`<проект>/.claude/rpv.json` → (объект, ошибка). Нет файла — ({}, None)."""
    try:
        with open(os.path.join(str(project_dir), ".claude", "rpv.json"), encoding="utf-8-sig") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}, None
    except (OSError, ValueError) as e:                      # ValueError — и JSONDecodeError, и UnicodeDecodeError
        return {}, f"файл не прочитан: {type(e).__name__}: {e}"[:160]
    return (data, None) if isinstance(data, dict) else ({}, "корень файла — не объект JSON")


def _str_list(data, key):
    """Ключ — список непустых строк (нет ключа — пусто) → (кортеж, ошибка)."""
    raw = data.get(key)
    if raw is None:
        return (), None
    if not isinstance(raw, list) or not all(isinstance(x, str) and norm(x).rstrip("/") for x in raw):
        return (), f"{key} — не список непустых строк"
    return tuple(raw), None


def load_protected(project_dir, rpv=None):
    """Защищённые пути проекта: `<проект>/.claude/rpv.json`, ключ `protected_paths` — список шаблонов путей (glob) →
    (шаблоны, ошибка). Нет файла или ключа — защиты нет. Файл есть, но не читается, не JSON или ключ не список непустых
    строк — ошибка: страж отказывает любому удалению и перезаписи (fail-closed), пока файл не исправят. Шаблон приводится
    к виду путей стража (строчные, `/`, без хвостового `/`); `..` в шаблоне недопустимо."""
    data, err = rpv or _load_rpv(project_dir)
    if err:
        return (), err
    raw, err = _str_list(data, "protected_paths")
    if err:
        return (), err
    out = tuple(norm(x).rstrip("/") for x in raw)
    bad = [x for x in out if ".." in x.split("/")]
    return ((), f"шаблон «{bad[0]}» недопустим (`..`)") if bad else (out, None)


def machine_rules(data):
    """Удалённые машины из настроек проекта (объект rpv.json) → (корни, стадия, закрытые хосты как regex, ошибка).
    `guard_remote_roots` — каталоги, где на любых удалённых машинах можно удалять (нет ключа — нигде); `guard_stage` —
    оперативная стадия (целиком); `guard_forbidden_hosts` — хосты/IP, где нельзя ничего. Неверный тип — ошибка
    (fail-closed, как у `protected_paths`)."""
    roots, e1 = _str_list(data, "guard_remote_roots")
    hosts, e2 = _str_list(data, "guard_forbidden_hosts")
    stage = data.get("guard_stage") or ""
    e3 = None if isinstance(stage, str) else "guard_stage — не строка"
    hosts = tuple(h.strip().lower() for h in hosts if h.strip())
    closed = re.compile("|".join(re.escape(h) for h in hosts), re.I) if hosts else None
    return (tuple(r.strip().lower() for r in roots if r.strip()), (stage if not e3 else "").strip().lower().rstrip("/"),
            closed, e1 or e2 or e3)


def push_scope_rule(data):
    """`guard_push_scope` — пуш в main/master уносит только коммиты принятого тикета (нет ключа — выключено, как раньше) →
    (включено, ошибка). Не true/false — ошибка (fail-closed, как у `protected_paths`)."""
    raw = data.get("guard_push_scope", False)
    return (raw, None) if isinstance(raw, bool) else (False, "guard_push_scope — не true/false")


def configure(project_dir):
    global LOCAL_ROOTS, WRITE_HOME_ROOTS, PROJECT_DIR, PROTECTED, PROTECTED_ERROR, REMOTE_ROOTS, STAGE, CLOSED_RE, PUSH_SCOPE
    LOCAL_ROOTS = project_roots(project_dir)
    WRITE_HOME_ROOTS = memory_roots(project_dir)
    PROJECT_DIR = str(project_dir)
    rpv = _load_rpv(project_dir)
    PROTECTED, PROTECTED_ERROR = load_protected(project_dir, rpv)
    REMOTE_ROOTS, STAGE, CLOSED_RE, err = machine_rules(rpv[0])
    PUSH_SCOPE, scope_err = push_scope_rule(rpv[0])
    PROTECTED_ERROR = PROTECTED_ERROR or err or scope_err


_PROJECT = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
SCRATCH = "/appdata/local/temp/claude/"
# пути проекта, защищённые пути и удалённые машины — из `<проект>/.claude/rpv.json`; проект сам решает, что в них
# вписать; нет настройки — защиты и удалённых корней нет. PROJECT_DIR — тикеты запуска (согласие на публикацию).
LOCAL_ROOTS = WRITE_HOME_ROOTS = REMOTE_ROOTS = PROTECTED = ()
PROJECT_DIR, STAGE, CLOSED_RE, PROTECTED_ERROR, PUSH_SCOPE = str(_PROJECT), "", None, None, False


configure(_PROJECT)
