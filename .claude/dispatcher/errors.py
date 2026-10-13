#!/usr/bin/env python3
"""Реестр ошибок: исправленная ошибка остаётся исправленной после очистки контекста. Ошибка считается исправленной,
только когда к её записи приложена проверка (тест или скрипт); приёмка Судьи (`tickets.py result … accept`, `accept`)
гоняет проверки ВСЕХ исправленных ошибок кодом, а не по тексту устава. Заводить и закрывать может любая роль. Только stdlib.

    errors.py [--project <путь>] add --title "..." [--ticket TK-N] [--what "..."]   # → E-NNN, status: open
    errors.py fix E-NNN --check "<команда>" [--timeout S] [--cwd <каталог>]   # прогон проверки; код 0 → status: fixed
    errors.py run [--cwd <каталог>] [--timeout S]     # проверки всех исправленных ошибок; упала любая — код 1
    errors.py show E-NNN
    errors.py list [--open]

Команда проверки (запускается без оболочки, из корня проекта или `--cwd`):
    python|python3|bash <скрипт под tools/ или .claude/, отслеживаемый git> [арг…]
    python -m pytest <путь или путь::тест, в git> [-k выражение] [-q|-v|-x|-s]
    python -m unittest <путь или модуль, в git> [-v|-q|-f|-b]
Файлы: `<проект>/.claude/errors/E-NNN.md` (шапка `id/title/ticket/found/status/check/fixed/fixed_sha/timeout` +
`## Что случилось`). Номер — следующий после наибольшего. Время — `ГГГГ-ММ-ДД ЧЧ:ММ` (местное). Повторный `fix` заменяет проверку.
Отказ приёмки из-за упавшей проверки (регресс) дописывает строку в `<проект>/.claude/dispatcher/regressions.log`: время, тикет,
упавшие ошибки через запятую (поля — через табуляцию); её читает ежедневный отчёт (`report.py`, критерий 7).
"""
from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hide  # noqa: E402
import project as P  # noqa: E402

ERROR_ID = re.compile(r"^E-(\d{3,})$")
INTERPRETERS = ("python", "python3", "bash")   # то же правило, что у on_met диспетчера (`dispatch._on_met_argv`)
SCRIPT_DIRS = ("tools", ".claude")
PYTEST_FLAGS = ("-q", "-qq", "-v", "-vv", "-x", "-s")
UNITTEST_FLAGS = ("-q", "-v", "-f", "-b")
CHECK_TIMEOUT_S = float(P.env("ERRORS_TIMEOUT_S", "300"))   # одна проверка; дольше — убита деревом процессов
TAIL = 600     # знаков конца вывода проверки в отказе
SHOWN = 5      # сколько упавших проверок раскрывает отказ приёмки


def errors_dir(root: Path) -> Path:
    return root / ".claude" / "errors"


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def header(path: Path) -> dict:
    m = re.match(r"^---\n(.*?)\n---\n", path.read_text(encoding="utf-8"), re.S)
    out = {}
    for ln in (m.group(1).splitlines() if m else []):
        k, _, v = ln.partition(":")
        out[k.strip()] = v.strip()
    return out


def errors(root: Path) -> dict[str, Path]:
    d = errors_dir(root)
    return {p.stem: p for p in sorted(d.glob("E-*.md")) if ERROR_ID.match(p.stem)} if d.is_dir() else {}


def _set_header(path: Path, updates: dict) -> None:
    """Правка полей только в шапке записи (тело с `check:` в тексте не трогаем)."""
    text = path.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    head = m.group(1)
    for k, v in updates.items():
        head = re.sub(rf"^{k}:.*$", lambda _: f"{k}: {v}", head, count=1, flags=re.M)
    path.write_text(f"---\n{head}\n---\n" + text[m.end():], encoding="utf-8")


def _one_line(s) -> str:
    return " ".join((s or "").split())


# --- допустимая команда проверки ---------------------------------------------------------------

def _tracked(cwd: Path, rel: str) -> bool:
    r = hide.run(["git", "-C", str(cwd), "ls-files", "--error-unmatch", "--", rel], capture_output=True, timeout=20)
    return r.returncode == 0


def _rel_ok(rel: str) -> bool:
    """Путь внутри проекта: относительный, слеши прямые, без `..`, диска и ведущего `-`."""
    return (bool(rel) and "\\" not in rel and not rel.startswith(("/", "-")) and ".." not in rel.split("/")
            and not re.match(r"^[A-Za-z]:", rel))


