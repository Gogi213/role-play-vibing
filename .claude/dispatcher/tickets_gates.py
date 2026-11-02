"""Гейты приёмки Судьи одним списком: пробный прогон `--dry-run` и исход «ложное срабатывание» (TK-089, В-36).

Гейты (решения, числа, хвосты, живая проверка, ярлыки договора) открывались по одному: исправил один отказ — вылез
следующий, и часть возвратов Инженеру была формальной. `run` гоняет их в прежнем порядке; `dry_run` — все сразу, ничего не
пишет (ни лога тикета, ни шапки, ни входящих CEO, ни regressions.log) и печатает весь список.
Эвристики хвостов и чисел могут ошибиться: Судья заводит ошибку (`errors.py add --title … --ticket TK-N`) и принимает с
`--false-positive E-NNN` — отказы этих двух гейтов снимаются, в запись приёмки идёт «ложное срабатывание: E-NNN». Остальные
гейты (решения, живая проверка, ярлыки, регрессия, PR) так не снимаются. Только stdlib; `tk` — модуль `tickets`.
"""
from __future__ import annotations

import re
import sys

import errors as errors_mod
import project as P

HEURISTIC_ARGS = ("-", "—")   # `--tail -` / `--numbers -`: отказ при этом значении — только эвристика по тексту сдачи
FP_HINT = ("отказ эвристики (хвосты или числа) ложный — заведи ошибку: `errors.py add --title \"ложное срабатывание: …\" "
           "--ticket <ID>` и прими с `--false-positive E-NNN`; настоящий — `result <ID> return`")


FP_TITLE = "ложное срабатывание"   # заголовок ошибки, по которой снимают отказ эвристики (так велит FP_HINT)


def repo_slug(args) -> str:
    """Репозиторий PR: `--repo`, иначе `ci_repo` из .claude/rpv.json; без догадки по cwd (PR живут не в этом репо)."""
    return (getattr(args, "repo", None) or P.setting("ci_repo", "") or "").strip()


def _false_positive(args, tk) -> tuple[str, str]:
    """`--false-positive E-NNN`: ошибка есть в реестре, заведена на этот тикет и названа «ложное срабатывание: …».
    → (отказ, номер); флага нет — ("", "")."""
    raw = (getattr(args, "false_positive", None) or "").strip()
    if not raw:
        return "", ""
    eid = errors_mod._norm(raw)
    path = errors_mod.errors(tk.PROJECT_ROOT).get(eid)
    if not path:
        return f"--false-positive {raw}: такой ошибки нет в реестре — сначала `errors.py add --title … --ticket {args.id}`", ""
    head = errors_mod.header(path)
    if head.get("ticket", "").strip().upper() != args.id.upper():
        return f"--false-positive {raw}: ошибка заведена не на {args.id} (`--ticket {args.id}` при `errors.py add`)", ""
    if FP_TITLE not in head.get("title", "").lower():
        return f"--false-positive {raw}: заголовок ошибки без «{FP_TITLE}» — это не отказ эвристики (`errors.py add --title \"{FP_TITLE}: …\"`)", ""
    return "", eid


def run(args, tk, collect_all: bool = False, proof: bool = True, heur: list | None = None) -> tuple[list[str], str, str]:
    """Гейты приёмки по порядку. → (отказы, пометки для лога, хвост для CEO). Без `collect_all` дорогие гейты (ярлыки) после отказа не гоняются — как было; с ним идут все. `proof=False` — ярлыки гонит вызывающий (cmd_accept).
    `--false-positive` снимает только отказ эвристики: `--tail -` со словом-хвостом в сдаче и `--numbers -` с числами в
    артефакте; нет файла, чужой номер тикета, число не сходится с источником — структурные отказы, остаются. В `heur`
    (если дан) — имена эвристик, чей отказ остался: по ним `dry_run` решает, нужна ли подсказка про `--false-positive`."""
    derr, ack = tk._decisions_ack(args)
    nerr, nack = tk._numbers_ack(args, args.id)
    terr, tack, tail_ceo = tk._tail_ack(args, args.id)
    lerr, lack = tk._live_ack(args, args.id)
    fperr, fp = _false_positive(args, tk)
    found = [name for name, err, given in (("числа", nerr, getattr(args, "numbers", None)), ("хвосты", terr, getattr(args, "tail", None)))
             if err and (given or "").strip() in HEURISTIC_ARGS]
    skipped = found if fp else []
    if heur is not None and not fp:
        heur.extend(found)
    nerr = "" if "числа" in skipped else nerr
    terr = "" if "хвосты" in skipped else terr
    errs = [fperr, derr, nerr, terr, tk._left_ack(args.id), lerr]
    perr, pack = ("", "") if any(errs) and not collect_all or not proof else tk._proof_ack(args.id)
    marks = f" ложное срабатывание: {fp} ({', '.join(skipped)})" if skipped else ""
    return [e for e in errs + [perr] if e], ack + nack + tack + lack + pack + marks, tail_ceo


