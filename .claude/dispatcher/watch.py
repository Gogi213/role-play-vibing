"""Сторож CEO без модели (судья TK-002 п.2) — заменяет получасовой крон CEO. Покрывает то же самое:
тикеты-сироты (in_progress/waiting без новой записи дольше порога), blocked/needs_owner.
Дедуп по (вид, ключ), повтор раз в
WATCH_DEDUP_REPEAT_HOURS, пока проблема не снята (п.2г). Пишет `ceo-wake.log`/`ceo-inbox.md` только
при находке; сердцебиение (`watch-heartbeat.json`) обновляется каждый цикл независимо от находок —
его возраст проверяет хук `role_memory.py` (кто сторожит сторожа, п.2а).

v2 (02.10): orphan по закрытым (done/cancelled) тикетам молчат, по открытым повторяются не чаще раза в сутки.
v3 (03.10, В-173): проверки трат (суточный/часовой расход, бюджет тикета) удалены — лимитов денег нет.
v4 (TK-100 №6/№26): блок «вторая машина» (ALERT-*/HOLD/очередь/заморозка по ssh) и check_dispatcher_alive удалены —
«диспетчер жив» остаётся у supervise и doctor; тревоги хоста — адаптер RPV_HOST_ALERTS_CMD.

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
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lifewatch as LW  # noqa: E402
import dispatch as D  # noqa: E402 — переиспользуем пути/константы/append_ceo_inbox/_pid_alive
import project as P  # noqa: E402
import pulsedata as PD  # noqa: E402
import ticket as T  # noqa: E402

# Пути состояния — в `<проект>/.claude/dispatcher/` (тот же каталог, что у диспетчера); проект выставляет
# D.configure_project() при импорте dispatch (по --project/RPV_PROJECT/CLAUDE_PROJECT_DIR/cwd) и в main().
DISPATCHER_DIR = WATCH_HEARTBEAT_FILE = WATCH_PID_FILE = WATCH_STATE_FILE = None


def _set_paths() -> None:
    global DISPATCHER_DIR, WATCH_HEARTBEAT_FILE, WATCH_PID_FILE, WATCH_STATE_FILE
    DISPATCHER_DIR = D.DISPATCHER_DIR
    WATCH_HEARTBEAT_FILE = DISPATCHER_DIR / "watch-heartbeat.json"
    WATCH_PID_FILE = DISPATCHER_DIR / "watch.pid"  # замок единственного экземпляра сторожа (D.acquire_instance_lock)
    WATCH_STATE_FILE = DISPATCHER_DIR / "watch-state.json"


_set_paths()


def configure_project(root) -> Path:
    """Переключает проект диспетчера и сторожа (пути состояния — от `<root>/.claude/dispatcher/`)."""
    D.configure_project(root)
    _set_paths()
    return D.PROJECT_ROOT


WATCH_INTERVAL_S = float(P.env("WATCH_INTERVAL", "120"))
WATCH_DEDUP_REPEAT_HOURS = float(P.env("WATCH_REPEAT_HOURS", "2"))
# v2: виды находок, которые повторяются не чаще раза в сутки (или пока не изменятся по сути); blocked/needs_owner
# диспетчер уже сообщил один раз (`ceo-inbox`), сторож лишь страхует — раз в сутки
WATCH_LONG_REPEAT_HOURS = float(P.env("WATCH_LONG_REPEAT_HOURS", "24"))
WATCH_LONG_REPEAT_KINDS = {"orphan-ticket", "blocked", "needs_owner"}
CLOSED_TICKET_STATUSES = ("done", "cancelled")
# План шагов на табло: через сколько минут «в работе без плана» / «шаги не менялись» сторож напоминает роли
NO_PLAN_MINUTES = float(P.env("WATCH_NO_PLAN_MIN", "30"))
PLAN_LAG_MINUTES = float(P.env("WATCH_PLAN_LAG_MIN", "5"))  # роль обновляет шаги чуть раньше итогового comment — это не «застыл»
ORPHAN_TICKET_HOURS = float(P.env("WATCH_ORPHAN_HOURS", "2"))


@dataclass
class Finding:
    kind: str
    key: str
    message: str


# --- сбор находок (чистые функции — без сети, кроме ssh-хелперов ниже) ------------------------

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
        entries = [e for e in tkt.log if not T.author_is(e.author, "watch")]  # записи сторожа не сбрасывают таймер
        last_ts = entries[-1].ts if entries else T.parse_dt(tkt.header.get("updated")) if tkt.header.get(
            "updated") else None
        if last_ts is None:
            continue
        age_h = (now - last_ts).total_seconds() / 3600
        if age_h > ORPHAN_TICKET_HOURS:
            out.append(Finding("orphan-ticket", tkt.id,
                                f"{tkt.id}: status={tkt.status} без новой записи {age_h:.1f} ч"))
    return out


def check_no_progress_view(now) -> list:
    """TK-060: тикет in_progress дольше NO_PLAN_MINUTES без плана шагов (plan.py set) — у табло нет честного процента.
    Читает status.json сборщика (без LLM, без сети); исполнителя будит notify_findings."""
    try:
        view = json.loads((D.PROJECT_ROOT / ".claude" / "pulse" / "status.json").read_text(encoding="utf-8"))["view2"]
        if now.timestamp() - float(view["built_ts"]) > 300:
            return []  # сборщик стоит — это другая находка, не эта
        procs = {p["id"]: p for p in view["processes"]}
    except (OSError, ValueError, KeyError, TypeError):
        return []
    out = []
    for path in T.list_tickets(D.TICKETS_DIR):
        try:
            tkt = T.read_ticket(path)
        except Exception:
            continue
        p = procs.get(tkt.id)
        if tkt.status != "in_progress" or p is None or p.get("plan"):
            continue
        upd = T.parse_dt(tkt.header.get("updated")) if tkt.header.get("updated") else None
        if upd is not None and (now - upd).total_seconds() / 60 > NO_PLAN_MINUTES:
            out.append(Finding("no-plan", tkt.id, f"{tkt.id}: в работе > {NO_PLAN_MINUTES:.0f} мин без плана шагов на табло"))
    return out


def check_stale_plan(now) -> list:
    """TK-061: у in_progress-тикета есть план, в логе запись роли новее плана, а шаги не менялись > NO_PLAN_MINUTES."""
    out = []
    for path in T.list_tickets(D.TICKETS_DIR):
        try:
            tkt = T.read_ticket(path)
            if tkt.status not in ("in_progress", "waiting") or tkt.owner not in ("researcher", "engineer", "judge"):
                continue
            plan = json.loads((PD.plans_dir(D.PROJECT_ROOT) / f"{tkt.id}.json").read_text(encoding="utf-8"))
            if not plan.get("steps") or all(s.get("state") == "done" for s in plan["steps"]):
                continue
            upd = T.parse_dt(plan["updated"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        except Exception:
            continue
        role_logs = [e for e in tkt.log if not (T.author_is(e.author, "ceo") or T.author_is(e.author, "watch"))]
        if role_logs and role_logs[-1].ts - upd > timedelta(minutes=PLAN_LAG_MINUTES) and (now - upd).total_seconds() / 60 > NO_PLAN_MINUTES:
            out.append(Finding("plan-stale" if tkt.status == "in_progress" else "plan-stale-waiting", tkt.id,
                               f"{tkt.id}: запись роли в логе новее плана, шаги на табло не менялись > {NO_PLAN_MINUTES:.0f} мин"))
    return out


# --- триаж ожиданий без LLM (TK-056 п.4) ---------------------------------------------------------
DEAD_WAIT_STRIKES = int(P.env("WATCH_DEAD_WAIT_STRIKES", "2"))  # подряд мёртвых проверок до действия
OWNER_ONLY_KINDS = ("no-plan", "plan-stale", "plan-stale-waiting")  # находки адресуются владельцу тикета, не CEO
AT_GRACE_MIN = int(P.env("WATCH_AT_GRACE_MIN", "30"))  # допуск после времени `at:` до тревоги: диспетчер проверяет ожидания каждые ~15 с, сторож — раз в ~2 мин, 30 мин — запас на простой диспетчера
EMPTY_WAIT_STRIKES = int(P.env("WATCH_EMPTY_WAIT_STRIKES", "3"))  # подряд проверок waiting без wait_for и без next: диспетчер берёт next за ~15 с, сторож ходит раз в ~2 мин — 3 цикла (~6 мин) отсекают гонку с ci_watch/снятием условия
MET_GRACE_MIN = int(P.env("WATCH_MET_GRACE_MIN", "15"))  # условие host-пути выполнено, а тикет ещё waiting: диспетчер сверяет раз в 300 с (RPV_DISPATCH_WAIT_RECON_S) — 15 мин = 3 сверки без реакции
SSH_FAIL_STRIKES = int(P.env("WATCH_SSH_FAIL_STRIKES", "5"))  # подряд молчаний ssh до тревоги владельцу тикета


def _producer_pattern(path: str) -> str:
    job = os.path.basename(path.rstrip("/"))
    job = job.rsplit(".", 1)[0] if "." in job else job
    job = re.sub(r"[^A-Za-z0-9_-]", "", job)
    return f"[{job[0]}]{job[1:]}" if len(job) >= 4 else ""


def probe_wait_target(alias: str, what: str, arg: str) -> str:
    """`exists` — путь есть; `producer` — пути нет, но юнит/процесс с именем задания жив; `dead` — нет ни того, ни
    другого; `ssh-error` — ssh не ответил (считается подряд, SSH_FAIL_STRIKES → тревога владельцу); `unknown` — форма
    не проверяется ssh (условие проверяет диспетчер). Имя задания — basename пути без расширения."""
    pat = _producer_pattern(arg)
    if what != "path" or not arg.startswith("/") or not pat:
        return "unknown"
    q = D._remote_test_arg(arg)
    cat = f"; cat {q}" if arg.endswith(".json") else ""  # содержимое нужно, чтобы отличить файл хода «идёт» от «готов»
    remote = (f"if test -e {q}; then echo exists{cat}; "
              f"elif {{ systemctl list-units --all --plain --no-legend --state=active,activating 2>/dev/null; "
              f"ps -eo args 2>/dev/null; }} | grep -q -e '{pat}'; then echo producer; else echo dead; fi")
    try:
        r = subprocess.run(D._ssh_cmd(alias, remote), capture_output=True, timeout=20)
    except Exception:
        return "ssh-error"
    text = (r.stdout or b"").decode("utf-8", "replace").strip()
    out = text.splitlines()
    if r.returncode != 0 or not out or out[0] not in ("exists", "producer", "dead"):
        return "ssh-error"
    if out[0] == "exists":  # как у диспетчера (_host_probe): файл хода — готов при done>=total, прочее — достаточно наличия
        prog = D._progress_done("\n".join(out[1:])) if arg.endswith(".json") else None
        return "producer" if prog is False else "met"
    return out[0]


LINK_PROBES = [h.strip() for h in P.env("WATCH_LINK_PROBES", "1.1.1.1:443,8.8.8.8:53").split(",") if h.strip()]


def pc_link_up() -> bool:
    """Есть ли у самого ПК выход в сеть (TK-090 Д-8): TCP-connect к опорным адресам. Молчание ssh при упавшей связи ПК —
    не молчание машины, и ожидание снимать нельзя (обрыв 07.10 21:40–00:50 снял wait_for у 5 тикетов)."""
    for item in LINK_PROBES:
        host, _, port = item.rpartition(":")
        try:
            with socket.create_connection((host, int(port)), timeout=3):
                return True
        except (OSError, ValueError):
            continue
    return False


def _wait_target_state(tkt, probe) -> str:
    parsed = T.parse_wait_for(tkt.header.get("wait_for") or "")
    if parsed is None:
        return "invalid"
    if parsed[0] == "file":
        return "exists"
    if parsed[0] == "at":  # ожидание по времени: не зависло и не сирота до времени + допуск; позже диспетчер уже обязан был разбудить
        late = datetime.now().astimezone() - parsed[1] > timedelta(minutes=AT_GRACE_MIN)
        return "dead" if late else "exists"
    if parsed[0] == "ticket":
        return "exists" if (D.TICKETS_DIR / f"{parsed[1]}.md").exists() else "dead"
    if parsed[0] == "host":
        return probe(parsed[1], parsed[2], parsed[3])
    return "unknown"


def _to_judge(path, why: str, now, clear_wait: bool = True) -> None:
    """Повтор после пробуждения владельца: тикет — Судье (В-206; `next: judge`, статус in_progress), не `blocked`: на
    blocked диспетчер будит CEO. CEO узнаёт, только если Судья сам встанет (его `result blocked`)."""
    T.append_log(path, "watch", why + " — второй раз, ход Судьи (не CEO): реши, перезапускать ли и что с wait_for", now)
    T.write_header_updates(path, {"status": "in_progress", "next": "judge", **({"wait_for": "", "on_met": ""} if clear_wait else {})}, now)


def _wake_owner_job(path, tkt, ws: dict, spec: str, why: str, now, key: str = "job_wakes") -> None:
    """Задание из `job:` упало/исчезло: владелец тикета будится записью (хвост лога + причина), при повторе после
    пробуждения — Судья (`_to_judge`)."""
    acted = ws.setdefault(key, {})  # у каждой причины свой счётчик повторов: пробуждение по заданию не делает пустое ожидание «вторым разом»
    repeat = tkt.id in acted
    with T.ticket_lock(path):
        if repeat:
            _to_judge(path, why, now)
        else:
            T.append_log(path, "watch", why + " — ожидание снято, владелец будится: перезапусти задание или смени wait_for", now)
            T.write_header_updates(path, {"status": "in_progress", "wait_for": "", "on_met": ""}, now)
    acted[tkt.id] = spec


def _fetch_owners(owners_probe) -> dict:
    probe = owners_probe or (lambda alias: LW.fetch_owners(alias, D._ssh_cmd))
    res = {}
    for alias in sorted(D.WATCHED_ALIASES):
        got = probe(alias)
        if got is not None:
            res[alias] = got
    if res:
        try:
            LW.save_owners(D.DISPATCHER_DIR, res)
        except OSError:
            pass
    return res


def _mark_failed_seen(ws: dict, tid: str, owners: dict, extra: str = "") -> None:
    """Пробуждение владельца покрывает ВСЕ упавшие к этому часу задания тикета (и то, что поймала форма `job:`):
    старое упавшее задание не будит повторно и не ведёт к ложному blocked → CEO (Судья 08.10)."""
    seen = ws.setdefault("owner_failed_seen", [])
    for jid in [j["id"] for lst in owners.values() for j in lst if j["ticket"] == tid and j["state"] == "failed"] + ([extra] if extra else []):
        if jid not in seen:
            seen.append(jid)
    del seen[:-200]


def _resubmitted(mine: list, j: dict) -> bool:
    return any(o["id"] != j["id"] and o["unit"] == j["unit"] and o["state"] == "done" for o in mine)


def _covered_failure(mine: list, j: dict) -> bool:
    """Упавшее задание не повод будить владельца: у тикета есть живое задание (идёт/в очереди — та же волна, wait_for
    снимать рано, п.4) либо пересдача с тем же именем юнита (done — п.1). Снятое владельцем адаптер отдаёт как
    `done` (договор `RPV_JOB_STATE_CMD`/`RPV_JOB_OWNERS_CMD`, п.2) — до сюда не доходит."""
    return any(o["id"] != j["id"] and (o["state"] in ("running", "queued") or (o["unit"] == j["unit"] and o["state"] == "done"))
               for o in mine)


def _apply_owned_jobs(path, tkt, ws: dict, spec: str, st: str, owners: dict, now) -> str:
    """Задания, записанные на этот тикет (сопоставление адаптера), уточняют цель `wait_for` любой формы: идёт/в очереди —
    производитель есть (не «мёртвая цель»); упало (один раз на задание) — владелец будится с id задания."""
    mine = [j for lst in owners.values() for j in lst if j["ticket"] == tkt.id]
    seen = ws.setdefault("owner_failed_seen", [])
    for j in mine:
        if j["state"] == "failed" and j["id"] not in seen:
            if _resubmitted(mine, j):  # пересдано: упавшее закрыто навсегда
                seen.append(j["id"])
                continue
            if _covered_failure(mine, j):  # живой сосед: только отложить — кончится волна без пересдачи, упавшее разбудит
                continue
            _mark_failed_seen(ws, tkt.id, owners, j["id"])
            _wake_owner_job(path, tkt, ws, spec, f"сторож: задание {j['unit']} (id {j['id']}) тикета упало; wait_for `{spec}` не дождётся", now)
            return "woken"
    if st == "dead" and any(j["state"] in ("running", "queued") for j in mine):
        return "producer"
    return st


def _triage_job(path, tkt, parsed, ws: dict, now, job_probe, states: dict, owners: dict = None) -> bool:
    """Одна проверка `job:`; True — тикет «жив» (ждёт, не сирота). failed — действие сразу (падение однозначно);
    missing — после DEAD_WAIT_STRIKES подряд (задание могло ещё не записаться); ssh-error — как у прочих форм."""
    _, alias, jid = parsed
    spec = tkt.header["wait_for"].strip()
    st, tail = job_probe(alias, jid)
    dead = ws.setdefault("dead_wait", {})
    if st in ("running", "queued", "done"):
        states[f"{alias}:{jid}"] = st
        if st == "done":
            ws.setdefault("job_wakes", {}).pop(tkt.id, None)  # довели до конца — следующее падение снова только владельцу
        dead.pop(tkt.id, None)
        ws.setdefault("ssh_fail_wait", {}).pop(tkt.id, None)
        return True
    if st == "ssh-error":
        ssh_fail = ws.setdefault("ssh_fail_wait", {})
        ent = ssh_fail.get(tkt.id) or {}
        n = ent.get("n", 0) + 1 if ent.get("spec") == spec else 1
        ssh_fail[tkt.id] = {"spec": spec, "n": n}
        if n < SSH_FAIL_STRIKES:
            return True
        ssh_fail.pop(tkt.id, None)
        _wake_owner_job(path, tkt, ws, spec, f"сторож: состояние задания `{spec}` не проверить {n} проверок подряд (ssh/адаптер)", now)
        return False
    if st == "failed":
        mine = [o for lst in (owners or {}).values() for o in lst if o["ticket"] == tkt.id]
        me = next((o for o in mine if o["id"] == jid), {"id": jid, "unit": ""})
        if _covered_failure(mine, me):  # пересдача или живое задание того же тикета: wait_for не снимаем
            return True
        why = f"сторож: задание `{spec}` упало"
        why += f"\nХвост лога:\n{tail}" if tail else ""
        r = LW.reason(jid, tail)
        _wake_owner_job(path, tkt, ws, spec, why + (f"\nПричина (Haiku): {r}" if r else ""), now)
        return False
    ent = dead.get(tkt.id) or {}  # missing: задание не найдено
    n = ent.get("n", 0) + 1 if ent.get("spec") == spec else 1
    dead[tkt.id] = {"spec": spec, "n": n}
    if n < DEAD_WAIT_STRIKES:
        return True
    dead.pop(tkt.id, None)
    _wake_owner_job(path, tkt, ws, spec, f"сторож: задание `{spec}` не найдено на машине и никто его не производит ({n} проверки подряд)", now)
    return False


def _triage_ci_run(path, tkt, parsed, ws: dict, now, run_probe) -> bool:
    """`ci-run:`: идёт/завершён — жив (завершённый закроет диспетчер); 404 DEAD_WAIT_STRIKES подряд — прогона нет,
    производителя нет → владелец; не удалось спросить — не считается."""
    st = run_probe(parsed[1], parsed[2])
    dead = ws.setdefault("dead_wait", {})
    if st != "missing":
        dead.pop(tkt.id, None)
        return True
    spec = tkt.header["wait_for"].strip()
    ent = dead.get(tkt.id) or {}
    n = ent.get("n", 0) + 1 if ent.get("spec") == spec else 1
    dead[tkt.id] = {"spec": spec, "n": n}
    if n < DEAD_WAIT_STRIKES:
        return True
    dead.pop(tkt.id, None)
    _wake_owner_job(path, tkt, ws, spec, f"сторож: прогон CI `{spec}` не найден на GitHub и никто его не производит ({n} проверки подряд)", now)
    return False


def triage_waits(ws: dict, now, probe=probe_wait_target, job_probe=None, owners_probe=None, run_probe=None, link_up=pc_link_up) -> set:
    """waiting-тикеты: цель wait_for существует или её делает живой юнит/процесс — тикет «жив» (возвращаются его id —
    сторож не зовёт его сиротой). Цель мертва DEAD_WAIT_STRIKES проверок подряд → тикет в in_progress с пустым wait_for и
    записью — диспетчер будит владельца (resume), CEO не нужен; повторно та же цель → тикет Судье (`next: judge`).
    Молчание ssh при упавшей связи самого ПК (`link_up()` ложно) не считается: счётчик стоит, ожидание не снимается."""
    dead = ws.setdefault("dead_wait", {})
    ssh_fail = ws.setdefault("ssh_fail_wait", {})
    alive, seen, states, empty_seen = set(), set(), {}, set()
    if run_probe is None:
        import ci_watch
        run_probe = ci_watch.run_state
    job_probe = job_probe or (lambda alias, jid: LW.probe_job(alias, jid, D._ssh_cmd))
    owners = _fetch_owners(owners_probe)  # {алиас: [задания]}: общее сопоставление «задание → тикет» (и для табло)
    link = None  # связь ПК проверяется лениво, раз за проход
    for path in T.list_tickets(D.TICKETS_DIR):
        try:
            tkt = T.read_ticket(path)
        except Exception:
            continue
        if tkt.status != "waiting":
            continue
        if not (tkt.header.get("wait_for") or "").strip():  # waiting без условия и без next — ждать нечего, производителя нет
            empty = ws.setdefault("empty_wait", {})
            if (tkt.header.get("next") or "").strip() or tkt.id in D.load_state().get("active_runs", {}):
                empty.pop(tkt.id, None)  # next поставлен или роль по тикету сейчас работает (обычная приёмка: ci_watch снял условие, диспетчер запустил Судью) — производитель есть
                continue
            empty_seen.add(tkt.id)
            n = empty.get(tkt.id, 0) + 1
            empty[tkt.id] = n
            if n >= EMPTY_WAIT_STRIKES:
                empty.pop(tkt.id, None)
                _wake_owner_job(path, tkt, ws, "", f"сторож: тикет waiting без wait_for и без next {n} проверок подряд — ждать нечего", now, key="empty_wakes")
            continue
        spec = tkt.header["wait_for"].strip()
        parsed = T.parse_wait_for(spec)
        if parsed and parsed[0] == "job":
            if _triage_job(path, tkt, parsed, ws, now, job_probe, states, owners):
                alive.add(tkt.id)
            else:
                _mark_failed_seen(ws, tkt.id, owners, parsed[2])
            continue
        if parsed and parsed[0] == "ci-run":
            if _triage_ci_run(path, tkt, parsed, ws, now, run_probe):
                alive.add(tkt.id)
            continue
        st = _wait_target_state(tkt, probe)
        st = _apply_owned_jobs(path, tkt, ws, spec, st, owners, now)
        if st == "woken":
            continue
        if st == "ssh-error":  # ssh молчит: условие не проверить; N раз подряд — владельцу тикета, не CEO
            if link is None:
                link = link_up()
            if not link:  # обрыв связи ПК, а не машины: страйк не засчитываем, ожидание живо (Д-8)
                alive.add(tkt.id)
                continue
            ent = ssh_fail.get(tkt.id) or {}
            n = ent.get("n", 0) + 1 if ent.get("spec") == spec else 1
            ssh_fail[tkt.id] = {"spec": spec, "n": n}
            dead.pop(tkt.id, None)
            if n < SSH_FAIL_STRIKES:
                alive.add(tkt.id)
                continue
            with T.ticket_lock(path):
                T.append_log(path, "watch", f"сторож: ssh к машине из `{spec}` молчит {n} проверок подряд — условие "
                             "не проверить; ожидание снято, владелец будится: проверь машину и задание, поставь wait_for заново", now)
                T.write_header_updates(path, {"status": "in_progress", "wait_for": "", "on_met": ""}, now)
            ssh_fail.pop(tkt.id, None)
            continue
        ssh_fail.pop(tkt.id, None)
        met = ws.setdefault("met_since", {})
        if st == "met":  # условие выполнено; диспетчер обязан разбудить за пару сверок — не разбудил → владелец
            ent = met.get(tkt.id)
            if not ent or ent.get("spec") != spec:
                ent = met[tkt.id] = {"spec": spec, "ts": T.now_iso(now)}
            if now - T.parse_dt(ent["ts"]) > timedelta(minutes=MET_GRACE_MIN):
                met.pop(tkt.id, None)
                with T.ticket_lock(path):
                    T.append_log(path, "watch", f"сторож: условие `{spec}` выполнено, но диспетчер не разбудил за {MET_GRACE_MIN} мин "
                                 "(сбой проверки/событий) — ожидание снято, владелец будится: продолжай с результата", now)
                    T.write_header_updates(path, {"status": "in_progress", "wait_for": "", "on_met": ""}, now)
                continue
            alive.add(tkt.id)
            dead.pop(tkt.id, None)
            continue
        met.pop(tkt.id, None)
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
                _to_judge(path, why, now, clear_wait=False)
            else:
                T.append_log(path, "watch", why + " — ожидание снято, владелец будится: перезапусти задание или смени wait_for", now)
                T.write_header_updates(path, {"status": "in_progress", "wait_for": "", "on_met": ""}, now)
        dead[tkt.id] = {"spec": spec, "n": 0, "acted": ent.get("acted", []) + [spec]}
    if states or ws.get("job_states_written"):  # состояние для диспетчера (check_wait_for); пустое тоже пишем один раз
        try:
            LW.save_states(D.DISPATCHER_DIR, states)
        except OSError:
            pass
        ws["job_states_written"] = bool(states)
    for tid in list(dead):
        if tid not in seen and tid not in alive:
            dead.pop(tid, None)
    for tid in list(ssh_fail):
        if tid not in alive:
            ssh_fail.pop(tid, None)
    met = ws.get("met_since", {})
    for tid in list(met):
        if tid not in alive:
            met.pop(tid, None)
    empty = ws.get("empty_wait", {})
    for tid in list(empty):
        if tid not in empty_seen:
            empty.pop(tid, None)
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


STRAY_WAKE_REPEAT_HOURS = float(P.env("WATCH_STRAY_WAKE_REPEAT_HOURS", "2"))  # повтор сигнала о том же прогоне: замер идёт часами, раз в 2 ч напомнить, не спамить


def check_strays(ws: dict, now, probe=None) -> list:
    """Прогон мимо планировщика поверх чужого задания (замер портит чужую волну): адаптер проекта `RPV_STRAY_CMD` даёт
    строки «тикет<TAB>описание»; владельца тикета-нарушителя будим записью, нет тикета — строка CEO. Один сигнал на
    (тикет, описание) за STRAY_WAKE_REPEAT_HOURS. Адаптер не задан — тихо ничего."""
    probe = probe or (lambda alias: LW.fetch_strays(alias, D._ssh_cmd))
    seen, out = ws.setdefault("stray_seen", {}), []
    for alias in sorted(D.WATCHED_ALIASES):
        for tid, desc in probe(alias) or []:
            key = f"{tid or '?'}|{desc}"
            prev = seen.get(key)
            if prev:
                try:
                    if now - T.parse_dt(prev) < timedelta(hours=STRAY_WAKE_REPEAT_HOURS):
                        continue
                except ValueError:
                    pass
            seen[key] = T.now_iso(now)
            why = f"сторож: на {alias} идёт прогон мимо планировщика поверх чужого задания — {desc[:200]}"
            path = D.TICKETS_DIR / f"{tid}.md"
            if tid and path.exists():
                try:
                    with T.ticket_lock(path):
                        tkt = T.read_ticket(path)
                        if tkt.owner in ("researcher", "engineer", "judge"):
                            T.append_log(path, "watch", why + ". Замер искажён: останови его и подай через планировщик.", now)
                            T.write_header_updates(path, {"next": tkt.owner}, stamp_updated=False)
                            continue
                except Exception as e:
                    print(f"[watch] stray: не разбудил {tid}: {type(e).__name__}: {e}", file=sys.stderr)
            out.append(Finding("stray-run", tid or "?", why + " — владельца определить не удалось, решение за CEO"))
    for key in [k for k, v in seen.items() if _stale(v, now)]:
        seen.pop(key, None)
    return out


def _stale(ts: str, now) -> bool:
    try:
        return now - T.parse_dt(ts) > timedelta(hours=24)
    except ValueError:
        return True


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
    запись watch и `next: judge` (Судья, не CEO). Один раз на последний запуск (ws['stall'])."""
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
            T.append_log(path, "watch", f"сторож: {why} (по runs.log) — ход Судьи (не CEO): реши, что с тикетом", now)
            T.write_header_updates(path, {"next": "judge"}, now)
        done[tkt.id] = T.now_iso(rows[-1][0])
        acted.append(tkt.id)
    return acted


