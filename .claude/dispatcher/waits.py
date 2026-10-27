"""Ожидания `wait_for` (host:/file:/ticket:/ci/merged/job), сверка машин одним ssh, строка исхода в jobs.jsonl и слепота
диспетчера (TK-217) — вынесено из dispatch.py (TK-011, ось 4). Пути проекта — через `Where` от вызывающего:
модуль к dispatch не обращается (тесты ставят пути прямо на dispatch)."""
from __future__ import annotations

import json
import re
import shlex
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

import blind
import project_paths as CFG
import hide
import project as P
import ticket as T


class Where(NamedTuple):
    """Три пути проекта, которые читают ожидания: корень, каталог состояния диспетчера, каталог тикетов."""
    root: Path
    dispatcher_dir: Path
    tickets_dir: Path


def project_where() -> Where:
    return Where(CFG.PROJECT_ROOT, CFG.DISPATCHER_DIR, CFG.TICKETS_DIR)


# --- wait_for ---------------------------------------------------------------------------------

def check_wait_for(spec: str, where: Where) -> bool:
    """Условие `wait_for` выполнено? Формы — `ticket.parse_wait_for` (README, «v4»). Незнакомая форма (в т.ч. свободный
    текст и старое `mention`) → False, но тихо не остаётся: `notify_wait_for_problem` пишет строку в ceo-inbox."""
    parsed = T.parse_wait_for(spec)
    if parsed is None:
        return False
    if parsed[0] == "file":
        path = Path(parsed[1])
        if not path.is_absolute():
            path = where.root / path
        return path.exists()
    if parsed[0] == "at":
        return datetime.now().astimezone() >= parsed[1]
    if parsed[0] == "ticket":
        return _other_ticket_done(parsed[1], where.tickets_dir)
    if parsed[0] == "ci":
        import ci_watch
        return ci_watch.ci_done(parsed[1], parsed[2])
    if parsed[0] == "ci-run":
        import ci_watch
        return ci_watch.run_done(parsed[1], parsed[2])
    if parsed[0] == "merged":
        import merge_rule
        return merge_rule.merged_done(parsed[1], parsed[2])
    if parsed[0] == "job":  # состояние пишет сторож жизни (watch.py → lifewatch.py), своего ssh нет
        import lifewatch
        return lifewatch.job_done(where.dispatcher_dir, parsed[1], parsed[2])
    _, alias, what, arg = parsed
    return _host_wait_met(alias, what, arg)


def _other_ticket_done(other_id: str, tickets_dir: Path) -> bool:
    """`wait_for: ticket:<ID>` — ждём, пока другой тикет дойдёт до status: done (судья 27.09, «можно потом»:
    зависимости T-XX жили только прозой TASKS.md, диспетчер их не видел)."""
    other_path = tickets_dir / f"{other_id}.md"
    if not other_path.exists():
        return False
    try:
        return T.read_ticket(other_path).status == "done"
    except Exception:
        return False


def _remote_test_arg(remote_path: str) -> str:
    """`test -e` аргумент: `~`/`~/...` — без кавычек вокруг тильды, иначе remote-шелл не раскроет её
    в $HOME (shlex.quote экранирует и тильду тоже — поймано боевым вызовом v1.1, 27.09)."""
    remote_path = remote_path.strip()
    if remote_path == "~":
        return "~"
    if remote_path.startswith("~/"):
        rest = remote_path[1:]  # оставляем ведущий '~' сырым, остальное — безопасно экранируем
        return "~" + shlex.quote(rest)
    return shlex.quote(remote_path)


_WAIT_CACHE = {}  # (алиас, "path"|"unit", арг) -> (time.time() отметка, результат) — см. _host_wait_met
_WAIT_RC = {}     # (алиас, "path", арг) -> текст файла итога задания (`*.rc`, tools/jobrun.sh), когда путь появился — см. wait_rc
WAIT_CHECK_CACHE_S = 60.0  # синхронный путь (--once, тесты): ssh-проверка одного условия не чаще раза в столько секунд
WAIT_ERR_EVERY_S = 600.0  # ошибка ssh по одному условию — строка в dispatch.err.log не чаще раза в 10 мин
_WAIT_ERR_LAST = {}  # ключ условия -> time.time() последней строки
_UNIT_RUNNING = ("active", "activating", "reloading", "deactivating", "refreshing")

