"""Версия плагина у сессии или службы против установленной (`installed_plugins.json`) — TK-039, П2 обратной связи invoice.

После `claude plugin update` открытая сессия продолжает работать на хуках старого каталога (`CLAUDE_PLUGIN_ROOT`):
новых запретов стража на неё нет. Хуки SessionStart и UserPromptSubmit берут `stale_line`, `doctor` — `root_of` +
`installed` для процессов диспетчера и сторожа. Только стандартная библиотека: хуки грузят модуль без остального диспетчера.
"""
import json
import os

PLUGIN_JSON = os.path.join(".claude-plugin", "plugin.json")
SCOPE_ORDER = ("local", "project", "user")   # более узкая область перекрывает более широкую


def _norm(path) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def root_of(path):
    """Корень плагина (каталог с `.claude-plugin/plugin.json`) для файла или каталога внутри него; нет — None."""
    cur = os.path.abspath(str(path))
    while True:
        if os.path.isfile(os.path.join(cur, PLUGIN_JSON)):
            return cur
        up = os.path.dirname(cur)
        if up == cur:
            return None
        cur = up


def meta(root) -> dict:
    """`plugin.json` корня плагина (пусто, если не читается)."""
    data = _read_json(os.path.join(str(root), PLUGIN_JSON))
    return data if isinstance(data, dict) else {}


def installed(project, root, config_dir=None):
    """(версия, каталог установки) плагина `root` для `project` из `installed_plugins.json`.

    Область: `user` или запись, чей `projectPath` — этот проект; самая узкая (local > project > user) перекрывает.
    Если каталог `root` — сам среди таких записей, возвращается он. Нет файла, записи или имени плагина — None."""
    name = meta(root).get("name")
    cfg = config_dir or os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    data = _read_json(os.path.join(cfg, "plugins", "installed_plugins.json"))
    plugins = data.get("plugins") if isinstance(data, dict) else None
    if not name or not isinstance(plugins, dict):
        return None
    mine = [e for key, entries in plugins.items() if key.split("@")[0] == name and isinstance(entries, list)
            for e in entries if isinstance(e, dict) and e.get("installPath") and e.get("version")]
    scoped = [e for e in mine if e.get("scope", "user") == "user"
              or (e.get("projectPath") and _norm(e["projectPath"]) == _norm(project))]
    own = next((e for e in scoped if _norm(e["installPath"]) == _norm(root)), None)
    rank = lambda e: SCOPE_ORDER.index(e["scope"]) if e.get("scope") in SCOPE_ORDER else len(SCOPE_ORDER)  # noqa: E731
    best = own or min(scoped, key=rank, default=None)
    return (str(best["version"]), best["installPath"]) if best else None


def mismatch(project, root, config_dir=None):
    """(версия каталога `root`, установленная версия), если `root` — старый установленный каталог этого плагина; иначе None.

    Сравнение — только для каталогов одной папки установки (…/<плагин>/<версия>): запуск из исходников или `--plugin-dir`
    не «устарел». Версия каталога — из его `plugin.json`."""
    inst = installed(project, root, config_dir)
    if inst is None or _norm(inst[1]) == _norm(root) or _norm(os.path.dirname(inst[1])) != _norm(os.path.dirname(root)):
        return None
    own = meta(root).get("version")
    return (str(own), inst[0]) if own and str(own) != inst[0] else None


def stale_line(project, root=None, config_dir=None):
    """Строка-предупреждение для сессии на старой версии плагина или None (`root` — `CLAUDE_PLUGIN_ROOT`, иначе по файлу)."""
    root = root or os.environ.get("CLAUDE_PLUGIN_ROOT") or root_of(__file__)
    got = mismatch(project, root, config_dir) if root else None
    if not got:
        return None
    return (f"⚠ сессия на плагине {got[0]}, установлена {got[1]} — перезапустите Claude Code; "
            f"запреты {got[1]} на эту сессию не действуют.")
