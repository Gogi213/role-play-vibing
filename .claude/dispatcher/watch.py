"""Сторож CEO без модели (судья TK-002 п.2) — заменяет получасовой крон CEO. Покрывает то же самое:
диспетчер жив, тревоги второй машины (ALERT-*), простой второй машины при непустой
очереди, тикеты-сироты (in_progress/waiting без новой записи дольше порога), blocked/needs_owner.
Ошибка ssh/чтения — сама сигнал (не молчание, п.2в). Дедуп по (вид, ключ), повтор раз в
WATCH_DEDUP_REPEAT_HOURS, пока проблема не снята (п.2г). Пишет `ceo-wake.log`/`ceo-inbox.md` только
при находке; сердцебиение (`watch-heartbeat.json`) обновляется каждый цикл независимо от находок —
его возраст проверяет хук `role_memory.py` (кто сторожит сторожа, п.2а).

v2 (02.10, аудит ролевой системы — «сторож: тревога с меткой времени в подписи → повтор каждые 10 мин»):
тревоги второй машины сравниваются по нормализованной сигнатуре (без ISO-времён, кусков вроде `T23:`, времён
суток и счётчиков «всего разборов N») и сообщаются один раз, пока тревога не исчезнет или не изменится по
сути (напоминание — раз в WATCH_LONG_REPEAT_HOURS); известные тревоги не «забываются» после цикла с
ошибкой ssh; таймаут проверки HOLD не превращает ожидаемый простой в тревогу; orphan по закрытым
(done/cancelled) тикетам молчат, по открытым повторяются не чаще раза в сутки.
v3 (03.10, В-173): проверки трат (суточный/часовой расход, бюджет тикета) удалены — лимитов денег нет.

Запуск из папки плагина (проект — --project <путь>, иначе RPV_PROJECT / CLAUDE_PROJECT_DIR / ближайший каталог
вверх с .claude/roles):
        python <плагин>/.claude/dispatcher/watch.py --project <проект> --once   (для крона/планировщика Windows)
        python <плагин>/.claude/dispatcher/watch.py --project <проект>          (цикл раз в WATCH_INTERVAL_S)
"""
from __future__ import annotations

import atexit
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
import project as P  # noqa: E402
import ticket as T  # noqa: E402

# Пути состояния — в `<проект>/.claude/dispatcher/` (тот же каталог, что у диспетчера); проект выставляет
# D.configure_project() при импорте dispatch (по --project/RPV_PROJECT/CLAUDE_PROJECT_DIR/cwd) и в main().
DISPATCHER_DIR = WATCH_HEARTBEAT_FILE = WATCH_PID_FILE = WATCH_STATE_FILE = DECK_OFF_FLAG = None


def _set_paths() -> None:
    global DISPATCHER_DIR, WATCH_HEARTBEAT_FILE, WATCH_PID_FILE, WATCH_STATE_FILE, DECK_OFF_FLAG
    DISPATCHER_DIR = D.DISPATCHER_DIR
    WATCH_HEARTBEAT_FILE = DISPATCHER_DIR / "watch-heartbeat.json"
    WATCH_PID_FILE = DISPATCHER_DIR / "watch.pid"  # замок единственного экземпляра сторожа (D.acquire_instance_lock)
    WATCH_STATE_FILE = DISPATCHER_DIR / "watch-state.json"
    # Файл есть — сторож вообще не ходит на вторую машину по ssh
    # (ни ALERT-*/HOLD/очередь, ни заморозка); наличие проверяется на КАЖДОМ цикле — перезапуск не нужен.
    DECK_OFF_FLAG = DISPATCHER_DIR / "deck-off"


_set_paths()


def configure_project(root) -> Path:
    """Переключает проект диспетчера и сторожа (пути состояния — от `<root>/.claude/dispatcher/`)."""
    D.configure_project(root)
    _set_paths()
    return D.PROJECT_ROOT


# Корень на второй машине (там очередь заданий, метки HOLD/DISK-FULL): RPV_DECK_ROOT, прежнее ALPHA_DECK_ROOT.
DECK_ROOT = P.env("DECK_ROOT", "~/rpv")

