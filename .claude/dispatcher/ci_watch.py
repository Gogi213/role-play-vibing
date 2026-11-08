"""События CI без ИИ (TK-079 п.1, В-195): опрос GitHub раз в CI_WATCH_INTERVAL_S (≤ 60 с, `gh api`, токены — из gh).
CI не ждём (TK-139, В-72): Судья на сдачу идёт сразу, PR вливает merge_rule по вердикту без CI. Для каждого открытого PR
смотрит check-runs головы; красный CI на голове — один раз (по паре PR+sha) запись `ci` в тикет этого PR (имена красных клеток),
без побудки: Судья видит запись, но не ждёт. Красный CI на голове основной ветки (там же — слитые PR) — один тикет Инженеру
«Починить CI» на голову, с красными клетками, упавшими тестами и ссылкой на прогон; без отката и без возврата принятого.
Тикет PR: поле шапки `pr: 22[, 23]`, иначе TK-<N> в ветке/заголовке. Тикет, ждавший `ci:<PR>` (поставлен до В-72), будится
как прежде и ожидание снимается.
Состояние — `ci-state.json` рядом с state.json диспетчера; из него `wait_for: ci:<владелец/репо>#<PR>` (готово, когда CI
на текущей голове завершён)."""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import project_paths as CFG  # noqa: E402
import hide  # noqa: E402
import merge_rule  # noqa: E402
import procs  # noqa: E402
import project as P  # noqa: E402
import ticket as T  # noqa: E402
from ci_core import gh_api, load_state, save_state, ticket_for_pr, wake  # noqa: E402,F401

INTERVAL_S = 60.0
RED = ("failure", "cancelled", "timed_out", "action_required", "startup_failure", "stale")


def ci_result(runs: list):
    """(state, красные): pending — клеток нет или есть незавершённая; failure — есть красная; иначе success."""
    if not runs or any(r.get("status") != "completed" for r in runs):
        return "pending", []
    red = sorted({r.get("name", "?") for r in runs if r.get("conclusion") in RED})
    if red:
        return "failure", red
    return "success", []


def judged_on(tkt, sha: str) -> bool:
    return any(T.author_is(e.author, "judge") and sha[:7] in e.text for e in tkt.log)


def decide(tkt, sha: str, state: str, red: list, pr_number: int, repo: str):
    """(кого будить | None, текст записи). pending и зелёный — тишина: CI не ждём (В-72). Красный — запись без побудки, Судья
    видит её, но не ждёт. Тикет, ждавший `ci:` этого PR (поставлен до В-72), будится как прежде: красный → владелец, зелёный
    → Судья (не владелец: лишний запуск ИИ ради передачи); ожидание при этом снимает wake()."""
    if state == "pending" or tkt.status not in T.ACTIVE_STATUSES:
        return None, ""
    s7 = sha[:7]
    waits = (tkt.header.get("wait_for") or "").strip() == f"ci:{repo}#{pr_number}"
    if state == "failure":
        text = f"CI красный на {s7} (PR #{pr_number}): {', '.join(red)}. Судья не ждёт CI (В-72); красное после слияния чинит тикет «Починить CI»."
        return (tkt.owner if waits else None), text
    if waits and not judged_on(tkt, sha):
        return "judge", f"CI зелёный на {s7} (PR #{pr_number}): проверь голову; вердикт — записью с {s7}."
    return None, ""


ERR_LIMIT = 3          # подряд сбоев опроса CI одного PR, после которых о нём узнаёт владелец тикета


def _count_error(st: dict, key: str, sha: str, tkt, pr_number: int, e: Exception) -> None:
    """Сбой опроса GitHub не молчит в stderr вечно: на ERR_LIMIT-м подряд — запись и `next` владельцу тикета (один раз на голову)."""
    ent = st.setdefault(key, {})
    ent["errs"] = ent.get("errs", 0) + 1
    if ent["errs"] != ERR_LIMIT or tkt is None or tkt.status not in T.ACTIVE_STATUSES:
        return
    wake(tkt.path, tkt, tkt.owner, f"CI PR #{pr_number} ({sha[:7]}): GitHub отвечает ошибкой {ERR_LIMIT} раза подряд: {e}. "
                                   "Проверь PR и права gh вручную; ci_watch продолжает опрос.")


