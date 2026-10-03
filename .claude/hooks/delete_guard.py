"""Удаление — только внутри своей папки (владелец 27.09: «нельзя удалять ничего кроме чего то внутри своей папки»).

Вызывается PreToolUse-хуком плагина (`hooks/hooks.json`: Bash, PowerShell, Write, Edit, MultiEdit,
NotebookEdit): точка входа `main()` читает JSON события со stdin, `check(cmd, cwd)` → причина отказа или None. Корень проекта — `CLAUDE_PROJECT_DIR` (иначе cwd); нет `.claude/roles` — молчит.
Своя папка: локально — папка проекта и scratchpad сессии; на удалённых машинах — каталоги из `RPV_GUARD_REMOTE_ROOTS`
(и `RPV_GUARD_HOST_ROOTS` для своего хоста); оперативная стадия — `RPV_GUARD_STAGE`. Никогда: хосты из
`RPV_GUARD_FORBIDDEN_HOSTS`, Storage Box (ssh порт 23), записи `root/` и `deep/` (единственные копии). Цель, которую нельзя
проверить (переменная без буквального префикса, список из конвейера/xargs, путь из кода), — отказ: переписать явным путём.

Разбор (02.10, аудит: регэксп по всему тексту давал ~52 ложных отказа за 5 дней — на переменную `rd`, слово «rm» в
комментарии тикета, `grep 'unlink'`): команда режется на простые команды (`;` `&&` `||` `|` `&`, перевод строки, `$(…)`,
кавычки и heredoc учитываются), и удалением считается только простая команда, у которой КОМАНДНОЕ слово — rm/rmdir/unlink/
shred/del/erase/rd/Remove-Item/ri, `find … -delete|-exec rm`, `git clean`, `rsync --delete*|--remove-source-files`,
либо исполняемый Python (`-c`, stdin/heredoc), вызывающий shutil.rmtree/os.remove/os.unlink/os.rmdir/Path.unlink/.rmdir().
Перезапись и усечение (аудит 03.10) — то же, что удаление, с теми же разрешёнными корнями: `> файл` (`>|`, `&>`, `2>`; не
`>>`), `truncate`, `dd of=`, `cp`/`mv` поверх существующего. Цель вне корней разрешена, только если локальный буквальный путь
не существует (создание нового файла); удалённый хост и непроверяемый путь считаются «существует». `git reset --hard`,
`git clean -f…`, `git push --force/-f/--force-with-lease` — отказ «только через CEO» в любом каталоге. Исключений по имени
скрипта нет: строка в тексте команды проверку не отключает.
Корни хоста `HOST_ROOTS` (`RPV_GUARD_HOST_ROOTS`) действуют, только если команда идёт по ssh на этот хост;
`git reset --hard` и `git clean -f…` разрешены в каталоге вне основного дерева, заданном явным путём (`cd <путь>`,
`git -C <путь>`, рабочий каталог вызова): scratchpad сессии, `<подкаталог из RPV_GUARD_REMOTE_ROOTS>`, `/tmp/<подкаталог>`, корни
хоста, связанные worktree внутри папки проекта (`.git` — файл); неизвестная переменная в `cd $W`,
`--git-dir`/`--work-tree`/GIT_DIR, основное дерево и вложенные репозитории — отказ; `git push --force` —
всегда отказ; перезапись/усечение (не удаление) разрешены в автопамяти `~/.claude/projects/<проект>/memory/`.
Дыры, закрытые вторым проходом аудита (03.10): (а) `mv` проверяет и ИСТОЧНИК — как удаление источника (`mv …/deep/x ./trash
&& rm -rf ./trash`); то же `Move-Item`/`move`/`ren`; (б) файловые инструменты Write/Edit/MultiEdit/NotebookEdit —
`check_file(путь, cwd)`: путь вне корней и файл уже есть — отказ (новый файл вне корней — можно, как для Bash); единая точка
входа хука — `check_tool(событие)`; (д) временные файлы — не данные владельца: запись, перезапись, удаление под `$TMP`/`$TEMP`/`$TMPDIR`/`$env:TEMP`/`%TEMP%`, `/tmp/<путь>`
(локально и на любой удалённой машине, кроме закрытых узлов), `…/AppData/Local/Temp/<путь>` и результатами `mktemp` без `-p`;
сам корень временного каталога, `*`, `..`, сегменты root/deep — по-прежнему нет; (в) `tee` без `-a`, `Out-File`/`Set-Content`/`Tee-Object`, `find … -exec cp|mv|tee|…
{}` (пути find — цели), `git checkout -- <путь>|.`, `git restore`, `git switch -f`, `git branch -D|-M|-f`, `git stash
clear|drop`, `git push +ref|:ref|--delete|--mirror` — как `git reset --hard` (вне основного дерева разрешено, иначе «только
через CEO»); (г) fail-closed: сбой самого стража (исключение при разборе, битое событие) — отказ с причиной, не пропуск.
Третий проход (03.10): (е) Write/Edit/MultiEdit/NotebookEdit вне корней ограничены только для запусков диспетчера
(`RPV_ROLE`); сессия CEO/владельца правит файлы вне проекта, кроме root/ и deep/ и закрытых узлов; (ж) `.git` — удаление,
перенос, шаблоны вроде `.*` никому (кроме `*.lock`), запись внутрь — не запускам диспетчера; (з) настройки Claude Code
`.claude/settings*.json` запуску диспетчера не менять (через них выключаются хуки и плагин).
Через обёртки (sudo/env/nohup/xargs/systemd-run/timeout/…), `ssh хост '<строка>'`, `bash|sh -c`, `powershell -Command`,
`cmd /c`, eval — разбор рекурсивный. Текст в аргументах прочих команд (git commit -m, tickets.py --text, echo, grep,
`cat > файл <<EOF`) удалением не считается. Не удалось разобрать (незакрытая кавычка) — прежний регэксп как страховка.
"""
import ast
import base64
import os
import posixpath
import re
import textwrap


def _env(name, default=""):
    """Переменная `RPV_<имя>`, запасная — `ALPHA_<имя>` (прежнее название)."""
    return os.environ.get("RPV_" + name) or os.environ.get("ALPHA_" + name) or default


def _env_list(name):
    return tuple(x.strip().lower() for x in _env(name).split(",") if x.strip())


def project_roots(project_dir):
    """Корни своей папки: каталог проекта (`c:/proj/` и путь Git Bash `/c/proj/`), строчными, с `/` на конце."""
    p = str(project_dir).replace("\\", "/").lower().rstrip("/") + "/"
    m = re.match(r"^([a-z]):/(.*)$", p)
    return (p, f"/{m.group(1)}/{m.group(2)}") if m else (p,)


def memory_roots(project_dir):
    """Автопамять проекта относительно домашнего каталога: `.claude/projects/<путь проекта через «-»>/memory/`."""
    enc = re.sub(r"[^a-z0-9]", "-", str(project_dir).lower())
    return (f".claude/projects/{enc}/memory/",)


def _env_host_roots():
    """`RPV_GUARD_HOST_ROOTS="хост=корень,корень;хост2=корень"` → {хост: (корни…)} строчными."""
    out = {}
    for part in _env("GUARD_HOST_ROOTS").split(";"):
        host, _, roots = part.partition("=")
        host = host.strip().lower()
        if host and roots.strip():
            out[host] = tuple(r.strip().lower() for r in roots.split(",") if r.strip())
    return out


def configure(project_dir):
    global LOCAL_ROOTS, WRITE_HOME_ROOTS
    LOCAL_ROOTS = project_roots(project_dir)
    WRITE_HOME_ROOTS = memory_roots(project_dir)


_PROJECT = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
LOCAL_ROOTS = project_roots(_PROJECT)
WRITE_HOME_ROOTS = memory_roots(_PROJECT)
SCRATCH = "/appdata/local/temp/claude/"
# Удалённые машины и закрытые узлы — только из окружения, без умолчаний (через запятую, примеры в README плагина):
# RPV_GUARD_REMOTE_ROOTS — каталоги, где на любых удалённых машинах можно удалять (нет переменной — нигде);
# RPV_GUARD_HOST_ROOTS — то же, но только для ssh на конкретный хост; RPV_GUARD_STAGE — оперативная стадия
# (целиком); RPV_GUARD_FORBIDDEN_HOSTS — хосты/IP, где нельзя вообще.
REMOTE_ROOTS = _env_list("GUARD_REMOTE_ROOTS")
HOST_ROOTS = _env_host_roots()
STAGE = _env("GUARD_STAGE").strip().lower().rstrip("/")
FORBIDDEN_SEG = ("root", "deep")
BOX_RE = re.compile("|".join(["storage-?box", "your-storagebox"] + [re.escape(h) for h in _env_list("GUARD_FORBIDDEN_HOSTS")]),
                    re.I)
REASON = ("Удаление запрещено вне своей папки. Можно только явным путём внутри папки проекта или scratchpad сессии; на "
          "удалённых машинах — только каталоги из RPV_GUARD_REMOTE_ROOTS и RPV_GUARD_HOST_ROOTS (для этого хоста). "
          "Закрытые узлы (Storage Box, RPV_GUARD_FORBIDDEN_HOSTS), записи root/ и deep/ — никогда (только владелец "
          "через CEO). Непроверяемая цель: {t}")
REASON_OVERWRITE = ("Перезапись/усечение файла (`> файл`, truncate, dd of=, cp/mv поверх существующего) вне своей папки "
                    "запрещены, как и удаление: внутри папки проекта или scratchpad сессии; на удалённых машинах — "
                    "только каталоги из RPV_GUARD_REMOTE_ROOTS и RPV_GUARD_HOST_ROOTS; автопамять проекта "
                    "(~/.claude/projects/<проект>/memory/) — можно. Новый локальный файл вне папки можно создать (его ещё "
                    "нет). Закрытые узлы, записи root/ и deep/ — никогда. Цель: {t}")
REASON_IRREVERSIBLE = ("Необратимая команда git ({t}): только через CEO — не выполнять самому, передать CEO записью "
                       "тикета (`tickets.py comment <ID> --author <роль> --text \"...\" --next ceo`). `git reset --hard`, "
                       "`git clean -f`, `git checkout -- <путь>|.`, `git restore`, `git switch -f`, `git branch -D|-M|-f`, "
                       "`git stash clear|drop` разрешены только в каталоге вне основного дерева, заданном явным путём "
                       "(`cd <путь>` или `git -C <путь>`): scratchpad сессии, удалённые корни из RPV_GUARD_REMOTE_ROOTS, "
                       "/tmp/<подкаталог>, корни хоста, связанный worktree (.claude/worktrees/…); `git push --force`, "
                       "`+ветка`, `--delete`, `:ветка`, `--mirror` — всегда только через CEO.")
