"""Сигналы CEO (`ceo-inbox.md`, `ceo-wake.log`) пишет только диспетчер."""
import re
from dguard.tools import file_target, SHELL_TOOLS


BENIGN_REDIRECT = re.compile(r"\d*>&?\s*(?:/dev/null|\d)")  # `2>/dev/null`, `2>&1` — не запись в файл


CEO_SIGNAL_NAMES = ("ceo-inbox.md", "ceo-wake.log")   # сигналы CEO пишет только код диспетчера (dispatch.append_ceo_inbox)
# Команды-записи: путь — аргумент команды, поэтому достаточно, что имя файла есть в той же части команды. Перенаправление
# `>` и `open(` разбираются отдельно (`shell_writes`): `>=` в Python и чтение `open(p, encoding=…)` — не запись (alpha 16.10).
CEO_SIGNAL_WRITE = re.compile(r"(\btee\b|\bsed\b[^|;&]*\s-i|\bmv\b|\bcp\b|\brm\b|\btruncate\b|\bdd\b|\binstall\b|"
                              r"Set-Content|Add-Content|Out-File|Clear-Content|Remove-Item|Move-Item|Copy-Item|"
                              r"(?<!stdout)(?<!stderr)\.write|WriteAll|AppendAll|os\.(?:remove|unlink|rename|replace|truncate)\b|"
                              r"\.unlink\(|shutil\.(?:copy|move|rmtree))", re.I)
REDIRECT = re.compile(r"(?<![=\-<>!])(?:\d|&)?>>?(?![=>&])\|?\s*([^\s;|&<>]+)")  # `>`/`>>` и его цель; не `>=`, `->`, `=>`, `2>&1`
OPEN_CALL = re.compile(r"\bopen\(", re.I)
READ_MODE = re.compile(r"[rbtU]*|open|read", re.I)        # Python `r`/`rb`/`rt`, .NET FileMode.Open / FileAccess.Read
MODE_LIKE = re.compile(r"[rwaxbtU+]{1,4}|open|read|append|create|createnew|openorcreate|truncate|write|readwrite", re.I)
NONLITERAL = re.compile(r"[$%`(]")                         # цель — переменная или подстановка: имени в ней не видно


def _call_args(text: str, start: int) -> list:
    """Аргументы вызова верхнего уровня от открывающей скобки `start`: разделение по запятым вне скобок и кавычек."""
    args, cur, depth, quote, i = [], "", 0, "", start + 1
    while i < len(text):
        c = text[i]
        if quote:
            if c == quote:
                quote = ""
        elif c in "'\"":
            quote = c
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            if depth == 0:
                break
            depth -= 1
        elif c == "," and depth == 0:
            args.append(cur.strip())
            cur = ""
            i += 1
            continue
        cur += c
        i += 1
    return args + ([cur.strip()] if cur.strip() else [])


def _literal(arg: str):
    """Строковый литерал без кавычек (и `\\"` из оболочки) или None — не литерал."""
    a = arg.replace('\\"', '"').strip()
    if len(a) >= 2 and a[0] == a[-1] and a[0] in "'\"":
        return a[1:-1]
    return None


def _open_writes(args: list) -> bool:
    """`open(...)` пишет: режим записи литералом (`'a'`, `mode='w'`, `'r+'`, `'Append'`) или режим не литерал (не знаем —
    считаем записью). Без режима или с `'r'`/`'rb'`/`encoding=…` — чтение. Режим ищется в первых двух позиционных
    аргументах (`open(путь, режим)`, `Path(...).open(режим)`) и в `mode=`/`FileMode`."""
    kw = {a.split("=", 1)[0].strip().lower(): a.split("=", 1)[1].strip() for a in args
          if re.match(r"\s*\w+\s*=(?!=)", a)}
    pos = [a for a in args if not re.match(r"\s*\w+\s*=(?!=)", a)]
    if "mode" in kw:
        lit = _literal(kw["mode"])
        return lit is None or not READ_MODE.fullmatch(lit)
    for a in pos[:2]:
        lit = _literal(a)
        if lit is not None and MODE_LIKE.fullmatch(lit):
            return not READ_MODE.fullmatch(lit)
        if lit is None and re.search(r"FileMode\.|FileAccess\.", a, re.I):
            return not re.search(r"\.(?:Open|Read)\b", a)
    return len(pos) >= 2 and _literal(pos[1]) is None and not re.fullmatch(r"\w+\.O_RDONLY", pos[1])


def shell_writes(part: str, names) -> bool:
    """Часть команды пишет в файл, путь которого содержит одно из `names` (нижний регистр, `/`). Чтение — нет:
    `cat`, `grep`, `python -c "...open(p, encoding='utf-8')... >= ..."` (alpha 16.10: подсчёт сигналов отклонялся)."""
    low = part.lower().replace("\\", "/")
    if not any(n in low for n in names):
        return False
    if CEO_SIGNAL_WRITE.search(part):
        return True
    for m in REDIRECT.finditer(part):
        target = m.group(1).replace("\\", "/").lower().strip("'\"")
        if any(n in target for n in names) or NONLITERAL.search(m.group(1)):
            return True
    return any(_open_writes(_call_args(part, m.end() - 1)) for m in OPEN_CALL.finditer(part))
CEO_SIGNAL_TEXT = re.compile(r"""--(?:text|why)(?:=|\s+)("(?:[^"\\]|\\.)*"|'[^']*')""", re.S)


def command_text(ti, benign=False):
    """Команда оболочки из tool_input; текст `--text`/`--why` у tickets.py не в счёт (TK-110 Ж-3); `benign` — и без
    `2>/dev/null`, `2>&1`."""
    cmd = str(ti.get("command") or "")
    if "tickets.py" in cmd:
        cmd = CEO_SIGNAL_TEXT.sub("--text X", cmd)
    return BENIGN_REDIRECT.sub("", cmd) if benign else cmd


def parts_write(cmd, names):
    """Какая-то часть команды (по `;`, `&&`, `||`, переводу строки) пишет в файл из `names`."""
    return any(shell_writes(part, names) for part in re.split(r"\n|&&|\|\||;", cmd))


def ceo_signal_write(tool, ti, names=None):
    """Запись в ceo-inbox.md/ceo-wake.log (или в файлы `names`) из сессии (Write/Edit/Bash) — отказ; чтение свободно; текст
    `--text`/`--why` у tickets.py не в счёт."""
    names = names or CEO_SIGNAL_NAMES
    if tool in SHELL_TOOLS:
        return parts_write(command_text(ti), names)
    path = file_target(ti)
    return bool(path) and path.replace("\\", "/").lower().rsplit("/", 1)[-1] in names
