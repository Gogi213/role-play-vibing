#!/usr/bin/env python3
"""Пульс (В-38), шаг 2: страница «Пульс» — крошечный сервер на stdlib, один файл.

    pulse_server.py --project <проект> [--host 127.0.0.1] [--port 8787] [--token <ключ>]

Проекты и откуда читать их `pulse.json` (путь или URL) — ключ `pulse_projects` в `<проект>/.claude/rpv.json`. Страница живёт по
адресу `/<ключ>/` (ключ — `--token` или переменная PULSE_TOKEN; без ключа сервер не стартует), `/<ключ>/body` — только
содержимое для автообновления раз в 30 с. Клетки и фраза — из pulse.json как есть (`dispatcher/pulse_view.py`); ничего сверх макета.
"""
from __future__ import annotations

import argparse
import hmac
import os
import sys
from datetime import datetime
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "dispatcher"))
import pulse_view as V  # noqa: E402

WINDOW = 32  # клеток на широком экране: 8 часов, правый край — текущая клетка
NARROW = 12  # на телефоне (< 480 px): сутки по 2 часа в клетке
GROUP = 8    # 15-минутных клеток в 2 часах
LEGEND = ('<div class="leg"><span><i style="background:var(--card);border:0.5px solid var(--line)"></i>'
          '<i style="background:#C0DD97"></i><i style="background:#639922"></i> тихо → работа за 15 мин</span>'
          '<span><i style="background:#FAC775"></i> ждали вас</span><span><i style="background:#F09595"></i> застряло</span></div>')


def level(cls: str, runs: int, step: int = 1) -> str:
    """Класс клетки страницы: n/m/h — работа (темнее = больше запусков), y — ждали владельца, o — застряло, '' — пусто."""
    if cls == "owner":
        return "y"
    if cls == "stuck":
        return "o"
    if cls == "work" or runs:
        return "h" if runs >= 4 * step else "m" if runs >= 2 * step else "n"
    return ""


def _spans(classes: list, cur: int) -> str:
    return "".join(f'<span class="{(c + (" cur" if i == cur else "")).strip()}"></span>' for i, c in enumerate(classes))


def wide_cells(view: dict) -> str:
    idx = view["idx"]
    cells = [view["cells"][i] if 0 <= i < len(view["cells"]) else ("none", 0) for i in range(idx - WINDOW + 1, idx + 1)]
    return f'<div class="c w">{_spans([level(*c) for c in cells], WINDOW - 1)}</div>'


def narrow_cells(view: dict) -> str:
    out = []
    for g in range(NARROW):
        part = view["cells"][g * GROUP:(g + 1) * GROUP]
        kinds = {c[0] for c in part}
        runs = sum(c[1] for c in part)
        out.append("o" if "stuck" in kinds else "y" if "owner" in kinds else level("work", runs, GROUP // 2) if runs or "work" in kinds else "")
    return f'<div class="c k">{_spans(out, view["idx"] // GROUP)}</div>'


def meta_of(view: dict) -> str:
    first = view["now"] or (V.tg_line(view).split(": ", 1)[-1] if view["state"] != "ok" else "")
    lines = [x for x in (first, view["pause"]) if x]
    return f'<div class="meta {"you" if view["state"] == "owner" else view["state"] if view["state"] == "stuck" else ""}">' \
           f'{"<br>".join(escape(x) for x in lines)}</div>'


def row(view: dict) -> str:
    return f'<div class="r"><b>{escape(view["name"])}</b>{wide_cells(view)}{narrow_cells(view)}{meta_of(view)}</div>'


def hours(idx: int) -> str:
    first = (idx - WINDOW + 1) // 4
    return ('<div class="r" style="padding-top:18px;padding-bottom:2px"><span></span><div class="hl">'
            + "".join(f"<span>{(first + k) % 24:02d}</span>" for k in range(WINDOW // 4)) + "</div><span></span></div>")


def body_html(views: list, now: datetime) -> str:
    """Содержимое рамки: фраза и часы, часы окна, по строке на проект, легенда."""
    states = {v["state"] for v in views}
    cls = "stuck" if "stuck" in states else "you" if "owner" in states else ""
    idx = max((v["idx"] for v in views), default=0)
    return (f'<div class="head"><div class="phrase {cls}">{escape(V.summary(views))}</div>'
            f'<div class="meta">{now:%H:%M} · сегодня</div></div>'
            + (hours(idx) if views else "") + "".join(row(v) for v in views) + LEGEND)


def page_html(views: list, now: datetime) -> str:
    return (HERE / "pulse.html").read_text(encoding="utf-8").replace("__BODY__", body_html(views, now))


def make_handler(project, token: str, fetch=None):
    kwargs = {"fetch": fetch} if fetch else {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            parts = self.path.split("?")[0].strip("/").split("/")
            if not parts or not hmac.compare_digest(parts[0].encode(), token.encode()) or len(parts) > 2:
                return self._send(404, "text/plain; charset=utf-8", "не найдено")
            now = datetime.now().astimezone()
            views = V.load_all(project, now, **kwargs)
            if len(parts) == 1:
                return self._send(200, "text/html; charset=utf-8", page_html(views, now))
            if parts[1] == "body":
                return self._send(200, "text/html; charset=utf-8", body_html(views, now))
            self._send(404, "text/plain; charset=utf-8", "не найдено")

        def _send(self, code: int, kind: str, text: str):
            data = text.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):  # ключ в пути — в журнал доступа не пишем
            pass

    return Handler


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="pulse_server.py", description=__doc__.split("\n\n")[0])
    ap.add_argument("--project", required=True)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--token", default=os.environ.get("PULSE_TOKEN", ""))
    a = ap.parse_args(argv)
    if not a.token:
        print("[pulse] нужен ключ в пути: --token или PULSE_TOKEN", file=sys.stderr)
        return 2
    project = Path(a.project).resolve()
    if not V.projects_of(project):
        print(f"[pulse] в {project}/.claude/rpv.json нет pulse_projects — страница будет пустой", file=sys.stderr)
    ThreadingHTTPServer((a.host, a.port), make_handler(project, a.token)).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