WATCH_INTERVAL_S = float(P.env("WATCH_INTERVAL", "120"))
WATCH_DEDUP_REPEAT_HOURS = float(P.env("WATCH_REPEAT_HOURS", "2"))
# v2: виды находок, которые повторяются не чаще раза в сутки (или пока не изменятся по сути); blocked/needs_owner
# диспетчер уже сообщил один раз (`ceo-inbox`), сторож лишь страхует — раз в сутки
WATCH_LONG_REPEAT_HOURS = float(P.env("WATCH_LONG_REPEAT_HOURS", "24"))
WATCH_LONG_REPEAT_KINDS = {"orphan-ticket", "deck-alert", "deck-idle-expected", "blocked", "needs_owner"}
CLOSED_TICKET_STATUSES = ("done", "cancelled")
DISPATCH_STALE_MINUTES = float(P.env("WATCH_DISPATCH_STALE_MIN", "5"))
ORPHAN_TICKET_HOURS = float(P.env("WATCH_ORPHAN_HOURS", "2"))
# через сколько минут без обновления STATUS непустая очередь на второй машине считается простоем
DECK_QUEUE_STALE_MINUTES = float(P.env("WATCH_DECK_QUEUE_STALE_MIN", "10"))


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


def check_orphan_tickets(now, skip_ids=()) -> list:
    """п.2б: in_progress/waiting/in_review без новой записи дольше порога — сирота (in_review — аудит 03.10: ревьюер
    мог упасть, и тикет висел бы вечно) (TK-001 п.2, до правила
    (а') это значило «замерла навсегда»; правило (а') её теперь будит, но сторож всё равно следит на
    случай, если тикет застрял по другой причине — троттлинг/сама роль не отвечает)."""
    out = []
    for path in T.list_tickets(D.TICKETS_DIR):
        try:
            tkt = T.read_ticket(path)
        except Exception:
            continue
        if tkt.status in CLOSED_TICKET_STATUSES or tkt.status not in ("in_progress", "waiting", "in_review"):
            continue
        if tkt.id in skip_ids:  # цель wait_for жива (TK-056 п.4): долгое ожидание — не сирота
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


# --- триаж ожиданий без LLM (TK-056 п.4) ---------------------------------------------------------
DEAD_WAIT_STRIKES = int(P.env("WATCH_DEAD_WAIT_STRIKES", "2"))  # подряд мёртвых проверок до действия


def _producer_pattern(path: str) -> str:
    job = os.path.basename(path.rstrip("/"))
    job = job.rsplit(".", 1)[0] if "." in job else job
    job = re.sub(r"[^A-Za-z0-9_-]", "", job)
    return f"[{job[0]}]{job[1:]}" if len(job) >= 4 else ""


def probe_wait_target(alias: str, what: str, arg: str) -> str:
    """`exists` — путь есть; `producer` — пути нет, но юнит/процесс с именем задания жив; `dead` — нет ни того, ни
    другого; `unknown` — ssh не ответил (не считаем). Имя задания — basename пути без расширения."""
    pat = _producer_pattern(arg)
    if what != "path" or not arg.startswith("/") or not pat:
        return "unknown"
    q = D._remote_test_arg(arg)
    remote = (f"if test -e {q}; then echo exists; "
              f"elif {{ systemctl list-units --all --plain --no-legend --state=active,activating 2>/dev/null; "
              f"ps -eo args 2>/dev/null; }} | grep -q -e '{pat}'; then echo producer; else echo dead; fi")
    try:
        r = subprocess.run(D._ssh_cmd(alias, remote), capture_output=True, timeout=20)
    except Exception:
        return "unknown"
    out = (r.stdout or b"").decode("utf-8", "replace").strip().splitlines()
    return out[0] if r.returncode == 0 and out and out[0] in ("exists", "producer", "dead") else "unknown"


def _wait_target_state(tkt, probe) -> str:
    parsed = T.parse_wait_for(tkt.header.get("wait_for") or "")
    if parsed is None:
        return "invalid"
    if parsed[0] == "file":
        return "exists"
    if parsed[0] == "ticket":
        return "exists" if (D.TICKETS_DIR / f"{parsed[1]}.md").exists() else "dead"
    if parsed[0] == "host":
        return probe(parsed[1], parsed[2], parsed[3])
    return "unknown"


