"""Гейты приёмки Судьи одним списком: пробный прогон `--dry-run` и исход «ложное срабатывание» (TK-089, В-36).

Гейты (решения, числа, хвосты, живая проверка, критик, ярлыки договора) открывались по одному: исправил один отказ — вылез
следующий, и часть возвратов Инженеру была формальной. `run` гоняет их в прежнем порядке; `dry_run` — все сразу, ничего не
пишет (ни лога тикета, ни шапки, ни входящих CEO, ни regressions.log, ни critic-runs.log) и печатает весь список.
Эвристики хвостов и чисел могут ошибиться: Судья заводит ошибку (`errors.py add --title … --ticket TK-N`) и принимает с
`--false-positive E-NNN` — отказы этих двух гейтов снимаются, в запись приёмки идёт «ложное срабатывание: E-NNN». Остальные
гейты (решения, живая проверка, критик, ярлыки, регрессия, CI) так не снимаются. Только stdlib; `tk` — модуль `tickets`.
"""
from __future__ import annotations

import re
import sys

import errors as errors_mod
import project as P

FP_GATES = ("numbers", "tail")   # эвристики по тексту — единственные, что снимает --false-positive
FP_HINT = ("отказ эвристики (хвосты или числа) ложный — заведи ошибку: `errors.py add --title \"ложное срабатывание: …\" "
           "--ticket <ID>` и прими с `--false-positive E-NNN`; настоящий — `result <ID> return`")


def _false_positive(args, tk) -> tuple[str, str]:
    """`--false-positive E-NNN`: ошибка есть в реестре и заведена на этот тикет. → (отказ, номер); флага нет — ("", "")."""
    raw = (getattr(args, "false_positive", None) or "").strip()
    if not raw:
        return "", ""
    eid = errors_mod._norm(raw)
    path = errors_mod.errors(tk.PROJECT_ROOT).get(eid)
    if not path:
        return f"--false-positive {raw}: такой ошибки нет в реестре — сначала `errors.py add --title … --ticket {args.id}`", ""
    if errors_mod.header(path).get("ticket", "").strip().upper() != args.id.upper():
        return f"--false-positive {raw}: ошибка заведена не на {args.id} (`--ticket {args.id}` при `errors.py add`)", ""
    return "", eid


def run(args, tk, collect_all: bool = False, proof: bool = True, record=None) -> tuple[list[str], str, str]:
    """Гейты приёмки по порядку. → (отказы, пометки для лога, хвост для CEO). Без `collect_all` дорогие гейты (критик,
    ярлыки) после отказа не гоняются — как было; с ним идут все. `proof=False` — ярлыки гонит вызывающий (cmd_accept)."""
    derr, ack = tk._decisions_ack(args)
    nerr, nack = tk._numbers_ack(args, args.id)
    terr, tack, tail_ceo = tk._tail_ack(args, args.id)
    lerr, lack = tk._live_ack(args, args.id)
    fperr, fp = _false_positive(args, tk)
    skipped = []
    if fp:
        for name, err, given in (("числа", nerr, getattr(args, "numbers", None)), ("хвосты", terr, getattr(args, "tail", None))):
            if err and (given or "").strip():
                skipped.append(name)
        nerr = "" if "числа" in skipped else nerr
        terr = "" if "хвосты" in skipped else terr
    errs = [fperr, derr, nerr, terr, lerr]
    cerr, cack = ("", "") if any(errs) and not collect_all else tk._critic_ack(args, args.id, record=record)
    perr, pack = ("", "") if (any(errs) or cerr) and not collect_all or not proof else tk._proof_ack(args.id)
    marks = f" ложное срабатывание: {fp} ({', '.join(skipped)})" if skipped else ""
    return [e for e in errs + [cerr, perr] if e], ack + nack + tack + lack + cack + pack + marks, tail_ceo


def dry_run(args, tk, slug: str = "", head_err: str = "") -> int:
    """`--dry-run`: все гейты за один раз, ничего не пишет. `head_err` — отказ входных данных `result` (неполная команда),
    `slug` — репозиторий PR (пусто — гейт CI не проверяется). 0 — приёмка пройдёт, 1 — список отказов."""
    import merge_rule
    found = [head_err] if head_err else []
    if args.pr and slug:
        found.append(merge_rule.check_pr(slug, int(args.pr), args.sha))
    errs, _, _ = run(args, tk, collect_all=True, record=lambda *a, **k: None)
    found += errs
    bad = errors_mod.run_all(tk.PROJECT_ROOT, tk.PROJECT_ROOT / ".claude" / "worktrees" / args.id
                             if (tk.PROJECT_ROOT / ".claude" / "worktrees" / args.id).is_dir() else tk.PROJECT_ROOT)
    if bad:
        found.append("регрессия — упали проверки исправленных ошибок: "
                     + "; ".join(f"{eid} «{title}»: {note.splitlines()[0] if note else ''}" for eid, title, note in bad[:5]))
    found = [e for e in found if e]
    if not found:
        print(f"accept --dry-run {args.id}: отказов нет — приёмка пройдёт (ничего не записано)")
        return 0
    print(f"accept --dry-run {args.id}: отказов {len(found)} (ничего не записано):", file=sys.stderr)
    for i, e in enumerate(found, 1):
        print(f"{i}. {re.sub(chr(10), chr(10) + '   ', e)}", file=sys.stderr)
    if any(k in e for e in found for k in ("хвост", "числ")):
        print(FP_HINT, file=sys.stderr)
    return 1


def dry_run_result(args, tk, role: str, tkt) -> int:
    """`result <ID> accept --dry-run`: неполная команда — тоже в списке; другие итоги пробного прогона не имеют."""
    if args.result != "accept":
        print("result --dry-run — только для accept", file=sys.stderr)
        return 1
    slug = (getattr(args, "repo", None) or P.setting("ci_repo", "") or "").strip()
    return dry_run(args, tk, slug, tk._result_refusal(args, role, tkt))
