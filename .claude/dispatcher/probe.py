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
FORM = ("способы — по строке на способ, в строке время (сек|мин|ч) и пик памяти (МБ|ГБ), один способ «как есть»; "
        "строка «тяжёлый: <элемент среза>»; строка «оценка: N мин|ч» полного времени")

_NUM = r"\d+(?:[.,]\d+)?"
_END = r"(?![а-яёa-z])"
_TIME_RE = re.compile(_NUM + r"\s*(?:мс|сек\w*|с|мин\w*|ч|час\w*)" + _END, re.I)
_MEM_RE = re.compile(_NUM + r"\s*(?:[кмгт]и?б|[kmgt]i?b)" + _END, re.I)
_ASIS_RE = re.compile(r"как есть", re.I)
_HEAVY_RE = re.compile(r"тяж[её]л\w*\s*:\s*\S", re.I)
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
        return "нет строки «тяжёлый: <самый тяжёлый элемент среза>»"
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
    if src:
        return "", {HEADER_KEY: f"файл {src} · {est}"}, f"[проба] файл {src} · {est}"
    return "", {HEADER_KEY: f"в логе · {est}"}, f"[проба] {text}"
