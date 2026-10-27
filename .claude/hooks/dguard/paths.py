"""Пути: что считается своей папкой, временным каталогом и можно ли писать в цель."""
import posixpath
import re
from dguard import config
from dguard.config import norm, SCRATCH


def under_cwd(cwd, p):
    """Относительный путь `p` от рабочего каталога `cwd`, нормализованный."""
    return posixpath.normpath(cwd + "/" + p)


def placeholder(p):
    """Нормализованный путь — не путь: пусто, заготовка `<…>` или `{}` (подстановка `find -exec`)."""
    return not p or p.startswith("<") or "{}" in p


def literal_part(p):
    """Буквальная часть нормализованного пути: до первой неизвестной переменной (`$home` — известна)."""
    return re.split(r"\$(?!home\b|\{home\})", p, maxsplit=1)[0] if "$" in p else p


def closed_host(text):
    """В тексте (путь, имя хоста, аргументы ssh) назван закрытый хост из `guard_forbidden_hosts`."""
    return bool(config.CLOSED_RE and config.CLOSED_RE.search(text or ""))


TEMP_VARS = ("$tmp", "$temp", "$tmpdir", "${tmp}", "${temp}", "${tmpdir}", "${tmpdir:-/tmp}", "${tmp:-/tmp}",
             "${temp:-/tmp}", "$env:tmp", "$env:temp", "%tmp%", "%temp%")
TEMP_DIR_MARK = "/appdata/local/temp/"             # %TEMP% Windows (`C:/Users/<имя>/AppData/Local/Temp/…`, в т.ч. короткое имя 8.3)
MKTEMP_RE = re.compile(r"^(?:\$\(\s*(?:command\s+)?mktemp\b([^()]*)\)|`\s*mktemp\b([^`]*)`)")


def _sys_temp():
    """Системный временный каталог этой машины (`tempfile.gettempdir()`: macOS `/var/folders/…/T`, Windows 8.3-имя) — только
    если он выглядит как temp (имя tmp/temp/t, не короче двух сегментов): `TMPDIR=/home/user` не делает домашнюю папку временной."""
    try:
        import tempfile
        t = norm(tempfile.gettempdir()).rstrip("/")
    except Exception:
        return ()
    return (t + "/",) if t.count("/") >= 2 and t.rsplit("/", 1)[-1] in ("tmp", "temp", "t") else ()


SYS_TEMP = _sys_temp()


def temp_rest(p):
    """Нормализованный путь (строчными) во временном каталоге → (остаток после корня, это результат mktemp); не временный
    путь или `mktemp -p КАТАЛОГ` (каталог не временный) → None."""
    for v in TEMP_VARS:
        if p == v or p.startswith(v + "/"):
            return p[len(v):].lstrip("/"), False
    m = MKTEMP_RE.match(p)
    if m:
        for tok in (m.group(1) or m.group(2) or "").split():
            if tok in ("-p", "--tmpdir") or tok.startswith(("--tmpdir=", "-p")):
                return None                                  # каталог задан явно — это уже не временный каталог
            if "/" in tok and temp_rest(tok.strip("'\"")) is None:
                return None                                  # шаблон с путём вне временного каталога
        rest = p[m.end():]
        return (rest.lstrip("/"), True) if (not rest or rest.startswith("/")) else None
    if p == "/tmp" or p.startswith("/tmp/"):
        return p[5:].lstrip("/"), False
    for r in SYS_TEMP + ("/private/tmp/",):
        if p.startswith(r):
            return p[len(r):].lstrip("/"), False
    i = p.find(TEMP_DIR_MARK)
    if i >= 0:
        return p[i + len(TEMP_DIR_MARK):].lstrip("/"), False
    return None


