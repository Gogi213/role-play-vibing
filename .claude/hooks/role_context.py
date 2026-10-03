"""SessionStart: возвращает сессии команды alpha её роль.

Роль — переменная `ALPHA_ROLE` (запуск диспетчера `claude -p`, одна задача — один тикет, id тикета —
`ALPHA_TICKET`, если диспетчер его передал), иначе название сессии в Claude Desktop (метаданные приложения по
`CLAUDE_CODE_HOST_SESSION_ID`, переживает клир).

Размер вставки — всегда ≤ LIMIT (8000) знаков: Claude Code режет всё, что больше ~10 тыс., до превью ~2 КБ, и
тогда роль не видит свой устав (аудит 02.10). Поэтому:
- resume / compact — 1–2 строки: роль и путь устава (разговор и так в контексте / сжат в сводку);
- startup / clear — личность роли, её устав `.claude/roles/<роль>.md` (целиком, если влезает; иначе путь),
  конец блокнота роли (≤ 4000 знаков), пути README команды и тикета. Не влезло — режется низший приоритет
  (блокнот → устав → указатели); личность не режется.
Хук никогда не падает: сбой — короткая строка, роль определяется вручную.
"""
import glob
import json
import os
import re
import sys

# (подстрока названия в нижнем регистре, файл устава) — первое совпадение
ROLES = [
    ("исследователь", "researcher"),
    ("инженер", "engineer"),
    ("судья", "judge"),
    ("ceo", "ceo"),
]
ROLE_NAMES = {"researcher": "Исследователь", "engineer": "Инженер", "judge": "Судья", "ceo": "CEO"}
# Корень — проект, в котором идёт сессия (плагин лежит в своей папке): CLAUDE_PROJECT_DIR, иначе текущий каталог.
ROOT = os.path.abspath(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
ROLES_DIR = os.path.join(ROOT, ".claude", "roles")
if not os.path.isdir(ROLES_DIR):  # в проекте нет команды ролей — хуки молчат
    sys.exit(0)
TICKETS_DIR = os.path.join(ROOT, ".claude", "tickets")

LIMIT = 8000          # знаков вставки; больше — Claude Code покажет роли только превью ~2 КБ
NOTEBOOK_TAIL = 4000  # знаков конца блокнота роли
SHORT_SOURCES = ("resume", "compact")

MANUAL = (
    "Роль сессии автоматически не определена. Если ты участник команды alpha — роль назовёт владелец "
    "(`CEO`, `Роль: Исследователь`, `Роль: Инженер`, `Роль: Судья`); уставы — .claude/roles/<ceo|researcher|"
    "engineer|judge>.md, команда — .claude/roles/README.md. Не из команды — правило ниже."
)
OUTSIDER = (
    "Общую память проекта (CLAUDE.md «СОСТОЯНИЕ», .memory/, docs/plan/SETTLED.md, автопамять "
    "~/.claude/projects/…/memory/) пишет только сессия `CEO`; задачи команды без просьбы владельца не брать."
)


def session_dirs():
    appdata = os.environ.get("APPDATA")
    if appdata:
        yield os.path.join(appdata, "Claude", "claude-code-sessions")
    local = os.environ.get("LOCALAPPDATA")
    if local:  # установка из Microsoft Store: данные приложения в пакете
        yield from glob.glob(os.path.join(
            glob.escape(local), "Packages", "Claude_*", "LocalCache", "Roaming", "Claude",
            "claude-code-sessions"))


def find_title(host_id, cli_id):
    """Название сессии: по id сессии приложения, иначе по id сессии CLI."""
    for d in session_dirs():
        if not os.path.isdir(d):
            continue
        pattern = os.path.join(glob.escape(d), "*", "*", "local_*.json")
        for f in glob.glob(pattern):
            name = os.path.basename(f)[: -len(".json")]
            if host_id and name != host_id:
                continue
            with open(f, encoding="utf-8") as fh:
                meta = json.load(fh)
            if host_id or (cli_id and meta.get("cliSessionId") == cli_id):
                return True, meta.get("title")
    return False, None


def env_role():
    """Роль из `ALPHA_ROLE` (запуск диспетчера) или None."""
    r = os.environ.get("ALPHA_ROLE")
    return r if r in ROLE_NAMES else None


def ticket_id():
    """Id тикета запуска диспетчера (`ALPHA_TICKET`), если передан и безопасен для имени файла."""
    t = os.environ.get("ALPHA_TICKET") or os.environ.get("ALPHA_TICKET_ID") or ""
    return t if re.fullmatch(r"[A-Za-z0-9_-]{1,32}", t) else None


def rel(path):
    return os.path.relpath(path, ROOT).replace(os.sep, "/")


def ticket_path():
    """Относительный путь файла тикета запуска или None."""
    tid = ticket_id()
    if tid and os.path.isfile(os.path.join(TICKETS_DIR, tid + ".md")):
        return f".claude/tickets/{tid}.md"
    return None


def detect(hook_in):
    """(роль, название, None) или (None, название|None, текст-вместо-вставки)."""
    er = env_role()
    if er:
        return er, f"ALPHA_ROLE={er}", None
    host_id = os.environ.get("CLAUDE_CODE_HOST_SESSION_ID")
    try:
        found, title = find_title(host_id, hook_in.get("session_id"))
    except Exception as e:
        return None, None, f"=== РОЛЬ СЕССИИ: не определена ({type(e).__name__}: {e}) ===\n{MANUAL}\n{OUTSIDER}"
    if not found or not title:
        why = "метаданные сессии не найдены" if not found else "у сессии нет названия"
        return None, None, f"=== РОЛЬ СЕССИИ: не определена ({why}) ===\n{MANUAL}\n{OUTSIDER}"
    role = next((r for key, r in ROLES if key in title.lower()), None)
    if role is None:
        return None, title, f"=== Сессия «{title}» — не роль команды alpha ===\n{OUTSIDER}"
    return role, title, None


def read_file(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read().strip()


def tail_chars(text, limit):
    """Конец текста ≤ limit знаков по границе строки (первая неполная строка отбрасывается)."""
    if len(text) <= limit:
        return text
    cut = text[-limit:]
    nl = cut.find("\n")
    return cut[nl + 1:] if 0 <= nl < len(cut) - 1 else cut


def short_context(role, title, source):
    """resume/compact: 1–2 строки — роль и путь устава."""
    lines = [f"=== РОЛЬ СЕССИИ: ты — «{ROLE_NAMES[role]}» команды alpha (хук role_context.py, событие: {source}) ===",
             f"Устав роли — .claude/roles/{role}.md, блокнот — .claude/roles/notes/{role}.md; "
             "если контекст сжат — перечитай устав."]
    tp = ticket_path()
    if tp:
        lines[1] += f" Тикет запуска — {tp}."
    return "\n".join(lines)


def full_context(role, title, source, hook_in):
    """startup/clear: личность + устав роли + конец блокнота + пути; итог ≤ LIMIT."""
    dispatcher = env_role() is not None
    name = ROLE_NAMES[role]
    shown = name if dispatcher else title
    tp = ticket_path()
    head = [f"=== РОЛЬ СЕССИИ: ты — «{shown}» команды alpha (хук .claude/hooks/role_context.py, событие: {source}) ==="]
    if dispatcher:
        head.append(f"Запуск диспетчера (ALPHA_ROLE={role}): одна задача — один тикет"
                    + (f" `{tp}`" if tp else " (`.claude/tickets/`; id — в промпте)")
                    + ": шапка, описание, последние записи `## Лог`.")
    elif role == "ceo":
        head.append("Ты говоришь с владельцем; работа команды — тикеты `.claude/tickets/`.")
    else:
        head.append("Работа — тикет `.claude/tickets/<ID>.md`, который назвал владелец или CEO.")
    head.append(f"Связь с командой — только лог тикета: `python .claude/dispatcher/tickets.py comment <ID> "
                f"--author {role} --text \"...\" [--next <роль>]` (`--next` — кого разбудить следующим; "
                "@упоминания никого не будят).")
    if role != "ceo":
        head.append("«Первое в новом чате» из CLAUDE.md — очередь CEO, не твоя. Общую память проекта (CLAUDE.md "
                    "«СОСТОЯНИЕ», .memory/, docs/plan/SETTLED.md, автопамять) пишет только `CEO`; ты работаешь по "
                    "своему тикету.")
    try:  # память ролей: догнать конспект прошлой сессии, дать на него ссылку
        from role_memory import on_session_start
        last = on_session_start(hook_in, role, title)
    except Exception:
        last = None
    if last:
        head.append(f"Конспект прошлой сессии этой роли: `{rel(last)}` — не читать целиком, грепом/секциями.")
    paths = ["Устав команды — .claude/roles/README.md (читать по необходимости)."]
    if tp:
        paths.append(f"Тикет — {tp}.")
    must = "\n".join(head + paths)

    budget = LIMIT - len(must) - 80   # 80 — заголовки блоков и переводы строк
    charter_rel = f".claude/roles/{role}.md"
    notebook_rel = f".claude/roles/notes/{role}.md"
    blocks = [must]
    try:
        charter = read_file(os.path.join(ROLES_DIR, f"{role}.md"))
        if len(charter) <= budget:
            blocks.append(f"--- {charter_rel} ---\n{charter}")
            budget -= len(charter)
        else:
            blocks.append(f"Устав роли не влез в вставку — прочитай {charter_rel}.")
    except Exception as e:
        blocks.append(f"Устав роли не прочитан ({type(e).__name__}) — {charter_rel}.")
    try:
        notebook = read_file(os.path.join(ROLES_DIR, "notes", f"{role}.md"))
        take = min(NOTEBOOK_TAIL, budget)
        if take >= 400:
            tail = tail_chars(notebook, take)
            cut = f", конец {len(tail)} из {len(notebook)} знаков; начало — в файле" if len(tail) < len(notebook) else ""
            blocks.append(f"--- блокнот {notebook_rel}{cut} ---\n{tail}")
        else:
            blocks.append(f"Блокнот роли не влез в вставку — прочитай {notebook_rel}.")
    except Exception:
        pass
    return "\n\n".join(blocks)


def fit(text):
    """Самопроверка: итог ≤ LIMIT. Сюда доходит только непредвиденное — последняя страховка."""
    if len(text) <= LIMIT:
        return text
    mark = "\n…[вставка обрезана хуком до лимита]"
    return text[:LIMIT - len(mark)] + mark


def context(source, hook_in):
    role, title, text = detect(hook_in)
    if role is None:
        return fit(text)
    if source in SHORT_SOURCES:
        try:  # догнать конспект прошлой сессии, если она оборвалась (без вывода)
            from role_memory import on_session_start
            on_session_start(hook_in, role, title)
        except Exception:
            pass
        return fit(short_context(role, title, source))
    return fit(full_context(role, title, source, hook_in))


def main():
    try:
        hook_in = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace"))
    except Exception:
        hook_in = {}
    source = hook_in.get("source") or (sys.argv[1] if len(sys.argv) > 1 else "?")
    try:
        text = fit(context(source, hook_in))
    except Exception as e:  # хук не должен ломать старт сессии
        text = fit(f"=== РОЛЬ СЕССИИ: сбой хука ({type(e).__name__}: {e}) ===\n{MANUAL}\n{OUTSIDER}")
    out = {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}
    # ensure_ascii=False: кириллица не раздувается в \uXXXX (вывод ≈ длине текста); байты — UTF-8 напрямую
    sys.stdout.buffer.write(json.dumps(out, ensure_ascii=False).encode("utf-8", "replace"))
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
