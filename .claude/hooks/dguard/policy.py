"""Личность запуска, согласия на публикацию, защищённые пути, настройки, уставы, слово владельца."""
import fnmatch
import os
import posixpath
import re
from dguard import config
from dguard.ceo_signal import command_text, parts_write, shell_writes
from dguard.config import _env, norm
from dguard.messages import REASON_CEO_CONFIG, REASON_GIT_DIR, REASON_GIT_WRITE, REASON_OWNER_RECORD, REASON_PROTECTED, REASON_PROTECTED_CONFIG, REASON_SETTINGS
from dguard.paths import literal_part, placeholder, temp_rest, under_cwd
from dguard.tools import file_target, SHELL_TOOLS
from dguard.writes import may_exist


# ------------------------------------------------------------------------------------------------------------
# Аудит-3 (03.10): чьи правила и что защищено всегда; защищённые пути проекта (CP7)
# ------------------------------------------------------------------------------------------------------------

DISPATCH_ROLES = ("researcher", "engineer", "judge")
SETTINGS_NAMES = ("settings.json", "settings.local.json", "rpv.json")      # rpv.json — настройки плагина (protected_paths)


def dispatcher_role():
    """Роль запуска диспетчера (`RPV_ROLE`, её ставит dispatch.launch_run; наследуют и помощники роли) или None —
    сессия CEO или владельца."""
    r = _env("ROLE").strip().lower()
    return r if r in DISPATCH_ROLES else None


# CP1/CP2 (слово владельца 14.10: «нельзя притворяться CEO»): запуск роли не меняет и не стирает свою роль и тикет —
# иначе `RPV_ROLE= python errors.py fix …` выглядит как CEO. Ловится запись (присваивание, unset, env -u/-i, $env:…=,
# Remove-Item Env:, SetEnvironmentVariable, os.environ / putenv / unsetenv); чтение (`echo $RPV_ROLE`) — можно. Второй слой —
# `project.caller_role`: роль узнаётся и по дереву процессов, стёртое окружение её не прячет.
_IDENT = r"(?:RPV_(?:ROLE|TICKET|PROJECT)|CLAUDE_PROJECT_DIR)\b"  # кто запущен и где
IDENTITY_RE = re.compile(
    r"(?:^|[\s;&|(`'\"])(?:export\s+|set\s+|declare\s+(?:-\w+\s+)*|typeset\s+|local\s+|readonly\s+)?" + _IDENT + r"\s*\+?="
    r"|\bunset\b[^;&|\n]*" + _IDENT
    + r"|\bexport\s+-n\b[^;&|\n]*" + _IDENT
    + r"|\benv\b[^;&|\n]*(?:-u\s*|--unset[= ])" + _IDENT
    + r"|\benv\s+(?:-\w*i\w*|--ignore-environment|-)(?=\s|$)"
    + r"|\$env:" + _IDENT + r"\s*="
    + r"|\b(?:Remove-Item|Set-Item|Clear-Item|New-Item|Rename-Item|ri|si|ni|del|rm|erase)\b[^;\n]*\benv:\\?" + _IDENT
    + r"|SetEnvironmentVariable\s*\(\s*['\"]" + _IDENT
    + r"|\bos\.(?:environ|putenv|unsetenv)\b[^;\n]*" + _IDENT
    + r"|" + _IDENT + r"[^;\n]*\bos\.(?:environ|putenv|unsetenv)\b"
    + r"|\bprocess\.env\." + _IDENT + r"\s*=|\bdelete\s+process\.env\." + _IDENT,
    re.I)


def identity_change(tool, ti) -> bool:
    """Команда оболочки запуска роли меняет или стирает `RPV_ROLE`/`RPV_TICKET` (или всё окружение)."""
    return tool in SHELL_TOOLS and bool(IDENTITY_RE.search(str(ti.get("command") or "")))


def publish_consent():
    """Согласие владельца на публикацию по тикету запуска (`RPV_TICKET`) → (есть, тикет для текста отказа). Читается только
    шапка `<проект>/.claude/tickets/<ID>.md`: строка `allow: publish (В-N)` (ставит `tickets.py allow`, только CEO). Не
    прочитано — согласия нет (fail-closed)."""
    tid = _env("TICKET").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", tid):
        return False, "(тикет запуска RPV_TICKET не определён)"
    try:
        with open(os.path.join(config.PROJECT_DIR, ".claude", "tickets", tid + ".md"), encoding="utf-8") as f:
            if f.readline().strip() != "---":
                return False, f"{tid} (в файле нет шапки)"
            granted = False
            for line in f:
                key, _, val = line.partition(":")
                if line.strip() == "---":
                    return granted, tid
                if key.strip() == "allow":                  # как в разборе шапки (ticket.py): последняя строка ключа решает
                    granted = bool(re.match(r"publish\b", val.strip()))
    except (OSError, ValueError) as e:                      # ValueError — и UnicodeDecodeError: тикет не читается
        return False, f"{tid} (шапка не прочитана: {type(e).__name__})"
    return False, f"{tid} (шапка не закрыта)"


