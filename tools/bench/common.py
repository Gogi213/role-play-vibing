"""Общее для замеров плагина (`tools/bench/`): окружение машины, песочница проекта, статистика, замер процесса.

Только stdlib, POSIX (Linux, macOS): `os.wait4` отдаёт процессорное время и пиковую память именно дочернего процесса.
Windows замеряется отдельно (`tools/e2e_clean_project.py`)."""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DISP = ROOT / ".claude" / "dispatcher"
HOOKS = ROOT / ".claude" / "hooks"
PY = sys.executable
GIT_ID = ["-c", "user.name=bench", "-c", "user.email=bench@example.invalid"]


def sh(cmd, cwd=None, env=None, timeout=None, input=None):
    return subprocess.run(cmd, cwd=str(cwd) if cwd else None, env=env, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout, input=input)


def clean_env(**extra) -> dict:
    """Окружение без чужих RPV_*: замер не должен зависеть от того, из какой сессии его запустили."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("RPV_") and k != "CLAUDE_PROJECT_DIR"}
    env.update({k: str(v) for k, v in extra.items()})
    return env


def env_info() -> dict:
    def first_line(cmd):
        try:
            return (sh(cmd, timeout=20).stdout or "").strip().splitlines()[0]
        except Exception:
            return ""
    mem = ""
    try:
        if sys.platform == "darwin":
            mem = f"{int(sh(['sysctl', '-n', 'hw.memsize']).stdout.strip()) / 2**30:.0f} GiB"
        else:
            kb = int(re.search(r"MemTotal:\s+(\d+)", Path("/proc/meminfo").read_text()).group(1))
            mem = f"{kb / 2**20:.0f} GiB"
    except Exception:
        pass
    distro = ""
    try:
        distro = re.search(r'PRETTY_NAME="([^"]+)"', Path("/etc/os-release").read_text()).group(1)
    except Exception:
        if sys.platform == "darwin":
            distro = "macOS " + platform.mac_ver()[0]
    return {
        "os": distro or platform.platform(), "kernel": platform.release(), "machine": platform.machine(),
        "cpus": os.cpu_count(), "memory": mem, "python": platform.python_version(),
        "git": first_line(["git", "--version"]), "claude": first_line([os.environ.get("CLAUDE_BIN") or "claude", "--version"]),
        "bash": first_line(["bash", "--version"]),
        "plugin_commit": first_line(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"]),
        "plugin_version": json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")).get("version", ""),
        "runner": os.environ.get("RUNNER_NAME") or ("github-hosted" if os.environ.get("GITHUB_ACTIONS") else "local"),
        "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }


# --- статистика ----------------------------------------------------------------------------------

def percentile(sorted_vals: list[float], q: float) -> float:
    """Перцентиль с линейной интерполяцией (как numpy по умолчанию); q в 0..1."""
    if not sorted_vals:
        return float("nan")
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = q * (len(sorted_vals) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def stats(samples: list[float], digits: int = 2) -> dict:
    s = sorted(float(x) for x in samples)
    if not s:
        return {"n": 0}
    return {"n": len(s), "min": round(s[0], digits), "p50": round(percentile(s, 0.5), digits),
            "p95": round(percentile(s, 0.95), digits), "max": round(s[-1], digits),
            "mean": round(statistics.fmean(s), digits), "stdev": round(statistics.pstdev(s), digits)}


# --- замер процесса ------------------------------------------------------------------------------

def run_measured(cmd, cwd=None, env=None, stdin_data: bytes | None = None, timeout: float = 120) -> dict:
    """Один запуск: wall (мс), CPU user+sys (мс), пиковая память (МиБ), код выхода, stdout. Память и CPU — именно этого
    процесса и его завершённых потомков (`wait4`), не всей машины."""
    import resource  # noqa: F401  (POSIX)
    out = tempfile.TemporaryFile()
    inp = subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL
    t0 = time.perf_counter()
    p = subprocess.Popen(cmd, cwd=str(cwd) if cwd else None, env=env, stdin=inp, stdout=out, stderr=subprocess.STDOUT)
    if stdin_data is not None:
        try:
            p.stdin.write(stdin_data)
            p.stdin.close()
        except BrokenPipeError:
            pass
    deadline = time.time() + timeout
    while True:
        pid, status, ru = os.wait4(p.pid, os.WNOHANG)
        if pid:
            break
        if time.time() > deadline:
            p.kill()
            pid, status, ru = os.wait4(p.pid, 0)
            break
        time.sleep(0.0005)
    wall = (time.perf_counter() - t0) * 1000
    rc = os.waitstatus_to_exitcode(status)
    p.returncode = rc
    out.seek(0)
    text = out.read().decode("utf-8", "replace")
    out.close()
    rss = ru.ru_maxrss / (2**20 if sys.platform == "darwin" else 2**10)  # macOS — байты, Linux — КиБ
    return {"wall_ms": wall, "cpu_ms": (ru.ru_utime + ru.ru_stime) * 1000, "rss_mib": rss, "rc": rc, "out": text}


def bench_cmd(cmd, n: int, warmup: int = 2, **kw) -> dict:
    """n запусков одной команды → {wall_ms, cpu_ms, rss_mib: stats, rc: последний код, rcs: множество кодов}."""
    for _ in range(warmup):
        run_measured(cmd, **kw)
    runs = [run_measured(cmd, **kw) for _ in range(n)]
    return {"wall_ms": stats([r["wall_ms"] for r in runs]), "cpu_ms": stats([r["cpu_ms"] for r in runs]),
            "rss_mib": stats([r["rss_mib"] for r in runs]), "rcs": sorted({r["rc"] for r in runs}),
            "last_out": runs[-1]["out"][-300:] if runs else ""}


# --- песочница проекта ---------------------------------------------------------------------------

def find_bash() -> str:
    b = shutil.which("bash")
    if not b:
        raise SystemExit("не найден bash")
    return b


def init_script() -> str:
    """Bash-блок из commands/rpv-init.md — проверяется сама команда плагина, а не её пересказ."""
    text = (ROOT / "commands" / "rpv-init.md").read_text(encoding="utf-8")
    block = re.search(r"```\n(.*?)\n```", text, re.S).group(1)
    return block.replace("${CLAUDE_PLUGIN_ROOT}", ROOT.as_posix())


def make_project(parent: Path | None = None, seed: dict | None = None, prefix: str = "rpv-bench-") -> tuple[Path, float]:
    """Чистый git-проект + `/rpv-init`. → (путь, секунд на init). `seed` — {путь: текст}, коммитятся до init."""
    proj = Path(tempfile.mkdtemp(prefix=prefix, dir=str(parent) if parent else None)).resolve()
    sh(["git", "init", "-q", "-b", "main"], proj)
    (proj / "README.md").write_text("# bench\n", encoding="utf-8")
    for rel, text in (seed or {}).items():
        p = proj / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    sh(["git", "add", "-A"], proj)
    sh(["git"] + GIT_ID + ["commit", "-qm", "init"], proj)
    t0 = time.perf_counter()
    r = sh([find_bash(), "-c", init_script()], proj)
    dt = time.perf_counter() - t0
    if r.returncode:
        raise RuntimeError(f"/rpv-init упал: {r.stderr[-400:]}")
    sh(["git", "add", "-A"], proj)
    sh(["git"] + GIT_ID + ["commit", "-qm", "rpv-init", "--allow-empty"], proj)
    return proj, dt


def cli(script: str, project: Path, *args, env=None, cwd=None, timeout=120):
    """Команда плагина (`tickets.py`, `registry.py`, …) в песочнице проекта."""
    return sh([PY, str(DISP / script), "--project", str(project), *map(str, args)], cwd=cwd or project,
              env=env or clean_env(), timeout=timeout)


def new_ticket(project: Path, owner: str, title: str, ref: str, desc: str, effort: str = "medium", env=None) -> str:
    r = cli("tickets.py", project, "new", "--owner", owner, "--title", title, "--ref", ref, "--effort", effort,
            "--desc", desc, env=env)
    tid = Path(r.stdout.strip().splitlines()[-1]).stem if r.stdout.strip() else ""
    if not tid.startswith("TK-"):
        raise RuntimeError(f"тикет не создан: {r.stdout!r} {r.stderr!r}")
    return tid


def ticket_state(project: Path, tid: str) -> tuple[str, str]:
    p = project / ".claude" / "tickets" / f"{tid}.md"
    if not p.exists():
        return "", ""
    text = p.read_text(encoding="utf-8")
    st = re.search(r"^status: *(\S+)", text, re.M)
    res = re.findall(r"^\[итог: ([\w-]+)\]", text, re.M)
    return (st.group(1) if st else ""), (res[-1] if res else "")


def du_bytes(path: Path) -> int:
    total = 0
    for dp, _, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(dp, f)).st_size
            except OSError:
                pass
    return total


def proc_tree_stats(pid: int) -> dict:
    """CPU-время (мс) и RSS (МиБ) живого процесса. Linux — /proc, macOS — ps."""
    try:
        if sys.platform != "darwin":
            st = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
            ticks = os.sysconf("SC_CLK_TCK")
            cpu = (int(st[11]) + int(st[12])) * 1000 / ticks
            rss = int(st[21]) * os.sysconf("SC_PAGE_SIZE") / 2**20
            return {"cpu_ms": cpu, "rss_mib": rss}
        out = sh(["ps", "-o", "rss=,time=", "-p", str(pid)]).stdout.split()
        rss = int(out[0]) / 1024
        t = out[1].replace(",", ".")
        parts = [float(x) for x in t.split(":")]
        secs = sum(v * 60 ** i for i, v in enumerate(reversed(parts)))
        return {"cpu_ms": secs * 1000, "rss_mib": rss}
    except Exception:
        return {}
