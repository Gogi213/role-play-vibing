"""Запуск роли: усилие, MCP-серверы, `--plugin-dir`, промпт — вынесено из dispatch.py (TK-011, ось 4).
Чистые функции от настроек проекта (`CFG`, `P`); лимит запуска (`RUN_TIMEOUT`) передаёт вызывающий."""
from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
from pathlib import Path

import project_paths as CFG
import plugin_version as PV
import project as P
import routes as ROUTES

CODE_DIR = Path(__file__).resolve().parent  # где лежит сам диспетчер (папка плагина) — к проекту отношения не имеет

# Усилие ролей (слово владельца 14.10): Судья — Opus 5.5 high, Инженер — Sonnet 5.5 xhigh, Исследователь — Sonnet 5.5 high.
# Для Судьи и Инженера усилие ЖЁСТКОЕ: поле `effort:` тикета их не меняет (раньше оно перекрывало усилие всех ролей тикета,
# и `effort: medium` молча понижал Судью — замер docs/bench/2026-10-14.md). Для Исследователя поле по-прежнему действует
# (`tickets.py new --effort`), см. effort_for().
ROLE_EFFORT = {"judge": "high", "engineer": "xhigh", "researcher": "high"}


FIXED_EFFORT_ROLES = ("judge", "engineer")  # усилие этих ролей не зависит от поля `effort:` тикета


# v2 (02.10): промпт без призыва @-упоминать роли — будит только явный `--next`; читать шапку, описание и
# последние записи лога (старое — в archive/<ID>-log.md); никаких фоновых помощников/задач внутри сессии.
PROMPT_TEMPLATE = (
    "Ты — {role} команды. Устав: .claude/roles/{role}.md, блокнот: .claude/roles/notes/{role}.md. "
    "Задача: .claude/tickets/{tid}.md — прочитай шапку, описание и последние записи «## Лог» (старые записи "
    "лежат в .claude/tickets/archive/{tid}-log.md — grep только при необходимости). Лимит этого запуска — "
    "{timeout_min} мин. Не запускай в сессии фоновых помощников и фоновых задач; долгая работа — фоновый процесс "
    "на машине (systemd-run) + status: waiting + wait_for (`{tickets_cli} wait {tid} "
    "host:<ssh-алиас>:<путь к файлу итога задания> --by ЧЧ:ММ` (срок обязателен, GMT+4); формы: host:<ssh-алиас>:<путь | unit:имя>, file:<путь>, ticket:<ID>), и выйди, "
    "не жди в сессии. Трать минимум: "
    "самый короткий путь к результату задачи; траты каждого запуска записываются и сравниваются с "
    "результатом. Сделай следующий шаг и сдай итог командой "
    "`{tickets_cli} result {tid} <{verbs}> --why \"что сделал, что дальше\"` (--why — не длиннее 200 знаков) "
    "ДО истечения лимита (доказательство: {proofs}; неполная команда — отказ с подсказкой): итог обязателен, "
    "частичный прогресс не провал. "
    "Кого будить дальше и status/wait_for ставит таблица маршрутов — --next и шапку руками не правь; "
    "blocked (задача встала) и ask-owner (развилка, вопрос) уходят Судье; владельцу вопрос о содержании "
    "исследования (какие гипотезы, гиперпараметры, метрики, что хотим увидеть) переадресует Судья; CEO не будят. "
    "`{tickets_cli} comment {tid} --author {role} --text \"...\"` — только промежуточная заметка без смены "
    "хода. @упоминания в тексте никого не будят."
)