def triage_waits(ws: dict, now, probe=probe_wait_target) -> set:
    """waiting-тикеты: цель wait_for существует или её делает живой юнит/процесс — тикет «жив» (возвращаются его id —
    сторож не зовёт его сиротой). Цель мертва DEAD_WAIT_STRIKES проверок подряд → тикет в in_progress с пустым wait_for и
    записью — диспетчер будит владельца (resume), CEO не нужен; повторно та же цель → blocked (CEO, решение)."""
    dead = ws.setdefault("dead_wait", {})
    alive, seen = set(), set()
    for path in T.list_tickets(D.TICKETS_DIR):
        try:
            tkt = T.read_ticket(path)
        except Exception:
            continue
        if tkt.status != "waiting" or not (tkt.header.get("wait_for") or "").strip():
            continue
        spec = tkt.header["wait_for"].strip()
        st = _wait_target_state(tkt, probe)
        if st in ("exists", "producer", "unknown"):  # годный wait_for: условие проверяется диспетчером — не сирота
            alive.add(tkt.id)
            dead.pop(tkt.id, None)
            continue
        if st != "dead":
            continue
        seen.add(tkt.id)
        ent = dead.get(tkt.id) or {}
        n = ent.get("n", 0) + 1 if ent.get("spec") == spec else 1
        dead[tkt.id] = {**ent, "spec": spec, "n": n}
        if n < DEAD_WAIT_STRIKES:
            continue
        repeat = spec in ent.get("acted", [])
        why = f"сторож: цель wait_for `{spec}` не существует и её никто не производит ({n} проверки подряд)"
        with T.ticket_lock(path):
            if repeat:
                T.append_log(path, "watch", why + " — второй раз та же цель, тикет blocked (решение за CEO)", now)
                T.write_header_updates(path, {"status": "blocked"}, now)
            else:
                T.append_log(path, "watch", why + " — ожидание снято, владелец будится: перезапусти задание или смени wait_for", now)
                T.write_header_updates(path, {"status": "in_progress", "wait_for": "", "on_met": ""}, now)
        dead[tkt.id] = {"spec": spec, "n": 0, "acted": ent.get("acted", []) + [spec]}
    for tid in list(dead):
        if tid not in seen and tid not in alive:
            dead.pop(tid, None)
    return alive


# --- сервер счёта: задания ждут замка, а машина простаивает (TK-070 п.1) ----------------------------------------------
SERVER_ALIAS = "calc"
SERVER_LOAD_MAX = float(P.env("WATCH_SERVER_LOAD_MAX", "4"))
SERVER_LOCK_WAIT_S = float(P.env("WATCH_SERVER_LOCK_WAIT_MIN", "20")) * 60
SERVER_WAKE_REPEAT_HOURS = float(P.env("WATCH_SERVER_WAKE_REPEAT_HOURS", "2"))
SERVER_PROBE = ("echo load $(cut -d' ' -f1 /proc/loadavg); "
                "ps -eo pid=,ppid=,etimes=,args= | grep -E 'benchrun.sh|flock -[xs] [0-9]' | grep -v grep")


def analyze_server(text: str) -> dict:
    """Вывод SERVER_PROBE → {load, wait_s (самое долгое ожидание замка), holder (args держателя или '')}.
    Ждущий — `flock -x|-s N` внутри benchrun.sh (его родитель — ждущий benchrun); держатель — benchrun.sh без такого flock."""
    load, procs = None, []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("load "):
            try:
                load = float(line.split()[1])
            except (ValueError, IndexError):
                pass
            continue
        parts = line.split(None, 3)
        if len(parts) == 4 and all(x.isdigit() for x in parts[:3]):
            procs.append((int(parts[0]), int(parts[1]), int(parts[2]), parts[3]))
    waiting_parents = {pp for _, pp, _, a in procs if a.startswith("flock -")}
    wait_s = max([e for _, _, e, a in procs if a.startswith("flock -")] or [0])
    holder = ""
    for pid, _, _, a in procs:
        if "benchrun.sh" in a and pid not in waiting_parents:
            holder = a
            break
    return {"load": load, "wait_s": wait_s, "holder": holder}


