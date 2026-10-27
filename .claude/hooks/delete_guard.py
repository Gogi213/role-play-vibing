"""Удаление — только внутри своей папки (владелец 27.09: «нельзя удалять ничего кроме чего то внутри своей папки»).

Вызывается PreToolUse-хуком плагина (`hooks/hooks.json`: Bash, PowerShell, Write, Edit, MultiEdit,
NotebookEdit): точка входа `main()` читает JSON события со stdin, `check(cmd, cwd)` → причина отказа или None. Корень проекта — `CLAUDE_PROJECT_DIR` (иначе cwd); нет `.claude/roles` — молчит.
Настройки — файл проекта `<проект>/.claude/rpv.json` (что в них вписать, решает проект; плагин своих правил про чужие
каталоги не несёт). Своя папка: локально — папка проекта и scratchpad сессии; на удалённых машинах — каталоги
`guard_remote_roots`; оперативная стадия — `guard_stage`. Никогда: хосты `guard_forbidden_hosts` и защищённые пути
`protected_paths`. Цель, которую нельзя проверить (переменная без буквального префикса, список из конвейера/xargs, путь
из кода), — отказ: переписать явным путём.

Разбор (02.10, аудит: регэксп по всему тексту давал ~52 ложных отказа за 5 дней — на переменную `rd`, слово «rm» в
комментарии тикета, `grep 'unlink'`): команда режется на простые команды (`;` `&&` `||` `|` `&`, перевод строки, `$(…)`,
кавычки и heredoc учитываются), и удалением считается только простая команда, у которой КОМАНДНОЕ слово — rm/rmdir/unlink/
shred/del/erase/rd/Remove-Item/ri, `find … -delete|-exec rm`, `git clean`, `rsync --delete*|--remove-source-files`,
либо исполняемый Python (`-c`, stdin/heredoc), вызывающий shutil.rmtree/os.remove/os.unlink/os.rmdir/Path.unlink/.rmdir().
Перезапись и усечение (аудит 03.10) — то же, что удаление, с теми же разрешёнными корнями: `> файл` (`>|`, `&>`, `2>`; не
`>>`), `truncate`, `dd of=`, `cp`/`mv` поверх существующего. Цель вне корней разрешена, только если локальный буквальный путь
не существует (создание нового файла); удалённый хост и непроверяемый путь считаются «существует». `git reset --hard`,
`git clean -f…`, `git push --force/-f/--force-with-lease` — отказ «только через CEO» в любом каталоге. Исключений по имени
скрипта нет: строка в тексте команды проверку не отключает.
`git reset --hard` и `git clean -f…` разрешены в каталоге вне основного дерева, заданном явным путём (`cd <путь>`,
`git -C <путь>`, рабочий каталог вызова): scratchpad сессии, `<подкаталог из guard_remote_roots>`, `/tmp/<подкаталог>`,
связанные worktree внутри папки проекта (`.git` — файл); неизвестная переменная в `cd $W`,
`--git-dir`/`--work-tree`/GIT_DIR, основное дерево и вложенные репозитории — отказ; `git push --force` —
всегда отказ; перезапись/усечение (не удаление) разрешены в автопамяти `~/.claude/projects/<проект>/memory/`.
Дыры, закрытые вторым проходом аудита (03.10): (а) `mv` проверяет и ИСТОЧНИК — как удаление источника (`mv защищённое ./trash
&& rm -rf ./trash`); то же `Move-Item`/`move`/`ren`; (б) файловые инструменты Write/Edit/MultiEdit/NotebookEdit —
`check_file(путь, cwd)`: путь вне корней и файл уже есть — отказ (новый файл вне корней — можно, как для Bash); единая точка
входа хука — `check_tool(событие)`; (д) временные файлы — не данные владельца: запись, перезапись, удаление под `$TMP`/`$TEMP`/`$TMPDIR`/`$env:TEMP`/`%TEMP%`, `/tmp/<путь>`
(локально и на любой удалённой машине, кроме закрытых узлов), `…/AppData/Local/Temp/<путь>` и результатами `mktemp` без `-p`;
сам корень временного каталога, `*`, `..` — по-прежнему нет; (в) `tee` без `-a`, `Out-File`/`Set-Content`/`Tee-Object`, `find … -exec cp|mv|tee|…
{}` (пути find — цели), `git checkout -- <путь>|.`, `git restore`, `git switch -f`, `git branch -D|-M|-f`, `git stash
clear|drop`, `git push +ref|:ref|--delete|--mirror` — как `git reset --hard` (вне основного дерева разрешено, иначе «только
через CEO»); (г) fail-closed: сбой самого стража (исключение при разборе, битое событие) — отказ с причиной, не пропуск.
Третий проход (03.10): (е) Write/Edit/MultiEdit/NotebookEdit вне корней ограничены только для запусков диспетчера
(`RPV_ROLE`); сессия CEO/владельца правит файлы вне проекта, кроме защищённых путей и закрытых узлов; (ж) `.git` — удаление,
перенос, шаблоны вроде `.*` никому (кроме `*.lock`), запись внутрь — не запускам диспетчера; (з) настройки Claude Code
`.claude/settings*.json` и настройки плагина `.claude/rpv.json` запуску диспетчера не менять (через них выключаются хуки и
защита путей).
Публикации (CP2, критерий 14: необратимое — только с согласия владельца): запуску диспетчера (`RPV_ROLE`) закрыты `gh release
create|upload|edit`, `gh pr merge` (вливает программа `merge_rule.py` после Судьи), `git push` в main/master, тега, `--all`
(и пуш без ветки, пока текущая — main/master или не определима), `npm|pnpm|yarn|cargo|poetry|uv publish`, `twine upload`,
`docker push`, `gem push`; запись в GitHub мимо них — `gh api` с записью (`-X/--method POST|PUT|PATCH|DELETE` либо поля/тело
`-f -F --field --input`) на слияние и выпуск (`/pulls/<n>/merge`, `/merges`, `/releases`, `/git/refs`, `/git/tags`, `/contents/`;
мутации GraphQL слияния, ветки, релиза) и `curl`/`wget`/`Invoke-WebRequest`/`Invoke-RestMethod` с записью (`-X`, `-d/--data*`, `-F`,
`-T`, `--upload-file`, `-Method Post|Put|Patch|Delete`, `-Body`) на `api.github.com`/`uploads.github.com`; чтение свободно — если в шапке
тикета запуска (`RPV_TICKET`) нет `allow: publish (В-N)`. Поле ставит только CEO
(`tickets.py allow`); запуск диспетчера строку `allow:` в файл тикета не пишет; шапка не прочитана — отказ. Сессии CEO и владельца не ограничены.
Пуш в main/master при `guard_push_scope: true` (TK-040, по умолчанию выключено) — и из запуска роли (после согласия), и из сессии CEO —
сверяет `<удалённый>/<ветка>..<что уходит>` с принятым тикетом: уйти могут коммиты голов PR из `accepted:` в шапке (роль — её
тикета `RPV_TICKET`, CEO — любого) и merge-коммиты принятых голов; чужие — отказ с sha и подсказкой «завести тикет или пушить
отдельно»; не сверить (нет `git fetch`, удалённая машина, каталог не определить) — тоже отказ (`dguard/push_scope.py`).
Через обёртки (sudo/env/nohup/xargs/systemd-run/timeout/…), `ssh хост '<строка>'`, `bash|sh -c`, `powershell -Command`,
`cmd /c`, eval — разбор рекурсивный. Текст в аргументах прочих команд (git commit -m, tickets.py --text, echo, grep,
`cat > файл <<EOF`) удалением не считается. Не удалось разобрать (незакрытая кавычка) — прежний регэксп как страховка.

Устройство: этот файл — вход хука (`main`, `check_tool`); разбор и правила лежат в пакете `dguard/`
(config → messages → tools → paths → lexer → words → cmd_scan/publish/writes/git_scan → legacy → policy → push_scope → ceo_signal → checks).
Изменяемые настройки (`LOCAL_ROOTS`, `PROTECTED`, `REMOTE_ROOTS`, `STAGE`, `CLOSED_RE`, …) живут в `dguard.config`.
"""
import json
import os

