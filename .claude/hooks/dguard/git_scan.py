"""Команды git: необратимые (`reset --hard`, `clean -f`, откат всего дерева, `push --force`, удаление main/master и веток
на удалённом) и публикация пушем. Откат одного файла и удаление локальной ветки — свободно."""
import os
import posixpath
import re
from dguard import config
from dguard.config import norm
from dguard.messages import Forbid, Publish
from dguard.paths import allowed, Ctx
from dguard.policy import protected_match
from dguard.words import join_cwd
from dguard.writes import fs_path

def linked_worktree(p):
    """Каталог внутри связанного git-worktree в папке проекта (`.claude/worktrees/agent-*`): ближайший `.git` вверх по
    пути — файл (у основного дерева и вложенных репозиториев это каталог). Только локально, по файловой системе."""
    cur = p.rstrip("/")
    for _ in range(40):
        if cur + "/" in config.LOCAL_ROOTS:
            return False
        fp = fs_path(cur + "/.git", None)
        if fp is None:
            return False
        if os.path.isfile(fp):
            return True
        if os.path.isdir(fp):
            # TK-070 п.5: отдельный клон (свой `.git`-каталог) внутри `.claude/worktrees/<имя>` — тоже рабочая копия вне основного дерева
            return bool(re.search(r"/\.claude/worktrees/[^/]+$", cur))
        parent = posixpath.dirname(cur)
        if parent == cur:
            return False
        cur = parent
    return False


def scratch_repo_ok(d, ctx):
    """`git reset --hard` / `git clean -f` / откат всего дерева не трогают основную копию: каталог репозитория известен явным
    путём (не переменная, не относительный без cwd) и лежит вне папки проекта — scratch (scratchpad сессии, `/tmp/<подкаталог>`)
    либо связанный worktree внутри папки проекта (`.git` — файл). Защищённый путь проекта (или настройка защиты не
    прочитана) — нет."""
    p = norm(d) if d else ""
    if not p or "$" in p or ".." in p.split("/"):
        return False
    if not (p.startswith(("/", "~")) or re.match(r"[a-z]:", p)):
        return False
    if config.PROTECTED_ERROR or (config.PROTECTED and protected_match(p, ctx, True)):
        return False
    probe = p.rstrip("/") + "/"
    if any(probe.startswith(r) for r in config.LOCAL_ROOTS):                # папка проекта: только связанный worktree
        return linked_worktree(p)
    if re.match(r"^/tmp/[^/*]+", p):
        return True
    return allowed(p, Ctx(None))


DEFAULT_BRANCHES = ("main", "master")
WHOLE_TREE = ("", ".", "./", "..", ":/", ":(top)", ":/.")


def whole_tree(paths):
    """Путь отката — всё дерево (`.`, `:/`) или маска (`*.py`, `src/*`): откат больше одного файла."""
    return any(p in WHOLE_TREE or any(c in p for c in "*?[") for p in paths)


def branch_deletes_default(rest):
    """`git branch -D|-d --force <ветка>…` называет main/master."""
    short = [a[1:] for a in rest if a.startswith("-") and not a.startswith("--") and len(a) > 1]
    force = "--force" in rest or any("f" in c for c in short)
    if not (any("D" in c for c in short) or (force and any("d" in c for c in short)) or "--delete" in rest):
        return False
    return any(a.removeprefix("refs/heads/") in DEFAULT_BRANCHES for a in rest if not a.startswith("-"))