def holder_ticket(holder: str):
    m = re.search(r"(?<![A-Za-z0-9])tk0?(\d{2,3})", holder, re.I) or re.search(r"TK-0?(\d{2,3})", holder)
    if not m:
        return None
    tid = f"TK-0{int(m.group(1)):02d}" if int(m.group(1)) < 100 else f"TK-{int(m.group(1))}"
    return tid if (D.TICKETS_DIR / f"{tid}.md").exists() else None


def check_server_idle(ws: dict, now, ssh_run=None) -> list:
    """Нагрузка сервера < SERVER_LOAD_MAX, а задания ждут замка > 20 мин → будим ВЛАДЕЛЬЦА тикета держателя замка
    (`next: <владелец>` + запись); держатель не определился — строка CEO (эскалация). Работает без сессии CEO."""
    try:
        if ssh_run is not None:
            out = ssh_run(SERVER_PROBE)
        else:
            r = subprocess.run(D._ssh_cmd(SERVER_ALIAS, SERVER_PROBE), capture_output=True, timeout=20)
            out = (r.stdout or b"").decode("utf-8", "replace") if r.returncode in (0, 1) else ""
    except Exception:
        return []
    info = analyze_server(out or "")
    if info["load"] is None or info["load"] >= SERVER_LOAD_MAX or info["wait_s"] < SERVER_LOCK_WAIT_S:
        ws.get("server_idle", {}).clear()
        return []
    tid = holder_ticket(info["holder"]) or "?"
    seen = ws.setdefault("server_idle", {})
    prev = seen.get(tid)
    if prev:
        try:
            if now - T.parse_dt(prev) < timedelta(hours=SERVER_WAKE_REPEAT_HOURS):
                return []
        except ValueError:
            pass
    seen[tid] = T.now_iso(now)
    why = (f"сервер счёта простаивает: load1={info['load']:.1f} < {SERVER_LOAD_MAX:g}, задания ждут замка "
           f"{int(info['wait_s'] // 60)} мин; держатель: {info['holder'][:120] or 'не определён'}")
    if tid != "?":
        path = D.TICKETS_DIR / f"{tid}.md"
        try:
            with T.ticket_lock(path):
                tkt = T.read_ticket(path)
                if tkt.owner in ("researcher", "engineer", "judge"):
                    T.append_log(path, "watch", why + ". Твоё задание держит замок — проверь, не завис ли шаг, "
                                 "и освободи замок или поправь задание.", now)
                    T.write_header_updates(path, {"next": tkt.owner}, stamp_updated=False)
                    return []
        except Exception as e:
            print(f"[watch] server-idle: не разбудил {tid}: {type(e).__name__}: {e}", file=sys.stderr)
    return [Finding("server-idle", tid, why + " — владельца тикета определить не удалось, решение за CEO")]


# --- счётчик застоя по runs.log (TK-056 п.4) -----------------------------------------------------
STALL_RUNS = int(P.env("WATCH_STALL_RUNS", "2"))  # подряд таймаутов / холостых запусков до блока
_RUN_KV = re.compile(r"(\w+)=(\S+)")