def _mark_closed(st: dict, repo: str, open_numbers: set, complete: bool) -> None:
    """Записи PR этого репо, которых нет среди открытых, помечаются `closed: true` (Пульс не жжёт ci_red по закрытым, TK-102);
    запись остаётся: из неё `ci_done`. Неполная страница (≥ 100 открытых) — не помечаем: отсутствие в ней ничего не доказывает."""
    if not complete:
        return
    for key, ent in st.items():
        head, _, number = key.partition("#")
        if head == repo and number.isdigit() and int(number) not in open_numbers and isinstance(ent, dict):
            ent["closed"] = True


def run_once(repo: str, gh=gh_api) -> list:
    """Один проход. Возвращает список (PR, sha7, state, кого разбудили)."""
    st, out = load_state(), []
    tickets = []
    for p in T.list_tickets(CFG.TICKETS_DIR):
        try:
            tickets.append(T.read_ticket(p))
        except Exception:
            continue
    prs = gh(f"repos/{repo}/pulls?state=open&per_page=100")
    _mark_closed(st, repo, {int(p["number"]) for p in prs}, complete=len(prs) < 100)
    for pr in prs:
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
        prev.pop("closed", None)  # открыт снова
        if prev.get("sha") == sha and prev.get("state") == state:
            continue
        woke = ""
        tkt = ticket_for_pr(pr, tickets)
        if state != "pending" and tkt is not None:
            who, text = decide(tkt, sha, state, red, pr["number"], repo)
            if who:
                wake(tkt.path, tkt, who, text, repo, pr["number"])
                woke = who
            elif text:
                with T.ticket_lock(tkt.path):
                    T.append_log(tkt.path, "ci", text)
        st[key] = {"sha": sha, "state": state, "red": red, "woke": woke, "at": T.now_iso()}
        out.append((pr["number"], sha[:7], state, woke))
    save_state(st)
    return out


FIX_PREFIX = "Починить CI"
_FAILED = re.compile(r"FAILED (\S+::\S+)")


def _job_log(repo: str, job_id) -> str:
    r = hide.run(["gh", "api", f"repos/{repo}/actions/jobs/{job_id}/logs"], capture_output=True, timeout=60)
    return (r.stdout or b"").decode("utf-8", "replace") if r.returncode == 0 else ""


def failed_tests(repo: str, runs: list, job_log=None) -> list:
    """Имена упавших тестов (`FAILED путь::тест` в журнале красных клеток); не удалось получить журнал — пусто. До 3 клеток, до 15 имён."""
    job_log, names = job_log or _job_log, []
    for r in [r for r in runs if r.get("conclusion") in RED][:3]:
        try:
            names += _FAILED.findall(job_log(repo, r.get("id")))
        except Exception:
            continue
    return sorted(set(names))[:15]


def _open_fix_ticket(tickets: list):
    return next((t for t in tickets if str(t.header.get("title") or "").startswith(FIX_PREFIX) and t.status not in ("done", "stopped")), None)


