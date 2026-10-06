"""Формат тикета диспетчера: `.claude/tickets/<ID>.md`.

Шапка между строками `---` (простые строки `ключ: значение`, без внешнего YAML):
`id, title, owner` (researcher|engineer|judge), `status`
(backlog|todo|in_progress|waiting|in_review|done|blocked|needs_owner|stopped), `reviewer` (опц.),
`wait_for` (опц.: `file:<путь>` локально, `host:<calc|vps|deck>:<путь>` / `host:<…>:unit:<имя>` на машине по ssh
(`deck:<путь>` — синоним `host:deck:<путь>`), `ticket:<ID>`; разбор — `parse_wait_for`),
`next` (опц., v2: `researcher|engineer|judge|ceo` — кого запустить один раз; пишет
`tickets.py comment --next`, диспетчер очищает при запуске), `effort` (опц., v2: `low|medium|high|xhigh`),
`updated`. `backlog` — задача перенесена (например из TASKS.md), но ещё не в работе: диспетчер её
не трогает (`dispatch.decide()`), в `todo` переводит `tickets.py start <ID>`. `stopped` — роль остановлена CEO
(`tickets.py stop` без `--next`): диспетчер не будит, сторож не считает сиротой; в `todo` — тем же `start`.

Тело: свободное описание, затем заголовок `## Лог` — записи `### <ISO-время> <автор>` + текст.
v2 (02.10): «роль оставила запись» диспетчер определяет по НОВОМУ заголовку записи этой роли
(`role_entry_keys()`), не по росту секции; @упоминания в тексте — обычный текст и никого не будят.
Лог больше 20 КБ ужимается (`compact_log()`): всё, кроме последних 8 записей, уходит в
`archive/<ID>-log.md`.

Только stdlib. Роли и авторы записей — латинские ключи (researcher/engineer/judge/ceo/
dispatcher), не русские названия: так упоминания и авторство сравниваются без транслитерации.
"""
from __future__ import annotations

import hashlib
import os
import re
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import project as P  # noqa: E402

try:  # кроссплатформенная блокировка файла: msvcrt на Windows, fcntl иначе
    import msvcrt
except ImportError:  # pragma: no cover
    msvcrt = None
try:
    import fcntl
except ImportError:
    fcntl = None

HEADER_RE = re.compile(r"^---\r?\n(.*?)\r?\n---\r?\n?", re.S)
LOG_HEADING_RE = re.compile(r"^##\s*Лог\s*$", re.M)
ENTRY_RE = re.compile(r"^###\s+(\S+)\s+(.+?)\s*$", re.M)
MENTION_RE = re.compile(r"@(researcher|engineer|judge|ceo)\b", re.I)
ROLE_TOKENS = ("researcher", "engineer", "judge", "ceo")
VALID_EFFORTS = ("low", "medium", "high", "xhigh")

# v2 (02.10): компакция лога тикета — файл > LOG_COMPACT_BYTES → всё, кроме последних LOG_KEEP_ENTRIES
# записей, переезжает в archive/<ID>-log.md (дословно); в логе остаётся одна строка-указатель.
LOG_COMPACT_BYTES = 20 * 1024
LOG_KEEP_ENTRIES = 8
ARCHIVE_DIRNAME = "archive"
ARCHIVE_POINTER_PREFIX = "> Архив лога:"
_POINTER_LINE_RE = re.compile(r"^" + re.escape(ARCHIVE_POINTER_PREFIX) + r".*\n?", re.M)


# --- wait_for: допустимые формы (одно место правды: диспетчер, tickets.py, проверка записи) --------------------
# Алиасы машин для `host:<алиас>:...`; адрес ssh — из окружения (RPV_<АЛИАС>_HOST, прежнее ALPHA_<АЛИАС>_HOST), без умолчаний.
WAIT_FOR_HOSTS = ("calc", "vps", "deck")
WAIT_FOR_FORMATS = ("file:<путь> | ticket:<ID> | host:<calc|vps|deck>:<путь> (…/job.json с done/total — готово при "
                    "done>=total, иначе файл существует) | host:<calc|vps|deck>:unit:<имя юнита> (готово, когда "
                    "systemctl is-active ≠ active) | deck:<путь> (= host:deck:<путь>) | ci:<владелец/репо>#<PR> (готово, когда CI на текущей голове PR завершён)")