def _recent_runs(runs_path, tid: str, limit: int) -> list:
    """Последние `limit` запусков ролей тикета из runs.log (без on_met/stopped), старые → новые: (ts, роль, статус)."""
    try:
        lines = Path(runs_path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    rows = []
    for ln in reversed(lines[-4000:]):
        parts = ln.split()
        if len(parts) < 4 or parts[1] != tid or parts[2] == "on_met":
            continue
        st = dict(_RUN_KV.findall(ln)).get("status", "?")
        if st == "stopped":
            continue
        rows.append((T.parse_dt(parts[0]), parts[2], st))
        if len(rows) >= limit:
            break
    return rows[::-1]


def triage_stalls(ws: dict, now, runs_path=None) -> list:
    """Застой тикета in_progress: последние STALL_RUNS запуска — все timeout (resume уже пробовался) либо все «холостые»
    (status=ok, а роль не оставила в логе ни одной записи между концом прежнего запуска тикета и концом этого) → тикет
    blocked с записью watch (сигнал CEO через находку blocked). Один раз на последний запуск (ws['stall'])."""
    done = ws.setdefault("stall", {})
    acted = []
    for path in T.list_tickets(D.TICKETS_DIR):
        try:
            tkt = T.read_ticket(path)
        except Exception:
            continue
        if tkt.status != "in_progress":
            continue
        rows = _recent_runs(runs_path or D.RUNS_LOG, tkt.id, STALL_RUNS + 1)
        if len(rows) < STALL_RUNS or done.get(tkt.id) == T.now_iso(rows[-1][0]):
            continue
        last = rows[-STALL_RUNS:]
        if all(r[2] == "timeout" for r in last):
            why = f"{STALL_RUNS} таймаута запуска подряд (resume уже пробовался)"
        elif all(r[2] == "ok" for r in last) and len(rows) > STALL_RUNS:
            idle = 0
            for i in range(len(rows) - STALL_RUNS, len(rows)):
                lo, hi, role = rows[i - 1][0], rows[i][0], rows[i][1]
                if lo and hi and not any(e.author == role and lo < e.ts <= hi for e in tkt.log):
                    idle += 1
            if idle < STALL_RUNS:
                continue
            why = f"{STALL_RUNS} холостых запуска подряд (роль не оставила записи в логе)"
        else:
            continue
        with T.ticket_lock(path):
            T.append_log(path, "watch", f"сторож: {why} — тикет blocked, решение за CEO (по runs.log)", now)
            T.write_header_updates(path, {"status": "blocked"}, now)
        done[tkt.id] = T.now_iso(rows[-1][0])
        acted.append(tkt.id)
    return acted


def deck_off() -> bool:
    """Проверки второй машины пропускаются: флаг `.claude/dispatcher/deck-off` или не задан `RPV_DECK_HOST`."""
    return DECK_OFF_FLAG.exists() or not P.env("DECK_HOST")


def _ssh_run_once(cmd_suffix: str, timeout: float = 10.0):
    host = P.env("DECK_HOST")  # без умолчания: нет переменной — проверки машины выключены (deck_off)
    if not host:
        return False, "RPV_DECK_HOST не задан"
    key = P.env("DECK_KEY")
    known_hosts = P.env("DECK_KNOWN_HOSTS")
    cmd = (["ssh"] + (["-i", key] if key else []) + (["-o", f"UserKnownHostsFile={known_hosts}"] if known_hosts else [])
           + ["-o", "BatchMode=yes", "-o", "ConnectTimeout=8", host, cmd_suffix])
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                            timeout=timeout)
        if r.returncode != 0:
            return False, (r.stderr or "").strip()[:200]
        return True, (r.stdout or "").strip()
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


DECK_SSH_IMMEDIATE_RETRIES = int(P.env("WATCH_DECK_SSH_RETRIES", "1"))


