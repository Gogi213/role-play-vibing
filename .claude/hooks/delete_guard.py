"""Страж PreToolUse (`hooks/hooks.json`: Bash, PowerShell, Monitor, Write, Edit, MultiEdit, NotebookEdit): удалять и перезаписывать
можно только в своей папке (владелец 27.09). Вход — `main()` (JSON события со stdin), `check_tool(событие)` → причина отказа или
None, `check(cmd, cwd)` для команды. Корень проекта — `CLAUDE_PROJECT_DIR` (иначе cwd); нет `.claude/roles` — молчит.

Своя папка: проект, его worktree (`<проект>/.claude/worktrees/*`), scratchpad сессии, временный каталог; перезапись — ещё
автопамять проекта. Защищённые пути `protected_paths` из `<проект>/.claude/rpv.json` — никому. Цель, которую нельзя проверить
(переменная без буквального префикса, список из конвейера/xargs, путь из кода), — отказ: переписать явным путём.

Что разбирается: простая команда (после деления по `;` `&&` `||` `|` `&`, `$(…)`, кавычкам, heredoc), у которой командное
слово — rm/rmdir/unlink/shred/del/erase/rd/Remove-Item/ri, `find … -delete|-exec rm`, `git clean`, исполняемый Python (`-c`,
stdin) с shutil.rmtree/os.remove/…; перезапись и усечение — `> файл`, `truncate`, `dd of=`, `tee`, `cp`/`mv` поверх существующего,
командлеты Out-File/Set-Content. Через обёртки (sudo/env/nohup/xargs/timeout/…), `bash|sh -c`, `powershell -Command`, `cmd /c`,
eval разбор рекурсивный. `ssh`/`scp`/`rsync` и текст в аргументах прочих команд (git commit -m, echo, grep) не разбираются.
Не удалось разобрать (незакрытая кавычка) — прежний регэксп как страховка; сбой самого стража — отказ, не пропуск.

Git: `git reset --hard`, `git clean -f`, откат всего дерева (`checkout .`, `restore .`), `checkout -f` — только в worktree проекта
и scratch; `git push --force|+ветка|--delete|:ветка|--mirror` и `git branch -D main|master` — никогда. Откат одного файла
(`checkout -- файл`, `restore файл`) и `branch -D` локальной ветки — свободно.

Запуску роли диспетчера (`RPV_ROLE`): файловые инструменты вне корней ограничены (существующий файл — отказ), `.git`, настройки
Claude Code и плагина, слово владельца (CLAUDE.md, реестр решений, цели, реестр ошибок) закрыты для записи; публикации (`gh release
create|upload|edit`, `gh pr merge`, `git push` в main/master и теги) — только с `allow: publish` в шапке тикета (`RPV_TICKET`, ставит
CEO командой `tickets.py allow`). Сессия CEO не правит уставы `.claude/roles/*.md` и `rpv.json` файловыми инструментами и
перезаписью (строки таблицы зон в README — можно).

Устройство: этот файл — вход хука; разбор и правила — пакет `dguard/` (config → messages → tools → paths → lexer → words →
cmd_scan/publish/writes/git_scan → legacy → policy → checks). Читающие команды (ls, cat, git status, …) пропускаются до импорта
пакета — `quick_pass`. Настройки (`LOCAL_ROOTS`, `PROTECTED`, …) живут в `dguard.config`.
"""
import json
import os
import sys

READ_CMDS = frozenset({"ls", "cat", "head", "tail", "grep", "egrep", "fgrep", "rg", "wc", "echo", "pwd", "find", "git"})
GIT_READ = frozenset({"status", "log", "diff", "show", "branch", "rev-parse", "ls-files", "blame", "describe"})
BRANCH_READ = frozenset({"-a", "-r", "-v", "-vv", "--all", "--remotes", "--verbose", "--list", "--show-current", "--merged", "--no-merged"})
FIND_WRITE = frozenset({"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprint0", "-fprintf", "-fls"})
SHELL_MARKS = (";", "&", ">", "<", "`", "$(", "\n", "\r", "\\")


def read_only_segment(words):
    """Одна простая команда читает и ничего не пишет: ls/cat/head/tail/grep/rg/wc/echo, `find` без -delete/-exec,
    `git status|log|diff|show|branch` без записи."""
    if not words:
        return False
    name = words[0].replace("\\", "/").rsplit("/", 1)[-1].lower()
    if name not in READ_CMDS or "=" in words[0]:
        return False
    args = words[1:]
    if any(a.startswith(("--output", "--pre")) for a in args):
        return False
    if name == "find":
        return not FIND_WRITE.intersection(args)
    if name == "git":
        if len(words) < 2 or words[1] not in GIT_READ:
            return False
        return words[1] != "branch" or all(a in BRANCH_READ for a in words[2:])
    return True


def quick_pass(data):
    """Событие заведомо безопасно: Bash/Monitor с цепочкой читающих команд (конвейер `|` допустим) — без разбора."""
    if not isinstance(data, dict) or data.get("tool_name") not in ("Bash", "Monitor"):
        return False
    ti = data.get("tool_input")
    cmd = ti.get("command") if isinstance(ti, dict) else None
    if not isinstance(cmd, str) or not cmd.strip() or any(m in cmd for m in SHELL_MARKS):
        return False
    return all(read_only_segment(seg.split()) for seg in cmd.split("|"))


_STDIN = None
if __name__ == "__main__":
    _STDIN = sys.stdin.read()
    try:
        if quick_pass(json.loads(_STDIN)):
            sys.exit(0)
    except ValueError:
        pass

from dguard.checks import _deny, check, check_file  # noqa: E402  (после быстрого выхода: импорт пакета ≈150 мс)
from dguard.config import configure  # noqa: E402
from dguard.messages import REASON_CRASH  # noqa: E402
from dguard.policy import charter_table_edit, dispatcher_role  # noqa: E402
from dguard.tools import GUARDED_TOOLS, SHELL_TOOLS, file_target  # noqa: E402


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
        if dispatcher_role() is None and charter_table_edit(tool, ti):
            return None                                 # строки таблицы зон README уставов правит и CEO
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
    Отказы — решающей веткой, разрешения — только у temp/legacy/publish (иначе их нуль ничего не значит). Сбой записи
    не мешает решению стража."""
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

    def deny(reason):
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                 "permissionDecisionReason": reason}}, ensure_ascii=True))
        return 0

    def has_roles(project):
        return os.path.isdir(os.path.join(project, ".claude", "roles"))

    try:
        data = json.loads(_STDIN) if _STDIN is not None else json.load(sys.stdin)
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
    sys.exit(main())
