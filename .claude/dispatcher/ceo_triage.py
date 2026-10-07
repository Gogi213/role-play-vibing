"""Сортировщик сигналов CEO (TK-086): CEO без долгой сессии и без будильников.

Событие очереди `ceo` (или строка ceo-inbox.md без шины) разбирается так:
  1. правила без ИИ — дубль за час, тикет уже закрыт, план-сигналы (дело владельца тикета), orphan при годном
     wait_for, bus-up → шум; blocked/needs-owner/ask-owner → действие (безопасная сторона, без модели);
  2. остальное — один вызов Haiku 5.5 на пачку (`--effort low`): действие CEO / сведения / шум + причина;
     модель не ответила или ответ не разобран → действие (пропуск сигнала дороже лишнего запуска);
  3. шум — ack и запись в triage.jsonl; сведения — строка в ceo-digest.md (её показывает хук UserPromptSubmit
     на сообщении владельца); действие — один запуск CEO как роли (claude -p со свежим малым контекстом,
     RPV_ROLE=ceo, устав .claude/roles/ceo.md), по одному за раз.
Каждая запись triage.jsonl хранит класс, причину и токены Haiku — по ним считается «до/после» и число ложных побудок.

    python <плагин>/.claude/dispatcher/ceo_triage.py --project <проект> --once   # один проход
    python <плагин>/.claude/dispatcher/ceo_triage.py --project <проект>          # цикл раз в CEO_TRIAGE_INTERVAL_S
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dispatch as D  # noqa: E402 — пути, шина, запуск claude
import ticket as T  # noqa: E402

HAIKU_MODEL = os.environ.get("CEO_TRIAGE_MODEL", "claude-haiku-5-5")
INTERVAL_S = float(os.environ.get("CEO_TRIAGE_INTERVAL_S", "30"))
DUP_WINDOW_S = float(os.environ.get("CEO_TRIAGE_DUP_S", "3600"))
CEO_RUN_TIMEOUT_S = float(os.environ.get("CEO_TRIAGE_RUN_TIMEOUT_S", str(20 * 60)))

NOISE, INFO, ACTION = "noise", "info", "action"
ALWAYS_ACTION = {"blocked", "needs-owner", "needs_owner", "ask-owner", "watch-blocked", "watch-needs-owner"}
PLAN_KINDS = {"watch-no-plan", "watch-plan-stale", "watch-plan-stale-waiting"}
INFO_KINDS = {"watch-summary", "summary", "model", "done", "bus-down"}
NOISE_KINDS = {"bus-up"}
_TID = re.compile(r"TK-\d+")
_ISO = re.compile(r"\d{4}-\d\d-\d\dT[\d:.+-]+|\b\d\d:\d\d(?::\d\d)?\b|#\d+")

PROMPT = (
    "Ты сортируешь сигналы диспетчера команды ролей для CEO. Для каждого события дай класс: "
    "action (CEO обязан что-то решить или сделать: тикет встал, нужен ответ владельцу, сбой машины или диспетчера), "
    "info (CEO знать полезно, но делать нечего: завершено, сводка, ожидаемое состояние), "
    "noise (повтор, уже разобрано, ложная тревога). Сомневаешься между action и info — action. "
    "Ответь ТОЛЬКО JSON-массивом той же длины и порядка: [{\"c\":\"action|info|noise\",\"why\":\"≤ 12 слов\"}]. События:\n"
)


def _paths() -> dict:
    d = D.DISPATCHER_DIR
    return {"log": d / "triage.jsonl", "digest": d / "ceo-digest.md", "state": d / "triage-state.json",
            "pid": d / "triage-ceo.pid", "seen": d / ".triage-inbox-seen"}


# --- правила без ИИ --------------------------------------------------------------------------

def _norm(note: str) -> str:
    return _ISO.sub("", note or "").strip().lower()[:200]


def rule_class(ev: dict, recent: dict, now: datetime) -> tuple | None:
    """(класс, причина) по правилам либо None — тогда решает Haiku. recent: подпись → время последнего показа."""
    kind, tid, note = ev["kind"], ev["tid"], ev["note"]
    if kind in NOISE_KINDS:
        return NOISE, "служебное: шина поднялась"
    if kind in PLAN_KINDS:
        return NOISE, "план шагов — дело владельца тикета (TK-079)"
    sig = f"{tid}|{kind}|{_norm(note)}"
    last = recent.get(sig)
    if last and (now - T.parse_dt(last)).total_seconds() < DUP_WINDOW_S:
        return NOISE, "дубль за последний час"
    m = _TID.search(tid) or _TID.search(note)
    tkt = None
    if m:
        try:
            tkt = T.read_ticket(D.TICKETS_DIR / f"{m.group(0)}.md")
        except Exception:
            tkt = None
    if kind in ALWAYS_ACTION:
        return ACTION, "задача встала / вопрос владельцу"
    if tkt is not None and kind not in ("done", "next-ceo"):
        if tkt.status in ("done", "cancelled"):
            return NOISE, f"{tkt.id} уже закрыт"
        if "orphan" in kind and tkt.status == "waiting" and (tkt.header.get("wait_for") or "").strip():
            return NOISE, "orphan при годном wait_for"
    if kind in INFO_KINDS:
        return INFO, "сведения"
    return None


# --- Haiku -------------------------------------------------------------------------------------

def haiku_classify(events: list, claude_bin: str = None) -> tuple:
    """([(класс, причина)…], usage). Любая ошибка → всё action (безопасная сторона)."""
    fallback = [(ACTION, "Haiku не ответил — безопасная сторона")] * len(events)
    if not events:
        return [], {}
    lines = "\n".join(f"{i + 1}. {e['tid']} [{e['kind']}] {e['note'][:300]}" for i, e in enumerate(events))
    env = dict(os.environ)
    for k in list(env):
        if "HOST_SESSION" in k.upper():
            env.pop(k, None)
    env["RPV_ROLE"] = env["ALPHA_ROLE"] = "triage"
    try:
        # cwd — пустой временный каталог: сортировщику не нужен проект, и он не подтягивает CLAUDE.md проекта
        with tempfile.TemporaryDirectory() as cwd:
            p = subprocess.run([claude_bin or D.CLAUDE_BIN, "-p", PROMPT + lines, "--model", HAIKU_MODEL,
                                "--effort", "low", "--output-format", "json"], capture_output=True, text=True,
                               encoding="utf-8", timeout=120, env=env, cwd=cwd)
        out = json.loads(p.stdout)
        text = out.get("result") or ""
        arr = json.loads(text[text.index("["):text.rindex("]") + 1])
        res = []
        for item in arr[:len(events)]:
            c = item.get("c")
            res.append((c if c in (NOISE, INFO, ACTION) else ACTION, str(item.get("why", ""))[:120]))
        if len(res) != len(events):
            return fallback, out.get("usage") or {}
        return res, out.get("usage") or {}
    except Exception:
        return fallback, {}


# --- источники и приёмники -------------------------------------------------------------------

def read_events() -> list:
    """Новые события: очередь ceo шины (без ack) либо строки ceo-inbox.md после отметки."""
    out = []
    if D._bus_configured():
        import busclient
        after = 0
        while True:
            batch = busclient.request(f"/q/ceo?after={after}&wait=0", timeout=5)["events"]
            fresh = [e for e in batch if e["seq"] > after]
            if not fresh:
                break
            for e in fresh:
                pl = e.get("payload") or {}
                tid = e["addr"].split(".")[1] if e["addr"].startswith("задача.") else e["addr"]
                out.append({"seq": e["seq"], "tid": tid, "kind": pl.get("kind", e["addr"]),
                            "note": str(pl.get("note", pl)), "src": "bus"})
            after = max(e["seq"] for e in fresh)
        return out
    seen_f = _paths()["seen"]
    try:
        seen = int(seen_f.read_text(encoding="utf-8").strip() or 0)
    except (OSError, ValueError):
        seen = 0
    try:
        lines = D.CEO_INBOX.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for n, ln in enumerate(lines[seen:], start=seen + 1):
        m = re.match(r"- (\S+) (\S+) \[([^\]]+)\](?: \[запасной путь\])? (.*)", ln)
        if m:
            out.append({"seq": n, "tid": m.group(2), "kind": m.group(3), "note": m.group(4), "src": "file"})
        else:
            out.append({"seq": n, "tid": "*", "kind": "raw", "note": ln, "src": "file"})
    return out


def ack(events: list) -> None:
    bus = [e["seq"] for e in events if e["src"] == "bus"]
    if bus:
        import busclient
        busclient.request("/ack", {"recipient": "ceo", "seqs": bus}, timeout=5)
    files = [e["seq"] for e in events if e["src"] == "file"]
    if files:
        _paths()["seen"].write_text(str(max(files)), encoding="utf-8")


def ceo_run_alive() -> bool:
    try:
        pid = int(_paths()["pid"].read_text(encoding="utf-8").split()[0])
    except (OSError, ValueError, IndexError):
        return False
    return D._pid_alive(pid)


def launch_ceo(events: list, now: datetime) -> bool:
    """Один запуск CEO-роли на пачку действий (свежий малый контекст); пока прошлый жив — не запускаем."""
    if ceo_run_alive():
        return False
    p = _paths()
    D.RUNS_DIR.mkdir(parents=True, exist_ok=True)
    stem = D.RUNS_DIR / f"{now.strftime('%Y%m%d-%H%M%S')}-ceo-triage"
    sig = "\n".join(f"- {e['tid']} [{e['kind']}] ({e['why']}) {e['note'][:400]}" for e in events)
    prompt = (f"Ты — CEO команды (запуск диспетчера по сигналам, не интерактивная сессия). Устав — .claude/roles/ceo.md, "
              f"блокнот — .claude/roles/notes/ceo.md; прочитай их и разбери сигналы ниже, действуй по уставу. "
              f"Общую память проекта пишешь только ты. Ответ владельцу не нужен: итог — в блокнот и в лог тикетов. "
              f"Лимит запуска {int(CEO_RUN_TIMEOUT_S // 60)} мин.\nСигналы:\n{sig}")
    env = dict(os.environ)
    for k in list(env):
        if "HOST_SESSION" in k.upper():
            env.pop(k, None)
    env["RPV_ROLE"] = env["ALPHA_ROLE"] = "ceo"
    env["RPV_PROJECT"] = str(D.PROJECT_ROOT)
    model = D.ROLE_MODEL.get("ceo", D.CLAUDE_MODEL)
    cmd = [D.CLAUDE_BIN, "-p", prompt, "--output-format", "json", "--permission-mode", "bypassPermissions",
           "--model", model, "--effort", "medium", "--session-id", str(uuid.uuid4())]
    popen = subprocess.Popen(cmd, cwd=str(D.PROJECT_ROOT), env=env, stdout=open(f"{stem}.json", "w", encoding="utf-8"),
                             stderr=open(f"{stem}.err.log", "w", encoding="utf-8"), text=True, start_new_session=True)
    p["pid"].write_text(f"{popen.pid} {T.now_iso(now)}", encoding="utf-8")
    return True


def _log(rec: dict) -> None:
    with open(_paths()["log"], "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def run_once(now: datetime = None, classify=haiku_classify, launch=launch_ceo) -> dict:
    now = now or datetime.now().astimezone()
    p = _paths()
    events = read_events()
    try:
        st = json.loads(p["state"].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        st = {}
    recent = st.setdefault("recent", {})
    result = {NOISE: [], INFO: [], ACTION: []}
    undecided = []
    for e in events:
        r = rule_class(e, recent, now)
        if r:
            e["cls"], e["why"], e["by"] = r[0], r[1], "rule"
        else:
            undecided.append(e)
        if e.get("cls") != NOISE:
            recent[f"{e['tid']}|{e['kind']}|{_norm(e['note'])}"] = T.now_iso(now)
    usage = {}
    if undecided:
        res, usage = classify(undecided)
        for e, (c, why) in zip(undecided, res):
            e["cls"], e["why"], e["by"] = c, why, "haiku"
            recent[f"{e['tid']}|{e['kind']}|{_norm(e['note'])}"] = T.now_iso(now)
    actions = [e for e in events if e["cls"] == ACTION]
    for e in events:
        result[e["cls"]].append(e)
    infos = [e for e in events if e["cls"] == INFO]
    if infos:
        with open(p["digest"], "a", encoding="utf-8") as fh:
            for e in infos:
                fh.write(f"- {T.now_iso(now)} {e['tid']} [{e['kind']}] {e['note'][:300]} — {e['why']}\n")
    launched = False
    if actions:
        launched = launch(actions, now)
    if actions and not launched:
        events = [e for e in events if e["cls"] != ACTION]  # CEO-роль занята: действия остаются в очереди до её конца
    ack(events)
    for e in events:
        _log({"ts": T.now_iso(now), "seq": e["seq"], "tid": e["tid"], "kind": e["kind"], "cls": e["cls"],
              "by": e["by"], "why": e["why"], "ceo_run": launched and e["cls"] == ACTION})
    if usage:
        _log({"ts": T.now_iso(now), "haiku_usage": usage, "events": len(undecided)})
    cutoff = now.timestamp() - DUP_WINDOW_S
    st["recent"] = {k: v for k, v in recent.items() if T.parse_dt(v).timestamp() > cutoff}
    p["state"].write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    return {"noise": len(result[NOISE]), "info": len(result[INFO]), "action": len(actions), "launched": launched}


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--project" in argv:
        i = argv.index("--project")
        D.configure_project(argv[i + 1])
    once = "--once" in argv
    while True:
        try:
            r = run_once()
            if r["noise"] or r["info"] or r["action"]:
                print(f"[triage] {r}", flush=True)
        except Exception as e:
            print(f"[triage] {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        if once:
            return 0
        time.sleep(INTERVAL_S)


if __name__ == "__main__":
    sys.exit(main())
