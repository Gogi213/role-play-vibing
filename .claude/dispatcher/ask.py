#!/usr/bin/env python3
"""Вопросы владельцу через табло v2 (`VIEW2.md плагина`): вариант с последствием — владелец отвечает одной клавишей.

    ask.py new TK-044 --from инженер --on pc --text "Признак «цены не было»: только число или браковать сутки?" \\
        --opt "a|только число|запустит «прогон» на VPS" --opt "b|браковать сутки|…" [--default a] [--step 3] [--no-log]
    ask.py answer q-TK-044-1 a      # владелец ответил (клавиша в TUI / кнопка на странице / MCP pulse_answer)
    ask.py list [--all]

new: файл `.claude/pulse/questions/<id>.json`; `--default a` — вариант по умолчанию (рекомендация спросившего): на табло
помечен «(предлагаю)», Enter / кнопка «Ок» отвечает им; без `--default` ответ — только явным выбором варианта; процесс — тикет → ещё запись в его лог («ВОПРОС ВЛАДЕЛЬЦУ (табло)», без
`--next`); `--step n` — шаг n плана тикета становится «вы · ждёт» с этим вопросом; `--no-log` — без записи в тикет.
answer: помечает отвеченным; тикет → запись «Владелец (табло, …)» (+ `--next` ролью, которая спросила); строка в
`.claude/dispatcher/ceo-inbox.md` — CEO записывает решение. Само решение табло НЕ исполняет (исполняет CEO).
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pulsedata as P  # noqa: E402
import hide  # noqa: E402

FROM = {"инженер": ("инженер", "engineer"), "engineer": ("инженер", "engineer"),
        "исследователь": ("исследователь", "researcher"), "researcher": ("исследователь", "researcher"),
        "судья": ("судья", "judge"), "judge": ("судья", "judge"), "ceo": ("CEO", "ceo")}
TICKETS_PY = Path(__file__).resolve().parent / "tickets.py"


def clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def qpath(qid: str) -> Path:
    return P.questions_dir() / f"{qid}.json"


def ticket_comment(tk: str, author: str, text: str, nxt: str | None = None) -> str | None:
    """Запись в лог тикета через tickets.py (замок, сжатие лога — как у всех). None — ок, иначе текст ошибки."""
    cmd = [sys.executable, str(TICKETS_PY), "comment", tk, "--author", author, "--text", text]
    if nxt:
        cmd += ["--next", nxt]
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    try:
        r = hide.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, cwd=str(P.questions_dir().parents[2]))
    except Exception as e:  # TK-109 п.16: таймаут/сбой запуска — не исключение наружу, а текст ошибки (ответ повторится)
        return f"tickets.py comment {tk}: {type(e).__name__}: {str(e)[:150]}"
    return None if r.returncode == 0 else f"tickets.py comment {tk}: код {r.returncode}: {(r.stderr or r.stdout).strip()[:200]}"


def parse_opt(spec: str) -> dict:
    parts = [x.strip() for x in spec.split("|")]
    if len(parts) < 2 or not parts[0] or not parts[1]:
        raise ValueError(f"--opt «{spec}»: нужно «клавиша|вариант[|что будет]»")
    return {"key": parts[0].lower(), "label": parts[1], "effect": parts[2] if len(parts) > 2 else ""}


def next_id(process: str) -> str:
    best = 0
    for p in P.questions_dir().glob(f"q-{process}-*.json"):
        m = re.fullmatch(rf"q-{re.escape(process)}-(\d+)\.json", p.name)
        if m:
            best = max(best, int(m.group(1)))
    return f"q-{process}-{best + 1}"


def new_question(process: str, frm: str, on: str, text: str, opts: list[str], step: int | None = None,
                 log: bool = True, default: str | None = None) -> tuple[str, list[str]]:
    """→ (id, предупреждения). Исключение ValueError — вход негоден (ничего не записано)."""
    process = P.tk_id(process)
    if frm.lower() not in FROM:
        raise ValueError(f"--from: {', '.join(sorted(set(k for k in FROM if k.isascii())))} или по-русски, не «{frm}»")
    if on not in P.VALID_ON or on == "you":
        raise ValueError("--on: pc|vps|calc|col")
    options = [parse_opt(o) for o in opts]
    if len(options) < 2 or len({o["key"] for o in options}) != len(options):
        raise ValueError("нужно ≥ 2 --opt с разными клавишами")
    default = (default or "").strip().lower() or None
    if default is not None and default not in {o["key"] for o in options}:
        raise ValueError(f"--default «{default}»: нет такого варианта (есть: {', '.join(o['key'] for o in options)})")
    frm_ru, role = FROM[frm.lower()]
    qid = next_id(process)
    q = {"id": qid, "process": process, "from": frm_ru, "from_role": role, "on": on, "text": text.strip(),
         "options": options, "default": default, "step": step, "since": P.iso(), "answered_at": None, "answer": None}
    P.write_json(qpath(qid), q)
    warns: list[str] = []
    if step is not None:
        try:
            import plan as PL
            PL.set_step(process, step, "wait", who="вы", on="you", question=qid)
        except Exception as e:  # noqa: BLE001
            warns.append(f"шаг {step} не обновлён: {e}")
    if log and P.is_ticket(process):
        vs = "; ".join(f"{o['key']}) {o['label']}" + (" (предлагаю)" if o["key"] == default else "") +
                       (f" — {o['effect']}" if o["effect"] else "") for o in options)
        err = ticket_comment(process, role, f"ВОПРОС ВЛАДЕЛЬЦУ (табло): {q['text']} Варианты: {vs}.")
        if err:
            warns.append(err)
    return qid, warns


def new_notice(root: Path, source: str, text: str) -> str | None:
    """TK-094: машинный сигнал «ждёт вас» без тикета (выпуск плагина, сортировщик) — строка на Диспетчерской: вопрос с
    одним вариантом «принято» (`notice`), без записей в лог и ceo-inbox. Тот же текст ещё не принят — не дублируется."""
    qdir = Path(root) / ".claude" / "pulse" / "questions"
    text = text.strip()
    best = 0
    for p in qdir.glob(f"q-{source}-*.json"):
        m = re.fullmatch(rf"q-{re.escape(source)}-(\d+)\.json", p.name)
        best = max(best, int(m.group(1))) if m else best
        q = P.read_json(p)
        if isinstance(q, dict) and not q.get("answered_at") and q.get("text") == text:
            return None
    qid = f"q-{source}-{best + 1}"
    P.write_json(qdir / f"{qid}.json", {"id": qid, "process": source, "from": source, "from_role": None, "on": "pc",
                 "text": text, "options": [{"key": "a", "label": "принято", "effect": ""}], "default": None, "step": None,
                 "since": P.iso(), "answered_at": None, "answer": None, "notice": True})
    return qid


def _retry_log(qid: str, q: dict) -> tuple[bool, str, list[str]]:
    """Повтор недошедшей записи ответа в тикет. Таймаут мог случиться ПОСЛЕ записи — сначала ищем текст в тикете."""
    pid, text, role = q["process"], q["log_pending"], q.get("from_role")
    done = False
    try:
        import ticket as T
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import dispatch as D
        done = any(text in e.text for e in T.read_ticket(D.TICKETS_DIR / f"{pid}.md").log)
    except Exception:  # noqa: BLE001
        pass
    err = None if done else ticket_comment(pid, "ceo", text, role if role in ("engineer", "researcher", "judge") else None)
    if err:
        return True, f"{qid}: ответ записан, запись в тикет снова не дошла", [err]
    q.pop("log_pending", None)
    P.write_json(qpath(qid), q)
    return True, f"{qid}: запись ответа в тикет доставлена", []


def flush_pending() -> int:
    """TK-109 п.16: тик диспетчера досылает недошедшие записи ответов (log_pending); → сколько доставлено."""
    n = 0
    for p in sorted(P.questions_dir().glob("q-*.json")):
        q = P.read_json(p)
        if isinstance(q, dict) and q.get("answered_at") and q.get("log_pending") and P.is_ticket(q.get("process", "")):
            try:
                _retry_log(q["id"], q)
                n += not P.read_json(p).get("log_pending")
            except Exception as e:  # noqa: BLE001
                print(f"[ask] досылка {p.name}: {type(e).__name__}: {e}", file=sys.stderr)
    return n


def answer_question(qid: str, key: str) -> tuple[bool, str, list[str]]:
    """→ (ответ принят, сообщение, предупреждения). Принят — файл вопроса записан; сбои побочных записей — в предупреждениях."""
    q = P.read_json(qpath(qid))
    if not isinstance(q, dict):
        return False, f"нет вопроса {qid}", []
    if q.get("answered_at") and q.get("log_pending") and P.is_ticket(q.get("process", "")):
        return _retry_log(qid, q)  # TK-109 п.16: ответ записан, а запись в тикет не дошла — повтор без нового ответа
    if q.get("answered_at"):
        return False, f"вопрос {qid} уже отвечен: «{q.get('answer_label') or q.get('answer')}»", []
    opt = next((o for o in q["options"] if o["key"] == str(key).strip().lower()), None)
    if opt is None:
        return False, f"{qid}: нет варианта «{key}» (есть: {', '.join(o['key'] for o in q['options'])})", []
    now = P.now_dt()
    q.update(answered_at=P.iso(now), answer=opt["key"], answer_label=opt["label"])
    P.write_json(qpath(qid), q)
    msgs: list[str] = []
    if q.get("notice"):  # сигнал машины: принято — и всё, CEO не будим
        return True, f"{qid}: принято", msgs
    pid, hh = q["process"], now.strftime("%H:%M")
    if q.get("step"):  # шаг «вы» → готово
        try:
            import plan as PL
            plan = P.read_json(PL.plan_path(pid)) or {}
            st = plan.get("steps", [])[q["step"] - 1]
            if st.get("question") == qid:
                PL.set_step(pid, q["step"], "done", f"ответ: {opt['label']}")
        except Exception as e:  # noqa: BLE001
            msgs.append(f"шаг плана не обновлён: {e}")
    role = q.get("from_role")
    if P.is_ticket(pid):
        text = f"Владелец (табло, {hh}): ответ «{opt['label']}» на вопрос «{q['text']}»."
        err = ticket_comment(pid, "ceo", text, role if role in ("engineer", "researcher", "judge") else None)
        if err:
            msgs.append(err)
            q["log_pending"] = text
            P.write_json(qpath(qid), q)
    try:  # CEO записывает решение; прямой дописью (диспетчер может стоять), kind не «summary» → будит
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import dispatch as D
        eff = f" ({opt['effect']})" if opt.get("effect") else ""
        tail = "CEO: записать решение" + ("" if P.is_ticket(pid) else " и исполнить")
        D.append_ceo_inbox(pid, "owner-answer", f"Владелец (табло, {hh}): ответ «{opt['label']}»{eff} на вопрос "
                           f"«{clip(q['text'], 160)}» (спросил: {q['from']}) — {tail}", now)
    except Exception as e:  # noqa: BLE001
        msgs.append(f"ceo-inbox не дописан: {type(e).__name__}: {e}")
    return True, f"{qid}: ответ «{opt['label']}» записан", msgs


def cmd_new(a) -> int:
    try:
        qid, warns = new_question(a.process, a.from_, a.on, a.text, a.opt, a.step, not a.no_log, a.default)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2
    print(qid)
    for w in warns:
        print("⚠ " + w, file=sys.stderr)
    return 2 if warns else 0


def cmd_answer(a) -> int:
    ok, msg, warns = answer_question(a.id, a.key)
    print(msg, file=sys.stdout if ok else sys.stderr)
    for w in warns:
        print("⚠ " + w, file=sys.stderr)
    return (2 if warns else 0) if ok else 1


def cmd_list(a) -> int:
    qs = [q for q in (P.read_json(p) for p in sorted(P.questions_dir().glob("q-*.json"))) if isinstance(q, dict)]
    qs.sort(key=lambda q: q.get("since") or "")
    for q in qs:
        if q.get("answered_at") and not a.all:
            continue
        ans = f" → {q.get('answer')} ({q.get('answer_label')})" if q.get("answered_at") else ""
        print(f"{q['id']} [{q['process']}] {q['from']}→вы {P.hhmm(q['since'])}: {q['text']}{ans}")
        for o in q["options"]:
            print(f"    {o['key']}) {o['label']}" + (" (предлагаю)" if o["key"] == q.get("default") else "") +
                  (f" — {o['effect']}" if o.get("effect") else ""))
    return 0


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(prog="ask.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("new")
    s.add_argument("process")
    s.add_argument("--from", dest="from_", required=True)
    s.add_argument("--on", default="pc")
    s.add_argument("--text", required=True)
    s.add_argument("--opt", action="append", default=[])
    s.add_argument("--step", type=int, default=None)
    s.add_argument("--default", default=None, help="клавиша варианта по умолчанию («предлагаю»)")
    s.add_argument("--no-log", action="store_true")
    s.set_defaults(func=cmd_new)
    s = sub.add_parser("answer")
    s.add_argument("id")
    s.add_argument("key")
    s.set_defaults(func=cmd_answer)
    s = sub.add_parser("list")
    s.add_argument("--all", action="store_true")
    s.set_defaults(func=cmd_list)
    a = ap.parse_args(argv)
    return a.func(a)


if __name__ == "__main__":
    raise SystemExit(main())