REASON_FILE = ("Запись в файл вне своей папки ({t}) запрещена, как и перезапись через Bash: Write/Edit/MultiEdit/NotebookEdit "
               "меняют существующие файлы только внутри папки проекта, scratchpad сессии, временного каталога и автопамяти "
               "проекта (~/.claude/projects/<проект>/memory/). Новый файл вне папки создать можно (его ещё нет). Записи "
               "root/ и deep/, закрытые узлы — никогда. Нужна правка вне папки — через владельца/CEO.")
REASON_CRASH = ("Страж удаления упал ({e}) — отказ по умолчанию (fail-closed), а не пропуск. Исправьте "
                ".claude/hooks/delete_guard.py плагина вручную (вне этой сессии) или отключите плагин; не получилось — "
                "сообщите владельцу/CEO.")
UNKNOWN_TARGET = "<цель из кода не видна>"
# куда писать можно всегда: не файлы (устройства-стоки, пустышка Windows/PowerShell)
HARMLESS_SINK = re.compile(r"^(?:/dev/(?:null|stdout|stderr|tty|zero|full|fd/\d+)|/proc/self/fd/\d+|nul|con|\$null)$", re.I)
MAX_DEPTH = 8


class Over(str):
    """Цель перезаписи/усечения (не удаления): вне корней отказ, только если файл существует или это неизвестно."""


class Forbid(str):
    """Необратимая команда (git reset --hard и т. п.): отказ «только через CEO», путь не при чём."""


def norm(p):
    return p.strip().strip("'\"").replace("\\", "/").lower()


def literal_part(p):
    """Буквальная часть нормализованного пути: до первой неизвестной переменной (`$home` — известна)."""
    return re.split(r"\$(?!home\b|\{home\})", p, maxsplit=1)[0] if "$" in p else p


def host_root_ok(literal, host):
    """Путь внутри корня хоста (`HOST_ROOTS`): не сам корень, без сегментов root/deep ниже корня."""
    for root in HOST_ROOTS.get(host or "", ()):
        if literal.startswith(root):
            rest = literal[len(root):]
            if rest.strip("/*") and not any(s in FORBIDDEN_SEG for s in rest.split("/") if s):
                return True
    return False


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
    """Путь внутри временного каталога (запись/перезапись/удаление разрешены) — кроме корня каталога, `*`, `..`, root/deep;
    на закрытом узле — нет."""
    tr = temp_rest(p)
    if tr is None or getattr(ctx, "host", None) == CLOSED_HOST:
        return False
    rest, from_mktemp = tr
    segs = [x for x in rest.split("/") if x]
    if ".." in segs or any(x in FORBIDDEN_SEG for x in segs):
        return False
    return from_mktemp or bool(rest.strip("/*"))


def allowed(t, ctx=None):
    """Цель удаления разрешена (путь внутри своей папки)? `ctx.cwd` — рабочий каталог для относительных путей."""
    p = norm(t)
    if not p or p.startswith("<") or "{}" in p:
        return False
    if BOX_RE.search(p):
        return False
    if temp_rest(p) is not None:                          # временные файлы — не данные владельца
        return temp_ok(p, ctx)
    literal = literal_part(p)
    if not literal or ".." in literal.split("/"):
        return False
    if STAGE and (literal == STAGE or literal.startswith(STAGE + "/")):
        return True
    if host_root_ok(literal, getattr(ctx, "host", None)):   # корни выделенного сервера — только при ssh на него
        return True
    if any(s in FORBIDDEN_SEG for s in literal.split("/") if s):
        return False
    for root in LOCAL_ROOTS + REMOTE_ROOTS:
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
    return allowed(posixpath.normpath(cwd + "/" + literal), Ctx(None, getattr(ctx, "remote", False),
                                                                host=getattr(ctx, "host", None)))


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
    if not p or p.startswith("<") or "{}" in p:
        return False
    literal = literal_part(p)
    if not literal or ".." in literal.split("/") or BOX_RE.search(p):
        return False
    rel = home_relative(literal)
    if rel is not None:
        return any(rel.startswith(r) and bool(rel[len(r):].strip("/*")) for r in WRITE_HOME_ROOTS)
    if literal.startswith(("/", "$", "c:", "%")) or re.match(r"[a-z]:", literal):
        return False
    cwd = getattr(ctx, "cwd", None)                         # относительный путь — от известного рабочего каталога
    return bool(cwd) and write_only_ok(posixpath.normpath(cwd + "/" + literal), None)


def linked_worktree(p):
    """Каталог внутри связанного git-worktree в папке проекта (`.claude/worktrees/agent-*`): ближайший `.git` вверх по
    пути — файл (у основного дерева и вложенных репозиториев это каталог). Только локально, по файловой системе."""
    cur = p.rstrip("/")
    for _ in range(40):
        if cur + "/" in LOCAL_ROOTS:
            return False
        fp = fs_path(cur + "/.git", None)
        if fp is None:
            return False
        if os.path.isfile(fp):
            return True
        if os.path.isdir(fp):
            return False
        parent = posixpath.dirname(cur)
        if parent == cur:
            return False
        cur = parent
    return False


def scratch_repo_ok(d, ctx):
    """`git reset --hard` / `git clean -f` не трогают основное дерево: каталог репозитория известен явным путём (не
    переменная, не относительный без cwd) и лежит вне папки проекта — scratch (scratchpad сессии, свои удалённые корни
    вроде <подкаталог из RPV_GUARD_REMOTE_ROOTS>, корни хоста), `/tmp/<подкаталог>` либо связанный worktree внутри папки
    проекта (`.git` — файл)."""
    p = norm(d) if d else ""
    if not p or "$" in p or ".." in p.split("/") or BOX_RE.search(p):
        return False
    host = getattr(ctx, "host", None)
    if host == CLOSED_HOST or (host and BOX_RE.search(host)):        # git на закрытом узле — как и удаление там
        return False
    if not (p.startswith(("/", "~")) or re.match(r"[a-z]:", p)):
        return False
    if any(sg in FORBIDDEN_SEG for sg in p.split("/") if sg) and not host_root_ok(p, host):
        return False
    probe = p.rstrip("/") + "/"
    if any(probe.startswith(r) for r in LOCAL_ROOTS):                # папка проекта: только связанный worktree
        return not getattr(ctx, "remote", False) and linked_worktree(p)
    if re.match(r"^/tmp/[^/*]+", p):
        return True
    return allowed(p, Ctx(None, getattr(ctx, "remote", False), host=host))


# ------------------------------------------------------------------------------------------------------------
# Лексер: текст команды → простые команды
# ------------------------------------------------------------------------------------------------------------

class ParseError(Exception):
    pass


class Cmd:
    __slots__ = ("words", "heredocs", "pipe_from", "overwrites")

    def __init__(self, words=None):
        self.words = list(words or [])
        self.heredocs = []      # тела heredoc / here-string этой команды (её stdin)
        self.pipe_from = None   # предыдущая команда конвейера
        self.overwrites = []    # цели усекающих перенаправлений `> f` `>| f` `&> f` `2> f` (не `>>`)


ESCAPABLE = ";|&<>()\"' $\\`"
ARRAY_HEAD = re.compile(r"[A-Za-z_]\w*\+?=")
HEREDOC_RE = re.compile(r"<<(-?)[ \t]*(?:'([^']*)'|\"([^\"]*)\"|\\?([^\s;|&<>()'\"]+))")


def find_close(s, j):
    """Индекс `)`, закрывающей `$(`, чей текст начинается с j. Кавычки и heredoc внутри учитываются."""
    n = len(s)
    depth = 1
    pend = []
    while j < n:
        c = s[j]
        if c == "'":
            k = s.find("'", j + 1)
            if k < 0:
                raise ParseError("'")
            j = k + 1
            continue
        if c == '"':
            j += 1
            while j < n and s[j] != '"':
                j += 2 if s[j] == "\\" else 1
            if j >= n:
                raise ParseError('"')
            j += 1
            continue
        if c == "\\":
            j += 2
            continue
        if c == "<" and s.startswith("<<", j) and not s.startswith("<<<", j):
            m = HEREDOC_RE.match(s, j)
            if m:
                pend.append((m.group(2) or m.group(3) or m.group(4), bool(m.group(1))))
                j = m.end()
                continue
        if c == "\n" and pend:
            j += 1
            for delim, strip in pend:
                while j < n:
                    e = s.find("\n", j)
                    line = s[j:] if e < 0 else s[j:e]
                    j = n if e < 0 else e + 1
                    if (line.lstrip("\t") if strip else line) == delim:
                        break
            pend = []
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return j
        j += 1
    raise ParseError("$(")


