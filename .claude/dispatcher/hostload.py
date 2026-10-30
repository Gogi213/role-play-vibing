"""TK-070 (G-009): процессы расчёта по замеру пика памяти; повтор после OOM-kill без нового замера не принимается.

Поверх записи пробы (`probe.py`, флаг `require_probe`): пик памяти одного процесса на тяжёлом элементе и N процессов.
`wait` для `host:`/`job:` сверяет их со свободными ресурсами машины: команда проекта `host_free_cmd` (по ssh на машине алиаса,
шаблон может содержать `{ticket}`) печатает первой строкой `<свободные ядра> <свободные ГБ>`. N больше ядер или
N × пик × `host_mem_reserve` (умолч. 1,2) больше памяти — отказ с `N_max = min(ядра, память ÷ (пик × запас))`; N меньше
`N_max` — в журнал строка «можно больше». Ключа `host_free_cmd` нет — проверяется только форма записи.
Сторож (`watch.note_oom`) кладёт отметку `oom-state.json`, когда задание тикета упало с OOM-kill: следующий `wait` тикета
требует `--probe` с пиком, отличным от записанного, или меньшим N — иначе отказ. Слов конкретного проекта нет.
"""
from __future__ import annotations

import json
import re
import shlex
from datetime import datetime
from pathlib import Path

import hide
import probe as PROBE
import project as P
import project_paths as CFG
import ticket as T

RESERVE = 1.2
OOM_NAME = "oom-state.json"
_OOM_RE = re.compile(r"oom[-_ ]?kill|out of memory|memoryerror|cannot allocate memory", re.I)
_NEG_RE = re.compile(r"(?:\bнет|\bне|\bnot|\bno|\bбез)\W{0,3}$", re.I)  # «не oom-kill», «нет oom-kill», «not out of memory»
_NUMS = re.compile(r"\d+(?:[.,]\d+)?")


def reserve() -> float:
    """Запас по памяти: ключ `host_mem_reserve`, не меньше 1 (в rpv.json могли вписать руками); не число — умолчание."""
    try:
        return max(1.0, float(P.setting("host_mem_reserve", RESERVE)))
    except (TypeError, ValueError):
        return RESERVE


