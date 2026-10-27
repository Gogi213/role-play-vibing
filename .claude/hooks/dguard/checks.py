"""Проверка команды и файла целиком: `check` (Bash/PowerShell), `check_file` (Write/Edit/…)."""
from dguard.cmd_scan import scan_text
from dguard.config import norm
from dguard.git_scan import branch_delete_consent
from dguard.legacy import legacy_found
from dguard.lexer import ParseError
from dguard.messages import BranchDelete, Forbid, Over, Publish, REASON, REASON_BRANCH_DELETE, REASON_CRASH, REASON_FILE, REASON_IRREVERSIBLE, REASON_OVERWRITE, REASON_PUBLISH
from dguard.paths import allowed, Ctx, temp_rest, write_only_ok
from dguard.policy import dispatcher_role, protected, publish_consent
from dguard.words import FUNC_DEF
from dguard.writes import may_exist


def _deny(trace, group, reason):
    trace.append((group, "deny"))
    return reason


def _temp_hit(target, trace):
    """Разрешение во временном каталоге — тоже в журнал: без него нуль отказов ветки temp ничего не говорит."""
    if temp_rest(norm(target)) is not None and ("temp", "allow") not in trace:
        trace.append(("temp", "allow"))


def check(cmd, cwd, trace=None):
    """Причина отказа или None. Сам не падает: сбой разбора — прежний регэксп, сбой и его — отказ (fail-closed).
    `trace` — список (группа ветки, deny|allow) для журнала: решающая ветка и разрешающие temp/legacy/publish."""
    cmd = cmd or ""
    trace = [] if trace is None else trace
    legacy = False
    try:
        role = dispatcher_role()
        start = Ctx(norm(cwd).rstrip("/") if cwd else None, False, frozenset(FUNC_DEF.findall(cmd)))
        try:
            found = list(scan_text(cmd, start))
        except ParseError:                          # незакрытая кавычка и т. п. — прежний регэксп как страховка
            found = legacy_found(cmd, cwd)
            legacy = True
        for target, ctx in found:
            group = "legacy" if legacy else ctx.via or ("ssh" if ctx.remote else None)
            if isinstance(target, Publish):
                if role:                                    # CEO и владелец публикуют свободно; роль — с согласием по тикету
                    granted, who = publish_consent()
                    if not granted:
                        return _deny(trace, "publish", REASON_PUBLISH.format(t=target, who=who))
                    trace.append(("publish", "allow"))
                continue
            if isinstance(target, BranchDelete) and not role:       # CEO/владелец: только с согласием на каждую ветку
                granted, why = branch_delete_consent(target.branches)
                if granted:
                    trace.append(("branch-delete", "allow"))
                    continue
                return _deny(trace, "git", REASON_BRANCH_DELETE.format(t=target, why=why))
            if isinstance(target, Forbid):
                return _deny(trace, "git", REASON_IRREVERSIBLE.format(t=target))
            why = protected(target, ctx, role, deleting=not isinstance(target, Over))
            if why:
                return _deny(trace, "protected", why)
            if isinstance(target, Over):
                if not (allowed(target, ctx) or write_only_ok(target, ctx)) and may_exist(target, ctx):
                    return _deny(trace, group or "tee-trunc-cp", REASON_OVERWRITE.format(t=target))
                _temp_hit(target, trace)
                continue
            if not allowed(target, ctx):
                return _deny(trace, group or "rm", REASON.format(t=target))
            _temp_hit(target, trace)
    except Exception as e:                          # страж упал: пропуск был бы дырой — отказ с причиной
        return _deny(trace, "crash", REASON_CRASH.format(e=f"{type(e).__name__}: {e}"[:200]))
    if legacy:
        trace.append(("legacy", "allow"))
    return None


def check_file(path, cwd, trace=None):
    """Запись файловым инструментом. Запуск диспетчера (`RPV_ROLE`) — как перезапись через Bash: путь вне корней (папка
    проекта, scratchpad, temp, автопамять) и файл уже есть (или не проверить) — отказ; новый файл вне корней создать можно.
    Сессия CEO/владельца (аудит-3) правит файлы вне проекта свободно (настройки, соседние репозитории, планы). Для всех:
    внутрь .git, в настройки Claude Code и плагина, в защищённые пути проекта существующий файл — см. protected."""
    try:
        trace = [] if trace is None else trace
        ctx = Ctx(norm(cwd).rstrip("/") if cwd else None, False)
        target = Over(path)
        role = dispatcher_role()
        why = protected(target, ctx, role, deleting=False)
        if why:
            return _deny(trace, "protected", why)
        if role is None:
            _temp_hit(target, trace)
            return None
        if allowed(target, ctx) or write_only_ok(target, ctx) or not may_exist(target, ctx):
            _temp_hit(target, trace)
            return None
        return _deny(trace, "file", REASON_FILE.format(t=path))
    except Exception as e:
        return _deny(trace, "crash", REASON_CRASH.format(e=f"{type(e).__name__}: {e}"[:200]))