def discard_label(sub, rest):
    """Метка подкоманды git, которая затирает всё рабочее дерево или удаляет main/master, либо None. Откат одного файла
    (`checkout -- файл`, `restore файл`) и удаление локальной ветки — не метка."""
    if any(a in ("--help", "-h") for a in rest):
        return None
    short = [a[1:] for a in rest if a.startswith("-") and not a.startswith("--") and len(a) > 1]
    long_ = {a.split("=", 1)[0] for a in rest if a.startswith("--")}
    pos = []
    k = 0
    while k < len(rest):
        a = rest[k]
        k += 1
        if a in ("-b", "-B", "--orphan", "--conflict", "-s", "--source"):
            k += 1                                       # значение: ветка или ревизия
        elif a == "--":
            pos += rest[k:]
            break
        elif not a.startswith("-"):
            pos.append(a)
    if sub == "checkout" and (long_ & {"--force"} or any("f" in c for c in short)):
        return "git checkout -f (затирает правки рабочего дерева)"
    if sub == "switch" and (long_ & {"--force", "--discard-changes"} or any("f" in c for c in short)):
        return "git switch -f (затирает правки рабочего дерева)"
    if sub == "checkout":
        paths = rest[rest.index("--") + 1:] if "--" in rest else (pos if len(pos) == 1 else pos[1:])
        return "git checkout <ревизия> . (затирает всё рабочее дерево)" if whole_tree(paths) else None
    if sub == "restore":
        staged_only = ("--staged" in long_ or any("S" in c for c in short)) and not ("--worktree" in long_ or any("W" in c for c in short))
        return None if staged_only or not whole_tree(pos) else "git restore . (затирает всё рабочее дерево)"
    return None


TAG_NAME = re.compile(r"^v?\d+\.\d+(?:\.\d+)*[-+.\w]*$")        # `v1.8.48`, `2.0.0-rc1`: без точки (`v2`) — ветка
PUSH_VALUE_OPTS = {"-o", "--push-option", "--repo", "--receive-pack", "--exec"}


def head_branch(repo):
    """Ветка, на которой стоит репозиторий каталога `repo`, по `.git/HEAD` (у связанного worktree `.git` — файл со ссылкой
    на gitdir); None — не определить: неизвестный каталог, отсоединённая HEAD."""
    cur = (repo or "").rstrip("/")
    for _ in range(40):
        fp = fs_path(cur + "/.git", None) if cur else None
        if fp is None:
            return None
        try:
            if os.path.isfile(fp):
                with open(fp, encoding="utf-8") as f:
                    m = re.match(r"gitdir:\s*(.+)", f.read().strip())
                if not m:
                    return None
                fp = os.path.join(cur, m.group(1).strip())                  # относительный gitdir — от каталога worktree
            if os.path.isdir(fp):
                with open(os.path.join(fp, "HEAD"), encoding="utf-8") as f:
                    m = re.match(r"ref:\s*refs/heads/(\S+)", f.read().strip())
                return m.group(1) if m else None
        except (OSError, ValueError):
            return None
        parent = posixpath.dirname(cur)
        if parent == cur:
            return None
        cur = parent
    return None


def push_words(rest):
    """Позиционные слова `git push` без значений опций и значение `--repo`: (слова, удалённый из `--repo` или None)."""
    pos, repo_opt, k = [], None, 0
    while k < len(rest):
        a = rest[k]
        k += 1
        if a.startswith("--repo="):
            repo_opt = a.partition("=")[2]
        elif a in PUSH_VALUE_OPTS:
            if a == "--repo" and k < len(rest):
                repo_opt = rest[k]
            k += 1
        elif not a.startswith("-"):
            pos.append(a)
    return pos, repo_opt


def refspec_publication(spec):
    """Одна запись refspec (`src`, `src:dst`) → метка публикации (тег, main/master, шаблон) либо None."""
    src, sep, dst = spec.partition(":")
    ref = dst if sep and dst else src
    name = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref
    if ref.startswith("refs/tags/") or TAG_NAME.match(ref):
        return f"git push {spec}: тег"
    if name in DEFAULT_BRANCHES:
        return f"git push {spec}: основная ветка"
    return f"git push {spec}: все ветки по шаблону" if "*" in ref else None


