"""Замер оркестрации плагина без моделей: настоящий диспетчер, но вместо `claude` — заглушка (`stub_claude.py`).
Всё, что здесь измерено, — вклад ПЛАГИНА (опрос, паузы, замки, подхват), а не модели: на живом прогоне это «мёртвое время»
между ходами. Боевые константы не трогаются (период тика 15 с, паузы, пределы) — кроме явно названного `fast`."""
from __future__ import annotations

import json
import os
import random
import re
import signal
import subprocess
import time
from pathlib import Path

import common as C

BENCH = C.ROOT / "tools" / "bench"


class Rig:
    """Песочница: проект + заглушка + управляемый диспетчер."""

    def __init__(self, scenario: dict | None = None, poll_s: float | None = None, rpv: dict | None = None):
        self.proj, self.init_s = C.make_project()
        self.bindir = self.proj.parent / (self.proj.name + "-bin")
        self.bindir.mkdir()
        w = self.bindir / "claude"
        w.write_text(f'#!/bin/sh\nexec "{C.PY}" "{BENCH / "stub_claude.py"}" "$@"\n')
        w.chmod(0o755)
        self.cfg = self.bindir / "stub.json"
        self.set_scenario(scenario or {})
        extra = {"BENCH_POLL_S": poll_s} if poll_s else {}
        self.env = C.clean_env(RPV_PROJECT=self.proj, CLAUDE_BIN=w, BENCH_PID_NAME="python", BENCH_STUB_CFG=self.cfg, **extra)
        if rpv:
            (self.proj / ".claude" / "rpv.json").write_text(json.dumps(rpv), encoding="utf-8")
        C.cli("registry.py", self.proj, "goal", "--title", "bench", "--quote", "нужен файл", "--criterion", "файл есть",
              env=self.env)
        self.disp: subprocess.Popen | None = None
        self.logf = None

    def set_scenario(self, scenario: dict) -> None:
        self.cfg.write_text(json.dumps(scenario), encoding="utf-8")

    def ticket(self, title: str = "bench", owner: str = "engineer", force: bool = False) -> str:
        return C.new_ticket(self.proj, owner, title, "G-001", "Сдать файл в out/. Примут, если: файл есть.", env=self.env, force=force)

    def start(self) -> subprocess.Popen:
        self.logf = open(self.proj / "dispatch.out", "ab")
        self.disp = subprocess.Popen([C.PY, str(BENCH / "bench_dispatch.py"), "--project", str(self.proj)], cwd=self.proj,
                                     env=self.env, stdout=self.logf, stderr=subprocess.STDOUT)
        return self.disp

    def stop(self, kill: bool = False) -> None:
        if self.disp and self.disp.poll() is None:
            self.disp.send_signal(signal.SIGKILL if kill else signal.SIGTERM)
            try:
                self.disp.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.disp.kill()
        if self.logf:
            self.logf.close()
            self.logf = None

    def wait(self, tids, states=("done", "blocked", "needs_owner", "stopped"), timeout=300, step=0.1) -> dict:
        """{тикет: (статус, время достижения)} — когда каждый впервые стал в одно из `states`."""
        tids = [tids] if isinstance(tids, str) else list(tids)
        got, end = {}, time.time() + timeout
        while len(got) < len(tids) and time.time() < end:
            for t in tids:
                if t not in got:
                    st = C.ticket_state(self.proj, t)[0]
                    if st in states:
                        got[t] = (st, time.time())
            time.sleep(step)
        return got

    def events(self) -> list[dict]:
        f = self.proj / ".claude" / "dispatcher" / "stub-events.jsonl"
        return [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines() if x.strip()] if f.exists() else []

    def runs_log(self) -> list[dict]:
        f = self.proj / ".claude" / "dispatcher" / "runs.log"
        rows = []
        for ln in (f.read_text(encoding="utf-8").splitlines() if f.exists() else []):
            kv = dict(re.findall(r"(\w+)=(\S+)", ln))
            p = ln.split()
            rows.append({"tid": p[1], "role": p[2], **kv})
        return rows

    def inbox(self) -> str:
        f = self.proj / ".claude" / "dispatcher" / "ceo-inbox.md"
        return f.read_text(encoding="utf-8") if f.exists() else ""

    def close(self) -> None:
        self.stop(kill=True)
        import shutil
        time.sleep(0.3)
        shutil.rmtree(self.proj, ignore_errors=True)
        shutil.rmtree(self.bindir, ignore_errors=True)