def _read_mcp_servers(path: Path, project_key: str | None = None) -> dict:
    """`mcpServers` из json-файла (`~/.claude.json`, `.mcp.json`); с `project_key` — ещё и записи проекта
    (`projects[<путь>].mcpServers`, они перекрывают пользовательские). Нет файла/битый json — пусто."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out = dict(data.get("mcpServers") or {}) if isinstance(data, dict) else {}
    if project_key and isinstance(data, dict):
        projects = data.get("projects") or {}
        for k, v in projects.items():
            if isinstance(v, dict) and PV.norm(k) == PV.norm(project_key):
                out.update(v.get("mcpServers") or {})
    return out


def role_mcp_args(run_stem: str) -> list[str]:
    """TK-171: MCP-серверы роли — `role_mcp` из `.claude/rpv.json`. Нет ключа/`all` — флагов нет, роль видит всё, что видит обычный
    `claude` (поведение до 1.8.26); `none` — `--strict-mcp-config` без конфига, ни одного сервера (TK-140: свой
    Playwright на каждый запуск ≈ 600 МБ); список имён — временный json только с этими серверами
    (user `~/.claude.json`, проектные записи оттуда, `<проект>/.mcp.json`; позднее перекрывает раннее) и
    `--strict-mcp-config --mcp-config <json>`. Имени нет ни в одном источнике — предупреждение в stderr, сервер пропущен."""
    mode = P.setting("role_mcp", "all")
    word = mode.strip().lower() if isinstance(mode, str) else ""
    if word in ("all", "none"):
        return [] if word == "all" else ["--strict-mcp-config"]
    names = [str(n).strip() for n in (mode if isinstance(mode, list) else str(mode).split(",")) if str(n).strip()]
    servers = _read_mcp_servers(Path.home() / ".claude.json", str(CFG.PROJECT_ROOT))
    servers.update(_read_mcp_servers(CFG.PROJECT_ROOT / ".mcp.json"))
    picked = {n: servers[n] for n in names if n in servers}
    for n in names:
        if n not in picked:
            print(f"[dispatch] rpv.json role_mcp: сервер {n!r} не найден ни в ~/.claude.json, ни в .mcp.json — пропущен",
                  file=sys.stderr, flush=True)
    cfg = CFG.RUNS_DIR / f"{run_stem}.mcp.json"
    cfg.write_text(json.dumps({"mcpServers": picked}, ensure_ascii=False, indent=2), encoding="utf-8")
    return ["--strict-mcp-config", "--mcp-config", str(cfg)]


def plugin_dir_args() -> list[str]:
    """CP11: `RPV_PLUGIN_DIR` — роли идут с плагином из этой папки (`claude --plugin-dir`, перекрывает установленный
    плагин того же имени). Нужна проверке рабочей копии на чистом проекте (`tools/e2e_clean_project.py`); не задана —
    флага нет, роли видят установленный плагин."""
    d = (P.env("PLUGIN_DIR", "") or "").strip()
    return ["--plugin-dir", d] if d else []


def effort_for(role: str, tkt=None) -> str:
    """Усилие запуска. Судья и Инженер — всегда ROLE_EFFORT (поле `effort:` тикета для них не действует: владелец 14.10).
    Остальные роли — поле `effort:` тикета, иначе умолчание роли (high)."""
    if role in FIXED_EFFORT_ROLES:
        return ROLE_EFFORT[role]
    if tkt is not None and getattr(tkt, "effort", ""):
        return tkt.effort
    return ROLE_EFFORT.get(role, "high")


def _py() -> str:
    """Интерпретатор для команд, которые диспетчер выдаёт ролям: на Mac/Linux слова `python` может не быть (TK-110 В-1); есть в PATH — прежнее `python` (кавычки вокруг пути ломают PowerShell)."""
    return "python " if shutil.which("python") else shlex.quote(Path(sys.executable).as_posix()) + " "


def tickets_cli() -> str:
    """Команда tickets.py из папки плагина: проект роль берёт из RPV_PROJECT (его ставит launch_run) и своего cwd."""
    return _py() + shlex.quote((CODE_DIR / "tickets.py").as_posix())


def build_prompt(role: str, tid: str, run_timeout_s: float, extra_note: str = None) -> str:
    prompt = PROMPT_TEMPLATE.format(role=role, tid=tid, timeout_min=int(run_timeout_s // 60),
                                    tickets_cli=tickets_cli(), verbs="|".join(ROUTES.verbs_for(role)),
                                    proofs=ROUTES.proofs_for(role))
    if extra_note:
        prompt += " " + extra_note
    return prompt


def launch_notes(extra_note, role: str, reason: str, stopped: bool, rotated: bool, stop_note: str):
    """Приписки к промпту запуска (по порядку): заказ диспетчера, остановка CEO, новая сессия, `--next`, заказ CEO после приёмки."""
    notes = [extra_note]
    if stopped:  # прошлый запуск оборван CEO (`tickets.py stop`): сессия новая, в промпте — пометка
        notes.append(stop_note)
    if rotated:  # каждое пробуждение — новая сессия; --resume только у повтора после таймаута (attempt > 0)
        notes.append(f"Начинаем новую сессию: прочитай блокнот .claude/roles/notes/{role}.md и прежние решения "
                     f"docs/research/reviews/ по нужной задаче.")
    if reason == "next":
        notes.append("Запуск по явной передаче (`--next`): прочитай последнюю запись лога, сделай "
                     "запрошенное и запиши итог.")
    if reason == "ceo-order-after-accept":
        notes.append("PR влит, но после приёмки Судьи в логе есть заказ CEO: ответь на него записью — выполнен (чем, "
                     "коммит/PR) или нет (что осталось); не закрывай тикет молча.")
    if reason == "after-merge-step":
        notes.append("PR влит, но в приёмке Судьи или в заказе указан шаг «после слияния:» (тег, выкладка, выпуск): сделай его и "
                     "сдай результат (`result … done --path <доказательство>`); тикет диспетчер не закрывал.")
    return " ".join(x for x in notes if x)


def role_env(role: str, tid: str) -> dict:
    """Окружение дочернего claude: без *HOST_SESSION* (иначе role_context.py/role_memory.py принимают роль за CEO),
    RPV_ROLE/RPV_TICKET/RPV_PROJECT для хуков и tickets.py роли."""
    env = dict(os.environ)
    # Судья 27.09, п.4 «обязательно»: дочерний claude иначе наследует CLAUDE_CODE_HOST_SESSION_ID сессии CEO (диспетчер
    # запущен из неё) — тревоги/inbox/«молчание» ломаются на все роли. RPV_ROLE сама не спасает: find_title()
    # срабатывает раньше при непустом host_id, если переменная не снята.
    for k in list(env):
        if "HOST_SESSION" in k.upper():
            env.pop(k, None)
    env["RPV_ROLE"] = role
    env["RPV_TICKET"] = tid
    env["RPV_PROJECT"] = str(CFG.PROJECT_ROOT)
    return env
