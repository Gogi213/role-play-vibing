"""TK-105: скриншоты Пульса v2 — рендер настоящих данных проекта (pulse.build по настоящим тикетам, state.json и runs.log корня)."""
import copy
import re
import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

WT = Path(r"C:\visual projects\role-play-vibing\.claude\worktrees\TK-105")
ROOT = Path(r"C:\visual projects\role-play-vibing")
OUT = WT / "docs" / "reviews"
TMP = Path(r"C:\Users\3D6B~1\AppData\Local\Temp") / "tk105_shots"
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
sys.path[:0] = [str(WT / ".claude" / "dispatcher"), str(WT / ".claude" / "board")]
import project_paths as CFG  # noqa: E402

CFG.configure_project(ROOT)
import pulse  # noqa: E402
import pulse_server as S  # noqa: E402
import pulse_view as V  # noqa: E402

now = datetime.now().astimezone()
state = json.loads((ROOT / ".claude" / "dispatcher" / "state.json").read_text(encoding="utf-8-sig"))
real = pulse.build(copy.deepcopy(state), now)
print("state:", real["state"], "goal:", real.get("goal"), "job:", real.get("job"), "day:", real.get("day"))

variants = {"ok": real}
first = (real.get("queue") or [{"ticket": "TK-105", "title": "Пульс v2"}])[0]
owner = copy.deepcopy(real)
owner.update(state="owner", ask={"ticket": first["ticket"], "question": first["title"]})
for c in owner["cells"][max(0, V._slot(now) - 5):V._slot(now)]:
    c["c"] = "owner"
variants["owner"] = owner
stuck = copy.deepcopy(real)
stuck.update(state="stuck", fix={"text": "диспетчер молчит 23 мин", "who": "перезапускает CEO"})
for c in stuck["cells"][max(0, V._slot(now) - 3):V._slot(now) + 1]:
    c["c"] = "stuck"
variants["stuck"] = stuck

TMP.mkdir(exist_ok=True)
DARK_RE = re.compile(r"@media \(prefers-color-scheme:dark\)\{(.*?\}\})", re.S)
for name, data in variants.items():
    page = S.page_html([V.normalise("rpv", data, now)], now)  # только настоящий проект этой копии, чужих карточек нет
    if name == "ok":
        page = page.replace('<details class="proj"', '<details class="proj" open', 1)
    m = DARK_RE.search(page)
    assert m, "блок тёмной темы не найден"
    (TMP / f"{name}.html").write_text(page.replace(m.group(0), "@media all{" + m.group(1)), encoding="utf-8")  # тёмная — всегда
    if name == "ok":
        (TMP / "ok-light.html").write_text(page.replace(m.group(0), ""), encoding="utf-8")  # светлая — переменные :root страницы


def shot(name, width, height, out):
    args = [EDGE, "--headless=new", "--disable-gpu", f"--window-size={width},{height}", f"--screenshot={out}", "--hide-scrollbars"]
    args.append((TMP / f"{name}.html").as_uri())
    subprocess.run(args, check=True, capture_output=True, timeout=90)


shot("ok", 1100, 900, OUT / "TK-105-pulse-wide-ok.png")
shot("owner", 1100, 900, OUT / "TK-105-pulse-wide-owner.png")
shot("stuck", 1100, 900, OUT / "TK-105-pulse-wide-stuck.png")
for name in ("ok", "owner"):  # Edge headless не уже 500 px: телефон — рамка 390 px внутри окна
    (TMP / f"phone-{name}.html").write_text(f'<body style="margin:0;background:#888"><iframe src="{(TMP / (name + ".html")).as_uri()}" width="390" height="1100" style="border:0"></iframe>', encoding="utf-8")
    shot(f"phone-{name}", 500, 1100, OUT / f"TK-105-pulse-phone-{name}.png")
shot("ok-light", 1100, 900, OUT / "TK-105-pulse-wide-light.png")
print("готово")
