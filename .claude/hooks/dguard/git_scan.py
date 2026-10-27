"""Команды git: необратимые (`reset --hard`, `clean`, `push --force`), удаление веток, публикация пушем."""
import json
import os
import posixpath
import re
from dguard import config
from dguard.config import norm
from dguard.messages import BranchDelete, Forbid, Publish
from dguard.paths import allowed, CLOSED_HOST, closed_host, Ctx
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
    """`git reset --hard` / `git clean -f` не трогают основное дерево: каталог репозитория известен явным путём (не
    переменная, не относительный без cwd) и лежит вне папки проекта — scratch (scratchpad сессии, свои удалённые корни
    вроде <подкаталог из guard_remote_roots>), `/tmp/<подкаталог>` либо связанный worktree внутри папки проекта
    (`.git` — файл). Защищённый путь проекта (или настройка защиты не прочитана) — нет."""
    p = norm(d) if d else ""
    if not p or "$" in p or ".." in p.split("/") or closed_host(p):
        return False
    host = getattr(ctx, "host", None)
    if host == CLOSED_HOST or closed_host(host):                     # git на закрытом узле — как и удаление там
        return False
    if not (p.startswith(("/", "~")) or re.match(r"[a-z]:", p)):
        return False
    if config.PROTECTED_ERROR or (config.PROTECTED and protected_match(p, ctx, True)):
        return False
    probe = p.rstrip("/") + "/"
    if any(probe.startswith(r) for r in config.LOCAL_ROOTS):                # папка проекта: только связанный worktree
        return not getattr(ctx, "remote", False) and linked_worktree(p)
    if re.match(r"^/tmp/[^/*]+", p):
        return True
    return allowed(p, Ctx(None, getattr(ctx, "remote", False), host=host))


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
    if sub == "checkout":
        return checkout_discard(rest, short, long_, ctx, repo)
    return {"branch": branch_discard, "restore": restore_discard, "switch": switch_discard}[sub](short, long_)


def branch_discard(short, long_):
    if "--force" in long_ or any(c for c in short if any(ch in c for ch in "DMCf")):
        return "git branch -D/-M/-C/-f (удаление или перезапись ветки)"
    return None


def restore_discard(short, long_):
    staged = "--staged" in long_ or any("S" in c for c in short)
    worktree = "--worktree" in long_ or any("W" in c for c in short)
    return None if staged and not worktree else "git restore (затирает файлы рабочего дерева)"


def switch_discard(short, long_):
    if "--discard-changes" in long_ or "--force" in long_ or any("f" in c for c in short):
        return "git switch -f (затирает правки рабочего дерева)"
    return None


def checkout_pathspecs(rest):
    """Позиционные слова `git checkout` без значений `-b/-B/--orphan/--conflict`."""
    pos = []
    k = 0
    while k < len(rest):
        a = rest[k]
        k += 1
        if a in ("-b", "-B", "--orphan", "--conflict"):
            k += 1                                       # значение: имя новой ветки
        elif not a.startswith("-"):
            pos.append(a)
    return pos


def checkout_discard(rest, short, long_, ctx, repo):
    if "--" in rest:
        return "git checkout -- <путь> (затирает файлы рабочего дерева)"
    if long_ & {"--force", "--patch", "--ours", "--theirs", "--pathspec-from-file"} or any(
            "f" in c or "p" in c for c in short):
        return "git checkout -f/-p/--ours/--theirs (затирает файлы рабочего дерева)"
    pos = checkout_pathspecs(rest)
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


DEFAULT_BRANCHES = ("main", "master")


def delete_branch_names(rest, pos):
    """Ветки, которые удаляет `git push <удалённый> --delete <ветка>…` или `<удалённый> :<ветка>…`. Не определить (маска,
    переменная, пустое имя, обычный пуш рядом с удалением, удалённый не назван) — None: команда остаётся запрещённой."""
    flagged = any(a == "--delete" or (a.startswith("-") and not a.startswith("--") and "d" in a[1:]) for a in rest)
    names = []
    for a in pos[1:]:                                   # pos[0] — удалённый репозиторий
        if not flagged and not a.startswith(":"):
            return None
        name = (a[1:] if a.startswith(":") else a).removeprefix("refs/heads/")
        if not name or name.startswith(("-", "refs/")) or ".." in name or any(c in name for c in "*?[]{}\\$`'\" \t"):
            return None
        names.append(name)
    return tuple(names) or None


CONFIG_LOG_NAMES = ("config-log.json",)               # журнал правок настроек (TK-007): пишет только `tickets.py config`
BRANCH_CONSENT_NAMES = ("branch-delete.json",)       # согласия на удаление веток: пишет только `tickets.py allow-delete`


