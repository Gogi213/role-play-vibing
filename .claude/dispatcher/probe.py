"""TK-069 (G-008): проба до долгого расчёта — ожидание дольше порога не принимается без записи пробы в тикете.

Расчёт, поставленный в `wait` вида из `probe_kinds` (умолчание host:, job: — расчёт на сервере; ожидание file: — не расчёт, проба
ему не нужна, TK-081) со сроком `--by` дальше `probe_threshold_min` (умолчание 15 мин), требует
`--probe <запись|файл>`: ≥ 2 способа (один «как есть») — у каждого в одной строке время и пик памяти на малом срезе, строка
«тяжёлый: …» (самый тяжёлый элемент среза) и «оценка: N мин|ч» полного времени; срок не короче оценки. Проба — один раз на
тикет: принятая пишется в шапку (`probe:`) и в лог, следующие `wait` идут по шапке. Порог считается по сумме сроков `--by` одного
расчёта: после `[итог: done|accept]` в логе счёт начинается заново. За флагом `require_probe` (умолчание
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
WAITED_KEY = "probe_waited"  # минуты: сумма сроков `wait` тикета без пробы — порог считается по ним (TK-073)
WAITED_AT_KEY = "probe_waited_at"  # когда сумма записана: итог done|accept в логе позже — расчёт кончился, сумма не в счёт (TK-081)
ALLOWED_KINDS = ("host", "file", "job")  # виды `wait`, которым вообще нужен срок --by
DEFAULT_KINDS = ("host", "job")  # проба — расчёту на сервере; file: ждёт чужой результат, а не считает
_CLOSED_RE = re.compile(r"\[итог:\s*(?:done|accept)\]")
FORM = ("способы — по строке на способ, в строке время (сек|мин|ч) и пик памяти (МБ|ГБ), один способ «как есть»; "
        "строка «тяжёлый: <элемент среза> · пик <МБ|ГБ> одного процесса»; строка «N: <число параллельных процессов полного "
        "расчёта>»; строка «оценка: N мин|ч» полного времени")

_NUM = r"\d+(?:[.,]\d+)?"
_END = r"(?![а-яёa-z])"
_TIME_RE = re.compile(_NUM + r"\s*(?:мс|ms|сек\w*|sec\w*|с|s|мин\w*|min\w*|ч|час\w*|h|hr|hours?)" + _END, re.I)
_MEM_RE = re.compile(_NUM + r"\s*(?:[кмгт]и?б|[kmgt]i?b|(?-i:[MGT]))" + _END, re.I)  # «512 M», «4G» — только заглавные
_ASIS_RE = re.compile(r"как есть", re.I)
_HEAVY_RE = re.compile(r"^.*тяж[её]л\w*\s*:\s*\S.*$", re.I | re.M)
_PROCS_RE = re.compile(r"^\s*(?:[-•]\s+|\d+[.)]\s+)?[nн]\s*[:=]\s*(\d+)", re.I | re.M)  # после _clean: «- N: 15», «1) N: 15»
_GB = {"к": 1 / 1048576, "м": 1 / 1024, "г": 1.0, "т": 1024.0, "k": 1 / 1048576, "m": 1 / 1024, "g": 1.0, "t": 1024.0}
_EST_PART = r"(" + _NUM + r")\s*(мин\w*|min\w*|ч|час\w*|h|hr|hours?)" + _END
_ESTIMATE_RE = re.compile(r"оценк\w*[^:\n]{0,30}:\s*(?:~|≈|около\s+)?\s*((?:" + _NUM + r"\s*(?:мин\w*|min\w*|ч|час\w*|h|hr|hours?)"
                          + _END + r"\s*)+)", re.I)
_PEAK_RE = re.compile(r"пик\w*\s*(?:памяти)?\s*[:=]?\s*(?:~|≈|около\s+)?\s*(" + _NUM
                      + r"\s*(?:[кмгт]и?б|[kmgt]i?b|(?-i:[MGT])))" + _END, re.I)


def enabled() -> bool:
    return bool(P.setting("require_probe", False))


def threshold_min() -> int:
    try:
        return int(P.setting("probe_threshold_min", THRESHOLD_MIN))
    except (TypeError, ValueError):
        return THRESHOLD_MIN  # в rpv.json вписано не число — умолчание, а не падение `wait`


def kinds() -> tuple:
    """Виды `wait`, которым нужна проба (`probe_kinds`: список или «host,job»); пусто или мусор — умолчание."""
    raw = P.setting("probe_kinds", DEFAULT_KINDS)
    raw = raw.split(",") if isinstance(raw, str) else raw
    got = tuple(k.strip() for k in raw if isinstance(k, str) and k.strip() in ALLOWED_KINDS) if isinstance(raw, (list, tuple)) else ()
    return got or DEFAULT_KINDS


def needs_probe(spec: str) -> bool:
    parsed = T.parse_wait_for(spec)
    return bool(parsed) and parsed[0] in kinds()


def waited_min(tkt) -> float:
    """Сумма сроков прежних `wait` расчёта без пробы, минуты (шапка `probe_waited:`); нет или не разобрать — 0; после записи
    суммы в логе есть итог done|accept (расчёт кончился) — тоже 0."""
    try:
        total = max(0.0, float((tkt.header.get(WAITED_KEY) or "0").strip()))
    except ValueError:
        return 0.0
    try:
        since = T.parse_dt(tkt.header.get(WAITED_AT_KEY) or "")
    except ValueError:
        return total  # метки времени нет (запись до TK-081) — сумма как была
    return 0.0 if any(e.ts > since and _CLOSED_RE.match(e.text.strip()) for e in getattr(tkt, "log", ())) else total


def _clean(text: str) -> str:
    """Без разметки: `**оценка:**` и `код` читаются как оценка: и код."""
    return (text or "").replace("*", "").replace("`", "")


def estimate_min(text: str):
    """«оценка: 5,5 ч», «оценка ≈ 1 ч 40 мин» → минуты (330.0; 100.0); строки нет — None."""
    m = _ESTIMATE_RE.search(_clean(text))
    if not m:
        return None
    return sum(float(v.replace(",", ".")) * (1 if u.lower().startswith("мин") or u.lower().startswith("min") else 60)
               for v, u in re.findall(_EST_PART, m.group(1), re.I))


def peak_gb(text: str):
    """Пик памяти одного процесса на тяжёлом элементе, ГБ: «пик <число МБ|ГБ>» в строке «тяжёлый: …»; без слова «пик» —
    единственное «число + МБ|ГБ» в строке; несколько без «пик» («вход 2 ГБ · 5 ГБ») или нет — None (TK-070, TK-073)."""
    heavy = _HEAVY_RE.search(_clean(text))
    if not heavy:
        return None
    m = _PEAK_RE.search(heavy.group(0))
    if m:
        return _gb_of(m.group(1))
    found = [x.group(0) for x in _MEM_RE.finditer(heavy.group(0))]
    return _gb_of(found[0]) if len(found) == 1 else None


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
    m = _PROCS_RE.search(_clean(text))
    return int(m.group(1)) if m and int(m.group(1)) >= 1 else None


def _method_lines(text: str) -> list:
    return [ln for ln in _clean(text).splitlines() if _TIME_RE.search(ln) and _MEM_RE.search(ln)]


def problem(text: str, left_min: float, elapsed_min: float = 0.0) -> str:
    """Причина отказа по форме записи пробы (пусто — годится); `left_min` — минут от сейчас до срока `--by`,
    `elapsed_min` — уже прошедшие ожидания тикета: срок сверяется с остатком оценки, не с полной."""
    text = _clean(text)
    methods = _method_lines(text)
    if len(methods) < 2:
        return f"способов с временем и пиком памяти в одной строке: {len(methods)}, нужно ≥ 2"
    if not any(_ASIS_RE.search(ln) for ln in methods):
        return "ни у одного способа нет «как есть» (текущий код без правок) — нужен один"
    if not _HEAVY_RE.search(text):
        return "нет строки «тяжёлый: <самый тяжёлый элемент среза> · пик <МБ|ГБ> одного процесса»"
    if peak_gb(text) is None:
        return ("в строке «тяжёлый: …» нет пика памяти одного процесса (однозначного): напиши «пик <число> МБ|ГБ» "
                "(несколько чисел с единицами без слова «пик» не читаются)")
    if round(peak_gb(text), 2) <= 0:
        return "пик памяти одного процесса должен быть больше 0 (не меньше 0,01 ГБ ≈ 10 МБ)"
    if procs(text) is None:
        return "нет строки «N: <число параллельных процессов полного расчёта>» (целое ≥ 1)"
    est = estimate_min(text)
    if est is None:
        return "нет строки «оценка: N мин|ч» — полное время расчёта"
    if left_min + elapsed_min < est:
        return (f"срок --by через {left_min:.0f} мин (+ уже прошло {elapsed_min:.0f}), а оценка пробы {est:.0f} мин — "
                f"срок не короче оценки (за вычетом уже прошедшего)")
    return ""


def load(arg: str, root) -> tuple:
    """`--probe`: путь к существующему файлу (от корня проекта или абсолютный) → (текст, путь); иначе сам аргумент."""
    arg = (arg or "").strip()
    if not arg or "\n" in arg:
        return arg, ""
    p = Path(arg)
    for base in ((None,) if p.is_absolute() else (Path(root), Path.cwd())):  # корень проекта, затем копия (worktree)
        try:
            q = p if base is None else base / p
            if q.is_file():
                return q.read_text(encoding="utf-8-sig"), arg
        except OSError:
            continue
    return arg, ""


def gate(tkt, spec: str, by_dt, arg, root, now: datetime | None = None) -> tuple:
    """→ (отказ, поля шапки, запись для лога). Флаг выключен, ожидание не расчёта или срок в пределах порога — пусто."""
    if not enabled() or not needs_probe(spec):  # флаг выключен или не расчёт на сервере (file:, at:…): запись — в лог, без проверки
        return "", {}, (f"[проба] {load(arg, root)[0]}" if (arg or "").strip() else "")
    if by_dt is None:
        return "", {}, ""
    now = now or datetime.now().astimezone()
    left = (by_dt - now).total_seconds() / 60
    have = (tkt.header.get(HEADER_KEY) or "").strip()
    before = 0.0 if have else waited_min(tkt)
    total = before + left
    if total <= threshold_min():
        # короткие --by складываются: обхода порогом нарезкой нет
        return "", ({} if have else {WAITED_KEY: f"{total:.1f}", WAITED_AT_KEY: now.isoformat(timespec="seconds")}), ""
    if not (arg or "").strip():
        if have:
            return "", {}, ""
        return (f"ожидание тикета {total:.0f} мин (этот срок {left:.0f} + прежние без пробы {before:.0f}) дальше порога "
                f"{threshold_min()} мин — расчёт не принимается в ожидание без пробы. "
                f"Добавь `--probe <запись|файл>`: {FORM} (G-008)"), {}, ""
    text, src = load(arg, root)
    why = problem(text, left, before)
    if why:
        return f"проба не принята: {why}. Нужно: {FORM}", {}, ""
    est = f"оценка {estimate_min(text):.0f} мин"
    ld = {LOAD_KEY: load_line(peak_gb(text), procs(text))}
    if before:
        ld[WAITED_KEY] = ld[WAITED_AT_KEY] = ""  # проба принята — счёт прежних ожиданий больше не нужен
    if src:
        return "", {HEADER_KEY: f"файл {src} · {est}", **ld}, f"[проба] файл {src} · {est}"
    return "", {HEADER_KEY: f"в логе · {est}", **ld}, f"[проба] {text}"
