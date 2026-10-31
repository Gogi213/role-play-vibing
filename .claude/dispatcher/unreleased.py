"""Сторож «влито, не выпущено» (TK-062, В-24): тикетов в работе нет дольше UNRELEASED_IDLE_MIN, а на origin/main (у GitHub,
не в локальном main) есть коммиты после последнего тега `v*` с файлами вне docs/ и tools/ — очередь пуста, выпуск никто не заведёт. Вид `watch-unreleased`
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
BRANCH_KEY = "unreleased_branch"  # watch-state.json: ветка репозитория по последнему ответу GitHub — ключ дедупа находки (TK-075)
_VERSION_TAG = re.compile(r"v(\d+(?:\.\d+)*)")
NOT_RELEASE_DIRS = ("docs/", "tools/")  # правки только здесь выпуска не требуют
FILES_CAP = 300  # потолок `files` в ответе GitHub compare (на 1-й странице; per_page листает коммиты): все 300 из docs/tools — «не доказано, что только docs»


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


def has_release_files(compare: dict) -> bool:
    """В ответе GitHub `compare` есть файл вне docs/ и tools/; упёршийся в потолок список без таких — тоже да (хвост не виден)."""
    files = [str(f.get("filename", "")) for f in compare.get("files") or [] if isinstance(f, dict)]
    return len(files) >= FILES_CAP or any(not name.startswith(NOT_RELEASE_DIRS) for name in files)


def unreleased_message(ws: dict, now, gh=None) -> "str | None":
    """Текст находки; пустая строка — находки нет: ключ выключен (только `true`), нет `ci_repo`, очередь не пуста / пуста меньше
    порога, тегов нет, после последнего тега ничего не влито или влиты только docs/ и tools/. None — GitHub не ответил: «не знаю»,
    сторож не снимает метку суточного повтора. `gh(путь)` — вызов GitHub; ветку берём у репозитория (`default_branch`)."""
    gh = gh or CORE.gh_api
    repo = P.setting("watch_unreleased", False) is True and P.setting("ci_repo", "")
    if not repo:
        ws.pop(IDLE_SINCE_KEY, None)
        return ""
    idle = idle_minutes(ws, now)
    if idle < UNRELEASED_IDLE_MIN:
        return ""
    try:
        tag = last_release_tag(gh(f"repos/{repo}/tags?per_page=100"))
        if not tag:
            return ""
        branch = ws[BRANCH_KEY] = str(gh(f"repos/{repo}")["default_branch"])
        compare = gh(f"repos/{repo}/compare/{tag}...{branch}?per_page=1")
        ahead = int(compare.get("ahead_by", 0))
    except Exception:
        return None
    if ahead <= 0 or not has_release_files(compare):
        return ""
    return (f"влито, не выпущено: на {repo} в {branch} {ahead} коммит(ов) после {tag} (есть файлы вне docs/ и tools/), "
            f"тикетов в работе нет {idle:.0f} мин — выпуск не заведён")