def _spans(events: list[dict], tid: str, role: str) -> list[tuple[float, float | None]]:
    starts = [e["t"] for e in events if e["tid"] == tid and e["role"] == role and e["at"] == "start"]
    ends = [e["t"] for e in events if e["tid"] == tid and e["role"] == role and e["at"] == "end"]
    return [(s, ends[i] if i < len(ends) else None) for i, s in enumerate(starts)]


# --- сценарии ------------------------------------------------------------------------------------

def handoff_latency(n: int = 8, seed: int = 1) -> dict:
    """Один тикет за раз при работающем диспетчере; тикеты заводятся в случайной фазе 15-секундного тика. Заглушка мгновенна,
    значит всё, что видно, — задержка плагина: заведён → запущен Инженер, Инженер сдал → запущен Судья, Судья сдал → `done`."""
    rng = random.Random(seed)
    rig = Rig(scenario={"default": {"delay_s": [4, 24]}})  # ход длится 4–24 с: фаза тика, в которую он кончается, — любая
    try:
        rig.start()
        time.sleep(2)  # первый тик
        q, h, total = [], [], []
        for i in range(n):
            time.sleep(rng.uniform(0, 15))
            t_new = time.time()
            tid = rig.ticket(f"handoff {i}")
            got = rig.wait(tid, timeout=180)
            if tid not in got:
                return {"error": f"{tid} не дошёл до конца за 180 с", "done": i}
            ev = rig.events()
            e, j = _spans(ev, tid, "engineer"), _spans(ev, tid, "judge")
            q.append(e[0][0] - t_new)
            h.append(j[0][0] - e[0][1])
            total.append(got[tid][1] - t_new)
        return {"tickets": n, "queue_s": C.stats(q), "handoff_s": C.stats(h), "ticket_total_s": C.stats(total),
                "final_status": "done", "dispatch_stderr_tail": (rig.proj / "dispatch.out").read_text()[-200:]}
    finally:
        rig.close()


def return_cycle() -> dict:
    """Судья вернул работу один раз: сколько стоит круг «вернуть → переделать → принять» в мёртвом времени."""
    rig = Rig(scenario={"default": {"judge": "return1"}})
    try:
        rig.start()
        t0 = time.time()
        tid = rig.ticket("return cycle")
        got = rig.wait(tid, timeout=420)
        ev = rig.events()
        eng, jud = _spans(ev, tid, "engineer"), _spans(ev, tid, "judge")
        out = {"status": got.get(tid, ("?",))[0], "total_s": round(got[tid][1] - t0, 1) if tid in got else None,
               "engineer_runs": len(eng), "judge_runs": len(jud)}
        if len(jud) >= 1 and len(eng) >= 2:
            out["return_to_rework_s"] = round(eng[1][0] - jud[0][1], 1)
        if len(eng) >= 2 and len(jud) >= 2:
            out["rework_to_second_review_s"] = round(jud[1][0] - eng[1][1], 1)
        return out
    finally:
        rig.close()


def burst(n: int = 12, delay_s: float = 3.0, role_parallel: int | None = None) -> dict:
    """n тикетов разом; заглушка «думает» delay_s на ход. Сравнение с идеалом: все ходы n·2·delay_s / параллельность."""
    rpv = C.role_parallel_config(role_parallel) if role_parallel else None
    rig = Rig(scenario={"default": {"delay_s": delay_s}}, rpv=rpv)
    try:
        rig.start()
        time.sleep(2)
        t0 = time.time()
        tids = [rig.ticket(f"burst {i}", force=True) for i in range(n)]
        t_created = time.time() - t0
        got = rig.wait(tids, timeout=900)
        done = sorted(got[t][1] - t0 for t in got if got[t][0] == "done")
        ev = rig.events()
        peak = _peak_concurrency(ev)
        dispatch_threads = None
        return {"tickets": n, "delay_s": delay_s, "role_parallel": role_parallel or "умолчание", "done": len(done),
                "ticket_create_s": round(t_created, 2), "makespan_s": round(done[-1], 1) if done else None,
                "first_done_s": round(done[0], 1) if done else None, "per_ticket_done_s": C.stats(done),
                "peak_concurrent_runs": peak,
                "ideal_makespan_s": round(n * 2 * delay_s / max(peak, 1), 1),
                "runs_total": len([r for r in rig.runs_log()]), "blocked": len([t for t in got if got[t][0] != "done"]),
                "dispatch_stderr_tail": (rig.proj / "dispatch.out").read_text()[-200:], "_": dispatch_threads}
    finally:
        rig.close()