def _abs_norm(t, ctx):
    """Нормализованный путь цели; относительный — от известного рабочего каталога (неизвестен — как есть)."""
    p = norm(t)
    if p and not (p.startswith(("/", "~", "$", "%")) or re.match(r"[a-z]:", p)):
        cwd = getattr(ctx, "cwd", None)
        if cwd:
            p = under_cwd(cwd, p)
    return p


def git_internal(t, ctx):
    """Цель — каталог `.git` или путь внутри него, в т. ч. шаблон оболочки, который совпадёт с `.git` (`.*`, `.[!.]*`,
    `.g*`). Исключения: зависший замок `….lock` внутри .git и временные каталоги."""
    import fnmatch
    p = _abs_norm(t, ctx)
    if not p or p.startswith("<") or temp_rest(p) is not None:
        return False
    segs = [s for s in p.split("/") if s]
    for i, s in enumerate(segs):
        if s == ".git" or (s.startswith(".") and any(c in s for c in "*?[") and fnmatch.fnmatchcase(".git", s)):
            rest = segs[i + 1:]
            if s == ".git" and rest and rest[-1].endswith(".lock") and not any(c in rest[-1] for c in "*?["):
                return False
            return True
    return False


def settings_file(t, ctx):
    """Цель — файл настроек Claude Code (`.claude/settings.json`, `.claude/settings.local.json`) в любом каталоге."""
    segs = [s for s in _abs_norm(t, ctx).split("/") if s]
    return len(segs) >= 2 and segs[-2] == ".claude" and segs[-1] in SETTINGS_NAMES


def charter_file(t, ctx):
    """Цель — устав роли `.claude/roles/<имя>.md` (блокнот `roles/notes/…` — не устав)."""
    segs = [x for x in _abs_norm(t, ctx).split("/") if x]
    return len(segs) >= 3 and segs[-2] == "roles" and segs[-3] == ".claude" and segs[-1].endswith(".md")


def addon_file(t, ctx):
    """Цель — добавка проекта к уставу `.claude/roles/project/<имя>.md` (TK-038): закрыта и когда файла ещё нет — его создаёт только `charter addon`."""
    segs = [x for x in _abs_norm(t, ctx).split("/") if x]
    return len(segs) >= 4 and segs[-2] == "project" and segs[-3] == "roles" and segs[-4] == ".claude" and segs[-1].endswith(".md")


def charter_table_edit(tool, ti):
    """Edit строк таблицы (`| … |`) в `.claude/roles/README.md` — таблица зон с путями проекта; остальное в уставе закрыто."""
    path = (file_target(ti) or "").replace("\\", "/").lower() if tool == "Edit" else ""
    if not path.endswith(".claude/roles/readme.md"):
        return False
    rows = [ln for k in ("old_string", "new_string") for ln in str(ti.get(k) or "").splitlines() if ln.strip()]
    return bool(rows) and all(ln.lstrip().startswith("|") for ln in rows)


REGISTRY_DECIDE = re.compile(r"""registry\.py["']?(?:\s+--project(?:=|\s+)(?:"[^"]*"|'[^']*'|\S+))?\s+decide\b""")


def decide_outside_secretary(tool, ti, data):
    """`registry.py decide` из сессии CEO (не помощник `secretary`: в событии хука `agent_type`) — суть пишет секретарь по
    слову владельца, а не CEO своими словами. Запуск роли реестр отказывает сам."""
    if tool not in SHELL_TOOLS or not REGISTRY_DECIDE.search(str(ti.get("command") or "")):
        return False
    return str(data.get("agent_type") or "").rsplit(":", 1)[-1].strip().lower() != "secretary"


def owner_record(t, ctx):
    """Цель — слово владельца (V2): `CLAUDE.md` в любом каталоге, реестр решений `.claude/decisions.md`, цели
    `.claude/goals/…` — и реестр ошибок `.claude/errors/…` (CP1: записи пишет только `errors.py`; прямая правка проверки
    или статуса — обход красного → зелёного)."""
    segs = [s for s in _abs_norm(t, ctx).split("/") if s]
    if not segs:
        return False
    if segs[-1] == "claude.md" or (len(segs) >= 2 and segs[-2] == ".claude" and segs[-1] == "decisions.md"):
        return True
    if segs[-3:] == [".claude", "dispatcher", "state.json"]:  # запись диспетчера «какая роль на каком тикете» — по ней права
        return True
    return any(segs[i] == ".claude" and segs[i + 1] in ("goals", "errors") for i in range(len(segs) - 1))