# Ожидание `host:<алиас>:…` — ssh к алиасу из ~/.ssh/config (любая машина, плагин своих настроек машин не держит). Боевой цикл
# (WAIT_ASYNC) ssh в тике не трогает: первую проверку нового условия и сверку всех ждущих раз в WAIT_RECON_S (один ssh на
# машину) делает поток _wait_poller, поэтому конец задания виден не позже периода сверки.
WAIT_ASYNC = False  # True ставит main() для боевого цикла; тесты и --once — синхронный путь
WAIT_POLL_S = 300.0
_WAIT_WATCH = set()  # ключи (алиас, what, арг), которые ждут тикеты — их опрашивает _wait_poller
_WAIT_NEW = threading.Event()
WAIT_RECON_S = 300.0  # сверка всех ждущих host:… одним ssh на машину
WATCHED_ALIASES = set()  # без rpv.json машин нет; тесты подменяют на модуле


def watched_aliases() -> set:
    """Машины, где сторож гоняет адаптеры заданий (`job_owners_cmd`, `stray_cmd`): `watched_aliases` из
    `.claude/rpv.json` (список ssh-алиасов), иначе WATCHED_ALIASES (пусто)."""
    v = P.setting("watched_aliases")
    return {str(a).strip() for a in v if str(a).strip()} if isinstance(v, list) else WATCHED_ALIASES
# TK-217: детектор «диспетчер слеп» — недоступность машины с ожиданиями дольше порога → тревога вне этой машины (вердикт Судьи 12.10)
BLIND = blind.Tracker(blind.ALERT_S, blind.REPEAT_S)
WAIT_RECON_BLIND_S = 30.0  # пока цель недоступна — сверка раз в 30 с (+ссылка таймаут 20 с), чтобы после возврата связи проснуться ≤ 60 с (стенд 12.10: 45 с таймаут + 60 с период дали 150 с)
_RECON_LAST = 0.0


def _wait_poller() -> None:
    """Боевой цикл: ssh вне тика — первая проверка нового условия из _WAIT_WATCH и сверка раз в WAIT_RECON_S."""
    global _RECON_LAST
    while True:
        for ckey in list(_WAIT_WATCH):
            if ckey in _WAIT_CACHE:
                continue
            try:
                _host_probe(*ckey)
            except Exception as e:
                _wait_err(f"poller:{ckey}", f"{type(e).__name__}: {e}")
        if _WAIT_WATCH and time.time() - _RECON_LAST >= (WAIT_RECON_BLIND_S if BLIND.any_down() else WAIT_RECON_S):
            _RECON_LAST = time.time()
            try:
                _reconcile()
            except Exception as e:
                _wait_err("reconcile", f"{type(e).__name__}: {e}")
        _WAIT_NEW.wait(min(WAIT_POLL_S, WAIT_RECON_BLIND_S if BLIND.any_down() else WAIT_RECON_S))
        _WAIT_NEW.clear()


def _wait_err(key: str, msg: str) -> None:
    now_ts = time.time()
    last = _WAIT_ERR_LAST.get(key)
    if last is not None and (now_ts - last) < WAIT_ERR_EVERY_S:
        return
    _WAIT_ERR_LAST[key] = now_ts
    print(f"[dispatch] {T.now_iso()} wait_for {key}: {msg}", file=sys.stderr, flush=True)


def _ssh_cmd(alias: str, remote_cmd: str) -> list:
    """ssh к алиасу из ~/.ssh/config: пользователь, ключ, known_hosts, прыжковый хост — там же. Алиас — только вида ssh-имени
    (ticket.host_alias_ok): имя с дефисом впереди ssh принял бы за свою опцию."""
    if not T.host_alias_ok(alias):
        raise RuntimeError(f"алиас ssh {alias!r} не годится (буквы, цифры, . _ -; не с дефиса)")
    return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", alias, remote_cmd]


