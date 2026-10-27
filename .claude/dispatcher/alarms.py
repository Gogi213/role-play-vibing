"""Тревога «работа стоит», ежедневный отчёт, Надзиратель и вопросы владельцу — побочная работа тика, вынесено из
dispatch.py (TK-011, ось 4). Процессы `report.py`/`overseer.py` запускает переданный вызывающим `popen` (тесты подменяют
`dispatch._popen`), строку CEO пишет переданный `ceo_inbox`; пути проекта — `project_paths`."""
from __future__ import annotations

import re
import sys
from pathlib import Path

import project_paths as CFG
import hide
import notify
import project as P
import report as REPORT
import ticket as T

CODE_DIR = Path(__file__).resolve().parent  # где лежат report.py и overseer.py


STALLED = []  # заголовки тикетов, стоящих в этом тике (инвариант хода, цикл ожиданий, ожидание без условия, не взят)


def _stall_title(tkt: T.Ticket) -> str:
    return " ".join((tkt.header.get("title") or tkt.id).split())[:70]


def _guarded(label: str, fn, *args) -> None:
    """Побочная работа тика (тревога, отчёт, Надзиратель, вопрос владельцу) не должна ронять тик."""
    try:
        fn(*args)
    except Exception as e:
        print(f"[dispatch] {label}: {type(e).__name__}: {e}", file=sys.stderr, flush=True)


def stall_alarm(state: dict, now, stalled: list, min_stalled: float, ceo_inbox) -> None:
    """CP5 (критерии 8, 9, 10): одна тревога владельцу — в Telegram (`notify.py`) и строкой CEO — когда готовая работа
    стоит STALL_ALARM_MIN минут подряд; одна на эпизод (`notify.episode`). Пошла снова — короткое «снова идёт»."""
    st = state.get("stall") or {}
    if stalled:
        st.setdefault("since", T.now_iso(now))

    def mins() -> float:
        return (now - T.parse_dt(st["since"])).total_seconds() / 60

    def down() -> str:
        names = sorted(set(stalled))
        return (f"Работа стоит {mins():.0f} мин: {len(names)} задач(и) ждут, их никто не делает — "
                + "; ".join(f"«{n}»" for n in names[:3]) + (f" и ещё {len(names) - 3}" if len(names) > 3 else "")
                + ". CEO разбирается; если сессии CEO нет — откройте её и скажите «почему стоит работа».")

    notify.episode(st, "sent", firing=bool(stalled) and mins() >= min_stalled, cleared=not stalled, down=down,
                   up=lambda: f"Работа снова идёт (стояла {mins():.0f} мин).", log_dir=CFG.DISPATCHER_DIR,
                   ceo_down=lambda text: ceo_inbox("*", "работа-стоит", text, now),
                   ceo_up=lambda text: ceo_inbox("*", "работа-идёт", text, now))
    state["stall"] = st if stalled else {}


REPORT_AT = "10:00"  # CP10: местное время ежедневного отчёта владельцу, ЧЧ:ММ; проект — `report_at` в rpv.json


_REPORT_PROC = None  # процесс отчёта (report.py): короткоживущий, тик его не ждёт


def daily_report(state: dict, now, popen) -> None:
    """CP10 (критерии 2, 9): раз в сутки в REPORT_AT — отчёт владельцу отдельным коротким процессом (`report.py`): медленный
    `claude -p` тик не держит. Метка `last_report_at` в state.json ставится до запуска: упавший отчёт не повторяется каждый
    тик. Диспетчер стоял в REPORT_AT — отчёт уходит на первом тике после и один раз. Первый тик проекта только ставит метку:
    отчёта за время, когда диспетчера не было, не бывает."""
    global _REPORT_PROC
    if _REPORT_PROC is not None and _REPORT_PROC.poll() is None:
        return
    last = state.get("last_report_at")
    if not last:
        state["last_report_at"] = T.now_iso(now)
        return
    if not REPORT.due(last, now, str(P.setting("report_at", REPORT_AT))):
        return
    state["last_report_at"] = T.now_iso(now)
    cmd = [sys.executable, str(CODE_DIR / "report.py"), "--project", str(CFG.PROJECT_ROOT), "--since", last,
           "--until", T.now_iso(now)]
    CFG.DISPATCHER_DIR.mkdir(parents=True, exist_ok=True)
    with open(CFG.DISPATCHER_DIR / "report.log", "a", encoding="utf-8") as log:
        _REPORT_PROC = popen(cmd, cwd=str(CFG.PROJECT_ROOT), stdout=log, stderr=log, **hide.hidden())