def watch_main(repo: str, gh=gh_api, job_log=None) -> list:
    """Красный CI на голове основной ветки (слитый PR приходит туда же): один тикет Инженеру «Починить CI» на голову (В-72), без
    отката и без возврата принятого. Уже есть открытый тикет починки — запись о новой голове в него, нового не заводим.
    Возвращает [(sha7, tid)]."""
    default = gh(f"repos/{repo}")["default_branch"]
    runs = gh(f"repos/{repo}/commits/{default}/check-runs?per_page=100").get("check_runs", [])
    state, red = ci_result(runs)
    if state != "failure":
        return []
    sha, key, st = str(runs[0].get("head_sha") or ""), f"main:{repo}", load_state()
    if not sha or (st.get(key) or {}).get("sha") == sha:
        return []
    tickets = []
    for p in T.list_tickets(CFG.TICKETS_DIR):
        try:
            tickets.append(T.read_ticket(p))
        except Exception:
            continue
    names, url = failed_tests(repo, runs, job_log), next((r.get("html_url") for r in runs if r.get("conclusion") in RED), "")
    what = (f"CI красный на {default} {sha[:7]}: клетки {', '.join(red)}; упавшие тесты: {', '.join(names) or 'не удалось прочитать журнал — смотри прогон'}; "
            f"прогон: {url or 'https://github.com/' + repo + '/commit/' + sha + '/checks'}.")
    open_t = _open_fix_ticket(tickets)
    if open_t is not None:
        with T.ticket_lock(open_t.path):
            T.append_log(open_t.path, "ci", f"Новая красная голова. {what}")
        tid = open_t.id
    else:
        nl = chr(10)
        desc = nl.join(["Ссылка на задачу владельца: В-72", "", what,
                        f"Слитое не откатываем и принятое не возвращаем (В-72): найди причину, поправь, сдай PR в {default}.",
                        f"Готово, когда: упавшие тесты зелёные (тесты самой правки), причина названа в итоге; PR в {default}."])
        tid = T.create_ticket(CFG.TICKETS_DIR, owner="engineer", title=f"{FIX_PREFIX} на {default} {sha[:7]}", description=desc,
                              reviewer="judge").stem
    st[key] = {"sha": sha, "state": state, "red": red, "ticket": tid, "at": T.now_iso()}
    save_state(st)
    return [(sha[:7], tid)]


def ci_done(repo: str, number: int, gh=gh_api) -> bool:
    """wait_for `ci:<репо>#<PR>`: CI завершён на ТЕКУЩЕЙ голове PR (её спрашиваем у GitHub, не берём из состояния:
    сразу после пуша состояние ещё хранит старую голову). Не удалось спросить — не готово. При `has_ci: false` клеток нет,
    состояние вечно pending, ожидание не снимется: постановку `ci:` там отказывает `ticket.ci_wait_problem` (TK-084)."""
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
    f = CFG.STATE_FILE.parent / "ci-heartbeat.json"
    T.atomic_write_text(f, json.dumps({"ts": T.now_iso()}))


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not CFG.ensure_project(argv, "ci_watch"):
        return 2
    repo = next((a.split("=", 1)[1] for a in argv if a.startswith("--repo=")), None) or P.setting("ci_repo", "")
    if not repo:
        print("ci_watch: нужен --repo=<владелец/репо> или ci_repo в .claude/rpv.json", file=sys.stderr)
        return 2
    if "--once" not in argv:  # цикл — единственный экземпляр (замок) с сердцебиением для присмотра supervise (TK-090 Д-1)
        lock = CFG.DISPATCHER_DIR / "ci_watch.pid"
        ok, why = procs.acquire_instance_lock(lock)
        if not ok:
            print(f"[ci_watch] {why}", file=sys.stderr)
            return 1
        import atexit
        atexit.register(procs.release_instance_lock, lock)
    while True:
        try:
            for n, s7, state, woke in run_once(repo):
                print(f"[ci_watch] PR #{n} {s7}: {state}" + (f" → {woke}" if woke else ""))
            for n, what in merge_rule.merge_once(repo):
                print(f"[merge_rule] PR #{n}: {what}")
            for s7, tid in watch_main(repo):
                print(f"[ci_watch] CI красный на основной ветке {s7} → {tid}")
        except Exception as e:
            print(f"[ci_watch] цикл: {type(e).__name__}: {e}", file=sys.stderr)
        write_heartbeat()
        if "--once" in argv:
            return 0
        time.sleep(INTERVAL_S)


if __name__ == "__main__":
    sys.exit(main())