def _progress_done(text: str):
    """Содержимое `.json` — файл хода (числа `done`/`total`)? → done >= total (и total > 0, чтобы
    не сработать на заготовке с нулями); иначе None — это не файл хода, достаточно того, что он существует."""
    try:
        obj = json.loads(text)
        done, total = obj["done"], obj["total"]
        if isinstance(done, bool) or isinstance(total, bool):
            return None
        done, total = float(done), float(total)
    except (ValueError, KeyError, TypeError):
        return None
    return total > 0 and done >= total


def _reads_body(arg: str) -> bool:
    """Содержимое пути нужно диспетчеру: файл хода (`.json`) и файл итога задания (`.rc`) читаются, прочее — `test -e`."""
    return arg.endswith((".json", ".rc"))


def _path_met(key: tuple, body: str) -> bool:
    """Путь по ssh есть и прочитан: файл хода — готов при done >= total; файл итога `.rc` — готов всегда, а текст запоминается
    (wait_rc) — роль увидит, чем кончилось задание; прочий путь — достаточно того, что он есть."""
    if key[2].endswith(".rc"):
        _WAIT_RC[key] = " ".join(body.split())[:300]
        return True
    progress = _progress_done(body) if key[2].endswith(".json") else None
    return True if progress is None else progress


def _host_wait_met(alias: str, what: str, arg: str) -> bool:
    """`host:<алиас>:<путь>` — путь на машине существует (`.json` с done/total — done >= total; `.rc` — файл итога jobrun.sh);
    `host:<алиас>:unit:<имя>` — юнит не работает (`systemctl is-active` ≠ active: задание закончилось или упало).
    Судья 27.09 («можно потом»): без кэша ssh дёргается на каждый ждущий тикет каждые 15 с — результат на
    WAIT_CHECK_CACHE_S; ошибка ssh (код 255, таймаут) = «не выполнено» + строка в dispatch.err.log раз в 10 мин."""
    ckey = (alias, what, arg)
    if WAIT_ASYNC:  # основной поток ssh не трогает: проверку делает _wait_poller (TK-055)
        if ckey not in _WAIT_WATCH:
            _WAIT_WATCH.add(ckey)
            _WAIT_NEW.set()  # первую проверку нового условия поток делает сразу, не через WAIT_POLL_S
        return (_WAIT_CACHE.get(ckey) or (0, False))[1]
    cached = _WAIT_CACHE.get(ckey)
    now_ts = time.time()
    if cached and (now_ts - cached[0]) < WAIT_CHECK_CACHE_S:
        return cached[1]
    return _host_probe(alias, what, arg)


def _host_probe(alias: str, what: str, arg: str) -> bool:
    ckey = (alias, what, arg)
    now_ts = time.time()
    if what == "unit":
        remote = f"systemctl is-active {shlex.quote(arg)}"
    elif _reads_body(arg):
        remote = f"cat {_remote_test_arg(arg)}"
    else:
        remote = f"test -e {_remote_test_arg(arg)}"
    label = f"host:{alias}:{'unit:' if what == 'unit' else ''}{arg}"
    result = False
    try:
        r = hide.run(_ssh_cmd(alias, remote), capture_output=True, timeout=15)
        _blind_note(alias, r.returncode != 255)
        out = (getattr(r, "stdout", b"") or b"").decode("utf-8", "replace")
        if what == "unit":
            state = (out.strip().splitlines() or [""])[0]
            if r.returncode == 255 or not state:
                _wait_err(label, f"ssh: код {r.returncode}, {_ssh_stderr(r)}")
            else:
                result = state not in _UNIT_RUNNING
        elif r.returncode == 0:
            result = _path_met(ckey, out)
        elif r.returncode != 1:  # 1 — файла нет (штатно); остальное (255…) — сбой ssh/хоста
            _wait_err(label, f"ssh: код {r.returncode}, {_ssh_stderr(r)}")
    except Exception as e:
        _blind_note(alias, False)
        _wait_err(label, f"ssh: {type(e).__name__}: {e}")
    _WAIT_CACHE[ckey] = (now_ts, result)
    return result


def _recon_script(keys: list) -> str:
    parts = []
    for i, (alias, what, arg) in enumerate(keys):
        parts.append(f"echo @@{i}")
        if what == "unit":
            parts.append(f"systemctl is-active {shlex.quote(arg)} 2>&1 | head -1; echo '@@rc 0'")
        else:
            parts.append(f"{'cat' if _reads_body(arg) else 'test -e'} {_remote_test_arg(arg)} 2>/dev/null; echo \"@@rc $?\"")
    return "; ".join(parts)


