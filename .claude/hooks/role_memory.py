"""Память ролей команды без внешних сервисов.

- UserPromptSubmit: подсказки минимальные и без повторов. Команда `/ceo` в сообщении ставит метку CEO текущей
  сессии (запасной путь к метке, которую ставит сама команда).
  Роль (не CEO): строка «молчание» — итог в лог тикета, не текстом в ход.
  CEO: одна строка «[диспетчер] N новых в ceo-inbox.md» — только когда пришли новые; сторож и разрешения —
  по разу на 10 сообщений; сторож контекста — от 450 тыс. токенов (45 % окна), не чаще раза в 10 сообщений.
- SessionEnd (клир, выход, остановка): конспект разговора — сообщения владельца/диспетчера и сессий, ответы
  (без инструментов и рассуждений) — в `.claude/roles/log/<роль>/`. Не сработал — `role_context.py` на следующем
  старте догоняет конспект по записанному пути.
- Ключ состояния сессии: для запуска диспетчера — `RPV_ROLE[-<RPV_TICKET>]` (раньше все роли писали в
  `unknown.json` и конспекты путались), для Desktop — id сессии приложения. Переменные — `RPV_*`, запасные `ALPHA_*`.
Хук никогда не падает и не блокирует: ошибка — тишина.
"""
import datetime
import glob
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from role_context import (ROLE_NAMES, ROLES, ROOT, STATE_DIR, env, env_role, find_title, read_label,  # noqa: E402
                          ticket_id, write_label)

# каталоги можно переопределить окружением — для проверок хука, чтобы не трогать рабочее состояние
LOG_DIR = env("LOG_DIR") or os.path.join(ROOT, ".claude", "roles", "log")
GMT4 = datetime.timezone(datetime.timedelta(hours=4))
REPEAT_EVERY = 10  # сообщений: самое частое повторение одной и той же подсказки


def read_stdin():
    try:
        return json.loads(sys.stdin.buffer.read().decode("utf-8", "replace"))
    except Exception:
        return {}


def current_role(hook_in=None):
    """(название, роль) текущей сессии или (название, None). Порядок как в role_context.py: `RPV_ROLE` (диспетчерский
    запуск `claude -p`; иначе, если launch_run не снял CLAUDE_CODE_HOST_SESSION_ID сессии CEO из env, поиск названия
    принял бы роль за CEO), метка сессии (`/ceo`), последним — название сессии Desktop."""
    er = env_role()
    if er:
        tid = ticket_id()
        return f"RPV_ROLE={er}" + (f" {tid}" if tid else ""), er
    lr = read_label((hook_in or {}).get("session_id"))
    if lr:
        return ROLE_NAMES[lr], lr
    host_id = os.environ.get("CLAUDE_CODE_HOST_SESSION_ID")
    found, title = find_title(host_id, None) if host_id else (False, None)
    if not found or not title:
        return None, None
    return title, next((r for key, r in ROLES if key in title.lower()), None)


def state_key(role=None):
    """Имя файла состояния: запуск диспетчера — роль (+ тикет); Desktop — id сессии приложения; иначе роль."""
    er = env_role()
    if er:
        tid = ticket_id()
        key = er + (f"-{tid}" if tid else "")
    else:
        key = os.environ.get("CLAUDE_CODE_HOST_SESSION_ID") or role or "unknown"
    return re.sub(r"[^A-Za-z0-9_.-]", "_", key)


def state_path(role=None):
    return os.path.join(STATE_DIR, state_key(role) + ".json")


def load_state(role=None):
    try:
        with open(state_path(role), encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def save_state(state, role=None):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = state_path(role) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False)
    os.replace(tmp, state_path(role))


def stamp(iso):
    try:
        t = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return t.astimezone(GMT4).strftime("%d.%m %H:%M")
    except Exception:
        return "?"


def digest_dir(role):
    """Каталог конспектов: запуск диспетчера по тикету (`RPV_TICKET`) — подкаталог тикета (роль, взявшая новый
    тикет, не получает конспект чужого; аудит 03.10), иначе каталог роли."""
    base = os.path.join(LOG_DIR, role)
    if env_role():
        tid = ticket_id()
        if tid:
            return os.path.join(base, tid)
    return base


def digest_path(role, cli_id):
    """Путь конспекта: существующий для этой сессии CLI или новый по текущему времени."""
    old = glob.glob(os.path.join(glob.escape(digest_dir(role)), f"*-{cli_id[:8]}.md"))
    if old:
        return old[0]
    name = datetime.datetime.now(GMT4).strftime("%Y-%m-%d_%H%M") + f"-{cli_id[:8]}.md"
    return os.path.join(digest_dir(role), name)


