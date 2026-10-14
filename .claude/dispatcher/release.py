"""Выпуск плагина вручную (CP4, критерии 14 и 16): версия, CHANGELOG, тег. Автовыпуска нет — слияние PR плагин не обновляет,
службы не перезапускает и не откатывает; пользователи обновляются штатно, командами `/plugin` Claude Code.

    python release.py bump X.Y.Z [--push]   версия в plugin.json и marketplace.json, проверка записи в CHANGELOG.md,
                                            коммит и тег vX.Y.Z (--push: и отправить — необратимо, только владелец/мейнтейнер)
    python release.py check                 версии в plugin.json, marketplace.json и CHANGELOG.md совпадают"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SEMVER = re.compile(r"\d+\.\d+\.\d+")
GIT_TIMEOUT_S = 120.0     # git без предела держал клон и блокировал установку (Б3 КТ-3)
GIT_STALL_ENV = {"GIT_HTTP_LOW_SPEED_LIMIT": "1000", "GIT_HTTP_LOW_SPEED_TIME": "30", "GIT_TERMINAL_PROMPT": "0"}


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


def _run(cmd: list, run=subprocess.run, timeout: float = GIT_TIMEOUT_S) -> subprocess.CompletedProcess:
    """git с пределом времени: завис — код 124, процесс убит; вызывающий видит обычный отказ."""
    try:
        return run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                   env={**os.environ, **GIT_STALL_ENV})
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", f"таймаут {timeout:.0f} с")


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
    if cmd == "check":
        problem = check()
        print(problem or "версии согласованы: " + json.dumps(read_versions(), ensure_ascii=False))
        return 1 if problem else 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
