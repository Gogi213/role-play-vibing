"""Прежний регэксп как страховка, когда команду не удалось разобрать (незакрытая кавычка)."""
import re
from dguard.config import norm
from dguard.messages import Publish, UNKNOWN_TARGET
from dguard.paths import closed_host, Ctx


# ------------------------------------------------------------------------------------------------------------
# Страховка: прежний регэксп (если разбор не удался — незакрытая кавычка и т.п.)
# ------------------------------------------------------------------------------------------------------------

LEGACY_VERBS = re.compile(
    r"(?:^|[\s;&|(`'\"])(?:sudo\s+)?(rm|rmdir|unlink|shred|Remove-Item|ri|del|erase|rd)(?=\s)"
    r"|find\s[^;&|\n]*-delete|rsync\s[^;&|\n]*--delete|git\s+clean|shutil\.rmtree|os\.(?:remove|unlink|rmdir)"
    r"|\.unlink\(|rmtree\(", re.I)
LEGACY_SEP = re.compile(r"[;&|\n)`]")
LEGACY_PUBLISH = re.compile(                      # публикации запуску роли и в неразобранной команде (CP2)
    r"\bgit\b[^;&|\n]*\bpush\b[^;&|\n]*(?:\b(?:main|master)\b|--(?:tags|all|branches|follow-tags))"
    r"|\bgh\s+(?:release\s+(?:create|upload|edit)|pr\s+merge)\b|\b(?:npm|pnpm|yarn|cargo|poetry|uv)\s+publish\b"
    r"|\btwine\s+upload\b|\bdocker\s+push\b|\bgem\s+push\b"
    r"|(?i:\bgh\s+api\b(?=[^;&|\n]*(?:-X\s*(?:POST|PUT|PATCH|DELETE)\b|--method[\s=]+(?:POST|PUT|PATCH|DELETE)\b"
    r"|\s-[fF]\b|--(?:raw-)?field\b|--input\b))"
    r"(?=[^;&|\n]*(?:pulls/[^/\s]+/merge|/merges|/releases|/git/refs|/git/tags|/contents/|mergePullRequest)))"
    r"|(?i:\b(?:curl|wget|invoke-webrequest|invoke-restmethod|iwr|irm)\b(?=[^;&|\n]*\b(?:api|uploads)\.github\.com\b)"
    r"(?=[^;&|\n]*(?:-(?-i:X)\s*(?:POST|PUT|PATCH|DELETE)\b|--(?:request|method)[\s=]+(?:POST|PUT|PATCH|DELETE)\b"
    r"|\s-(?-i:[dFT])\b|--(?:data|json|form|upload-file|post-|body-)|-Method[\s:]+(?:POST|PUT|PATCH|DELETE)\b|-Body\b|-InFile\b)))")


def legacy_found(cmd, cwd):
    found = []
    remote = "ssh" in cmd.lower() or bool(re.search(r"\b(?:cd|Set-Location|pushd)\s", cmd, re.I))
    ctx = Ctx(None if remote else norm(cwd or "").rstrip("/"), False)
    for m in LEGACY_VERBS.finditer(cmd):
        verb = (m.group(1) or "").lower()
        end = LEGACY_SEP.search(cmd, m.end())
        if not verb:
            tail = cmd[m.start():end.start() if end else len(cmd)]
            quoted = re.findall(r"['\"]([^'\"]+)['\"]", tail)
            words = [w for w in re.split(r"\s+", tail) if w and not w.startswith("-")]
            if tail[:4].lower() == "find":
                ts = words[1:2] or ["<find без пути>"]
            elif tail[:5].lower() == "rsync":
                ts = words[-1:] or ["<rsync без цели>"]
            elif tail[:3].lower() == "git":
                ts = ["."]
            else:
                ts = quoted or [UNKNOWN_TARGET]
        else:
            tail = cmd[m.end():end.start() if end else len(cmd)]
            ts = [w for w in re.findall(r"\"[^\"]*\"|'[^']*'|\S+", tail)
                  if not w.startswith("-") and w not in (">", "2>")]
        found.extend((t, ctx) for t in ts)
    if found and closed_host(cmd):
        found.append(("<закрытый хост в команде>", ctx))
    found.extend((Publish(m.group(0)[:60]), ctx) for m in LEGACY_PUBLISH.finditer(cmd))
    return found
