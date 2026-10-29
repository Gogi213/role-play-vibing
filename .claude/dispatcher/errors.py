#!/usr/bin/env python3
"""Реестр ошибок: исправленная ошибка остаётся исправленной после очистки контекста. Ошибка считается исправленной,
только когда к её записи приложена проверка (тест или скрипт), которая падала до исправления и проходит после; приёмка
Судьи (`tickets.py result … accept`, `accept`) гоняет проверки ВСЕХ исправленных ошибок кодом, а не по тексту устава.
Заводить ошибку может любая роль; исправленной её объявляет только Судья (`RPV_ROLE=judge`) или сессия без роли (CEO,
владелец). Только stdlib.

    errors.py [--project <путь>] add --title "..." [--ticket TK-N] [--what "..."]   # → E-NNN, status: open
    errors.py fix E-NNN --check "<команда>" [--before <коммит>] [--timeout S] [--cwd <каталог>]   # красный → зелёный → fixed
    errors.py run [--cwd <каталог>] [--timeout S]     # проверки всех исправленных ошибок; упала любая — код 1
    errors.py show E-NNN
    errors.py list [--open]

Команда проверки (запускается без оболочки, из корня проекта или `--cwd`):
    python|python3|bash <скрипт под tools/ или .claude/, отслеживаемый git> [арг…]
    python -m pytest <путь или путь::тест, в git> [-k выражение] [-n auto|logical|N] [-q|-v|-x|-s]
    python -m unittest <путь или модуль, в git> [-v|-q|-f|-b]
Красный → зелёный: `fix` сначала гоняет проверку сейчас (в `--cwd` или корне проекта) — должна пройти; потом на коммите «до
исправления» — должна упасть. Коммит «до» — `--before <коммит>`, иначе `added_sha` записи (HEAD проекта в момент `add`); ни того, ни
другого (старая запись) или не git-репозиторий — отказ. «До» идёт во временном `git worktree add --detach` (каталог ОС), в который
копируются файлы проверки из текущего дерева: новой проверки на старом коммите нет, и без копирования она «упала» бы от отсутствия
файла, а не от ошибки. Файлы проверки в `fix` обязаны быть закоммичены: `fixed_sha` — коммит, где они лежат в проверенном виде,
а `check_hash` — отпечаток их содержимого (JSON `{путь: sha256}`, концы строк приведены к LF).
Приёмка (`run`, `gate`) перед запуском каждой проверки сверяет её файлы в каталоге запуска: с отпечатком `check_hash` (git не нужен —
отпечаток переживает rebase и gc); нет отпечатка (запись до 2.0.1) — с версией на `fixed_sha` (`git show`). Отличаются или удалены —
FAIL («менять проверку можно только через Судью, `fix` заново»). Нет ни отпечатка, ни коммита `fixed_sha` — тоже FAIL, не пропуск
(«не сверена … `fix` заново»); коммит есть, а файла проверки на нём нет (старая запись) — сверка пропущена, `run` пишет это в
строке ошибки.
Файлы: `<проект>/.claude/errors/E-NNN.md` (шапка `id/title/ticket/found/added_sha/status/check/fixed/fixed_sha/check_hash/timeout` +
`## Что случилось`). Номер — следующий после наибольшего. Время — `ГГГГ-ММ-ДД ЧЧ:ММ` (местное). Повторный `fix` заменяет проверку.
Записи в `.claude/errors/` напрямую роли запуска (`RPV_ROLE`) не пишут — страж (`delete_guard.py`) закрывает каталог; пишет скрипт.
Отказ приёмки из-за упавшей проверки (регресс) дописывает строку в `<проект>/.claude/dispatcher/regressions.log`: время, тикет,
упавшие ошибки через запятую (поля — через табуляцию); её читает ежедневный отчёт (`report.py`, критерий 7).
У проверки три исхода: прошла, упала (код ≠ 0 — регресс, работа возвращается) и «не запустилась» (не стартовала — OSError — или не
уложилась в срок): не проверено, не вина работы. Отказ тогда велит ждать (`wait at:…`), а не возвращать; в regressions.log
строка с четвёртым полем `infra` (регрессом в отчёте не считается); после 3 таких отказов подряд по тикету — сигнал CEO. Читать
вывод проверки на «BUILD FAILED» и подобное нельзя: его печатает и ошибка компиляции в самой работе.
Исправление, чей `fixed_sha` не предок головы каталога запуска (ветка PR ещё не влита), приёмка пропускает: «исправление ещё не влито».
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hide  # noqa: E402
import project as P  # noqa: E402
import ticket as T  # noqa: E402

ERROR_ID = re.compile(r"^E-(\d{3,})$")
INTERPRETERS = ("python", "python3", "bash")   # то же правило, что у on_met диспетчера (`on_met._on_met_argv`)
SCRIPT_DIRS = ("tools", ".claude")
PYTEST_FLAGS = ("-q", "-qq", "-v", "-vv", "-x", "-s")
PYTEST_WORKERS = re.compile(r"auto|logical|[1-9]\d?")   # значение `-n` (pytest-xdist): параллельный прогон
UNITTEST_FLAGS = ("-q", "-v", "-f", "-b")
CHECK_TIMEOUT_S = 300.0   # одна проверка; дольше — убита деревом процессов
INFRA_MAX = 3             # приёмок подряд с «не запустилась» без сигнала CEO; следующая — сигнал
NOT_MERGED = "исправление ещё не влито"
TAIL = 600     # знаков конца вывода проверки в отказе
SHOWN = 5      # сколько упавших проверок раскрывает отказ приёмки


def errors_dir(root: Path) -> Path:
    return root / ".claude" / "errors"


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def header(path: Path) -> dict:
    fm = P.frontmatter(path.read_text(encoding="utf-8"))
    return fm[0] if fm else {}


def errors(root: Path) -> dict[str, Path]:
    d = errors_dir(root)
    return {p.stem: p for p in sorted(d.glob("E-*.md")) if ERROR_ID.match(p.stem)} if d.is_dir() else {}


def _set_header(path: Path, updates: dict) -> None:
    """Правка полей только в шапке записи (тело с `check:` в тексте не трогаем). Под замком записи: две правки подряд
    (приёмка и сторож) не теряют друг друга."""
    with T.ticket_lock(path):
        text = path.read_text(encoding="utf-8")
        m = P.FRONTMATTER.match(text)
        head = m.group(1)
        for k, v in updates.items():
            head, n = re.subn(rf"^{k}:.*$", lambda _: f"{k}: {v}", head, count=1, flags=re.M)
            if not n:                                  # запись прежней версии без этого поля — дописываем в конец шапки
                head += f"\n{k}: {v}"
        P.atomic_write_text(path, f"---\n{head}\n---\n" + text[m.end():])


def _one_line(s) -> str:
    return " ".join((s or "").split())


# --- допустимая команда проверки ---------------------------------------------------------------

def _tracked(cwd: Path, rel: str) -> bool:
    return hide.git(cwd, "ls-files", "--error-unmatch", "--", rel, timeout=20).returncode == 0


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


def _valued(argv, i: int, module: str) -> bool:
    """`argv[i]` — флаг со значением (`-k выражение`; у pytest ещё `-n auto|logical|N` — xdist): значение — не цель."""
    a = argv[i]
    if i + 1 >= len(argv):
        return False
    return a == "-k" or (module == "pytest" and a == "-n" and bool(PYTEST_WORKERS.fullmatch(argv[i + 1])))


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
                if _valued(argv, i, module):
                    i += 2
                    continue
                if a.startswith("-") and a not in flags:
                    return None, f"флаг `{a}` не допущен (допустимы: -k, {'-n auto|N, ' if module == 'pytest' else ''}{', '.join(flags)})"
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


def run_check(argv, cwd: Path, timeout: float) -> tuple[bool | None, str]:
    """Прогон одной проверки в `cwd`. (True, "") — код 0; (False, причина и конец вывода) — красная (код ≠ 0); (None, причина) —
    «не запустилась»: не стартовала (OSError) или не уложилась в срок. Это «не проверено», а не «проверка упала»: «BUILD FAILED»
    печатает и ошибка компиляции в самой работе, поэтому по тексту вывода инфраструктуру не отличаем. Вывод — во временный
    файл, не в трубу: внук, переживший убитую проверку, не повесит чтение. Таймаут — дерево процессов убито."""
    run_argv = [sys.executable if argv[0] in ("python", "python3") else argv[0], *argv[1:]]   # тот же Python, что у команды
    kw = hide.hidden() if os.name == "nt" else {"start_new_session": True}
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}   # вывод проверки читаем как utf-8, а не в кодировке консоли
    with tempfile.TemporaryFile() as out:
        try:
            proc = subprocess.Popen(run_argv, cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=out,
                                    stderr=subprocess.STDOUT, env=env, **kw)
        except OSError as e:
            return None, f"не запустилась: {type(e).__name__}: {e}"
        try:
            code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            import procs
            procs._kill(proc.pid, tree=True)
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
    return (False if code is not None else None), why + (f"\n{tail}" if tail else "")


def _seconds(v) -> float:
    try:
        return float(v) if v and float(v) > 0 else 0.0
    except ValueError:
        return 0.0


# --- файлы проверки: красный → зелёный и сверка с fixed_sha -------------------------------------

def _git(cwd: Path, *args, timeout: float = 60):
    """git в `cwd` → CompletedProcess (байты) или None, если git не запустился или завис."""
    try:
        return hide.git(cwd, *args, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None


def _git_lines(r) -> list[str]:
    """Пути из вывода `git … -z` (None или код не 0 → пусто)."""
    return [p for p in r.stdout.decode("utf-8", "replace").split("\0") if p] if r is not None and r.returncode == 0 else []


def _module_path(arg: str, cwd: Path) -> str:
    """Цель `python -m unittest`: путь как есть либо модуль `пакет.модуль[.Класс.тест]` → файл или каталог в git (самая
    длинная приставка, которая есть в дереве)."""
    if arg.endswith(".py") or not re.fullmatch(r"[A-Za-z_]\w*(\.[A-Za-z_]\w*)*", arg):
        return arg
    parts = arg.split(".")
    for n in range(len(parts), 0, -1):
        base = "/".join(parts[:n])
        for cand in (base + ".py", base):
            if _tracked(cwd, cand):
                return cand
    return arg


def check_targets(argv, cwd: Path) -> list[str]:
    """Пути проверки из допустимой команды (файлы или каталоги в git): скрипт либо цели `-m pytest|unittest`. Их копируют на
    коммит «до исправления» и сверяют с версией на `fixed_sha`."""
    if argv[0] == "bash" or argv[1] != "-m":
        return [argv[1]]
    module, out, i = argv[2], [], 3
    while i < len(argv):
        a = argv[i]
        if _valued(argv, i, module):
            i += 2
            continue
        if not a.startswith("-"):
            out.append(_module_path(a, cwd) if module == "unittest" else a.split("::")[0])
        i += 1
    return out


def _files_at(root: Path, sha: str, targets) -> list[str] | None:
    """Файлы целей на коммите `sha` (`git ls-tree -r`), пути — от корня проекта. None — коммита в репозитории нет."""
    out: set[str] = set()
    for t in targets:
        r = _git(root, "ls-tree", "-r", "--name-only", "-z", sha, "--", t)
        if r is None or r.returncode != 0:
            return None
        out.update(_git_lines(r))
    return sorted(out)


def _eol(b: bytes) -> bytes:
    return b.replace(b"\r\n", b"\n")   # autocrlf: рабочая копия может быть с CRLF, в git — LF


def _digest(b: bytes) -> str:
    return hashlib.sha256(_eol(b)).hexdigest()


def fingerprint(cwd: Path, targets) -> str:
    """Отпечаток файлов проверки — отслеживаемые git файлы целей в `cwd`: JSON `{путь: sha256 содержимого}` (концы строк — LF).
    Идёт в поле `check_hash` записи; пусто, если читать нечего."""
    out = {}
    for rel in _git_lines(_git(cwd, "ls-files", "-z", "--", *targets)):
        try:
            out[rel] = _digest((cwd / rel).read_bytes())
        except OSError:
            continue
    return json.dumps(out, ensure_ascii=False, sort_keys=True, separators=(",", ":")) if out else ""


def _parse_fingerprint(raw: str) -> dict:
    """Поле `check_hash` → {путь: sha256}; пусто или не разобралось — {} (отпечатка нет)."""
    try:
        d = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return {k: v for k, v in d.items() if isinstance(k, str) and isinstance(v, str)} if isinstance(d, dict) else {}


NOT_VERIFIED = "проверка исправленной ошибки не сверена: нет ни отпечатка, ни коммита — Судья, `errors.py fix` заново"


def _changed(f: str, what: str) -> str:
    return (f"проверка изменена после исправления — менять её можно только через Судью (`errors.py fix` заново): "
            f"{f} {what}")


def tampered(root: Path, cwd: Path, h: dict, argv) -> tuple[str, str]:
    """Файлы проверки в `cwd` против записи → (причина FAIL, пометка). Первым — отпечаток `check_hash` (git не нужен: он переживает
    rebase и gc). Нет отпечатка (запись до 2.0.1) — версия на `fixed_sha` (`git show` из корня проекта). Отличается или удалён —
    причина (проверку не запускаем). Нет ни отпечатка, ни коммита (поле пусто или коммит пропал) — причина `NOT_VERIFIED`: сверить
    нечем, а тихо пропускать нельзя. Коммит есть, а файла проверки на нём нет (старые записи) — сверка пропущена, пометка уходит
    в отчёт."""
    prints = _parse_fingerprint(h.get("check_hash", ""))
    if prints:
        for f, digest in sorted(prints.items()):
            try:
                now = (cwd / f).read_bytes() if (cwd / f).is_file() else None
            except OSError:
                now = None
            if now is None:
                return _changed(f, "удалён"), ""
            if _digest(now) != digest:
                return _changed(f, "отличается от отпечатка, снятого при исправлении"), ""
        return "", ""
    sha = h.get("fixed_sha", "")
    files = _files_at(root, sha, check_targets(argv, cwd)) if sha else None
    if files is None:
        return NOT_VERIFIED, ""
    if not files:
        return "", f"сверка файла проверки пропущена: на {sha} файла проверки нет"
    for f in files:
        r = _git(root, "show", f"{sha}:./{f}")
        try:
            now = (cwd / f).read_bytes() if (cwd / f).is_file() else None
        except OSError:
            now = None
        if r is None or r.returncode != 0:
            what = f"не прочитан на {sha}"
        elif now is None:
            what = "удалён"
        elif _eol(now) != _eol(r.stdout):
            what = f"отличается от версии на {sha}"
        else:
            continue
        return _changed(f, what), ""
    return "", ""


def run_before(argv, cwd: Path, sha: str, timeout: float) -> tuple:
    """Проверка на коммите `sha` («до исправления»): временный `git worktree add --detach` в каталоге ОС, в него копируются файлы
    проверки из `cwd` (на старом коммите новой проверки нет — без копирования она «упала» бы от отсутствия файла, а не от
    ошибки), запуск той же `run_check`, `git worktree remove --force`. -> (прошла?, причина или конец вывода); прошла = None,
    если прогон не удался (причина в тексте)."""
    base = Path(tempfile.mkdtemp(prefix="rpv-before-"))
    tree, added = base / "tree", False
    try:
        r = _git(cwd, "worktree", "add", "--detach", str(tree), sha, timeout=300)
        if r is None or r.returncode != 0:
            return None, f"git worktree add {sha}: " + (_tail(r.stderr) if r is not None else "git не запустился")
        added = True
        for rel in _git_lines(_git(cwd, "ls-files", "-z", "--", *check_targets(argv, cwd))):
            if (cwd / rel).is_file():
                (tree / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(cwd / rel, tree / rel)
        return run_check(argv, tree, timeout)
    except OSError as e:
        return None, f"{type(e).__name__}: {e}"
    finally:
        if added:
            _git(cwd, "worktree", "remove", "--force", str(tree))
        shutil.rmtree(base, ignore_errors=True)
        _git(cwd, "worktree", "prune")


def _not_merged(cwd: Path, sha: str) -> bool:
    """`fixed_sha` известен git, но не предок головы `cwd`: исправление лежит в невлитой ветке — проверять его здесь нечем и рано.
    Неизвестный коммит или не git — не «не влито» (дальше решает обычная сверка)."""
    r = _git(cwd, "merge-base", "--is-ancestor", sha, "HEAD") if sha else None
    return r is not None and r.returncode == 1


def run_all(root: Path, cwd=None, timeout: float | None = None, report=None, infra: list | None = None) -> list:
    """Проверки всех исправленных ошибок в `cwd` (по умолчанию корень проекта). -> [(E-NNN, название, причина)] упавших;
    пусто — всё зелёно или проверять нечего. `report(E-NNN, название, ok, причина)` — по каждой проверке (вывод `run`); у
    прошедшей причина — пометка, если сверку файла проверки с `fixed_sha` пришлось пропустить. Перед запуском файлы проверки в
    `cwd` сверяются с версией на `fixed_sha`: изменены после исправления — FAIL, проверка не запускается. Исправление, чей
    `fixed_sha` не предок головы `cwd`, пропускается («исправление ещё не влито»). Проверка «не запустилась» (`run_check` дал
    None): в `infra`, если список передан (в упавшие не идёт), иначе — в упавшие, как раньше; `report` получает ok=None."""
    cwd = Path(cwd or root)
    bad = []
    for eid, path in errors(root).items():
        h = header(path)
        if h.get("status") != "fixed":
            continue
        title, spec = h.get("title", ""), h.get("check", "")
        if _not_merged(cwd, h.get("fixed_sha", "")):
            ok, note = True, NOT_MERGED
        elif not spec:
            ok, note = False, "исправлена без проверки (check пуст) — приложить проверку или вернуть в open"
        else:
            argv, why = check_argv(spec, cwd)
            if argv is None:
                ok, note = False, f"проверка не допущена: {why}"
            else:
                changed, skipped = tampered(root, cwd, h, argv)
                if changed:
                    ok, note = False, changed
                else:
                    ok, note = run_check(argv, cwd, timeout or _seconds(h.get("timeout")) or CHECK_TIMEOUT_S)
                    note = skipped if ok else note
        if report:
            report(eid, title, ok, note)
        if ok is None and infra is not None:
            infra.append((eid, title, note))
        elif not ok:
            bad.append((eid, title, note))
    return bad


def _log_line(root: Path, tid: str, eids: list, mark: str = "") -> None:
    """Строка regressions.log: время, тикет, ошибки через запятую (мера критерия 7: регрессов должно быть 0) и, если это не
    регресс, пометка: `infra` — проверка не запустилась, `ok` — приёмка прошла (обрывает счёт «подряд»). Сбой записи
    приёмку не ломает."""
    try:
        d = root / ".claude" / "dispatcher"
        d.mkdir(parents=True, exist_ok=True)
        with (d / "regressions.log").open("a", encoding="utf-8") as f:
            f.write("\t".join((P.now_iso(), tid or "-", ",".join(eids),
                               *([mark] if mark else []))) + "\n")
    except OSError:
        pass


def _infra_streak(root: Path, tid: str) -> int:
    """Сколько приёмок тикета подряд закончились «не запустилась»: последние его строки regressions.log с пометкой `infra`
    (строка регресса или `ok` счёт обрывает; строки других тикетов не в счёт)."""
    try:
        lines = (root / ".claude" / "dispatcher" / "regressions.log").read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0
    n = 0
    for ln in reversed(lines):
        f = ln.split("\t")
        if len(f) < 3 or f[1] != (tid or "-"):
            continue
        if f[3:4] != ["infra"]:
            break
        n += 1
    return n


def _infra_refusal(root: Path, tid: str, cwd: Path, infra: list, signal) -> str:
    """Приёмка не смогла проверить: отказ «не проверено, не вина работы», строка infra в regressions.log, а на повторе сверх
    потолка INFRA_MAX — ещё и сигнал CEO через `signal(tid, вид, текст)`."""
    n = _infra_streak(root, tid) + 1
    _log_line(root, tid, [eid for eid, _, _ in infra], "infra")
    lines = [f"{eid} «{title}»: " + note.replace("\n", "\n    ") for eid, title, note in infra[:SHOWN]]
    if len(infra) > SHOWN:
        lines.append(f"…и ещё не запустилось: {len(infra) - SHOWN}")
    text = (f"не проверено — проверка не запустилась (прогон в {cwd}):\n" + "\n".join(lines)
            + "\nНе проверено, не вина работы: не return. Поставь `wait <ID> at:<время ISO, +10–15 мин>` и повтори приёмку "
              f"позже; запись в тикете не сделана. Подряд не запустилась: {n} из {INFRA_MAX} допустимых.")
    if n > INFRA_MAX:
        if signal:
            signal(tid or "-", "проверка-не-запустилась", f"приёмка не смогла проверить {n} раз подряд: "
                   + ", ".join(eid for eid, _, _ in infra) + f" (прогон в {cwd})")
        text += " Потолок исчерпан — CEO позван, не повторяй."
    return text


def gate(root: Path, tid: str = "", signal=None) -> str:
    """Приёмка Судьи: пусто — проверки всех исправленных ошибок зелёные (или их нет), принимать можно; иначе текст отказа:
    регресс (проверка красная) — вернуть работу; «не запустилась» (OSError, таймаут) — не проверено, ждать и повторить, с
    четвёртого раза подряд ещё и `signal` (сигнал CEO). Идут в worktree тикета `.claude/worktrees/<ID>`, если он есть,
    иначе в корне проекта."""
    wt = root / ".claude" / "worktrees" / tid
    cwd = wt if tid and wt.is_dir() else root
    infra = []
    bad = run_all(root, cwd, infra=infra)
    if not bad:
        if infra:
            return _infra_refusal(root, tid, cwd, infra, signal)
        if _infra_streak(root, tid):
            _log_line(root, tid, [], "ok")
        return ""
    _log_line(root, tid, [eid for eid, _, _ in bad])
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
    added = _head_sha(root)   # коммит «до исправления» для `fix`: проверка должна падать на нём
    n = max((int(k[2:]) for k in errors(root)), default=0)
    while True:
        n += 1
        eid = f"E-{n:03d}"
        text = "\n".join(["---", f"id: {eid}", f"title: {title}", f"ticket: {ticket}", f"found: {_now()}",
                          f"added_sha: {added}", "status: open", "check:", "fixed:", "fixed_sha:", "check_hash:", "timeout:", "---",
                          "## Что случилось", (a.what or "").strip() or "не описано", ""])
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
        r = hide.git(cwd, "rev-parse", "--short", "HEAD", text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def _before_commit(a, rec: dict, cwd: Path) -> tuple[str, str]:
    """Коммит «до исправления» → (полный sha, "") или ("", причина отказа): `--before`, иначе `added_sha` записи."""
    rev = (a.before or rec.get("added_sha") or "").strip()
    if not rev:
        return "", ("у записи нет отметки коммита, на котором ошибка была (`added_sha` — записи, заведённые до V2.0): укажи "
                    "--before <коммит> — проверка обязана падать на нём")
    if rev.startswith("-"):
        return "", f"--before {rev!r}: нужен коммит"
    r = _git(cwd, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}")
    sha = r.stdout.decode("utf-8", "replace").strip() if r is not None and r.returncode == 0 else ""
    return (sha, "") if sha else ("", f"коммит «до исправления» {rev} не найден в репозитории: --before <коммит>")


def cmd_fix(a, root: Path) -> int:
    role = P.caller_role(root)  # RPV_ROLE, а стёрта — по дереву процессов: запуск роли CEO не притворится
    if role and role != "judge":
        print(f"fix: исправленной ошибку объявляет Судья (запуск роли {role} — не может): сдай работу "
              f"`tickets.py result <ID> pr|done --why \"...\"` с указанием проверки (`--check` и, если ошибка заведена до "
              f"V2.0, `--before <коммит>`) — Судья проверит, что она падала до исправления, и объявит `errors.py fix`",
              file=sys.stderr)
        return 1
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
    git = _git(cwd, "rev-parse", "--is-inside-work-tree")
    if git is None or git.returncode != 0:
        print(f"fix: {cwd} — не git-репозиторий: «красный → зелёный» проверить нечем (проверка должна падать на коммите до "
              f"исправления), ошибка остаётся открытой", file=sys.stderr)
        return 1
    argv, why = check_argv(spec, cwd) if spec else (None, "нужна --check — команда проверки")
    if argv is None:
        print(f"fix: проверка не допущена: {why}. Допустимо: python|python3|bash <скрипт под tools/ или .claude/ из git>; "
              f"python -m pytest|unittest <тест из git>", file=sys.stderr)
        return 1
    if a.timeout is not None and a.timeout <= 0:
        print("--timeout: секунд больше нуля", file=sys.stderr)
        return 1
    before, why = _before_commit(a, header(path), cwd)
    if not before:
        print(f"fix: {why}; {eid} остаётся открытой", file=sys.stderr)
        return 1
    ok, note = run_check(argv, cwd, a.timeout or CHECK_TIMEOUT_S)
    if not ok:
        print(f"fix: проверка не прошла — {eid} остаётся открытой (исправленной ошибка считается, только когда проверка "
              f"зелёная):\n{note}", file=sys.stderr)
        return 1
    targets = check_targets(argv, cwd)
    st = _git(cwd, "status", "--porcelain", "--untracked-files=no", "--", *targets)
    if st is None or st.returncode != 0 or st.stdout.strip():
        print(f"fix: файлы проверки ({', '.join(targets)}) не закоммичены в {cwd}: закоммить их — приёмка сверяет проверку "
              f"с версией на коммите исправления (fixed_sha), а не с рабочей копией; {eid} остаётся открытой", file=sys.stderr)
        return 1
    prints = fingerprint(cwd, targets)   # файлы чисты относительно HEAD: отпечаток снят с того, что лежит в fixed_sha
    red, note = run_before(argv, cwd, before, a.timeout or CHECK_TIMEOUT_S)
    if red is None:
        print(f"fix: прогон проверки на коммите «до» {before[:10]} не удался — {note}; {eid} остаётся открытой", file=sys.stderr)
        return 1
    if red:
        print(f"fix: проверка не ловит ошибку: на {before[:10]} она уже зелёная — {eid} остаётся открытой. Проверка обязана "
              f"падать до исправления и проходить после: перепиши её так, чтобы она падала на той самой ошибке "
              f"(другой коммит «до» — --before <коммит>)", file=sys.stderr)
        return 1
    _set_header(path, {"status": "fixed", "check": spec, "fixed": _now(), "fixed_sha": _head_sha(cwd),
                       "check_hash": prints, "timeout": f"{a.timeout:g}" if a.timeout else ""})
    print(f"{eid}: fixed, проверка: {spec} (падала на {before[:10]}, проходит на {_head_sha(cwd) or 'рабочей копии'})")
    return 0


def cmd_run(a, root: Path) -> int:
    cwd = Path(a.cwd).expanduser().resolve() if a.cwd else root
    seen = []

    def report(eid, title, ok, note):
        seen.append(eid)
        print(f"{eid} ok — {title}" + (f" ({note})" if note else "") if ok
              else f"{eid} {'FAIL' if ok is False else 'НЕ ЗАПУСТИЛАСЬ'} — {title}: " + note.replace("\n", "\n    "))
    infra = []
    bad = run_all(root, cwd, a.timeout, report, infra)
    if not seen:
        print("исправленных ошибок с проверками нет")
    return 1 if bad else 2 if infra else 0


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
    fx = sub.add_parser("fix", help="только Судья: проверка падала до исправления и зелёная сейчас → status: fixed")
    fx.add_argument("id")
    fx.add_argument("--check", required=True, help='команда проверки, например "python -m pytest tests/test_x.py"')
    fx.add_argument("--before", default=None, help="коммит до исправления: проверка обязана падать на нём "
                                                   "(по умолчанию added_sha записи — HEAD в момент add)")
    fx.add_argument("--timeout", type=float, default=None, help=f"секунд на проверку (по умолчанию {CHECK_TIMEOUT_S:g})")
    fx.add_argument("--cwd", default=None, help="каталог запуска (по умолчанию корень проекта), например worktree тикета")
    fx.set_defaults(func=cmd_fix)
    rn = sub.add_parser("run", help="проверки всех исправленных ошибок; упала любая — код 1, не запустилась (OSError, таймаут) — 2")
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