from dguard.ceo_signal import ceo_signal_write
from dguard.checks import _deny, check, check_file
from dguard.config import configure
from dguard.git_scan import BRANCH_CONSENT_NAMES, CONFIG_LOG_NAMES
from dguard.messages import (REASON_BRANCH_CONSENT_FILE, REASON_CEO_CHARTER, REASON_CEO_SIGNAL, REASON_CONFIG_LOG, REASON_CONSENT, REASON_CRASH,
                             REASON_DECIDE, REASON_IDENTITY, REASON_OWNER_RECORD)
from dguard.charter_cmd import charter_shell_write
from dguard.policy import (charter_table_edit, consent_forge, decide_outside_secretary, dispatcher_role, identity_change,
                           owner_record_write)
from dguard.tools import GUARDED_TOOLS, SHELL_TOOLS, file_target


def check_tool(data, trace=None):
    """Событие PreToolUse (словарь из JSON хука) → причина отказа или None. Чужой инструмент — None. Путь файлового
    инструмента определить нельзя — отказ (схема могла измениться: молча пропускать нельзя)."""
    trace = [] if trace is None else trace
    if not isinstance(data, dict):
        return _deny(trace, "crash", REASON_CRASH.format(e="событие хука не объект JSON"))
    tool = data.get("tool_name")
    if tool not in GUARDED_TOOLS:
        return None
    ti = data.get("tool_input")
    ti = ti if isinstance(ti, dict) else {}
    cwd = str(data.get("cwd") or "")
    try:
        if ceo_signal_write(tool, ti):
            return _deny(trace, "ceo-signal", REASON_CEO_SIGNAL)
        if ceo_signal_write(tool, ti, BRANCH_CONSENT_NAMES):
            return _deny(trace, "ceo-signal", REASON_BRANCH_CONSENT_FILE)
        if ceo_signal_write(tool, ti, CONFIG_LOG_NAMES):
            return _deny(trace, "protected", REASON_CONFIG_LOG)
        role = dispatcher_role()
        if role is None and charter_table_edit(tool, ti):
            return None
        if role is None and (charter := charter_shell_write(tool, ti, cwd)):
            return _deny(trace, "protected", REASON_CEO_CHARTER.format(t=charter))
        if role is None and decide_outside_secretary(tool, ti, data):
            return _deny(trace, "protected", REASON_DECIDE)
        if role and owner_record_write(tool, ti):
            return _deny(trace, "protected", REASON_OWNER_RECORD.format(t="CLAUDE.md / .claude/decisions.md / .claude/goals / .claude/errors / dispatcher/state.json"))
        if role and consent_forge(tool, ti):
            return _deny(trace, "publish", REASON_CONSENT)
        if role and identity_change(tool, ti):
            return _deny(trace, "identity", REASON_IDENTITY)
        if tool in SHELL_TOOLS:
            reason = check(str(ti.get("command") or ""), cwd, trace)
            if tool == "PowerShell" and trace and trace[-1] == ("rm", "deny"):
                trace[-1] = ("ps", "deny")              # инструмент PowerShell: общая ветка удаления — группа ps
            return reason
        path = file_target(ti)
        if path is None:
            return _deny(trace, "crash", REASON_CRASH.format(e=f"{tool}: в tool_input нет пути файла"))
        return check_file(path, cwd, trace)
    except Exception as e:
        return _deny(trace, "crash", REASON_CRASH.format(e=f"{type(e).__name__}: {e}"[:200]))


