"""Уставы проекта `.claude/roles/*.md` — перенос из шаблонов плагина только командой (TK-029, В-12).

    tickets.py charter sync [--file <имя>.md]... [--why "..."] --ref В-N        # только CEO; без --ref отказ
    tickets.py charter addon <роль|README> --text "..." [--replace] [--why "..."] --ref В-N   # добавка проекта (TK-038)

CEO уставы руками не правит (страж `delete_guard`, TK-007); после выпуска плагина новые строки уставов лежат в его
`templates/roles/`. Команда кладёт файлы оттуда в `.claude/roles/` как есть, без правки текста. Исключение — README.md:
строки таблицы зон (`| … |`) принадлежат проекту, и те, что уже есть в проекте, остаются его. Каждый перенос пишется в журнал
`dispatcher/charter-log.json`; ежедневный отчёт показывает его в разделе «CEO изменил работу команды» (как `config set`).

Правила проекта для ролей лежат не в уставе, а в добавке `.claude/roles/project/<роль>.md` (и `project/README.md`): синк её не трогает,
хук запуска роли (`role_context.py`) кладёт добавку в контекст сразу после устава. Писать её — только `charter addon`: слово владельца
`--ref В-N`, запись в журнал; руками страж не даёт, как и уставы. Строки, которые синк убрал из устава проекта и которых нет в шаблоне,
он выводит списком «убрано — перенеси в добавку» и кладёт в запись журнала (`removed`).
"""
from __future__ import annotations

import re
import sys
from datetime import datetime
from pathlib import Path

import plugin_version as PV
import project as P
import registry as registry_mod
import rpv_config as cfg_mod

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
LOG_NAME = "charter-log.json"
TABLE_FILE = "README.md"                     # единственный устав со строками таблицы зон проекта
ADDON_DIR = "project"                        # `.claude/roles/project/<роль>.md` — добавки проекта; синк их не трогает


def log_path(root: Path) -> Path:
    return Path(root) / ".claude" / "dispatcher" / LOG_NAME


def read_log(root: Path) -> list:
    return P.read_log(log_path(root))


def plugin_version(plugin_root: Path) -> str:
    return str(PV.meta(plugin_root).get("version") or "?")


def _cell(line: str) -> str:
    """Первая ячейка строки таблицы (по ней строка проекта находит свою пару в шаблоне)."""
    return line.strip().strip("|").split("|", 1)[0].strip()


def merge_table_rows(template: str, current: str) -> str:
    """Шаблон как есть, но строки таблицы, которые уже есть в проекте (по первой ячейке), — проекта; строки проекта, которых нет
    в шаблоне (его зоны), остаются — после последней строки таблицы шаблона."""
    mine = {_cell(ln): ln.strip() for ln in current.splitlines() if ln.lstrip().startswith("|")}
    out, last, seen = [], None, set()
    for ln in template.splitlines(keepends=True):
        if ln.lstrip().startswith("|"):
            last = len(out)
            seen.add(_cell(ln))
            if _cell(ln) in mine:
                ln = mine[_cell(ln)] + ln[len(ln.rstrip("\r\n")):]
        out.append(ln)
    extra = [row for cell, row in mine.items() if cell not in seen]
    if extra:
        at = len(out) if last is None else last + 1
        eol = "\r\n" if template.endswith("\r\n") else "\n"
        if at and not out[at - 1].endswith(("\n", "\r")):
            out[at - 1] += eol
        out[at:at] = [row + eol for row in extra]
    return "".join(out)


def _ref_error(root: Path, ref: str, required: bool = False) -> str:
    """'' или причина отказа: `--ref` — решение владельца В-N, уже занесённое в реестр."""
    if not ref:
        return "нужен --ref В-N: решение владельца, уже занесённое в реестр (`registry.py list`)" if required else ""
    if not re.fullmatch(r"В-\d+", ref) or not registry_mod.ref_exists(root, ref):
        return f"--ref {ref}: такого решения нет в реестре (`registry.py list`)"
    return ""


