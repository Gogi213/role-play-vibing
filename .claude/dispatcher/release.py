"""Выпуск плагина (TK-076 п.5): версия, CHANGELOG, обновление установленного плагина, откат одной командой.

    python release.py bump X.Y.Z [--push]   версия в plugin.json и marketplace.json, проверка записи в CHANGELOG.md,
                                            коммит и тег vX.Y.Z (--push: и отправить)
    python release.py update                обновить установленный плагин до последнего выпуска (версия до/после)
    python release.py rollback [X.Y.Z]      вернуть прошлую версию (по умолчанию — записанная перед последним update)
    python release.py check                 версии в plugin.json, marketplace.json и CHANGELOG.md совпадают
Откат ставит маркетплейс с тегом vX.Y.Z (`owner/repo#vX.Y.Z`); обновить обратно — `update` (маркетплейс на main)."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
NAME = "role-play-vibing"
REPO = "Gogi213/role-play-vibing"
STATE = Path.home() / ".claude" / "rpv-release.json"
SEMVER = re.compile(r"\d+\.\d+\.\d+")


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


def update(run=subprocess.run, state: Path = STATE) -> int:
    before = installed_version(run)
    for cmd in (["claude", "plugin", "marketplace", "update", NAME],
                ["claude", "plugin", "update", f"{NAME}@{NAME}"]):
        r = _run(cmd, run)
        if r.returncode:
            print(f"[release] {' '.join(cmd)} -> код {r.returncode}: {(r.stderr or r.stdout).strip()[:300]}", file=sys.stderr)
            return 1
    after = installed_version(run)
    if before != after:
        _save_prev(before, state)
    print(f"[release] {before} -> {after} (перезапустить Claude Code для применения)")
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
    before = installed_version(run)
    for cmd in (["claude", "plugin", "marketplace", "remove", NAME],
                ["claude", "plugin", "marketplace", "add", f"{REPO}#v{version}"],
                ["claude", "plugin", "install", f"{NAME}@{NAME}"]):
        r = _run(cmd, run)
        if r.returncode:
            print(f"[release] {' '.join(cmd)} -> код {r.returncode}: {(r.stderr or r.stdout).strip()[:300]}", file=sys.stderr)
            return 1
    _save_prev(before, state)
    print(f"[release] {before} -> {installed_version(run)} (откат на v{version}; перезапустить Claude Code)")
    return 0


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
