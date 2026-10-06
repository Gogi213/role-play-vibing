#!/usr/bin/env python3
"""Табло v2: живой TUI на Textual (нужен пакет `textual`). Только рисует раздел `view2` из `<проект>/.claude/pulse/status.json`,
который раз в 5 с пишет `board_push.py --loop 5` (плагин, `.claude/dispatcher/`).

    python .claude/board/board.py [--project <путь>]   # живое табло (данные раз в 2 с)
    python .claude/board/board.py --sample             # принудительно пример из sample-view2.json (с пометкой «ПРИМЕР»)

Корень проекта — `--project`, иначе RPV_PROJECT, CLAUDE_PROJECT_DIR, иначе поиск вверх от текущего каталога (`project.py`).
Клавиши: ↑↓ выбор процесса · enter «процесс подробно» · esc назад · m ресурсы машин · клавиша варианта (a/b/y/n…)
отвечает на вопрос (при одинаковых клавишах у двух вопросов — сначала цифра вопроса), enter — подтвердить, esc — отмена ·
q выход. Ответ — `ask.py answer <id> <key>` из `.claude/dispatcher/`.
Контракт данных — VIEW2.md рядом. Время — GMT+4.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

os.environ.setdefault("TEXTUAL_COLOR_SYSTEM", "truecolor")

from rich.console import Group  # noqa: E402
from rich.style import Style  # noqa: E402
from rich.text import Text  # noqa: E402
from textual.app import App, ComposeResult  # noqa: E402
from textual.containers import Horizontal, VerticalScroll  # noqa: E402
from textual.screen import Screen  # noqa: E402
from textual.theme import Theme  # noqa: E402
from textual.widget import Widget  # noqa: E402

HERE = Path(__file__).resolve().parent
DISP = HERE.parent / "dispatcher"
sys.path.insert(0, str(DISP))
import project  # noqa: E402

ROOT = HERE  # корень проекта — в main() (project.resolve_project); до этого — заглушка для импорта
STATUS = ROOT / ".claude" / "pulse" / "status.json"
SAMPLE = HERE / "sample-view2.json"
ASK = DISP / "ask.py"
STALE_S = 30

# --- палитра макета владельца ------------------------------------------------------------------------------------------
BG, FG, DIM, WHITE = "#0E1014", "#C9CED6", "#7C8494", "#F4F5F7"
GREEN, BLUE, AMBER, RED = "#5FD38D", "#6AA9FF", "#F2B84B", "#F27C6D"
LINE, HEAD_BG, ARROW, SEP, KEYBG = "#2A3140", "#1A1F29", "#4A5262", "#1C212B", "#262C38"
TAGC = {
    "purple": dict(bg="#3A2F5C", fg="#D9CCFF", border="#4A3F73", lane="#14111D", arrow="#D9CCFF"),
    "teal": dict(bg="#0F4A50", fg="#9BEAF0", border="#1B5E64", lane="#0E1719", arrow="#9BEAF0"),
    "blue": dict(bg="#1B3355", fg="#A9CBFF", border="#2D4A73", lane="#0F1520", arrow="#A9CBFF"),
    "gray": dict(bg="#1E2430", fg="#7C8494", border="#2A3140", lane="#12151B", arrow="#7C8494"),
    "amber": dict(bg="#F2B84B", fg="#1A1405", border="#F2B84B", lane="#17140D", arrow="#F2B84B"),
}
DEFAULT_TAGS = {
    "pc": {"tag": "ПК", "name": "этот ПК", "color": "purple"},
    "vps": {"tag": "VPS", "name": "сервер", "color": "teal"},
    "calc": {"tag": "СЧЁТ", "name": "сервер счёта", "color": "blue"},
    "col": {"tag": "КОЛ", "name": "сборщик", "color": "gray"},
    "you": {"tag": "ВЫ", "name": "вы", "color": "amber"},
}
ICON = {"done": ("✓", GREEN), "run": ("▸", BLUE), "wait": ("⏸", AMBER), "todo": ("○", DIM), "bad": ("✗", RED)}
CHIP_BORDER = {"done": "#2B5A40", "run": BLUE, "wait": AMBER, "todo": LINE, "bad": RED}
CHIP_BG = {"run": "#121B2A", "wait": "#1A160C", "bad": "#1E1212"}
CONN = {"done": "#3E8E63", "run": BLUE, "wait": AMBER, "bad": RED, "todo": ARROW}
SEL_BG = "#121722"

Seg = tuple  # (текст, стиль-строка); сегмент со стилем «… on #цвет» (метка, клавиша) при переносе не рвётся


# --- стили и текст -----------------------------------------------------------------------------------------------------
_SC: dict = {}


def S(x) -> Style:
    if isinstance(x, Style):
        return x
    st = _SC.get(x)
    if st is None:
        st = _SC[x] = Style.parse(x) if x else Style()
    return st


def clen(segs) -> int:
    return sum(len(t) for t, _ in segs)


def mk(segs, w: int, bg: str | None = None) -> Text:
    """Строка из сегментов, добитая пробелами до ширины w (no_wrap, без переноса)."""
    t = Text(no_wrap=True, overflow="crop")
    for tx, st in segs:
        t.append(tx, S(st))
    if w > t.cell_len:
        t.append(" " * (w - t.cell_len))
    if bg:
        t.stylize_before(Style(bgcolor=bg))
    return t


def wrap_segs(segs, width: int, indent: int = 0, first: int | None = None) -> list:
    """Перенос по словам для сегментов; метки/клавиши (стиль с фоном) — неделимы. Возвращает список строк-из-сегментов."""
    width = max(8, width)
    first = indent if first is None else first
    toks = []
    for tx, st in segs:
        if " on " in st or not tx.strip():
            toks.append((tx, st, not tx.strip()))
        else:
            toks += [(m.group(), st, m.group().isspace()) for m in re.finditer(r"\s+|\S+", tx)]
    lines, cur, space = [[("", "")]], first, None
    if first:
        lines[0] = [(" " * first, "")]
    for tx, st, is_sp in toks:
        if is_sp:
            if cur > (first if len(lines) == 1 else indent):
                space = (tx, st)
            continue
        need = cur + (len(space[0]) if space else 0) + len(tx)
        base = first if len(lines) == 1 else indent
        if cur > base and need > width:
            lines.append([(" " * indent, "")] if indent else [])
            cur, space = indent, None
        if space:
            lines[-1].append(space)
            cur += len(space[0])
            space = None
        while len(tx) > width - cur and not (" on " in st) and width - cur > 0:  # слово длиннее строки
            lines[-1].append((tx[: width - cur], st))
            tx = tx[width - cur:]
            lines.append([(" " * indent, "")] if indent else [])
            cur = indent
        lines[-1].append((tx, st))
        cur += len(tx)
    return lines


def short(title: str, n: int) -> str:
    """Название по словам до n знаков (число/ETA рядом не режем — они не часть названия)."""
    if len(title) <= n:
        return title
    words = title.split()
    while len(words) > 1 and len(" ".join(words)) > n:
        words.pop()
    t = " ".join(words)
    return t if len(t) <= n else t[: max(1, n - 1)] + "…"


def mins(m) -> str:
    m = int(round(float(m)))
    return "<1 мин" if m < 1 else f"{m} мин" if m < 60 else f"{m // 60} ч {m % 60:02d} мин"


# --- рисование по ячейкам (рамки фишек, дорожки) -----------------------------------------------------------------------
class Canvas:
    def __init__(self, w: int, h: int, bg: str | None = None):
        self.w, self.h = w, h
        base = Style(bgcolor=bg) if bg else Style()
        self.ch = [[" "] * w for _ in range(h)]
        self.st = [[base] * w for _ in range(h)]

    def put(self, x: int, y: int, text: str, style="", clip: int | None = None):
        sty = S(style)
        for c in text:
            if 0 <= y < self.h and 0 <= x < self.w and (clip is None or x < clip):
                self.ch[y][x] = c
                self.st[y][x] = Style(bgcolor=self.st[y][x].bgcolor) + sty
            x += 1

    def puts(self, x: int, y: int, segs, clip: int | None = None):
        for tx, st in segs:
            self.put(x, y, tx, st, clip)
            x += len(tx)

    def fill(self, x: int, y: int, w: int, h: int, bg: str):
        for yy in range(y, y + h):
            for xx in range(x, x + w):
                if 0 <= yy < self.h and 0 <= xx < self.w:
                    self.ch[yy][xx] = " "
                    self.st[yy][xx] = Style(bgcolor=bg)

    def box(self, x: int, y: int, w: int, h: int, color: str, bg: str | None = None, dashed: bool = False):
        if bg:
            self.fill(x, y, w, h, bg)
        hz, vt = ("┄", "┆") if dashed else ("─", "│")
        self.put(x, y, "╭" + hz * (w - 2) + "╮", color)
        self.put(x, y + h - 1, "╰" + hz * (w - 2) + "╯", color)
        for yy in range(y + 1, y + h - 1):
            self.put(x, yy, vt, color)
            self.put(x + w - 1, yy, vt, color)

    def lines(self) -> list:
        out = []
        for y in range(self.h):
            t, run, cur = Text(no_wrap=True, overflow="crop"), "", None
            for x in range(self.w):
                st = self.st[y][x]
                if run and st != cur:
                    t.append(run, cur)
                    run = ""
                cur = st
                run += self.ch[y][x]
            if run:
                t.append(run, cur)
            out.append(t)
        return out


# --- данные ------------------------------------------------------------------------------------------------------------
def tagdefs(d: dict) -> dict:
    t = {k: dict(v) for k, v in DEFAULT_TAGS.items()}
    t.update(d.get("tags") or {})
    return t


def tcol(d: dict, mid) -> dict:
    return TAGC.get(tagdefs(d).get(mid, {}).get("color"), TAGC["gray"])


def tseg(d: dict, mid) -> tuple:
    if not mid:
        return ("", "")
    t = tagdefs(d).get(mid)
    c = tcol(d, mid)
    return (f" {t['tag'] if t else mid} ", f"bold {c['fg']} on {c['bg']}")


def linkify(d: dict, text: str, style: str = DIM) -> list:
    """Слова-названия машин («VPS», «ПК») в тексте — метками."""
    names = {t["tag"]: k for k, t in tagdefs(d).items()}
    out = []
    for m in re.finditer(r"\s+|\S+", text or ""):
        w = m.group()
        core = w.rstrip(".,;:)»")
        if core in names:
            out += [tseg(d, names[core]), (w[len(core):], style)]
        else:
            out.append((w, style))
    return out


def key_seg(k: str, hot: bool = False) -> tuple:
    return (f" {k} ", f"bold #1A1405 on {AMBER}" if hot else f"{WHITE} on {KEYBG}")


def procs_sorted(d: dict) -> list:
    return sorted(d.get("processes") or [], key=lambda p: p.get("state") == "done")  # активные сверху, стабильно


def cur_step(p: dict):
    steps = p.get("steps") or []
    k = p.get("step_now")
    if k:
        for s in steps:
            if s.get("n") == k:
                return s
    for s in steps:
        if s.get("state") != "done":
            return s
    return steps[-1] if steps else None


def qmap_of(d: dict) -> dict:
    return {q["id"]: q for q in d.get("questions") or []}


def step_label(s: dict, qm: dict) -> str:
    q = qm.get(s.get("question") or "")
    return f"вопрос {q['n']}" if q else s.get("title", "")


def disp_state(p: dict) -> str:
    cs = cur_step(p)
    if p.get("state") in ("done", "todo"):
        return p["state"]
    return cs.get("state", p.get("state", "todo")) if cs else p.get("state", "todo")


def status_segs(p: dict) -> list:
    st = disp_state(p)
    icon, col = ICON.get(st, ICON["todo"])
    if st == "done":
        return [("✓ готово", GREEN)]
    if st == "todo":
        return [("○ впереди", DIM)]
    base = f"{icon} шаг {p.get('step_now', '?')} из {p.get('steps_total', len(p.get('steps') or []))}"
    if st == "run":
        return [(base, BLUE)] + ([(f" · ~{mins(p['eta_min'])}", DIM)] if p.get("eta_min") is not None else [])
    return [(base + (" · ждёт вас" if st == "wait" else " · проблема"), col)]


def flow_segs(d: dict, p: dict, with_for: bool) -> list:
    segs = []
    for i, f in enumerate(p.get("flow") or []):
        if i:
            segs.append((" → ", DIM))
        segs += [(f["text"] + " ", DIM), tseg(d, f.get("on"))]
    fr = p.get("for")
    if with_for and fr:
        segs += [(" → для ", DIM), tseg(d, fr["on"]), (" " + fr["text"], DIM)] if fr.get("on") else [(" · " + fr["text"], DIM)]
    return segs


# --- шапка / машины ----------------------------------------------------------------------------------------------------
def head_lines(app, w: int) -> list:
    d = app.d
    left = [(" RPV ", f"bold {BG} on #E6E8EC"), ("  ", ""), (d.get("time", "--:--"), WHITE)]
    if app.sample:
        left += [("  ", ""), (" ПРИМЕР ", f"bold {BG} on {AMBER}")]
    if app.stale_s and app.stale_s > STALE_S:
        left += [(f"  данные устарели на {int(app.stale_s)} с", RED)]
    hl = d.get("headline") or {}
    hs = hl.get("state", "ok")
    ic, col = {"wait": ("⏸", AMBER), "bad": ("✗", RED)}.get(hs, ("●", GREEN))
    c = d.get("counters") or {}
    right = [(f"{ic} {hl.get('text', '')}", f"bold {col}"), ("   ", ""), ("шаги ", DIM),
             (f"✓{c.get('done', 0)}", GREEN), (" ", ""), (f"▸{c.get('run', 0)}", BLUE), (" ", ""),
             (f"⏸{c.get('wait', 0)}", AMBER), (" ", ""), (f"○{c.get('todo', 0)}", DIM)]
    if c.get("bad"):
        orph = sum(m.get("orphans", 0) for m in d.get("machines") or [])
        right += [("   ", ""), (f"✗ {c['bad']} {'без хозяина' if orph else 'проблем'}", RED)]
    if clen(left) + 2 + clen(right) <= w:
        return [mk(left + [(" " * (w - clen(left) - clen(right)), "")] + right, w, HEAD_BG)]
    return [mk(left, w, HEAD_BG), mk(right, w, HEAD_BG)]


def machines_lines(app, w: int) -> list:
    d = app.d
    ms = d.get("machines") or []
    cols = 4 if w >= 100 else 2
    cw = (w - (cols - 1)) // cols
    out = []
    for r in range(0, len(ms), cols):
        cards = []
        for m in ms[r:r + cols]:
            off, down = m.get("state") == "off", m.get("state") == "down"
            t = tagdefs(d).get(m["id"], {})
            c = tcol(d, m["id"])
            inner = cw - 4
            status = [("✗ нет связи", RED)] if down else [("○ выключен", DIM)] if off else [("● в сети", GREEN)]
            title = [tseg(d, m["id"]), (" ", ""), (t.get("name", m["id"]), f"bold {DIM if off else WHITE}")]
            lines = [(title, status if clen(title) + 1 + clen(status) <= inner else None)]
            if lines[0][1] is None:
                lines.append((status, None))
            for ln in wrap_segs([(m.get("load", ""), DIM)], inner):
                lines.append((ln, None))
            now = m.get("now") or {}
            ns = now.get("state", "idle")
            if ns == "off":
                lines.append(([(now.get("text", ""), DIM)], None))
            else:
                ic, col = {"run": ("▸ ", BLUE), "wait": ("⏸ ", AMBER), "bad": ("✗ ", RED)}.get(ns, ("", DIM))
                row = [("сейчас ", FG), (ic + now.get("text", ""), col)]
                if m.get("orphans"):
                    row += [(" · ", DIM), (f"✗ {m['orphans']}", RED)]
                lines += [(ln, None) for ln in wrap_segs(row, inner)]
            if app.show_res:
                parts = [f"ЦП {m['cpu']} %" if m.get("cpu") is not None else "",
                         f"ОЗУ {m['mem']} %" if m.get("mem") is not None else "",
                         f"диск {m['disk_mb_s']} МБ/с" if m.get("disk_mb_s") is not None else ""]
                lines.append(([(" · ".join(p for p in parts if p), DIM)], None))
            cards.append((lines, RED if down else LINE if off else c["border"], off))
        h = max(len(c[0]) for c in cards) + 2
        cv = Canvas(w, h)
        for k, (lines, border, off) in enumerate(cards):
            x = k * (cw + 1)
            cv.box(x, 0, cw, h, border, dashed=off)
            for j, (segs, rt) in enumerate(lines):
                cv.puts(x + 2, 1 + j, segs, clip=x + cw - 2)
                if rt:
                    cv.puts(x + cw - 2 - clen(rt), 1 + j, rt)
        out += cv.lines()
    return out


# --- процессы: обзор ---------------------------------------------------------------------------------------------------
def arrow_for(d: dict, prev: dict, nxt: dict) -> tuple:
    if nxt.get("state") == "todo":
        return ("┄▶", ARROW)
    pm, nm = prev.get("on"), nxt.get("on")
    if pm != nm and pm not in (None, "you") and nm not in (None, "you"):
        return ("═▶", tcol(d, nm)["arrow"])
    return ("─▶", {"done": ARROW}.get(nxt.get("state"), CONN.get(nxt.get("state"), ARROW)))


def draw_chip(cv: Canvas, d: dict, x: int, y: int, w: int, s: dict, qm: dict):
    st = s.get("state", "todo")
    icon, col = ICON.get(st, ICON["todo"])
    cv.box(x, y, w, 4, CHIP_BORDER.get(st, LINE), CHIP_BG.get(st), dashed=(st == "todo"))
    extra = []
    if st == "done" and s.get("pct") is not None:
        extra = [(f" {s['pct']} %", GREEN)]
    elif st == "run":
        extra = [(f" ~{mins(s['eta_min'])}", BLUE)] if s.get("eta_min") is not None else \
            [(f" {s['pct']} %", BLUE)] if s.get("pct") is not None else []
    title = short(step_label(s, qm), w - 4 - 2 - clen(extra))
    ts = {"done": FG, "run": f"bold {WHITE}", "wait": f"bold {AMBER}", "todo": DIM}.get(st, FG)
    cv.puts(x + 2, y + 1, [(icon + " ", col), (title, ts)] + extra)
    line2 = [tseg(d, s.get("on"))]
    who = s.get("for") if st == "bad" else (s.get("who") if s.get("who") != "вы" else "")
    if who:
        line2.append((" " + who, DIM))
    cv.puts(x + 2, y + 2, line2, clip=x + w - 2)


def glyphs(steps: list) -> list:
    return [(ICON.get(s.get("state"), ICON["todo"])[0], ICON.get(s.get("state"), ICON["todo"])[1]) for s in steps]


def proc_lines(app, p: dict, w: int, selected: bool) -> list:
    d, qm = app.d, qmap_of(app.d)
    gut, inner = 2, w - 2
    steps = p.get("steps") or []
    n = max(1, len(steps))
    chip_w = min(23, (inner - 2 - 4 * (n - 1)) // n)
    cs = cur_step(p)
    status = status_segs(p)
    left = [(f"{p.get('n', '')}  ", DIM), (p.get("title", ""), f"bold {WHITE}"), ("   ", "")]
    body = []
    for with_for in (True, False):
        fl = flow_segs(d, p, with_for)
        if clen(left) + clen(fl) + 2 + clen(status) <= inner:
            body.append(left + fl + [(" " * (inner - clen(left) - clen(fl) - clen(status)), "")] + status)
            break
    else:
        body.append(left[:2] + [(" " * max(1, inner - clen(left[:2]) - clen(status)), "")] + status)
        body += wrap_segs(flow_segs(d, p, True), inner, 3)
    chain = []
    if chip_w >= 19:
        cv = Canvas(n * chip_w + 4 * (n - 1), 4)
        for i, s in enumerate(steps):
            x = i * (chip_w + 4)
            draw_chip(cv, d, x, 0, chip_w, s, qm)
            if i:
                at, ac = arrow_for(d, steps[i - 1], s)
                cv.put(x - 3, 1, at, ac)
        chain = cv.lines()
    else:  # не влезает цепочкой — строка «✓✓▸○○ шаг 3/5 · сверка ~5 мин [метка]»
        row = glyphs(steps) + [("  ", "")]
        if cs:
            row += [(f"шаг {p.get('step_now', '?')}/{p.get('steps_total', n)} · {step_label(cs, qm)}", FG)]
            if cs.get("state") == "run" and cs.get("eta_min") is not None:
                row += [(f" ~{mins(cs['eta_min'])}", BLUE)]
            row += [("  ", ""), tseg(d, cs.get("on"))]
        body += wrap_segs(row, inner, 3)
    bg = SEL_BG if selected else None
    gt = [("▌ ", BLUE)] if selected else [("  ", "")]
    out = [mk(gt + segs, w, bg) for segs in body]
    for ln in chain:
        t = mk(gt, 0)
        t.append_text(ln)
        if w > t.cell_len:
            t.append(" " * (w - t.cell_len))
        if bg:
            t.stylize_before(Style(bgcolor=bg))
        out.append(t)
    out.append(mk([("─" * w, SEP)], w))
    return out


def eff_lines(d: dict, q: dict, w: int, indent: int, app=None) -> list:
    """Варианты ответа: одной строкой с общим «→ последствие» или по строке на вариант."""
    opts = q.get("options") or []
    hot = app.pending[1].get("key") if app and app.pending and app.pending[0] is q else None
    effs = {o.get("effect") for o in opts}
    out = []
    if len(effs) <= 1:
        segs = []
        for o in opts:
            segs += [key_seg(o["key"], o["key"] == hot), (" " + o["label"] + "   ", FG)]
        if opts and opts[0].get("effect"):
            segs += [("→ ", DIM)] + linkify(d, opts[0]["effect"])
        out += wrap_segs(segs, w, indent + 2, indent)
    else:
        for o in opts:
            segs = [key_seg(o["key"], o["key"] == hot), (" " + o["label"], FG)]
            if o.get("effect"):
                segs += [("  → ", DIM)] + linkify(d, o["effect"])
            out += wrap_segs(segs, w, indent + 2, indent)
    return out


def ask_lines(app, w: int) -> list:
    d = app.d
    qs = d.get("questions") or []
    out = [mk([("ВАШ ХОД", f"bold {AMBER}")], w)]
    if not qs:
        return out + [mk([("вопросов к вам нет", DIM)], w)]
    for q in qs:
        num = (f" {q['n']} ", f"bold #1A1405 on {'#FFFFFF' if app.qfocus == q['n'] else AMBER}")
        out += [mk(ln, w) for ln in wrap_segs([num, (" ", ""), (q["text"], WHITE)], w, 4, 0)]
        if q["id"] in app.answered:
            out.append(mk([("    ✓ ответ записан: ", GREEN), (app.answered[q["id"]], DIM)], w))
        else:
            out += [mk(ln, w) for ln in eff_lines(d, q, w, 4, app)]
    return out


def feed_lines(app, w: int) -> list:
    d = app.d
    out = [mk([("ЛЕНТА", f"bold {DIM}")], w)]
    for f in (d.get("feed") or [])[-6:]:
        ic, col = ICON.get(f.get("state"), ICON["todo"])
        segs = [(f.get("time", ""), DIM), (" ", ""), (ic, col), (" ", "")]
        for i, m in enumerate(f.get("on") or []):
            segs.append(tseg(d, m))
        if f.get("to"):
            segs += [("→", DIM), tseg(d, f["to"])]
        segs += [(" " + f.get("text", ""), FG)]
        out += [mk(ln, w) for ln in wrap_segs(segs, w, 6)]
    return out


LEGEND = [("✓", GREEN), (" готово  ", DIM), ("▸", BLUE), (" идёт  ", DIM), ("⏸", AMBER), (" ждёт вас  ", DIM),
          ("○ впереди  ", DIM), ("✗", RED), (" проблема  ═▶ переход на другую машину", DIM)]


def answer_keys(app, pid=None) -> str:
    ks = []
    for q in app.d.get("questions") or []:
        if (pid is None or q.get("process") == pid) and q["id"] not in app.answered:
            ks += [o["key"] for o in q.get("options") or [] if o["key"] not in ks]
    return " ".join(ks)


def keys_lines(app, w: int) -> list:
    if app.pending:
        q, o = app.pending
        txt = f" Ответить „{o['label']}“ на вопрос {q['n']}? enter — да, esc — нет "
        return [mk([(txt, f"bold #1A1405 on {AMBER}")], w, AMBER)]
    on_proc = isinstance(app.screen, ProcessScreen)
    ak = answer_keys(app, app.screen.pid if on_proc else None)
    segs = [key_seg("esc"), (" к обзору   ", DIM)] if on_proc else \
        [key_seg("↑↓"), (" процесс   ", DIM), key_seg("enter"), (" раскрыть   ", DIM), key_seg("m"), (" ресурсы   ", DIM)]
    if ak:
        segs += [key_seg(ak), (" ответить   ", DIM)]
    segs += [key_seg("q"), (" выход", DIM)]
    if on_proc:
        return [mk(segs, w)]
    if clen(segs) + 6 + clen(LEGEND) <= w:
        return [mk(segs + [("      ", "")] + LEGEND, w)]
    return [mk(segs, w), mk(LEGEND, w)]


# --- экран «Процесс подробно» ------------------------------------------------------------------------------------------
def pscreen_proc(app):
    return next((p for p in app.d.get("processes") or [] if p["id"] == app.screen.pid), None)


def phead_lines(app, w: int) -> list:
    p = pscreen_proc(app)
    if not p:
        return [mk([("процесс закрыт", DIM)], w, HEAD_BG)]
    st = disp_state(p)
    ic, col = ICON.get(st, ICON["todo"])
    cs = cur_step(p)
    if st == "wait":
        txt = f"{ic} шаг {p.get('step_now')} из {p.get('steps_total')} · ждёт вас" + (f" {mins(p['wait_min'])}" if p.get("wait_min") is not None else "")
    elif st == "run":
        txt = f"{ic} шаг {p.get('step_now')} из {p.get('steps_total')}" + (f" · ~{mins(p['eta_min'])}" if p.get("eta_min") is not None else "")
    elif st == "bad":
        txt = f"{ic} шаг {p.get('step_now')} из {p.get('steps_total')} · проблема"
    else:
        txt = "✓ готово" if st == "done" else "○ впереди"
    left = [(" RPV › ", DIM), (p.get("title", ""), f"bold {WHITE}")]
    right = [(txt, f"bold {col}"), ("   ", ""), (app.d.get("time", ""), WHITE)]
    if app.sample:
        right = [(" ПРИМЕР ", f"bold {BG} on {AMBER}"), ("  ", "")] + right
    return [mk(left + [(" " * max(1, w - clen(left) - clen(right)), "")] + right, w, HEAD_BG)]


def pflow_lines(app, w: int) -> list:
    p, d = pscreen_proc(app), app.d
    if not p:
        return [mk([], w)]
    steps = p.get("steps") or []
    segs = []
    prev = None
    for f in p.get("flow") or []:
        actor = next((s.get("who") for s in steps if s.get("on") == f.get("on") and s.get("who") not in ("автомат", "вы")), None)
        name = tagdefs(d).get(f.get("on"), {}).get("name", "")
        if prev is not None:
            segs.append(("   " + ("═▶" if prev != f.get("on") else "─▶") + "   ", DIM))
        segs += [(f["text"] + " ", DIM), tseg(d, f.get("on")), (" " + name + (f", {actor}" if actor else ""), DIM)]
        prev = f.get("on")
    fr = p.get("for")
    if fr:
        segs.append(("   " + ("═▶" if prev != fr.get("on") else "─▶") + "   ", DIM))
        segs += [("результат для ", DIM), tseg(d, fr["on"]), (" " + fr["text"], DIM)] if fr.get("on") else [("· " + fr["text"], DIM)]
    return [mk(ln, w) for ln in wrap_segs(segs, w, 3)]


def sec_lines(title: str):
    return lambda app, w: [mk([(title, f"bold {DIM}")], w)]


def lanes_lines(app, w: int) -> list:
    """Дорожки по машинам: по строке на машину, шаг — фишка в своей колонке, соединители ┐└▶ между дорожками."""
    p, d = pscreen_proc(app), app.d
    if not p:
        return []
    steps, qm = p.get("steps") or [], qmap_of(app.d)
    ids = [m for m in tagdefs(d) if any(s.get("on") == m for s in steps)]
    ids += [s["on"] for s in steps if s.get("on") and s["on"] not in ids]
    lane = {m: i for i, m in enumerate(ids)}
    LW, GAP = 8, 5
    avail = w - LW - 1
    per = max(1, (avail + GAP) // (14 + GAP))
    nb = -(-len(steps) // per) if steps else 1
    size = -(-len(steps) // nb) if steps else 1
    out = []
    for b in range(0, len(steps), size):
        band = steps[b:b + size]
        cw = min(22, (avail + GAP) // len(band) - GAP)
        cv = Canvas(w, 1 + 2 * len(ids))
        for m, i in lane.items():
            cv.fill(0, 1 + 2 * i, w, 1, TAGC.get(tagdefs(d).get(m, {}).get("color"), TAGC["gray"])["lane"])
            cv.puts(1, 1 + 2 * i, [tseg(d, m)])
        for j, s in enumerate(band):
            x = LW + j * (cw + GAP)
            st = s.get("state", "todo")
            icon, col = ICON.get(st, ICON["todo"])
            first = (step_label(s, qm) if s.get("question") else s.get("title", "")).split()[:1]
            cv.put(x, 0, short(f"{s.get('n')} {first[0] if first else ''}", cw + GAP - 1),
                   {"wait": f"bold {AMBER}", "run": BLUE, "bad": RED}.get(st, DIM))
            y = 1 + 2 * lane.get(s.get("on"), 0)
            label = short(step_label(s, qm), cw - 4)
            cv.fill(x, y, cw, 1, CHIP_BG.get(st, BG))
            cv.puts(x + 1, y, [(f" {icon} ", col), (label, {"wait": f"bold {AMBER}", "run": f"bold {WHITE}", "todo": DIM}.get(st, FG))],
                    clip=x + cw)
            if j:
                px = x - GAP
                pa = lane.get(band[j - 1].get("on"), 0)
                a = lane.get(s.get("on"), 0)
                ya, yb = 1 + 2 * pa, y
                color = CONN.get(st, ARROW)
                hz = "┄" if st == "todo" else "─"
                if pa == a:
                    cv.put(px, ya, hz * (GAP - 1) + "▶", color)
                else:
                    cv.put(px, ya, hz * 2 + ("┐" if a > pa else "┘"), color)
                    for yy in range(min(ya, yb) + 1, max(ya, yb)):
                        cv.put(px + 2, yy, "│", color)
                    cv.put(px + 2, yb, "└" if a > pa else "┌", color)
                    cv.put(px + 3, yb, hz + "▶", color)
        out += cv.lines()
        out.append(mk([], w))
    return out[:-1] if out else out


def steps_lines(app, w: int) -> list:
    p, d = pscreen_proc(app), app.d
    if not p:
        return []
    qm = qmap_of(d)
    wid = [4, 26, 11, 14, 22, 26]
    tw = 8
    over = sum(wid) + tw - w
    for i, lo, cut in ((4, 14, 8), (1, 18, 8), (5, 20, 6), (3, 12, 2), (2, 9, 2)):
        take = min(max(0, over), max(0, wid[i] - lo), cut)
        wid[i] -= take
        over -= take
    tw = max(6, w - sum(wid))
    heads = ["#", "ШАГ", "КТО", "ДЕЛАЕТСЯ НА", "ДЛЯ", "СТАТУС", "ВРЕМЯ"]
    cols = wid + [tw]
    hl = Text(no_wrap=True, overflow="crop")
    for h, cwid in zip(heads, cols):
        hl.append((" " + h if h == "#" else h).ljust(cwid)[:cwid], Style(color="#0B0D10", bgcolor=GREEN))
    out = [hl]
    seen_wait = False
    for k, s in enumerate(p.get("steps") or []):
        st = s.get("state", "todo")
        icon, col = ICON.get(st, ICON["todo"])
        qq = qm.get(s.get("question") or "")
        title = s.get("title", "")
        stc = {"wait": f"bold {AMBER}", "run": BLUE, "bad": RED, "done": GREEN, "todo": DIM}[st]
        if st == "done":
            status = [("✓ готово", GREEN)]
        elif st == "run":
            status = [("▸ идёт", BLUE)] + ([(f" {s['pct']} %", BLUE)] if s.get("pct") is not None else []) + \
                ([(f" · ~{mins(s['eta_min'])}", DIM)] if s.get("eta_min") is not None else [])
        elif st == "wait":
            status = [(f"⏸ ждёт вас" + (f" с {s['started']}" if s.get("started") else ""), stc)]
        elif st == "bad":
            status = [("✗ проблема", RED)]
        else:
            prev = (p["steps"][k - 1] if k else {}).get("state")
            status = [("○ после ответа" if prev == "wait" else "○ впереди", DIM)]
        if st == "done":
            tm = s.get("finished") or "—"
        elif st == "run":
            tm = f"с {s['started']}" if s.get("started") else "—"
        elif st == "wait":
            tm = mins(qq["wait_min"] if qq and qq.get("wait_min") is not None else p.get("wait_min") or 0) if (qq or p.get("wait_min") is not None) else "—"
        else:
            tm = "—"
        dim = st == "todo"
        cells = [[(f" {s.get('n')}", f"bold {AMBER}" if st == "wait" else BLUE if st == "run" else DIM)],
                 [(title, DIM if dim else f"bold {WHITE}" if st in ("wait", "run") else WHITE)],
                 [(s.get("who", ""), DIM if dim else FG)],
                 [tseg(d, s.get("on"))],
                 linkify(d, s.get("for") or "", DIM if dim else FG),
                 status,
                 [(tm, AMBER if st == "wait" else DIM)]]
        wrapped = [wrap_segs(c, cw - 1) for c, cw in zip(cells, cols)]
        bg = "#2A2416" if st == "wait" else None
        for li in range(max(len(x) for x in wrapped)):
            segs = []
            for ln, cw in zip(wrapped, cols):
                piece = ln[li] if li < len(ln) else []
                segs += piece + [(" " * max(0, cw - clen(piece)), "")]
            out.append(mk(segs, w, bg))
        if s.get("detail"):
            out += [mk(ln, w, bg) for ln in wrap_segs([(s["detail"], DIM)], w, 4)]
    return out


def pquestion_lines(app, w: int) -> list:
    p, d = pscreen_proc(app), app.d
    out = []
    for q in app.d.get("questions") or []:
        if not p or q.get("process") != p["id"]:
            continue
        if out:
            out.append(mk([], w))
        head = [(f" ВОПРОС {q['n']} ", f"bold #1A1405 on {'#FFFFFF' if app.qfocus == q['n'] else AMBER}"), ("  ", ""), (q["text"], f"bold {WHITE}")]
        who = [(f"спросил {q.get('from', '')} с ", DIM), tseg(d, q.get("on"))]
        if clen(head) + 2 + clen(who) <= w:
            out.append(mk(head + [(" " * (w - clen(head) - clen(who)), "")] + who, w))
        else:
            out += [mk(ln, w) for ln in wrap_segs(head, w, 2)] + [mk(who, w)]
        if q["id"] in app.answered:
            out.append(mk([("✓ ответ записан: ", GREEN), (app.answered[q["id"]], DIM)], w))
        else:
            out += [mk(ln, w) for ln in eff_lines(d, q, w, 0, app)]
    return out or [mk([("вопросов по этому процессу нет", DIM)], w)]


# --- виджеты -----------------------------------------------------------------------------------------------------------
class Block(Widget):
    DEFAULT_CSS = "Block { height: auto; }"

    def __init__(self, fn, **kw):
        super().__init__(**kw)
        self.fn = fn

    def lines(self, w: int) -> list:
        return self.fn(self.app, w) or [Text("")]

    def get_content_height(self, container, viewport, width) -> int:
        return max(1, len(self.lines(max(10, width))))

    def render(self):
        return Group(*self.lines(max(10, self.content_size.width or 80)))


class ProcRow(Block):
    def __init__(self, pid: str, **kw):
        super().__init__(None, **kw)
        self.pid = pid

    def lines(self, w: int) -> list:
        p = next((x for x in self.app.d.get("processes") or [] if x["id"] == self.pid), None)
        return proc_lines(self.app, p, w, self.app.sel == self.pid) if p else [Text("")]

    def on_click(self) -> None:
        if self.app.sel == self.pid:
            self.app.open_proc()
        else:
            self.app.sel = self.pid
            self.app.redraw()


class Procs(VerticalScroll, can_focus=False):
    pass


class Overview(Screen):
    def on_mount(self) -> None:
        self.call_after_refresh(self.app.rebuild, True)

    def compose(self) -> ComposeResult:
        yield Block(head_lines, id="head")
        yield Block(sec_lines("МАШИНЫ"), classes="sec")
        yield Block(machines_lines, id="machines")
        yield Block(sec_lines("ПРОЦЕССЫ  шаги слева направо · под шагом — где он делается"), classes="sec")
        yield Procs(id="procs")
        with Horizontal(id="bottom"):
            yield Block(ask_lines, id="ask")
            yield Block(feed_lines, id="feed")
        yield Block(keys_lines, id="keys")


class ProcessScreen(Screen):
    def __init__(self, pid: str):
        super().__init__()
        self.pid = pid

    def compose(self) -> ComposeResult:
        yield Block(phead_lines, id="phead")
        with VerticalScroll(id="pbody"):
            yield Block(pflow_lines, id="pflow")
            yield Block(sec_lines("ПОСЛЕДОВАТЕЛЬНОСТЬ ПО МАШИНАМ"), classes="sec")
            yield Block(lanes_lines, id="lanes")
            yield Block(sec_lines("ШАГИ"), classes="sec")
            yield Block(steps_lines, id="steps")
            yield Block(pquestion_lines, id="pq")
        yield Block(keys_lines, id="pkeys")


# --- источник данных ---------------------------------------------------------------------------------------------------
def load_source(force_sample: bool):
    """(view2, пример?, built_ts)."""
    sample = json.loads(SAMPLE.read_text(encoding="utf-8"))["view2"]
    if force_sample:
        return sample, True, None
    try:
        st = json.loads(STATUS.read_text(encoding="utf-8"))
        ts = (st.get("view2") or {}).get("built_ts") or STATUS.stat().st_mtime
    except (OSError, ValueError, AttributeError):
        st, ts = None, None
    if isinstance(st, dict) and isinstance(st.get("view2"), dict):
        return st["view2"], False, ts
    return sample, True, None


def run_ask(qid: str, key: str):
    """Ответ владельца → ask.py. (True|False|None, текст); None — ответ не отправлен (не подключён / пример)."""
    if not ASK.exists():
        return None, "ответ пока не подключён"
    try:
        r = subprocess.run([sys.executable, str(ASK), "answer", qid, key], capture_output=True, text=True,
                           encoding="utf-8", timeout=30, cwd=str(ROOT), env=dict(os.environ, RPV_PROJECT=str(ROOT)))
    except Exception as e:
        return False, f"ask.py не отработал: {e}"
    if r.returncode == 0:
        return True, ""
    return False, ((r.stderr or r.stdout).strip().splitlines() or ["ошибка ответа"])[-1]


def sample_ask(qid: str, key: str):
    return None, "пример: ответ не отправлен"


# --- приложение --------------------------------------------------------------------------------------------------------
class Board(App):
    ENABLE_COMMAND_PALETTE = False
    CSS = f"""
    Screen {{ background: {BG}; color: {FG}; }}
    #head, #phead {{ dock: top; padding: 0 2; background: {HEAD_BG}; }}
    #keys, #pkeys {{ dock: bottom; padding: 0 2; }}
    .sec {{ margin: 1 2 0 2; height: auto; }}
    #machines {{ margin: 0 2; }}
    #procs {{ height: 1fr; margin: 0 2; min-height: 4; scrollbar-size-vertical: 1; scrollbar-color: {LINE}; scrollbar-background: {BG}; }}
    ProcRow {{ width: 100%; }}
    #bottom {{ height: auto; max-height: 16; margin: 0 2; }}
    #ask {{ width: 3fr; border: round {AMBER}; background: #16140E; padding: 0 1; }}
    #feed {{ width: 2fr; border: round {LINE}; padding: 0 1; margin-left: 1; }}
    #pbody {{ margin: 0 2; height: 1fr; scrollbar-size-vertical: 1; scrollbar-color: {LINE}; scrollbar-background: {BG}; }}
    #pq {{ margin-top: 1; border: round {AMBER}; background: #16140E; padding: 0 1; }}
    Toast {{ background: #1A1F29; color: {WHITE}; }}
    """

    def __init__(self, sample: bool = False):
        super().__init__()
        self.force_sample = sample
        self.d: dict = {}
        self.sample = True
        self.stale_s = 0.0
        self.sel: str | None = None
        self.show_res = False
        self.pending = None  # (question, option)
        self.qfocus: int | None = None
        self.answered: dict = {}  # id вопроса → подпись выбранного варианта
        self.answer_fn = sample_ask if sample else run_ask
        self._raw = None

    def on_mount(self) -> None:
        self.register_theme(Theme(name="rpv", primary=BLUE, secondary=DIM, warning=AMBER, error=RED, success=GREEN,
                                  accent=AMBER, foreground=FG, background=BG, surface=BG, panel=HEAD_BG, dark=True))
        self.theme = "rpv"
        self.push_screen(Overview())
        self.tick()
        self.set_interval(2, self.tick)

    # данные
    def tick(self) -> None:
        d, sample, ts = load_source(self.force_sample)
        self.sample = sample
        self.stale_s = (time.time() - float(ts)) if (ts and not sample) else 0.0
        if not self.force_sample and sample is False:
            pass
        raw = json.dumps(d, sort_keys=True)
        changed = raw != self._raw
        self._raw = raw
        self.d = d
        ids = [p["id"] for p in procs_sorted(d)]
        if self.sel not in ids:
            self.sel = ids[0] if ids else None
        live_q = {q["id"] for q in d.get("questions") or []}
        for qid in [k for k in self.answered if k not in live_q]:
            del self.answered[qid]
        self.rebuild(changed or bool(self.stale_s))

    def rebuild(self, force: bool = False) -> None:
        scr = self.screen_stack[0] if self.screen_stack else None
        for s in self.screen_stack:
            if isinstance(s, Overview):
                scr = s
        if not isinstance(scr, Overview) or not scr.is_mounted:
            return
        procs = scr.query_one("#procs")
        ids = [p["id"] for p in procs_sorted(self.d)]
        if [r.pid for r in procs.query(ProcRow)] != ids:
            procs.remove_children()
            procs.mount(*[ProcRow(i) for i in ids])
            force = True
        if force:
            self.redraw()

    def redraw(self) -> None:
        for s in self.screen_stack:
            for b in s.query(Block):
                b.refresh(layout=True)
        if isinstance(self.screen, Overview):
            for r in self.screen.query(ProcRow):
                if r.pid == self.sel:
                    r.scroll_visible(animate=False)

    def open_proc(self) -> None:
        if self.sel and isinstance(self.screen, Overview):
            self.qfocus = None
            self.push_screen(ProcessScreen(self.sel))

    # ввод
    def cands(self) -> list:
        pid = self.screen.pid if isinstance(self.screen, ProcessScreen) else None
        return [q for q in self.d.get("questions") or [] if q["id"] not in self.answered and (pid is None or q.get("process") == pid)]

    def on_key(self, event) -> None:
        key, ch = event.key, (event.character or "")
        event.stop()
        if self.pending:
            if key == "enter":
                q, o = self.pending
                self.pending = None
                self.redraw()
                self.run_worker(self.send_answer(q, o), exclusive=False)
            elif key == "escape":
                self.pending = None
                self.redraw()
            return
        if key in ("q", "ctrl+q"):
            self.exit()
            return
        on_proc = isinstance(self.screen, ProcessScreen)
        if key == "escape":
            if self.qfocus:
                self.qfocus = None
                self.redraw()
            elif on_proc:
                self.pop_screen()
                self.redraw()
            return
        if not on_proc:
            ids = [p["id"] for p in procs_sorted(self.d)]
            if key in ("up", "down") and ids:
                i = ids.index(self.sel) if self.sel in ids else 0
                self.sel = ids[max(0, min(len(ids) - 1, i + (1 if key == "down" else -1)))]
                self.redraw()
                return
            if key == "enter":
                self.open_proc()
                return
            if key == "m":
                self.show_res = not self.show_res
                self.redraw()
                return
        if not ch or not ch.strip():
            return
        ch = ch.lower()
        qs = self.cands()
        foc = [q for q in qs if q["n"] == self.qfocus]
        if ch.isdigit():
            if foc and any(o["key"].lower() == ch for o in foc[0].get("options") or []):
                pass  # цифра — вариант выбранного вопроса
            else:
                hit = [q for q in qs if str(q["n"]) == ch]
                if hit:
                    self.qfocus = hit[0]["n"]
                    self.redraw()
                return
        pool = foc or qs
        hits = [(q, o) for q in pool for o in q.get("options") or [] if o["key"].lower() == ch]
        if len(hits) == 1:
            self.pending = hits[0]
            self.redraw()
        elif len(hits) > 1:
            self.notify(f"Клавиша «{ch}» есть у нескольких вопросов — сначала выберите вопрос цифрой", severity="warning")

    async def send_answer(self, q: dict, o: dict) -> None:
        import asyncio  # noqa: PLC0415

        ok, msg = await asyncio.to_thread(self.answer_fn, q["id"], o["key"])
        if ok:
            self.answered[q["id"]] = o["label"]
            self.qfocus = None
            self.notify("Ответ записан")
        else:
            self.notify(msg or "Ответ не записан", severity="warning" if ok is None else "error", timeout=6)
        self.redraw()


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    global ROOT, STATUS
    argv = sys.argv[1:]
    try:
        ROOT = project.resolve_project(argv)
    except project.ProjectNotFound as e:
        print(e, file=sys.stderr)
        return 2
    STATUS = ROOT / ".claude" / "pulse" / "status.json"
    Board(sample="--sample" in argv).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