def _peak_concurrency(events: list[dict]) -> int:
    pts = []
    open_ = {}
    for e in events:
        k = (e["tid"], e["role"], e.get("pid"))
        if e["at"] == "start":
            pts.append((e["t"], 1))
            open_[(e["tid"], e["role"])] = e["t"]
        else:
            pts.append((e["t"], -1))
    cur = peak = 0
    for _, d in sorted(pts):
        cur += d
        peak = max(peak, cur)
    return peak


def crash_recovery() -> dict:
    """Критерий 15: диспетчер убит (SIGKILL) посреди хода Инженера; роль живёт отдельно. После перезапуска тикет должен дойти до
    `done`, ход Инженера — идти ровно один раз, а траты — записаться по разу на запуск."""
    rig = Rig(scenario={"default": {"delay_s": 12}})
    try:
        rig.start()
        t0 = time.time()
        tid = rig.ticket("crash recovery")
        while time.time() - t0 < 60 and not [e for e in rig.events() if e["at"] == "start"]:
            time.sleep(0.1)
        time.sleep(2)
        rig.stop(kill=True)
        t_kill = time.time()
        time.sleep(1)
        rig.start()
        got = rig.wait(tid, timeout=240)
        time.sleep(16)  # дать диспетчеру записать итог последнего запуска в runs.log
        ev = rig.events()
        eng, jud = _spans(ev, tid, "engineer"), _spans(ev, tid, "judge")
        runs = [r for r in rig.runs_log() if r["tid"] == tid]
        return {"status": got.get(tid, ("?",))[0], "recovery_to_done_s": round(got[tid][1] - t_kill, 1) if tid in got else None,
                "engineer_starts": len(eng), "judge_starts": len(jud), "runs_log_lines": len(runs),
                "engineer_run_lines": len([r for r in runs if r["role"] == "engineer"]),
                "double_charged": len([r for r in runs if r["role"] == "engineer"]) > 1}
    finally:
        rig.close()


def failure_modes() -> dict:
    """Что делает плагин, когда роль ведёт себя плохо: падает, молчит, спрашивает владельца, встаёт. Сигналы CEO и попытка
    отправки в Telegram пишет следующий тик (до 15 с) — ждём их до 25 с после конечного статуса и меряем задержку."""
    out = {}
    for name, scen in {
        "engineer_crash": {"default": {"engineer": "crash"}},
        "engineer_silent": {"default": {"engineer": "nowrite"}},
        "judge_blocked": {"default": {"judge": "blocked"}},
        "judge_asks_owner": {"default": {"judge": "ask-owner"}},
    }.items():
        rig = Rig(scenario=scen)
        try:
            rig.start()
            t0 = time.time()
            tid = rig.ticket(name)
            got = rig.wait(tid, timeout=360)
            t_final = got[tid][1] if tid in got else time.time()
            notify = rig.proj / ".claude" / "dispatcher" / "notify.log"
            inbox_at = tg_at = None
            while time.time() - t_final < 25 and (inbox_at is None or (tg_at is None and name == "judge_asks_owner")):
                if inbox_at is None and re.search(rf"\b{tid}\b", rig.inbox()):
                    inbox_at = time.time() - t_final
                if tg_at is None and notify.exists() and "Вопрос вам" in notify.read_text(encoding="utf-8"):
                    tg_at = time.time() - t_final
                time.sleep(0.25)
            kinds = re.findall(r"\[([\w-]+)\]", rig.inbox())
            out[name] = {"final_status": got.get(tid, ("не дошёл",))[0],
                         "to_final_s": round(t_final - t0, 1) if tid in got else None,
                         "runs": len([r for r in rig.runs_log() if r["tid"] == tid]),
                         "ceo_inbox_kinds": kinds, "ceo_inbox_delay_s": None if inbox_at is None else round(inbox_at, 1),
                         "telegram_attempt_delay_s": None if tg_at is None else round(tg_at, 1),
                         "notify_log_tail": notify.read_text(encoding="utf-8")[-200:] if notify.exists() else ""}
        finally:
            rig.close()
    return out


