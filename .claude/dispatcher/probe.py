"""TK-069 (G-008): проба до долгого расчёта — ожидание дольше порога не принимается без записи пробы в тикете.

Расчёт, поставленный в `wait` (host:/file:/job:) со сроком `--by` дальше `probe_threshold_min` (умолчание 15 мин), требует
`--probe <запись|файл>`: ≥ 2 способа (один «как есть») — у каждого в одной строке время и пик памяти на малом срезе, строка
«тяжёлый: …» (самый тяжёлый элемент среза) и «оценка: N мин|ч» полного времени; срок не короче оценки. Проба — один раз на
тикет: принятая пишется в шапку (`probe:`) и в лог, следующие `wait` идут по шапке. За флагом `require_probe` (умолчание
выключен — `wait` как раньше). Слов конкретного проекта нет: код проверяет форму записи, а не содержание.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path

import project as P
import ticket as T

THRESHOLD_MIN = 15
HEADER_KEY = "probe"
LOAD_KEY = "probe_load"  # «пик 5.50 ГБ · N 15» — нагрузка из записи пробы (TK-070)
FORM = ("способы — по строке на способ, в строке время (сек|мин|ч) и пик памяти (МБ|ГБ), один способ «как есть»; "
        "строка «тяжёлый: <элемент среза> · пик <МБ|ГБ> одного процесса»; строка «N: <число параллельных процессов полного "
        "расчёта>»; строка «оценка: N мин|ч» полного времени")

_NUM = r"\d+(?:[.,]\d+)?"
_END = r"(?![а-яёa-z])"
_TIME_RE = re.compile(_NUM + r"\s*(?:мс|сек\w*|с|мин\w*|ч|час\w*)" + _END, re.I)
_MEM_RE = re.compile(_NUM + r"\s*(?:[кмгт]и?б|[kmgt]i?b)" + _END, re.I)
_ASIS_RE = re.compile(r"как есть", re.I)
_HEAVY_RE = re.compile(r"^.*тяж[её]л\w*\s*:\s*\S.*$", re.I | re.M)
_PROCS_RE = re.compile(r"^\s*[nн]\s*[:=]\s*(\d+)", re.I | re.M)
_GB = {"к": 1 / 1048576, "м": 1 / 1024, "г": 1.0, "т": 1024.0, "k": 1 / 1048576, "m": 1 / 1024, "g": 1.0, "t": 1024.0}
_ESTIMATE_RE = re.compile(r"оценк\w*[^:\n]{0,30}:\s*(" + _NUM + r")\s*(мин\w*|ч|час\w*)" + _END, re.I)


def enabled() -> bool:
    return bool(P.setting("require_probe", False))


def threshold_min() -> int:
    return int(P.setting("probe_threshold_min", THRESHOLD_MIN))


def estimate_min(text: str):
    """«оценка: 5,5 ч» → 330.0 минут; строки нет — None."""
    m = _ESTIMATE_RE.search(text or "")
    if not m:
        return None
    value = float(m.group(1).replace(",", "."))
    return value if m.group(2).lower().startswith("мин") else value * 60


def peak_gb(text: str):
    """Пик памяти одного процесса на тяжёлом элементе, ГБ: первое «число + МБ|ГБ» в строке «тяжёлый: …»; нет — None (TK-070)."""
    heavy = _HEAVY_RE.search(text or "")
    m = _MEM_RE.search(heavy.group(0)) if heavy else None
    if not m:
        return None
    return _gb_of(m.group(0))


def _gb_of(token: str) -> float:
    num = re.match(_NUM, token).group(0).replace(",", ".")
    unit = re.search(r"[кмгтkmgt]", token[len(num):], re.I).group(0).lower()
    return float(num) * _GB[unit]


def load_line(peak: float, n: int) -> str:
    return f"пик {peak:.2f} ГБ · N {n}"


def load_of_header(tkt):
    """(пик ГБ, N) из шапки `probe_load:`; нет или не разобрать — None."""
    m = re.search(r"пик (\d+(?:\.\d+)?) ГБ · N (\d+)", (tkt.header.get(LOAD_KEY) or ""))
    return (float(m.group(1)), int(m.group(2))) if m else None


def procs(text: str):
    """«N: 15» — число параллельных процессов полного расчёта (≥ 1); нет строки — None (TK-070)."""
    m = _PROCS_RE.search(text or "")
    return int(m.group(1)) if m and int(m.group(1)) >= 1 else None


def _method_lines(text: str) -> list:
    return [ln for ln in (text or "").splitlines() if _TIME_RE.search(ln) and _MEM_RE.search(ln)]


def problem(text: str, left_min: float) -> str:
    """Причина отказа по форме записи пробы (пусто — годится); `left_min` — минут от сейчас до срока `--by`."""
    methods = _method_lines(text)
    if len(methods) < 2:
        return f"способов с временем и пиком памяти в одной строке: {len(methods)}, нужно ≥ 2"
    if not any(_ASIS_RE.search(ln) for ln in methods):
        return "ни у одного способа нет «как есть» (текущий код без правок) — нужен один"
    if not _HEAVY_RE.search(text):
        return "нет строки «тяжёлый: <самый тяжёлый элемент среза> · пик <МБ|ГБ> одного процесса»"
    if peak_gb(text) is None:
        return "в строке «тяжёлый: …» нет пика памяти одного процесса на этом элементе (число и МБ|ГБ)"
    if procs(text) is None:
        return "нет строки «N: <число параллельных процессов полного расчёта>» (целое ≥ 1)"
    est = estimate_min(text)
    if est is None:
        return "нет строки «оценка: N мин|ч» — полное время расчёта"
    if left_min < est:
        return f"срок --by через {left_min:.0f} мин, а оценка пробы {est:.0f} мин — срок не короче оценки"
    return ""


def load(arg: str, root) -> tuple:
    """`--probe`: путь к существующему файлу (от корня проекта или абсолютный) → (текст, путь); иначе сам аргумент."""
    arg = (arg or "").strip()
    try:
        p = Path(arg)
        p = p if p.is_absolute() else Path(root) / p
        if "\n" not in arg and p.is_file():
            return p.read_text(encoding="utf-8-sig"), arg
    except OSError:
        pass
    return arg, ""


def gate(tkt, spec: str, by_dt, arg, root, now: datetime | None = None) -> tuple:
    """→ (отказ, поля шапки, запись для лога). Флаг выключен, ожидание не расчёта или срок в пределах порога — пусто."""
    if not enabled() or by_dt is None or not T.wait_needs_by(spec):
        return "", {}, ""
    now = now or datetime.now().astimezone()
    left = (by_dt - now).total_seconds() / 60
    if left <= threshold_min():
        return "", {}, ""
    have = (tkt.header.get(HEADER_KEY) or "").strip()
    if not (arg or "").strip():
        if have:
            return "", {}, ""
        return (f"срок {left:.0f} мин дальше порога {threshold_min()} мин — расчёт не принимается в ожидание без пробы. "
                f"Добавь `--probe <запись|файл>`: {FORM} (G-008)"), {}, ""
    text, src = load(arg, root)
    why = problem(text, left)
    if why:
        return f"проба не принята: {why}. Нужно: {FORM}", {}, ""
    est = f"оценка {estimate_min(text):.0f} мин"
    ld = {LOAD_KEY: load_line(peak_gb(text), procs(text))}
    if src:
        return "", {HEADER_KEY: f"файл {src} · {est}", **ld}, f"[проба] файл {src} · {est}"
    return "", {HEADER_KEY: f"в логе · {est}", **ld}, f"[проба] {text}"
