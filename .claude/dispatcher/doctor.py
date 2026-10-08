"""/rpv-doctor — здоровье команды одной таблицей (TK-076 п.3): диспетчер, сторож, присмотр ОС, шина, очередь, ошибки за сутки.

    python doctor.py [--project P] [--json]      код возврата 0 — нет FAIL, 1 — есть FAIL, 2 — проект не найден
Только читает: ничего не запускает и не пишет (шина — GET /health и /stats)."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bus"))
import project as P  # noqa: E402
import start as S  # noqa: E402
import hide  # noqa: E402
import supervise as SV  # noqa: E402
import ticket as T  # noqa: E402
import worktree_hygiene as WH  # noqa: E402

OK, WARN, FAIL, OFF = "OK", "WARN", "FAIL", "—"
DAY = 86400


def _age(s: float | None) -> str:
    if s is None:
        return "нет"
    return f"{s:.0f} с" if s < 120 else f"{s / 60:.0f} мин" if s < 7200 else f"{s / 3600:.1f} ч"


def check_service(name: str, state_dir: Path, now: float) -> tuple:
    beat, field = SV.BEATS[name]
    pid = S.read_pid(state_dir / f"{name}.pid")
    alive = S.is_ours(pid, f"{name}.py")
    age = SV.heartbeat_age(state_dir / beat, field, now)
    if not alive:
        return name, FAIL, f"не запущен (pid-файл: {pid or 'нет'}); сердцебиение {_age(age)}"
    if age is None or age > SV.STALE_S:
        return name, FAIL, f"pid {pid} жив, сердцебиение {_age(age)} (> {SV.STALE_S:.0f} с) — завис"
    return name, OK, f"pid {pid}, сердцебиение {_age(age)} назад"


def scheduler_installed(project: Path, run=hide.run) -> bool | None:
    """Стоит ли присмотр ОС (None — не смогли проверить)."""
    name = SV.task_name(project)
    try:
        if os.name == "nt":
            cmd = ["schtasks", "/Query", "/TN", name]
        elif sys.platform == "darwin":
            cmd = ["launchctl", "list", f"dev.rpv.{name}"]
        else:
            cmd = ["systemctl", "--user", "is-enabled", f"{name}.timer"]
        return run(cmd, capture_output=True, timeout=15).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return None


def check_supervise(project: Path, state_dir: Path, now: float, installed=scheduler_installed) -> tuple:
    inst = installed(project)
    log = state_dir / "supervise.log"
    acts = _log_lines_since(log, now - DAY)
    tail = f"; перезапусков за сутки: {len(acts)}"
    if inst is False:
        return "присмотр ОС", WARN, "не установлен — `python supervise.py --install`" + tail
    if inst is None:
        return "присмотр ОС", WARN, "не смог проверить планировщик" + tail
    return "присмотр ОС", OK, "планировщик установлен" + tail


def _log_lines_since(path: Path, since: float) -> list:
    out = []
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                if datetime.fromisoformat(line.split(" ", 1)[0]).timestamp() >= since:
                    out.append(line)
            except ValueError:
                continue
    except OSError:
        pass
    return out


def check_bus(request=None, state_url: str | None = None) -> tuple:
    import busclient as B
    url, tok = B.config()
    url = state_url if state_url is not None else url  # адрес, с которым живёт диспетчер (state.json), главнее оболочки
    if not url:
        return "шина", OFF, "выключена (RPV_BUS_URL не задан) — сигналы идут файлами"
    if request is None:
        def request(path, _url=url, _tok=tok):  # B.request берёт адрес оболочки — опрашиваем именно адрес диспетчера
            import json as _j
            import urllib.request as _u
            req = _u.Request(_url + path, headers={"Authorization": "Bearer " + _tok})
            with _u.urlopen(req, timeout=5) as r:
                return _j.loads(r.read())
    try:
        h = request("/stats")
    except Exception as e:  # сеть/401/таймаут — сами по себе диагноз
        return "шина", FAIL, f"{url}: недоступна ({type(e).__name__}: {e})"
    if not isinstance(h, dict) or not h.get("ok"):
        return "шина", FAIL, f"{url}: ответ {str(h)[:80]}"
    q = h.get("queues") or {}
    blocked = h.get("blocked") or {}
    msg = f"{url}: ok, событий {h.get('last_seq')}, не доставлено {sum(q.values())}, блоков {len(blocked)}"
    return "шина", (WARN if blocked else OK), msg


def check_queue(project: Path, state_dir: Path) -> list:
    tickets = []
    for p in sorted((project / ".claude" / "tickets").glob("*.md")):
        try:
            tickets.append(T.read_ticket(p))
        except Exception:
            continue
    c = Counter(t.status for t in tickets)
    try:
        active = json.loads((state_dir / "state.json").read_text(encoding="utf-8")).get("active_runs") or {}
    except (OSError, ValueError):
        active = {}
    rows = [("очередь", OK, ", ".join(f"{k or '?'}: {v}" for k, v in sorted(c.items())) or "тикетов нет"),
            ("запуски сейчас", OK, f"{len(active)}: {', '.join(sorted(active)) or '—'}")]
    stuck = [t.id for t in tickets if t.status == "waiting" and not t.header.get("wait_for") and not t.header.get("next")]
    if stuck:
        rows.append(("ожидание без условия", WARN, ", ".join(stuck) + " — ждут владельца или потеряны"))
    blocked = [t.id for t in tickets if t.status == "blocked"]
    if blocked:
        rows.append(("blocked", WARN, ", ".join(blocked)))
    return rows


def check_worktrees(project: Path) -> tuple:
    status = {}
    for p in (project / ".claude" / "tickets").glob("*.md"):
        try:
            t = T.read_ticket(p)
            status[t.id] = t.status
        except Exception:
            continue
    try:
        closed, foreign = WH.findings(project, status)
    except (OSError, subprocess.SubprocessError):
        return "рабочие копии", OFF, "git недоступен"
    if closed or foreign:
        msg = "; ".join(x for x in (
            closed and "закрытых тикетов: " + ", ".join(p.name for p in closed),
            foreign and "вне .claude/worktrees: " + ", ".join(str(p) for p in foreign)) if x)
        return "рабочие копии", WARN, msg
    return "рабочие копии", OK, "лишних нет"


def check_errors(state_dir: Path, now: float) -> tuple:
    lines = _log_lines_since(state_dir / "runs.log", now - DAY)
    bad = [l for l in lines if " status=ok" not in l and "status=" in l]
    detail = f"запусков за сутки: {len(lines)}, не ok: {len(bad)}"
    if bad:
        detail += " — последний: " + bad[-1][:140]
    return "ошибки за сутки", (WARN if bad else OK), detail


def check_idle(state_dir: Path, now: float, slo_min: float = 10.0) -> tuple:
    import downtime
    try:
        st = json.loads((state_dir / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "простой сегодня", OFF, "диспетчер ещё не вёл счёт"
    rec = (st.get("downtime") or {}).get(datetime.fromtimestamp(now).date().isoformat())
    if not rec:
        return "простой сегодня", OK, "0 мин"
    m = downtime.total_min(rec)
    detail = (f"{m:.0f} мин из {slo_min:.0f} (готовая работа без исполнителя {rec['idle_s'] / 60:.0f}, "
              f"ожидание без условия {rec['wait_s'] / 60:.0f}, молчание диспетчера {rec['stall_s'] / 60:.0f}, "
              f"тормоз запусков {rec.get('throttle_s', 0) / 60:.0f}; сумма корзин; пауза лимита вне SLO "
              f"{rec.get('limit_s', 0) / 60:.0f})")
    return "простой сегодня", (FAIL if m > slo_min else OK), detail


def _state_bus_url(sd: Path):
    try:
        return json.loads((sd / "state.json").read_text(encoding="utf-8")).get("bus_url")
    except (OSError, ValueError):
        return None


def check_copies(project: Path, name: str, procs=None) -> tuple | None:
    """Ровно одна копия службы: две — гонка за замок и двойная работа (FAIL); нет копии ловит check_service."""
    n = len(S.find_copies(f"{name}.py", project, procs))
    return (f"копии {name}", FAIL, f"{n} копий вместо одной") if n > 1 else None


def run_checks(project: Path, now: float | None = None, installed=scheduler_installed, bus_request=None,
               alive_only: bool = False, procs=None) -> list:
    """alive_only — проверка выпуска: накопленный за сутки простой (check_idle) к исправности новой версии не относится."""
    now = time.time() if now is None else now
    sd = project / ".claude" / "dispatcher"
    names = S.services()
    procs = S._list_procs() if procs is None else procs   # один снимок на все службы
    rows = [check_service(n, sd, now) for n in names]
    rows += [c for c in (check_copies(project, n, procs) for n in names) if c]
    rows += [check_supervise(project, sd, now, installed), check_bus(bus_request, _state_bus_url(sd))]
    if not alive_only:
        rows.append(check_idle(sd, now, float(P.env("IDLE_SLO_MIN", "10"))))
    rows += [*check_queue(project, sd), check_worktrees(project), check_errors(sd, now)]
    return [("диспетчер" if r[0] == "dispatch" else "сторож" if r[0] == "watch" else r[0], *r[1:]) for r in rows]


def render(rows: list) -> str:
    w = max(len(r[0]) for r in rows)
    return "\n".join(f"{r[1]:<4}  {r[0]:<{w}}  {r[2]}" for r in rows)


def main(argv=None) -> int:
    P.utf8_stdio()
    ap = argparse.ArgumentParser()
    ap.add_argument("--project")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--alive", action="store_true", help="проверка выпуска: без накопленного простоя за сутки")
    a, _ = ap.parse_known_args(argv)
    try:
        project = P.resolve_project(["--project", a.project] if a.project else [])
    except P.ProjectNotFound:
        print(P.NOT_FOUND_HINT, file=sys.stderr)
        return 2
    rows = run_checks(Path(project), alive_only=a.alive)
    print(json.dumps([dict(zip(("check", "status", "detail"), r)) for r in rows], ensure_ascii=False, indent=1)
          if a.json else render(rows))
    return 1 if any(r[1] == FAIL for r in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