def check_host_alerts(run=subprocess.run) -> int:
    """№17 аудита: адаптер проекта `RPV_HOST_ALERTS_CMD` печатает новые тревоги хоста по строке на тревогу; каждая
    строка — «ждёт вас» на Диспетчерской (ask.new_notice, тот же непринятый текст не дублируется). Нет настройки — молчим.
    Команда сама отвечает за «только новое»: принятая строка при повторной печати встанет снова. Возвращает число строк."""
    cmd = os.environ.get("RPV_HOST_ALERTS_CMD", "").strip()
    if not cmd:
        return 0
    import ask
    try:
        r = run(cmd, shell=True, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    except (OSError, subprocess.SubprocessError):
        return 0
    lines = [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()]
    for ln in lines:
        ask.new_notice(D.PROJECT_ROOT, "host", ln[:300])
    return len(lines)


def collect_findings(now, alive_waits=()) -> list:
    findings = []
    findings += check_blocked_and_needs_owner(now)
    findings += check_orphan_tickets(now, alive_waits)
    findings += check_no_progress_view(now)
    findings += check_stale_plan(now)
    check_host_alerts()
    return findings


# --- дедуп (вид, ключ) с повтором раз в WATCH_DEDUP_REPEAT_HOURS, пока не снято (п.2г) -----------


def _repeat_hours(kind: str) -> float:
    """Через сколько часов та же находка напоминается: orphan/blocked/needs_owner — раз в сутки (v2), остальные —
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


_PLAN_PY = str(Path(__file__).resolve().with_name("plan.py"))


def _wake_for_plan(tid: str, now, stale: bool = False) -> None:
    """no-plan: запись в лог тикета и `next: <владелец>` — исполнитель запишет план (plan.py set, 3–6 шагов)."""
    path = D.TICKETS_DIR / f"{tid}.md"
    try:
        with T.ticket_lock(path):
            tkt = T.read_ticket(path)
            if tkt.owner not in ("researcher", "engineer", "judge"):
                return
            if stale:
                T.append_log(path, "watch", "Сторож (без LLM): в логе есть запись роли, а шаги на табло не менялись "
                             f"> {NO_PLAN_MINUTES:.0f} мин. Обнови по факту: `python " + _PLAN_PY + " step " + tid +
                             " <N> <run|done|todo>` (wait — только когда ждём владельца), затем продолжай работу.")
            else:
                T.append_log(path, "watch", "Сторож (без LLM): задача в работе дольше "
                             f"{NO_PLAN_MINUTES:.0f} мин, а плана шагов на табло нет — табло не может показать процент. "
                             "Запиши план: `python " + _PLAN_PY + " set " + tid + " ...` (3–6 шагов по-людски, "
                             "справка — `plan.py --help`), затем продолжай работу.")
            T.write_header_updates(path, {"next": tkt.owner}, stamp_updated=False)
    except Exception as e:
        print(f"[watch] no-plan: не разбудил {tid}: {type(e).__name__}: {e}", file=sys.stderr)


def _remind_plan_waiting(tid: str) -> None:
    """waiting + застывший план: не будим (запуск впустую) — напоминание в лог, следующий запуск роли обновит шаги."""
    path = D.TICKETS_DIR / f"{tid}.md"
    try:
        T.append_log(path, "watch", "Сторож (без LLM): запись роли новее плана, а шаги на табло не менялись "
                     f"> {NO_PLAN_MINUTES:.0f} мин. В следующем запуске обнови по факту: `python " + _PLAN_PY + " step "
                     + tid + " <N> <run|done|todo>`.")
    except Exception as e:
        print(f"[watch] plan-stale-waiting: не записал {tid}: {type(e).__name__}: {e}", file=sys.stderr)


def notify_findings(findings: list, ws: dict, now) -> list:
    """Возвращает находки, по которым реально написали (для тестов). Дедуп — (kind, key) с повтором раз в
    _repeat_hours(kind)."""
    notified = ws.setdefault("notified", {})
    current_keys = set()
    posted = []
    for f in findings:
        marker = f"{f.kind}:{f.key}"
        current_keys.add(marker)
        entry = notified.get(marker) or {}
        if isinstance(entry, str):  # прежний формат watch-state.json (v1.4): значение — только время
            entry = {"ts": entry}
        last_ts = entry.get("ts")
        time_elapsed = last_ts is None or (now - T.parse_dt(last_ts)).total_seconds() >= (
            _repeat_hours(f.kind) * 3600)
        if time_elapsed:
            if f.kind not in OWNER_ONLY_KINDS:  # план шагов — дело владельца тикета, действия CEO тут нет (TK-079 п.3)
                D.append_ceo_inbox("*", f"watch-{f.kind}", f.message, now)
            if f.kind == "no-plan":
                _wake_for_plan(f.key, now)
            elif f.kind == "plan-stale":
                _wake_for_plan(f.key, now, stale=True)
            elif f.kind == "plan-stale-waiting":
                _remind_plan_waiting(f.key)
            notified[marker] = {"ts": T.now_iso(now)}
            posted.append(f)
    # снятые находки — забыть, чтобы будущее повторение не ждало старого окна дедупа
    for marker in list(notified):
        if marker not in current_keys:
            notified.pop(marker, None)
    return posted


def write_heartbeat(now, findings_count: int) -> None:
    WATCH_HEARTBEAT_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = WATCH_HEARTBEAT_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"ts": T.now_iso(now), "findings": findings_count}, ensure_ascii=False),
                    encoding="utf-8")
    tmp.replace(WATCH_HEARTBEAT_FILE)


def run_once(now=None, probes=True) -> list:
    now = now or datetime.now().astimezone()
    ws = load_watch_state()
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
        if probes:  # тесты выключают — боевой сервер не трогают
            server_findings = check_server_idle(ws, now)
    except Exception as e:
        print(f"[watch] check_server_idle: {type(e).__name__}: {e}", file=sys.stderr)
    try:
        if probes:
            server_findings += check_strays(ws, now)
    except Exception as e:
        print(f"[watch] check_strays: {type(e).__name__}: {e}", file=sys.stderr)
    findings = collect_findings(now, alive_waits=alive_waits) + server_findings
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