class _Lexer:
    def __init__(self, s, depth):
        if depth > MAX_DEPTH:
            raise ParseError("вложенность")
        self.s = s.replace("\r\n", "\n").replace("\r", "\n")
        self.depth = depth
        self.i = 0
        self.cmds = []
        self.cur = Cmd()
        self.buf = []
        self.inword = False
        self.skip = False   # следующее слово — цель перенаправления (True), here-string ("here") или усекающего `>` ("out")
        self.pending = []   # heredoc, тела которых читаются после перевода строки

    # --- слова и команды
    def push(self, text):
        self.buf.append(text)
        self.inword = True

    def end_word(self):
        if self.inword:
            w = "".join(self.buf)
            if self.skip == "here":
                self.cur.heredocs.append(w)
            elif self.skip == "out":
                self.cur.overwrites.append(w)
            elif not self.skip:
                self.cur.words.append(w)
            self.skip = False
        self.buf = []
        self.inword = False

    def end_cmd(self, sep):
        self.end_word()
        self.skip = False
        done = self.cur
        if done.words or done.overwrites:
            self.cmds.append(done)
        self.cur = Cmd()
        if sep == "|" and done.words:
            self.cur.pipe_from = done

    def sub(self, inner):
        """Команды внутри `$(…)`/`` `…` ``/`<(…)` исполняются — разобрать и добавить в общий список."""
        self.cmds.extend(tokenize(inner, self.depth + 1))

    # --- разбор
    def run(self):
        s = self.s
        n = len(s)
        while self.i < n:
            c = s[self.i]
            i = self.i
            if c in " \t":
                self.end_word()
                self.i += 1
            elif c == "\n":
                self.end_cmd("\n")
                self.i += 1
                self.read_heredocs()
            elif c == "#" and not self.inword:
                j = s.find("\n", i)
                self.i = n if j < 0 else j
            elif c == "\\":
                self.backslash()
            elif c == "'":
                j = s.find("'", i + 1)
                if j < 0:
                    raise ParseError("'")
                self.push(s[i + 1:j])
                self.i = j + 1
            elif c == '"':
                self.double()
            elif c == "$" and s.startswith("$(", i):
                j = find_close(s, i + 2)
                self.sub(s[i + 2:j])
                self.push(s[i:j + 1])
                self.i = j + 1
            elif c == "`":
                if s.startswith("`\n", i):
                    self.i += 2
                    continue
                j = s.find("`", i + 1)
                if j < 0:
                    self.push(c)
                    self.i += 1
                else:
                    self.sub(s[i + 1:j])
                    self.push(s[i:j + 1])
                    self.i = j + 1
            elif c == "@" and not self.inword and re.match(r"@(['\"])[ \t]*\n", s[i:i + 40]):
                q = s[i + 1]
                start = s.index("\n", i) + 1
                end = s.find("\n" + q + "@", start - 1)
                if end < 0:
                    raise ParseError("here-string")
                self.push(s[start:end])
                self.i = end + 3
            elif c == ";":
                self.end_cmd(";")
                self.i += 1
            elif c == "|":
                if s.startswith("||", i):
                    self.end_cmd("||")
                    self.i += 2
                elif s.startswith("|&", i):
                    self.end_cmd("|")
                    self.i += 2
                else:
                    self.end_cmd("|")
                    self.i += 1
            elif c == "&":
                if s.startswith("&&", i):
                    self.end_cmd("&&")
                    self.i += 2
                elif s.startswith("&>", i):
                    self.end_word()
                    append = s.startswith("&>>", i)
                    self.i += 3 if append else 2
                    self.skip = True if append else "out"
                else:
                    self.end_cmd("&")
                    self.i += 1
            elif c in "<>":
                self.redirect()
            elif c == "(" and self.inword and ARRAY_HEAD.fullmatch("".join(self.buf)):
                j = find_close(s, i + 1)            # `K=(-i key -o X)` — массив bash одним словом
                self.push(s[i:j + 1])
                self.i = j + 1
            elif c in "()":
                self.end_cmd(c)
                self.i += 1
            else:
                self.push(c)
                self.i += 1
        self.end_cmd("")
        return self.cmds

    def backslash(self):
        s, i = self.s, self.i
        nxt = s[i + 1] if i + 1 < len(s) else ""
        if nxt == "\n":
            self.i += 2                     # продолжение строки
        elif nxt and nxt in ESCAPABLE:
            self.push(nxt)                  # `\;` `\ ` `\"` `\\` — буквальный символ
            self.i += 2
        else:
            self.push("\\")                 # путь Windows: `C:\Windows\x`
            self.i += 1

    def double(self):
        s, n = self.s, len(self.s)
        j = self.i + 1
        out = []
        while j < n:
            c = s[j]
            if c == '"':
                break
            if c == "\\" and j + 1 < n and s[j + 1] in '"\\$`':
                out.append(s[j + 1])
                j += 2
            elif c == "$" and s.startswith("$(", j):
                e = find_close(s, j + 2)
                self.sub(s[j + 2:e])
                out.append(s[j:e + 1])
                j = e + 1
            elif c == "`":
                e = s.find("`", j + 1)
                if e < 0:
                    out.append(c)
                    j += 1
                else:
                    self.sub(s[j + 1:e])
                    out.append(s[j:e + 1])
                    j = e + 1
            else:
                out.append(c)
                j += 1
        else:
            raise ParseError('"')
        self.push("".join(out))
        self.i = j + 1

    def redirect(self):
        s, n = self.s, len(self.s)
        i = self.i
        c = s[i]
        if self.inword and "".join(self.buf).isdigit():   # `2>` — номер дескриптора, не аргумент
            self.buf = []
            self.inword = False
        else:
            self.end_word()
        if s.startswith("<<<", i):
            self.i = i + 3
            self.skip = "here"
            return
        if s.startswith("<<", i):
            m = HEREDOC_RE.match(s, i)
            if not m:
                self.i = i + 2
                return
            bare = m.group(4)
            quoted = m.group(2) is not None or m.group(3) is not None or s[m.start(4) - 1:m.start(4)] == "\\"
            delim = m.group(2) if m.group(2) is not None else (m.group(3) if m.group(3) is not None else bare)
            self.pending.append((delim, bool(m.group(1)), not quoted, self.cur))
            self.i = m.end()
            return
        i += 1
        append = False
        if i < n and s[i] == c and c == ">":
            i += 1                      # `>>` — дописывание, файл не усекается
            append = True
        trunc = "out" if (c == ">" and not append) else True
        if i < n and s[i] == "&":
            i += 1                      # `>&2`, `2>&1`, `>&-`
            while i < n and (s[i].isdigit() or s[i] == "-"):
                i += 1
        elif i < n and s[i] == "|":
            i += 1
            self.skip = trunc
        elif i < n and s[i] == "(":     # process substitution `<(…)` / `>(…)`
            j = find_close(s, i + 1)
            self.sub(s[i + 1:j])
            i = j + 1
        else:
            self.skip = trunc
        self.i = i

    def read_heredocs(self):
        s, n = self.s, len(self.s)
        i = self.i
        for delim, strip, expand, cmd in self.pending:
            body = []
            while i < n:
                j = s.find("\n", i)
                line = s[i:] if j < 0 else s[i:j]
                i = n if j < 0 else j + 1
                line = line.lstrip("\t") if strip else line
                if line == delim:
                    break
                body.append(line)
            text = "\n".join(body)
            cmd.heredocs.append(text)
            if expand:                  # в heredoc без кавычек у разделителя `$(…)` и `` `…` `` исполняются
                self.expansions(text)
        self.pending = []
        self.i = i

    def expansions(self, text):
        j = 0
        try:
            while True:
                j = text.find("$(", j)
                if j < 0:
                    break
                e = find_close(text, j + 2)
                self.sub(text[j + 2:e])
                j = e + 1
        except ParseError:
            pass


def tokenize(s, depth=0):
    return _Lexer(s, depth).run()


# ------------------------------------------------------------------------------------------------------------
# Разбор простых команд
# ------------------------------------------------------------------------------------------------------------

class Ctx:
    __slots__ = ("cwd", "remote", "funcs", "host")

    def __init__(self, cwd=None, remote=False, funcs=frozenset(), host=None):
        self.cwd = cwd          # нормализованный каталог или None (неизвестен)
        self.remote = remote
        self.funcs = funcs      # имена функций оболочки, объявленных в этой же команде (`rsh() { ssh …; }`)
        self.host = host        # хост ssh без `user@`, строчными (для HOST_ROOTS) или None

    def copy(self):
        return Ctx(self.cwd, self.remote, self.funcs, self.host)


CLOSED_HOST = "<закрытый узел>"    # Ctx.host закрытого узла (Storage Box, коллектор, порт 23, хост не определён)


def host_of(arg):
    """`root@203.0.113.3` / `host:path` → `203.0.113.3` (строчными) или None."""
    h = re.sub(r"^[^@\s]*@", "", arg or "").split(":", 1)[0].strip().strip("[]").lower()
    return h if h and "$" not in h else None


ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
DEL_VERBS = {"rm", "rmdir", "unlink", "shred", "del", "erase", "rd", "remove-item", "ri"}
RESERVED = {"if", "then", "else", "elif", "fi", "do", "done", "while", "until", "for", "select", "case", "esac",
            "in", "!", "{", "}", "function"}
PY_NAMES = re.compile(r"^(?:python|pythonw|py)[0-9.]*$")
SHELLS = {"bash", "sh", "zsh", "dash", "ash", "ksh"}
PS_NAMES = {"powershell", "pwsh"}
CMD_FLAG = re.compile(r"^/(?:[sqfpa]|ar|as|ah|al|\?)(?::\S*)?$", re.I)
PY_CALL_RE = re.compile(r"\brmtree\s*\(|\bos\s*\.\s*(?:remove|unlink|rmdir|removedirs)\s*\(|\.\s*(?:unlink|rmdir)\s*\(")
DEL_OS = {"remove", "unlink", "rmdir", "removedirs"}
PS_DOTNET_DELETE = re.compile(r"^\[(?:system\.)?io\.(?:file|directory)\]::delete", re.I)
SUBPROC = {"run", "call", "check_call", "check_output", "popen"}
RUNNERS = {"uv", "poetry", "pipenv", "pdm", "hatch", "rye", "conda", "mamba", "micromamba", "pipx", "tox", "nox"}
OTHER_INTERP = {"perl", "ruby", "node", "nodejs", "php", "deno", "bun"}
OTHER_DEL_RE = re.compile(r"\b(?:unlink|rmtree|remove_tree|rmSync|rmdirSync|unlinkSync|rm_rf|rm_r|remove_entry|"
                          r"FileUtils\.rm\w*|File\.delete|Deno\.remove)\b")

HOST_RE = re.compile(r"[\w.-]+@[\w.-]+|\d{1,3}(?:\.\d{1,3}){3}")
FUNC_DEF = re.compile(r"(?m)^[ 	]*(?:function[ 	]+)?([A-Za-z_][\w-]*)[ 	]*(?:\([ 	]*\))?[ 	]*\{")
TEXT_CMDS = {"echo", "printf", "grep", "egrep", "fgrep", "rg", "ag", "sed", "awk", "gawk", "cat", "head", "tail",
             "tee", "ls", "stat", "wc", "sort", "uniq", "cut", "tr", "diff", "cmp", "jq", "date", "sleep", "read",
             "test", "[", "[[", "true", "false", "cp", "mv", "mkdir", "touch", "chmod", "chown", "ln", "tar", "gzip",
             "zip", "unzip", "curl", "wget", "gh", "claude", "npm", "pip", "cargo", "make", "sha256sum", "md5sum",
             "b3sum", "du", "df", "file", "which", "type", "man", "scp", "ping", "write-output", "write-host"}
PS_VALUE_PARAMS = {"-include", "-exclude", "-filter", "-credential", "-stream", "-erroraction", "-ea", "-warningaction",
                   "-wa", "-informationaction", "-ia", "-errorvariable", "-ev", "-outvariable", "-ov", "-outbuffer",
                   "-ob", "-pipelinevariable", "-pv"}