_UNIT_NAME_RE = re.compile(r"^[A-Za-z0-9_.@:-]+$")
_TICKET_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_CI_RE = re.compile(r"^([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(\d+)$")


def parse_wait_for(spec: str):
    """Разбор `wait_for`. Возвращает ("file", путь) | ("ticket", ID) | ("host", алиас, "path"|"unit", арг) либо None
    (форма не понята). Пустая строка — None: «ничего не ждём» проверяется отдельно."""
    spec = (spec or "").strip()
    if spec.startswith("file:"):
        arg = spec[len("file:"):].strip()
        return ("file", arg) if arg else None
    if spec.startswith("ticket:"):
        arg = spec[len("ticket:"):].strip()
        return ("ticket", arg) if _TICKET_ID_RE.match(arg) else None
    if spec.startswith("ci:"):
        m = _CI_RE.match(spec[len("ci:"):].strip())
        return ("ci", m.group(1), int(m.group(2))) if m else None
    if spec.startswith("deck:"):
        arg = spec[len("deck:"):].strip()
        return ("host", "deck", "path", arg) if arg else None
    if spec.startswith("host:"):
        alias, sep, rest = spec[len("host:"):].partition(":")
        rest = rest.strip()
        if alias not in WAIT_FOR_HOSTS or not sep or not rest:
            return None
        if rest.startswith("unit:"):
            unit = rest[len("unit:"):].strip()
            return ("host", alias, "unit", unit) if _UNIT_NAME_RE.match(unit) else None
        return ("host", alias, "path", rest)
    return None


def wait_cycle(tickets_dir, tid: str, target: str):
    """Цикл ожиданий (TK-076 п.7): если `tid` будет ждать `ticket:<target>`, а цепочка waiting-тикетов с wait_for
    `ticket:<ID>` из target возвращается в tid — список [tid, target, ..., tid]; иначе None. Рёбра самого tid не берём
    (проверяем предлагаемое ребро; для уже существующего — то же, исходящее ребро tid заменяется на себя же)."""
    edges = {}
    for path in list_tickets(tickets_dir):
        try:
            tkt = read_ticket(path)
        except Exception:
            continue
        if tkt.id == tid or tkt.status != "waiting":
            continue
        parsed = parse_wait_for(tkt.header.get("wait_for", ""))
        if parsed and parsed[0] == "ticket":
            edges[tkt.id] = parsed[1]
    chain, seen, cur = [tid, target], {tid}, target
    while cur != tid:
        if cur in seen:
            return None
        seen.add(cur)
        cur = edges.get(cur)
        if cur is None:
            return None
        chain.append(cur)
    return chain


def check_wait_for_format(spec: str) -> None:
    """Пустой `wait_for` (снять ожидание) допустим; непустой неизвестной формы — ValueError с подсказкой форм."""
    spec = (spec or "").strip()
    if spec and parse_wait_for(spec) is None:
        raise ValueError(f"wait_for не понят: {spec!r}. Допустимо: {WAIT_FOR_FORMATS}")


def now_iso(now: datetime | None = None) -> str:
    dt = now or datetime.now().astimezone()
    return dt.isoformat(timespec="seconds")


def parse_dt(value: str) -> datetime:
    dt = datetime.fromisoformat(value.strip())
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


@dataclass
class LogEntry:
    ts: datetime
    ts_raw: str
    author: str
    text: str

    @property
    def mentions(self) -> set:
        return {m.lower() for m in MENTION_RE.findall(self.text)}


@dataclass
class Ticket:
    path: Path
    header: dict
    description: str
    log: list
    log_raw: str = ""  # сырой текст секции «## Лог» целиком (диспетчер им больше не пользуется; для утилит)

    @property
    def id(self) -> str:
        return self.header.get("id") or (self.path.stem if self.path else "")

    @property
    def status(self) -> str:
        return self.header.get("status", "")

    @property
    def owner(self) -> str:
        return self.header.get("owner", "")

    @property
    def reviewer(self) -> str:
        return self.header.get("reviewer") or ""

    @property
    def executor(self) -> str:
        return self.header.get("executor") or ""

    @property
    def kind(self) -> str:
        return self.header.get("kind") or ""

    @property
    def next_role(self) -> str:
        """v2: кого запустить один раз (`next:` в шапке); пусто/незнакомое значение — никого."""
        value = (self.header.get("next") or "").strip().lower()
        return value if value in ROLE_TOKENS else ""

    @property
    def effort(self) -> str:
        """v2: усилие запуска по тикету (`effort:`); пусто/незнакомое значение — роль решает по умолчанию."""
        value = (self.header.get("effort") or "").strip().lower()
        return value if value in VALID_EFFORTS else ""

    def logged_since(self, author: str, since: datetime) -> bool:
        author = author.lower()
        return any(e.author.lower() == author and e.ts > since for e in self.log)


