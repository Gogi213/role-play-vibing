# Диспетчер

Раз в 15 с читает `<проект>/.claude/tickets/*.md` и будит роль (`claude -p`) или CEO (строка в `ceo-inbox.md`). Только stdlib, Python 3.11.

Скрипты запускаются прямо из папки плагина (`<плагин>`), копировать их в проект не нужно. Корень проекта — `--project <путь>`
(у `tickets.py` — перед подкомандой), иначе `RPV_PROJECT`, иначе `CLAUDE_PROJECT_DIR`, иначе ближайший каталог вверх от
текущего с `.claude/roles` (не расположение файла); не нашли — ошибка с подсказкой `--project` / `/rpv-init`, каталоги не
создаются. Состояние (`state.json`, `runs.log`, `runs/`, pid, `ceo-inbox.md`, `ceo-wake.log`, сердцебиение сторожа) — в
`<проект>/.claude/dispatcher/` (создаётся при старте); тикеты и роли — из проекта.

```
python <плагин>/.claude/dispatcher/start.py [--project <проект>]    # диспетчер + сторож в фоне, уже запущенные перезапускает (/rpv-start)
python <плагин>/.claude/dispatcher/dispatch.py --project <проект>   # диспетчер, боевой цикл (--once — один тик; --help — справка)
python <плагин>/.claude/dispatcher/watch.py --project <проект>      # сторож: диспетчер жив, сироты, blocked (--once — один цикл)
python <плагин>/.claude/dispatcher/tickets.py --project <проект> new --owner engineer --title "..." [--reviewer judge] [--effort high]
python <плагин>/.claude/dispatcher/tickets.py --project <проект> comment TK-001 --author engineer --text "..." [--next judge|ceo]
python <плагин>/.claude/dispatcher/tickets.py --project <проект> start TK-001 | status
python -m unittest discover -s <плагин>/.claude/dispatcher && python -m unittest discover -s <плагин>/.claude/hooks   # тесты
```

Роль, запущенная диспетчером, получает в окружении `RPV_ROLE`, `RPV_TICKET`, `RPV_PROJECT` (и `ALPHA_ROLE`, `ALPHA_TICKET` —
для хуков, читающих прежние имена); в промпте — команда `tickets.py` с абсолютным путём из папки плагина (то же хук
`role_context.py` вставляет в начало каждой сессии роли и CEO).

Статусы: backlog, todo, in_progress, waiting (+ `wait_for: file:… | ticket:… | deck:…`), in_review, done, blocked,
needs_owner. Будят: `todo`, `in_progress`, выполненный `wait_for` — владельца; `done`/`in_review` с `reviewer` —
ревьюера; `--next` — названную роль один раз. Запуск без новой записи — один повтор, затем `blocked`. Траты считаются
(`runs.log`, `tickets.py status`), ничего не ограничивают.

Переменные окружения (умолчания в скобках). Имена `RPV_*`; если `RPV_<имя>` не задана, берётся прежняя `ALPHA_<имя>`:

| переменная | смысл |
|---|---|
| `CLAUDE_BIN` | путь к `claude` (из PATH) |
| `RPV_DISPATCH_INTERVAL` / `_MAX_PARALLEL` / `_TIMEOUT` | тик (15 с) / запусков сразу (3) / таймаут запуска (1200 с) |
| `RPV_DISPATCH_MIN_GAP_S` / `_MAX_RUNS_PER_TICKET_HOUR` | пауза между запусками тикета (60 с) / запусков в час (6) |
| `RPV_DISPATCH_MAX_SAME_STATUS_RUNS` / `_SAME_STATUS_WARN_RUNS` | запусков подряд с записью, но без смены статуса: на половине порога одна строка CEO, на пороге → `blocked` (12 и 0 = половина, назначено) |
| `RPV_DISPATCH_MAX_REVIEW_RETURNS` | сколько раз ревьюер может вернуть работу; после предела тикет, снова пришедший на ревью, уходит CEO (`next: ceo`), ревьюера не будим (3, назначено) |
| `RPV_DISPATCH_MAX_IDLE_RUNS` | запусков подряд без записи и без смены статуса → `blocked` (2, назначено) |
| `RPV_DISPATCH_ROTATE_TOKENS` | контекст, после которого сессия роли начинается заново (120000) |
| `RPV_DISPATCH_MODEL` / `_ROLE_MODEL` / `_EFFORT` | модель и усилие ролей, `роль:значение,…` |
| `RPV_WATCH_INTERVAL` / `_ORPHAN_HOURS` | цикл сторожа (120 с) / порог сирот (2 ч) |
| `RPV_DECK_HOST` (+ `_KEY`, `_KNOWN_HOSTS`, `_ROOT`) | вторая машина для ssh-проверок (`_ROOT` — корень очереди на ней, `~/rpv`); **нет хоста — проверки выключены**; флаг-файл `<проект>/.claude/dispatcher/deck-off` — тоже |
| `RPV_GUARD_REMOTE_ROOTS` / `_HOST_ROOTS` / `_STAGE` / `_FORBIDDEN_HOSTS` | страж удаления: где можно удалять на любых удалённых машинах / только при ssh на хост (`хост=корень,корень;хост2=…`) / стадия / закрытые хосты (через запятую; нет — нигде) |

`git reset --hard` и `git clean -f` страж пропускает только в каталоге вне основного дерева, заданном явным путём (scratchpad, корни из переменных выше, `/tmp/<подкаталог>`, связанный worktree); `git push --force` — никогда. Перезапись файлов в автопамяти проекта (`~/.claude/projects/<проект>/memory/`) разрешена.
