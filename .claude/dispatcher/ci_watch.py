"""События CI в шину без ИИ (TK-079 п.1, В-195): опрос GitHub раз в CI_WATCH_INTERVAL_S (≤ 60 с, `gh api`, токены — из gh).
Для каждого открытого PR проекта смотрит check-runs головы; когда CI на голове завершён — один раз (по паре PR+sha) пишет
запись `ci` в тикет этого PR и будит: красный → владелец тикета (имена красных клеток), зелёный → Судья, если вердикта на
этой голове ещё нет (запись Судьи с началом sha). Тикет PR: поле шапки `pr: 22[, 23]`, иначе TK-<N> в ветке/заголовке.
Состояние — `ci-state.json` рядом с state.json диспетчера; из него `wait_for: ci:<владелец/репо>#<PR>` (готово, когда CI
на текущей голове завершён)."""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dispatch as D  # noqa: E402
import project as P  # noqa: E402
import ticket as T  # noqa: E402

INTERVAL_S = min(60.0, float(P.env("CI_WATCH_INTERVAL_S", "60")))
RED = ("failure", "cancelled", "timed_out", "action_required", "startup_failure", "stale")
GREEN = ("success", "skipped", "neutral")
_TK_RE = re.compile(r"\bTK-?(\d+)\b", re.I)
ACTIVE = ("todo", "in_progress", "in_review", "waiting")


def state_path() -> Path:
    return D.STATE_FILE.parent / "ci-state.json"


def load_state() -> dict:
    try:
        return json.loads(state_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(st: dict) -> None:
    T.atomic_write_text(state_path(), json.dumps(st, ensure_ascii=False, indent=1))


def gh_api(path: str, method: str = "GET", **fields):
    args = ["gh", "api", "-X", method, path] + [x for k, v in fields.items() for x in ("-f", f"{k}={v}")]
    r = subprocess.run(args, capture_output=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or b"").decode("utf-8", "replace").strip()[:200])
    return json.loads(r.stdout.decode("utf-8"))


def ci_result(runs: list):
    """(state, красные): pending — клеток нет или есть незавершённая; failure — есть красная; иначе success."""
    if not runs or any(r.get("status") != "completed" for r in runs):
        return "pending", []
    red = sorted({r.get("name", "?") for r in runs if r.get("conclusion") in RED})
    if red:
        return "failure", red
    return "success", []


def ticket_for_pr(pr: dict, tickets: list):
    """Тикет по шапке `pr:` (список номеров), иначе по TK-<N> в имени ветки/заголовке. Нет — None."""
    num = str(pr["number"])
    for t in tickets:
        if num in re.findall(r"\d+", str(t.header.get("pr") or "")):
            return t
    m = _TK_RE.search(f"{pr.get('head', {}).get('ref', '')} {pr.get('title', '')}")
    if m:
        want = f"TK-{int(m.group(1)):03d}"
        for t in tickets:
            if t.id == want:
                return t
    return None


def judged_on(tkt, sha: str) -> bool:
    return any(T.author_is(e.author, "judge") and sha[:7] in e.text for e in tkt.log)


def decide(tkt, sha: str, state: str, red: list, pr_number: int, repo: str):
    """(кого будить | None, текст записи). pending — тишина. Тикет, ждущий `ci:` этого PR, будится так же, как прочие:
    зелёный → Судья (не владелец: лишний запуск ИИ ради передачи), красный → владелец; ожидание при этом снимает wake()."""
    if state == "pending" or tkt.status not in ACTIVE:
        return None, ""
    s7 = sha[:7]
    if state == "failure":
        return tkt.owner, f"CI красный на {s7} (PR #{pr_number}): {', '.join(red)}. Исправь и запушь — CI запустится сам."
    if judged_on(tkt, sha):
        return None, ""
    return "judge", f"CI зелёный на {s7} (PR #{pr_number}): проверь голову; вердикт — записью с {s7}."


def wake(path, tkt, who: str, text: str, repo: str = "", pr_number: int = 0) -> None:
    upd = {"next": who}
    if repo and (tkt.header.get("wait_for") or "").strip() == f"ci:{repo}#{pr_number}":
        upd.update({"wait_for": "", "on_met": ""})
    elif repo and who != "judge" and (tkt.header.get("wait_for") or "").strip() == f"merged:{repo}#{pr_number}":
        # красный CI на принятом PR: ждать влития нечего (PR не влить) — владелец будится, ожидание снято (TK-092, CEO 05:25)
        upd.update({"wait_for": "", "on_met": "", "status": "in_progress"})
    with T.ticket_lock(path):
        T.append_log(path, "ci", text)
        T.write_header_updates(path, upd, stamp_updated=False)


def bus_emit(pr_number: int, sha: str, state: str, red: list, who: str, tid: str) -> None:
    """Событие `ci.<PR>.<sha7>` в шину (журнал для табло/аудита); шины нет или лежит — молча, тикет уже записан."""
    try:
        import busclient
        busclient.post(f"ci.{pr_number}.{sha[:7]}", {"state": state, "red": red, "woke": who, "ticket": tid}, timeout=3)
    except Exception:
        pass


ERR_LIMIT = 3          # подряд сбоев опроса CI одного PR, после которых о нём узнаёт владелец тикета


