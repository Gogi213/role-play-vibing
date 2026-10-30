"""Сторож «влито, не выпущено» (TK-062, В-24): тикетов в работе нет дольше UNRELEASED_IDLE_MIN, а на origin/main (у GitHub,
не в локальном main) есть коммиты после последнего тега `v*` — очередь пуста, выпуск никто не заведёт. Вид `watch-unreleased`
будит CEO; за ключом `watch_unreleased` в `.claude/rpv.json` (умолчание — выключен), репозиторий — `ci_repo`.
Находку собирает `watch.check_unreleased`; здесь только проверка без записи сигналов."""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ci_core as CORE  # noqa: E402
import project as P  # noqa: E402
import project_paths as CFG  # noqa: E402
import ticket as T  # noqa: E402

UNRELEASED_IDLE_MIN = 10  # очередь пуста дольше этого — и только тогда проверяем теги (сторож ходит раз в ~2 мин)
IDLE_SINCE_KEY = "unreleased_idle_since"  # watch-state.json: когда сторож впервые увидел пустую очередь
_VERSION_TAG = re.compile(r"v(\d+(?:\.\d+)*)")


def queue_empty() -> bool:
    """Нет тикетов в T.OPEN_STATUSES. Тикет не читается — неизвестно, что в нём: очередь не считается пустой."""
    for path in T.list_tickets(CFG.TICKETS_DIR):
        try:
            tkt = T.read_ticket(path)
        except Exception:
            return False
        if tkt.status in T.OPEN_STATUSES:
            return False
    return True


def last_release_tag(tags) -> str:
    """Старший по номеру тег вида `v2.0.14` из ответа GitHub `repos/<репо>/tags`; нет такого — пустая строка."""
    found = []
    for item in tags or []:
        name = str(item.get("name", "")) if isinstance(item, dict) else ""
        if _VERSION_TAG.fullmatch(name):
            found.append((tuple(int(n) for n in name[1:].split(".")), name))
    return max(found)[1] if found else ""


def idle_minutes(ws: dict, now) -> float:
    """Сколько минут сторож подряд видит пустую очередь (отметка в ws); очередь не пуста — отметка сбрасывается, -1."""
    if not queue_empty():
        ws.pop(IDLE_SINCE_KEY, None)
        return -1.0
    since = T.parse_dt(ws[IDLE_SINCE_KEY]) if ws.get(IDLE_SINCE_KEY) else None
    if since is None:
        ws[IDLE_SINCE_KEY] = T.now_iso(now)
        return 0.0
    return max((now - since).total_seconds() / 60, 0.0)


def unreleased_message(ws: dict, now, gh=None) -> str:
    """Текст находки или пустая строка: ключ выключен, нет `ci_repo`, очередь не пуста / пуста меньше порога, тегов нет,
    GitHub не ответил (сторож не падает и не шумит), после последнего тега ничего не влито. `gh(путь)` — вызов GitHub."""
    gh = gh or CORE.gh_api
    repo = P.setting("watch_unreleased", False) and P.setting("ci_repo", "")
    if not repo:
        ws.pop(IDLE_SINCE_KEY, None)
        return ""
    idle = idle_minutes(ws, now)
    if idle < UNRELEASED_IDLE_MIN:
        return ""
    try:
        tag = last_release_tag(gh(f"repos/{repo}/tags?per_page=100"))
        ahead = int(gh(f"repos/{repo}/compare/{tag}...main?per_page=1").get("ahead_by", 0)) if tag else 0
    except Exception:
        return ""
    if ahead <= 0:
        return ""
    return (f"влито, не выпущено: на {repo} в main {ahead} коммит(ов) после {tag}, тикетов в работе нет {idle:.0f} мин — "
            "выпуск не заведён")