SUDO_VAL = {"-u", "-g", "-h", "-p", "-C", "-D", "-R", "-T", "-r", "-t", "-U"}
XARGS_VAL = {"-I", "-n", "-P", "-L", "-d", "-E", "-s", "-a", "--max-args", "--max-procs", "--delimiter",
             "--arg-file", "--max-lines", "--eof"}
SYSTEMD_VAL = {"-u", "--unit", "-p", "--property", "-E", "--setenv", "--working-directory", "--description",
               "--slice", "--on-active", "--on-boot", "--on-startup", "--on-unit-active", "--on-unit-inactive",
               "--on-calendar", "--timer-property", "--uid", "--gid", "--nice", "--machine", "-M", "-H", "--host",
               "--service-type", "-G"}
WRAPPERS = {
    "sudo": SUDO_VAL, "doas": {"-u", "-C"}, "env": {"-u", "-C", "-S"}, "nice": {"-n", "--adjustment"},
    "ionice": {"-c", "-n", "-p", "-P", "-u", "--class", "--classdata"}, "nohup": set(), "time": set(),
    "timeout": {"-s", "-k", "--signal", "--kill-after"}, "command": set(), "builtin": set(), "exec": set(),
    "setsid": set(), "xargs": XARGS_VAL, "systemd-run": SYSTEMD_VAL, "flock": {"-w", "-E", "--timeout"},
    "stdbuf": {"-i", "-o", "-e"}, "unbuffer": set(), "sshpass": {"-p", "-f", "-d", "-P"}, "chronic": set(),
    "wsl": {"-d", "--distribution", "-u", "--user", "--cd"}, "busybox": set(),
}
SSH_VAL = {"-b", "-c", "-D", "-E", "-e", "-F", "-I", "-i", "-J", "-L", "-l", "-m", "-O", "-o", "-p", "-Q", "-R", "-S",
           "-W", "-w"}
RSYNC_VAL = {"-e", "--rsh", "--exclude", "--include", "--exclude-from", "--include-from", "-f", "--filter",
             "--files-from", "--log-file", "--partial-dir", "-T", "--temp-dir", "--backup-dir", "--compare-dest",
             "--copy-dest", "--link-dest", "--port", "--timeout", "--bwlimit", "--chmod", "--chown", "--max-size",
             "--min-size", "--rsync-path", "--address", "--out-format", "--info", "--debug", "--sockopts", "-M",
             "--remote-option", "--suffix", "--usermap", "--groupmap", "--read-batch", "--write-batch"}


def cmd_name(w):
    n = re.split(r"[\\/]", w)[-1].lower()
    return n[:-4] if n.endswith(".exe") else n


def expand_vars(s, vars_):
    if "$" not in s or not vars_:
        return s

    def repl(m):
        v = vars_.get(m.group(1) or m.group(2))
        return v if isinstance(v, str) else m.group(0)
    return re.sub(r"\$(\w+)|\$\{(\w+)\}", repl, s)


def expand_args(words, vars_):
    """Подстановка известных переменных; `"${K[@]}"` — элементы массива."""
    out = []
    for a in words:
        m = re.fullmatch(r"\$\{(\w+)\[[@*]\]\}", a)
        if m and isinstance(vars_.get(m.group(1)), list):
            out.extend(vars_[m.group(1)])
        else:
            out.append(expand_vars(a, vars_))
    return out


def split_remote(arg):
    """`host:path` → (host, path); локальный путь (в т.ч. `C:/…`) → (None, arg)."""
    if re.match(r"^[A-Za-z]:[\\/]", arg) or arg.startswith(("/", "./", "../", "~")):
        return None, arg
    m = re.match(r"^(?:[^/@\s:]+@)?([^/@\s:]+):(.*)$", arg, re.S)
    return (m.group(1), m.group(2)) if m else (None, arg)


def pipe_src(cmd):
    """Тексты, которые команда читает со stdin: heredoc предыдущей команды конвейера и её литеральные аргументы."""
    p = cmd.pipe_from
    if p is None:
        return []
    out = list(p.heredocs)
    if p.words:
        if len(p.words) == 1:
            out.append(p.words[0])              # here-string/строка PowerShell: `@'…'@ | python -`
        elif cmd_name(p.words[0]) in ("echo", "printf", "write-output", "write-host", "cat", "type"):
            out.append("\n".join(p.words[1:]))
    return out


def skip_wrapper(name, words, i):
    """Индекс слова после обёртки (sudo, env, xargs, …); None — это не запуск (`command -v`)."""
    valset = WRAPPERS[name]
    j = i + 1
    n = len(words)
    if name == "command" and j < n and words[j] in ("-v", "-V"):
        return None
    while j < n:
        a = words[j]
        if a == "--":
            j += 1
            break
        if name == "wsl" and a in ("-e", "--exec"):
            j += 1
            continue
        if a.startswith("-") and len(a) > 1:
            j += 2 if a in valset else 1
        else:
            break
    if name == "timeout":
        j += 1                                   # длительность
    elif name == "flock" and "-c" not in words[i + 1:j]:
        j += 1                                   # файл блокировки
    return j


def join_cwd(cwd, target, vars_):
    """Новый рабочий каталог после `cd target` или None (не определить)."""
    if not target or target == "-":
        return None
    t = norm(expand_vars(target, vars_))
    if temp_rest(t) is not None:                          # `cd $TMP/x`, `cd "$(mktemp -d)"` — каталог известен как временный
        return posixpath.normpath(t)
    if "$" in t.replace("$home", "").replace("${home}", "") or "%" in t:
        return None
    absolute = t.startswith(("/", "~", "$home", "${home}")) or re.match(r"[a-z]:", t)
    if absolute:
        return posixpath.normpath(t)
    if not cwd:
        return None
    return posixpath.normpath(cwd + "/" + t)


def python_consts(tree):
    """Таблица `имя → строка` по простым присваиваниям (`p = '/x'`, `p = Path('/x') / 'y'`)."""
    env = {}
    assigns = sorted((n for n in ast.walk(tree) if isinstance(n, ast.Assign)),
                     key=lambda n: (n.lineno, n.col_offset))
    for node in assigns:
        if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            v = const_str(node.value, env)
            if v is not None:
                env[node.targets[0].id] = v
    return env


