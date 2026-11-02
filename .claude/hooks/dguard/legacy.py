"""Прежний регэксп как страховка, когда команду не удалось разобрать (незакрытая кавычка)."""
import re
from dguard.config import norm
from dguard.messages import Publish, UNKNOWN_TARGET
from dguard.paths import Ctx


LEGACY_VERBS = re.compile(
    r"(?:^|[\s;&|(`'\"])(?:sudo\s+)?(rm|rmdir|unlink|shred|Remove-Item|ri|del|erase|rd)(?=\s)"
    r"|find\s[^;&|\n]*-delete|git\s+clean|shutil\.rmtree|os\.(?:remove|unlink|rmdir)"
    r"|\.unlink\(|rmtree\(", re.I)
LEGACY_SEP = re.compile(r"[;&|\n)`]")
LEGACY_PUBLISH = re.compile(                      # публикации запуску роли и в неразобранной команде
    r"\bgit\b[^;&|\n]*\bpush\b[^;&|\n]*(?:\b(?:main|master)\b|--(?:tags|all|branches|follow-tags))"
    r"|\bgh\s+(?:release\s+(?:create|upload|edit)|pr\s+merge)\b")


def legacy_found(cmd, cwd):
    found = []
    moved = bool(re.search(r"\b(?:cd|Set-Location|pushd)\s", cmd, re.I))
    ctx = Ctx(None if moved else norm(cwd or "").rstrip("/"))
    for m in LEGACY_VERBS.finditer(cmd):
        verb = (m.group(1) or "").lower()
        end = LEGACY_SEP.search(cmd, m.end())
        if not verb:
            tail = cmd[m.start():end.start() if end else len(cmd)]
            quoted = re.findall(r"['\"]([^'\"]+)['\"]", tail)
            words = [w for w in re.split(r"\s+", tail) if w and not w.startswith("-")]
            if tail[:4].lower() == "find":
                ts = words[1:2] or ["<find без пути>"]
            elif tail[:3].lower() == "git":
                ts = ["."]
            else:
                ts = quoted or [UNKNOWN_TARGET]
        else:
            tail = cmd[m.end():end.start() if end else len(cmd)]
            ts = [w for w in re.findall(r"\"[^\"]*\"|'[^']*'|\S+", tail)
                  if not w.startswith("-") and w not in (">", "2>")]
        found.extend((t, ctx) for t in ts)
    found.extend((Publish(m.group(0)[:60]), ctx) for m in LEGACY_PUBLISH.finditer(cmd))
    return found