def _wanted(templates: Path, names) -> tuple:
    """→ (имена, None) или (None, причина): имена из `--file` — только простые `*.md` из шаблонов; без них — все шаблоны."""
    have = sorted(p.name for p in Path(templates).glob("*.md"))
    if not have:
        return None, f"нет шаблонов уставов: {templates}"
    names = list(dict.fromkeys(names or have))
    bad = [n for n in names if n not in have]
    return (None, f"нет в шаблонах плагина: {', '.join(bad)} (есть: {', '.join(have)})") if bad else (names, None)


def _new_bytes(name: str, template: Path, target: Path) -> bytes:
    data = template.read_bytes()
    if name != TABLE_FILE or not target.exists():
        return data
    text = merge_table_rows(data.decode("utf-8"), target.read_text(encoding="utf-8"))
    return text.encode("utf-8")


def _removed_lines(old: bytes, new: bytes) -> list:
    """Непустые строки прежнего устава проекта, которых нет в новом тексте (по обрезанным пробелам), по порядку, без повторов."""
    kept = {ln.strip() for ln in new.decode("utf-8", "replace").splitlines()}
    gone = (ln.strip() for ln in old.decode("utf-8", "replace").splitlines())
    return list(dict.fromkeys(ln for ln in gone if ln and ln not in kept))


def _removed_report(removed: dict) -> str:
    if not removed:
        return ""
    out = ["убрано из уставов проекта (в шаблоне плагина этих строк нет) — перенеси нужное в добавку: "
           "`tickets.py charter addon <роль> --text \"...\" --ref В-N`"]
    for name, rows in removed.items():
        out += [f"  {name}: {len(rows)} стр."] + [f"    {ln}" for ln in rows]
    return "\n" + "\n".join(out)


def sync(root: Path, names=None, why: str = "", ref: str = "", templates: Path = None, now=None):
    """→ (запись журнала | None, сообщение, ошибка). Совпадающие с шаблоном файлы не трогаются; всё, что нужно, проверено до
    первой записи. Совпало всё — записи в журнале нет. `--ref` обязателен в команде (`run`), здесь — если задан, то существует."""
    now = now or datetime.now().astimezone()
    templates = Path(templates or PLUGIN_ROOT / "templates" / "roles")
    roles = Path(root) / ".claude" / "roles"
    if not roles.is_dir():
        return None, "", f"нет {roles}: сначала /rpv-init"
    todo, err = _wanted(templates, names)
    if err:
        return None, "", err
    if err := _ref_error(root, ref):
        return None, "", err
    changes, removed = [], {}
    for name in todo:
        target = roles / name
        data = _new_bytes(name, templates / name, target)
        if not target.exists() or target.read_bytes() != data:
            changes.append((target, data, "updated" if target.exists() else "created"))
            if target.exists() and (rows := _removed_lines(target.read_bytes(), data)):
                removed[name] = rows
    if not changes:
        return None, f"уставы уже совпадают с шаблонами плагина: {', '.join(todo)} — менять нечего", None
    for target, data, _ in changes:
        P.atomic_write_bytes(target, data)             # читать устав могут запущенные роли: повторы при PermissionError
    log = read_log(root)
    version = plugin_version(templates.parents[1])
    entry = {"id": f"CH-{len(log) + 1}", "at": now.isoformat(timespec="seconds"), "version": version,
             "files": {t.name: how for t, _, how in changes}, "why": " ".join((why or "").split()) or f"перенос из шаблонов плагина {version}",
             "ref": ref, "quote": cfg_mod.quote_of(root, ref) if ref else ""}
    if removed:
        entry["removed"] = removed
    log.append(entry)
    P.write_json(log_path(root), log)
    msg = f"{entry['id']}: перенесено из шаблонов плагина {version}: " + ", ".join(f"{n} ({h})" for n, h in entry["files"].items())
    return entry, msg + _removed_report(removed), None


