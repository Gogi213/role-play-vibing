"""TK-148: карточки проектов по живым pulse.json Софии — pulse_server.py с Софии (scp vps:/opt/rpv/.claude/board/) и из main (копия TK-148).
У Софии alpha без цели по имени в коде; в main — по ключу pulse_hide_goal проекта сервера. Сравниваются карточки <details> целиком.

    python TK-148-sofia-compare.py <каталог: inbox/ с pulse.json Софии и sofia/.claude/{board,dispatcher}> <корень копии main>
"""
import importlib.util
import json
import re
import sys
import tempfile
from pathlib import Path

live = Path(sys.argv[1])
main_root = Path(sys.argv[2])
inbox = live / "inbox"
sys.path.insert(0, str(main_root / ".claude" / "dispatcher"))
import pulse_view as V  # noqa: E402


def load(path, tag):
    spec = importlib.util.spec_from_file_location(tag, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sofia = load(live / "sofia" / ".claude" / "board" / "pulse_server.py", "sofia_server")
main = load(main_root / ".claude" / "board" / "pulse_server.py", "main_server")
extra = {f.stem: str(f) for f in sorted(inbox.glob("*.json"))}
stamps = [V.stamp_of(json.loads(Path(p).read_text(encoding="utf-8-sig"))) for p in extra.values()]
now = max(s for s in stamps if s) .astimezone()

with tempfile.TemporaryDirectory() as tmp:
    plain = Path(tmp) / "plain"          # проект сервера Софии: rpv.json нет
    keyed = Path(tmp) / "keyed"          # проект сервера с pulse_hide_goal
    for d in (plain, keyed):
        (d / ".claude").mkdir(parents=True)
    (keyed / ".claude" / "rpv.json").write_text(json.dumps({"pulse_hide_goal": ["alpha"]}), encoding="utf-8")
    views_sofia = V.load_all(plain, now, extra=extra, silence=V.NO_DATA)
    views_main = V.load_all(keyed, now, extra=extra, silence=V.NO_DATA)

cards = lambda mod, views: dict(zip((v["name"] for v in views), re.findall(r"<details class=\"proj\".*?</details>", mod.body_html(views, now), re.S)))
s_cards, m_cards = cards(sofia, views_sofia), cards(main, views_main)
ok = True
for name in sorted(s_cards):
    same = s_cards[name] == m_cards[name]
    ok &= same
    card_html = m_cards[name]
    body = re.search(r'<div class="body">.*</div></details>', card_html, re.S).group(0)
    yes = lambda flag: "есть" if flag else "нет"
    print(name + ": карточка " + ("совпадает" if same else "РАЗЛИЧАЕТСЯ") + f"; {len(card_html)} знаков; цель " + yes('class="goal"' in card_html)
          + "; «принято ещё» " + yes("принято ещё" in card_html) + "; «Осталось» " + yes("Осталось" in card_html)
          + "; таблиц st в раскрытой части " + str(body.count('<table class="st">')) + "; <ul> " + yes("<ul>" in body))
    if not same:
        a, b = s_cards[name], m_cards[name]
        i = next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))
        print("  Софiя:", a[max(0, i - 80): i + 160])
        print("  main :", b[max(0, i - 80): i + 160])
print("ИТОГ:", "все карточки совпадают" if ok else "есть различия")
