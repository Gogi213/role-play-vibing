"""TK-117: скриншоты Пульса (блок «Статус» раскрытой карточки) — настоящие данные проекта: pulse.build по настоящим тикетам, state.json и
runs.log корня, блок «Статус» — pulse_facts.status_of. У настоящего проекта нет открытой цели, поэтому «целью» взяты настоящие тикеты
Пульса (TK-105…TK-117); варианты «ждёт ответа» / «стоит» / «застряло» — подмена отметки в копии данных."""
import copy
import json
import re
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

WT = Path(__file__).resolve().parents[2]
ROOT = Path(r"C:\visual projects\role-play-vibing")
OUT = WT / "docs" / "reviews"
TMP = Path(tempfile.gettempdir()) / "tk117_shots"
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PULSE_TICKETS = {"TK-105", "TK-106", "TK-108", "TK-109", "TK-110", "TK-112", "TK-116", "TK-117"}
sys.path[:0] = [str(WT / ".claude" / "dispatcher"), str(WT / ".claude" / "board")]
import project_paths as CFG  # noqa: E402

CFG.configure_project(ROOT)
import goal_idle  # noqa: E402
import pulse  # noqa: E402
import pulse_facts as F  # noqa: E402
import pulse_server as S  # noqa: E402
import pulse_view as V  # noqa: E402

now = datetime.now().astimezone()
state = json.loads((ROOT / ".claude" / "dispatcher" / "state.json").read_text(encoding="utf-8-sig"))
tickets, runs = goal_idle._read_all(), pulse._runs(now)
real = pulse.build(copy.deepcopy(state), now, tickets, runs)
F._of_goal = lambda tkt, gid: tkt.id in PULSE_TICKETS  # «цель» G-000 — настоящие тикеты Пульса
real["status"] = F.status_of("G-000", tickets, state, [], [], runs, now, real.get("plan", []), real.get("ask"), real.get("fix"))
real["goal"] = {"id": "G-000", "title": "Пульс: блок «Статус»"}
print("state:", real["state"], "job:", real.get("job"))
print(json.dumps(real["status"], ensure_ascii=False, indent=1))
live = next(r for r in real["status"]["rows"] if r["mark"] != "done")
slot = V._slot(now)

variants = {"ok": real}
warn = copy.deepcopy(real)
next(r for r in warn["status"]["rows"] if r["ticket"] == live["ticket"]).update(mark="run", where="стоит 23 мин", level="warn")
warn["status"]["hold"] = f"{live['ticket']} стоит 23 мин"
variants["warn"] = warn
owner = copy.deepcopy(real)
owner.update(state="owner", ask={"ticket": live["ticket"], "question": "Брать фьючерсы или только спот?"})
next(r for r in owner["status"]["rows"] if r["ticket"] == live["ticket"]).update(mark="ask", where="ждёт вашего ответа", level="")
owner["status"]["hold"] = f"ваш ответ по {live['ticket']}"
for c in owner["cells"][max(0, slot - 5):slot]:
    c["c"] = "owner"
variants["owner"] = owner
stuck = copy.deepcopy(real)
stuck.update(state="stuck", fix={"text": "диспетчер молчит 47 мин", "who": "перезапускает CEO"})
next(r for r in stuck["status"]["rows"] if r["ticket"] == live["ticket"]).update(mark="stuck", where="стоит 47 мин", level="bad")
stuck["status"]["hold"] = "диспетчер молчит 47 мин"
for c in stuck["cells"][max(0, slot - 3):slot + 1]:
    c["c"] = "stuck"
variants["stuck"] = stuck

TMP.mkdir(exist_ok=True)
DARK_RE = re.compile(r"@media \(prefers-color-scheme:dark\)\{(.*?\}\})", re.S)
for name, data in variants.items():
    page = S.page_html([V.normalise("rpv", data, now)], now).replace('<details class="proj"', '<details class="proj" open', 1)  # только этот проект
    m = DARK_RE.search(page)
    assert m, "блок тёмной темы не найден"
    (TMP / f"{name}.html").write_bytes(page.replace(m.group(0), "@media all{" + m.group(1)).encode("utf-8"))  # тёмная — всегда
    if name == "ok":
        (TMP / "ok-light.html").write_bytes(page.replace(m.group(0), "").encode("utf-8"))  # светлая — переменные :root страницы


def shot(name, width, height, out):
    args = [EDGE, "--headless=new", "--disable-gpu", f"--window-size={width},{height}", f"--screenshot={out}", "--hide-scrollbars", (TMP / f"{name}.html").as_uri()]
    subprocess.run(args, check=True, capture_output=True, timeout=90)


for name in ("ok", "warn", "owner", "stuck"):
    shot(name, 1100, 900, OUT / f"TK-117-pulse-wide-{name}.png")
for name in ("ok", "owner"):  # Edge headless не уже 500 px: телефон — рамка 390 px внутри окна
    (TMP / f"phone-{name}.html").write_bytes(f'<body style="margin:0;background:#888"><iframe src="{(TMP / (name + ".html")).as_uri()}" width="390" height="1300" style="border:0"></iframe>'.encode("utf-8"))
    shot(f"phone-{name}", 500, 1300, OUT / f"TK-117-pulse-phone-{name}.png")
shot("ok-light", 1100, 900, OUT / "TK-117-pulse-wide-light.png")
print("готово")