def push_publication(rest, repo):
    """Публикация `git push` (`Publish`) либо None: main/master в любой записи refspec, тег (`--tags`, `--follow-tags`,
    `refs/tags/…`, `tag <имя>`, `v1.2.3`), все ветки (`--all`, шаблон `*`); пуш без ветки (`git push`, `origin HEAD`) — по текущей
    ветке репозитория, не определена — тоже публикация. Рабочая ветка и `--dry-run` — None."""
    if any(a in ("--dry-run", "-n") for a in rest):
        return None
    pos, repo_opt = push_words(rest)
    specs = pos if repo_opt is not None else pos[1:]                                  # первое слово — удалённый репозиторий
    flag = next((a for a in rest if a in ("--tags", "--all", "--branches") or a.startswith("--follow-tags")), None)
    label = f"git push {flag}" if flag else None
    if specs[:1] == ["tag"]:
        label = label or "git push tag: тег"
        specs = []
    for spec in specs:
        label = label or refspec_publication(spec)
    if not label and (not specs or "HEAD" in specs or "@" in specs):     # ветка не названа: уйдёт текущая
        cur = head_branch(repo)
        label = f"git push без ветки: текущая — {cur}" if cur in DEFAULT_BRANCHES else (
            "git push без ветки: текущую не определить" if cur is None else None)
    return Publish(label) if label else None


def git_options(args, vars_, redirected):
    """Глобальные опции до подкоманды: (индекс подкоманды, последний `-C`, все `-C`, нестандартные каталоги репозитория)."""
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
    return j, base, bases, exotic


def push_scan(rest, repo, ctx):
    pos = [a for a in rest if not a.startswith("-")]
    if any(a == "--force" or a.startswith("--force-with-lease")
           or (a.startswith("-") and not a.startswith("--") and "f" in a[1:]) for a in rest):
        yield (Forbid("git push --force"), ctx.copy())
    elif any(a.startswith("+") for a in pos):
        yield (Forbid("git push +ветка (принудительное обновление)"), ctx.copy())
    elif (any(a in ("--delete", "--mirror", "--prune") for a in rest) or any(a.startswith(":") for a in pos)
          or any(a.startswith("-") and not a.startswith("--") and "d" in a[1:] for a in rest)):
        yield (Forbid("git push --delete/--mirror/:ветка (удаление веток на удалённом)"), ctx.copy())
    elif publication := push_publication(rest, repo):
        yield (publication, ctx.copy())


def clean_scan(rest, base, scratch, ctx):
    """`git clean`: с `-f` — запрет в основной копии; без `-f` — чистит указанные пути (или каталог `-C`)."""
    if any(a == "--dry-run" or (a.startswith("-") and not a.startswith("--") and "n" in a) for a in rest):
        return
    if any(a == "--force" or (a.startswith("-") and not a.startswith("--") and "f" in a[1:]) for a in rest):
        if not scratch:
            yield (Forbid("git clean -f"), ctx.copy())
        return
    after = rest[rest.index("--") + 1:] if "--" in rest else []
    specs = after or [a for a in rest if not a.startswith("-")]
    b = base or "."
    for sp in (specs or [None]):
        yield ((b if sp is None else posixpath.join(b, sp)), ctx.copy())


def git_scan(args, ctx, vars_, redirected=False):
    """`redirected` — GIT_DIR/GIT_WORK_TREE в окружении команды: каталог репозитория не равен рабочему."""
    j, base, bases, exotic = git_options(args, vars_, redirected)
    if j >= len(args):
        return
    sub, rest = args[j], args[j + 1:]
    repo = None if exotic else ctx.cwd                    # каталог репозитория: cwd, сдвинутый цепочкой `-C`
    for b in bases:                                       # абсолютный `-C` задаёт каталог и при неизвестном cwd
        repo = None if exotic else join_cwd(repo, b, vars_)
    scratch = not exotic and scratch_repo_ok(repo, ctx)
    if sub == "reset":
        if "--hard" in rest and not scratch:
            yield (Forbid("git reset --hard"), ctx.copy())
    elif sub == "push":
        yield from push_scan(rest, repo, ctx)
    elif sub == "branch":
        if branch_deletes_default(rest):
            yield (Forbid("git branch -D main|master"), ctx.copy())
    elif sub in ("checkout", "switch", "restore"):
        label = discard_label(sub, rest)
        if label and not scratch:
            yield (Forbid(label), ctx.copy())
    elif sub == "clean":
        yield from clean_scan(rest, base, scratch, ctx)