def addon_file(templates: Path, name: str) -> str:
    """Имя файла добавки `<роль>.md` (роль или README — по шаблонам плагина) или ''."""
    stem = (name or "").strip().removesuffix(".md")
    return f"{stem}.md" if stem and (Path(templates) / f"{stem}.md").is_file() else ""


def addon(root: Path, name: str, text: str, why: str = "", ref: str = "", replace: bool = False, templates: Path = None, now=None):
    """→ (запись журнала | None, сообщение, ошибка). `.claude/roles/project/<роль>.md`: дописать текст в конец (`replace` — заменить файл)."""
    now = now or datetime.now().astimezone()
    templates = Path(templates or PLUGIN_ROOT / "templates" / "roles")
    roles = Path(root) / ".claude" / "roles"
    if not roles.is_dir():
        return None, "", f"нет {roles}: сначала /rpv-init"
    if not (file := addon_file(templates, name)):
        have = ", ".join(sorted(p.stem for p in Path(templates).glob("*.md")))
        return None, "", f"добавка к «{name}»: такой роли нет (есть: {have})"
    if err := _ref_error(root, ref, required=True):
        return None, "", err
    text = (text or "").strip()
    if not text:
        return None, "", "нужен текст добавки: --text \"...\""
    target = roles / ADDON_DIR / file
    old = target.read_text(encoding="utf-8") if target.exists() else ""
    new = text + "\n" if replace or not old.strip() else old.rstrip("\r\n") + "\n\n" + text + "\n"
    how = "created" if not target.exists() else "replaced" if replace else "appended"
    target.parent.mkdir(parents=True, exist_ok=True)
    P.atomic_write_bytes(target, new.encode("utf-8"))
    log = read_log(root)
    entry = {"id": f"CH-{len(log) + 1}", "at": now.isoformat(timespec="seconds"), "kind": "addon",
             "version": plugin_version(templates.parents[1]), "files": {f"{ADDON_DIR}/{file}": how},
             "why": " ".join((why or "").split()) or "добавка проекта к уставу", "ref": ref, "quote": cfg_mod.quote_of(root, ref)}
    log.append(entry)
    P.write_json(log_path(root), log)
    return entry, f"{entry['id']}: добавка проекта {ADDON_DIR}/{file} ({how}); роль получит её после устава при запуске", None


def report_lines(root: Path, since, until) -> list:
    """Строки раздела отчёта «CEO изменил работу команды» за период (since, until]."""
    lines = []
    for e in read_log(root):
        try:
            at = P.parse_time(e["at"], strict=True)
        except (KeyError, ValueError):
            continue
        if since < at <= until:
            ref = f" {e['ref']}: «{e['quote'] or '—'}»" if e.get("ref") else " слово владельца не названо"
            what = "добавка проекта к уставу" if e.get("kind") == "addon" else f"уставы из шаблонов плагина {e.get('version', '?')}"
            lines.append(f"• {at:%d.%m %H:%M} {what}: {', '.join(sorted(e.get('files') or {}))} ({e.get('why', '')};{ref})")
    return lines


def run(args, role: str, root: Path) -> int:
    """`tickets.py charter sync|addon`: только CEO (сессия без роли или `ceo`); слово владельца `--ref В-N` обязательно."""
    if role and role != "ceo":
        print(f"charter {args.action} — только CEO; эта сессия — роль {role}", file=sys.stderr)
        return 1
    ref = (args.ref or "").strip()
    if args.action == "addon":
        _, msg, err = addon(root, args.name, args.text, args.why or "", ref, args.replace)
    elif err := _ref_error(root, ref, required=True):
        msg = ""
    else:
        _, msg, err = sync(root, args.file, args.why or "", ref)
    if err:
        print(f"charter {args.action}: {err}", file=sys.stderr)
        return 1
    print(msg)
    return 0
