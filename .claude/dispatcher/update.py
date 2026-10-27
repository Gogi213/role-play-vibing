"""/rpv-update — обновить плагин целиком одной командой (В-13).

    python <плагин>/.claude/dispatcher/update.py [--project P] [--ref В-N]

Шаги по порядку: `claude plugin marketplace update` → `claude plugin update` → из НОВОЙ версии (путь — из
`installed_plugins.json`) `start.py`, `supervise.py --install`, `tickets.py charter sync --ref В-N` (слово владельца
уже в реестре; без `--ref` шаг — FAIL, уставы не тронуты), `doctor.py`. Версия не изменилась — служб это не касается:
start, присмотр и doctor всё равно идут, синк уставов пропущен. Итог — одна таблица `OK/FAIL/—`, любой FAIL doctor
назван как есть. Сессия Claude Code остаётся на старой версии, пока её не перезапустят: об этом последняя строка.
Код возврата 0 — все шаги OK, 1 — есть FAIL, 2 — проект не найден."""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import charter  # noqa: E402
import hide  # noqa: E402
import plugin_version as PV  # noqa: E402
import project as P  # noqa: E402
from doctor import FAIL, OFF, OK, render  # noqa: E402

CODE_DIR = Path(__file__).resolve().parent
MARKETPLACE = "role-play-vibing"
PLUGIN = f"{MARKETPLACE}@{MARKETPLACE}"
RESTART = "Перезапустите Claude Code — до перезапуска сессия на старой версии."


def install_entry(project: Path) -> dict | None:
    """Установка плагина для проекта — `plugin_version.plugin_entry` (то же правило, что у хуков и doctor)."""
    return PV.plugin_entry(project, MARKETPLACE)


def run_cmd(cmd: list, timeout: float) -> tuple:
    """(код возврата, вывод); не запустилась или зависла — код 127 и причина."""
    try:
        r = hide.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        return 127, f"{type(e).__name__}: {e}"
    return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()


def _last(out: str) -> str:
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    return lines[-1][:200] if lines else ""


def _fail_names(out: str) -> str:
    return ", ".join(ln.split(None, 1)[1].split("  ")[0].strip() for ln in out.splitlines() if ln.startswith("FAIL"))


def update(project: Path, run=run_cmd, installed=install_entry, ref: str = "",
           python: str = sys.executable, claude: str = "claude") -> tuple:
    """(строки таблицы, версия после обновления). Сбой обновления самого плагина — дальше не идём."""
    rows = []

    def step(name: str, rc: int, out: str, ok_text: str = "") -> bool:
        rows.append((name, OK if rc == 0 else FAIL, ok_text or _last(out) or f"код {rc}"))
        return rc == 0

    before = installed(project) or {}
    old = str(before.get("version") or "?")
    scope = ["--scope", before["scope"]] if before.get("scope") else []
    if not step("marketplace", *run([claude, "plugin", "marketplace", "update", MARKETPLACE], 300)):
        return rows, old
    if not step("плагин", *run([claude, "plugin", "update", PLUGIN, *scope], 300)):
        return rows, old
    after = installed(project) or {}
    new, root = str(after.get("version") or "?"), Path(str(after.get("installPath") or ""))
    code = root / ".claude" / "dispatcher"
    if not (code / "start.py").is_file():
        rows.append(("плагин", FAIL, f"нет {code / 'start.py'} — установка {new} не найдена в installed_plugins.json"))
        return rows, new
    rows[-1] = ("плагин", OK, f"{old} → {new}" if new != old else f"уже последняя версия {new}")
    proj = ["--project", str(project)]
    step("start", *run([python, str(code / "start.py"), *proj], 120), "диспетчер и сторож запущены из " + new)
    step("присмотр ОС", *run([python, str(code / "supervise.py"), "--install", *proj], 120), "перезаписан из " + new)
    if new == old:
        rows.append(("уставы", OFF, "версия не менялась — синк не нужен"))
    elif not ref:
        rows.append(("уставы", FAIL, "не синхронизированы: нужен --ref В-N (слово владельца об обновлении — в реестр, "
                                      "потом `update.py --ref В-N`)"))
    else:
        step("уставы", *run([python, str(code / "tickets.py"), *proj, "charter", "sync", "--ref", ref,
                              "--why", f"обновление плагина {old} → {new} (/rpv-update)"], 120), f"charter sync --ref {ref}")
    rc, out = run([python, str(code / "doctor.py"), *proj], 180)
    bad = _fail_names(out)
    rows.append(("doctor", OK if rc == 0 else FAIL, "ни одного FAIL" if rc == 0 else f"FAIL: {bad or _last(out)}"))
    if rc != 0:
        rows.append(("doctor, вывод", OFF, "\n" + out))
    return rows, new


def main(argv=None) -> int:
    P.utf8_stdio()
    ap = argparse.ArgumentParser(prog="update.py", description="Обновить плагин целиком: плагин, службы, присмотр, уставы")
    ap.add_argument("--project")
    ap.add_argument("--ref", default="", help="решение владельца В-N об обновлении (нужно для синка уставов)")
    a = ap.parse_args(sys.argv[1:] if argv is None else argv)
    try:
        project = Path(P.resolve_project(["--project", a.project] if a.project else []))
    except P.ProjectNotFound:
        print(P.NOT_FOUND_HINT, file=sys.stderr)
        return 2
    claude = os.environ.get("CLAUDE_BIN") or shutil.which("claude")
    if not claude:
        print(render([("claude", FAIL, "`claude` не найден в PATH и CLAUDE_BIN не задан")]))
        return 1
    running = charter.plugin_version(CODE_DIR.parents[1])
    rows, new = update(project, ref=a.ref.strip(), claude=claude)
    print(render(rows))
    bad = [r[0] for r in rows if r[1] == FAIL]
    print(f"\nНЕ ГОТОВО — FAIL: {', '.join(bad)}." if bad else f"\nГотово: плагин {new}, службы и присмотр на нём.")
    print(RESTART if new != running else f"Эта сессия уже на {new}.")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