def temp_ok(p, ctx):
    """Путь внутри временного каталога (запись/перезапись/удаление разрешены) — кроме корня каталога, `*`, `..`;
    на закрытом узле — нет."""
    tr = temp_rest(p)
    if tr is None or getattr(ctx, "host", None) == CLOSED_HOST:
        return False
    rest, from_mktemp = tr
    segs = [x for x in rest.split("/") if x]
    if ".." in segs:
        return False
    return from_mktemp or bool(rest.strip("/*"))


def allowed(t, ctx=None):
    """Цель удаления разрешена (путь внутри своей папки)? `ctx.cwd` — рабочий каталог для относительных путей."""
    p = norm(t)
    if placeholder(p):
        return False
    if closed_host(p):
        return False
    if temp_rest(p) is not None:                          # временные файлы — не данные владельца
        return temp_ok(p, ctx)
    literal = literal_part(p)
    if not literal or ".." in literal.split("/"):
        return False
    if config.STAGE and (literal == config.STAGE or literal.startswith(config.STAGE + "/")):
        return True
    for root in config.LOCAL_ROOTS + config.REMOTE_ROOTS:
        if literal.startswith(root):
            return bool(literal[len(root):].strip("/*"))  # не саму корневую папку
    if SCRATCH in literal:
        return True
    if literal.startswith(("/", "~", "$", "c:", "%")) or re.match(r"[a-z]:", literal):
        return False
    # относительный путь: от известного рабочего каталога (локально — cwd проекта, на ssh — после `cd ~/проект/…`)
    cwd = getattr(ctx, "cwd", None)
    if not cwd:
        return False
    return allowed(under_cwd(cwd, literal), Ctx(None, getattr(ctx, "remote", False), host=getattr(ctx, "host", None)))


def home_relative(literal):
    """Путь относительно домашнего каталога (`~/…`, `$HOME/…`, `/c/Users/<имя>/…`, `C:/Users/<имя>/…`) или None."""
    for pre in ("~/", "$home/", "${home}/"):
        if literal.startswith(pre):
            return literal[len(pre):]
    m = re.match(r"^(?:/[a-z]|[a-z]:)/users/[^/]+/(.*)$", literal)
    return m.group(1) if m else None


def write_only_ok(t, ctx=None):
    """Перезапись/усечение (не удаление!) разрешены в автопамяти проекта: `~/.claude/projects/<проект>/memory/`
    (только локально: на удалённой машине `~` — другой домашний каталог)."""
    if getattr(ctx, "remote", False):
        return False
    p = norm(t)
    if placeholder(p):
        return False
    literal = literal_part(p)
    if not literal or ".." in literal.split("/") or closed_host(p):
        return False
    rel = home_relative(literal)
    if rel is not None:
        return any(rel.startswith(r) and bool(rel[len(r):].strip("/*")) for r in config.WRITE_HOME_ROOTS)
    if literal.startswith(("/", "$", "c:", "%")) or re.match(r"[a-z]:", literal):
        return False
    cwd = getattr(ctx, "cwd", None)                         # относительный путь — от известного рабочего каталога
    return bool(cwd) and write_only_ok(under_cwd(cwd, literal), None)


# ------------------------------------------------------------------------------------------------------------
# Разбор простых команд
# ------------------------------------------------------------------------------------------------------------

class Ctx:
    __slots__ = ("cwd", "remote", "funcs", "host", "via")

    def __init__(self, cwd=None, remote=False, funcs=frozenset(), host=None, via=None):
        self.cwd = cwd          # нормализованный каталог или None (неизвестен)
        self.remote = remote
        self.funcs = funcs      # имена функций оболочки, объявленных в этой же команде (`rsh() { ssh …; }`)
        self.host = host        # хост ssh без `user@`, строчными, или None
        self.via = via          # метка группы журнала для целей из кода Python/PowerShell ("py-ast", "ps"); иначе None

    def copy(self):
        return Ctx(self.cwd, self.remote, self.funcs, self.host, self.via)


CLOSED_HOST = "<закрытый узел>"    # Ctx.host закрытого узла (хост из `guard_forbidden_hosts` или не определён)
