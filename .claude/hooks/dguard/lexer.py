"""Лексер shell-строки: разбор на простые команды (кавычки, heredoc, `$(…)`, перенаправления)."""
import re
from dguard.messages import MAX_DEPTH


# ------------------------------------------------------------------------------------------------------------
# Лексер: текст команды → простые команды
# ------------------------------------------------------------------------------------------------------------

class ParseError(Exception):
    pass


class Cmd:
    __slots__ = ("words", "heredocs", "pipe_from", "overwrites")

    def __init__(self, words=None):
        self.words = list(words or [])
        self.heredocs = []      # тела heredoc / here-string этой команды (её stdin)
        self.pipe_from = None   # предыдущая команда конвейера
        self.overwrites = []    # цели усекающих перенаправлений `> f` `>| f` `&> f` `2> f` (не `>>`)


ESCAPABLE = ";|&<>()\"' $\\`"
ARRAY_HEAD = re.compile(r"[A-Za-z_]\w*\+?=")
HEREDOC_RE = re.compile(r"<<(-?)[ \t]*(?:'([^']*)'|\"([^\"]*)\"|\\?([^\s;|&<>()'\"]+))")


def find_close(s, j):
    """Индекс `)`, закрывающей `$(`, чей текст начинается с j. Кавычки и heredoc внутри учитываются."""
    n = len(s)
    depth = 1
    pend = []
    while j < n:
        c = s[j]
        if c == "'":
            k = s.find("'", j + 1)
            if k < 0:
                raise ParseError("'")
            j = k + 1
            continue
        if c == '"':
            j += 1
            while j < n and s[j] != '"':
                j += 2 if s[j] == "\\" else 1
            if j >= n:
                raise ParseError('"')
            j += 1
            continue
        if c == "\\":
            j += 2
            continue
        if c == "<" and s.startswith("<<", j) and not s.startswith("<<<", j):
            m = HEREDOC_RE.match(s, j)
            if m:
                pend.append((m.group(2) or m.group(3) or m.group(4), bool(m.group(1))))
                j = m.end()
                continue
        if c == "\n" and pend:
            j += 1
            for delim, strip in pend:
                while j < n:
                    e = s.find("\n", j)
                    line = s[j:] if e < 0 else s[j:e]
                    j = n if e < 0 else e + 1
                    if (line.lstrip("\t") if strip else line) == delim:
                        break
            pend = []
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return j
        j += 1
    raise ParseError("$(")