def n_max(cores: int, mem_gb: float, peak: float, res: float) -> int:
    """Сколько процессов выдержит машина: меньшее из свободных ядер и памяти ÷ (пик × запас)."""
    if peak * res <= 0:  # пика нет (≤ 0) — память не ограничивает, остаются ядра
        return max(0, int(cores))
    return max(0, min(int(cores), int(mem_gb // (peak * res))))


def free(alias: str, tid: str, ssh_cmd, timeout: float = 25.0):
    """(ядра, ГБ) свободных на машине алиаса по `host_free_cmd`; не задана / не ответила / не разобрать — None."""
    tpl = P.setting("host_free_cmd")
    if not tpl:
        return None
    try:
        r = hide.run(ssh_cmd(alias, tpl.replace("{ticket}", shlex.quote(tid))), capture_output=True, timeout=timeout)
    except Exception:
        return None
    lines = [ln for ln in (r.stdout or b"").decode("utf-8", "replace").splitlines() if ln.strip()]
    nums = _NUMS.findall(lines[0]) if lines and r.returncode == 0 else []
    return (int(float(nums[0].replace(",", "."))), float(nums[1].replace(",", "."))) if len(nums) >= 2 else None


# --- отметка OOM-kill -------------------------------------------------------------------------------------------
def _oom_path() -> Path:
    return Path(CFG.DISPATCHER_DIR) / OOM_NAME


def _oom_read() -> dict:
    try:
        return json.loads(_oom_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def oom_mark(tid: str):
    """Отметка OOM по тикету: {"peak": ГБ|None, "n": int|None, "at": ISO} или None."""
    return _oom_read().get(tid)


def is_oom(text: str) -> bool:
    """В тексте есть OOM-kill / нехватка памяти без отрицания перед ним («не oom-kill»)."""
    return any(not _NEG_RE.search(text[max(0, m.start() - 8):m.start()]) for m in _OOM_RE.finditer(text or ""))


def note_oom(tkt, why: str, now: datetime = None) -> bool:
    """Сторож: `why` — причина падения самого задания тикета (поле адаптера или хвост его лога, не текст пробуждения):
    в ней OOM-kill — запомнить пик и N из шапки на тот момент."""
    if not PROBE.enabled() or not is_oom(why):
        return False
    peak, n = PROBE.load_of_header(tkt) or (None, None)
    data = _oom_read()
    data[tkt.id] = {"peak": peak, "n": n, "at": (now or datetime.now().astimezone()).isoformat(timespec="seconds")}
    try:
        P.write_json(_oom_path(), data, sort_keys=True)
    except OSError:
        return False  # отметка не легла — сторож из-за неё не падает
    return True


def clear_oom(tid: str) -> None:
    data = _oom_read()
    if data.pop(tid, None) is not None:
        P.write_json(_oom_path(), data, sort_keys=True)


def _oom_problem(mark: dict, new) -> str:
    if new is None:
        return "нужен `--probe` с «тяжёлый: … пик …» и «N: …»"
    old_peak, old_n = mark.get("peak"), mark.get("n")
    if (old_peak is None or round(new[0], 2) != round(old_peak, 2)) or (old_n is not None and new[1] < old_n):
        return ""
    return f"новый пик (был {old_peak:.2f} ГБ) или меньшее N (было {old_n})"


# --- ворота wait ------------------------------------------------------------------------------------------------
def gate(tkt, spec: str, arg, root, ssh_cmd=None) -> tuple:
    """→ (отказ, поля шапки, запись для лога). Флаг `require_probe` выключен или ожидание не расчёта — пусто."""
    if not PROBE.enabled() or not T.wait_needs_by(spec):
        return "", {}, ""
    text, _ = PROBE.load(arg, root) if (arg or "").strip() else ("", "")
    new = (PROBE.peak_gb(text), PROBE.procs(text))
    if new[0] is not None and round(new[0], 2) <= 0:
        return "пик памяти одного процесса должен быть больше 0 (не меньше 0,01 ГБ ≈ 10 МБ): замерь на тяжёлом элементе", {}, ""
    new = new if None not in new else None
    mark = oom_mark(tkt.id)
    if mark:
        why = _oom_problem(mark, new)
        if why:
            return (f"прошлый запуск тикета убит по памяти (OOM, {mark.get('at', '')}): повтор без нового замера не принимается. "
                    f"Замерь заново и передай `--probe` с {why}"), {}, ""
        if form := PROBE.problem(text, float("inf")):  # короткий --by форму не проверил, а новая запись снимает отметку
            return f"запись пробы не принята: {form}. Нужно: {PROBE.FORM}", {}, ""
    load = new or PROBE.load_of_header(tkt)
    upd = {PROBE.LOAD_KEY: PROBE.load_line(*load)} if new else {}
    parsed = T.parse_wait_for(spec)
    if not load or not parsed or parsed[0] not in ("host", "job") or not P.setting("host_free_cmd"):
        return "", upd, ""
    if ssh_cmd is None:
        import waits
        ssh_cmd = waits._ssh_cmd
    got = free(parsed[1], tkt.id, ssh_cmd)
    if got is None:
        return "", upd, "[нагрузка] свободные ядра и память не проверены: host_free_cmd не ответила"
    (peak, n), (cores, mem), res = load, got, reserve()
    top, need = n_max(cores, mem, peak, res), n * peak * res
    line = f"N={n} × пик {peak:.2f} ГБ × запас {res:g} = {need:.1f} ГБ; свободно {cores} ядер, {mem:g} ГБ; N_max = {top}"
    if n > cores or need > mem:
        return f"процессов больше, чем выдержит машина: {line}. Уменьши N до {top} или освободи машину", {}, ""
    return "", upd, f"[нагрузка] {line}" + (" — машина недогружена, можно больше" if n < top else "")
