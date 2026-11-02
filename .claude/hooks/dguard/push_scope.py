"""Пуш в main/master уносит только коммиты принятого тикета (TK-040, П4 обратной связи invoice). Включает `guard_push_scope`."""
import os
import posixpath
import re
import subprocess
from dguard import config
from dguard.config import _env
from dguard.messages import REASON_PUSH_SCOPE, REASON_PUSH_SCOPE_UNKNOWN
from dguard.writes import fs_path

GIT_TIMEOUT_S = 20
SHOW_FOREIGN = 10                                                 # сколько чужих коммитов назвать в отказе
ACCEPTED_SHA = re.compile(r"@([0-9a-fA-F]{7,40})\b")              # `accepted: 22@sha, 23@sha`
REMOTE_NAME = re.compile(r"[\w.-]+")


class Unknown(Exception):
    """Коммиты не сверить: причина — в тексте отказа (fail-closed)."""


def real_dir(path):
    """Каталог на диске по пути стража (он строчный): как есть, иначе по частям с поиском имени без учёта регистра."""
    fp = fs_path(path, None) if path else None
    if fp is None:
        raise Unknown("каталог репозитория не определить (переменная, `GIT_DIR` или неизвестный cwd)")
    if os.path.isdir(fp):
        return fp
    cur = fp[:3] if re.match(r"^[A-Za-z]:/", fp) else "/"
    for seg in [s for s in fp[len(cur):].split("/") if s]:
        nxt = posixpath.join(cur, seg)
        if not os.path.isdir(nxt):
            try:
                nxt = next(posixpath.join(cur, n) for n in os.listdir(cur) if n.lower() == seg.lower())
            except (OSError, StopIteration):
                raise Unknown(f"каталог репозитория {path} не найден") from None
        cur = nxt
    return cur


def git(repo, *args):
    """Вывод `git -C repo …` (удалённых обращений нет) либо None, если команда не удалась."""
    env = {k: v for k, v in os.environ.items() if k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")}
    try:
        r = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=GIT_TIMEOUT_S, env=env)
    except (OSError, subprocess.SubprocessError) as e:
        raise Unknown(f"git не запустился: {type(e).__name__}") from None
    return r.stdout if r.returncode == 0 else None


def resolve(repo, ref):
    """Полный sha коммита по ссылке либо None."""
    out = git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    return out.strip() if out and out.strip() else None


def ticket_accepted(path):
    """sha из строки `accepted:` шапки тикета (последняя строка ключа решает, как в разборе шапки). Нет файла — пусто."""
    try:
        with open(path, encoding="utf-8") as f:
            if f.readline().strip() != "---":
                return set()
            value = ""
            for line in f:
                key, _, val = line.partition(":")
                if line.strip() == "---":
                    break
                if key.strip() == "accepted":
                    value = val
    except (OSError, ValueError):
        return set()
    return {m.lower() for m in ACCEPTED_SHA.findall(value)}


def accepted_shas(ticket):
    """Принятые головы PR: тикета запуска роли (`ticket`) либо — сессия CEO (None) — всех тикетов проекта."""
    folder = os.path.join(config.PROJECT_DIR, ".claude", "tickets")
    if ticket:
        names = [ticket + ".md"]
    else:
        try:
            names = sorted(n for n in os.listdir(folder) if n.endswith(".md"))
        except OSError:
            names = []
    found = set()
    for n in names:
        found |= ticket_accepted(os.path.join(folder, n))
    return found


def own_commits(repo, tracking, ticket):
    """Коммиты принятых голов, которых ещё нет в `tracking`: их и только их можно отдавать в main."""
    heads = [h for h in (resolve(repo, s) for s in sorted(accepted_shas(ticket))) if h]
    if not heads:
        return set()
    out = git(repo, "rev-list", *heads, f"^{tracking}")
    return set(out.split()) if out else set()


def foreign_commits(repo, remote, src, dst, optional, ticket):
    """Коммиты `<удалённый>/<dst>..<src>`, которых нет у принятых голов тикета, — список (sha, тема); слияние принятых голов
    (merge-коммит, у которого все родители кроме первого — принятые) свой. Ветки назначения или источника нет — Unknown
    (необязательная пара — пусто)."""
    tip = resolve(repo, src)
    if tip is None:
        if optional:
            return []
        raise Unknown(f"ссылки {src} в репозитории нет")
    tracking = f"refs/remotes/{remote}/{dst}"
    if resolve(repo, tracking) is None:
        raise Unknown(f"нет {remote}/{dst} в репозитории: сначала `git fetch {remote}`")
    own = own_commits(repo, tracking, ticket)
    out = git(repo, "log", "--topo-order", "--reverse", "--format=%H%x09%P%x09%s", f"{tracking}..{tip}", "--")
    if out is None:
        raise Unknown(f"`git log {remote}/{dst}..{src}` не удался")
    foreign = []
    for line in out.splitlines():
        sha, _, rest = line.partition("\t")
        parents, _, subject = rest.partition("\t")
        parents = parents.split()
        if sha in own or (len(parents) > 1 and all(p in own for p in parents[1:])):
            own.add(sha)
        else:
            foreign.append((sha, subject))
    return foreign


def push_scope_reason(publication, remote_host, role):
    """Причина отказа пушу в main/master с коммитами вне принятого тикета либо None (настройка `guard_push_scope` выключена,
    пуш не в main/master, чужих коммитов нет). Запуску роли — коммиты её тикета (`RPV_TICKET`), сессии CEO — любого принятого."""
    refs = getattr(publication, "main", None)
    if not (config.PUSH_SCOPE and refs):
        return None
    ticket = _env("TICKET").strip() if role else None
    who = ticket or "любого принятого тикета"
    try:
        if remote_host:
            raise Unknown("пуш с удалённой машины: коммиты там не сверить")
        if role and not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", ticket):
            raise Unknown("тикет запуска RPV_TICKET не определён")
        if not REMOTE_NAME.fullmatch(refs.remote):
            raise Unknown(f"удалённый «{refs.remote}» назван не именем (`origin`), к нему нет ветки отслеживания")
        repo = real_dir(refs.repo)
        foreign = {}
        for src, dst, optional in refs.pairs:
            for sha, subject in foreign_commits(repo, refs.remote, src, dst, optional, ticket):
                foreign.setdefault(sha, subject)
    except Unknown as e:
        return REASON_PUSH_SCOPE_UNKNOWN.format(t=publication, why=e)
    if not foreign:
        return None
    shown = "; ".join(f"{sha[:8]} {subject[:60]}" for sha, subject in list(foreign.items())[:SHOW_FOREIGN])
    more = f" (и ещё {len(foreign) - SHOW_FOREIGN})" if len(foreign) > SHOW_FOREIGN else ""
    return REASON_PUSH_SCOPE.format(t=publication, n=len(foreign), who=who, shown=shown + more)
