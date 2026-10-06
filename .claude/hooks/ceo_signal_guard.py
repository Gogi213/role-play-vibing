"""PreToolUse (TK-074, В-192): сигналы CEO идут только через шину; ceo-inbox.md и ceo-wake.log пишет один код —
dispatch.append_ceo_inbox (запасной путь при лежащей шине). Запись из сессии (Write/Edit/Bash) отказ; чтение — свободно."""
import json
import re
import sys

NAMES = ("ceo-inbox.md", "ceo-wake.log")
WRITE_BASH = re.compile(r"(>|\btee\b|\bsed\b[^|;&]*\s-i|\bmv\b|\bcp\b|\brm\b|\btruncate\b|\bdd\b|\binstall\b|"
                        r"Set-Content|Add-Content|Out-File|Clear-Content|Remove-Item|Move-Item|Copy-Item|\.write|open\()", re.I)
TEXT_ARG = re.compile(r"""--text(?:=|\s+)("(?:[^"\\]|\\.)*"|'[^']*')""", re.S)
MSG = ("Сигналы команды идут только через шину: ceo-inbox.md/ceo-wake.log пишет лишь dispatch.append_ceo_inbox "
       "(запасной путь). Нужно CEO — `tickets.py comment <ID> --next ceo`; очередь читает `tickets.py inbox` (README «Стандарт сигналов»).")


def denied(tool, inp):
    if tool in ("Write", "Edit", "NotebookEdit"):
        path = str(inp.get("file_path") or inp.get("notebook_path") or "").replace("\\", "/").lower()
        return path.rsplit("/", 1)[-1] in NAMES
    if tool in ("Bash", "PowerShell"):
        cmd = str(inp.get("command") or "")
        if "tickets.py" in cmd:
            cmd = TEXT_ARG.sub("--text X", cmd)
        if not any(n in cmd.lower() for n in NAMES):
            return False
        return any(WRITE_BASH.search(part) and any(n in part.lower() for n in NAMES)
                   for part in re.split(r"\n|&&|\|\||;", cmd))
    return False


def main():
    try:
        d = json.load(sys.stdin)
    except Exception:
        return 0
    if denied(d.get("tool_name", ""), d.get("tool_input") or {}):
        print(MSG, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
