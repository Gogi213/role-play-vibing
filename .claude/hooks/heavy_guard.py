"""Замок замеров на выделенной машине (`RPV_GUARD_HEAVY_HOST`) — какие команды из ssh-сессии считаются тяжёлыми.

Зачем: замер скорости идёт под замком (скрипт-обёртка, замораживающий чужие юниты), но команда, запущенная прямо из
ssh-сессии, — не юнит: замок её не видит. Без `RPV_GUARD_HEAVY_HOST` проверка выключена.

Эта часть — чистая классификация: `heavy_label(имя, аргументы, stdin, xargs)` → короткая метка тяжёлой команды или None.
Разбор команды (ssh, обёртки, `bash -c`, heredoc, переменные) и признак «обёрнуто в systemd-run / идёт через benchrun»
делает `delete_guard.scan_one`: он зовёт сюда только для простой команды на хосте `HEAVY_HOST` и только без обёртки
systemd-run; скрипт-обёртка замера (`/data/benchrun.sh stand <команда>`) — скрипт, его содержимое страж не разбирает,
поэтому `… benchrun.sh stand <команда>` проходит сам.

Тяжёлое (без юнита): du, find, rsync, vmtouch (и их родня tree/ncdu/locate/fd); `ls -R`, `grep -r`; `cp -r|-R|-a`;
`tar` кроме распаковки со stdin (`tar xzf - -C …` — выкладка файлов с этой машины, ограничена каналом); md5sum/sha*sum/b3sum/
cksum/cmp/`diff -r` — если аргумент каталог, маска, неизвестная переменная, список из xargs, `-c`, либо файл `*.binlog|*.abin`;
`cat`/`cp` файлов `*.binlog|*.abin` и масок под /data/alpha/; `python3 <файл>`, `python3 -m <не py_compile>`,
`python3 -c`/stdin длиннее 200 символов. Исключения — `RPV_GUARD_HEAVY_ALLOW` (регэксп по простой команде `имя арг…`, напр. `^python3 /data/sched/alsched[.]py (submit|status)( |$)`).
Всё прочее (cat/tail/head/ls/grep по файлам, systemctl, uptime, sha256sum отдельных
файлов, `cat > файл && mv`, `python3 -m py_compile`, `python3 -c '<короткое>'`) — свободно.
"""
import os
import re

# Хост замка — только из окружения (`user@` не нужен, как у RPV_GUARD_HOST_ROOTS); пусто — замок выключен.
HEAVY_HOST = (os.environ.get("RPV_GUARD_HEAVY_HOST") or "").strip().lower()


def _compile_allow(raw):
    """`RPV_GUARD_HEAVY_ALLOW` — регэксп по разобранной простой команде `имя арг…`; пусто или негодный регэксп — исключений нет."""
    try:
        return re.compile(raw) if raw else None
    except re.error:
        return None


HEAVY_ALLOW = _compile_allow((os.environ.get("RPV_GUARD_HEAVY_ALLOW") or "").strip())
PY_NAMES = re.compile(r"^(?:python|pythonw|py)[0-9.]*$")
PY_ONELINER_MAX = 200
ALWAYS = {"du", "find", "rsync", "vmtouch", "tree", "ncdu", "locate", "plocate", "mlocate", "updatedb", "fd", "fdfind"}
HASH = {"md5sum", "sha1sum", "sha224sum", "sha256sum", "sha384sum", "sha512sum", "b3sum", "cksum", "shasum", "xxhsum",
        "cmp"}
GREPS = {"grep", "egrep", "fgrep"}
BIG_FILE = re.compile(r"\.(?:binlog|abin)\b", re.I)          # бинлоги и .abin — многогигабайтные файлы данных
TAR_MODES = {"c": "c", "t": "t", "d": "d", "r": "r", "u": "u", "A": "A", "x": "x"}
TAR_LONG = {"--create": "c", "--list": "t", "--diff": "d", "--compare": "d", "--append": "r", "--update": "u",
            "--concatenate": "A", "--catenate": "A", "--delete": "D", "--extract": "x", "--get": "x"}
TAR_VALUE_LETTERS = set("bCfFgHIKLNTVX")                     # короткие опции tar со значением


def opts(args):
    """(буквы коротких опций, имена длинных, позиционные); после `--` всё позиционное. Значения опций с отдельным токеном
    попадают в позиционные — для нужд классификации (флаги, пути) этого достаточно."""
    short, long_, pos = [], set(), []
    options = True
    for a in args:
        if options and a == "--":
            options = False
        elif options and a.startswith("--"):
            long_.add(a.split("=", 1)[0])
        elif options and a.startswith("-") and len(a) > 1:
            short.append(a[1:])
        else:
            pos.append(a)
    return "".join(short), long_, pos


def unknown_path(p):
    """Путь нельзя считать «отдельным файлом»: каталог, маска, brace/подстановка, неизвестная переменная."""
    return any(c in p for c in "*?[{$`") or p.endswith("/") or p in (".", "..")