OWNER_RECORD_NAMES = ("claude.md", ".claude/decisions.md", ".claude/goals", ".claude/errors", "dispatcher/state.json")


def owner_record_write(tool, ti):
    """Запись запуска диспетчера в слово владельца через оболочку, в т. ч. дописывание `>>` (его разбор целей не видит);
    чтение свободно (в т. ч. с `2>/dev/null`, `2>&1`), текст `--text`/`--why` у tickets.py не в счёт. Сам `errors.py` через
    оболочку по-прежнему пишет в `.claude/errors/`: путь скрипта (`…/dispatcher/errors.py`) названием каталога не считается."""
    return tool in SHELL_TOOLS and parts_write(command_text(ti, benign=True), OWNER_RECORD_NAMES)


CONSENT_LINE = re.compile(r"(?mi)^[ \t]*allow[ \t]*:")      # строка шапки тикета `allow: publish (В-N)`


def _strings(x):
    """Все строки внутри значения tool_input (Edit: new_string, MultiEdit: edits[].new_string, Write: content)."""
    if isinstance(x, str):
        yield x
    elif isinstance(x, dict):
        for v in x.values():
            yield from _strings(v)
    elif isinstance(x, (list, tuple)):
        for v in x:
            yield from _strings(v)


def consent_forge(tool, ti):
    """Запись запуском диспетчера строки `allow:` в файл тикета (`.claude/tickets/…`) мимо `tickets.py allow`: согласие на
    публикацию ставит только CEO. Файловые инструменты — строка шапки `allow:` в новом тексте; оболочка — команда, которая
    пишет (как в `owner_record_write`), называет `.claude/tickets/` и `allow:`; чтение и текст `--text`/`--why` не в счёт."""
    if tool in SHELL_TOOLS:
        cmd = command_text(ti, benign=True)
        return bool(shell_writes(cmd, (".claude/tickets/",)) and re.search(r"\ballow[ \t]*:", cmd, re.I))
    path = file_target(ti) or ""
    return ".claude/tickets/" in path.replace("\\", "/").lower() and any(CONSENT_LINE.search(s) for s in _strings(ti))


def _segs(p):
    return [s for s in p.split("/") if s and s != "."]


def _glob_seg(pseg, tseg):
    """Сегмент шаблона `pseg` подходит сегменту пути `tseg`; сегмент-маска цели (`rm data/*`) — «может подойти»."""
    return fnmatch.fnmatchcase(tseg, pseg) or (any(c in tseg for c in "*?[") and fnmatch.fnmatchcase(pseg, tseg))


def _glob_covers(pp, ps):
    """Путь `ps` (сегменты) попадает под шаблон `pp`: равен ему или лежит внутри совпавшего каталога; `**` — любое число
    сегментов."""
    if not pp:
        return True
    if pp[0] == "**":
        return any(_glob_covers(pp[1:], ps[k:]) for k in range(len(ps) + 1))
    return bool(ps) and _glob_seg(pp[0], ps[0]) and _glob_covers(pp[1:], ps[1:])


def _glob_contains(pp, ps):
    """Каталог `ps` может содержать то, что назвал шаблон `pp`: удаление каталога заденет защищённое."""
    if not ps:
        return bool(pp)
    if not pp:
        return False
    if pp[0] == "**":
        return _glob_contains(pp[1:], ps) or _glob_contains(pp, ps[1:])
    return _glob_seg(pp[0], ps[0]) and _glob_contains(pp[1:], ps[1:])


def _canon(p, remote):
    """Путь для сопоставления с шаблонами: локально `~`/`$home` — домашний каталог, `/c/x` — `c:/x` (Windows); на удалённой
    машине путь остаётся текстом."""
    if remote:
        return p
    for pre in ("~", "$home", "${home}"):
        if p == pre or p.startswith(pre + "/"):
            return norm(os.path.expanduser("~")).rstrip("/") + p[len(pre):]
    m = re.match(r"^/([a-z])/(.*)$", p)
    return f"{m.group(1)}:/{m.group(2)}" if m and os.name == "nt" else p


