"""Вливание PR по правилу без ИИ (TK-079 п.2, В-195). Вливает, когда вместе: вердикт Судьи `accepted: <PR>@<sha>` на ТЕКУЩЕЙ
голове (`tickets.py accept`) + CI зелёный на ней (ci-state.json) + GitHub считает PR сливаемым. Голова сменилась после
вердикта — не вливает. Конфликт — запись и `next` владельцу тикета (не CEO), один раз на голову. База не main (цепочка):
пока родительский PR открыт — ждёт; родитель влит — переключает базу на main. После слияния — проверка `merged`, запись
в тикет и событие в шину. Защиту main не ставим (В-193): `sha` в запросе слияния защищает от гонки со сменой головы."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ci_watch as C  # noqa: E402
import dispatch as D  # noqa: E402
import ticket as T  # noqa: E402


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


def merged_done(repo: str, number: int, gh=C.gh_api) -> bool:
    """wait_for `merged:<репо>#<PR>`: PR влит. Не удалось спросить у GitHub — не готово."""
    try:
        return bool(gh(f"repos/{repo}/pulls/{number}").get("merged"))
    except Exception:
        return False


def repo_slug() -> str:
    """<владелец/репо> для `merged:`: RPV_CI_REPO, иначе `gh repo view`; не определился — пусто."""
    import subprocess
    slug = (C.P.env("CI_REPO", "") or "").strip()
    if slug:
        return slug
    try:
        r = subprocess.run(["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
                           capture_output=True, timeout=30)
        return r.stdout.decode("utf-8", "replace").strip() if r.returncode == 0 else ""
    except Exception:
        return ""


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
        cur = ci.get(f"{repo}#{n}") or {}
        if cur.get("sha") != sha or cur.get("state") != "success":
            continue
        base = pr["base"]["ref"]
        if base != default:
            if base in open_heads:
                continue
            gh(f"repos/{repo}/pulls/{n}", method="PATCH", base=default)
            out.append((n, f"база {base} → {default}"))
            continue
        key = f"{repo}#{n}"
        detail = gh(f"repos/{repo}/pulls/{n}")
        if detail.get("mergeable") is None:
            continue
        if detail.get("mergeable") is False:
            if (st.get(key) or {}).get("conflict") != sha:
                _note(tkt, f"PR #{n} принят на {sha[:7]}, но конфликтует с {default}: слей {default} в ветку и запушь "
                           f"(CI и вердикт на новой голове — заново).", wake_owner=True, bus=bus)
                st[key] = {"conflict": sha}
                out.append((n, "конфликт → владельцу"))
            continue
        try:
            gh(f"repos/{repo}/pulls/{n}/merge", method="PUT", merge_method="merge", sha=sha)
        except Exception as e:
            if (st.get(key) or {}).get("failed") != sha:
                _note(tkt, f"PR #{n}: слияние не удалось на {sha[:7]}: {e}", wake_owner=True, bus=bus)
                st[key] = {"failed": sha}
                out.append((n, "слияние не удалось → владельцу"))
            continue
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