def _parse_recon(out: str, n: int) -> dict:
    """Ответ _recon_script → {i: {"rc": int|None, "body": str}}."""
    res, cur = {}, None
    for line in out.splitlines():
        line = line.rstrip("\r")
        if line.startswith("@@") and line[2:].isdigit() and int(line[2:]) < n:
            cur = int(line[2:])
            res[cur] = {"rc": None, "body": []}
        elif cur is not None and line.startswith("@@rc "):
            try:
                res[cur]["rc"] = int(line[5:])
            except ValueError:
                pass
        elif cur is not None:
            res[cur]["body"].append(line)
    for v in res.values():
        v["body"] = "\n".join(v["body"])
    return res


def _recon_result(alias: str, what: str, arg: str, v: dict):
    """Ответ сверки по одному ключу → выполнено ли условие; None — ответ неясен, кэш не трогаем."""
    if what == "unit":
        state = (v["body"].strip().splitlines() or [""])[0]
        return state not in _UNIT_RUNNING if state else None
    if v["rc"] == 0:
        return _path_met((alias, what, arg), v["body"])
    return False if v["rc"] == 1 else None


def _reconcile() -> None:
    """Раз в WAIT_RECON_S один ssh на машину проверяет ВСЕ ждущие host:…: пропущенный конец задания стоит не дороже одного шага
    сверки. Машины — параллельно (TK-217: таймаут одной не задерживает другую, иначе 60-секундная сверка при слепоте не держится)."""
    by_alias = {}
    for k in list(_WAIT_WATCH):
        by_alias.setdefault(k[0], []).append(k)
    threads = [threading.Thread(target=_reconcile_alias, args=(alias, sorted(keys)), daemon=True)
               for alias, keys in by_alias.items()]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)


def _reconcile_alias(alias: str, keys: list) -> None:
    try:
        r = hide.run(_ssh_cmd(alias, _recon_script(keys)), capture_output=True, timeout=20 if BLIND.any_down() else 45)
    except Exception as e:
        _blind_note(alias, False)
        _wait_err(f"reconcile:{alias}", f"ssh: {type(e).__name__}: {e}")
        return
    out = (getattr(r, "stdout", b"") or b"").decode("utf-8", "replace")
    if r.returncode == 255 or not out.strip():
        _blind_note(alias, False)
        _wait_err(f"reconcile:{alias}", f"ssh: код {r.returncode}, {_ssh_stderr(r)}")
        return
    _blind_note(alias, True)
    now_ts = time.time()
    for i, v in _parse_recon(out, len(keys)).items():
        if v["rc"] is None:
            continue
        _, what, arg = keys[i]
        result = _recon_result(alias, what, arg, v)
        if result is None:
            continue
        _WAIT_CACHE[keys[i]] = (now_ts, result)


# --- итоги ожиданий внешних заданий (CP6, критерий 12) ----------------------------------------------

JOBS_LOG_NAME = "jobs.jsonl"  # строка на исход ожидания: {"ts", "ticket", "wait_for", "outcome": "met"|"overdue", "rc": текст файла итога | null}


def wait_rc(spec: str) -> str:
    """Текст файла итога задания (`host:<алиас>:<путь>.rc`), если диспетчер его прочитал; иначе пусто."""
    parsed = T.parse_wait_for(spec)
    if not parsed or parsed[0] != "host" or parsed[2] != "path":
        return ""
    return _WAIT_RC.get((parsed[1], "path", parsed[3]), "")


def _rc_failed(rc: str) -> bool:
    """Файл итога (`SERVICE_RESULT=… EXIT_STATUS=…`) говорит о неудаче: результат не success или код не 0. Чужой вид текста — нет."""
    kv = dict(re.findall(r"(\w+)=(\S*)", rc))
    return kv.get("SERVICE_RESULT", "success") != "success" or kv.get("EXIT_STATUS", "0") not in ("0", "")