def idle_burn(seconds: int = 60, tickets: int = 100) -> dict:
    """Диспетчер и сторож простаивают с `tickets` закрытыми тикетами: CPU в процентах от одного ядра, рост памяти и каталога
    состояния. Для круглосуточной службы это стоимость «ничего не делать»."""
    rig = Rig()
    try:
        _synth_closed(rig.proj, tickets)
        rig.start()
        wp = subprocess.Popen([C.PY, str(C.DISP / "watch.py"), "--project", str(rig.proj)], cwd=rig.proj, env=rig.env,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(20)  # первые тики: разовые уведомления по старым тикетам
        a_d, a_w = C.proc_tree_stats(rig.disp.pid), C.proc_tree_stats(wp.pid)
        size_a = C.du_bytes(rig.proj / ".claude" / "dispatcher")
        t0 = time.time()
        time.sleep(seconds)
        dt = time.time() - t0
        b_d, b_w = C.proc_tree_stats(rig.disp.pid), C.proc_tree_stats(wp.pid)
        size_b = C.du_bytes(rig.proj / ".claude" / "dispatcher")
        wp.terminate()
        res = {"seconds": seconds, "tickets": tickets, "state_dir_growth_bytes": size_b - size_a}
        for name, a, b in (("dispatcher", a_d, b_d), ("watch", a_w, b_w)):
            if a and b:
                res[name] = {"cpu_pct_of_core": round((b["cpu_ms"] - a["cpu_ms"]) / (dt * 10), 3),
                             "rss_mib": round(b["rss_mib"], 1), "rss_drift_mib": round(b["rss_mib"] - a["rss_mib"], 2)}
        return res
    finally:
        rig.close()


def _synth_closed(proj: Path, n: int) -> None:
    """n закрытых тикетов (done, принято Судьёй) с живым на вид логом — для замеров масштаба. Файлы пишутся напрямую: через
    `tickets.py new` это сотни запусков процесса."""
    tdir = proj / ".claude" / "tickets"
    for i in range(1000, 1000 + n):
        tid = f"TK-{i}"
        log = []
        for k, (role, text) in enumerate([
            ("engineer", "[итог: done] результат: out/x.txt — написал модуль, тесты зелёные, прогон 4 с"),
            ("judge", "[итог: accept] проверка: out/x.txt решения: связанных нет числа: нет — по пунктам договора всё да"),
        ]):
            log.append(f"### 2026-10-0{1 + k % 9}T1{k}:00:00+00:00 {role}\n{text}\n")
        (tdir / f"{tid}.md").write_text(
            f"---\nid: {tid}\ntitle: синтетический {i}\nowner: engineer\nstatus: done\nreviewer: judge\neffort: medium\n"
            f"wait_for: \nupdated: 2026-10-02T12:00:00+00:00\nnext: \n---\n\nСсылка на задачу владельца: G-001\n\n"
            f"Описание синтетического тикета {i}: " + "слово " * 60 + "\n\n## Лог\n\n" + "\n".join(log), encoding="utf-8")


def telegram_delivery() -> dict:
    """Вопрос Судьи владельцу доходит до настоящего Telegram: от статуса `needs_owner` до записи «отправлено» в notify.log, и
    круг напрямую через `notify.py send`. Токен и чат — только из окружения процесса (`BENCH_TG_TOKEN`, `BENCH_TG_CHAT`), в
    файлы замера не попадают; сообщения помечены «[замер]». Нет переменных — раздел пропускается."""
    token, chat = os.environ.get("BENCH_TG_TOKEN"), os.environ.get("BENCH_TG_CHAT")
    if not (token and chat):
        return {"skipped": "нет BENCH_TG_TOKEN/BENCH_TG_CHAT"}
    rig = Rig(scenario={"default": {"judge": "ask-owner"}})
    rig.env.update(RPV_TG_TOKEN=token, RPV_TG_CHAT=chat)
    try:
        direct = C.run_measured([C.PY, str(C.DISP / "notify.py"), "--project", str(rig.proj), "send", "[замер] прямая отправка notify.py"],
                                cwd=rig.proj, env=rig.env, timeout=60)
        rig.start()
        t0 = time.time()
        tid = rig.ticket("[замер] вопрос владельцу")
        got = rig.wait(tid, states=("needs_owner",), timeout=240)
        t_ask = got[tid][1] if tid in got else None
        notify = rig.proj / ".claude" / "dispatcher" / "notify.log"
        sent_at = None
        while t_ask and time.time() - t_ask < 40:
            txt = notify.read_text(encoding="utf-8") if notify.exists() else ""
            if "отправлено: Вопрос вам" in txt:
                sent_at = time.time() - t_ask
                break
            time.sleep(0.25)
        log = notify.read_text(encoding="utf-8")[-300:] if notify.exists() else ""
        return {"direct_send_ms": round(direct["wall_ms"]), "direct_rc": direct["rc"], "direct_out": direct["out"].strip()[-80:],
                "needs_owner_after_s": round(t_ask - t0, 1) if t_ask else None,
                "question_delivered_after_s": None if sent_at is None else round(sent_at, 1),
                "notify_log_tail": log.replace(token, "<токен>")}
    finally:
        rig.close()
