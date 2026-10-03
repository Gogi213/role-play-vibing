"""Удаление — только внутри своей папки (владелец 27.09: «нельзя удалять ничего кроме чего то внутри своей папки»).

Вызывается из PreToolUse-хука Bash/PowerShell (`~/.claude/hooks/alpha_one_build.py`, только в проекте alpha):
`check(cmd, cwd)` → причина отказа или None.
Своя папка: локально — папка проекта и scratchpad сессии; на Steam Deck — `~/alpha/<подкаталог>`; на VPS —
`/opt/alpha-compute/<подкаталог>`; оперативная стадия `/dev/shm/alpha-stage` (В-162). Никогда: коллектор
(`HOST_COLLECTOR`), Storage Box (ssh порт 23), записи `root/` и `deep/` (единственные копии, В-106). Цель, которую нельзя
проверить (переменная без буквального префикса, список из конвейера/xargs, путь из кода), — отказ: переписать явным путём.

Разбор (02.10, аудит: регэксп по всему тексту давал ~52 ложных отказа за 5 дней — на переменную `rd`, слово «rm» в
комментарии тикета, `grep 'unlink'`): команда режется на простые команды (`;` `&&` `||` `|` `&`, перевод строки, `$(…)`,
кавычки и heredoc учитываются), и удалением считается только простая команда, у которой КОМАНДНОЕ слово — rm/rmdir/unlink/
shred/del/erase/rd/Remove-Item/ri, `find … -delete|-exec rm`, `git clean`, `rsync --delete*|--remove-source-files`,
либо исполняемый Python (`-c`, stdin/heredoc), вызывающий shutil.rmtree/os.remove/os.unlink/os.rmdir/Path.unlink/.rmdir().
Через обёртки (sudo/env/nohup/xargs/systemd-run/timeout/…), `ssh хост '<строка>'`, `bash|sh -c`, `powershell -Command`,
`cmd /c`, eval — разбор рекурсивный. Текст в аргументах прочих команд (git commit -m, tickets.py --text, echo, grep,
`cat > файл <<EOF`) удалением не считается. Не удалось разобрать (незакрытая кавычка) — прежний регэксп как страховка.
"""
import ast
import base64
import datetime
import posixpath
import re
import textwrap

LOCAL_ROOTS = ("c:/visual projects/alpha/", "/c/visual projects/alpha/")
SCRATCH = "/appdata/local/temp/claude/"
REMOTE_ROOTS = ("~/alpha/", "$home/alpha/", "${home}/alpha/", "/home/deck/alpha/", "/opt/alpha-compute/")
STAGE = "/dev/shm/alpha-stage"  # оперативная стадия подкачки суток на Steam Deck (В-162); целиком, включая root/ внутри
FORBIDDEN_SEG = ("root", "deep")
BOX_RE = re.compile(r"storage-?box|your-storagebox|HOST_COLLECTOR", re.I)
REASON = ("Удаление запрещено вне своей папки (владелец 27.09: «нельзя удалять ничего кроме чего то внутри своей "
          "папки»). Можно только явным путём: локально — внутри C:/visual projects/alpha или scratchpad сессии; "
          "на Steam Deck — ~/alpha/<подкаталог>; на VPS — /opt/alpha-compute/<подкаталог>. Коллектор, Storage Box, "
          "записи root/ и deep/ — никогда (только владелец через CEO). Непроверяемая цель: {t}")
UNKNOWN_TARGET = "<цель из кода не видна>"
MAX_DEPTH = 8


def norm(p):
    return p.strip().strip("'\"").replace("\\", "/").lower()


def allowed(t, ctx=None):
    """Цель удаления разрешена (путь внутри своей папки)? `ctx.cwd` — рабочий каталог для относительных путей."""
    p = norm(t)
    if not p or p.startswith("<") or "{}" in p:
        return False
    literal = re.split(r"\$(?!home\b|\{home\})", p, maxsplit=1)[0] if "$" in p else p
    if not literal or ".." in literal.split("/") or BOX_RE.search(p):
        return False
    if literal == STAGE or literal.startswith(STAGE + "/"):
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
    # относительный путь: от известного рабочего каталога (локально — cwd проекта, на ssh — после `cd ~/alpha/…`)
    cwd = getattr(ctx, "cwd", None)
    if not cwd:
        return False
    return allowed(posixpath.normpath(cwd + "/" + literal), None)


# ------------------------------------------------------------------------------------------------------------
# Лексер: текст команды → простые команды
# ------------------------------------------------------------------------------------------------------------

class ParseError(Exception):
    pass


