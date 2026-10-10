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
python <плагин>/.claude/dispatcher/watch.py --project <проект>      # сторож: сироты, blocked (--once — один цикл)
python <плагин>/.claude/dispatcher/tickets.py --project <проект> new --owner engineer --title "..." [--reviewer judge] [--effort high]
python <плагин>/.claude/dispatcher/tickets.py --project <проект> comment TK-001 --author engineer --text "..." [--next judge|ceo]
python <плагин>/.claude/dispatcher/tickets.py --project <проект> start TK-001 | status
python <плагин>/.claude/dispatcher/tickets.py --project <проект> stop TK-001 --text "<новая постановка>" [--next judge]   # только CEO
python -m unittest discover -s <плагин>/.claude/dispatcher && python -m unittest discover -s <плагин>/.claude/hooks   # тесты
```

`start.py` запускает службы отвязанными от сессии (Windows — WMI `Win32_Process.Create`, вне job-объекта приложения; Linux с systemd — `systemd-run --user`, юнит `rpv-<служба>-<хеш пути проекта>`; иначе `Popen`) и печатает «отвязан: да/нет»; чтобы юниты пережили выход из системы — `loginctl enable-linger $USER` (перезапуск юнита гасит и запущенные роли).

Роль, запущенная диспетчером, получает в окружении `RPV_ROLE`, `RPV_TICKET`, `RPV_PROJECT` (и `ALPHA_ROLE`, `ALPHA_TICKET` —
для хуков, читающих прежние имена); в промпте — команда `tickets.py` с абсолютным путём из папки плагина (то же хук
`role_context.py` вставляет в начало каждой сессии роли и CEO).

Статусы: backlog, todo, in_progress, waiting (+ `wait_for: file:… | ticket:… | host:…`), in_review, done, blocked,
needs_owner, stopped (остановлено CEO — не будит, пока CEO не вернёт в `todo`). Будят: `todo`, `in_progress`, выполненный `wait_for` — владельца; `done`/`in_review` с `reviewer` —
ревьюера; `--next` — названную роль один раз. Запуск без новой записи — один повтор, затем `blocked`. Траты считаются
(`runs.log`, `tickets.py status`), ничего не ограничивают.

Остановка роли — `tickets.py stop TK-NN --text "<новая постановка>" [--next <роль>]` (только CEO; из сессии роли, где стоит `RPV_ROLE`, отказ): заявка `<проект>/.claude/dispatcher/stop/<ID>.json`; на ближайшем тике диспетчер снимает запущенную роль этого тикета ВСЕМ деревом процессов (Windows — `taskkill /T /F /PID`, иначе — `killpg` группы: роль запускается с `start_new_session=True`) и проверяет, что процесс умер (не умер — строка CEO `[stop-failed]`, заявка остаётся).
Остановка — не провал: ни повтора, ни пометки «запуск не оставил запись»; счётчики холостых и «запись есть, статус тот же» запусков, повторов, ротации (сессия и контекст тикета) и часовой лимит запусков по тикету — с нуля.
След: в лог — запись `dispatcher` «остановлен CEO в ЧЧ:ММ», затем запись CEO с постановкой (последняя в логе); следующий запуск тикета — НОВАЯ сессия (без `--resume`), в промпте «прошлый запуск оборван CEO — проверь git status и недописанные правки, начни с новой постановки».
Статус: с `--next` — `todo` + `next: <роль>` (роль стартует в этом же тике); без `--next` — `stopped`: диспетчер не будит, сторож не считает сиротой, в `todo` возвращает CEO (`tickets.py start TK-NN` или правка шапки).
Ограничение: задания на машинах (`systemd-run`, ssh) диспетчер не останавливает — их снимает CEO; запущенный диспетчер видит заявку только после перезапуска (`/rpv-start`).

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
| `RPV_DECK_HOST` (+ `_KEY`, `_KNOWN_HOSTS`, `_ROOT`) | хост для `wait_for` `host:deck:…` (ключ и known_hosts); сторож вторую машину не опрашивает |
| `RPV_GUARD_REMOTE_ROOTS` / `_HOST_ROOTS` / `_STAGE` / `_FORBIDDEN_HOSTS` | страж удаления: где можно удалять на любых удалённых машинах / только при ssh на хост (`хост=корень,корень;хост2=…`) / стадия / закрытые хосты (через запятую; нет — нигде) |

`git reset --hard` и `git clean -f` страж пропускает только в каталоге вне основного дерева, заданном явным путём (scratchpad, корни из переменных выше, `/tmp/<подкаталог>`, связанный worktree); `git push --force` — никогда. Перезапись файлов в автопамяти проекта (`~/.claude/projects/<проект>/memory/`) разрешена.