def _target_problem(target: str, module: str, cwd: Path) -> str:
    """Тест из команды `-m pytest|unittest` — файл или каталог из git (у pytest — с `::тест`); у unittest ещё модуль
    `пакет.модуль`, чей верхний пакет (`пакет.py` или каталог) в git. Пусто — годится."""
    path = target.split("::")[0] if module == "pytest" else target
    if module == "unittest" and not path.endswith(".py") and re.fullmatch(r"[A-Za-z_]\w*(\.[A-Za-z_]\w*)*", path):
        top = path.split(".")[0]
        return "" if _tracked(cwd, f"{top}.py") or _tracked(cwd, top) else f"модуль `{target}` не из файлов под git"
    if not _rel_ok(path):
        return f"путь `{target}` — не относительный путь внутри проекта (слеши прямые)"
    return "" if _tracked(cwd, path) else f"`{path}` не отслеживается git"


def check_argv(spec: str, cwd: Path):
    """(argv, None) | (None, причина отказа). Оболочки нет — argv идёт в процесс как есть."""
    try:
        argv = shlex.split(spec, posix=True)
    except ValueError as e:
        return None, f"разбор команды: {e}"
    if len(argv) < 2 or argv[0] not in INTERPRETERS:
        return None, f"argv[0] должен быть из {INTERPRETERS}, дальше — скрипт или `-m pytest|unittest <тест>`"
    try:
        if argv[0] != "bash" and argv[1] == "-m":
            module = argv[2] if len(argv) > 2 else ""
            if module not in ("pytest", "unittest"):
                return None, "после `-m` допустимы только pytest и unittest"
            flags = PYTEST_FLAGS if module == "pytest" else UNITTEST_FLAGS
            targets, i = [], 3
            while i < len(argv):
                a = argv[i]
                if a == "-k" and i + 1 < len(argv):
                    i += 2
                    continue
                if a.startswith("-") and a not in flags:
                    return None, f"флаг `{a}` не допущен (допустимы: -k, {', '.join(flags)})"
                if not a.startswith("-"):
                    targets.append(a)
                i += 1
            if not targets:
                return None, f"`-m {module}` без теста: укажите путь или модуль из git"
            for t in targets:
                if why := _target_problem(t, module, cwd):
                    return None, why
            return argv, None
        rel = argv[1]
        if not _rel_ok(rel) or rel.split("/")[0] not in SCRIPT_DIRS:
            return None, f"скрипт `{rel}` вне tools/ и .claude/ (пути — с прямыми слешами)"
        if not _tracked(cwd, rel):
            return None, f"скрипт `{rel}` не отслеживается git"
        return argv, None
    except (OSError, subprocess.SubprocessError) as e:
        return None, f"git ls-files: {type(e).__name__}: {e}"


# --- прогон ------------------------------------------------------------------------------------

def _tail(b, limit: int = TAIL) -> str:
    return (b or b"").decode("utf-8", "replace").strip()[-limit:]


def run_check(argv, cwd: Path, timeout: float) -> tuple[bool, str]:
    """Прогон одной проверки в `cwd`. (True, "") — код 0; иначе (False, причина и конец вывода). Вывод — во временный
    файл, не в трубу: внук, переживший убитую проверку, не повесит чтение. Таймаут — дерево процессов убито."""
    run_argv = [sys.executable if argv[0] in ("python", "python3") else argv[0], *argv[1:]]   # тот же Python, что у команды
    kw = hide.hidden() if os.name == "nt" else {"start_new_session": True}
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}   # вывод проверки читаем как utf-8, а не в кодировке консоли
    with tempfile.TemporaryFile() as out:
        try:
            proc = subprocess.Popen(run_argv, cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=out,
                                    stderr=subprocess.STDOUT, env=env, **kw)
        except OSError as e:
            return False, f"не запустилась: {type(e).__name__}: {e}"
        try:
            code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            import dispatch
            dispatch._kill_tree(proc.pid)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
            code = None
        out.seek(0)
        tail = _tail(out.read())
    if code == 0:
        return True, ""
    why = f"таймаут {timeout:g} с, дерево процессов убито" if code is None else f"код {code}"
    return False, why + (f"\n{tail}" if tail else "")


def _seconds(v) -> float:
    try:
        return float(v) if v and float(v) > 0 else 0.0
    except ValueError:
        return 0.0