def write_digest(transcript, role, title, cli_id, why, dispatcher=None):
    """Конспект разговора из транскрипта; возвращает путь или None (пустой разговор).
    Запуск диспетчера: сообщения пользователя в транскрипте — промпты диспетчера, не владельца."""
    if not transcript or not cli_id or not os.path.isfile(transcript):
        return None
    if dispatcher is None:
        dispatcher = env_role() is not None
    sender = "диспетчер" if dispatcher else "владелец"
    turns = []
    with open(transcript, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("isSidechain"):
                continue
            msg = d.get("message") or {}
            content = msg.get("content")
            when = stamp(d.get("timestamp", ""))
            if d.get("type") == "user" and isinstance(content, str):
                who = "сообщение сессии" if d.get("isMeta") else sender
                if d.get("isMeta") and "cross-session-message" not in content:
                    continue
                turns.append(f"### {when} · {who}\n{content.strip()}\n")
            elif d.get("type") == "assistant" and isinstance(content, list):
                text = "\n".join(b.get("text", "") for b in content if b.get("type") == "text").strip()
                if text:
                    turns.append(f"### {when} · ответ\n{text}\n")
    if not turns:
        return None
    path = digest_path(role, cli_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    head = (f"# Конспект сессии «{title}» ({why})\n\n"
            f"Сессия CLI `{cli_id}`, полный транскрипт: `{transcript}`. Только сообщения и ответы — "
            f"без инструментов и рассуждений. Читать секциями/грепом, не целиком.\n\n")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(head + "\n".join(turns))
    os.replace(tmp, path)
    return path


def latest_digest(role, exclude_cli):
    files = sorted(glob.glob(os.path.join(glob.escape(digest_dir(role)), "*.md")))
    files = [f for f in files if not (exclude_cli and f.endswith(f"-{exclude_cli[:8]}.md"))]
    return files[-1] if files else None


def on_session_start(hook_in, role, title):
    """Из role_context.py: догнать конспект прошлой сессии, запомнить текущую; путь прошлого конспекта."""
    cli_id, transcript = hook_in.get("session_id"), hook_in.get("transcript_path")
    state = load_state(role)
    prev_cli, prev_tr = state.get("cli"), state.get("transcript")
    if prev_cli and prev_cli != cli_id and not glob.glob(os.path.join(
            glob.escape(digest_dir(role)), f"*-{prev_cli[:8]}.md")):
        write_digest(prev_tr, role, title, prev_cli, "догнан при следующем старте")
    if prev_cli != cli_id:
        save_state({"cli": cli_id, "transcript": transcript, "n": 0}, role)
    return latest_digest(role, cli_id)


# --- подсказки на сообщение -----------------------------------------------------------------------

# правило устава «роль не пишет текст в ход» не держится после клира; стиль вывода включается только на новом
# процессе, эта строка — на каждом сообщении
SILENT_TEXT = ("[роль: молчание] Текста в ход не писать: ни между инструментами, ни пересказом. Итог/вопрос — в лог "
               "тикета (итог шага — `tickets.py result <ID> <done|pr|accept|return|blocked|ask-owner|wait> --why \"...\"`); сообщение "
               "диспетчера или другой сессии — не владелец. Конец хода — одна строка ≤ 80 знаков или ничего. "
               "Исключение — владелец сам написал в эту сессию.")


def throttled(state, key, sig, text):
    """Показать подсказку, если изменилась её суть (sig) или с прошлого показа прошло ≥ REPEAT_EVERY сообщений."""
    if not text:
        return None
    n = state.get("n", 0)
    last = (state.setdefault("shown", {})).get(key) or {}
    if last.get("sig") == sig and n - last.get("n", -10 ** 9) < REPEAT_EVERY:
        return None
    state["shown"][key] = {"sig": sig, "n": n}
    return text


# Судья TK-002 п.2а («кто сторожит сторожа», v1.4): сторож .claude/dispatcher/watch.py пишет отметку
# сердцебиения на каждый цикл; здесь только её возраст (сам сторож — свой процесс, не этот хук).
DISPATCHER_DIR = env("DISPATCHER_DIR") or os.path.join(ROOT, ".claude", "dispatcher")  # для проверок
WATCH_HEARTBEAT_FILE = os.path.join(DISPATCHER_DIR, "watch-heartbeat.json")
WATCH_STALE_S = 2 * 120  # 2 интервала сторожа (WATCH_INTERVAL_S по умолчанию в watch.py — 120 с)


def watch_alert():
    """(суть, текст) или None: сторож жив — молчим."""
    try:
        with open(WATCH_HEARTBEAT_FILE, encoding="utf-8") as f:
            hb = json.load(f)
    except (OSError, ValueError):
        return ("missing", "[сторож] .claude/dispatcher/watch-heartbeat.json не найден — сторож CEO (watch.py) "
                           "ни разу не отчитался или не запущен.")
    try:
        ts = datetime.datetime.fromisoformat(str(hb.get("ts", "")).replace("Z", "+00:00"))
        age = (datetime.datetime.now(ts.tzinfo) - ts).total_seconds()
    except Exception:
        return "broken", "[сторож] watch-heartbeat.json повреждён — не разобрать время последнего цикла."
    if age > WATCH_STALE_S:
        return "stale", f"[сторож] сердцебиение watch.py устарело на {age / 60:.0f} мин — проверить, жив ли сторож."
    return None


# Диспетчер задач (.claude/dispatcher/): непрочитанные строки ceo-inbox.md — одна короткая строка; сами строки CEO
# читает из файла. Отметка прочитанного — число уже показанных строк; пока новых нет, подсказки нет.
DISPATCHER_CEO_INBOX = os.path.join(DISPATCHER_DIR, "ceo-inbox.md")
DISPATCHER_CEO_INBOX_SEEN = os.path.join(DISPATCHER_DIR, ".ceo-inbox-seen")


def ceo_inbox_alert():
    try:
        with open(DISPATCHER_CEO_INBOX, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return None
    try:
        with open(DISPATCHER_CEO_INBOX_SEEN, encoding="utf-8") as f:
            seen = int(f.read().strip() or 0)
    except (OSError, ValueError):
        seen = 0
    new = [ln for ln in lines[seen:] if ln.strip()]
    if len(lines) != seen:
        try:
            with open(DISPATCHER_CEO_INBOX_SEEN, "w", encoding="utf-8") as f:
                f.write(str(len(lines)))
        except OSError:
            pass
    if not new:
        return None
    return f"[диспетчер] {len(new)} новых в .claude/dispatcher/ceo-inbox.md"


# Сводка сортировщика (ceo_triage.py, TK-086): «сведения» копятся в ceo-digest.md и показываются CEO на сообщении
# владельца один раз — будильник (Monitor) не нужен. Отметка прочитанного — число показанных строк.
DISPATCHER_CEO_DIGEST = os.path.join(DISPATCHER_DIR, "ceo-digest.md")
DISPATCHER_CEO_DIGEST_SEEN = os.path.join(DISPATCHER_DIR, ".ceo-digest-seen")
DIGEST_SHOW_MAX = 15


def ceo_digest_alert():
    try:
        with open(DISPATCHER_CEO_DIGEST, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return None
    try:
        with open(DISPATCHER_CEO_DIGEST_SEEN, encoding="utf-8") as f:
            seen = int(f.read().strip() or 0)
    except (OSError, ValueError):
        seen = 0
    new = [ln.rstrip() for ln in lines[seen:] if ln.strip()]
    if len(lines) != seen:
        try:
            with open(DISPATCHER_CEO_DIGEST_SEEN, "w", encoding="utf-8") as f:
                f.write(str(len(lines)))
        except OSError:
            pass
    if not new:
        return None
    more = f" (и ещё {len(new) - DIGEST_SHOW_MAX} в ceo-digest.md)" if len(new) > DIGEST_SHOW_MAX else ""
    return "\n".join(["[сводка сортировщика] сведения без действий CEO:"] + new[-DIGEST_SHOW_MAX:]) + more


# вызов роли может часами ждать подтверждения в её сессии, куда никто не смотрит. Уведомление роли «нужно
# разрешение» пишется меткой; CEO видит её на своём сообщении.
PENDING = os.path.join(STATE_DIR, "pending-permission.json")


def on_notification(hook_in, role):
    if role == "ceo":
        return
    msg = hook_in.get("message") or ""
    if "permission" not in (msg + " " + (hook_in.get("notification_type") or "")).lower():
        return
    try:
        with open(PENDING, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    os.makedirs(STATE_DIR, exist_ok=True)
    data[role] = {"ts": time.time(), "message": msg[:200]}
    with open(PENDING, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def pending_permissions():
    """(суть, текст) или None."""
    try:
        with open(PENDING, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    fresh = {r: v for r, v in data.items() if time.time() - v.get("ts", 0) < 3 * 3600}
    if not fresh:
        return None
    items = "; ".join(f"{r} с {datetime.datetime.fromtimestamp(v['ts']).strftime('%H:%M')} — {v.get('message', '')}"
                      for r, v in fresh.items())
    sig = ",".join(f"{r}@{int(v['ts'])}" for r, v in sorted(fresh.items()))
    return sig, (f"[разрешения] роль ждёт подтверждения в своей сессии: {items}. Если ещё стоит — сразу сказать "
                 "владельцу, какая сессия и что подтвердить. Разобрано — удалить запись роли из "
                 ".claude/roles/.state/pending-permission.json.")


# Сторож контекста: порог по умолчанию — 45 % окна 1 млн токенов. Только для сессий, где есть владелец, — у запуска
# диспетчера просить клир некого. Переопределение — RPV_CONTEXT_WARN_TOKENS (запасная — ALPHA_CONTEXT_WARN_TOKENS).
CONTEXT_WARN_TOKENS = int(env("CONTEXT_WARN_TOKENS", "450000"))


def context_tokens(transcript):
    """Размер контекста по последнему ответу модели в транскрипте (вход + кэш + вывод)."""
    if not transcript or not os.path.isfile(transcript):
        return 0
    with open(transcript, "rb") as fh:
        fh.seek(0, 2)
        start = max(0, fh.tell() - 600_000)
        fh.seek(start)
        lines = fh.read().decode("utf-8", "replace").splitlines()
        if start:
            lines = lines[1:]  # первая строка хвоста может быть оборвана
    for line in reversed(lines):
        if '"usage"' not in line:
            continue
        try:
            usage = (json.loads(line).get("message") or {}).get("usage") or {}
        except Exception:
            continue
        total = sum(int(usage.get(k) or 0) for k in (
            "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens"))
        if total:
            return total
    return 0


def context_advice(hook_in, state):
    """≥ 450 тыс. токенов (45 % окна 1 млн): одна строка, не чаще раза в REPEAT_EVERY сообщений."""
    tokens = context_tokens(hook_in.get("transcript_path"))
    if tokens < CONTEXT_WARN_TOKENS:
        return None
    n = state.get("n", 0)
    last = state.get("ctx_n")
    if last is not None and n - last < REPEAT_EVERY:
        return None
    state["ctx_n"] = n
    return f"контекст ≈ {tokens // 1000} тыс. — закончи шаг, обнови блокнот и попроси владельца сделать клир"


def on_prompt_all(hook_in, role):
    """Подсказки к сообщению (или None). Состояние — по ключу роли/тикета/сессии."""
    cli_id = hook_in.get("session_id")
    state = load_state(role)
    if state.get("cli") != cli_id:
        state = {"cli": cli_id, "transcript": hook_in.get("transcript_path"), "n": 0}
    state["n"] = state.get("n", 0) + 1
    parts = []
    if role != "ceo":
        parts.append(SILENT_TEXT)
    else:
        for getter in (pending_permissions, watch_alert):
            try:
                got = getter()
            except Exception:
                got = None
            if got:
                parts.append(throttled(state, getter.__name__, got[0], got[1]))
        try:
            parts.append(ceo_inbox_alert())
        except Exception:
            pass
        try:
            parts.append(ceo_digest_alert())
        except Exception:
            pass
    if env_role() is None:
        try:
            parts.append(context_advice(hook_in, state))
        except Exception:
            pass
    try:
        save_state(state, role)
    except Exception:
        pass
    return "\n".join(t for t in parts if t) or None


# сообщение владельца — команда `/ceo` (или `/<плагин>:ceo`): метка CEO ставится хуком сразу, не дожидаясь, пока
# команда выполнит свой шаг (без Bash, без подстановки id сессии в тексте команды)
CEO_COMMAND = re.compile(r"^\s*/(?:[\w.-]+:)?ceo(?:\s|$)", re.I)


def mark_ceo_on_command(hook_in):
    if env_role() is None and CEO_COMMAND.match(hook_in.get("prompt") or ""):
        write_label("ceo", hook_in.get("session_id"))


def main():
    hook_in = read_stdin()
    event = hook_in.get("hook_event_name")
    try:
        if event == "PostToolUse":  # напоминаний после инструментов нет
            return 0
        if event == "UserPromptSubmit":
            try:
                mark_ceo_on_command(hook_in)
            except Exception:
                pass
        title, role = current_role(hook_in)
        if role is None:
            return 0
        if event == "UserPromptSubmit":
            text = on_prompt_all(hook_in, role)
            if text:
                sys.stdout.write(json.dumps({"hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit", "additionalContext": text}}, ensure_ascii=True))
        elif event == "Notification":
            on_notification(hook_in, role)
        elif event == "SessionEnd":
            write_digest(hook_in.get("transcript_path"), role, title, hook_in.get("session_id"),
                         f"закрытие: {hook_in.get('reason', '?')}")
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
