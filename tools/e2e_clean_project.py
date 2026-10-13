#!/usr/bin/env python3
"""Проверка CP11 (критерий 17 «новый проект подключается за минуты»): чистый проект → `/rpv-init` → цель владельца →
тикет → роли под диспетчером → приёмка Судьи. Меряет время «цель → принятый результат» и цену запусков. Тратит деньги
(настоящие `claude -p`: Инженер и Судья), поэтому не тест pytest — запуск руками.

    python tools/e2e_clean_project.py [--timeout-min 40] [--keep]

Роли идут с кодом плагина из этой рабочей копии (`RPV_PLUGIN_DIR` → `claude --plugin-dir`), установленный плагин и его
проекты не затрагиваются. Итог — таблица в stdout и `<проект>/e2e-result.json`; каталог проекта печатается (`--keep` —
не удалять).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DISP = ROOT / ".claude" / "dispatcher"
GOAL_QUOTE = "нужен скрипт hello.py, который печатает слово привет"
CRITERION = "python hello.py печатает «привет»"


def sh(cmd, cwd, **kw):
    return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace", **kw)


def init_script() -> str:
    """Bash-блок из commands/rpv-init.md — проверяется сама команда, а не её пересказ."""
    text = (ROOT / "commands" / "rpv-init.md").read_text(encoding="utf-8")
    block = re.search(r"```\n(.*?)\n```", text, re.S).group(1)
    return block.replace("${CLAUDE_PLUGIN_ROOT}", ROOT.as_posix())


def ticket_state(proj: Path, tid: str) -> tuple[str, str]:
    p = proj / ".claude" / "tickets" / f"{tid}.md"
    if not p.exists():
        return "", ""
    text = p.read_text(encoding="utf-8")
    st = re.search(r"^status: *(\S+)", text, re.M)
    results = re.findall(r"^\[итог: ([\w-]+)\]", text, re.M)
    return (st.group(1) if st else ""), (results[-1] if results else "")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--timeout-min", type=float, default=40)
    ap.add_argument("--keep", action="store_true")
    a = ap.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    proj = Path(tempfile.mkdtemp(prefix="rpv-e2e-")).resolve()
    py = sys.executable
    env = {k: v for k, v in os.environ.items() if not k.startswith("RPV_")}
    env.update(RPV_PLUGIN_DIR=str(ROOT), RPV_DISPATCH_INTERVAL="5", RPV_PROJECT=str(proj))
    marks, out = {}, {"project": str(proj)}
    t0 = time.time()
    try:
        sh(["git", "init", "-q"], proj)
        (proj / "README.md").write_text("# e2e\n", encoding="utf-8")
        sh(["git", "add", "-A"], proj)
        sh(["git", "-c", "user.name=e2e", "-c", "user.email=e2e@example.invalid", "commit", "-qm", "init"], proj)
        r = sh(["bash", "-c", init_script()], proj)
        marks["init_s"] = round(time.time() - t0, 1)
        out["init_out"] = r.stdout.strip().splitlines()[-3:]
        if r.returncode:
            raise SystemExit(f"/rpv-init упал: {r.stderr[-500:]}")
        reg = [py, str(DISP / "registry.py"), "--project", str(proj)]
        sh(reg + ["goal", "--title", "hello.py печатает привет", "--quote", GOAL_QUOTE, "--criterion", CRITERION], proj, env=env)
        r = sh([py, str(DISP / "tickets.py"), "--project", str(proj), "new", "--owner", "engineer", "--title",
                "hello.py печатает «привет»", "--ref", "G-001", "--effort", "medium", "--desc",
                f"Сдать: файл hello.py в корне проекта, закоммичен. Примут, если: {CRITERION}. Не входит: всё остальное."],
               proj, env=env)
        tid = Path(r.stdout.strip()).stem
        if not tid.startswith("TK-"):
            raise SystemExit(f"тикет не создан: {r.stdout} {r.stderr}")
        marks["ticket_s"] = round(time.time() - t0, 1)
        log = open(proj / "e2e-dispatch.log", "w", encoding="utf-8")
        disp = subprocess.Popen([py, str(DISP / "dispatch.py"), "--project", str(proj)], cwd=str(proj), env=env,
                                stdout=log, stderr=subprocess.STDOUT)
        deadline = time.time() + a.timeout_min * 60
        status, last = "", ""
        while time.time() < deadline:
            time.sleep(10)
            status, last = ticket_state(proj, tid)
            if status in ("done", "blocked", "needs_owner", "stopped"):
                break
        disp.terminate()
        try:
            disp.wait(timeout=30)
        except subprocess.TimeoutExpired:
            disp.kill()
        marks["end_s"] = round(time.time() - t0, 1)
        accepted = status == "done" and "accept" in (proj / ".claude" / "tickets" / f"{tid}.md").read_text(encoding="utf-8")
        hello = sh([py, "hello.py"], proj).stdout.strip() if (proj / "hello.py").exists() else ""
        runs = (proj / ".claude" / "dispatcher" / "runs.log")
        lines = runs.read_text(encoding="utf-8").splitlines() if runs.exists() else []
        cost = sum(float(m.group(1)) for ln in lines if (m := re.search(r"cost_usd=([\d.]+)", ln)))
        out.update(ticket=tid, status=status, last_result=last, accepted_by_judge=accepted, hello_output=hello,
                   runs=len(lines), cost_usd=round(cost, 3), roles=sorted({ln.split()[2] for ln in lines if len(ln.split()) > 2}),
                   seconds=marks, ok=bool(accepted and "привет" in hello),
                   at=datetime.now().astimezone().isoformat(timespec="seconds"))
        (proj / "e2e-result.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0 if out["ok"] else 1
    finally:
        if not a.keep and out.get("ok"):
            shutil.rmtree(proj, ignore_errors=True)
        else:
            print(f"проект оставлен: {proj}")


if __name__ == "__main__":
    sys.exit(main())