def run_all(root: Path, cwd=None, timeout: float | None = None, report=None) -> list:
    """Проверки всех исправленных ошибок в `cwd` (по умолчанию корень проекта). -> [(E-NNN, название, причина)] упавших;
    пусто — всё зелёно или проверять нечего. `report(E-NNN, название, ok, причина)` — по каждой проверке (вывод `run`)."""
    cwd = Path(cwd or root)
    bad = []
    for eid, path in errors(root).items():
        h = header(path)
        if h.get("status") != "fixed":
            continue
        title, spec = h.get("title", ""), h.get("check", "")
        if not spec:
            ok, note = False, "исправлена без проверки (check пуст) — приложить проверку или вернуть в open"
        else:
            argv, why = check_argv(spec, cwd)
            ok, note = (False, f"проверка не допущена: {why}") if argv is None else run_check(
                argv, cwd, timeout or _seconds(h.get("timeout")) or CHECK_TIMEOUT_S)
        if report:
            report(eid, title, ok, note)
        if not ok:
            bad.append((eid, title, note))
    return bad


def _log_refusal(root: Path, tid: str, bad: list) -> None:
    """Отказ приёмки из-за регресса — строка «время · тикет · упавшие ошибки» в regressions.log (мера критерия 7: должно
    быть 0). Сбой записи приёмку не ломает."""
    try:
        d = root / ".claude" / "dispatcher"
        d.mkdir(parents=True, exist_ok=True)
        with (d / "regressions.log").open("a", encoding="utf-8") as f:
            f.write("\t".join((datetime.now().astimezone().isoformat(timespec="seconds"), tid or "-",
                               ",".join(eid for eid, _, _ in bad))) + "\n")
    except OSError:
        pass


def gate(root: Path, tid: str = "") -> str:
    """Приёмка Судьи: пусто — проверки всех исправленных ошибок зелёные (или их нет), принимать можно; иначе текст отказа.
    Идут в worktree тикета `.claude/worktrees/<ID>`, если он есть, иначе в корне проекта."""
    wt = root / ".claude" / "worktrees" / tid
    cwd = wt if tid and wt.is_dir() else root
    bad = run_all(root, cwd)
    if not bad:
        return ""
    _log_refusal(root, tid, bad)
    lines = [f"{eid} «{title}»: " + note.replace("\n", "\n    ") for eid, title, note in bad[:SHOWN]]
    if len(bad) > SHOWN:
        lines.append(f"…и ещё упавших: {len(bad) - SHOWN}")
    return (f"регрессия — упали проверки исправленных ошибок (прогон в {cwd}):\n" + "\n".join(lines)
            + "\nПринять нельзя: верни работу автору (`result <ID> return`) с этими номерами; запись в тикете не сделана. "
              "Прогон — `errors.py run`.")


# --- команды -----------------------------------------------------------------------------------

def _norm(eid: str) -> str:
    m = re.fullmatch(r"E-(\d+)", (eid or "").strip().upper())
    return f"E-{int(m.group(1)):03d}" if m else (eid or "")


def cmd_add(a, root: Path) -> int:
    title = _one_line(a.title)
    if not title:
        print("нужен --title", file=sys.stderr)
        return 1
    ticket = (a.ticket or "").strip().upper()
    if ticket and not re.fullmatch(r"TK-\d+", ticket):
        print(f"--ticket {a.ticket!r}: нужен номер тикета TK-N", file=sys.stderr)
        return 1
    d = errors_dir(root)
    d.mkdir(parents=True, exist_ok=True)
    n = max((int(k[2:]) for k in errors(root)), default=0)
    while True:
        n += 1
        eid = f"E-{n:03d}"
        text = "\n".join(["---", f"id: {eid}", f"title: {title}", f"ticket: {ticket}", f"found: {_now()}", "status: open",
                          "check:", "fixed:", "fixed_sha:", "timeout:", "---", "## Что случилось",
                          (a.what or "").strip() or "не описано", ""])
        try:
            with (d / f"{eid}.md").open("x", encoding="utf-8") as w:   # номер занят другой ролью одновременно — следующий
                w.write(text)
            break
        except FileExistsError:
            continue
    print(eid)
    return 0


