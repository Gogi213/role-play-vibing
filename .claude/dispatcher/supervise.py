"""Присмотр за диспетчером и сторожем проекта (TK-072): отдельный от них механизм ОС раз в 5 мин зовёт этот скрипт.
Сердцебиение старше STALE_S или процесса нет -> остановить зависший и поднять тем же `start.spawn` (WMI / systemd /
Popen), строка в `<проект>/.claude/dispatcher/supervise.log`. Сам ничего не держит.

    python supervise.py [--project P]              один проход (его зовёт планировщик)
    python supervise.py --install [--project P]    поставить планировщик: Windows — Планировщик заданий,
                                                   Linux — systemd --user timer, macOS — launchd (раз в 5 мин)
    python supervise.py --uninstall [--project P]
Окружение диспетчера (RPV_*, ALPHA_*, CLAUDE_*) при --install запоминается в supervise.env.json: планировщик его не наследует."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import project as P  # noqa: E402
import start as S  # noqa: E402

CODE_DIR = Path(__file__).resolve().parent
STALE_S = float(os.environ.get("RPV_SUPERVISE_STALE_S", "600"))
PERIOD_MIN = 5
# служба -> (файл сердцебиения, поле времени)
BEATS = {"dispatch": ("state.json", "last_tick"), "watch": ("watch-heartbeat.json", "ts")}


def heartbeat_age(path: Path, field: str, now: float) -> float | None:
    """Возраст сердцебиения в секундах; None — файла/поля нет или он нечитаем."""
    try:
        return now - datetime.fromisoformat(json.loads(path.read_text(encoding="utf-8"))[field]).timestamp()
    except (OSError, ValueError, KeyError, TypeError):
        return None


def decide(age: float | None, alive: bool, stale_s: float = STALE_S) -> str:
    """ok | start (процесса нет) | restart (процесс есть, сердцебиение старое — завис)."""
    if not alive:
        return "start"
    return "restart" if age is None or age > stale_s else "ok"


def _log(state_dir: Path, msg: str) -> None:
    with (state_dir / "supervise.log").open("a", encoding="utf-8") as f:
        f.write(f"{datetime.now().astimezone().isoformat(timespec='seconds')} {msg}\n")


def load_env(state_dir: Path) -> None:
    try:
        os.environ.update(json.loads((state_dir / "supervise.env.json").read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass


def run_once(project: Path, now: float | None = None, stop=None, spawn=None) -> dict:
    now = time.time() if now is None else now
    real = stop is None and spawn is None
    stop, spawn = stop or S.stop_running, spawn or S.spawn
    state_dir = project / ".claude" / "dispatcher"
    load_env(state_dir)
    use_systemd = real and S._launcher() == "systemd"
    out = {}
    beats = dict(BEATS)
    if os.environ.get("RPV_CEO_TRIAGE") == "1":  # TK-086: сортировщик сигналов CEO — по включению, умолчание прежнее
        beats["ceo_triage"] = ("triage-heartbeat.json", "ts")
    for name, (beat, field) in beats.items():
        age = heartbeat_age(state_dir / beat, field, now)
        alive = S.is_ours(S.read_pid(state_dir / f"{name}.pid"), f"{name}.py")
        act = out[name] = decide(age, alive)
        if act == "ok":
            continue
        unit = S.unit_name(name, project) if use_systemd else None
        stopped = stop(state_dir / f"{name}.pid", f"{name}.py", unit=unit)
        r = spawn(CODE_DIR / f"{name}.py", project, state_dir / f"{name}.run.log")
        _log(state_dir, f"{name}: {act} (сердцебиение {'нет' if age is None else f'{age:.0f} с'}, "
                        f"остановлен pid {stopped or '-'}) -> pid {r.pid} {r.how}")
    return out


def task_name(project: Path) -> str:
    return "rpv-supervise-" + S.unit_name("x", project).rsplit("-", 1)[-1]


def _secret_name(k: str) -> bool:
    """Секрет — по окончанию имени (…_TOKEN, …_API_KEY); пороги (…_TOKENS) и пути (…_DECK_KEY, …_FILE) — нет."""
    u = k.upper()
    return u.endswith(("_TOKEN", "_SECRET", "_PASSWORD", "_PASS", "_API_KEY", "_ACCESS_KEY", "_SECRET_KEY", "_PRIVATE_KEY"))


def snapshot_env(state_dir: Path) -> None:
    keep = {k: v for k, v in os.environ.items()
            if (k.startswith(S.FORWARD_ENV_PREFIXES) or k in S.FORWARD_ENV_NAMES) and not _secret_name(k)}
    (state_dir / "supervise.env.json").write_text(json.dumps(keep, ensure_ascii=False), encoding="utf-8")


def systemd_files(name: str, python: str, script: Path, project: Path) -> dict:
    svc = (f"[Unit]\nDescription=rpv: присмотр за диспетчером ({project})\n\n[Service]\nType=oneshot\n"
           f'ExecStart="{python}" "{script}" --project "{project}"\n')
    tim = (f"[Unit]\nDescription=rpv: присмотр раз в {PERIOD_MIN} мин\n\n[Timer]\nOnBootSec=1min\n"
           f"OnUnitActiveSec={PERIOD_MIN}min\nPersistent=true\n\n[Install]\nWantedBy=timers.target\n")
    return {f"{name}.service": svc, f"{name}.timer": tim}


def launchd_plist(label: str, python: str, script: Path, project: Path) -> str:
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<plist version="1.0"><dict>\n'
            f"<key>Label</key><string>{label}</string>\n<key>ProgramArguments</key><array>"
            f"<string>{python}</string><string>{script}</string><string>--project</string><string>{project}</string></array>\n"
            f"<key>StartInterval</key><integer>{PERIOD_MIN * 60}</integer>\n<key>RunAtLoad</key><true/>\n</dict></plist>\n")


def schtasks_create(name: str, python: str, script: Path, project: Path) -> list:
    """Задание Планировщика: pythonw (без окна), полный путь — иначе «файл не найден» (0x80070002)."""
    exe = Path(python)
    pyw = exe.with_name("pythonw.exe") if exe.with_name("pythonw.exe").exists() else exe
    return ["schtasks", "/Create", "/F", "/TN", name, "/SC", "MINUTE", "/MO", str(PERIOD_MIN),
            "/TR", f'"{pyw}" "{script}" --project "{project}"']


def install(project: Path, remove: bool = False) -> None:
    name, script, py = task_name(project), CODE_DIR / "supervise.py", sys.executable
    if not remove:
        snapshot_env(project / ".claude" / "dispatcher")
    if os.name == "nt":
        cmd = ["schtasks", "/Delete", "/F", "/TN", name] if remove else schtasks_create(name, py, script, project)
        subprocess.run(cmd, check=not remove)
    elif sys.platform == "darwin":
        label = f"dev.rpv.{name}"
        plist = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
        subprocess.run(["launchctl", "unload", str(plist)])
        if remove:
            plist.unlink(missing_ok=True)
        else:
            plist.parent.mkdir(parents=True, exist_ok=True)
            plist.write_text(launchd_plist(label, py, script, project), encoding="utf-8")
            subprocess.run(["launchctl", "load", str(plist)], check=True)
    else:
        d = Path.home() / ".config" / "systemd" / "user"
        if remove:
            subprocess.run(["systemctl", "--user", "disable", "--now", f"{name}.timer"])
            for f in (d / f"{name}.service", d / f"{name}.timer"):
                f.unlink(missing_ok=True)
        else:
            d.mkdir(parents=True, exist_ok=True)
            for fname, text in systemd_files(name, py, script, project).items():
                (d / fname).write_text(text, encoding="utf-8")
            subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
            subprocess.run(["systemctl", "--user", "enable", "--now", f"{name}.timer"], check=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="supervise.py")
    ap.add_argument("--project", default=None)
    ap.add_argument("--install", action="store_true")
    ap.add_argument("--uninstall", action="store_true")
    a = ap.parse_args(sys.argv[1:] if argv is None else argv)
    project = P.resolve_project(["--project", a.project] if a.project else [])
    if a.install or a.uninstall:
        install(project, remove=a.uninstall)
        return 0
    print(run_once(project))
    return 0


if __name__ == "__main__":
    sys.exit(main())
