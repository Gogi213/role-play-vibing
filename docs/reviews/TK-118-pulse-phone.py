"""TK-118: Пульс на телефоне (390 px) — карточка не шире экрана. Данные — те же настоящие, что в TK-117-pulse-shots.py (его начало, до записи
страниц, выполняется как есть). Для каждого варианта страница грузится в рамку 390 px и сама присылает родителю ширины: рабочая область окна, документ, карточка и
элементы, чей правый край за рабочей областью (документ шире неё — есть горизонтальная прокрутка); родитель выводит их через --dump-dom. Снимок — Edge headless (уже 500 px окно не бывает, поэтому рамка).
Запуск: python TK-118-pulse-phone.py <префикс файлов снимков: before | after>"""
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PREFIX = sys.argv[1]
exec(compile((HERE / "TK-117-pulse-shots.py").read_text(encoding="utf-8").split("TMP.mkdir")[0], "TK-117-pulse-shots", "exec"))
OUT = HERE
TMP = Path(tempfile.gettempdir()) / "tk118_shots"
TMP.mkdir(exist_ok=True)
PROBE = """<script>addEventListener('load',function(){var w=document.documentElement.clientWidth,bad=[].slice.call(document.querySelectorAll('body *')).filter(function(e){
return e.getBoundingClientRect().right>w+0.5&&!e.closest('.tip')}).map(function(e){return e.tagName.toLowerCase()+'.'+e.className+' '+Math.round(e.getBoundingClientRect().right)+' '+(e.textContent||'').trim().slice(0,40)});
var c=document.querySelector('.proj');parent.postMessage(JSON.stringify({client:w,doc:document.documentElement.scrollWidth,card:Math.round(c.getBoundingClientRect().right),over:bad}),'*')})</script>"""
FRAME = """<body style="margin:0;background:#888"><pre id="r" style="display:none"></pre><iframe src="{src}" width="390" height="1300" style="border:0"></iframe>
<script>addEventListener('message',function(e){document.getElementById('r').textContent='PROBE '+e.data})</script>"""


DARK_RE = re.compile(r"@media \(prefers-color-scheme:dark\)\{(.*?\}\})", re.S)


def run(args):
    return subprocess.run([EDGE, "--headless=new", "--disable-gpu", *args], check=True, capture_output=True, timeout=90).stdout.decode("utf-8", "replace")


for name in ("ok", "owner", "stuck"):
    page = S.page_html([V.normalise("rpv", variants[name], now)], now).replace('<details class="proj"', '<details class="proj" open', 1)
    dark = DARK_RE.search(page)
    assert dark, "блок тёмной темы не найден"
    html = page.replace(dark.group(0), "@media all{" + dark.group(1))  # тёмная — всегда, как в TK-117
    (TMP / f"{name}.html").write_bytes(html.replace("</body>", PROBE + "</body>").encode("utf-8") if "</body>" in html else (html + PROBE).encode("utf-8"))
    (TMP / f"f-{name}.html").write_bytes(FRAME.replace("{src}", (TMP / f"{name}.html").as_uri()).encode("utf-8"))
    dom = run(["--window-size=500,1300", "--virtual-time-budget=4000", "--dump-dom", (TMP / f"f-{name}.html").as_uri()])
    got = re.search(r"PROBE (\{.*?\})\s*</pre>", dom, re.S)
    res = json.loads(got.group(1).replace("&quot;", '"').replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")) if got else None
    print(name, "→", json.dumps(dict(res, over=len(res["over"]), first=res["over"][:2]), ensure_ascii=False) if res else "нет ответа страницы")
    if name != "stuck":
        run(["--window-size=500,1300", f"--screenshot={OUT / f'TK-118-phone-{PREFIX}-{name}.png'}", "--hide-scrollbars", (TMP / f"f-{name}.html").as_uri()])