class Cmd:
    __slots__ = ("words", "heredocs", "pipe_from")

    def __init__(self, words=None):
        self.words = list(words or [])
        self.heredocs = []      # тела heredoc / here-string этой команды (её stdin)
        self.pipe_from = None   # предыдущая команда конвейера


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
        self.skip = False   # следующее слово — цель перенаправления (True) или here-string ("here")
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
            elif not self.skip:
                self.cur.words.append(w)
            self.skip = False
        self.buf = []
        self.inword = False

    def end_cmd(self, sep):
        self.end_word()
        self.skip = False
        done = self.cur
        if done.words:
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
                    self.i += 3 if s.startswith("&>>", i) else 2
                    self.skip = True
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
        if i < n and s[i] == c and c == ">":
            i += 1                      # `>>`
        if i < n and s[i] == "&":
            i += 1                      # `>&2`, `2>&1`, `>&-`
            while i < n and (s[i].isdigit() or s[i] == "-"):
                i += 1
        elif i < n and s[i] == "|":
            i += 1
            self.skip = True
        elif i < n and s[i] == "(":     # process substitution `<(…)` / `>(…)`
            j = find_close(s, i + 1)
            self.sub(s[i + 1:j])
            i = j + 1
        else:
            self.skip = True
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
    __slots__ = ("cwd", "remote", "funcs")

    def __init__(self, cwd=None, remote=False, funcs=frozenset()):
        self.cwd = cwd          # нормализованный каталог или None (неизвестен)
        self.remote = remote
        self.funcs = funcs      # имена функций оболочки, объявленных в этой же команде (`rsh() { ssh …; }`)

    def copy(self):
        return Ctx(self.cwd, self.remote, self.funcs)


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
            if inner:
                deleting = True
                extra += [x for x in inner if "{}" not in x[0]]
        k += 1
    if deleting:
        for p in paths or ["."]:
            yield (p, ctx.copy())
        yield from extra


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
            yield (path, Ctx(None, True))
        else:
            yield (path, ctx.copy())


def git_scan(args, ctx, vars_):
    j = 0
    base = None
    while j < len(args):
        a = args[j]
        if a == "-C" and j + 1 < len(args):
            base = args[j + 1]
            j += 2
        elif a in ("-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"):
            j += 2
        elif a.startswith("-"):
            j += 1
        else:
            break
    if j >= len(args) or args[j] != "clean":
        return
    rest = args[j + 1:]
    if any(a == "--dry-run" or (a.startswith("-") and not a.startswith("--") and "n" in a) for a in rest):
        return
    after = rest[rest.index("--") + 1:] if "--" in rest else []
    specs = after or [a for a in rest if not a.startswith("-")]
    b = base or "."
    for sp in (specs or [None]):
        yield ((b if sp is None else posixpath.join(b, sp)), ctx.copy())


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
    remote = Ctx(None, True)
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
    sin = list(cmd.heredocs) + list(stdin) + pipe_src(cmd)
    if not words:
        return
    sin = [expand_vars(x, vars_) for x in sin]
    if len(words) == 3 and re.fullmatch(r"\$\w+", words[0]) and words[1] in ("=", "+="):
        vars_[words[0][1:]] = words[2]                # PowerShell: `$script = @'...'@` — строка, которую потом отдадут ssh
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
    elif name == "git":
        yield from git_scan(args, ctx, vars_)
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


# Узкое исключение (В-154, владелец 02.10: «удаляй лишнее точно»): дедуп TK-020 — только скриптом по манифесту,
# который перед удалением каждого файла заново сверяет sha256 с каноном на Storage Box. Срок — до 2026-10-09.
DEDUPE_APPLY = re.compile(r"tk020-dedupe-apply\.(?:sh|py)")
DEDUPE_UNTIL = "2026-10-09"


def check(cmd, cwd):
    """Причина отказа или None."""
    cmd = cmd or ""
    if DEDUPE_APPLY.search(cmd) and datetime.date.today().isoformat() <= DEDUPE_UNTIL:
        return None
    start = Ctx(norm(cwd).rstrip("/") if cwd else None, False, frozenset(FUNC_DEF.findall(cmd)))
    try:
        found = list(scan_text(cmd, start))
    except Exception:                               # ParseError и любая неожиданность — прежний разбор
        try:
            found = legacy_found(cmd, cwd)
        except Exception:
            found = [("<команду не удалось разобрать>", start)] if LEGACY_VERBS.search(cmd) else []
    for target, ctx in found:
        if not allowed(target, ctx):
            return REASON.format(t=target)
    return None