def _count_error(st: dict, key: str, sha: str, tkt, pr_number: int, e: Exception) -> None:
    """Сбой опроса GitHub не молчит в stderr вечно: на ERR_LIMIT-м подряд — запись и `next` владельцу тикета (один раз на голову)."""
    ent = st.setdefault(key, {})
    ent["errs"] = ent.get("errs", 0) + 1
    if ent["errs"] != ERR_LIMIT or tkt is None or tkt.status not in ACTIVE:
        return
    wake(tkt.path, tkt, tkt.owner, f"CI PR #{pr_number} ({sha[:7]}): GitHub отвечает ошибкой {ERR_LIMIT} раза подряд: {e}. "
                                   "Проверь PR и права gh вручную; ci_watch продолжает опрос.")


def run_once(repo: str, gh=gh_api) -> list:
    """Один проход. Возвращает список (PR, sha7, state, кого разбудили)."""
    st, out = load_state(), []
    tickets = []
    for p in T.list_tickets(D.TICKETS_DIR):
        try:
            tickets.append(T.read_ticket(p))
        except Exception:
            continue
    for pr in gh(f"repos/{repo}/pulls?state=open&per_page=100"):
        sha = pr["head"]["sha"]
        try:
            runs = gh(f"repos/{repo}/commits/{sha}/check-runs?per_page=100").get("check_runs", [])
        except Exception as e:
            print(f"[ci_watch] PR #{pr['number']}: {e}", file=sys.stderr)
            _count_error(st, f"{repo}#{pr['number']}", sha, ticket_for_pr(pr, tickets), pr["number"], e)
            continue
        state, red = ci_result(runs)
        key = f"{repo}#{pr['number']}"
        prev = st.get(key) or {}
        prev.pop("errs", None)
        if prev.get("sha") == sha and prev.get("state") == state:
            continue
        woke = ""
        tkt = ticket_for_pr(pr, tickets)
        if state != "pending" and tkt is not None:
            who, text = decide(tkt, sha, state, red, pr["number"], repo)
            if who:
                wake(tkt.path, tkt, who, text, repo, pr["number"])
                woke = who
        if state != "pending":
            bus_emit(pr["number"], sha, state, red, woke, tkt.id if tkt is not None else "")
        st[key] = {"sha": sha, "state": state, "red": red, "woke": woke}
        out.append((pr["number"], sha[:7], state, woke))
    save_state(st)
    return out


def ci_done(repo: str, number: int, gh=gh_api) -> bool:
    """wait_for `ci:<репо>#<PR>`: CI завершён на ТЕКУЩЕЙ голове PR (её спрашиваем у GitHub, не берём из состояния:
    сразу после пуша состояние ещё хранит старую голову). Не удалось спросить — не готово."""
    ent = load_state().get(f"{repo}#{number}") or {}
    if ent.get("state") not in ("success", "failure"):
        return False
    try:
        head = gh(f"repos/{repo}/pulls/{number}")["head"]["sha"]
    except Exception:
        return False
    return head == ent.get("sha")


def run_state(repo: str, run_id: int, gh=gh_api) -> str:
    """Прогон CI по id: `running` (идёт/в очереди), `completed` (с любым исходом), `missing` (GitHub ответил 404),
    `error` (не удалось спросить — не считается ни за что)."""
    try:
        run = gh(f"repos/{repo}/actions/runs/{run_id}")
    except Exception as e:
        return "missing" if "404" in str(e) or "Not Found" in str(e) else "error"
    return "completed" if run.get("status") == "completed" else "running"


def run_done(repo: str, run_id: int, gh=gh_api) -> bool:
    """wait_for `ci-run:<репо>#<id>`: прогон завершён (успех или красный — владелец сам смотрит исход)."""
    return run_state(repo, run_id, gh) == "completed"


def write_heartbeat() -> None:
    f = D.STATE_FILE.parent / "ci-heartbeat.json"
    T.atomic_write_text(f, json.dumps({"ts": datetime.now().astimezone().isoformat(timespec="seconds")}))


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not D.ensure_project(argv, "ci_watch"):
        return 2
    repo = next((a.split("=", 1)[1] for a in argv if a.startswith("--repo=")), None) or P.env("CI_REPO", "")
    if not repo:
        print("ci_watch: нужен --repo=<владелец/репо> или RPV_CI_REPO", file=sys.stderr)
        return 2
    if "--once" not in argv:  # цикл — единственный экземпляр (замок) с сердцебиением для присмотра supervise (TK-090 Д-1)
        lock = D.DISPATCHER_DIR / "ci_watch.pid"
        ok, why = D.acquire_instance_lock(lock)
        if not ok:
            print(f"[ci_watch] {why}", file=sys.stderr)
            return 1
        import atexit
        atexit.register(D.release_instance_lock, lock)
    while True:
        try:
            for n, s7, state, woke in run_once(repo):
                print(f"[ci_watch] PR #{n} {s7}: {state}" + (f" → {woke}" if woke else ""))
            import merge_rule
            import tickets
            for n, what in merge_rule.merge_once(repo, bus=tickets.bus_emit):
                print(f"[merge_rule] PR #{n}: {what}")
        except Exception as e:
            print(f"[ci_watch] цикл: {type(e).__name__}: {e}", file=sys.stderr)
        write_heartbeat()
        if "--once" in argv:
            return 0
        time.sleep(INTERVAL_S)


if __name__ == "__main__":
    sys.exit(main())