def protected_match(t, ctx, deleting):
    """Шаблон из `PROTECTED`, под который попадает цель, или None. Попадает: сама цель, всё внутри совпавшего каталога, а
    при удалении — ещё каталог, внутри которого лежит защищённое (`rm -rf data` при `data/raw/**`; шаблоны, начинающиеся с
    имени или `**`, родителей не закрывают: глубина у них любая). Шаблон без `/` — имя на любой глубине (как в .gitignore);
    с `/` — путь от корня проекта; с начальным `/`, `~` или диском (`c:/`) — именно этот путь. Маска в цели (`rm data/*`)
    считается «может задеть». На удалённой машине корень проекта неизвестен: шаблон с `/` ищется на любой глубине. Путь,
    начало которого — неизвестная переменная, не видно (решают прочие правила); временный каталог (`$TMP/x`) — виден."""
    remote = bool(getattr(ctx, "remote", False))
    p = _abs_norm(t, ctx)
    if placeholder(p):
        return None
    lit = literal_part(p)
    rel = None                                               # путь от корня проекта (только локально)
    if lit:
        if lit != p and not lit.endswith("/"):
            lit += "*"                                       # `data/ra$X` — как `data/ra*`
        p = _canon(lit, remote)
        if ".." in p.split("/"):
            p = posixpath.normpath(p)
        absolute = p.startswith(("/", "~", "$home", "${home}")) or bool(re.match(r"[a-z]:", p))
        segs = _segs(p)
        if not remote:
            for root in config.LOCAL_ROOTS:
                if (p + "/").startswith(root):
                    rel = _segs(p[len(root):])
                    break
            if rel is None and not absolute:
                rel = segs                                   # относительный путь без известного cwd — от корня проекта
        name_segs = rel if rel is not None else segs
        if rel is None and not remote:                       # вне проекта домашний каталог в имена не входит
            home = _segs(norm(os.path.expanduser("~")))
            if home and segs[:len(home)] == home:
                name_segs = segs[len(home):]
    else:
        tr = temp_rest(p)                                    # `$TMP/x`, `$(mktemp -d)/x`: известна только часть после корня
        if tr is None:
            return None
        absolute, segs = False, _segs(tr[0])
        name_segs = segs
    for pat in config.PROTECTED:
        if pat.startswith(("/", "~", "$home", "${home}")) or re.match(r"[a-z]:", pat):
            pp, subj = _segs(_canon(pat, remote)), (segs if absolute else None)
        elif "/" in pat and not remote:
            pp, subj = _segs(pat), rel
        else:
            pp, subj = ["**"] + _segs(pat), name_segs
        if subj is None:
            continue
        if _glob_covers(pp, subj) or (deleting and pp[:1] not in (["**"], []) and _glob_contains(pp, subj)):
            return pat
    return None


def rpv_config(t, ctx):
    """Цель — файл настроек плагина `.claude/rpv.json`."""
    return _segs(_abs_norm(t, ctx))[-2:] == [".claude", "rpv.json"]


def protected_path(t, ctx, deleting):
    """Защищённые пути проекта (`.claude/rpv.json` → `protected_paths`) → причина отказа или None. Перезапись (не
    удаление) — только если файл есть или это неизвестно: новый файл ничего не затирает. Настройка не прочитана — отказ
    любой цели, кроме самого файла настроек (его и надо исправить)."""
    if config.PROTECTED_ERROR:
        if rpv_config(t, ctx):
            return None
        why = REASON_PROTECTED_CONFIG.format(e=config.PROTECTED_ERROR)
    else:
        pat = protected_match(t, ctx, deleting) if config.PROTECTED else None
        if pat is None:
            return None
        why = REASON_PROTECTED.format(t=t, p=pat)
    return why if deleting or may_exist(t, ctx) else None


def protected(t, ctx, role, deleting):
    """Защищённые места → причина отказа или None. `.git`: удаление и перенос — никому, запись — запускам диспетчера;
    настройки Claude Code и плагина, слово владельца (CLAUDE.md, решения, цели) и реестр ошибок `.claude/errors/` — запускам
    диспетчера (ни удалить, ни перезаписать); защищённые пути проекта (`protected_paths`) — никому, ни роли, ни CEO."""
    if git_internal(t, ctx):
        if deleting:
            return REASON_GIT_DIR.format(t=t)
        if role:
            return REASON_GIT_WRITE.format(t=t)
    if role and settings_file(t, ctx):
        return REASON_SETTINGS.format(t=t)
    if role and owner_record(t, ctx):
        return REASON_OWNER_RECORD.format(t=t)
    if role is None and (addon_file(t, ctx) or ((charter_file(t, ctx) or (rpv_config(t, ctx) and not config.PROTECTED_ERROR))             and (deleting or may_exist(t, ctx)))):         # TK-007: CEO правит настройки командой; сломанный файл — руками
        return REASON_CEO_CONFIG.format(t=t)
    return protected_path(t, ctx, deleting)