def python_label(args, stdin):
    code = None
    k = 0
    while k < len(args):
        a = args[k]
        if a in ("-V", "-VV", "--version", "-h", "--help"):
            return None
        if re.fullmatch(r"-[A-Za-z]*m", a):                  # -m МОДУЛЬ: разрешён только py_compile
            mod = args[k + 1] if k + 1 < len(args) else ""
            return None if mod == "py_compile" else f"python3 -m {mod}".strip()
        if re.fullmatch(r"-[A-Za-z]*c", a) and k + 1 < len(args):
            code = [args[k + 1]]
            break
        if a == "-":
            break
        if a in ("-W", "-X"):
            k += 2
        elif a.startswith("-"):
            k += 1
        else:
            return f"python3 {a}"                            # скрипт-файл
    if code is None:                                       # `python3 -` / без аргументов: код читается со stdin
        code = list(stdin)
        if not code:
            return "python3 со stdin (код не виден)"
    n = len("\n".join(code))
    return None if n <= PY_ONELINER_MAX else f"python3 -c/stdin на {n} символов (больше {PY_ONELINER_MAX})"


def tar_label(args):
    """Разрешена только распаковка из stdin (`tar xzf - -C каталог`, `tar x`)."""
    if "--version" in args or "--help" in args:
        return None
    modes = set()
    farg = None
    k = 0
    n = len(args)
    while k < n:
        a = args[k]
        k += 1
        if a.startswith("--"):
            nm, eq, val = a.partition("=")
            if nm in TAR_LONG:
                modes.add(TAR_LONG[nm])
            elif nm == "--file":
                farg = val if eq else (args[k] if k < n else None)
                k += 0 if eq else 1
            elif nm in ("--directory", "--files-from", "--exclude-from", "--exclude", "--transform", "--to-command") and not eq:
                k += 1
            continue
        old = k == 1 and not a.startswith("-")               # `tar xzf - …` — буквы без дефиса, значения — следующие токены
        letters = a if old else a[1:]
        for idx, ch in enumerate(letters):
            if ch in TAR_VALUE_LETTERS:
                rest = "" if old else letters[idx + 1:]
                if rest:
                    val = rest
                else:
                    val = args[k] if k < n else None
                    k += 1
                if ch == "f":
                    farg = val
                if not old:
                    break
            elif ch in TAR_MODES:
                modes.add(TAR_MODES[ch])
    if modes == {"x"} and farg in (None, "-"):
        return None
    return "tar " + ("".join(sorted(modes)) or "?") + (f" -f {farg}" if farg not in (None, "-") else "")


def heavy_label(name, args, stdin=(), xargs=False):
    """Тяжёлая ли команда `name args…` на сервере счёта вне замка: метка или None. `name` — имя команды строчными без пути;
    `args` — слова после неё (переменные, известные в той же команде, уже подставлены); `stdin` — тексты, которые команда
    читает со stdin (heredoc, here-string, строка конвейера); `xargs` — цели приходят из stdin xargs."""
    if HEAVY_ALLOW and HEAVY_ALLOW.search(" ".join([name, *args])):
        return None
    if name in ALWAYS:
        return name
    short, long_, pos = opts(args)
    if name == "ls":
        return "ls -R" if ("R" in short or "--recursive" in long_) else None
    if name in GREPS:
        return f"{name} -r" if ({"r", "R"} & set(short) or {"--recursive", "--dereference-recursive"} & long_) else None
    if name == "cp":
        if {"r", "R", "a"} & set(short) or {"--recursive", "--archive"} & long_:
            return "cp -r"
        return "cp бинлога" if any(BIG_FILE.search(p) for p in pos) else None
    if name == "tar":
        return tar_label(args)
    if name in HASH:
        if xargs:
            return f"{name} по списку из xargs"
        if name.endswith("sum") and ("c" in short or "--check" in long_):
            return f"{name} -c (список файлов из файла)"
        for p in pos:
            if p != "-" and (unknown_path(p) or BIG_FILE.search(p)):
                return f"{name} {p}"
        return None
    if name == "diff":
        if "r" in short or "R" in short or "--recursive" in long_:
            return "diff -r"
        return "diff бинлога" if any(BIG_FILE.search(p) for p in pos) else None
    if name == "cat":
        for p in pos:
            if BIG_FILE.search(p) or (p.startswith("/data/alpha/") and unknown_path(p)):
                return f"cat {p}"
        return None
    if PY_NAMES.match(name):
        return python_label(args, stdin)
    return None


LEGACY_HEAVY = re.compile(r"(?:^|[\s;&|(`'\"])(?:du|find|vmtouch|rsync|tar)\s|\bpython3?(?:\.\d+)?\s+(?:-\w+\s+)*[^\s-]\S*\.py\b")


def legacy_heavy(cmd):
    """Страховка на случай, когда разбор не удался (незакрытая кавычка): грубый поиск в тексте команды с этим хостом."""
    if not HEAVY_HOST or HEAVY_HOST not in cmd or "systemd-run" in cmd or "benchrun" in cmd:
        return None
    m = LEGACY_HEAVY.search(cmd)
    return m.group(0).strip(" ;&|(`'\"\t\n") if m else None
