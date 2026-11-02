"""Настройки стража: корни своей папки и защищённые пути из `.claude/rpv.json`; `configure` их ставит."""
import json
import os
import re


def _env(name, default=""):
    """Переменная `RPV_<имя>`."""
    return os.environ.get("RPV_" + name) or default


def norm(p):
    return p.strip().strip("'\"").replace("\\", "/").lower()


def project_roots(project_dir):
    """Корни своей папки: каталог проекта (`c:/proj/` и путь Git Bash `/c/proj/`), строчными, с `/` на конце. Сессия в
    worktree проекта (`<проект>/.claude/worktrees/<имя>`) получает и корень основного проекта, а с ним все его worktree."""
    p = str(project_dir).replace("\\", "/").lower().rstrip("/") + "/"
    roots = [p]
    i = p.find("/.claude/worktrees/")
    if i > 0:
        roots.append(p[:i + 1])
    out = []
    for r in roots:
        m = re.match(r"^([a-z]):/(.*)$", r)
        out.extend((r, f"/{m.group(1)}/{m.group(2)}") if m else (r,))
    return tuple(out)


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


def configure(project_dir):
    global LOCAL_ROOTS, WRITE_HOME_ROOTS, PROJECT_DIR, PROTECTED, PROTECTED_ERROR
    LOCAL_ROOTS = project_roots(project_dir)
    WRITE_HOME_ROOTS = memory_roots(project_dir)
    PROJECT_DIR = str(project_dir)
    PROTECTED, PROTECTED_ERROR = load_protected(project_dir)


_PROJECT = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
SCRATCH = "/appdata/local/temp/claude/"
# защищённые пути проекта — из `<проект>/.claude/rpv.json` (ключ `protected_paths`); нет настройки — защиты нет.
# PROJECT_DIR — тикеты запуска (согласие на публикацию).
LOCAL_ROOTS = WRITE_HOME_ROOTS = PROTECTED = ()
PROJECT_DIR, PROTECTED_ERROR = str(_PROJECT), None


configure(_PROJECT)
