# Диспетчер

Раз в 15 с читает `.claude/tickets/*.md` и будит роль (`claude -p`) или CEO (строка в `ceo-inbox.md`). Только stdlib, Python 3.11.

```
python .claude/dispatcher/dispatch.py          # диспетчер, боевой цикл (--once — один тик; --help — справка)
python .claude/dispatcher/watch.py             # сторож: диспетчер жив, сироты, blocked (--once — один цикл)
python .claude/dispatcher/tickets.py new --owner engineer --title "..." [--reviewer judge] [--effort high]
python .claude/dispatcher/tickets.py comment TK-001 --author engineer --text "..." [--next judge|ceo]
python .claude/dispatcher/tickets.py start TK-001 | status
python -m unittest discover -s .claude/dispatcher && python -m unittest discover -s .claude/hooks   # тесты
```

Статусы: backlog, todo, in_progress, waiting (+ `wait_for: file:… | ticket:… | deck:…`), in_review, done, blocked,
needs_owner. Будят: `todo`, `in_progress`, выполненный `wait_for` — владельца; `done`/`in_review` с `reviewer` —
ревьюера; `--next` — названную роль один раз. Запуск без новой записи — один повтор, затем `blocked`. Траты считаются
(`runs.log`, `tickets.py status`), ничего не ограничивают.

Переменные окружения (умолчания в скобках):

| переменная | смысл |
|---|---|
| `CLAUDE_BIN` | путь к `claude` (из PATH) |
| `ALPHA_DISPATCH_INTERVAL` / `_MAX_PARALLEL` / `_TIMEOUT` | тик (15 с) / запусков сразу (3) / таймаут запуска (1200 с) |
| `ALPHA_DISPATCH_MIN_GAP_S` / `_MAX_RUNS_PER_TICKET_HOUR` | пауза между запусками тикета (60 с) / запусков в час (6) |
| `ALPHA_DISPATCH_MAX_SAME_STATUS_RUNS` | запусков подряд с записью, но без смены статуса, → `blocked` (6, назначено) |
| `ALPHA_DISPATCH_MAX_IDLE_RUNS` | запусков подряд без записи и без смены статуса → `blocked` (2, назначено) |
| `ALPHA_DISPATCH_ROTATE_TOKENS` | контекст, после которого сессия роли начинается заново (120000) |
| `ALPHA_DISPATCH_MODEL` / `_ROLE_MODEL` / `_EFFORT` | модель и усилие ролей, `роль:значение,…` |
| `ALPHA_WATCH_INTERVAL` / `_ORPHAN_HOURS` | цикл сторожа (120 с) / порог сирот (2 ч) |
| `ALPHA_DECK_HOST` (+ `_KEY`, `_KNOWN_HOSTS`) | вторая машина для ssh-проверок; **нет хоста — проверки выключены**; флаг-файл `.claude/dispatcher/deck-off` — тоже |
| `ALPHA_GUARD_REMOTE_ROOTS` / `_STAGE` / `_FORBIDDEN_HOSTS` | страж удаления: где можно удалять на удалённых машинах / стадия / закрытые хосты (через запятую; нет — нигде) |
