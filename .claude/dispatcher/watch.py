"""Сторож CEO без модели (судья TK-002 п.2) — заменяет получасовой крон CEO. Покрывает то же самое:
диспетчер жив, тревоги Steam Deck (ALERT-*), простой Steam Deck при непустой
очереди, тикеты-сироты (in_progress/waiting без новой записи дольше порога), blocked/needs_owner.
Ошибка ssh/чтения — сама сигнал (не молчание, п.2в). Дедуп по (вид, ключ), повтор раз в
WATCH_DEDUP_REPEAT_HOURS, пока проблема не снята (п.2г). Пишет `ceo-wake.log`/`ceo-inbox.md` только
при находке; сердцебиение (`watch-heartbeat.json`) обновляется каждый цикл независимо от находок —
его возраст проверяет хук `role_memory.py` (кто сторожит сторожа, п.2а).

v2 (02.10, аудит ролевой системы — «сторож: тревога с меткой времени в подписи → повтор каждые 10 мин»):
тревоги Steam Deck сравниваются по нормализованной сигнатуре (без ISO-времён, кусков вроде `T23:`, времён
суток и счётчиков «всего разборов N») и сообщаются один раз, пока тревога не исчезнет или не изменится по
сути (напоминание — раз в WATCH_LONG_REPEAT_HOURS); известные тревоги не «забываются» после цикла с
ошибкой ssh; таймаут проверки HOLD не превращает ожидаемый простой в тревогу; orphan по закрытым
(done/cancelled) тикетам молчат, по открытым повторяются не чаще раза в сутки.
v3 (03.10, В-173): проверки трат (суточный/часовой расход, бюджет тикета) удалены — лимитов денег нет.

Запуск: python .claude/dispatcher/watch.py --once   (для крона/планировщика Windows)
        python .claude/dispatcher/watch.py           (цикл раз в WATCH_INTERVAL_S)
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dispatch as D  # noqa: E402 — переиспользуем пути/константы/append_ceo_inbox/_pid_alive
import ticket as T  # noqa: E402

DISPATCHER_DIR = Path(__file__).resolve().parent
WATCH_HEARTBEAT_FILE = DISPATCHER_DIR / "watch-heartbeat.json"
WATCH_STATE_FILE = DISPATCHER_DIR / "watch-state.json"
# Владелец 03.10 04:26: «стимдек больше не трогаем». Файл есть — сторож вообще не ходит на Steam Deck по ssh
# (ни ALERT-*/HOLD/очередь, ни заморозка); наличие проверяется на КАЖДОМ цикле — перезапуск не нужен.
DECK_OFF_FLAG = DISPATCHER_DIR / "deck-off"

WATCH_INTERVAL_S = float(os.environ.get("ALPHA_WATCH_INTERVAL", "120"))
WATCH_DEDUP_REPEAT_HOURS = float(os.environ.get("ALPHA_WATCH_REPEAT_HOURS", "2"))
# v2: виды находок, которые повторяются не чаще раза в сутки (или пока не изменятся по сути); blocked/needs_owner
# диспетчер уже сообщил один раз (`ceo-inbox`), сторож лишь страхует — раз в сутки
WATCH_LONG_REPEAT_HOURS = float(os.environ.get("ALPHA_WATCH_LONG_REPEAT_HOURS", "24"))
WATCH_LONG_REPEAT_KINDS = {"orphan-ticket", "deck-alert", "deck-idle-expected", "blocked", "needs_owner"}
CLOSED_TICKET_STATUSES = ("done", "cancelled")
DISPATCH_STALE_MINUTES = float(os.environ.get("ALPHA_WATCH_DISPATCH_STALE_MIN", "5"))
ORPHAN_TICKET_HOURS = float(os.environ.get("ALPHA_WATCH_ORPHAN_HOURS", "2"))
# TK-016: 30 → 10 мин (простой 28.09 07:22–08:31 — очередь стояла 69 мин незамеченной)
DECK_QUEUE_STALE_MINUTES = float(os.environ.get("ALPHA_WATCH_DECK_QUEUE_STALE_MIN", "10"))


@dataclass
class Finding:
    kind: str
    key: str
    message: str


# --- сбор находок (чистые функции — без сети, кроме ssh-хелперов ниже) ------------------------

def check_dispatcher_alive(state: dict, now, started_at=None) -> list:
    """п.2б: диспетчер жив — по времени последнего тика (dispatch.tick() пишет state["last_tick"]).
    CEO 27.09: «нет last_tick» сразу после старта — не находка, а грация в 2 интервала диспетчера
    (POLL_INTERVAL) — сторож и диспетчер могли стартовать одновременно, первый тик ещё не случился."""
    last_tick = state.get("last_tick")
    if not last_tick:
        if started_at is not None and (now - started_at).total_seconds() < 2 * D.POLL_INTERVAL:
            return []
        return [Finding("dispatcher-down", "last_tick", "в state.json нет last_tick — диспетчер ни разу не тикнул "
                                                          "с начала наблюдения или это не тот state.json")]
    age_min = (now - T.parse_dt(last_tick)).total_seconds() / 60
    if age_min > DISPATCH_STALE_MINUTES:
        return [Finding("dispatcher-down", "last_tick",
                         f"последний тик диспетчера {age_min:.0f} мин назад (> {DISPATCH_STALE_MINUTES:.0f})")]
    return []


def _ticket_status(tid: str):
    """status тикета по id или None (файла нет/не читается)."""
    path = D.TICKETS_DIR / f"{tid}.md"
    try:
        return T.read_ticket(path).status
    except Exception:
        return None


def check_blocked_and_needs_owner(now) -> list:
    """п.2б: blocked/needs_owner — сторож пересобирает список сам (не полагаясь только на то, что
    dispatch.py однажды уже написал в ceo-inbox — вдруг тот запуск и был тем, что легло)."""
    out = []
    for path in T.list_tickets(D.TICKETS_DIR):
        try:
            tkt = T.read_ticket(path)
        except Exception as e:
            out.append(Finding("ticket-unreadable", path.stem, f"{path.stem}: не читается — {type(e).__name__}: {e}"))
            continue
        if tkt.status in ("blocked", "needs_owner"):
            out.append(Finding(tkt.status, tkt.id, f"{tkt.id}: status={tkt.status}"))
    return out


def check_orphan_tickets(now) -> list:
    """п.2б: in_progress/waiting без новой записи дольше порога — сирота (TK-001 п.2, до правила
    (а') это значило «замерла навсегда»; правило (а') её теперь будит, но сторож всё равно следит на
    случай, если тикет застрял по другой причине — троттлинг/сама роль не отвечает)."""
    out = []
    for path in T.list_tickets(D.TICKETS_DIR):
        try:
            tkt = T.read_ticket(path)
        except Exception:
            continue
        if tkt.status in CLOSED_TICKET_STATUSES or tkt.status not in ("in_progress", "waiting"):
            continue
        last_ts = tkt.log[-1].ts if tkt.log else T.parse_dt(tkt.header.get("updated")) if tkt.header.get(
            "updated") else None
        if last_ts is None:
            continue
        age_h = (now - last_ts).total_seconds() / 3600
        if age_h > ORPHAN_TICKET_HOURS:
            out.append(Finding("orphan-ticket", tkt.id,
                                f"{tkt.id}: status={tkt.status} без новой записи {age_h:.1f} ч"))
    return out


def deck_off() -> bool:
    """Флаг `.claude/dispatcher/deck-off` — Steam Deck выключен/не трогаем: проверки деки пропускаются."""
    return DECK_OFF_FLAG.exists()


def _ssh_run_once(cmd_suffix: str, timeout: float = 10.0):
    host = os.environ.get("ALPHA_DECK_HOST", "deck@<HOST_DECK>")
    key = os.environ.get("ALPHA_DECK_KEY", r"<HOME>/.ssh/id_rsa")
    known_hosts = os.environ.get("ALPHA_DECK_KNOWN_HOSTS", r"<HOME>/.ssh/known_hosts")
    cmd = ["ssh", "-i", key, "-o", f"UserKnownHostsFile={known_hosts}", "-o", "BatchMode=yes",
           "-o", "ConnectTimeout=8", host, cmd_suffix]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                            timeout=timeout)
        if r.returncode != 0:
            return False, (r.stderr or "").strip()[:200]
        return True, (r.stdout or "").strip()
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


DECK_SSH_IMMEDIATE_RETRIES = int(os.environ.get("ALPHA_WATCH_DECK_SSH_RETRIES", "1"))


def _ssh_run(cmd_suffix: str, timeout: float = 10.0):
    """Общий ssh-вызов на Steam Deck теми же умолчаниями, что и dispatch._deck_file_exists (кириллический
    HOME) плюс явная кодировка UTF-8 (CEO 27.09: без неё вывод шёл кракозябрами — Python декодировал
    ssh-байты локальной кодировкой Windows-консоли, как уже исправлено в role_memory.py:deck_alert).
    Один немедленный повтор при неудаче (CEO 27.09: разовый ssh-таймаут, а через мгновение сам ssh
    отвечает за 0,44 с — без повтора это была ложная тревога) — фильтрует разовый сетевой сбой внутри
    ОДНОГО цикла; устойчивость к сбоям ПОДРЯД НЕСКОЛЬКИХ циклов — в _apply_ssh_fail_streak.
    Возвращает (ok, stdout) — ok=False на любой ошибке (сама по себе становится находкой, п.2в)."""
    result = _ssh_run_once(cmd_suffix, timeout)
    for _ in range(DECK_SSH_IMMEDIATE_RETRIES):
        if result[0]:
            return result
        result = _ssh_run_once(cmd_suffix, timeout)
    return result


# CEO 27.09: ALERT-idle-deck при существующем HOLD — ожидаемое состояние (паузу ставит CEO по слову
# владельца), не будить — одной строкой в сводку (см. WATCH_SUMMARY_KINDS/notify_findings).
DECK_IDLE_ALERT_NAME = "ALERT-idle-deck"


def check_steam_deck(ssh_run=_ssh_run, hold_hint=None, observed: dict = None) -> list:
    """п.2б/в: ALERT-* Steam Deck + простой при непустой очереди; ssh-хелпер подменяем в тестах.
    v2: `hold_hint` — последнее известное состояние HOLD (из watch-state); если проверка HOLD сама упала
    (таймаут ssh), используем его (нет подсказки — считаем HOLD активным: сам сбой проверки сообщается
    отдельно как deck-ssh-error), чтобы ожидаемый простой не превращался в тревогу. `observed` — словарь,
    куда кладём свежее состояние HOLD (`observed["hold"]`), если его удалось прочитать.
    Есть флаг `deck-off` — ssh не вызывается вовсе, находок нет."""
    if deck_off():
        return []
    out = []
    ok, alerts = ssh_run("for f in ~/alpha/queue/ALERT-*; do [ -f \"$f\" ] && "
                          "echo \"$(basename $f): $(head -c 200 $f)\"; done; true")
    ok_hold, hold_out = ssh_run("[ -f ~/alpha/queue/HOLD ] && echo HOLD || echo NOHOLD")
    if ok_hold:
        hold_active = hold_out.strip() == "HOLD"
        if observed is not None:
            observed["hold"] = hold_active
    else:
        hold_active = True if hold_hint is None else bool(hold_hint)
    if not ok:
        out.append(Finding("deck-ssh-error", "alerts", f"не удалось проверить тревоги Steam Deck: {alerts}"))
    elif alerts.strip():
        for line in alerts.strip().splitlines():
            name = line.split(":", 1)[0].strip()
            if name == DECK_IDLE_ALERT_NAME and hold_active:
                out.append(Finding("deck-idle-expected", name, f"Steam Deck (HOLD активен, ожидаемо): {line[:200]}"))
            else:
                out.append(Finding("deck-alert", name, f"Steam Deck: {line[:200]}"))

    # простой при непустой очереди: HOLD снят, очередь непуста, но STATUS давно не обновлялся
    if not ok_hold:
        out.append(Finding("deck-ssh-error", "hold", f"не удалось проверить HOLD Steam Deck: {hold_out}"))
    elif not hold_active:
        # TK-016: задания gridq — queue/pending/*.job (прежний счёт queue/*.json всегда давал 0 → молчание)
        ok2, status_info = ssh_run(
            "n=$(ls ~/alpha/queue/pending/ ~/alpha/queue/running/ 2>/dev/null | grep -c '\\.job$'); "
            "age=$(( $(date +%s) - $(stat -c %Y ~/alpha/queue/STATUS 2>/dev/null || echo 0) )); "
            "echo \"$n $age\"")
        if not ok2:
            out.append(Finding("deck-ssh-error", "queue", f"не удалось проверить очередь Steam Deck: {status_info}"))
        elif status_info.strip():
            try:
                n_pending, age_s = (int(x) for x in status_info.split())
                if n_pending > 0 and age_s > DECK_QUEUE_STALE_MINUTES * 60:
                    out.append(Finding("deck-queue-stale", "queue",
                                        f"очередь Steam Deck не пуста ({n_pending}), STATUS не обновлялся "
                                        f"{age_s // 60:.0f} мин — похоже на простой"))
            except ValueError:
                pass  # неожиданный вывод — не валим находками на угад, но и не молчим полностью
    out += check_deck_frozen(ssh_run)
    return out


# TK-016: disk-guard.sh замораживает счёт (метка ~/alpha/sync/DISK-FULL) — замороженный gridq сам ALERT-*
# не пишет, STATUS стоит. Отдельный запрос: метка (число «frozen» в ней), свободно ГБ, замороженные юниты.
DECK_FROZEN_CMD = ("if [ -f ~/alpha/sync/DISK-FULL ]; then echo \"mark $(grep -c '^frozen ' ~/alpha/sync/DISK-FULL)\"; "
                   "else echo nomark; fi; echo \"free $(df --output=avail -BG ~/alpha | tail -1 | tr -dc 0-9)\"; "
                   "systemctl --user list-units --state=frozen --no-legend --plain | awk '{print \"frozen \" $1}'; true")


def check_deck_frozen(ssh_run=_ssh_run) -> list:
    if deck_off():
        return []
    ok, info = ssh_run(DECK_FROZEN_CMD)
    if not ok:
        return [Finding("deck-ssh-error", "frozen", f"не удалось проверить заморозку Steam Deck: {info}")]
    mark_n, free_gb, frozen = None, "?", []
    for line in info.strip().splitlines():
        head, _, rest = line.strip().partition(" ")
        if head == "mark":
            mark_n = rest.strip() or "0"
        elif head == "free":
            free_gb = rest.strip() or "?"
        elif head == "frozen" and rest.strip():
            frozen.append(rest.strip())
    if mark_n is None and not frozen:
        return []
    why = "метка ~/alpha/sync/DISK-FULL (disk-guard)" if mark_n is not None else "без метки DISK-FULL"
    n = len(frozen) if frozen else mark_n
    units = ", ".join(frozen[:6]) + (" …" if len(frozen) > 6 else "")
    return [Finding("deck-frozen", "frozen",
                    f"Steam Deck заморожен: {why}, заморожено юнитов {n}, свободно {free_gb} ГБ"
                    + (f" ({units})" if units else "") + " — счёт стоит, нужно место/разморозка")]


def collect_findings(state: dict, now, ssh_run=_ssh_run, started_at=None, hold_hint=None,
                     observed: dict = None) -> list:
    findings = []
    findings += check_dispatcher_alive(state, now, started_at)
    findings += check_blocked_and_needs_owner(now)
    findings += check_orphan_tickets(now)
    findings += check_steam_deck(ssh_run, hold_hint=hold_hint, observed=observed)
    return findings


# --- дедуп (вид, ключ) с повтором раз в WATCH_DEDUP_REPEAT_HOURS, пока не снято (п.2г) -----------
#
# Steam Deck перезаписывает ALERT-* каждые ~15 мин с той же сутью, но новой меткой времени внутри
# (CEO 27.09: «18:26Z → 18:41Z → 18:56Z», иначе будило бы на каждое перезаписывание) — для deck-alert/
# deck-idle-expected сравниваем СОДЕРЖИМОЕ без времени, не только (вид, ключ): та же суть — молчим до
# истечения WATCH_DEDUP_REPEAT_HOURS, другая суть — будим сразу, как новую находку.
_TIME_TOKEN_RE = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\s*(?:Z|UTC)?\b", re.IGNORECASE)

# v2 (02.10): нормализация сигнатуры. Прежняя вырезала только «ЧЧ:ММ», и от ISO-метки «2026-10-02T00:03:40Z»
# оставалось «2026-10-02T00:<t>» — час менялся, и сигнатура «дрейфовала» каждый час; счётчики тревог
# («всего разборов 3», «1 суток», «load1 0.31») дрейфовали тоже — один и тот же ALERT-rework уходил CEO
# снова и снова.
_ISO_TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}(?:T\d{1,2}(?::\d{0,2}){0,2}|\s\d{1,2}:\d{2}(?::\d{2})?)"
                        r"(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?")
_T_FRAGMENT_RE = re.compile(r"\bT\d{1,2}:\d{0,2}")  # «T23:» — метка, обрезанная на границе строки
_COUNTER_RES = (
    (re.compile(r"(всего\s+\S+\s+)\d+", re.IGNORECASE), r"\1#"),            # «всего разборов 3»
    (re.compile(r"\b\d+(\s+суток)\b"), r"#\1"),                              # «1 суток»
    (re.compile(r"\b\d+(\s+р(?:аз|аза)?)\b"), r"#\1"),                       # «3 р», «3 раз»
    (re.compile(r"\b(load\d*\s+)\d+(?:\.\d+)?", re.IGNORECASE), r"\1#"),     # «load1 0.31»
    (re.compile(r"\b\d+\.\d+\b"), "#"),                                      # прочие десятичные (замеры)
)
SIGNATURE_MAX_CHARS = 140  # хвост строки обрезан `head -c 200` и «плавает» вместе с шириной чисел — не сравниваем


def normalize_signature(text: str) -> str:
    """Суть тревоги без дрейфующих частей: ISO-времена, «T23:», время суток, счётчики → заглушки."""
    t = _ISO_TS_RE.sub("<ts>", text)
    t = _T_FRAGMENT_RE.sub("<ts>", t)
    t = _TIME_TOKEN_RE.sub("<t>", t)
    for rx, repl in _COUNTER_RES:
        t = rx.sub(repl, t)
    return re.sub(r"\s+", " ", t).strip()[:SIGNATURE_MAX_CHARS]

WATCH_SUMMARY_KINDS = {"deck-idle-expected", "deck-ssh-error-transient"}
WATCH_SUMMARY_EVERY_HOURS = float(os.environ.get("ALPHA_WATCH_SUMMARY_HOURS", "1"))

# CEO 27.09: разовые ssh-таймауты (23:59, 00:11 — сразу после ssh отвечал за 0,44 с, ни нагрузки, ни
# давления I/O) будили немедленно. Один повтор внутри цикла — в _ssh_run; здесь — устойчивость к
# ПОДРЯД НЕСКОЛЬКИМ неудачным ЦИКЛАМ: будим только после DECK_SSH_FAIL_STREAK_TO_WAKE подряд (умолч.
# 3 цикла × WATCH_INTERVAL_S ≈ 6 мин), разовые/парные неудачи — в сводку (deck-ssh-error-transient).
DECK_SSH_FAIL_STREAK_TO_WAKE = int(os.environ.get("ALPHA_WATCH_DECK_SSH_FAIL_STREAK", "3"))


def _apply_ssh_fail_streak(findings: list, ws: dict) -> list:
    streaks = ws.setdefault("deck_ssh_fail_streak", {})
    failed_keys_this_cycle = {f.key for f in findings if f.kind == "deck-ssh-error"}
    out = []
    for f in findings:
        if f.kind != "deck-ssh-error":
            out.append(f)
            continue
        streaks[f.key] = streaks.get(f.key, 0) + 1
        if streaks[f.key] >= DECK_SSH_FAIL_STREAK_TO_WAKE:
            out.append(f)
        else:
            out.append(Finding("deck-ssh-error-transient", f.key,
                                f"{f.message} (сбой {streaks[f.key]}/{DECK_SSH_FAIL_STREAK_TO_WAKE} циклов подряд)"))
    for key in list(streaks):
        if key not in failed_keys_this_cycle:
            streaks.pop(key, None)  # ssh снова отвечает — счётчик подряд сбрасывается
    return out


def _content_signature(f) -> str:
    if f.kind not in ("deck-alert", "deck-idle-expected"):
        return ""  # для остальных видов сигнатура не участвует — только временное окно дедупа
    return normalize_signature(f.message)


def _repeat_hours(kind: str) -> float:
    """Через сколько часов та же находка напоминается: orphan/deck-alert — раз в сутки (v2), остальные —
    WATCH_DEDUP_REPEAT_HOURS."""
    return WATCH_LONG_REPEAT_HOURS if kind in WATCH_LONG_REPEAT_KINDS else WATCH_DEDUP_REPEAT_HOURS


def load_watch_state() -> dict:
    try:
        return json.loads(WATCH_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_watch_state(ws: dict) -> None:
    WATCH_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = WATCH_STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(ws, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(WATCH_STATE_FILE)


def _flush_pending_summary(ws: dict, now) -> None:
    pending = ws.get("pending_summary") or []
    if pending:
        line = f"{len(pending)} сигнал(ов): " + " | ".join(pending)
        D.append_ceo_inbox("*", "watch-summary", line, now)
    ws["pending_summary"] = []
    ws["last_summary_flush"] = T.now_iso(now)


def notify_findings(findings: list, ws: dict, now) -> list:
    """Возвращает находки, по которым реально написали (для тестов). Дедуп — (kind, key) + для
    Steam Deck ещё и содержимое без времени (см. выше). Виды из WATCH_SUMMARY_KINDS не будят сразу —
    копятся и уходят одной строкой не реже WATCH_SUMMARY_EVERY_HOURS (не молчание, просто не срочно;
    CEO 27.09: ALERT-idle-deck при активном HOLD — ожидаемое состояние)."""
    notified = ws.setdefault("notified", {})
    current_keys = set()
    posted = []
    # v2: цикл с ошибкой ssh не видит тревог Steam Deck вовсе — их маркеры НЕ забываем, иначе первый же
    # успешный цикл сообщит те же самые тревоги заново
    ssh_failed = any(f.kind in ("deck-ssh-error", "deck-ssh-error-transient") for f in findings)
    for f in findings:
        marker = f"{f.kind}:{f.key}"
        current_keys.add(marker)
        entry = notified.get(marker) or {}
        if isinstance(entry, str):  # прежний формат watch-state.json (v1.4): значение — только время
            entry = {"ts": entry}
        sig = _content_signature(f)
        sig_changed = bool(sig) and entry.get("sig") is not None and entry.get("sig") != sig
        last_ts = entry.get("ts")
        time_elapsed = last_ts is None or (now - T.parse_dt(last_ts)).total_seconds() >= (
            _repeat_hours(f.kind) * 3600)
        if sig_changed or time_elapsed:
            if f.kind in WATCH_SUMMARY_KINDS:
                pending = ws.setdefault("pending_summary", [])
                pending.append(f"{T.now_iso(now)} [{f.kind}] {f.message[:150]}")
            else:
                D.append_ceo_inbox("*", f"watch-{f.kind}", f.message, now)
            notified[marker] = {"sig": sig, "ts": T.now_iso(now)}
            posted.append(f)
        else:
            entry["sig"] = sig  # молча освежаем — на случай, если контент чуть дрейфует без смены сути
    # снятые находки — забыть, чтобы будущее повторение не ждало старого окна дедупа
    for marker in list(notified):
        if marker not in current_keys:
            if ssh_failed and marker.startswith("deck-") and not marker.startswith("deck-ssh-error"):
                continue  # проверка Steam Deck в этом цикле не состоялась — «снятой» тревога не считается
            notified.pop(marker, None)
    # периодический флаш накопленной сводки — независимо от того, добавилось что-то в этом цикле или нет
    last_flush = ws.get("last_summary_flush")
    last_flush_dt = T.parse_dt(last_flush) if last_flush else None
    due = last_flush_dt is None or (now - last_flush_dt).total_seconds() >= WATCH_SUMMARY_EVERY_HOURS * 3600
    if ws.get("pending_summary") and due:
        _flush_pending_summary(ws, now)
    elif "last_summary_flush" not in ws:
        ws["last_summary_flush"] = T.now_iso(now)  # точка отсчёта окна с первого же цикла
    return posted


def write_heartbeat(now, findings_count: int) -> None:
    WATCH_HEARTBEAT_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = WATCH_HEARTBEAT_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"ts": T.now_iso(now), "findings": findings_count}, ensure_ascii=False),
                    encoding="utf-8")
    tmp.replace(WATCH_HEARTBEAT_FILE)


def run_once(now=None, ssh_run=_ssh_run) -> list:
    now = now or datetime.now().astimezone()
    ws = load_watch_state()
    if "started_at" not in ws:
        ws["started_at"] = T.now_iso(now)  # с первого цикла — точка отсчёта грации check_dispatcher_alive
    started_at = T.parse_dt(ws["started_at"])
    state = D.load_state()
    observed = {}
    findings = collect_findings(state, now, ssh_run, started_at, hold_hint=ws.get("deck_hold"), observed=observed)
    if "hold" in observed:
        ws["deck_hold"] = observed["hold"]  # последнее известное состояние HOLD — на случай таймаута его проверки
    findings = _apply_ssh_fail_streak(findings, ws)
    posted = notify_findings(findings, ws, now)
    save_watch_state(ws)
    write_heartbeat(now, len(findings))
    return posted


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--once" in argv:
        posted = run_once()
        print(f"[watch] once: findings={len(posted)}")
        return 0
    print(f"[watch] loop every {WATCH_INTERVAL_S:.0f}s")
    while True:
        try:
            run_once()
        except Exception as e:
            print(f"[watch] cycle error: {type(e).__name__}: {e}", file=sys.stderr)
        time.sleep(WATCH_INTERVAL_S)


if __name__ == "__main__":
    sys.exit(main())