class _Lexer:
    def __init__(self, s, depth):
        if depth > MAX_DEPTH:
            raise ParseError("вложенность")
        self.s = s.replace("\r\n", "\n").replace("\r", "\n")
        self.depth = depth
        self.i = 0
        self.cmds = []
        self.cur = Cmd()
        self.buf = []
        self.inword = False
        self.skip = False   # следующее слово — цель перенаправления (True), here-string ("here") или усекающего `>` ("out")
        self.pending = []   # heredoc, тела которых читаются после перевода строки

    # --- слова и команды
    def push(self, text):
        self.buf.append(text)
        self.inword = True

    def end_word(self):
        if self.inword:
            w = "".join(self.buf)
            if self.skip == "here":
                self.cur.heredocs.append(w)
            elif self.skip == "out":
                self.cur.overwrites.append(w)
            elif not self.skip:
                self.cur.words.append(w)
            self.skip = False
        self.buf = []
        self.inword = False

    def end_cmd(self, sep):
        self.end_word()
        self.skip = False
        done = self.cur
        if done.words or done.overwrites:
            self.cmds.append(done)
        self.cur = Cmd()
        if sep == "|" and done.words:
            self.cur.pipe_from = done

    def sub(self, inner):
        """Команды внутри `$(…)`/`` `…` ``/`<(…)` исполняются — разобрать и добавить в общий список."""
        self.cmds.extend(tokenize(inner, self.depth + 1))

    # --- разбор
    def run(self):
        s = self.s
        n = len(s)
        while self.i < n:
            c = s[self.i]
            i = self.i
            if c in " \t":
                self.end_word()
                self.i += 1
            elif c == "\n":
                self.end_cmd("\n")
                self.i += 1
                self.read_heredocs()
            elif c == "#" and not self.inword:
                j = s.find("\n", i)
                self.i = n if j < 0 else j
            elif c == "\\":
                self.backslash()
            elif c == "'":
                self.single()
            elif c == '"':
                self.double()
            elif c == "$" and s.startswith("$(", i):
                j = find_close(s, i + 2)
                self.sub(s[i + 2:j])
                self.push(s[i:j + 1])
                self.i = j + 1
            elif c == "`":
                self.backtick()
            elif c == "@" and not self.inword and re.match(r"@(['\"])[ \t]*\n", s[i:i + 40]):
                self.here_string()
            elif c == ";":
                self.end_cmd(";")
                self.i += 1
            elif c == "|":
                self.pipe()
            elif c == "&":
                self.amp()
            elif c in "<>":
                self.redirect()
            elif c == "(" and self.inword and ARRAY_HEAD.fullmatch("".join(self.buf)):
                j = find_close(s, i + 1)            # `K=(-i key -o X)` — массив bash одним словом
                self.push(s[i:j + 1])
                self.i = j + 1
            elif c in "()":
                self.end_cmd(c)
                self.i += 1
            else:
                self.push(c)
                self.i += 1
        self.end_cmd("")
        return self.cmds

    def single(self):
        s, i = self.s, self.i
        j = s.find("'", i + 1)
        if j < 0:
            raise ParseError("'")
        self.push(s[i + 1:j])
        self.i = j + 1

    def backtick(self):
        s, i = self.s, self.i
        if s.startswith("`\n", i):
            self.i += 2
            return
        j = s.find("`", i + 1)
        if j < 0:
            self.push("`")
            self.i += 1
        else:
            self.sub(s[i + 1:j])
            self.push(s[i:j + 1])
            self.i = j + 1

    def here_string(self):
        """PowerShell `@'…'@` / `@"…"@` — многострочная строка одним словом."""
        s, i = self.s, self.i
        q = s[i + 1]
        start = s.index("\n", i) + 1
        end = s.find("\n" + q + "@", start - 1)
        if end < 0:
            raise ParseError("here-string")
        self.push(s[start:end])
        self.i = end + 3

    def pipe(self):
        s, i = self.s, self.i
        if s.startswith("||", i):
            self.end_cmd("||")
            self.i += 2
        elif s.startswith("|&", i):
            self.end_cmd("|")
            self.i += 2
        else:
            self.end_cmd("|")
            self.i += 1

    def amp(self):
        s, i = self.s, self.i
        if s.startswith("&&", i):
            self.end_cmd("&&")
            self.i += 2
        elif s.startswith("&>", i):
            self.end_word()
            append = s.startswith("&>>", i)
            self.i += 3 if append else 2
            self.skip = True if append else "out"
        else:
            self.end_cmd("&")
            self.i += 1

    def backslash(self):
        s, i = self.s, self.i
        nxt = s[i + 1] if i + 1 < len(s) else ""
        if nxt == "\n":
            self.i += 2                     # продолжение строки
        elif nxt and nxt in ESCAPABLE:
            self.push(nxt)                  # `\;` `\ ` `\"` `\\` — буквальный символ
            self.i += 2
        else:
            self.push("\\")                 # путь Windows: `C:\Windows\x`
            self.i += 1

    def double(self):
        s, n = self.s, len(self.s)
        j = self.i + 1
        out = []
        while j < n:
            c = s[j]
            if c == '"':
                break
            if c == "\\" and j + 1 < n and s[j + 1] in '"\\$`':
                out.append(s[j + 1])
                j += 2
            elif c == "$" and s.startswith("$(", j):
                e = find_close(s, j + 2)
                self.sub(s[j + 2:e])
                out.append(s[j:e + 1])
                j = e + 1
            elif c == "`":
                e = s.find("`", j + 1)
                if e < 0:
                    out.append(c)
                    j += 1
                else:
                    self.sub(s[j + 1:e])
                    out.append(s[j:e + 1])
                    j = e + 1
            else:
                out.append(c)
                j += 1
        else:
            raise ParseError('"')
        self.push("".join(out))
        self.i = j + 1

    def redirect(self):
        s, n = self.s, len(self.s)
        i = self.i
        c = s[i]
        if self.inword and "".join(self.buf).isdigit():   # `2>` — номер дескриптора, не аргумент
            self.buf = []
            self.inword = False
        else:
            self.end_word()
        if s.startswith("<<<", i):
            self.i = i + 3
            self.skip = "here"
            return
        if s.startswith("<<", i):
            m = HEREDOC_RE.match(s, i)
            if not m:
                self.i = i + 2
                return
            bare = m.group(4)
            quoted = m.group(2) is not None or m.group(3) is not None or s[m.start(4) - 1:m.start(4)] == "\\"
            delim = m.group(2) if m.group(2) is not None else (m.group(3) if m.group(3) is not None else bare)
            self.pending.append((delim, bool(m.group(1)), not quoted, self.cur))
            self.i = m.end()
            return
        i += 1
        append = False
        if i < n and s[i] == c and c == ">":
            i += 1                      # `>>` — дописывание, файл не усекается
            append = True
        trunc = "out" if (c == ">" and not append) else True
        if i < n and s[i] == "&":
            i += 1                      # `>&2`, `2>&1`, `>&-`
            while i < n and (s[i].isdigit() or s[i] == "-"):
                i += 1
        elif i < n and s[i] == "|":
            i += 1
            self.skip = trunc
        elif i < n and s[i] == "(":     # process substitution `<(…)` / `>(…)`
            j = find_close(s, i + 1)
            self.sub(s[i + 1:j])
            i = j + 1
        else:
            self.skip = trunc
        self.i = i

    def read_heredocs(self):
        s, n = self.s, len(self.s)
        i = self.i
        for delim, strip, expand, cmd in self.pending:
            body = []
            while i < n:
                j = s.find("\n", i)
                line = s[i:] if j < 0 else s[i:j]
                i = n if j < 0 else j + 1
                line = line.lstrip("\t") if strip else line
                if line == delim:
                    break
                body.append(line)
            text = "\n".join(body)
            cmd.heredocs.append(text)
            if expand:                  # в heredoc без кавычек у разделителя `$(…)` и `` `…` `` исполняются
                self.expansions(text)
        self.pending = []
        self.i = i

    def expansions(self, text):
        j = 0
        try:
            while True:
                j = text.find("$(", j)
                if j < 0:
                    break
                e = find_close(text, j + 2)
                self.sub(text[j + 2:e])
                j = e + 1
        except ParseError:
            pass


def tokenize(s, depth=0):
    return _Lexer(s, depth).run()