def branch_delete_consent(branches):
    """Согласие владельца на удаление веток → (есть, пояснение для отказа). Файл `<проект>/.claude/dispatcher/branch-delete.json`:
    {ветка: {"ref": "В-N", "until": время}} (ставит `tickets.py allow-delete`, только CEO). Нужно на КАЖДУЮ ветку; main/master —
    никогда; срок вышел, файла нет, не читается — согласия нет (fail-closed)."""
    from datetime import datetime
    try:
        with open(os.path.join(config.PROJECT_DIR, ".claude", "dispatcher", "branch-delete.json"), encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return False, "файл согласий не читается (не объект JSON)"
        now = datetime.now().astimezone()
        bad = []
        for b in branches:
            e = data.get(b)
            until = datetime.fromisoformat(str(e.get("until", ""))).astimezone() if isinstance(e, dict) and e.get("until") else None
            if b in DEFAULT_BRANCHES or not (isinstance(e, dict) and re.fullmatch(r"В-\d+", str(e.get("ref", ""))) and until and until > now):
                bad.append(b)
        if bad:
            return False, "нет действующего согласия владельца на ветки: " + ", ".join(bad) + (
                " (main/master удалять нельзя)" if any(b in DEFAULT_BRANCHES for b in bad) else "")
        return True, ""
    except (OSError, ValueError) as e:                  # ValueError — и битый JSON, и время
        return False, f"согласия не прочитаны ({type(e).__name__})"
PUSH_VALUE_OPTS = {"-o", "--push-option", "--repo", "--receive-pack", "--exec"}
TAG_NAME = re.compile(r"^v?\d+\.\d+(?:\.\d+)*[-+.\w]*$")        # `v1.8.48`, `2.0.0-rc1`: без точки (`v2`) — ветка


def head_branch(repo, ctx):
    """Ветка, на которой стоит репозиторий каталога `repo`, по `.git/HEAD` (у связанного worktree `.git` — файл со ссылкой
    на gitdir); None — не определить: удалённая машина, неизвестный каталог, отсоединённая HEAD."""
    cur = (repo or "").rstrip("/")
    if getattr(ctx, "remote", False):
        return None
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


def push_publication(rest, repo, ctx):
    """Метка публикации у `git push` — основная ветка (main/master) в любой записи refspec (`HEAD:main`, `refs/heads/main`,
    `x:main`), тег (`--tags`, `--follow-tags`, `refs/tags/…`, `tag <имя>`, `v1.2.3`), все ветки (`--all`, шаблон `*`); пуш без
    ветки (`git push`, `origin HEAD`) — по текущей ветке репозитория, не определена — тоже публикация. Рабочая ветка и
    `--dry-run` — None."""
    if any(a in ("--dry-run", "-n") for a in rest):
        return None
    flag = next((a for a in rest if a in ("--tags", "--all", "--branches") or a.startswith("--follow-tags")), None)
    if flag:
        return f"git push {flag}"
    pos, k = [], 0
    while k < len(rest):
        a = rest[k]
        k += 1
        if a in PUSH_VALUE_OPTS:
            k += 1
        elif not a.startswith("-"):
            pos.append(a)
    specs = pos if any(a == "--repo" or a.startswith("--repo=") for a in rest) else pos[1:]    # первое слово — удалённый репозиторий
    if specs[:1] == ["tag"]:
        return "git push tag: тег"
    for spec in specs:
        src, sep, dst = spec.partition(":")
        ref = dst if sep and dst else src
        name = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref
        if ref.startswith("refs/tags/") or TAG_NAME.match(ref):
            return f"git push {spec}: тег"
        if name in DEFAULT_BRANCHES:
            return f"git push {spec}: основная ветка"
        if "*" in ref:
            return f"git push {spec}: все ветки по шаблону"
    if not specs or "HEAD" in specs or "@" in specs:                  # ветка не названа: уйдёт текущая
        cur = head_branch(repo, ctx)
        if cur in DEFAULT_BRANCHES:
            return f"git push без ветки: текущая — {cur}"
        if cur is None:
            return "git push без ветки: текущую не определить"
    return None


def scratch_ok(exotic, repo, ctx):
    return not exotic and scratch_repo_ok(repo, ctx)


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
        label = "git push --delete/--mirror/:ветка (удаление веток на удалённом)"
        names = None if any(a in ("--mirror", "--prune") for a in rest) else delete_branch_names(rest, pos)
        yield (BranchDelete(label, names) if names else Forbid(label), ctx.copy())
    elif label := push_publication(rest, repo, ctx):
        yield (Publish(label), ctx.copy())


def clean_scan(rest, base, exotic, repo, ctx):
    """`git clean`: с `-f` — запрет вне черновых копий; без `-f` — чистит указанные пути (или каталог `-C`)."""
    if any(a == "--dry-run" or (a.startswith("-") and not a.startswith("--") and "n" in a) for a in rest):
        return
    if any(a == "--force" or (a.startswith("-") and not a.startswith("--") and "f" in a[1:]) for a in rest):
        if not scratch_ok(exotic, repo, ctx):
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
    if sub == "reset":
        if "--hard" in rest and not scratch_ok(exotic, repo, ctx):
            yield (Forbid("git reset --hard"), ctx.copy())
    elif sub == "push":
        yield from push_scan(rest, repo, ctx)
    elif sub in ("checkout", "switch", "restore", "branch", "stash"):
        label = git_discard(sub, rest, ctx, repo)
        if label and not scratch_ok(exotic, repo, ctx):
            yield (Forbid(label), ctx.copy())
    elif sub == "clean":
        yield from clean_scan(rest, base, exotic, repo, ctx)