def const_str(node, env):
    """Значение выражения как строки пути или None (не определить)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return env.get(node.id)
    if isinstance(node, ast.JoinedStr):
        parts = []
        for v in node.values:
            s = const_str(v.value if isinstance(v, ast.FormattedValue) else v, env)
            if s is None:
                return None
            parts.append(s)
        return "".join(parts)
    if isinstance(node, ast.BinOp):
        a, b = const_str(node.left, env), const_str(node.right, env)
        if a is None or b is None:
            return None
        if isinstance(node.op, ast.Add):
            return a + b
        if isinstance(node.op, ast.Div):
            return a.rstrip("/") + "/" + b.lstrip("/")
        return None
    if isinstance(node, ast.Call):
        f = node.func
        fname = f.id if isinstance(f, ast.Name) else (f.attr if isinstance(f, ast.Attribute) else "")
        args = [const_str(a, env) for a in node.args]
        if fname.endswith("Path") or fname == "join":
            if not args or any(a is None for a in args):
                return None if args else "."
            out = args[0]
            for a in args[1:]:                      # os.path.join / Path: абсолютный сегмент начинает путь заново
                out = a if a.startswith("/") else out.rstrip("/") + "/" + a
            return out
        if fname in ("str", "fspath", "expanduser", "abspath", "normpath", "realpath") and args and args[0] is not None:
            return args[0]
        if fname in ("resolve", "expanduser", "absolute") and isinstance(f, ast.Attribute):
            return const_str(f.value, env)
    return None


def python_scan(code, ctx, depth):
    """Удаления в коде Python: вызовы shutil.rmtree/os.remove/os.unlink/os.rmdir/Path.unlink/.rmdir() (не слова)."""
    code = textwrap.dedent(code)
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError):
        if PY_CALL_RE.search(code):
            yield (UNKNOWN_TARGET, ctx.copy())
        return
    os_names = {"os"}
    funcs = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "os":
                    os_names.add(a.asname or "os")
        elif isinstance(node, ast.ImportFrom) and node.module in ("os", "shutil"):
            for a in node.names:
                if a.name == "rmtree" or (node.module == "os" and a.name in DEL_OS):
                    funcs.add(a.asname or a.name)
    env = python_consts(tree)

    def arg0(call):
        if call.args:
            return call.args[0]
        for kw in call.keywords:
            if kw.arg in ("path", "name", "dir"):
                return kw.value
        return None

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        target = False   # False — не удаление; иначе узел-цель (или None)
        if isinstance(f, ast.Attribute):
            on_os = isinstance(f.value, ast.Name) and f.value.id in os_names
            if f.attr == "rmtree":
                target = arg0(node)
            elif f.attr in DEL_OS and on_os:
                target = arg0(node)
            elif f.attr in ("unlink", "rmdir"):
                target = arg0(node) if on_os else f.value
            elif f.attr in ("system", "popen") and on_os and node.args:
                text = const_str(node.args[0], env)
                if text is not None:
                    yield from scan_text(text, ctx, depth=depth + 1)
            elif f.attr in SUBPROC and isinstance(f.value, ast.Name) and f.value.id == "subprocess" and node.args:
                a = node.args[0]
                if isinstance(a, (ast.List, ast.Tuple)):
                    words = [const_str(e, env) or "$?" for e in a.elts]
                    yield from scan_one(Cmd(words), ctx, {}, (), depth + 1)
                else:
                    s = const_str(a, env)
                    if s is not None:
                        yield from scan_text(s, ctx, depth=depth + 1)
        elif isinstance(f, ast.Name) and f.id in funcs:
            target = arg0(node)
        if target is not False:
            v = const_str(target, env) if target is not None else None
            yield (v if v is not None else UNKNOWN_TARGET, ctx.copy())


def del_targets(name, args, xargs, piped):
    """Цели команды-удаления; None — это не удаление (справка, сухой прогон)."""
    ps = name in ("remove-item", "ri")
    legacy_dos = name in ("del", "erase", "rd", "rmdir")
    targets = []
    options = True
    k = 0
    while k < len(args):
        a = args[k]
        k += 1
        low = a.lower()
        if options and a == "--":
            options = False
            continue
        if options and a.startswith("-") and len(a) > 1:
            if low in ("--help", "--version", "-whatif", "-?"):
                return None
            if low.startswith(("-path:", "-literalpath:")):
                targets.append(a.split(":", 1)[1])
            elif low in PS_VALUE_PARAMS:
                k += 1
            continue
        if options and legacy_dos and CMD_FLAG.match(a):
            if a == "/?":
                return None
            continue
        if a == "}" and k == len(args):
            continue
        if ps:
            targets.extend(t for t in a.split(",") if t)
        else:
            targets.append(a)
    if xargs:
        targets.append("<цели из stdin xargs>")
    elif not targets:
        targets.append("<цели из конвейера>" if piped else "<цель не указана>")
    return targets


def find_scan(args, ctx, vars_, depth):
    k = 0
    while k < len(args) and args[k] in ("-H", "-L", "-P", "-E", "-X", "-x", "-s"):
        k += 1
    paths = []
    while k < len(args) and not (args[k].startswith("-") or args[k] in ("!", "(", ")")):
        paths.append(args[k])
        k += 1
    expr = args[k:]
    deleting = "-delete" in expr
    overwriting = False
    extra = []
    k = 0
    while k < len(expr):
        if expr[k] in ("-exec", "-execdir", "-ok", "-okdir"):
            sub = []
            k += 1
            while k < len(expr) and expr[k] not in (";", "+"):
                sub.append(expr[k])
                k += 1
            inner = list(scan_one(Cmd(sub), ctx, vars_, (), depth + 1))
            if any(not isinstance(x[0], (Over, Forbid)) for x in inner):
                deleting = True                     # внутри -exec удаление: целью становятся и пути find
            for x in inner:
                if isinstance(x[0], Over) and "{}" in x[0]:
                    head = x[0][:x[0].index("{}")]
                    if head:                        # `cp a /dir/{}` — члены каталога неизвестны: сам каталог как цель
                        extra.append((Over(head), x[1]))
                    else:                           # `cp a {}` / `tee {}` — перезаписываются найденные find файлы
                        overwriting = True
            extra += [x for x in inner if "{}" not in x[0]]
        elif expr[k] in ("-fprint", "-fprint0", "-fprintf", "-fls") and k + 1 < len(expr):
            extra.append((Over(expr[k + 1]), ctx.copy()))    # find пишет вывод в файл и усекает его
            k += 1
        k += 1
    if overwriting:
        for p in paths or ["."]:
            yield (Over(p), ctx.copy())
    if deleting:
        for p in paths or ["."]:
            yield (p, ctx.copy())
        yield from extra
    else:
        yield from (x for x in extra if isinstance(x[0], (Over, Forbid)))


def rsync_scan(args, ctx):
    delete = any(a.startswith("--delete") or a == "--del" for a in args)
    remove_src = "--remove-source-files" in args
    if not (delete or remove_src):
        return
    # транспорт Storage Box — ssh порт 23 (`-e 'ssh -p 23 …'`): весь список аргументов, не только значение -e
    bad = bool(re.search(r"(?:-p\s*23\b|port[= ]23\b)", " ".join(args), re.I))
    pos = []
    k = 0
    while k < len(args):
        a = args[k]
        k += 1
        if a in RSYNC_VAL or (a.startswith("-") and not a.startswith("--") and len(a) > 2 and a.endswith("e")):
            k += 1                                   # значение опции (`-e CMD`, `--exclude PAT`, кластер `-avze CMD`)
        elif not a.startswith("-"):
            pos.append(a)
    if not pos:
        yield ("<rsync без цели>", ctx.copy())
        return
    chosen = []
    if delete:
        chosen.append(pos[-1])
    if remove_src:
        chosen.extend(pos[:-1] if len(pos) > 1 else pos)
    for arg in chosen:
        host, path = split_remote(arg)
        if host and (bad or BOX_RE.search(host)):
            yield (f"<rsync: {arg} — закрытый узел>", ctx.copy())
        elif host:
            yield (path, Ctx(None, True, host=host_of(arg)))
        else:
            yield (path, ctx.copy())


def git_discard(sub, rest, ctx, repo):
    """Метка подкоманды git, которая затирает рабочее дерево, ветки или stash (checkout -- путь|., restore, switch -f,
    branch -D|-M|-f, stash clear|drop), либо None — команда безопасна."""
    if any(a in ("--help", "-h") for a in rest):
        return None
    short = [a[1:] for a in rest if a.startswith("-") and not a.startswith("--") and len(a) > 1]
    long_ = {a.split("=", 1)[0] for a in rest if a.startswith("--")}
    if sub == "stash":
        pos = [a for a in rest if not a.startswith("-")]
        return f"git stash {pos[0]}" if pos and pos[0] in ("clear", "drop") else None
    if sub == "branch":
        if "--force" in long_ or any(c for c in short if any(ch in c for ch in "DMCf")):
            return "git branch -D/-M/-C/-f (удаление или перезапись ветки)"
        return None
    if sub == "restore":
        staged = "--staged" in long_ or any("S" in c for c in short)
        worktree = "--worktree" in long_ or any("W" in c for c in short)
        return None if staged and not worktree else "git restore (затирает файлы рабочего дерева)"
    if sub == "switch":
        if "--discard-changes" in long_ or "--force" in long_ or any("f" in c for c in short):
            return "git switch -f (затирает правки рабочего дерева)"
        return None
    # checkout
    if "--" in rest:
        return "git checkout -- <путь> (затирает файлы рабочего дерева)"
    if long_ & {"--force", "--patch", "--ours", "--theirs", "--pathspec-from-file"} or any(
            "f" in c or "p" in c for c in short):
        return "git checkout -f/-p/--ours/--theirs (затирает файлы рабочего дерева)"
    pos = []
    k = 0
    while k < len(rest):
        a = rest[k]
        k += 1
        if a in ("-b", "-B", "--orphan", "--conflict"):
            k += 1                                       # значение: имя новой ветки
        elif not a.startswith("-"):
            pos.append(a)
    if len(pos) >= 2 or any(a == "." or any(ch in a for ch in "*?[") for a in pos):
        return "git checkout <ревизия> <путь>|. (затирает файлы рабочего дерева)"
    if len(pos) == 1 and not getattr(ctx, "remote", False):
        try:
            fp = fs_path(pos[0], Ctx(repo or ctx.cwd, False))
            if fp is not None and os.path.lexists(fp):   # `git checkout файл` без `--`: существующий путь — восстановление
                return "git checkout <путь> (затирает файл рабочего дерева)"
        except (OSError, ValueError):
            pass
    return None


def git_scan(args, ctx, vars_, redirected=False):
    """`redirected` — GIT_DIR/GIT_WORK_TREE в окружении команды: каталог репозитория не равен рабочему."""
    j = 0
    base = None
    bases = []
    exotic = redirected or any(k in vars_ for k in ("GIT_DIR", "GIT_WORK_TREE"))
    while j < len(args):
        a = args[j]
        if a == "-C" and j + 1 < len(args):
            base = args[j + 1]
            bases.append(base)
            j += 2
        elif a in ("--git-dir", "--work-tree") or a.startswith(("--git-dir=", "--work-tree=")):
            exotic = True
            j += 1 if "=" in a else 2
        elif a in ("-c", "--namespace", "--exec-path"):
            j += 2
        elif a.startswith("-"):
            j += 1
        else:
            break
    if j >= len(args):
        return
    sub, rest = args[j], args[j + 1:]
    repo = None if exotic else ctx.cwd                    # каталог репозитория: cwd, сдвинутый цепочкой `-C`
    for b in bases:                                       # абсолютный `-C` задаёт каталог и при неизвестном cwd
        repo = None if exotic else join_cwd(repo, b, vars_)
    if sub == "reset":
        if "--hard" in rest and not (not exotic and scratch_repo_ok(repo, ctx)):
            yield (Forbid("git reset --hard"), ctx.copy())
        return
    if sub == "push":
        pos = [a for a in rest if not a.startswith("-")]
        if any(a == "--force" or a.startswith("--force-with-lease")
               or (a.startswith("-") and not a.startswith("--") and "f" in a[1:]) for a in rest):
            yield (Forbid("git push --force"), ctx.copy())
        elif any(a.startswith("+") for a in pos):
            yield (Forbid("git push +ветка (принудительное обновление)"), ctx.copy())
        elif (any(a in ("--delete", "--mirror", "--prune") for a in rest) or any(a.startswith(":") for a in pos)
              or any(a.startswith("-") and not a.startswith("--") and "d" in a[1:] for a in rest)):
            yield (Forbid("git push --delete/--mirror/:ветка (удаление веток на удалённом)"), ctx.copy())
        return
    if sub in ("checkout", "switch", "restore", "branch", "stash"):
        label = git_discard(sub, rest, ctx, repo)
        if label and not (not exotic and scratch_repo_ok(repo, ctx)):
            yield (Forbid(label), ctx.copy())
        return
    if sub != "clean":
        return
    if any(a == "--dry-run" or (a.startswith("-") and not a.startswith("--") and "n" in a) for a in rest):
        return
    if any(a == "--force" or (a.startswith("-") and not a.startswith("--") and "f" in a[1:]) for a in rest):
        if not (not exotic and scratch_repo_ok(repo, ctx)):
            yield (Forbid("git clean -f"), ctx.copy())
        return
    after = rest[rest.index("--") + 1:] if "--" in rest else []
    specs = after or [a for a in rest if not a.startswith("-")]
    b = base or "."
    for sp in (specs or [None]):
        yield ((b if sp is None else posixpath.join(b, sp)), ctx.copy())


def fs_path(t, ctx):
    """Путь для проверки существования на этой машине или None (неизвестная переменная, glob, подстановка, `{}`,
    относительный путь без известного cwd — не определить)."""
    p = t.strip().strip("'\"")
    p = re.sub(r"\$(\w+)|\$\{(\w+)\}|%(\w+)%",   # переменные окружения этой машины (`$TEMP/x.py`) — известны
               lambda m: os.environ.get(m.group(1) or m.group(2) or m.group(3), m.group(0)), p)
    if not p or any(c in p for c in "$%`*?[{<"):
        return None
    p = p.replace("\\", "/")
    if p.startswith("~"):
        p = os.path.expanduser(p)
    m = re.match(r"^/([A-Za-z])/(.*)$", p)
    if m and os.name == "nt":
        p = f"{m.group(1)}:/{m.group(2)}"                # путь Git Bash `/c/Users/x` → `c:/Users/x`
    if p.startswith("/") or re.match(r"^[A-Za-z]:/", p):
        return p
    cwd = getattr(ctx, "cwd", None)
    return posixpath.join(cwd, p) if cwd else None


def may_exist(t, ctx):
    """Цель перезаписи, возможно, существует: удалённый узел и непроверяемый путь — да; локальный буквальный — по ФС."""
    if getattr(ctx, "remote", False):
        return True
    fp = fs_path(t, ctx)
    if fp is None or fp.startswith("/dev/"):              # устройство (`of=/dev/sda`) — всегда «существует»
        return True
    try:
        return os.path.lexists(fp)
    except OSError:
        return True


def over(t, vars_, ctx):
    """Цель усечения/перезаписи → запись для проверки; устройства-стоки (`/dev/null`, `$null`) — не файлы."""
    t = expand_vars(t, vars_)
    if HARMLESS_SINK.match(t.strip().strip("'\"")):
        return ()
    return ((Over(t), ctx.copy()),)


def cp_scan(args, ctx, vars_, move=False, xargs=False):
    """`cp`/`mv`: цель — существующий файл (или `каталог/имя` существующего каталога); `-n`/`--no-clobber` — не трогает.
    `mv` (`move=True`) вдобавок УДАЛЯЕТ источники: каждый источник проверяется как цель удаления (`mv …/deep/x ./trash`
    с последующим `rm -rf ./trash` — отказ на первом шаге); источники из stdin `xargs` не проверить — отказ."""
    pos = []
    dest_opt = None
    noclobber = False
    options = True
    k = 0
    while k < len(args):
        a = args[k]
        k += 1
        if options and a == "--":
            options = False
        elif options and a.startswith("--"):
            if a in ("--help", "--version"):
                return
            if a == "--no-clobber":
                noclobber = True
            elif a == "--target-directory" and k < len(args):
                dest_opt = args[k]
                k += 1
            elif a.startswith("--target-directory="):
                dest_opt = a.split("=", 1)[1]
        elif options and a.startswith("-") and len(a) > 1:
            cluster = a[1:]
            noclobber = noclobber or "n" in cluster
            if cluster.endswith("t") and k < len(args):
                dest_opt = args[k]
                k += 1
        else:
            pos.append(a)
    if dest_opt is not None:
        dest, srcs = dest_opt, pos
    elif len(pos) >= 2:
        dest, srcs = pos[-1], pos[:-1]
    else:
        dest, srcs = None, []
    if move and (dest is not None or xargs):
        for src in srcs:
            yield (expand_vars(src, vars_), ctx.copy())          # источник mv исчезает со своего места
        if xargs:
            yield ("<цели из stdin xargs>", ctx.copy())
    if noclobber or dest is None:
        return
    dest = expand_vars(dest, vars_)
    if xargs and dest_opt is not None:                            # `xargs cp -t КАТАЛОГ`: члены неизвестны — сам каталог
        yield from over(dest, {}, ctx)
        return
    fp = None if ctx.remote else fs_path(dest, ctx)
    is_dir = dest_opt is not None or dest.endswith(("/", "\\")) or (fp is not None and os.path.isdir(fp))
    if is_dir and not ctx.remote and fp is not None:      # известный каталог: затрагиваются только его члены
        for src in srcs:
            name = posixpath.basename(expand_vars(src, vars_).replace("\\", "/").rstrip("/"))
            yield from over(dest.rstrip("/\\") + "/" + name, {}, ctx)
    else:
        yield from over(dest, {}, ctx)


def tee_scan(args, ctx, vars_):
    """`tee файл…` усекает файлы; `-a`/`--append` — дописывает (не трогает)."""
    pos = []
    options = True
    for a in args:
        if options and a == "--":
            options = False
        elif options and a.startswith("--"):
            if a in ("--help", "--version", "--append"):
                return
        elif options and a.startswith("-") and len(a) > 1:
            if "a" in a[1:]:
                return
        else:
            pos.append(a)
    for t in pos:
        yield from over(t, vars_, ctx)


PS_COPY_NAMES = {"copy-item": False, "ci": False, "copy": False, "move-item": True, "mi": True, "move": True}
PS_RENAME_NAMES = {"rename-item", "rni", "ren", "rename"}
PS_WRITE_NAMES = {"out-file", "tee-object", "set-content", "sc", "clear-content"}
PS_COPY_VALUES = ("path", "literalpath", "destination", "filter", "include", "exclude", "credential", "stream")
PS_RENAME_VALUES = ("path", "literalpath", "newname", "credential")
PS_WRITE_VALUES = ("filepath", "path", "literalpath", "value", "encoding", "width", "inputobject", "variable", "stream",
                   "delimiter", "credential", "filter", "include", "exclude")


def ps_parse(args, value_params):
    """Аргументы командлета PowerShell → (именованные {имя: значение}, позиционные, флаги). Имена параметров — строчными,
    допустима однозначная приставка (`-Dest`), `-Имя:значение`."""
    named, pos, flags = {}, [], set()
    k = 0
    while k < len(args):
        a = args[k]
        k += 1
        if a.startswith("-") and len(a) > 1 and not a[1].isdigit():
            name, _, val = a[1:].partition(":")
            name = name.lower()
            canon = next((vp for vp in value_params if len(name) >= 2 and vp.startswith(name)), None)
            if canon is None and name in value_params:
                canon = name
            if canon is None:
                flags.add(name)
            elif val:
                named[canon] = val
            elif k < len(args):
                named[canon] = args[k]
                k += 1
        else:
            pos.append(a)
    return named, pos, flags


def ps_flag(flags, full, minlen=2):
    return any(f and len(f) >= minlen and full.startswith(f) for f in flags)


def ps_list(v):
    return [x for x in re.split(r",(?![^()]*\))", v or "") if x]


def ps_copy_scan(name, args, ctx, vars_, xargs=False):
    """`Copy-Item`/`Move-Item` (и `copy`/`move`, `ci`/`mi`): `Источник [Приёмник]` — как `cp`/`mv` (у Move-Item источник
    исчезает)."""
    args = [a for a in args if not re.fullmatch(r"/(?:-?y|v|n|z|d)", a, re.I)]       # ключи cmd `copy /Y`, `move /Y`
    named, pos, flags = ps_parse(args, PS_COPY_VALUES)
    if ps_flag(flags, "whatif") or ps_flag(flags, "?", 1):
        return
    srcs = ps_list(named.get("path") or named.get("literalpath")) or ps_list(pos.pop(0) if pos else "")
    dest = named.get("destination") or (pos.pop(0) if pos else None)
    if dest is None:
        return
    yield from cp_scan(srcs + [dest], ctx, vars_, move=PS_COPY_NAMES[name], xargs=xargs)


def ps_rename_scan(args, ctx, vars_):
    """`Rename-Item`/`ren`: старое имя исчезает — как удаление источника."""
    named, pos, flags = ps_parse(args, PS_RENAME_VALUES)
    if ps_flag(flags, "whatif"):
        return
    for src in ps_list(named.get("path") or named.get("literalpath")) or ps_list(pos[0] if pos else ""):
        yield (expand_vars(src, vars_), ctx.copy())


def ps_write_scan(name, args, ctx, vars_):
    """`Out-File`/`Tee-Object`/`Set-Content`/`Clear-Content` без `-Append`/`-NoClobber` усекают файл."""
    named, pos, flags = ps_parse(args, PS_WRITE_VALUES)
    if ps_flag(flags, "whatif") or ps_flag(flags, "append") or ps_flag(flags, "noclobber", 3):
        return
    target = named.get("filepath") or named.get("path") or named.get("literalpath")
    if target is None and pos and not (name == "tee-object" and "variable" in named):
        target = pos[0]
    for t in ps_list(target):
        yield from over(t, vars_, ctx)


def truncate_scan(args, ctx, vars_):
    pos = []
    k = 0
    while k < len(args):
        a = args[k]
        k += 1
        if a in ("--help", "--version"):
            return
        if a in ("-s", "--size", "-r", "--reference"):
            k += 1
        elif a.startswith("-") and len(a) > 1:
            continue
        else:
            pos.append(a)
    for t in pos:
        yield from over(t, vars_, ctx)


def dd_scan(args, ctx, vars_):
    for a in args:
        if a.startswith("of="):
            yield from over(a[3:], vars_, ctx)


def ssh_scan(args, ctx, vars_, stdin, depth):
    j = 0
    port = None
    ident = []
    while j < len(args):
        a = args[j]
        if a == "--":
            j += 1
            break
        if a.startswith("-") and len(a) > 1:
            if a in SSH_VAL:
                val = args[j + 1] if j + 1 < len(args) else ""
                if a == "-p":
                    port = val
                elif a == "-o" and re.match(r"port\s*[= ]\s*(\d+)", val, re.I):
                    port = re.match(r"port\s*[= ]\s*(\d+)", val, re.I).group(1)
                elif a in ("-i", "-F", "-J", "-l"):
                    ident.append(val)
                j += 2
                continue
            if a.startswith("-p") and a[2:].isdigit():
                port = a[2:]
            elif a.lower().startswith("-oport"):
                port = re.sub(r"\D", "", a)
            j += 1
            continue
        if (re.fullmatch(r"\$\{?\w+(?:\[[@*]\])?\}?", a) and j + 1 < len(args)
                and re.fullmatch(r"[^@\s$]+@[^@\s$]+|\d+\.\d+\.\d+\.\d+", args[j + 1])):
            j += 1                                  # `ssh "${K[@]}" user@host …` — неизвестный массив/строка опций
            continue
        break
    host = args[j] if j < len(args) else ""
    rest = args[j + 1:]
    host = expand_vars(host, vars_)
    bad = (port == "23" or bool(BOX_RE.search(host + " " + " ".join(ident))) or "$" in host)
    remote = Ctx(None, True, host=CLOSED_HOST if bad else host_of(host))
    if rest:
        found = list(scan_text(" ".join(rest), remote, stdin, depth + 1))
    else:                                       # оболочка на стороне ssh читает stdin (heredoc / конвейер)
        found = []
        for text in stdin:
            found.extend(scan_text(text, remote, (), depth + 1))
    if found and bad:
        yield (f"<ssh {host}: удаление на закрытом узле (Storage Box, коллектор, хост не определён)>", ctx.copy())
    else:
        yield from found


def shell_code(args):
    """(строка-код | None, найден ли скрипт-файл) для `bash …`: `-c STRING` либо скрипт/stdin."""
    j = 0
    while j < len(args):
        a = args[j]
        if a in ("-o", "+o", "-O", "+O"):
            j += 2
        elif re.fullmatch(r"-[A-Za-z]*c[A-Za-z]*", a) and j + 1 < len(args):
            return args[j + 1], False
        elif a.startswith(("-", "+")) and a != "-":
            j += 1
        else:
            return None, a != "-"
    return None, False


def ps_code(args):
    for j, a in enumerate(args):
        low = a.lower()
        if low in ("-c", "-command") or (low.startswith("-com") and len(low) <= 8 and "command".startswith(low[1:])):
            return " ".join(args[j + 1:])
        if low in ("-e", "-ec", "-enc", "-encodedcommand") and j + 1 < len(args):
            try:
                return base64.b64decode(args[j + 1]).decode("utf-16le")
            except Exception:
                return "rm <недекодируемый -EncodedCommand>"
    return None


def record_var(vars_, word, depth):
    """`NAME=value` → строка; `NAME=(a b c)` → список слов."""
    k, _, v = word.partition("=")
    if v.startswith("(") and v.endswith(")"):
        vars_[k.rstrip("+")] = [w for c in tokenize(v[1:-1].replace(chr(10), " "), depth + 1) for w in c.words]
    else:
        vars_[k] = expand_vars(v, vars_)


def scan_one(cmd, ctx, vars_, stdin, depth):
    """Удаления одной простой команды: (цель, контекст) по каждой."""
    words = list(cmd.words)
    for t in cmd.overwrites:                          # `> файл` усекает файл — как удаление
        yield from over(t, vars_, ctx)
    sin = list(cmd.heredocs) + list(stdin) + pipe_src(cmd)
    if not words:
        return
    sin = [expand_vars(x, vars_) for x in sin]
    if len(words) == 3 and re.fullmatch(r"\$\w+", words[0]) and words[1] in ("=", "+="):
        vars_[words[0][1:]] = words[2]                # PowerShell: `$script = @'...'@` — строка, которую потом отдадут ssh
        return
    if len(words) == 1 and re.match(r"^\$\w+=", words[0]):    # PowerShell: `$out="data\dashboard"` (без пробелов)
        name_, _, value_ = words[0].partition("=")
        vars_[name_[1:]] = expand_vars(value_, vars_)
        return
    if all(ASSIGN.match(w) for w in words):          # `NAME=value` без команды — запомнить переменные
        for w in words:
            record_var(vars_, w, depth)
        return
    xargs = False
    i = 0
    for _ in range(40):
        if i >= len(words):
            return
        w = words[i]
        if ASSIGN.match(w):
            i += 1
            continue
        name = cmd_name(w)
        if name in RESERVED:
            i += 1
            continue
        if name in WRAPPERS:
            j = skip_wrapper(name, words, i)
            if j is None:
                return
            xargs = xargs or name == "xargs"
            i = j
            continue
        if w.startswith("$") and not w.startswith("$(") and expand_vars(w, vars_) != w:
            val = expand_vars(w, vars_)              # `$SSHC host 'rm x'` при SSHC="ssh -p 23 …"
            sub = tokenize(val)
            words[i:i + 1] = sub[0].words if sub else []
            continue
        break
    else:
        return
    if i >= len(words):
        return
    w = words[i]
    name = cmd_name(w)
    args = expand_args(words[i + 1:], vars_)
    if name in ("export", "declare", "local", "readonly", "typeset"):
        for a in words[i + 1:]:
            if ASSIGN.match(a):
                record_var(vars_, a, depth)
        return
    if name in DEL_VERBS:
        targets = del_targets(name, args, xargs, cmd.pipe_from is not None)
        for t in targets or ():
            yield (t, ctx.copy())
    elif PS_DOTNET_DELETE.match(w):
        yield (UNKNOWN_TARGET, ctx.copy())
    elif name == "find":
        yield from find_scan(args, ctx, vars_, depth)
    elif name == "rsync":
        yield from rsync_scan(args, ctx)
    elif name in ("cp", "mv"):
        yield from cp_scan(args, ctx, vars_, move=name == "mv", xargs=xargs)
    elif name in PS_COPY_NAMES:
        yield from ps_copy_scan(name, args, ctx, vars_, xargs)
    elif name in PS_RENAME_NAMES:
        yield from ps_rename_scan(args, ctx, vars_)
    elif name == "tee":
        yield from tee_scan(args, ctx, vars_)
    elif name in PS_WRITE_NAMES:
        yield from ps_write_scan(name, args, ctx, vars_)
    elif name == "truncate":
        yield from truncate_scan(args, ctx, vars_)
    elif name == "dd":
        yield from dd_scan(args, ctx, vars_)
    elif name == "git":
        redirected = any(re.match(r"(?:GIT_DIR|GIT_WORK_TREE)=", x) for x in words[:i])
        yield from git_scan(args, ctx, vars_, redirected)
    elif name == "rclone":
        if args and args[0] in ("delete", "deletefile", "purge", "rmdir", "rmdirs", "cleanup"):
            yield (f"<rclone {args[0]}>", ctx.copy())
    elif name == "ssh":
        yield from ssh_scan(args, ctx, vars_, sin, depth)
    elif name in RUNNERS:                            # `uv run python -c …`, `poetry run rm …`
        for k, a in enumerate(args):
            nm = cmd_name(a)
            if nm in DEL_VERBS or nm in SHELLS or nm in ("find", "git", "rsync", "ssh") or PY_NAMES.match(nm):
                yield from scan_one(Cmd(args[k:]), ctx, vars_, sin, depth + 1)
                break
    elif name in OTHER_INTERP:                       # perl/ruby/node/php: вызовы удаления в коде — цель не видна
        if any(OTHER_DEL_RE.search(t) for t in list(args) + list(sin)):
            yield (UNKNOWN_TARGET, ctx.copy())
    elif name == "robocopy":                         # /MIR и /PURGE удаляют лишнее в приёмнике (второй путь)
        flags = [a.lower() for a in args if a.startswith("/")]
        if "/mir" in flags or "/purge" in flags:
            pos = [a for a in args if not a.startswith("/")]
            yield (pos[1] if len(pos) > 1 else "<robocopy без цели>", ctx.copy())
    elif PY_NAMES.match(name):
        code = None
        k = 0
        while k < len(args):
            a = args[k]
            if re.fullmatch(r"-[A-Za-z]*c", a) and k + 1 < len(args):
                code = [args[k + 1]]
                break
            if a == "-":
                code = sin
                break
            if a in ("-W", "-X"):
                k += 2
            elif a.startswith("-"):
                k += 1
            else:
                break                                  # скрипт-файл или -m: тело не видно
        else:
            code = sin
        for text in code or ():
            yield from python_scan(text, ctx, depth)
    elif name in SHELLS or name == "eval" or name in ("invoke-expression", "iex"):
        if name in SHELLS:
            text, has_script = shell_code(args)
            texts = [text] if text is not None else ([] if has_script else sin)
        else:
            texts = [" ".join(args)]
        for text in texts:
            yield from scan_text(text, ctx, (), depth + 1)
    elif name in PS_NAMES:
        text = ps_code(args)
        for t in ([text] if text is not None else []):
            yield from scan_text(t, ctx, (), depth + 1)
    elif name == "cmd":
        for k, a in enumerate(args):
            if a.lower() in ("/c", "/k", "/r"):
                yield from scan_text(" ".join(args[k + 1:]), ctx, (), depth + 1)
                break
    elif name in ("cd", "pushd", "chdir", "set-location", "sl"):
        pos = [a for a in args if not a.startswith("-") or a == "-"]
        ctx.cwd = join_cwd(ctx.cwd, pos[0] if pos else None, vars_)
    elif name == "popd":
        ctx.cwd = None
    else:
        yield from generic_scan(w, name, args, ctx, vars_, sin, depth)


def generic_scan(w, name, args, ctx, vars_, sin, depth):
    """Неизвестная команда: обёртка над ssh (`/tmp/sshx user@host 'rm …'`, `$SSH host '…'`, функция `rsh() {…}`).
    Строки-аргументы разбираются как удалённые команды только если есть признак обёртки — `user@host` среди
    аргументов, переменная вместо команды или функция, объявленная здесь же; иначе это просто текст."""
    if name in TEXT_CMDS:
        return
    unresolved = w.startswith("$") and not w.startswith("$(")
    if unresolved and args and args[0] in ("=", "+="):          # PowerShell: `$x = <строка>` — присваивание
        return
    if any(HOST_RE.fullmatch(a) for a in args):
        yield from ssh_scan(args, ctx, vars_, sin, depth)
    elif unresolved or name in ctx.funcs:
        inner = []
        for a in args:
            if re.search(r"\s", a):
                inner.extend(scan_text(a, Ctx(None, True, ctx.funcs), (), depth + 1))
        if inner and unresolved:
            yield (f"<команда задана переменной {w}>", ctx.copy())
        else:
            yield from inner


def scan_cmds(cmds, ctx, stdin=(), depth=0):
    if depth > MAX_DEPTH:
        yield ("<вложенность разбора слишком глубока>", ctx.copy())
        return
    ctx = ctx.copy()
    vars_ = {}
    for cmd in cmds:
        yield from scan_one(cmd, ctx, vars_, stdin, depth)


def scan_text(text, ctx, stdin=(), depth=0):
    yield from scan_cmds(tokenize(text, depth), ctx, stdin, depth)


# ------------------------------------------------------------------------------------------------------------
# Страховка: прежний регэксп (если разбор не удался — незакрытая кавычка и т.п.)
# ------------------------------------------------------------------------------------------------------------

LEGACY_VERBS = re.compile(
    r"(?:^|[\s;&|(`'\"])(?:sudo\s+)?(rm|rmdir|unlink|shred|Remove-Item|ri|del|erase|rd)(?=\s)"
    r"|find\s[^;&|\n]*-delete|rsync\s[^;&|\n]*--delete|git\s+clean|shutil\.rmtree|os\.(?:remove|unlink|rmdir)"
    r"|\.unlink\(|rmtree\(", re.I)
LEGACY_SEP = re.compile(r"[;&|\n)`]")


def legacy_found(cmd, cwd):
    found = []
    remote = "ssh" in cmd.lower() or bool(re.search(r"\b(?:cd|Set-Location|pushd)\s", cmd, re.I))
    ctx = Ctx(None if remote else norm(cwd or "").rstrip("/"), False)
    for m in LEGACY_VERBS.finditer(cmd):
        verb = (m.group(1) or "").lower()
        end = LEGACY_SEP.search(cmd, m.end())
        if not verb:
            tail = cmd[m.start():end.start() if end else len(cmd)]
            quoted = re.findall(r"['\"]([^'\"]+)['\"]", tail)
            words = [w for w in re.split(r"\s+", tail) if w and not w.startswith("-")]
            if tail[:4].lower() == "find":
                ts = words[1:2] or ["<find без пути>"]
            elif tail[:5].lower() == "rsync":
                ts = words[-1:] or ["<rsync без цели>"]
            elif tail[:3].lower() == "git":
                ts = ["."]
            else:
                ts = quoted or [UNKNOWN_TARGET]
        else:
            tail = cmd[m.end():end.start() if end else len(cmd)]
            ts = [w for w in re.findall(r"\"[^\"]*\"|'[^']*'|\S+", tail)
                  if not w.startswith("-") and w not in (">", "2>")]
        found.extend((t, ctx) for t in ts)
    if found and BOX_RE.search(cmd):
        found.append(("<Storage Box/коллектор в команде>", ctx))
    return found


# ------------------------------------------------------------------------------------------------------------
# Аудит-3 (03.10): чьи правила и что защищено всегда
# ------------------------------------------------------------------------------------------------------------

DISPATCH_ROLES = ("researcher", "engineer", "judge")
SETTINGS_NAMES = ("settings.json", "settings.local.json")
REASON_GIT_DIR = ("Каталог .git ({t}) — история репозитория: удалять или переносить его и файлы внутри нельзя никому "
                  "(`rm -rf .git`, `rm -rf .*`, `mv .git …`); можно только снять зависший замок `*.lock`. Чистка "
                  "истории — только владелец через CEO.")
REASON_GIT_WRITE = ("Запуск диспетчера не пишет внутрь .git ({t}): состояние репозитория меняют команды git, а не правка "
                    "файлов. Нужно иное — через CEO.")
REASON_SETTINGS = ("Настройки Claude Code ({t}) запуску диспетчера менять нельзя: через них выключаются хуки и плагин "
                   "(`disableAllHooks`, `enabledPlugins`), а с ними и этот страж. Правка настроек — через CEO.")
REASON_FILE_HARD = ("Запись в {t}: записи root/ и deep/ (единственные копии) и закрытые узлы — никогда, в любой сессии. "
                    "Только владелец через CEO.")


def dispatcher_role():
    """Роль запуска диспетчера (`RPV_ROLE`, её ставит dispatch.launch_run; наследуют и помощники роли) или None —
    сессия CEO или владельца."""
    r = _env("ROLE").strip().lower()
    return r if r in DISPATCH_ROLES else None


def _abs_norm(t, ctx):
    """Нормализованный путь цели; относительный — от известного рабочего каталога (неизвестен — как есть)."""
    p = norm(t)
    if p and not (p.startswith(("/", "~", "$", "%")) or re.match(r"[a-z]:", p)):
        cwd = getattr(ctx, "cwd", None)
        if cwd:
            p = posixpath.normpath(cwd + "/" + p)
    return p


def git_internal(t, ctx):
    """Цель — каталог `.git` или путь внутри него, в т. ч. шаблон оболочки, который совпадёт с `.git` (`.*`, `.[!.]*`,
    `.g*`). Исключения: зависший замок `….lock` внутри .git и временные каталоги."""
    import fnmatch
    p = _abs_norm(t, ctx)
    if not p or p.startswith("<") or temp_rest(p) is not None:
        return False
    segs = [s for s in p.split("/") if s]
    for i, s in enumerate(segs):
        if s == ".git" or (s.startswith(".") and any(c in s for c in "*?[") and fnmatch.fnmatchcase(".git", s)):
            rest = segs[i + 1:]
            if s == ".git" and rest and rest[-1].endswith(".lock") and not any(c in rest[-1] for c in "*?["):
                return False
            return True
    return False


def settings_file(t, ctx):
    """Цель — файл настроек Claude Code (`.claude/settings.json`, `.claude/settings.local.json`) в любом каталоге."""
    segs = [s for s in _abs_norm(t, ctx).split("/") if s]
    return len(segs) >= 2 and segs[-2] == ".claude" and segs[-1] in SETTINGS_NAMES


def protected(t, ctx, role, deleting):
    """Защищённые места → причина отказа или None. `.git`: удаление и перенос — никому, запись — запускам диспетчера;
    настройки Claude Code — запускам диспетчера (ни удалить, ни перезаписать)."""
    if git_internal(t, ctx):
        if deleting:
            return REASON_GIT_DIR.format(t=t)
        if role:
            return REASON_GIT_WRITE.format(t=t)
    if role and settings_file(t, ctx):
        return REASON_SETTINGS.format(t=t)
    return None


def hard_forbidden_file(path, ctx):
    """Сессиям вне диспетчера (CEO, владелец) файловые инструменты закрыты только правилом «никогда»: записи root/ и deep/
    и закрытые узлы. Домашний каталог (`/root` у root на Linux) сегментом не считается; во временном каталоге — сегменты
    после его корня."""
    p = norm(fs_path(path, ctx) or path)
    if BOX_RE.search(p):
        return True
    tr = temp_rest(p)
    if tr is not None:
        p = tr[0]
    else:
        home = norm(os.path.expanduser("~")).rstrip("/") + "/"
        if p.startswith(home):
            p = p[len(home):]
    return any(s in FORBIDDEN_SEG for s in literal_part(p).split("/") if s)


def check(cmd, cwd):
    """Причина отказа или None. Сам не падает: сбой разбора — прежний регэксп, сбой и его — отказ (fail-closed)."""
    cmd = cmd or ""
    try:
        role = dispatcher_role()
        start = Ctx(norm(cwd).rstrip("/") if cwd else None, False, frozenset(FUNC_DEF.findall(cmd)))
        try:
            found = list(scan_text(cmd, start))
        except ParseError:                          # незакрытая кавычка и т. п. — прежний регэксп как страховка
            found = legacy_found(cmd, cwd)
        for target, ctx in found:
            if isinstance(target, Forbid):
                return REASON_IRREVERSIBLE.format(t=target)
            why = protected(target, ctx, role, deleting=not isinstance(target, Over))
            if why:
                return why
            if isinstance(target, Over):
                if not (allowed(target, ctx) or write_only_ok(target, ctx)) and may_exist(target, ctx):
                    return REASON_OVERWRITE.format(t=target)
                continue
            if not allowed(target, ctx):
                return REASON.format(t=target)
    except Exception as e:                          # страж упал: пропуск был бы дырой — отказ с причиной
        return REASON_CRASH.format(e=f"{type(e).__name__}: {e}"[:200])
    return None


FILE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
SHELL_TOOLS = ("Bash", "PowerShell")
GUARDED_TOOLS = SHELL_TOOLS + FILE_TOOLS
FILE_KEYS = ("file_path", "notebook_path", "path", "filePath")


def file_target(tool_input):
    """Путь, который меняет файловый инструмент (Write/Edit/MultiEdit: `file_path`, NotebookEdit: `notebook_path`)."""
    for k in FILE_KEYS:
        v = tool_input.get(k) if isinstance(tool_input, dict) else None
        if isinstance(v, str) and v.strip():
            return v
    return None


def check_file(path, cwd):
    """Запись файловым инструментом. Запуск диспетчера (`RPV_ROLE`) — как перезапись через Bash: путь вне корней (папка
    проекта, scratchpad, temp, автопамять) и файл уже есть (или не проверить) — отказ; новый файл вне корней создать можно.
    Сессия CEO/владельца (аудит-3) правит файлы вне проекта свободно (настройки, соседние репозитории, планы); для неё
    остаётся только «никогда»: root/ и deep/, закрытые узлы. Для всех: внутрь .git и в настройки Claude Code — см. protected."""
    try:
        ctx = Ctx(norm(cwd).rstrip("/") if cwd else None, False)
        target = Over(path)
        role = dispatcher_role()
        why = protected(target, ctx, role, deleting=False)
        if why:
            return why
        if role is None:
            if hard_forbidden_file(path, ctx) and may_exist(target, ctx):
                return REASON_FILE_HARD.format(t=path)
            return None
        if allowed(target, ctx) or write_only_ok(target, ctx) or not may_exist(target, ctx):
            return None
        return REASON_FILE.format(t=path)
    except Exception as e:
        return REASON_CRASH.format(e=f"{type(e).__name__}: {e}"[:200])


def check_tool(data):
    """Событие PreToolUse (словарь из JSON хука) → причина отказа или None. Чужой инструмент — None. Путь файлового
    инструмента определить нельзя — отказ (схема могла измениться: молча пропускать нельзя)."""
    if not isinstance(data, dict):
        return REASON_CRASH.format(e="событие хука не объект JSON")
    tool = data.get("tool_name")
    if tool not in GUARDED_TOOLS:
        return None
    ti = data.get("tool_input")
    ti = ti if isinstance(ti, dict) else {}
    cwd = str(data.get("cwd") or "")
    try:
        if tool in SHELL_TOOLS:
            return check(str(ti.get("command") or ""), cwd)
        path = file_target(ti)
        if path is None:
            return REASON_CRASH.format(e=f"{tool}: в tool_input нет пути файла")
        return check_file(path, cwd)
    except Exception as e:
        return REASON_CRASH.format(e=f"{type(e).__name__}: {e}"[:200])


def main():
    """PreToolUse-хук: stdin — JSON события; отказ — JSON с permissionDecision=deny. Fail-closed: битое событие и сбой
    стража — отказ с причиной. Тишина — только если инструмент не наш или в проекте нет команды ролей (`.claude/roles`)."""
    import json
    import sys

    def deny(reason):
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                 "permissionDecisionReason": reason}}, ensure_ascii=True))
        return 0

    def has_roles(project):
        return os.path.isdir(os.path.join(project, ".claude", "roles"))

    try:
        data = json.load(sys.stdin)
        if not isinstance(data, dict):
            raise ValueError("событие не объект JSON")
    except Exception as e:
        if not has_roles(os.path.abspath(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())):
            return 0
        return deny(REASON_CRASH.format(e=f"событие хука не прочитано: {type(e).__name__}: {e}"[:200]))
    try:
        if data.get("tool_name") not in GUARDED_TOOLS:
            return 0
        project = os.path.abspath(os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd") or os.getcwd())
        if not has_roles(project):                                      # в проекте нет команды ролей — молчим
            return 0
        configure(project)
        reason = check_tool(data)
    except Exception as e:
        reason = REASON_CRASH.format(e=f"{type(e).__name__}: {e}"[:200])
    return deny(reason) if reason else 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