def _match_header(text: str):
    m = HEADER_RE.match(text)
    if not m:
        raise ValueError("тикет без шапки `---` ... `---`")
    return m


def _parse_header(text: str):
    m = _match_header(text)
    header = {}
    for line in m.group(1).splitlines():
        if not line.strip():
            continue
        key, _, value = line.partition(":")
        header[key.strip()] = value.strip()
    return header, m.end()


def _parse_log(rest: str):
    entries = []
    heading = LOG_HEADING_RE.search(rest)
    if not heading:
        return entries
    body = rest[heading.end():]
    matches = list(ENTRY_RE.finditer(body))
    for i, m in enumerate(matches):
        ts_raw, author = m.group(1), m.group(2).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        entry_text = body[start:end].strip("\n")
        try:
            ts = parse_dt(ts_raw)
        except ValueError:
            continue  # не запись лога (например, случайный `### ...` в описании) — пропустить
        entries.append(LogEntry(ts=ts, ts_raw=ts_raw, author=author, text=entry_text))
    return entries


def author_is(author: str, role: str) -> bool:
    """Автор записи — эта роль: первое слово заголовка без скобок («engineer», «engineer (запуск 2)»)."""
    words = (author or "").strip().lower().split()
    return bool(words) and words[0].strip("[]():,") == role.lower()


def role_entry_keys(tkt: "Ticket", role: str) -> list:
    """Ключи «<ts> <автор>» записей лога, оставленных ролью (заголовок `### <ts> <роль>`). v2 (02.10):
    «роль оставила запись» = в логе появился ключ, которого не было на старте запуска; от смещений
    (компакция лога, правки текста) не зависит — только от заголовков записей."""
    return [f"{e.ts_raw} {e.author}" for e in tkt.log if author_is(e.author, role)]


def atomic_write_text(path, text: str, retries: int = 8) -> None:
    """Запись через временный файл + os.replace. На Windows replace падает с PermissionError, пока
    другой процесс (диспетчер) держит файл открытым на чтение, — несколько коротких повторов. Сбой записи
    оставляет прежний файл целым и не оставляет `.tmp`."""
    path = Path(path)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8", newline="\n")
        for i in range(retries):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if i == retries - 1:
                    raise
                time.sleep(0.05 * (i + 1))
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


# --- блокировка на тикет (аудит 03.10): диспетчер, tickets.py и роли пишут один файл из разных процессов ------------
LOCK_TIMEOUT_S = float(P.env("TICKET_LOCK_TIMEOUT", "120"))
_LOCKS_DIR = Path(tempfile.gettempdir()) / "rpv-ticket-locks"  # вне репозитория: тикетный каталог не засоряется
_thread_locks: dict = {}
_thread_locks_guard = threading.Lock()
_held = threading.local()


def _os_lock(fh) -> None:
    if msvcrt is not None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    elif fcntl is not None:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _os_unlock(fh) -> None:
    try:
        if msvcrt is not None:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        elif fcntl is not None:
            fcntl.flock(fh, fcntl.LOCK_UN)
    except OSError:
        pass