OWNER_ASK = re.compile(r"^\[итог:\s*ask-owner\]")


def owner_questions(state: dict, now) -> None:
    """Критерии 5 и 9: вопрос Судьи владельцу (тикет `needs_owner`) — сразу одним сообщением в Telegram, а не только строкой
    CEO: сессии CEO может не быть. Повод и сам вопрос; отвечают CEO. Один раз на вопрос (тикет и время записи вопроса)."""
    sent = state.setdefault("owner_asked", {})
    for path in T.list_tickets(CFG.TICKETS_DIR):
        try:
            tkt = T.read_ticket(path)
        except Exception:
            continue
        if tkt.status != "needs_owner":
            continue
        q = next((e for e in reversed(tkt.log) if T.author_is(e.author, "judge") and OWNER_ASK.match(e.text.strip())), None)
        if q is None or sent.get(tkt.id) == q.ts_raw:
            continue
        why = re.search(r"повод: ([\w-]+)", q.text)
        body = re.sub(r"^\[итог:\s*ask-owner\]\s*(?:повод: [\w-]+)?\s*[—-]?\s*", "", q.text.strip())
        title = " ".join((tkt.header.get("title") or tkt.id).split())[:80]
        notify.send(f"Вопрос вам ({why.group(1) if why else 'без повода'}): «{title}» — {' '.join(body.split())[:400]} "
                    f"Ответьте CEO.", log_dir=CFG.DISPATCHER_DIR)
        sent[tkt.id] = q.ts_raw


OVERSEER_EVERY_S = 15 * 60.0  # CP14: Надзиратель — не чаще раза в 15 мин и только если тикеты менялись


_OVERSEER_PROC = None  # процесс Надзирателя (overseer.py): короткоживущий, тик его не ждёт


def overseer(state: dict, now, popen) -> None:
    """CP14 (критерии 1, 3, 4, 10): раз в OVERSEER_EVERY_S, если с прошлого взгляда менялся хоть один тикет, — один взгляд
    Надзирателя отдельным процессом (`overseer.py`: правила «дорого» и «всплеск» и Haiku по новым записям; находки — Судье записью тикета).
    Первый тик только ставит метку `overseer_at`; прошлый взгляд ещё идёт — ждём его."""
    global _OVERSEER_PROC
    if _OVERSEER_PROC is not None and _OVERSEER_PROC.poll() is None:
        return
    last = state.get("overseer_at")
    if not last:
        state["overseer_at"] = T.now_iso(now)
        return
    if (now - T.parse_dt(last)).total_seconds() < OVERSEER_EVERY_S:
        return
    changed = max((f.stat().st_mtime for f in CFG.TICKETS_DIR.glob("TK-*.md")), default=0.0)
    if changed <= T.parse_dt(last).timestamp():
        return
    state["overseer_at"] = T.now_iso(now)
    cmd = [sys.executable, str(CODE_DIR / "overseer.py"), "--project", str(CFG.PROJECT_ROOT)]
    CFG.DISPATCHER_DIR.mkdir(parents=True, exist_ok=True)
    with open(CFG.DISPATCHER_DIR / "overseer.run.log", "a", encoding="utf-8") as log:
        _OVERSEER_PROC = popen(cmd, cwd=str(CFG.PROJECT_ROOT), stdout=log, stderr=log, **hide.hidden())
