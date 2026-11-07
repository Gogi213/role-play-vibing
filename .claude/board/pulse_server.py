#!/usr/bin/env python3
"""Пульс (В-38), шаг 2; v2 — TK-105 (В-50, В-51): страница «Пульс» — крошечный сервер на stdlib, один файл.

    pulse_server.py --project <проект> [--host 127.0.0.1] [--port 8787] [--token <ключ>] [--inbox <каталог> --write-token <ключ>]

Проекты и откуда читать их `pulse.json` (путь или URL) — ключ `pulse_projects` в `<проект>/.claude/rpv.json`. Страница живёт по
адресу `/<ключ>/` (ключ — `--token` или переменная PULSE_TOKEN; без ключа сервер не стартует), `/<ключ>/body` — только
содержимое для автообновления раз в 30 с. Шапка, итоги дня, карточки и подсказки клеток — из pulse.json как есть
(`dispatcher/pulse_view.py`); макет — docs/pulse-mockup-v2.html. Фильтр и раскрытые карточки живут в браузере (pulse.html).

Сервер без доступа к машинам проектов (TK-113, В-56): `--inbox` — каталог, куда проекты сами присылают свой `pulse.json`
(`PUT /put/<имя>` с `Authorization: Bearer <ключ записи>`; ключ записи — `--write-token` или PULSE_WRITE_TOKEN, не тот, что открывает
страницу). Присланные файлы — проекты страницы наравне с `pulse_projects`; молчит проект дольше порога — «нет данных N мин».
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import sys
from datetime import datetime
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "dispatcher"))
import pulse_view as V  # noqa: E402

MAX_BODY = 512 * 1024  # байт: pulse.json сутки — десятки КБ (alpha ~65 КБ), больше — не он
NAME = re.compile(r"[A-Za-z0-9_-]{1,40}")  # имя проекта в пути приёма и имя файла в inbox
WINDOW = 32      # клеток на широком экране: 8 часов, правый край — текущая клетка
NARROW = 16      # на телефоне (< 760 px): 4 часа
PER_HOUR = 4     # 15-минутных клеток в часе
CELL_MIN = 60 // PER_HOUR
TIP_RUNS = 3     # строк запусков в подсказке клетки (остальные — «и ещё N»)
KIND = {"ok": "", "owner": "owner", "stuck": "stuck"}  # класс шапки и строки «сейчас»
SEG = (("all", "Все"), ("ok", "Идёт"), ("owner", "Нужен ответ"), ("stuck", "Застряло"))
SUM = (("accepted", ("принято сегодня",) * 3), ("returns", ("возврат", "возврата", "возвратов")), ("working", ("в работе",) * 3),
       ("queued", ("в очереди",) * 3))
LEGEND = ('<div class="legend"><span><i style="background:var(--c0)"></i>тихо</span>'
          '<span><i style="background:var(--c1)"></i><i style="background:var(--c3)"></i>работа, темнее — больше</span>'
          '<span><i style="background:var(--owner)"></i>ждали ответа</span><span><i style="background:var(--stuck)"></i>застряло</span>'
          '<span>клетка — 15 минут</span></div>')


def level(cls: str, runs: int) -> str:
    """Класс клетки страницы: a/b/c — работа (темнее = больше запусков), o — ждали владельца, s — застряло, '' — тихо."""
    if cls == "owner":
        return "o"
    if cls == "stuck":
        return "s"
    return ("c" if runs >= 4 else "b" if runs >= 2 else "a") if cls == "work" or runs else ""


def hhmm(slot: int) -> str:
    minutes = slot * CELL_MIN % 1440
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def run_line(run: dict) -> str:
    """«Роль · заголовок тикета (TK-N)»; роли нет или заголовка нет — без них."""
    ticket = f"({run['ticket']})" if run["title"] else run["ticket"]
    return " · ".join(x for x in (run["who"], " ".join(x for x in (run["title"], ticket) if x)) if x)


def cell_info(view: dict, slot: int, current: int) -> dict:
    """Подсказка клетки: время, метка статуса с цветом, до TIP_RUNS строк. Нет источника — строки нет (ничего не выдумываем)."""
    cls, n = view["cells"][slot] if 0 <= slot < V.CELLS else ("none", 0)
    runs = view["runs"][slot] if 0 <= slot < V.CELLS else []
    when = f"{hhmm(slot)}–{hhmm(slot + 1)}" + (" · сейчас" if slot == current else "")
    stuck_now, fix = view["state"] == "stuck", view["fix"]
    if cls == "owner":
        lines = [V.ask_text(view)] if view["state"] == "owner" else []
        return {"t": when, "st": ["Ждали ответа", "var(--owner)"], "l": lines}
    if cls == "stuck":
        return {"t": when, "st": ["Застряло", "var(--stuck)"], "l": [fix["text"], fix["who"]] if stuck_now else []}
    if cls == "work" or n:
        lines = [run_line(r) for r in runs[:TIP_RUNS]]
        if runs and n > len(runs):
            lines.append(f"и ещё {n - len(runs)}")
        return {"t": when, "st": [f"{n} {V.plural(n, 'запуск', 'запуска', 'запусков')}" if n else "Работа", "var(--c2)"], "l": lines}
    return {"t": when, "st": ["Тихо", "var(--c0)"], "l": ["запусков не было"]}


def cell_html(view: dict, slot: int, current: int) -> str:
    cls, n = view["cells"][slot] if 0 <= slot < V.CELLS else ("none", 0)
    info = escape(json.dumps(cell_info(view, slot, current), ensure_ascii=False), quote=True)
    mark = (level(cls, n) + (" now" if slot == current else "")).strip()
    return f'<i class="{mark}" data-k="{slot}" data-info="{info}"></i>'


def hours_html(first: int, count: int) -> str:
    """Часы над своими клетками: метка часа занимает его клетки в окне (неполные края — сколько влезло, одна клетка — без метки)."""
    out, k = [], 0
    while k < count:
        slot = first + k
        span = min(PER_HOUR - slot % PER_HOUR, count - k)
        label = f"{slot // PER_HOUR % 24:02d}" if span > 1 else ""
        out.append(f'<span style="grid-column:{k + 1} / span {span}">{label}</span>')
        k += span
    return "".join(out)


def timeline(view: dict, end: int, count: int, kind: str) -> str:
    first = end - count + 1
    cells = "".join(cell_html(view, slot, end) for slot in range(first, end + 1))
    return f'<div class="tl {kind}"><div class="hours">{hours_html(first, count)}</div><div class="cells">{cells}</div></div>'


def goal_html(view: dict) -> str:
    goal = view["goal"]
    if not goal:
        return ""
    step = f'<br>шаг {goal["step"]} из {goal["steps"]}' if "step" in goal else ""
    bar = f'<div class="bar"><i style="width:{goal["step"] / goal["steps"] * 100:.0f}%"></i></div>' if "step" in goal else ""
    return f'<div class="goal">{escape(goal["title"])}{step}</div>{bar}'


def safe_link(url: str) -> str:
    return url if url.startswith(("http://", "https://")) else ""


def now_line(view: dict) -> str:
    """Справа в карточке: метка-таймер, что делается / вопрос / причина и кто чинит; ниже «TK-N · длительность». Число метки в строке не повторяется."""
    state, job, ask, fix = view["state"], view["job"], view["ask"] or {}, view["fix"]
    pill, span = pill_html(view)
    link = ""
    if state == "owner":
        main, who = ask.get("question", ""), ask.get("ticket", "")
        link = safe_link(ask.get("link", ""))
    elif state == "stuck":
        main, who = without(fix["text"], span), fix["who"]
    elif job:
        role = job["who"]
        main = " ".join(x for x in (role, job["verb"] if role else "", job["title"] or job["ticket"]) if x)
        who = " · ".join(x for x in (job["ticket"], "" if job["since"] == span else job["since"]) if x)
    else:
        main, who = view["now"] or "Сейчас тихо", ""
    button = f'<a class="act" href="{escape(link, quote=True)}">Ответить</a>' if link else ""
    return (f'<div class="now-line {KIND[state]}">{pill}{escape(main)}' + (f'<span class="who">{escape(who)}</span>' if who else "") + button + "</div>")


PLAN_ICON = {"done": "✓", "run": "◐", "ask": "?", "stuck": "!", "next": "○"}
PLAN_CLASS = {"done": "d", "run": "r", "ask": "o", "stuck": "s", "next": "w"}
LIVE = ("run", "ask", "stuck")  # отметки текущего шага: у него есть метка-таймер


def timer(row: dict) -> tuple:
    """Метка-таймер шага (текст, цвет): «принят чч:мм» · «N мин назад» (есть движение) · «стоит N мин» (жёлтая от порога, красная от второго) ·
    «ждёт N мин» (ответ владельца) · «впереди». Времени нет (файл устарел, источника нет) — пустая метка."""
    mark, idle = row["mark"], row["idle"]
    if mark == "done":
        return (f"принят {row['at']}" if row["at"] else "принят"), ""
    if mark == "next":
        return "впереди", ""
    if mark == "ask":
        return (f"ждёт {row['waiting']}" if row["waiting"] else ""), ""
    if not idle:
        return "", ""
    if mark == "stuck" or row["level"]:
        return f"стоит {idle}", row["level"]
    return ("только что" if idle == "меньше минуты" else f"{idle} назад"), ""


def plan_sub(row: dict) -> str:
    """Вторая строка шага: «Роль · TK-N»; у вопроса владельцу — «ждёт вашего ответа · TK-N»."""
    if row["mark"] == "ask":
        return " · ".join(x for x in ("ждёт вашего ответа", row["ticket"]) if x)
    return " · ".join(x for x in (row["who"], row["ticket"]) if x) if row["mark"] in ("run", "stuck") else ""


def plan_html(view: dict) -> str:
    """«План цели» первым блоком раскрытой карточки: значок, слева колонка времени, шаг и подстрока. Плана нет — ничего."""
    items = []
    for row in view["plan"]:
        label, color = timer(row)
        title = f'Шаг {row["n"]} · {row["title"]}' if row["n"] else row["title"]
        sub = plan_sub(row)
        items.append(f'<li class="{PLAN_CLASS[row["mark"]]}"><span class="ic">{PLAN_ICON[row["mark"]]}</span>'
                     f'<span class="age {color}">{escape(label)}</span>'
                     f'<span><span class="t">{escape(title)}</span>' + (f'<span class="sub">{escape(sub)}</span>' if sub else "") + "</span></li>")
    return f'<div class="planbox"><h4>План цели</h4><ul class="plan">{"".join(items)}</ul></div>' if items else ""


def pill_html(view: dict) -> tuple:
    """Метка-таймер текущего шага плана перед строкой «что сейчас» и её длительность (чтобы строка её не повторяла)."""
    row = next((r for r in view["plan"] if r["mark"] in LIVE), None)
    label, color = timer(row) if row else ("", "")
    if not label:
        return "", ""
    return f'<span class="pill {color}" title="последнее движение">{escape(label)}</span>', row["waiting"] if row["mark"] == "ask" else row["idle"]


def without(text: str, span: str) -> str:
    """Строка без длительности `span` в конце («диспетчер молчит 23 мин» → «диспетчер молчит»): число уже в метке."""
    return re.sub(rf"\s*(?:уже\s+)?{re.escape(span)}$", "", text) if span else text


def items_html(items: list, empty: str, with_time: bool) -> str:
    if not items:
        return f'<li><span class="mute">{empty}</span></li>'
    return "".join(f'<li>{"<em>" + escape(i["time"]) + "</em>" if with_time else ""}'
                   f'<span>{escape(i["title"])} <span class="tag">{escape(i["ticket"])}</span></span></li>' for i in items)


def card(view: dict, end: int) -> str:
    return (f'<details class="proj" data-n="{escape(view["name"], quote=True)}" data-s="{view["state"]}"><summary>'
            f'<div><div class="name">{escape(view["name"])}</div>{goal_html(view)}</div>'
            f'{timeline(view, end, WINDOW, "w")}{timeline(view, end, NARROW, "n")}{now_line(view)}</summary>'
            f'<div class="body">{plan_html(view)}<div><h4>Сделано сегодня</h4><ul>{items_html(view["done_today"], "пока ничего", True)}</ul></div>'
            f'<div><h4>Дальше по очереди</h4><ul>{items_html(view["queue"], "пусто", False)}</ul></div></div></details>')


def summary_html(views: list) -> str:
    counts, totals = V.counts(views), V.totals(views)
    seg = "".join(f'<button data-s="{key}"{" class=on" if key == "all" else ""}>{label} <sup>{counts[key]}</sup></button>' for key, label in SEG)
    day = "".join(f"<div><b>{totals[key]}</b>{V.plural(totals[key], *words)}</div>" for key, words in SUM)
    return f'<div class="seg" id="seg">{seg}</div><div class="sum">{day}</div>'


def body_html(views: list, now: datetime) -> str:
    """Содержимое страницы: шапка, переключатель, итоги дня, карточки проектов, легенда."""
    head = V.header(views)
    end = max((v["idx"] for v in views), default=0)
    zone = next((v["zone"] for v in views if v.get("zone")), None)  # часы — по поясу проектов, не сервера
    shown = now.astimezone(zone) if zone else now
    sub = f"<small>{escape(head['sub'])}</small>" if head["sub"] else ""
    return (f'<div class="top"><div class="state {KIND[head["kind"]]}">{escape(head["title"])}{sub}</div>'
            f'<div class="meta">обновлено {shown:%H:%M}</div></div>'
            + (summary_html(views) if views else "") + "".join(card(v, end) for v in views)
            + '<p class="empty" id="empty" hidden>Таких проектов нет</p>' + LEGEND)


def page_html(views: list, now: datetime) -> str:
    return (HERE / "pulse.html").read_text(encoding="utf-8").replace("__BODY__", body_html(views, now))


def inbox_projects(inbox) -> dict:
    """Присланные проекты {имя: путь к файлу} по алфавиту; каталога нет — {}."""
    return {f.stem: str(f) for f in sorted(Path(inbox).glob("*.json")) if NAME.fullmatch(f.stem)} if inbox else {}


def accept(inbox: Path, name: str, body: bytes) -> tuple:
    """Принять присланный `pulse.json`: (код, текст). Только читаемый вывод шага 1 (`pulse_view.stamp_of`); пишется целиком и атомарно."""
    try:
        data = json.loads(body.decode("utf-8-sig"))
    except ValueError:
        return 400, "не JSON"
    if V.stamp_of(data) is None:
        return 400, "не pulse.json: нужны generated с поясом, state, cells"
    inbox.mkdir(parents=True, exist_ok=True)
    V.P.write_json(inbox / f"{name}.json", data)
    return 200, "принято"


def make_handler(project, token: str, fetch=None, inbox=None, write_token: str = ""):
    kwargs = {"fetch": fetch} if fetch else {}

    def load(now: datetime) -> list:
        """Проекты страницы; присланные могли прибавиться, поэтому список файлов inbox — на каждый запрос."""
        return V.load_all(project, now, **kwargs, **({"extra": inbox_projects(inbox), "silence": V.NO_DATA} if inbox else {}))

    class Handler(BaseHTTPRequestHandler):
        def do_PUT(self):  # noqa: N802
            parts = self.path.split("?")[0].strip("/").split("/")
            if not inbox or not write_token or parts[0] != "put":
                return self._send(404, "text/plain; charset=utf-8", "не найдено")
            if not hmac.compare_digest(self.headers.get("Authorization", "").encode(), f"Bearer {write_token}".encode()):
                return self._send(401, "text/plain; charset=utf-8", "нужен ключ записи")
            if len(parts) != 2 or not NAME.fullmatch(parts[1]):
                return self._send(404, "text/plain; charset=utf-8", "не найдено")
            length = self.headers.get("Content-Length", "")
            if not length.isdigit():
                return self._send(411, "text/plain; charset=utf-8", "нужен Content-Length")
            if int(length) > MAX_BODY:
                return self._send(413, "text/plain; charset=utf-8", "слишком большой")
            code, text = accept(Path(inbox), parts[1], self.rfile.read(int(length)))
            self._send(code, "text/plain; charset=utf-8", text)

        do_POST = do_PUT  # noqa: N815

        def do_GET(self):  # noqa: N802
            parts = self.path.split("?")[0].strip("/").split("/")
            if not parts or not hmac.compare_digest(parts[0].encode(), token.encode()) or len(parts) > 2:
                return self._send(404, "text/plain; charset=utf-8", "не найдено")
            now = datetime.now().astimezone()
            views = load(now)
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
    ap.add_argument("--inbox", help="каталог для присланных pulse.json (приём PUT /put/<имя>)")
    ap.add_argument("--write-token", default=os.environ.get("PULSE_WRITE_TOKEN", ""))
    a = ap.parse_args(argv)
    if not a.token:
        print("[pulse] нужен ключ в пути: --token или PULSE_TOKEN", file=sys.stderr)
        return 2
    if a.inbox and (not a.write_token or a.write_token == a.token):
        print("[pulse] с --inbox нужен ключ записи (--write-token или PULSE_WRITE_TOKEN), не равный ключу чтения", file=sys.stderr)
        return 2
    project = Path(a.project).resolve()
    if not V.projects_of(project) and not a.inbox:
        print(f"[pulse] в {project}/.claude/rpv.json нет pulse_projects — страница будет пустой", file=sys.stderr)
    handler = make_handler(project, a.token, inbox=Path(a.inbox) if a.inbox else None, write_token=a.write_token)
    ThreadingHTTPServer((a.host, a.port), handler).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
