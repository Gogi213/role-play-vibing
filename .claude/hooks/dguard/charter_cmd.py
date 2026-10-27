"""Сессия CEO пишет в устав `.claude/roles/*.md` через оболочку (TK-029, В-12) — отказ; перенос из шаблонов — `tickets.py charter sync`.

Смотрим на цель записи, а не на любое упоминание устава: `cp .claude/roles/judge.md /tmp/x` — чтение. Каталог, в который перешли
(`cd .claude/roles && cp x judge.md`), учитывается; копирование каталога поверх `.claude` (`cp -r … .claude/`) — тоже запись."""
import posixpath
import re
from dguard import config
from dguard.ceo_signal import REDIRECT, command_text, shell_writes
from dguard.paths import Ctx, under_cwd
from dguard.tools import SHELL_TOOLS
from dguard.words import join_cwd
from dguard.writes import may_exist

ROLES = ".claude/roles"
ADDON = "project"                                                            # добавки проекта `roles/project/<роль>.md` (TK-038)
WORD = re.compile(r"""\"[^\"]*\"|'[^']*'|\S+""")
CD_NAMES = frozenset({"cd", "pushd", "chdir", "set-location", "sl", "popd"})   # как в dguard.cmd_scan
TOKEN = re.compile(r"""[^\s"'`=<>|;&()]*\.claude/roles(?:/[^\s"'`<>|;&()]*)?""", re.I)
COPY_TO = frozenset({"cp", "mv", "install", "ln", "rsync", "copy", "move", "copy-item", "move-item", "rename-item"})  # назначение — последний путь
ALL_ARGS = frozenset({"tee", "rm", "truncate", "set-content", "add-content", "out-file", "clear-content", "remove-item", "new-item"})
DIRY_SOURCE = re.compile(r"(?:^|/)(?:roles|\.claude|\.|\.\.)/?$|[*?\[]")   # источник, под которым может лежать каталог уставов


def _words(sub):
    return [w.strip("'\"") for w in WORD.findall(sub.replace("\\", "/"))]


def _abs(tok, here):
    """Путь цели: относительный — от каталога `here` (если известен); с переменной в начале — как есть."""
    p = tok.strip().strip("'\"").replace("\\", "/")
    if not p or p.startswith(("/", "~", "$", "%")) or re.match(r"[A-Za-z]:", p) or not here:
        return p
    return under_cwd(here, p) + ("/" if p.endswith("/") else "")


def _cd(here, words):
    """Рабочий каталог после `cd`/`pushd` (общий `join_cwd` стража); неизвестный → None. `popd` каталог не меняет: стек не ведётся,
    а `cd .claude/roles; popd` при пустом стеке остаётся в уставах — осторожная сторона. Путь с неизвестной переменной, но
    с каталогом уставов в хвосте (`cd $ROOT/.claude/roles`), остаётся буквальным."""
    if words[0].lower() == "popd":
        return here
    pos = [w for w in words[1:] if not w.startswith("-") or w == "-"]
    target = pos[0] if pos else None
    new = join_cwd(here, target, {})
    if new is None and target and ROLES in target.replace("\\", "/").lower():
        return _abs(target, here).rstrip("/")
    return new


def _charter_path(p, ctx):
    """Путь — устав: каталог уставов, маска или существующий `*.md` прямо в нём. Блокнот, журнал, `.state`, новый файл (его создаёт
    первый `/rpv-init`: `roles/$b`) и переменная в имени файла — нет."""
    i = p.lower().find(ROLES)
    if i < 0:
        return False
    tail = p[i + len(ROLES):].strip("/")
    if not tail:
        return True
    if "$" in tail or "%" in tail:
        return False
    if tail.lower().startswith(ADDON + "/"):                                 # добавка проекта: и новый файл — только командой
        tail = tail[len(ADDON) + 1:]
        return "/" not in tail and (tail.lower().endswith(".md") or any(c in tail for c in "*?["))
    if "/" in tail:
        return False
    if any(c in tail for c in "*?["):
        return True
    return tail.lower().endswith(".md") and may_exist(p, ctx)


