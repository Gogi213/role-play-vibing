"""Разбор простой команды рекурсивно: удаление, Python-код, `find`, `ssh`, обёртки; вход — `scan_text`."""
import ast
import re
import textwrap
from dguard.git_scan import git_scan
from dguard.lexer import Cmd, tokenize
from dguard.messages import Forbid, MAX_DEPTH, Over, Publish, UNKNOWN_TARGET
from dguard.paths import CLOSED_HOST, closed_host, Ctx
from dguard.publish import HTTP_NAMES, publish_label, PUBLISH_SUBS
from dguard.words import ASSIGN, cmd_name, DEL_OS, DEL_VERBS, expand_args, expand_vars, host_of, HOST_RE, join_cwd, OTHER_DEL_RE, OTHER_INTERP, pipe_src, ps_code, PS_DOTNET_DELETE, PS_NAMES, PY_CALL_RE, PY_NAMES, RESERVED, RUNNERS, shell_code, SHELLS, skip_wrapper, SSH_VAL, SUBPROC, TEXT_CMDS, WRAPPERS
from dguard.writes import cp_scan, dd_scan, del_targets, over, PS_COPY_NAMES, ps_copy_scan, PS_RENAME_NAMES, ps_rename_scan, PS_WRITE_NAMES, ps_write_scan, rsync_scan, tee_scan, truncate_scan


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


def ssh_scan(args, ctx, vars_, stdin, depth):
    j = 0
    ident = []
    while j < len(args):
        a = args[j]
        if a == "--":
            j += 1
            break
        if a.startswith("-") and len(a) > 1:
            if a in SSH_VAL:
                if a in ("-i", "-F", "-J", "-l"):
                    ident.append(args[j + 1] if j + 1 < len(args) else "")
                j += 2
                continue
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
    bad = closed_host(host + " " + " ".join(ident)) or "$" in host
    remote = Ctx(None, True, host=CLOSED_HOST if bad else host_of(host))
    if rest:
        found = list(scan_text(" ".join(rest), remote, stdin, depth + 1))
    else:                                       # оболочка на стороне ssh читает stdin (heredoc / конвейер)
        found = []
        for text in stdin:
            found.extend(scan_text(text, remote, (), depth + 1))
    if bad and any(not isinstance(x[0], Publish) for x in found):     # публикация на закрытом узле — не удаление: решает check
        yield (f"<ssh {host}: удаление на закрытом хосте (`guard_forbidden_hosts`) или хост не определён>", ctx.copy())
    else:
        yield from found


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
    if len(words) > 3 and re.fullmatch(r"\$\w+", words[0]) and words[1] in ("=", "+="):
        words = words[2:]                             # PowerShell: `$r = Invoke-RestMethod …` — разбирается команда справа
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
    label = publish_label(name, args)                # публикация (CP2): запуску роли — только с согласием, решает check()
    if label:
        yield (Publish(label), ctx.copy())
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
            if (nm in DEL_VERBS or nm in SHELLS or nm in ("find", "git", "rsync", "ssh") or nm in PUBLISH_SUBS
                    or nm in HTTP_NAMES or PY_NAMES.match(nm)):
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
        pctx = ctx.copy()
        pctx.via = "py-ast"
        for text in code or ():
            yield from python_scan(text, pctx, depth)
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
        sctx = ctx.copy()
        sctx.via = "ps"
        for t in ([text] if text is not None else []):
            yield from scan_text(t, sctx, (), depth + 1)
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