def _head_sha(cwd: Path) -> str:
    try:
        r = hide.run(["git", "-C", str(cwd), "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def cmd_fix(a, root: Path) -> int:
    eid = _norm(a.id)
    path = errors(root).get(eid)
    if not path:
        print(f"нет ошибки {a.id}", file=sys.stderr)
        return 1
    spec = _one_line(a.check)
    cwd = Path(a.cwd).expanduser().resolve() if a.cwd else root
    if not cwd.is_dir():
        print(f"--cwd {cwd}: нет такого каталога", file=sys.stderr)
        return 1
    argv, why = check_argv(spec, cwd) if spec else (None, "нужна --check — команда проверки")
    if argv is None:
        print(f"fix: проверка не допущена: {why}. Допустимо: python|python3|bash <скрипт под tools/ или .claude/ из git>; "
              f"python -m pytest|unittest <тест из git>", file=sys.stderr)
        return 1
    if a.timeout is not None and a.timeout <= 0:
        print("--timeout: секунд больше нуля", file=sys.stderr)
        return 1
    ok, note = run_check(argv, cwd, a.timeout or CHECK_TIMEOUT_S)
    if not ok:
        print(f"fix: проверка не прошла — {eid} остаётся открытой (исправленной ошибка считается, только когда проверка "
              f"зелёная):\n{note}", file=sys.stderr)
        return 1
    _set_header(path, {"status": "fixed", "check": spec, "fixed": _now(), "fixed_sha": _head_sha(cwd),
                       "timeout": f"{a.timeout:g}" if a.timeout else ""})
    print(f"{eid}: fixed, проверка: {spec}")
    return 0


def cmd_run(a, root: Path) -> int:
    cwd = Path(a.cwd).expanduser().resolve() if a.cwd else root
    seen = []

    def report(eid, title, ok, note):
        seen.append(eid)
        print(f"{eid} ok — {title}" if ok else f"{eid} FAIL — {title}: " + note.replace("\n", "\n    "))
    bad = run_all(root, cwd, a.timeout, report)
    if not seen:
        print("исправленных ошибок с проверками нет")
    return 1 if bad else 0


def cmd_show(a, root: Path) -> int:
    path = errors(root).get(_norm(a.id))
    if not path:
        print(f"нет ошибки {a.id}", file=sys.stderr)
        return 1
    print(path.read_text(encoding="utf-8").rstrip())
    return 0


def cmd_list(a, root: Path) -> int:
    for eid, path in errors(root).items():
        h = header(path)
        if a.open and h.get("status") != "open":
            continue
        print(f"{eid} [{h.get('status', '?')}] {h.get('title', '')}" + (f" ({h['ticket']})" if h.get("ticket") else ""))
    return 0


def main(argv=None) -> int:
    P.utf8_stdio()
    ap = argparse.ArgumentParser(prog="errors.py", description=__doc__.split("\n\n")[0])
    ap.add_argument("--project", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    ad = sub.add_parser("add", help="новая ошибка → E-NNN в .claude/errors/, status: open")
    ad.add_argument("--title", required=True)
    ad.add_argument("--ticket", default=None, help="тикет, в котором найдена (TK-N)")
    ad.add_argument("--what", default=None, help="что случилось, свободным текстом")
    ad.set_defaults(func=cmd_add)
    fx = sub.add_parser("fix", help="проверка зелёная → status: fixed; красная — ошибка остаётся открытой")
    fx.add_argument("id")
    fx.add_argument("--check", required=True, help='команда проверки, например "python -m pytest tests/test_x.py"')
    fx.add_argument("--timeout", type=float, default=None, help=f"секунд на проверку (по умолчанию {CHECK_TIMEOUT_S:g})")
    fx.add_argument("--cwd", default=None, help="каталог запуска (по умолчанию корень проекта), например worktree тикета")
    fx.set_defaults(func=cmd_fix)
    rn = sub.add_parser("run", help="проверки всех исправленных ошибок; упала любая — код 1")
    rn.add_argument("--cwd", default=None)
    rn.add_argument("--timeout", type=float, default=None, help="секунд на каждую проверку (поверх записи ошибки)")
    rn.set_defaults(func=cmd_run)
    s = sub.add_parser("show")
    s.add_argument("id")
    s.set_defaults(func=cmd_show)
    li = sub.add_parser("list")
    li.add_argument("--open", action="store_true", help="только открытые ошибки")
    li.set_defaults(func=cmd_list)
    a = ap.parse_args(argv)
    try:
        root = Path(a.project).expanduser().resolve() if a.project else P.resolve_project()
    except P.ProjectNotFound:
        print(f"[errors] ошибка: {P.NOT_FOUND_HINT}", file=sys.stderr)
        return 2
    return a.func(a, root)


if __name__ == "__main__":
    sys.exit(main())