def _tree_over_claude(dest, sources, recursive, contents):
    """Копирование каталога поверх `.claude` проекта (затирает `roles/`): назначение — сам `.claude`, источник — `roles`, `.`, маска (rsync: любой каталог со `/` на конце);
    или источник `.claude`, а назначение — корень проекта."""
    d = dest.lower().rstrip("/")
    if recursive and (d == ".claude" or d.endswith("/.claude")) and any(DIRY_SOURCE.search(s) or (contents and s.endswith("/")) for s in sources):
        return True
    root = config.PROJECT_DIR.replace("\\", "/").lower().rstrip("/")
    return recursive and d == root and any(s.rstrip("/").endswith(".claude") for s in sources)


def _copy_targets(name, words):
    """→ ([назначения], источники, рекурсивно): `-t КАТАЛОГ` / `--target-directory=` / `of=` / последний путь без флагов."""
    flags = [w for w in words if w.startswith("-") and len(w) > 1]
    pos, to, skip = [], None, False
    for k, w in enumerate(words):
        if skip:
            skip = False
        elif w.lower() in ("-t", "--target-directory", "-destination") and k + 1 < len(words):
            to, skip = words[k + 1], True
        elif w.startswith("--target-directory="):
            to = w.split("=", 1)[1]
        elif w.startswith("of="):
            to = w[3:]
        elif not w.startswith("-") and not w.startswith("if="):
            pos.append(w)
    recursive = name in ("mv", "rsync") or any(re.fullmatch(r"-[a-zA-Z]*[rRa][a-zA-Z]*|--recursive|-recurse", f, re.I) for f in flags)
    if to is not None:
        return [to], pos, recursive
    return pos[-1:], pos[:-1], recursive


def _sub_hit(sub, here, ctx):
    """Путь, в который пишет простая команда (без `|`), если он устав."""
    words = _words(sub)
    while words and re.fullmatch(r"\w+=\S*|sudo|command|env|time|nohup", words[0]):
        words = words[1:]
    name = re.sub(r"\.exe$", "", posixpath.basename(words[0]).lower()) if words else ""
    args = words[1:]
    targets, structured = [], False
    if name in COPY_TO or name == "dd":
        dest, sources, recursive = _copy_targets(name, args)
        for d in dest:
            p = _abs(d, here)
            if _charter_path(p, ctx) or _tree_over_claude(p, sources, recursive, name == "rsync"):
                return d
        for src in sources if name in ("mv", "move", "move-item") else ():      # перенос уносит устав из каталога
            if _charter_path(_abs(src, here), ctx):
                return src
        structured = True
    elif name in ALL_ARGS or (name in ("sed", "perl") and any(re.fullmatch(r"-[a-zA-Z]*i[a-zA-Z.]*", a) for a in args)):
        targets, structured = [a for a in args if not a.startswith("-")], True
    for m in REDIRECT.finditer(sub):                                         # `echo x > устав`, `>>`
        targets.append(m.group(1))
    for t in targets:
        if _charter_path(_abs(t, here), ctx):
            return t
    if structured:
        return None
    text = sub.replace("\\", "/")                                            # интерпретатор и прочее: путь — в тексте команды
    for token in TOKEN.findall(text) + (re.findall(r"[\w.\-]+\.md\b", text) if ctx.cwd and ROLES in ctx.cwd.lower() else []):
        p = _abs(token, here)
        if _charter_path(p, ctx) and shell_writes(sub, (token.lower(),)):
            return token
    return None


def charter_shell_write(tool, ti, cwd):
    """Сессия CEO пишет в устав `.claude/roles/*.md` через оболочку (`cp`, `mv`, `tee`, `>`, `python`, `sed -i`, `install`, `rsync`…) →
    название пути или None. Чтение и `--text`/`--why` у tickets.py не в счёт."""
    if tool not in SHELL_TOOLS:
        return None
    here = str(cwd).replace("\\", "/").rstrip("/") if cwd else None
    for part in re.split(r"\n|&&|\|\||;", command_text(ti, benign=True)):
        words = _words(part)
        if words and words[0].lower() in CD_NAMES:
            here = _cd(here, words)
            continue
        ctx = Ctx(here, False)
        for sub in part.split("|"):
            if hit := _sub_hit(sub, here, ctx):
                return hit
    return None