@contextmanager
def ticket_lock(path, timeout: float = None):
    """Эксклюзивная блокировка на файл тикета (или любой путь): между потоками — threading.Lock, между процессами —
    файл-замок ОС (снимается сам при смерти процесса). Повторный вход из того же потока допускается."""
    key = hashlib.sha1(str(Path(path).resolve()).lower().encode("utf-8")).hexdigest()[:24]
    depth = getattr(_held, "depth", None)
    if depth is None:
        depth = _held.depth = {}
    if depth.get(key):
        depth[key] += 1
        try:
            yield
        finally:
            depth[key] -= 1
        return
    timeout = LOCK_TIMEOUT_S if timeout is None else timeout
    deadline = time.time() + timeout
    with _thread_locks_guard:
        tlock = _thread_locks.setdefault(key, threading.Lock())
    if not tlock.acquire(timeout=timeout):
        raise TimeoutError(f"блокировка тикета {Path(path).name}: занята дольше {timeout:.0f} с")
    fh = None
    try:
        _LOCKS_DIR.mkdir(parents=True, exist_ok=True)
        fh = open(_LOCKS_DIR / f"{key}.lock", "a+b")
        while True:
            try:
                _os_lock(fh)
                break
            except OSError:
                if time.time() >= deadline:
                    raise TimeoutError(f"блокировка тикета {Path(path).name}: занята дольше {timeout:.0f} с")
                time.sleep(0.05)
        depth[key] = 1
        try:
            yield
        finally:
            depth.pop(key, None)
            _os_unlock(fh)
    finally:
        if fh is not None:
            fh.close()
        tlock.release()


def _entry_starts(body: str) -> list:
    """Смещения начала записей `### <ISO-время> <автор>` в тексте секции лога."""
    starts = []
    for m in ENTRY_RE.finditer(body):
        try:
            parse_dt(m.group(1))
        except ValueError:
            continue  # «### Заметка» внутри записи — не граница записей
        starts.append(m.start())
    return starts


def compact_log(path, keep: int = None, limit_bytes: int = None) -> int:
    """v2 (02.10): файл тикета > limit_bytes (20 КБ) → все записи лога, кроме последних `keep` (8),
    дописываются ДОСЛОВНО в `archive/<ID>-log.md` (рядом с каталогом тикетов; `list_tickets` берёт только
    `*.md` верхнего уровня), в логе остаётся одна строка-указатель. Запись атомарная (tmp + replace);
    сперва архив, потом тикет — при сбое между ними записи продублируются, но не потеряются.
    Возвращает число перенесённых записей (0 — ничего не делали)."""
    path = Path(path)
    with ticket_lock(path):
        return _compact_log_locked(path, keep, limit_bytes)


def _compact_log_locked(path, keep, limit_bytes) -> int:
    keep = LOG_KEEP_ENTRIES if keep is None else keep
    limit_bytes = LOG_COMPACT_BYTES if limit_bytes is None else limit_bytes
    text = path.read_text(encoding="utf-8")
    if len(text.encode("utf-8")) <= limit_bytes:
        return 0
    heading = LOG_HEADING_RE.search(text)
    if not heading:
        return 0
    body = text[heading.end():]
    starts = _entry_starts(body)
    if len(starts) <= keep:
        return 0
    cut = starts[-keep] if keep > 0 else len(body)
    old_part = _POINTER_LINE_RE.sub("", body[:cut]).strip("\n")  # прежний указатель в архив не уносим
    if not old_part.strip():
        return 0
    moved = len([s for s in starts if s < cut])
    archive = path.parent / ARCHIVE_DIRNAME / f"{path.stem}-log.md"
    archive.parent.mkdir(parents=True, exist_ok=True)
    if archive.exists():
        prev = archive.read_text(encoding="utf-8")
    else:
        prev = (f"# {path.stem} — архив лога\n\nЗаписи, перенесённые из `.claude/tickets/{path.stem}.md` "
                "(старые сверху, текст дословно).\n")
    if not prev.endswith("\n"):
        prev += "\n"
    atomic_write_text(archive, prev + "\n" + old_part + "\n")
    pointer = (f"{ARCHIVE_POINTER_PREFIX} .claude/tickets/{ARCHIVE_DIRNAME}/{path.stem}-log.md — старые записи "
               "(читать grep-ом, только если нужно).")
    atomic_write_text(path, text[:heading.start()] + "## Лог\n\n" + pointer + "\n\n" + body[cut:])
    return moved


def parse_text(text: str, path: Path = None) -> Ticket:
    header, body_start = _parse_header(text)
    rest = text[body_start:]
    heading = LOG_HEADING_RE.search(rest)
    description = (rest[: heading.start()] if heading else rest).strip()
    log = _parse_log(rest)
    log_raw = rest[heading.end():] if heading else ""
    return Ticket(path=path, header=header, description=description, log=log, log_raw=log_raw)


def read_ticket(path) -> Ticket:
    path = Path(path)
    return parse_text(path.read_text(encoding="utf-8"), path)


