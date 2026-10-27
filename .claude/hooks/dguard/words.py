"""Слова простой команды: переменные, обёртки (sudo/env/xargs/…), хост и путь, код `bash -c` / `powershell -Command`."""
import base64
import posixpath
import re
from dguard.config import norm
from dguard.paths import temp_rest, under_cwd


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
               "--service-type"}      # `-G` у systemd-run — `--collect`, без значения (съедал команду: `systemd-run -G rm …` проходил)
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
    return under_cwd(cwd, t)


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
