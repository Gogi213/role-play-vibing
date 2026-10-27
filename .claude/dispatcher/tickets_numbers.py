"""Таблица ключевых чисел сдачи для приёмки Судьи (`--numbers`): разбор чисел в тексте и сверка с источником.

Только stdlib; корень проекта приходит параметром. Команды `tickets.py` зовут `numbers_ack` из `tickets.py`.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import registry as registry_mod

NUMBER = re.compile(r"[-+−]?\d+(?:[.,]\d+)?")
NUMBER_ROW = re.compile(r"^\s*\|([^|]+)\|([^|]+)\|")
# `--numbers -` («чисел в сдаче нет») код проверяет сам: в текстовом артефакте приёмки по --path ищет числа
NUMBERS_TEXT_EXT = {".md", ".txt", ".csv", ".tsv", ".json", ".log"}
NUMBERS_SCAN_FILES = 50          # каталог-артефакт: не больше стольких текстовых файлов
NUMBERS_SCAN_BYTES = 1_000_000   # и не больше стольких байт с каждого
# что числом не считается (метки, а не величины); каждое совпадение заменяется пробелом до поиска чисел
_NOT_NUMBERS = tuple(re.compile(p, f) for p, f in (
    (r"https?://\S+", 0),                                                                   # адреса
    (r"(?<![0-9a-f])[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?![0-9a-f])", 0),   # uuid
    (r"(?<![\w./\\-])[\w./\\-]{0,200}\w\.[A-Za-z][A-Za-z0-9]{0,4}\b(?::\d+(?:[-–]\d+)?)?", 0),   # файл.расш[:строка]
    (r"(?<!\d)\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:[.,]\d+)?)?(?:Z|[+-]\d{2}(?::?\d{2})?)?)?", 0),  # ISO
    (r"\d{1,2}\s+(?:января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)"
     r"(?:\s+\d{4})?(?:\s*г\.?)?", re.I),                                                   # 14 октября 2026
    (r"(?<![\d.,])\d{1,2}\.\d{1,2}\.\d{4}(?!\d)", 0),                                        # ДД.ММ.ГГГГ
    (r"(?<![\d.,])(?:0[1-9]|[12]\d|3[01])\.(?:0[1-9]|1[0-2])(?:\.(?:\d{4}|\d{2}))?(?!\d|[.,]\d)", 0),   # ДД.ММ[.ГГ]
    (r"(?<![\w:.,])\d{1,2}:\d{2}(?::\d{2})?(?!\d)", 0),                                      # ЧЧ:ММ[:СС]
    (r"(?<![\w.])[vV]?\d+(?:\.\d+){2,}(?:-[A-Za-z][0-9A-Za-z.]*)?", 0),                      # версия X.Y.Z
    (r"(?<!\w)[A-ZА-ЯЁ]{1,3}-\d+(?:\.\d+)*", 0),                                             # TK-236, В-12, G-001, E-3, UTF-8
    (r"(?<!\w)[Pp][Rr]\s*№?\s*#?\d+|[#№]\s*\d+", 0),                                              # PR #7, #7, №7
    (r"(?<!\w)(?:пп?|пункт\w*|§)\.?\s*\d+(?:[.,]\d+)*", 0),                                   # п.4, пункт 4
    (r"(?<!\w)(?=[0-9a-f]*[a-f])(?=[0-9a-f]*\d)[0-9a-f]{7,40}(?!\w)", 0),                    # sha (7–40 hex)
    (r"^[ \t]*(?:[-*+>][ \t]*)*\d+(?:\.\d+)*[.)](?=\s|$)", re.M),                            # «1.», «2)» в начале строки
    (r"^[ \t]*#{1,6}[ \t]+\d+(?:\.\d+)*[.)]?(?=\s|$)", re.M),                                # ## 1
))
_FREE_NUMBER = re.compile(r"(?<![\w.,])\d+(?:[.,]\d+)*")  # «CP9», «v2», «sha256» — метки, а не числа


def text_numbers(text: str) -> list[str]:
    """Числа в тексте сдачи — всё, что не дата, время, номер (TK-/В-/G-/E-/PR #/п.), версия X.Y.Z, sha, файл:строка,
    адрес, номер пункта списка и заголовка. Метка с буквой слева («CP9», «v2») числом не считается."""
    for rx in _NOT_NUMBERS:
        text = rx.sub(" ", text)
    return [m.group(0) for m in _FREE_NUMBER.finditer(text)]


def artifact_numbers(target: Path | None) -> list[str]:
    """Числа в текстовом артефакте (md, txt, csv, tsv, json, log) или в текстовых файлах каталога (до
    NUMBERS_SCAN_FILES файлов); не текст — пусто: код читать нечем, это держит проверка Судьи."""
    if target is None:
        return []
    if target.is_file():
        files = [target]
    else:
        files = []
        for dirpath, dirnames, names in os.walk(target):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            files += [Path(dirpath) / n for n in sorted(names) if Path(n).suffix.lower() in NUMBERS_TEXT_EXT]
            if len(files) >= NUMBERS_SCAN_FILES:
                break
    found: list[str] = []
    for f in files[:NUMBERS_SCAN_FILES]:
        if f.suffix.lower() not in NUMBERS_TEXT_EXT:
            continue
        try:
            found += text_numbers(f.read_bytes()[:NUMBERS_SCAN_BYTES].decode("utf-8", errors="replace"))
        except OSError:
            continue
    return found


def has_number(text: str, num: str) -> bool:
    """Число `num` есть в `text` целым числом (3 — не в 13 и не в 0,3); запятая и точка — одно."""
    text = text.replace("−", "-")
    return any(re.search(r"(?<![\d.,])" + re.escape(v) + r"(?!\d|[.,]\d)", text)
               for v in {num, num.replace(",", "."), num.replace(".", ",")})


def numbers_ack(args, tid: str, project_root: Path) -> tuple[str, str]:
    """CP9 (критерий 4): приёмка Судьи в V2-проекте — с таблицей ключевых чисел сдачи `| число | файл:строка |`
    (`--numbers <файл>`) или `--numbers -` (чисел нет). Номер строки обязателен: число обязано быть в названной строке ±2
    своего источника, иначе оно «из головы», отказ. `--numbers -` при приёмке по --path (без PR) код проверяет сам: в текстовом
    артефакте есть числа — отказ. Пути — от worktree тикета, если он есть, иначе от корня проекта."""
    if not registry_mod.decisions_file(project_root).exists():
        return "", ""
    raw = (getattr(args, "numbers", None) or "").strip()
    root = project_root.resolve()
    wt = root / ".claude" / "worktrees" / tid

    def find(rel: str, dirs: bool = False):
        for base in ((wt, root) if wt.is_dir() else (root,)):
            f = (Path(rel) if Path(rel).is_absolute() else base / rel).resolve()
            if (f.is_file() or (dirs and f.is_dir())) and f.is_relative_to(root):
                return f
        return None

    if raw in ("-", "—"):
        art = (getattr(args, "path", None) or "").strip()
        if art and not (getattr(args, "pr", None) or getattr(args, "sha", None)):  # приёмка по PR (код) — без этой проверки
            nums = list(dict.fromkeys(artifact_numbers(find(art, dirs=True))))
            if nums:
                return (f"в сдаче есть числа (первые: {', '.join(nums[:5])}) — нужна таблица --numbers <файл с таблицей "
                        f"«| число | файл:строка |»>; --numbers - только когда чисел в {art} нет"), ""
        return "", " числа: нет"
    if not raw:
        return ("приёмка — с таблицей ключевых чисел: --numbers <файл с таблицей «| число | файл:строка |»> или "
                "--numbers - (чисел в сдаче нет); число без источника владельцу не уходит"), ""
    table = find(raw)
    if not table:
        return f"--numbers {raw}: файла нет в проекте", ""
    rows = []
    for ln in table.read_text(encoding="utf-8", errors="replace").splitlines():
        m = NUMBER_ROW.match(ln)
        num = NUMBER.search(m.group(1)) if m else None
        if num:
            rows.append((num.group(0).replace("−", "-"), m.group(2).strip().strip("`").strip()))
    if not rows:
        return f"--numbers {raw}: нет строк таблицы «| число | файл:строка |»", ""
    bad = []
    for num, src in rows:
        at = re.fullmatch(r"(.+?):(\d+)", src)
        f = find(at.group(1) if at else src)
        if not f:
            bad.append(f"{num} — источника {src} нет")
            continue
        lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        if not at:  # номер строки обязателен: короткое «3» иначе найдётся в любом месте файла
            hit = [str(i) for i, ln in enumerate(lines, 1) if has_number(ln, num)][:3]
            bad.append(f"{num} — у {src} нет номера строки, пиши файл:строка"
                       + (f" (число есть в строках {', '.join(hit)})" if hit else " (такого числа в файле нет)"))
            continue
        n = int(at.group(2))
        if not 1 <= n <= len(lines):
            bad.append(f"{num} — в {at.group(1)} нет строки {n} (строк: {len(lines)})")
        elif not has_number("\n".join(lines[max(n - 3, 0):n + 2]), num):
            bad.append(f"{num} — нет в {src} (строки {max(n - 2, 1)}–{min(n + 2, len(lines))})")
    if bad:
        return "числа не сходятся с источником (число «из головы» владельцу не уходит): " + "; ".join(bad[:8]), ""
    return "", f" числа: сверено {len(rows)} ({raw})"