def write_header_updates(path, updates: dict, now: datetime = None, stamp_updated: bool = True) -> None:
    """Точечно правит строки шапки (значения `updates`), тело файла не трогает. Под блокировкой тикета, запись атомарная."""
    path = Path(path)
    if "wait_for" in updates:
        check_wait_for_format(updates["wait_for"])  # неизвестная форма — отказ до записи (иначе тикет ждёт вечно)
    with ticket_lock(path):
        text = path.read_text(encoding="utf-8")
        m = _match_header(text)
        lines = m.group(1).splitlines()
        updates = dict(updates)
        if stamp_updated and "updated" not in updates:
            updates["updated"] = now_iso(now)
        seen = set()
        new_lines = []
        for line in lines:
            key = line.split(":", 1)[0].strip() if ":" in line else None
            if key in updates:
                new_lines.append(f"{key}: {updates[key]}")
                seen.add(key)
            else:
                new_lines.append(line)
        for key, value in updates.items():
            if key not in seen:
                new_lines.append(f"{key}: {value}")
        new_header = "---\n" + "\n".join(new_lines) + "\n---\n"
        # LF, не CRLF: `git commit` на Windows иначе предупреждает (судья 27.09) — atomic_write_text пишет newline="\n"
        atomic_write_text(path, new_header + text[m.end():])


def append_log(path, author: str, text: str, now: datetime = None) -> None:
    """Дописывает запись `### <время> <автор>` в конец файла (лог — последняя секция). Под блокировкой, атомарно."""
    path = Path(path)
    with ticket_lock(path):
        content = path.read_text(encoding="utf-8")
        if not content.endswith("\n"):
            content += "\n"
        if not LOG_HEADING_RE.search(content):
            if not content.endswith("\n\n"):
                content += "\n"
            content += "## Лог\n"
        if not content.endswith("\n\n"):
            content += "\n"
        content += f"### {now_iso(now)} {author}\n{text.strip()}\n"
        atomic_write_text(path, content)


def next_ticket_id(tickets_dir, prefix: str = "TK-") -> str:
    tickets_dir = Path(tickets_dir)
    best = 0
    if tickets_dir.exists():
        pat = re.compile(rf"^{re.escape(prefix)}(\d+)$")
        for p in tickets_dir.glob(f"{prefix}*.md"):
            m = pat.match(p.stem)
            if m:
                best = max(best, int(m.group(1)))
    return f"{prefix}{best + 1:03d}"


def create_ticket(tickets_dir, owner: str, title: str, reviewer: str = None,
                   description: str = "", wait_for: str = "", now: datetime = None,
                   prefix: str = "TK-", status: str = "todo", executor: str = None,
                   kind: str = None, effort: str = None) -> Path:
    check_wait_for_format(wait_for)
    tickets_dir = Path(tickets_dir)
    tickets_dir.mkdir(parents=True, exist_ok=True)
    with ticket_lock(tickets_dir / ".new-ticket"):      # номер и файл — под одной блокировкой, id не повторяются
        return _create_ticket_locked(tickets_dir, owner, title, reviewer, description, wait_for, now, prefix,
                                      status, executor, kind, effort)


def _create_ticket_locked(tickets_dir, owner, title, reviewer, description, wait_for, now, prefix, status, executor,
                           kind, effort) -> Path:
    tid = next_ticket_id(tickets_dir, prefix)
    lines = [f"id: {tid}", f"title: {title}", f"owner: {owner}", f"status: {status}"]
    if reviewer:
        lines.append(f"reviewer: {reviewer}")
    if executor:
        lines.append(f"executor: {executor}")
    if kind:
        lines.append(f"kind: {kind}")
    if effort:
        lines.append(f"effort: {effort}")
    lines.append(f"wait_for: {wait_for}")
    lines.append(f"updated: {now_iso(now)}")
    text = "---\n" + "\n".join(lines) + "\n---\n\n"
    if description.strip():
        text += description.strip() + "\n\n"
    text += "## Лог\n"
    path = tickets_dir / f"{tid}.md"
    atomic_write_text(path, text)
    return path


def list_tickets(tickets_dir):
    tickets_dir = Path(tickets_dir)
    if not tickets_dir.exists():
        return []
    return sorted(tickets_dir.glob("*.md"))