def _ssh_run(cmd_suffix: str, timeout: float = 10.0):
    """Общий ssh-вызов на вторую машину теми же умолчаниями, что и dispatch._deck_file_exists (кириллический
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


def check_second_machine(ssh_run=_ssh_run, hold_hint=None, observed: dict = None) -> list:
    """п.2б/в: ALERT-* второй машины + простой при непустой очереди; ssh-хелпер подменяем в тестах.
    v2: `hold_hint` — последнее известное состояние HOLD (из watch-state); если проверка HOLD сама упала
    (таймаут ssh), используем его (нет подсказки — считаем HOLD активным: сам сбой проверки сообщается
    отдельно как deck-ssh-error), чтобы ожидаемый простой не превращался в тревогу. `observed` — словарь,
    куда кладём свежее состояние HOLD (`observed["hold"]`), если его удалось прочитать.
    Есть флаг `deck-off` — ssh не вызывается вовсе, находок нет."""
    if deck_off():
        return []
    out = []
    ok, alerts = ssh_run(f"for f in {DECK_ROOT}/queue/ALERT-*; do [ -f \"$f\" ] && "
                          "echo \"$(basename $f): $(head -c 200 $f)\"; done; true")
    ok_hold, hold_out = ssh_run(f"[ -f {DECK_ROOT}/queue/HOLD ] && echo HOLD || echo NOHOLD")
    if ok_hold:
        hold_active = hold_out.strip() == "HOLD"
        if observed is not None:
            observed["hold"] = hold_active
    else:
        hold_active = True if hold_hint is None else bool(hold_hint)
    if not ok:
        out.append(Finding("deck-ssh-error", "alerts", f"не удалось проверить тревоги второй машины: {alerts}"))
    elif alerts.strip():
        for line in alerts.strip().splitlines():
            name = line.split(":", 1)[0].strip()
            if name == DECK_IDLE_ALERT_NAME and hold_active:
                out.append(Finding("deck-idle-expected", name, f"вторая машина (HOLD активен, ожидаемо): {line[:200]}"))
            else:
                out.append(Finding("deck-alert", name, f"вторая машина: {line[:200]}"))

    # простой при непустой очереди: HOLD снят, очередь непуста, но STATUS давно не обновлялся
    if not ok_hold:
        out.append(Finding("deck-ssh-error", "hold", f"не удалось проверить HOLD второй машины: {hold_out}"))
    elif not hold_active:
        # задания очереди — queue/pending/*.job и queue/running/*.job
        ok2, status_info = ssh_run(
            f"n=$(ls {DECK_ROOT}/queue/pending/ {DECK_ROOT}/queue/running/ 2>/dev/null | grep -c '\\.job$'); "
            f"age=$(( $(date +%s) - $(stat -c %Y {DECK_ROOT}/queue/STATUS 2>/dev/null || echo 0) )); "
            "echo \"$n $age\"")
        if not ok2:
            out.append(Finding("deck-ssh-error", "queue", f"не удалось проверить очередь второй машины: {status_info}"))
        elif status_info.strip():
            try:
                n_pending, age_s = (int(x) for x in status_info.split())
                if n_pending > 0 and age_s > DECK_QUEUE_STALE_MINUTES * 60:
                    out.append(Finding("deck-queue-stale", "queue",
                                        f"очередь второй машины не пуста ({n_pending}), STATUS не обновлялся "
                                        f"{age_s // 60:.0f} мин — похоже на простой"))
            except ValueError:
                pass  # неожиданный вывод — не валим находками на угад, но и не молчим полностью
    out += check_deck_frozen(ssh_run)
    return out


# Метка заморозки <DECK_ROOT>/sync/DISK-FULL: замороженная очередь сама ALERT-*
# не пишет, STATUS стоит. Отдельный запрос: метка (число «frozen» в ней), свободно ГБ, замороженные юниты.
DECK_FROZEN_CMD = (f"if [ -f {DECK_ROOT}/sync/DISK-FULL ]; then echo \"mark $(grep -c '^frozen ' {DECK_ROOT}/sync/DISK-FULL)\"; "
                   f"else echo nomark; fi; echo \"free $(df --output=avail -BG {DECK_ROOT} | tail -1 | tr -dc 0-9)\"; "
                   "systemctl --user list-units --state=frozen --no-legend --plain | awk '{print \"frozen \" $1}'; true")


def check_deck_frozen(ssh_run=_ssh_run) -> list:
    if deck_off():
        return []
    ok, info = ssh_run(DECK_FROZEN_CMD)
    if not ok:
        return [Finding("deck-ssh-error", "frozen", f"не удалось проверить заморозку второй машины: {info}")]
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
    why = f"метка {DECK_ROOT}/sync/DISK-FULL" if mark_n is not None else "без метки DISK-FULL"
    n = len(frozen) if frozen else mark_n
    units = ", ".join(frozen[:6]) + (" …" if len(frozen) > 6 else "")
    return [Finding("deck-frozen", "frozen",
                    f"вторая машина заморожена: {why}, заморожено юнитов {n}, свободно {free_gb} ГБ"
                    + (f" ({units})" if units else "") + " — счёт стоит, нужно место/разморозка")]


def collect_findings(state: dict, now, ssh_run=_ssh_run, started_at=None, hold_hint=None,
                     observed: dict = None, alive_waits=()) -> list:
    findings = []
    findings += check_dispatcher_alive(state, now, started_at)
    findings += check_blocked_and_needs_owner(now)
    findings += check_orphan_tickets(now, alive_waits)
    findings += check_second_machine(ssh_run, hold_hint=hold_hint, observed=observed)
    return findings


# --- дедуп (вид, ключ) с повтором раз в WATCH_DEDUP_REPEAT_HOURS, пока не снято (п.2г) -----------
#
# Вторая машина перезаписывает ALERT-* каждые ~15 мин с той же сутью, но новой меткой времени внутри
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
WATCH_SUMMARY_EVERY_HOURS = float(P.env("WATCH_SUMMARY_HOURS", "1"))

# CEO 27.09: разовые ssh-таймауты (23:59, 00:11 — сразу после ssh отвечал за 0,44 с, ни нагрузки, ни
# давления I/O) будили немедленно. Один повтор внутри цикла — в _ssh_run; здесь — устойчивость к
# ПОДРЯД НЕСКОЛЬКИМ неудачным ЦИКЛАМ: будим только после DECK_SSH_FAIL_STREAK_TO_WAKE подряд (умолч.
# 3 цикла × WATCH_INTERVAL_S ≈ 6 мин), разовые/парные неудачи — в сводку (deck-ssh-error-transient).
DECK_SSH_FAIL_STREAK_TO_WAKE = int(P.env("WATCH_DECK_SSH_FAIL_STREAK", "3"))


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
    второй машины ещё и содержимое без времени (см. выше). Виды из WATCH_SUMMARY_KINDS не будят сразу —
    копятся и уходят одной строкой не реже WATCH_SUMMARY_EVERY_HOURS (не молчание, просто не срочно;
    CEO 27.09: ALERT-idle-deck при активном HOLD — ожидаемое состояние)."""
    notified = ws.setdefault("notified", {})
    current_keys = set()
    posted = []
    # v2: цикл с ошибкой ssh не видит тревог второй машины вовсе — их маркеры НЕ забываем, иначе первый же
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
                continue  # проверка второй машины в этом цикле не состоялась — «снятой» тревога не считается
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
    try:
        alive_waits = triage_waits(ws, now)
    except Exception as e:  # триаж не должен ронять цикл сторожа
        print(f"[watch] triage_waits: {type(e).__name__}: {e}", file=sys.stderr)
        alive_waits = set()
    try:
        triage_stalls(ws, now)
    except Exception as e:
        print(f"[watch] triage_stalls: {type(e).__name__}: {e}", file=sys.stderr)
    server_findings = []
    try:
        if ssh_run is _ssh_run:  # тесты подставляют свой ssh_run — боевой сервер не трогают
            server_findings = check_server_idle(ws, now)
    except Exception as e:
        print(f"[watch] check_server_idle: {type(e).__name__}: {e}", file=sys.stderr)
    findings = collect_findings(state, now, ssh_run, started_at, hold_hint=ws.get("deck_hold"), observed=observed,
                                alive_waits=alive_waits)
    if "hold" in observed:
        ws["deck_hold"] = observed["hold"]  # последнее известное состояние HOLD — на случай таймаута его проверки
    findings += server_findings
    findings = _apply_ssh_fail_streak(findings, ws)
    posted = notify_findings(findings, ws, now)
    save_watch_state(ws)
    write_heartbeat(now, len(findings))
    return posted


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not D.ensure_project(argv, "watch"):      # нет проекта — ошибка с подсказкой, каталоги не создаём
        return 2
    if P.project_arg(argv):                      # проект переключён флагом — пути сторожа от него
        _set_paths()
    DISPATCHER_DIR.mkdir(parents=True, exist_ok=True)
    if "--once" in argv:
        posted = run_once()
        print(f"[watch] once: findings={len(posted)}")
        return 0
    ok, why = D.acquire_instance_lock(WATCH_PID_FILE)
    if not ok:                                   # второй сторож — дублировал бы строки CEO
        print(f"[watch] {why}", file=sys.stderr)
        return 1
    atexit.register(D.release_instance_lock, WATCH_PID_FILE)
    print(f"[watch] loop every {WATCH_INTERVAL_S:.0f}s")
    while True:
        try:
            run_once()
        except Exception as e:
            print(f"[watch] cycle error: {type(e).__name__}: {e}", file=sys.stderr)
        time.sleep(WATCH_INTERVAL_S)


if __name__ == "__main__":
    if not sys.stdout.isatty():  # демон с перенаправленным выводом: чужой Ctrl+C общей консоли его не убивает (TK-072)
        import signal
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    sys.exit(main())