def journal_guard(project, tool, trace, reason):
    """Строки в `<проект>/.claude/dispatcher/guard.log`: время, инструмент, группа ветки, deny|allow, начало причины.
    Отказы — решающей веткой, разрешения — только у temp/legacy/publish (иначе их нуль ничего не значит): по журналу срез №5
    режет группы с нулём отказов за неделю (решение Судьи). Сбой записи не мешает решению стража."""
    try:
        from datetime import datetime
        path = os.path.join(project, ".claude", "dispatcher", "guard.log")
        ts = datetime.now().astimezone().isoformat(timespec="seconds")
        why = " ".join((reason or "").split())[:90]
        with open(path, "a", encoding="utf-8") as f:
            for group, verdict in trace:
                f.write(f"{ts}\t{tool}\t{group}\t{verdict}\t{why if verdict == 'deny' else ''}\n")
    except Exception:
        pass


def main():
    """PreToolUse-хук: stdin — JSON события; отказ — JSON с permissionDecision=deny. Fail-closed: битое событие и сбой
    стража — отказ с причиной. Тишина — только если инструмент не наш или в проекте нет команды ролей (`.claude/roles`)."""
    import sys

    def deny(reason):
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                 "permissionDecisionReason": reason}}, ensure_ascii=True))
        return 0

    def has_roles(project):
        return os.path.isdir(os.path.join(project, ".claude", "roles"))

    try:
        data = json.load(sys.stdin)
        if not isinstance(data, dict):
            raise ValueError("событие не объект JSON")
    except Exception as e:
        if not has_roles(os.path.abspath(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())):
            return 0
        return deny(REASON_CRASH.format(e=f"событие хука не прочитано: {type(e).__name__}: {e}"[:200]))
    project = None
    trace = []
    try:
        if data.get("tool_name") not in GUARDED_TOOLS:
            return 0
        project = os.path.abspath(os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd") or os.getcwd())
        if not has_roles(project):                                      # в проекте нет команды ролей — молчим
            return 0
        configure(project)
        reason = check_tool(data, trace)
    except Exception as e:
        reason = _deny(trace, "crash", REASON_CRASH.format(e=f"{type(e).__name__}: {e}"[:200]))
    if trace and project:
        journal_guard(project, data.get("tool_name"), trace, reason)
    return deny(reason) if reason else 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
