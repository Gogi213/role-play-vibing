"""Выпуск версии (инструмент хозяина репо, не входит в поставку): версия в plugin.json и marketplace.json, проверка
записи в CHANGELOG.md, коммит и тег vX.Y.Z.

    python tools/release_bump.py X.Y.Z [--push]     (--push: и отправить коммит с тегом)
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".claude" / "dispatcher"))
import release  # noqa: E402


def bump(version: str, root: Path = ROOT) -> None:
    if not release.SEMVER.fullmatch(version):
        raise SystemExit(f"версия должна быть X.Y.Z, получено {version!r}")
    if not re.search(rf"(?m)^## {re.escape(version)}\b", (root / "CHANGELOG.md").read_text(encoding="utf-8")):
        raise SystemExit(f"в CHANGELOG.md нет раздела «## {version}» — сначала запись об изменениях")
    for rel, edit in ((".claude-plugin/plugin.json", lambda d: d.update(version=version)),
                      (".claude-plugin/marketplace.json", lambda d: d["plugins"][0].update(version=version))):
        p = root / rel
        data = json.loads(p.read_text(encoding="utf-8"))
        edit(data)
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    problem = release.check(root)
    if problem:
        raise SystemExit(problem)


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print(__doc__)
        return 2
    bump(argv[0])
    git = ["git", "-C", str(ROOT)]
    for c in (git + ["add", ".claude-plugin", "CHANGELOG.md"], git + ["commit", "-m", f"Выпуск {argv[0]}"],
              git + ["tag", f"v{argv[0]}"]) + ((git + ["push", "origin", "HEAD", f"v{argv[0]}"],) if "--push" in argv else ()):
        r = release._run(c)
        if r.returncode:
            print(f"[release] {' '.join(c)}: {(r.stderr or r.stdout).strip()}", file=sys.stderr)
            return 1
    print(f"[release] выпуск {argv[0]}: тег v{argv[0]} поставлен" + ("" if "--push" in argv else "; отправить: git push origin HEAD v" + argv[0]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
