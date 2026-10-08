"""Вливание PR по правилу без ИИ (TK-079 п.2, В-195). Вливает, когда вместе: вердикт Судьи `accepted: <PR>@<sha>` на ТЕКУЩЕЙ
голове (`tickets.py accept`) + CI зелёный на ней (ci-state.json) + GitHub считает PR сливаемым. Голова сменилась после
вердикта — не вливает. Конфликт — запись и `next` владельцу тикета (не CEO), один раз на голову. База не main (цепочка):
пока родительский PR открыт — ждёт; родитель влит — переключает базу на main. После слияния — проверка `merged`, запись
в тикет и событие в шину. Защиту main не ставим (В-193): `sha` в запросе слияния защищает от гонки со сменой головы."""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ci_watch as C  # noqa: E402
import dispatch as D  # noqa: E402
import ticket as T  # noqa: E402


ERR_LIMIT = 3          # подряд сбоев GitHub (5xx, сеть) на одном PR, после которых о нём узнаёт владелец
_TRANSIENT = re.compile(r"HTTP 5\d\d|timed out|timeout|connection", re.I)


def merge_state_path() -> Path:
    return D.STATE_FILE.parent / "merge-state.json"


def _load() -> dict:
    import json
    try:
        return json.loads(merge_state_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(st: dict) -> None:
    import json
    T.atomic_write_text(merge_state_path(), json.dumps(st, ensure_ascii=False, indent=1))


def parse_accepted(value) -> dict:
    """`accepted: 22@sha, 23@sha` → {22: 'sha', 23: 'sha'} (несколько PR одного тикета)."""
    out = {}
    for part in str(value or "").split(","):
        num, _, sha = part.partition("@")
        if num.strip().isdigit() and sha.strip():
            out[int(num)] = sha.strip()
    return out


def format_accepted(acc: dict) -> str:
    return ", ".join(f"{n}@{s}" for n, s in sorted(acc.items()))


def accepted_head(tkt, pr_number: int):
    """sha из `accepted` для этого PR, иначе None."""
    return parse_accepted(tkt.header.get("accepted")).get(pr_number)


def _drop_accepted(tkt, pr_number: int) -> None:
    with T.ticket_lock(tkt.path):
        cur = T.read_ticket(tkt.path)
        acc = parse_accepted(cur.header.get("accepted"))
        if acc.pop(pr_number, None) is not None:
            T.write_header_updates(tkt.path, {"accepted": format_accepted(acc)}, stamp_updated=False)


def _note(tkt, text: str, wake_owner: bool = False, bus=None) -> None:
    if wake_owner:
        C.wake(tkt.path, tkt, tkt.owner, text)
    else:
        with T.ticket_lock(tkt.path):
            T.append_log(tkt.path, "merge", text)
    if bus:
        bus(tkt.id, "статус", {"merge": text[:120]})


def _note_not_found(repo: str, number: int) -> None:
    """404 в `merged:`: запись в лог тикета, который ждёт этот PR, — проснувшийся владелец видит причину в тикете, а не
    только в журнале диспетчера. Повторный опрос того же ожидания вторую запись не пишет."""
    text = f"PR #{number} не найден в {repo} — ожидание merged: снято; проверь номер PR и репозиторий (RPV_CI_REPO)."
    want = f"merged:{repo}#{number}"
    for p in T.list_tickets(D.TICKETS_DIR):
        try:
            tkt = T.read_ticket(p)
        except Exception:
            continue
        if (tkt.header.get("wait_for") or "").strip() != want:
            continue
        if any(e.author == "merge" and e.text.strip() == text for e in tkt.log):
            continue
        _note(tkt, text)


def landed_prs(tkt) -> set:
    """Номера PR тикета, влитых программой: запись лога «PR #N влит в …» (_drop_accepted снимает их из `accepted`)."""
    return {int(m.group(1)) for e in tkt.log for m in [re.match(r"PR #(\d+) влит в ", e.text.strip())] if m}


def merged_done(repo: str, number: int, gh=C.gh_api) -> bool:
    """wait_for `merged:<репо>#<PR>`: PR влит. PR не найден (404) — ожидание вечным не делаем: «готово», в тикет пишется
    причина, владелец просыпается. Прочий сбой GitHub — не готово (повтор на след. тике)."""
    try:
        return bool(gh(f"repos/{repo}/pulls/{number}").get("merged"))
    except Exception as e:
        if "404" in str(e) or "Not Found" in str(e):
            print(f"merged:{repo}#{number}: PR не найден — ожидание снято, проверь репозиторий", file=sys.stderr)
            _note_not_found(repo, number)
            return True
        return False


def check_pr(repo: str, number: int, sha: str, gh=C.gh_api) -> str:
    """accept: PR есть в этом репозитории и его голова начинается с sha. Пустая строка — годно, иначе текст отказа."""
    try:
        head = gh(f"repos/{repo}/pulls/{number}")["head"]["sha"]
    except Exception as e:
        return f"PR #{number} не найден в {repo} ({e}) — проверь RPV_CI_REPO/--repo"
    if not head.startswith(sha.lower()):
        return f"голова PR #{number} в {repo} — {head[:7]}, а вердикт на {sha[:7]}: сверь --sha"
    return ""


def _gh_error(st: dict, key: str, sha: str, tkt, what: str, e: Exception, bus) -> str:
    """Сбой GitHub на принятом PR. Временный (5xx, сеть) — повтор на след. тике, владельцу — после ERR_LIMIT подряд;
    прочий (отказ слияния, 4xx) — владельцу сразу. Одна запись на голову. Возвращает пометку для отчёта или ''."""
    ent = st.get(key) or {}
    if ent.get("failed") == sha:
        return ""
    errs = ent.get("errs", 0) + 1 if ent.get("errs_sha") == sha else 1
    if _TRANSIENT.search(str(e)) and errs < ERR_LIMIT:
        st[key] = {"errs": errs, "errs_sha": sha}
        return ""
    n = key.rsplit("#", 1)[1]
    note = f"PR #{n}: {what} не удалось на {sha[:7]}: {e}" + (f" ({errs} раз подряд)" if errs > 1 else "")
    _note(tkt, note, wake_owner=True, bus=bus)
    st[key] = {"failed": sha}
    return f"{what}: сбой → владельцу"


def merge_once(repo: str, gh=C.gh_api, bus=None) -> list:
    """Один проход по открытым PR. Возвращает [(PR, что сделано)]."""
    st, out = _load(), []
    tickets = []
    for p in T.list_tickets(D.TICKETS_DIR):
        try:
            tickets.append(T.read_ticket(p))
        except Exception:
            continue
    default = gh(f"repos/{repo}")["default_branch"]
    prs = gh(f"repos/{repo}/pulls?state=open&per_page=100")
    open_heads = {p["head"]["ref"] for p in prs}
    ci = C.load_state()
    for pr in prs:
        n, sha = pr["number"], pr["head"]["sha"]
        tkt = C.ticket_for_pr(pr, tickets)
        if tkt is None or tkt.status not in C.ACTIVE + ("done",):
            continue
        acc = accepted_head(tkt, n)
        if not acc or not sha.startswith(acc):
            continue
        key = f"{repo}#{n}"
        ci_ok = (ci.get(key) or {}).get("sha") == sha and (ci.get(key) or {}).get("state") == "success"
        base = pr["base"]["ref"]
        if base != default:
            if not ci_ok or base in open_heads:
                continue
            gh(f"repos/{repo}/pulls/{n}", method="PATCH", base=default)
            out.append((n, f"база {base} → {default}"))
            continue
        try:
            detail = gh(f"repos/{repo}/pulls/{n}")
        except Exception as e:
            r = _gh_error(st, key, sha, tkt, "запрос состояния PR", e, bus)
            if r:
                out.append((n, r))
            continue
        if "errs" in (st.get(key) or {}):
            st.pop(key)
        if detail.get("mergeable") is False:                  # конфликт сообщаем сразу: CI на конфликтной голове не нужен
            if (st.get(key) or {}).get("conflict") != sha:
                _note(tkt, f"PR #{n} принят на {sha[:7]}, но конфликтует с {default}: слей {default} в ветку и запушь "
                           f"(CI и вердикт на новой голове — заново).", wake_owner=True, bus=bus)
                st[key] = {"conflict": sha}
                out.append((n, "конфликт → владельцу"))
            continue
        if not ci_ok or detail.get("mergeable") is None:
            continue
        try:
            gh(f"repos/{repo}/pulls/{n}/merge", method="PUT", merge_method="merge", sha=sha)
        except Exception as e:
            r = _gh_error(st, key, sha, tkt, "слияние", e, bus)
            if r:
                out.append((n, r))
            continue
        st.pop(key, None)
        if gh(f"repos/{repo}/pulls/{n}").get("merged"):
            _note(tkt, f"PR #{n} влит в {default} на голове {sha[:7]} (проверено: merged).", bus=bus)
            _drop_accepted(tkt, n)
            st[key] = {"merged": sha}
            out.append((n, "влит"))
        else:
            _note(tkt, f"PR #{n}: запрос слияния принят, но merged=false на {sha[:7]} — проверь вручную.",
                  wake_owner=True, bus=bus)
            st[key] = {"failed": sha}
            out.append((n, "не подтверждено → владельцу"))
    _save(st)
    return out
