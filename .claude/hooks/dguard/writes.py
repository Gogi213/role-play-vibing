"""Удаление и запись в файл: `rm`, `cp`/`mv`, `tee`, PowerShell-командлеты, `truncate`, `dd`, `rsync`."""
import os
import posixpath
import re
from dguard.messages import HARMLESS_SINK, Over
from dguard.paths import closed_host, Ctx
from dguard.words import CMD_FLAG, expand_vars, host_of, PS_VALUE_PARAMS, RSYNC_VAL, split_remote


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


def rsync_scan(args, ctx):
    delete = any(a.startswith("--delete") or a == "--del" for a in args)
    remove_src = "--remove-source-files" in args
    if not (delete or remove_src):
        return
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
        if host and closed_host(host):
            yield (f"<rsync: {arg} — закрытый узел>", ctx.copy())
        elif host:
            yield (path, Ctx(None, True, host=host_of(arg)))
        else:
            yield (path, ctx.copy())


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
    `mv` (`move=True`) вдобавок УДАЛЯЕТ источники: каждый источник проверяется как цель удаления (`mv защищённое ./trash`
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