def record_job_outcome(tid: str, spec: str, outcome: str, rc: str = "", now=None, *, where: Where) -> bool:
    """Строка в jobs.jsonl на исход ожидания внешнего задания (ежедневный отчёт считает по ней «кончилось / потеряно»):
    `met` — условие закрылось, владелец будится; `overdue` — срок `--by` вышел (+15 мин), сторож разбудил владельца.
    Одна строка на (тикет, wait_for, исход): True — записано сейчас, False — такая уже есть."""
    path = where.dispatcher_dir / JOBS_LOG_NAME
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if (row.get("ticket"), row.get("wait_for"), row.get("outcome")) == (tid, spec, outcome):
                return False
    except OSError:
        pass
    row = {"ts": T.now_iso(now), "ticket": tid, "wait_for": spec, "outcome": outcome, "rc": rc or None}
    try:
        where.dispatcher_dir.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError as e:  # счёт не должен ронять тик
        print(f"[dispatch] {JOBS_LOG_NAME}: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
    return True


def note_wait_met(path: Path, tkt: T.Ticket, now, where: Where) -> T.Ticket:
    """waiting с условием внешнего задания (host:, file:, job:) закрылось: исход `met` в jobs.jsonl и, если машина отдала файл
    итога, запись в лог тикета — роль, проснувшись, видит, чем кончилось задание. Свежий тикет, если запись добавлена."""
    spec = (tkt.header.get("wait_for") or "").strip()
    if not T.wait_needs_by(spec) or not check_wait_for(spec, where):
        return tkt
    rc = wait_rc(spec)
    if not record_job_outcome(tkt.id, spec, "met", rc, now, where=where) or not rc:
        return tkt
    T.append_log(path, "dispatcher", f"ожидание `{spec}` закончилось; итог задания: {rc}"
                 + (" — задание НЕ успешно, разберись до продолжения" if _rc_failed(rc) else ""), now=now)
    return T.read_ticket(path)


# --- слепота (TK-217) --------------------------------------------------------------------------

def _blind_note(alias: str, ok: bool) -> None:
    BLIND.record(f"ssh:{alias}", ok, time.time())


def _blind_sound() -> None:
    try:
        import winsound
        for _ in range(3):
            winsound.Beep(1000, 400)
    except Exception:
        pass  # не Windows/нет звука — остаются файл и ceo-wake.log


def blind_tick(where: Where, mech_log) -> None:
    """Раз в тик: тревога по каждой цели отдельно (ALERT-BLIND.txt + звук + ceo-wake.log). Канал не использует ни одна из машин
    с ожиданиями. Возврат цели — строка «зрение вернулось» и немедленная сверка."""
    global _RECON_LAST
    now = time.time()
    active = {f"ssh:{k[0]}" for k in list(_WAIT_WATCH)}
    acts = BLIND.evaluate(now, active)
    for act, tgt, secs in acts:
        text = (f"диспетчер слеп: {tgt} недоступна {secs} с" if act != "recovered"
                else f"зрение вернулось: {tgt} снова отвечает (простой {secs} с)")
        mech_log("*", "blind", text)  # CP5: механика — в лог; стоит работа — скажет одна тревога «работа стоит»
        if act == "recovered":
            _RECON_LAST = 0.0  # сверка сразу
            _WAIT_NEW.set()
    down = BLIND.alerted_targets()  # файл синхронен набору каждый тик: цель ушла из active — файл снимается
    path = where.dispatcher_dir / "ALERT-BLIND.txt"
    try:
        if down:
            lines = [f"{t}: недоступна с {T.now_iso(datetime.fromtimestamp(since).astimezone())}" for t, since in sorted(down.items())]
            waits = ", ".join(sorted(t.id for t in map(T.read_ticket, T.list_tickets(where.tickets_dir)) if t.status == "waiting"))
            path.write_text("ДИСПЕТЧЕР СЛЕП (TK-217)\n" + "\n".join(lines) + f"\nждут (waiting): {waits or '—'}\n", encoding="utf-8")
        elif path.exists():
            path.unlink()
    except OSError:
        pass
    if any(a in ("alert", "repeat") for a, _, _ in acts):
        threading.Thread(target=_blind_sound, daemon=True, name="blind-sound").start()


def _ssh_stderr(r) -> str:
    return ((getattr(r, "stderr", b"") or b"").decode("utf-8", "replace").strip().splitlines() or ["—"])[-1][:200]


