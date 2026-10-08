"""Выпуск плагина (TK-076 п.5): версия, CHANGELOG, обновление установленного плагина, откат одной командой.

    python release.py bump X.Y.Z [--push]   версия в plugin.json и marketplace.json, проверка записи в CHANGELOG.md,
                                            коммит и тег vX.Y.Z (--push: и отправить)
    python release.py update                обновить установленный плагин до последнего выпуска (версия до/после)
    python release.py rollback [X.Y.Z]      вернуть прошлую версию (по умолчанию — записанная перед последним update)
    python release.py auto --project P      автовыпуск после влития PR (TK-094): update, службы проекта заново из чистого
                                            окружения, doctor; провал — откат на прошлую версию, службы заново, сигнал владельцу
    python release.py check                 версии в plugin.json, marketplace.json и CHANGELOG.md совпадают
Откат: проверка тега на GitHub до любых изменений, маркетплейс `owner/repo#vX.Y.Z`, при сбое — возврат прежнего;
update снимает прибивку к тегу и возвращает маркетплейс на main."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
NAME = "role-play-vibing"
REPO = "Gogi213/role-play-vibing"
STATE = Path.home() / ".claude" / "rpv-release.json"
SEMVER = re.compile(r"\d+\.\d+\.\d+")
INSTALLED = Path.home() / ".claude" / "plugins" / "installed_plugins.json"
SESSION_DROP = ("RPV_ROLE", "RPV_TICKET", "ALPHA_ROLE", "ALPHA_TICKET", "HOST_SESSION")  # как supervise.SESSION_ENV + хост
SESSION_DROP_PREFIX = ("CLAUDECODE", "CLAUDE_CODE_")  # метки сессии Claude; *_DISPATCH_*, ключи и прочие настройки служб остаются
LOCK_STALE_S = 1800


def read_versions(root: Path = ROOT) -> dict:
    plugin = json.loads((root / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    market = json.loads((root / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    m = re.search(r"(?m)^## (\d+\.\d+\.\d+)", (root / "CHANGELOG.md").read_text(encoding="utf-8"))
    return {"plugin": plugin["version"], "marketplace": market["plugins"][0]["version"],
            "changelog": m.group(1) if m else None}


def check(root: Path = ROOT) -> str | None:
    v = read_versions(root)
    return None if len(set(v.values())) == 1 else f"версии расходятся: {v}"


def bump(version: str, root: Path = ROOT) -> None:
    if not SEMVER.fullmatch(version):
        raise SystemExit(f"версия должна быть X.Y.Z, получено {version!r}")
    if not re.search(rf"(?m)^## {re.escape(version)}\b", (root / "CHANGELOG.md").read_text(encoding="utf-8")):
        raise SystemExit(f"в CHANGELOG.md нет раздела «## {version}» — сначала запись об изменениях")
    for rel, edit in ((".claude-plugin/plugin.json", lambda d: d.update(version=version)),
                      (".claude-plugin/marketplace.json", lambda d: d["plugins"][0].update(version=version))):
        p = root / rel
        data = json.loads(p.read_text(encoding="utf-8"))
        edit(data)
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    problem = check(root)
    if problem:
        raise SystemExit(problem)


def _run(cmd: list, run=subprocess.run) -> subprocess.CompletedProcess:
    if cmd[0] == "claude":
        cmd = [shutil.which("claude") or "claude"] + cmd[1:]
    return run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")


def installed_version(run=subprocess.run) -> str | None:
    out = _run(["claude", "plugin", "list"], run).stdout
    m = re.search(rf"{NAME}@\S+\s+Version:\s*(\S+)", out)
    return m.group(1) if m else None


def _save_prev(version: str | None, state: Path) -> None:
    if version:
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(json.dumps({"previous": version}), encoding="utf-8")


def _tag_exists(version: str, run=subprocess.run) -> bool:
    r = _run(["git", "ls-remote", "--tags", f"https://github.com/{REPO}", f"refs/tags/v{version}"], run)
    return r.returncode == 0 and f"refs/tags/v{version}" in r.stdout


def pinned_ref(run=subprocess.run) -> str | None:
    """Тег, к которому прибит маркетплейс («Source: GitHub (owner/repo@vX.Y.Z)»); None — на main."""
    out = _run(["claude", "plugin", "marketplace", "list"], run).stdout
    m = re.search(rf"{re.escape(REPO)}@(\S+?)\)", out)
    return m.group(1) if m else None


def _fail(cmd: list, r) -> int:
    print(f"[release] {' '.join(cmd)} -> код {r.returncode}: {(r.stderr or r.stdout).strip()[:300]}", file=sys.stderr)
    return 1


def _reinstall(source: str, run) -> int:
    """Маркетплейс заново на source (owner/repo или owner/repo#vX.Y.Z) и плагин из него."""
    for cmd in (["claude", "plugin", "marketplace", "remove", NAME],
                ["claude", "plugin", "marketplace", "add", source],
                ["claude", "plugin", "install", f"{NAME}@{NAME}"]):
        r = _run(cmd, run)
        if r.returncode:
            return _fail(cmd, r)
    return 0


def update(run=subprocess.run, state: Path = STATE) -> int:
    before = installed_version(run)
    pin = pinned_ref(run)
    if pin:  # после отката маркетплейс прибит к тегу — вернуть на main
        if _reinstall(REPO, run):
            return 1
    for cmd in (["claude", "plugin", "marketplace", "update", NAME],
                ["claude", "plugin", "update", f"{NAME}@{NAME}"]):
        r = _run(cmd, run)
        if r.returncode:
            return _fail(cmd, r)
    after = installed_version(run)
    if before != after:
        _save_prev(before, state)
    print(f"[release] {before} -> {after}" + (f" (маркетплейс снят с {pin}, снова на main)" if pin else "")
          + " (перезапустить Claude Code для применения)")
    return 0


def rollback(version: str | None = None, run=subprocess.run, state: Path = STATE) -> int:
    if version is None:
        try:
            version = json.loads(state.read_text(encoding="utf-8")).get("previous")
        except (OSError, ValueError):
            version = None
    if not version or not SEMVER.fullmatch(version):
        print("[release] не знаю, на какую версию откатывать: укажите `rollback X.Y.Z`", file=sys.stderr)
        return 2
    if not _tag_exists(version, run):  # до любых изменений на машине
        print(f"[release] тега v{version} нет в {REPO} — ничего не тронуто", file=sys.stderr)
        return 3
    before = installed_version(run)
    old_pin = pinned_ref(run)
    if _reinstall(f"{REPO}#v{version}", run):
        print(f"[release] откат не удался — возвращаю прежний маркетплейс ({old_pin or 'main'})", file=sys.stderr)
        _reinstall(f"{REPO}#{old_pin}" if old_pin else REPO, run)
        return 1
    _save_prev(before, state)
    print(f"[release] {before} -> {installed_version(run)} (откат на v{version}; перезапустить Claude Code)")
    return 0


def _vtuple(v: str) -> tuple:
    return tuple(int(x) for x in v.split("."))


def plugin_version_at(gh, repo: str, ref: str) -> str:
    """Версия из .claude-plugin/plugin.json на ref (ветка или sha) — через API GitHub, без клона."""
    import base64
    data = gh(f"repos/{repo}/contents/.claude-plugin/plugin.json?ref={ref}")
    return json.loads(base64.b64decode(data["content"]).decode("utf-8"))["version"]


def bump_problem(gh, repo: str, head_sha: str, default: str) -> str | None:
    """TK-094: PR плагина вливается только с выпуском — версия головы выше версии main (иначе нечего ставить и откату
    не к чему возвращаться). None — можно вливать; иначе текст причины."""
    head_v, base_v = plugin_version_at(gh, repo, head_sha), plugin_version_at(gh, repo, default)
    if SEMVER.fullmatch(head_v) and SEMVER.fullmatch(base_v) and _vtuple(head_v) > _vtuple(base_v):
        return None
    return (f"версия в PR {head_v} не выше версии {default} ({base_v}): поднять `python .claude/dispatcher/release.py bump X.Y.Z` "
            "(раздел в CHANGELOG.md, plugin.json, marketplace.json) и запушить — иначе автовыпуск нечего ставить")


def tag_release(gh, repo: str, version: str, sha: str) -> None:
    """Тег vX.Y.Z на коммите слияния (на него опирается откат `rollback`); уже есть — не ошибка."""
    try:
        gh(f"repos/{repo}/git/refs", method="POST", ref=f"refs/tags/v{version}", sha=sha)
    except Exception as e:
        if "already exists" not in str(e):
            raise


def installed_dir(project: Path, registry: Path = INSTALLED) -> Path | None:
    """Каталог установленного плагина для проекта (installPath из installed_plugins.json); нет записи — None."""
    try:
        entries = json.loads(registry.read_text(encoding="utf-8"))["plugins"].get(f"{NAME}@{NAME}") or []
    except (OSError, ValueError, KeyError):
        return None
    mine = [e for e in entries if e.get("scope") == "project" and Path(e.get("projectPath", "")) == project]
    pick = (mine or entries or [None])[-1]
    return Path(pick["installPath"]) if pick else None


def clean_env(env: dict | None = None) -> dict:
    """Окружение для перезапуска служб: без роли/тикета/сессии Claude — иначе supervise --install откажет (дыра (г) TK-090).
    Настройки диспетчера (RPV_/ALPHA_ *_DISPATCH_*, лимиты) остаются: start._forward_env пробрасывает их службам."""
    return {k: v for k, v in (os.environ if env is None else env).items()
            if k.upper() not in SESSION_DROP and not k.upper().startswith(SESSION_DROP_PREFIX)}


def restart_services(project: Path, code_dir: Path | None, run=subprocess.run) -> bool:
    """start.py и supervise --install из каталога плагина `code_dir`, окружение чистое. True — оба кода 0."""
    if code_dir is None:
        return False
    ok = True
    for args in (["start.py", "--project", str(project)], ["supervise.py", "--project", str(project), "--install"]):
        r = run([sys.executable, str(code_dir / ".claude" / "dispatcher" / args[0]), *args[1:]], capture_output=True,
                text=True, encoding="utf-8", errors="replace", env=clean_env(), cwd=str(project))
        if r.returncode:
            print(f"[release] {args[0]} -> код {r.returncode}: {(r.stderr or r.stdout).strip()[:300]}", file=sys.stderr)
            ok = False
    return ok


def verify_alive(project: Path, code_dir: Path | None, run=subprocess.run, wait_s: float = 60.0, step_s: float = 5.0) -> bool:
    """doctor.py без FAIL; службам нужно время на первое сердцебиение — повтор до wait_s."""
    if code_dir is None:
        return False
    deadline = time.time() + wait_s
    while True:
        r = run([sys.executable, str(code_dir / ".claude" / "dispatcher" / "doctor.py"), "--project", str(project), "--alive"],
                capture_output=True, text=True, encoding="utf-8", errors="replace", env=clean_env())
        if r.returncode == 0:
            return True
        if time.time() >= deadline:
            return False
        time.sleep(step_s)


def _journal(project: Path, rec: dict) -> None:
    d = project / ".claude" / "dispatcher"
    d.mkdir(parents=True, exist_ok=True)
    rec = {"ts": datetime.now().astimezone().isoformat(timespec="seconds"), **rec}
    with open(d / "release-journal.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    if rec["result"] != "ok":  # «ждёт вас» на Диспетчерскую (вопрос-сигнал), CEO не будится
        import ask
        ask.new_notice(project, "release", f"Выпуск плагина {rec.get('after')}: {rec['result']} ({rec.get('detail', '')})")


def autorelease(project: Path, run=subprocess.run, state: Path = STATE, registry: Path = INSTALLED,
                restart=restart_services, verify=verify_alive) -> int:
    """TK-094: после влития PR плагина — update, службы заново, проверка живы; не прошла — откат и сигнал владельцу.
    0 — выпущено или нечего выпускать; 1 — откатили; 2 — откат тоже не удался (службы могут стоять)."""
    before = installed_version(run)
    if update(run, state):
        _journal(project, {"result": "update-failed", "before": before, "after": before, "detail": "claude plugin update"})
        return 1
    after = installed_version(run)
    if before == after:  # влит PR без смены версии — не молча: выпуск ждёт bump (`release.py bump`)
        _journal(project, {"result": "no-bump", "before": before, "after": after,
                           "detail": "влит PR плагина без смены версии — нужен release.py bump"})
        print(f"[release] auto: версия {after} уже стоит — нечего выпускать (PR влит без bump)")
        return 0
    code_dir = installed_dir(project, registry)
    if restart(project, code_dir, run) and verify(project, code_dir, run):
        _journal(project, {"result": "ok", "before": before, "after": after})
        print(f"[release] auto: {before} -> {after}, службы живы")
        return 0
    if rollback(before, run, state):
        _journal(project, {"result": "rollback-failed", "before": before, "after": after, "detail": "службы могут стоять"})
        return 2
    old_dir = installed_dir(project, registry)
    alive = restart(project, old_dir, run) and verify(project, old_dir, run)
    _journal(project, {"result": "rolled-back", "before": before, "after": after,
                       "detail": "службы живы на прежней версии" if alive else "после отката службы не поднялись"})
    print(f"[release] auto: {after} не прошла проверку — откат на {before}", file=sys.stderr)
    return 1 if alive else 2


def _auto_paths(project: Path) -> tuple:
    d = project / ".claude" / "dispatcher"
    return d / "release-auto.lock", d / "release-auto.pending"


def _lock_acquire(lock: Path, now=time.time) -> bool:
    """Замок автовыпуска: один update/start за раз. Занят живым (моложе LOCK_STALE_S) — False."""
    lock.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            os.close(os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            return True
        except FileExistsError:
            try:
                if now() - lock.stat().st_mtime < LOCK_STALE_S:
                    return False
                lock.unlink()
            except OSError:
                pass
    return False


def run_auto_locked(project: Path, run_one=autorelease) -> int:
    """Тело процесса `auto`: замок; занят — пометка «ещё один выпуск» (доделает держатель после текущего)."""
    lock, pending = _auto_paths(project)
    if not _lock_acquire(lock):
        pending.touch()
        print("[release] auto: выпуск уже идёт — поставлен в очередь")
        return 0
    rc = 0
    try:
        while True:
            pending.unlink(missing_ok=True)
            rc = max(rc, run_one(project))
            if not pending.exists():
                return rc
    finally:
        lock.unlink(missing_ok=True)


def spawn_auto(project: Path, popen=subprocess.Popen) -> bool:
    """Автовыпуск отдельным процессом, отвязанным от диспетчера (start.py остановит самого диспетчера). RPV_AUTORELEASE=0 — выкл.
    Несколько влитых за проход PR дают несколько процессов — их сводит замок (run_auto_locked)."""
    if os.environ.get("RPV_AUTORELEASE", "1") == "0":
        return False
    flags = (subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS) if os.name == "nt" else 0
    log = project / ".claude" / "dispatcher" / "release-auto.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    popen([sys.executable, str(Path(__file__).resolve()), "auto", "--project", str(project)], env=clean_env(),
          cwd=str(project), stdout=open(log, "a", encoding="utf-8"), stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
          creationflags=flags, start_new_session=(os.name != "nt"))
    return True


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    cmd = argv[0] if argv else ""
    if cmd == "bump" and len(argv) >= 2:
        bump(argv[1])
        git = ["git", "-C", str(ROOT)]
        for c in (git + ["add", ".claude-plugin", "CHANGELOG.md"], git + ["commit", "-m", f"Выпуск {argv[1]}"],
                  git + ["tag", f"v{argv[1]}"]) + ((git + ["push", "origin", "HEAD", f"v{argv[1]}"],) if "--push" in argv else ()):
            r = _run(c)
            if r.returncode:
                print(f"[release] {' '.join(c)}: {(r.stderr or r.stdout).strip()}", file=sys.stderr)
                return 1
        print(f"[release] выпуск {argv[1]}: тег v{argv[1]} поставлен" + ("" if "--push" in argv else "; отправить: git push origin HEAD v" + argv[1]))
        return 0
    if cmd == "update":
        return update()
    if cmd == "auto":
        pj = argv[argv.index("--project") + 1] if "--project" in argv[1:] else None
        if not pj:
            print("[release] auto: нужен --project <корень проекта>", file=sys.stderr)
            return 2
        return run_auto_locked(Path(pj).resolve())
    if cmd == "rollback":
        return rollback(argv[1] if len(argv) > 1 else None)
    if cmd == "check":
        problem = check()
        print(problem or "версии согласованы: " + json.dumps(read_versions(), ensure_ascii=False))
        return 1 if problem else 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
