"""TK-108: скриншоты Пульса (план цели) — настоящие данные проекта: pulse.build по настоящим тикетам, state.json и runs.log корня.
У настоящего проекта нет открытой цели (все G-* приняты), поэтому план собран из настоящих тикетов TK-105/TK-106/TK-108 теми же функциями,
которыми его считает диспетчер (pulse_facts._plan_row); варианты «ждёт ответа» / «стоит» — подмена отметки в копии данных."""
import copy
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

WT = Path(__file__).resolve().parents[2]
ROOT = Path(r"C:\visual projects\role-play-vibing")
OUT = WT / "docs" / "reviews"
TMP = Path(r"C:\Users\3D6B~1\AppData\Local\Temp") / "tk108_shots"
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
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
by_id = {t.id: t for t in tickets}
steps = [(1, "TK-105"), (2, "TK-106"), (3, "TK-108"), (4, "")]
titles = {"": "Выпуск версии и обновление проектов", **{tid: F._title(by_id[tid]) for _, tid in steps if tid}}
real["plan"] = [F._plan_row(n, titles[tid], [by_id[tid]] if tid else [], state, set(), "", runs, now) for n, tid in steps]
real["goal"] = {"id": "G-000", "title": "Пульс: план цели по шагам", "step": F.plan_step(real["plan"])[0], "steps": len(steps)}
print("state:", real["state"], "job:", real.get("job"))
for r in real["plan"]:
    print(r)
cur = next(r for r in real["plan"] if r["mark"] == "run")
slot = V._slot(now)

variants = {"ok": real}
warn = copy.deepcopy(real)
next(r for r in warn["plan"] if r["mark"] == "run").update(idle="23 мин", level="warn")
variants["warn"] = warn
owner = copy.deepcopy(real)
owner.update(state="owner", ask={"ticket": cur["ticket"], "question": "Брать фьючерсы или только спот?"})
next(r for r in owner["plan"] if r["mark"] == "run").update(mark="ask", waiting="45 мин", idle="", level="")
for c in owner["cells"][max(0, slot - 5):slot]:
    c["c"] = "owner"
variants["owner"] = owner
stuck = copy.deepcopy(real)
stuck.update(state="stuck", fix={"text": "диспетчер молчит 47 мин", "who": "перезапускает CEO"})
next(r for r in stuck["plan"] if r["mark"] == "run").update(mark="stuck", idle="47 мин", level="bad")
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
    shot(name, 1100, 900, OUT / f"TK-108-pulse-wide-{name}.png")
for name in ("ok", "owner"):  # Edge headless не уже 500 px: телефон — рамка 390 px внутри окна
    (TMP / f"phone-{name}.html").write_bytes(f'<body style="margin:0;background:#888"><iframe src="{(TMP / (name + ".html")).as_uri()}" width="390" height="1300" style="border:0"></iframe>'.encode("utf-8"))
    shot(f"phone-{name}", 500, 1300, OUT / f"TK-108-pulse-phone-{name}.png")
shot("ok-light", 1100, 900, OUT / "TK-108-pulse-wide-light.png")
print("готово")