def _pr_refusal(args, slug: str, head_err: str) -> str:
    """Отказ по PR в пробном прогоне: репозиторий не задан, нет головы (`--sha`), голова не та. Пусто — годно (или PR нет)."""
    import merge_rule
    if not args.pr:
        return ""
    if not slug:
        return "репозиторий не задан — --repo <владелец/репо> или ci_repo в .claude/rpv.json: PR не проверен"
    sha = (args.sha or "").lower()   # как в cmd_accept: регистр не важен, слиянию уходит нижний
    if not re.fullmatch(r"[0-9a-f]{7,40}", sha):
        return "" if head_err else "--sha — хеш головы PR (7–40 hex): голова PR не проверена"
    return merge_rule.check_pr(slug, int(args.pr), sha)


def dry_run(args, tk, slug: str = "", head_err: str = "") -> int:
    """`--dry-run`: все гейты за один раз, ничего не пишет. `head_err` — отказ входных данных `result` (неполная команда),
    `slug` — репозиторий PR (пусто при `--pr` — отказ «репозиторий не задан»). Проверка, что «не запустилась», — отдельная
    строка «не проверено, не return» (как у настоящей приёмки), а не регресс. 0 — приёмка пройдёт, 1 — список отказов."""
    found = [head_err, _pr_refusal(args, slug, head_err)]
    heur: list[str] = []
    errs, _, _ = run(args, tk, collect_all=True, heur=heur)
    found += errs
    wt = tk.PROJECT_ROOT / ".claude" / "worktrees" / args.id
    infra = []
    bad = errors_mod.run_all(tk.PROJECT_ROOT, wt if wt.is_dir() else tk.PROJECT_ROOT, infra=infra)
    if bad:
        found.append("регрессия — упали проверки исправленных ошибок: "
                     + "; ".join(f"{eid} «{title}»: {note.splitlines()[0] if note else ''}" for eid, title, note in bad[:5]))
    if infra:
        found.append("не проверено, не return — проверка не запустилась: "
                     + "; ".join(f"{eid} «{title}»: {note.splitlines()[0] if note else ''}" for eid, title, note in infra[:5])
                     + "; поставь `wait <ID> at:<время>` и повтори приёмку позже")
    found = [e for e in found if e]
    if not found:
        print(f"accept --dry-run {args.id}: отказов нет — приёмка пройдёт (ничего не записано)")
        return 0
    print(f"accept --dry-run {args.id}: отказов {len(found)} (ничего не записано):", file=sys.stderr)
    for i, e in enumerate(found, 1):
        print(f"{i}. {re.sub(chr(10), chr(10) + '   ', e)}", file=sys.stderr)
    if heur:   # подсказка — только когда остался отказ эвристики, а не любое «хвост»/«числ» в тексте отказа
        print(FP_HINT, file=sys.stderr)
    return 1


def dry_run_result(args, tk, role: str, tkt) -> int:
    """`result <ID> accept --dry-run`: неполная команда — тоже в списке; другие итоги пробного прогона не имеют."""
    if args.result != "accept":
        print("result --dry-run — только для accept", file=sys.stderr)
        return 1
    return dry_run(args, tk, repo_slug(args), tk._result_refusal(args, role, tkt))
