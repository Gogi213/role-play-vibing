# Настройка: переменные окружения

Все переменные плагина называются `RPV_*`. Если `RPV_<имя>` не задана, берётся прежняя `ALPHA_<имя>` (они работают как запасные). Пустое значение считается «не задано».

Службу запускает `/rpv-start` (`start.py`): она получает только `RPV_*`, `ALPHA_*`, `CLAUDE_BIN`, `CLAUDE_PROJECT_DIR`, `CLAUDE_CONFIG_DIR` и `PATH` (на POSIX); секреты (`ANTHROPIC_*`, токены) не переносятся. Присмотр ОС запоминает `RPV_*` и `CLAUDE_*` в `supervise.env.json` без секретов — [reliability.md](reliability.md#присмотр-за-диспетчером-и-сторожем).

Краткая таблица главных переменных — в [README](../README.md#настройка).

## Проект и роли

| переменная | по умолчанию | смысл |
|---|---|---|
| `RPV_PROJECT` | — | корень проекта; порядок поиска: `--project`, `RPV_PROJECT`, `CLAUDE_PROJECT_DIR`, ближайший каталог вверх от текущего с `.claude/roles` |
| `CLAUDE_BIN` | `claude` из `PATH` | путь к Claude Code CLI |
| `CLAUDE_PROJECT_DIR`, `CLAUDE_CONFIG_DIR` | — | задаёт Claude Code; переносятся службам |
| `RPV_ROLE` | — | роль сессии; выставляет диспетчер при запуске роли (хуки читают её, чтобы вставить устав) |
| `RPV_TICKET` (`RPV_TICKET_ID`) | — | тикет запуска; выставляет диспетчер |
| `RPV_STATE_DIR` | `<проект>/.claude/roles/.state` | метки сессий и состояние хуков |
| `RPV_LOG_DIR` | `<проект>/.claude/roles/log` | конспекты сессий ролей (пишет хук `SessionEnd`) |
| `RPV_DISPATCHER_DIR` | `<проект>/.claude/dispatcher` | где хуки ищут состояние диспетчера |
| `RPV_CONTEXT_WARN_TOKENS` | `450000` | порог размера контекста, с которого CEO предупреждают о клире (45 % окна в 1 млн токенов) |

## Диспетчер (`RPV_DISPATCH_*`)

| переменная | по умолчанию | смысл |
|---|---|---|
| `RPV_DISPATCH_INTERVAL` | `15` | тик, с |
| `RPV_DISPATCH_MAX_PARALLEL` | `3` | сколько запусков ролей одновременно |
| `RPV_DISPATCH_ROLE_PARALLEL` | пусто (по одному на роль) | предел запусков по ролям, `engineer:3,researcher:1`; на один тикет — всегда один запуск роли |
| `RPV_DISPATCH_TIMEOUT` | `1200` | таймаут запуска роли, с |
| `RPV_DISPATCH_MIN_GAP_S` | `60` | пауза между запусками одного тикета, с |
| `RPV_DISPATCH_MAX_RUNS_PER_TICKET_HOUR` | `6` | запусков тикета в час |
| `RPV_DISPATCH_MAX_SAME_STATUS_RUNS` | `12` | запусков подряд с записью, но без смены статуса: на половине порога одна строка CEO, на пороге тикет становится `blocked` |
| `RPV_DISPATCH_SAME_STATUS_WARN_RUNS` | `0` (половина `…MAX_SAME_STATUS_RUNS`) | порог предупреждения CEO |
| `RPV_DISPATCH_MAX_IDLE_RUNS` | `2` | запусков подряд без записи и без смены статуса → `blocked` |
| `RPV_DISPATCH_MAX_REVIEW_RETURNS` | `3` | сколько раз ревьюер может вернуть работу; после предела тикет, снова пришедший на ревью, уходит CEO (`next: ceo`), ревьюера не будим |
| `RPV_DISPATCH_STOP_VERIFY_S` | `10` | сколько ждать смерти процесса после `tickets.py stop`, с |
| `RPV_DISPATCH_MODEL` | `claude-sonnet-5-5` | модель ролей |
| `RPV_DISPATCH_ROLE_MODEL` | Судья — `claude-opus-5-5`, остальные — `RPV_DISPATCH_MODEL` | модель по ролям, `judge:…,engineer:…` или одно значение на все роли |
| `RPV_DISPATCH_EFFORT` | Судья `xhigh`, Инженер и Исследователь `high` | усилие по ролям, `judge:xhigh,engineer:high` или одно значение на все; поле `effort` тикета перекрывает |
| `RPV_DISPATCH_HAIKU_MODEL` | `claude-haiku-5-5` | модель для механических задач (`executor: haiku`) |
| `RPV_DISPATCH_SESSION_SCOPE` | `judge:ticket,researcher:ticket,engineer:ticket` | область сессии роли: `ticket` — своя сессия на тикет, `role` — одна долгая сессия на все тикеты |
| `RPV_DISPATCH_ROTATE_TOKENS` | `120000` | контекст прошлого запуска, после которого роль начинает новую сессию |
| `RPV_DISPATCH_WAIT_POLL_S` | `300` | период запасного ssh-опроса `wait_for host:…`, с |
| `RPV_DISPATCH_WAIT_RECON_S` | `300` | период сверки `wait_for host:…` одним ssh на машину (с шиной), с |
| `RPV_DISPATCH_BUS_DOWN_SSH_S` | `600` | через сколько секунд лежания шины включается аварийный ssh-опрос, с |
| `RPV_WATCHED_ALIASES` | не задан | машины со сторожем (`calc,vps`): сверка, закрывшая условие без события с такой машины, даёт тревогу `recon-miss` |
| `RPV_DISPATCH_DECK_CACHE_S` | `60` | кэш ssh-проверок `wait_for`, с |
| `RPV_DISPATCH_ON_MET_TIMEOUT_S` | `120` | таймаут команды `on_met`, с |
| `RPV_DISPATCH_SUMMARY_HOURS` | `1` | как часто копящиеся некритичные сигналы уходят CEO одной строкой, ч |
| `RPV_DISPATCH_INVARIANT_GRACE_S` | `600` | через сколько секунд открытый тикет без хода считается нарушением инварианта |
| `RPV_TICKET_LOCK_TIMEOUT` | `120` | сколько ждать блокировки тикета, с (диспетчер, `tickets.py` и роли пишут один файл) |
| `RPV_PROGRESS_DIR` | `~/rpv/progress` (у табло — не задан) | каталог файлов хода заданий на машинах |
| `RPV_IDLE_SLO_MIN` | `10` | допустимый простой, минут в сутки ([reliability.md](reliability.md#счётчик-простоя)) |

## Сторож (`RPV_WATCH_*`)

| переменная | по умолчанию | смысл |
|---|---|---|
| `RPV_WATCH_INTERVAL` | `120` | цикл, с |
| `RPV_WATCH_REPEAT_HOURS` | `2` | повтор тревоги, пока проблема не снята, ч |
| `RPV_WATCH_LONG_REPEAT_HOURS` | `24` | повтор для редких видов находок (сироты, `blocked`, `needs_owner`, тревоги второй машины), ч |
| `RPV_WATCH_DISPATCH_STALE_MIN` | `5` | через сколько минут после последнего тика диспетчер считается неживым |
| `RPV_WATCH_NO_PLAN_MIN` | `30` | через сколько минут «в работе без плана шагов» или «шаги на табло не менялись» сторож пишет роли в тикет и будит её |
| `RPV_WATCH_PLAN_LAG_MIN` | `5` | допуск: запись роли в логе не считается «новее плана», если позже обновления шагов меньше чем на столько минут |
| `RPV_WATCH_ORPHAN_HOURS` | `2` | через сколько часов без новой записи открытый тикет считается сиротой |
| `RPV_WATCH_DEAD_WAIT_STRIKES` | `2` | подряд мёртвых проверок цели `wait_for` до действия |
| `RPV_WATCH_STALL_RUNS` | `2` | подряд таймаутов или холостых запусков до блока |
| `RPV_WATCH_SUMMARY_HOURS` | `1` | период сводки некритичных находок, ч |

## Вторая машина и ssh

Проверки второй машины (сторож, ssh) выключены, пока не задан `RPV_DECK_HOST`; пустой файл `<проект>/.claude/dispatcher/deck-off` выключает их и при заданном хосте.

| переменная | по умолчанию | смысл |
|---|---|---|
| `RPV_CALC_HOST`, `RPV_VPS_HOST`, `RPV_DECK_HOST` | не заданы | хосты для `wait_for: host:<calc\|vps\|deck>:…` (`user@host` или алиас из `~/.ssh/config`); `RPV_DECK_HOST` — ещё и вторая машина сторожа |
| `RPV_DECK_KEY` | — | ключ ssh (`-i`) |
| `RPV_DECK_KNOWN_HOSTS` | — | файл known_hosts (`-o UserKnownHostsFile=`) |
| `RPV_DECK_ROOT` | `~/rpv` | корень очереди заданий на второй машине |
| `RPV_WATCH_DECK_QUEUE_STALE_MIN` | `10` | через сколько минут без обновления `STATUS` непустая очередь на второй машине считается простоем |
| `RPV_WATCH_DECK_SSH_RETRIES` | `1` | повторов ssh внутри цикла |
| `RPV_WATCH_DECK_SSH_FAIL_STREAK` | `3` | подряд неудачных циклов ssh, после которых будят CEO |
| `RPV_WATCH_SERVER_LOAD_MAX` | `4` | простой сервера (алиас `calc`): нагрузка (loadavg) машины ниже порога, а задания под замком `benchrun.sh` ждут дольше `RPV_WATCH_SERVER_LOCK_WAIT_MIN`… |
| `RPV_WATCH_SERVER_LOCK_WAIT_MIN` | `20` | …минут — сторож будит владельца тикета, державшего замок |
| `RPV_WATCH_SERVER_WAKE_REPEAT_HOURS` | `2` | повтор такого сигнала, ч |

## Страж удаления (`RPV_GUARD_*`)

Хук `PreToolUse` (`hooks/hooks.json`) ловит Bash, PowerShell, Write, Edit, MultiEdit и NotebookEdit: удалять и перезаписывать можно только внутри проекта и scratchpad сессии. Без переменных на удалённых машинах удалять нельзя нигде.

| переменная | смысл |
|---|---|
| `RPV_GUARD_REMOTE_ROOTS` | каталоги, где можно удалять на любых удалённых машинах |
| `RPV_GUARD_HOST_ROOTS` | то же, но только при ssh на конкретный хост: `хост=корень,корень;хост2=…` |
| `RPV_GUARD_STAGE` | оперативная стадия — каталог, целиком разрешённый для удаления |
| `RPV_GUARD_FORBIDDEN_HOSTS` | закрытые хосты и IP, через запятую: там нельзя ничего |
| `RPV_GUARD_HEAVY_HOST` | хост машины замеров (без `user@`); не задан — замок выключен. Тяжёлая команда (du, find, rsync, tar, cp -r, md5sum по каталогам, python-скрипт и т. п.), идущая по ssh на этот хост прямо из сессии, а не юнитом (`systemd-run …`) или скриптом-обёрткой замера, — отказ: замок замеров не видит ssh-сессии |
| `RPV_GUARD_HEAVY_ALLOW` | регэксп (`re.search`) по разобранной простой команде `имя арг…` на хосте замка: совпало — команда не считается тяжёлой (напр. постановщик в очередь `^python3 /data/sched/alsched[.]py (submit\|status)( \|$)`). Пусто или негодный регэксп — исключений нет; на разбор с ошибкой кавычек не действует |
| `RPV_GUARD_HEAVY_HINT` | фраза в тексте отказа замка: как у вас запускать под замком (напр. `Обёртка: /data/benchrun.sh stand <команда>`) |

`git reset --hard` и `git clean -f` страж пропускает только в каталоге вне основного дерева, заданном явным путём; `git push --force` — никогда.

## Шина событий

| переменная | по умолчанию | смысл |
|---|---|---|
| `RPV_BUS_URL` | не задан — шина выключена | адрес шины |
| `RPV_BUS_TOKEN` | — | токен значением |
| `RPV_BUS_TOKEN_FILE` | запасной `~/.rpv-bus-token` | токен файлом |
| `RPV_BUS_SPOOL` | `<tmp>/rpv-bus-spool.jsonl` | очередь неотправленных событий |
| `RPV_BUS_DISABLE` | — | `1` — выключить шину |
| `RPV_BUS_SNAPSHOT_S` | `300` | период полного снимка блокеров, с |
| `RPV_RUNS_DIR` | `/var/lib/rpv/runs` | куда `jobrun.sh` пишет запись запуска (InvocationID) |

Задан `RPV_BUS_URL` — сигналы к CEO идут через очередь `ceo` шины, а `ceo-inbox.md` и `ceo-wake.log` становятся запасным путём и будильником; не задан или `RPV_BUS_DISABLE` — файлы остаются основным путём ([signals.md](signals.md)). Порог «не обработано» у очереди `ceo` — 2 часа (константа шины), у остальных — флаг `--stale-after` у `bus.py` (600 с).

Подробнее — [bus.md](bus.md).

## Табло и Диспетчерская

| переменная | по умолчанию | смысл |
|---|---|---|
| `RPV_BOARD` | не задан | строка подключения `https://host/<токен>/#<ключ>`; задан — `/rpv-start` запускает и `board_push.py --loop 5` |
| `RPV_MACHINES` | не задан | машины: `pc2=user@host,srv=алиас`; `id=!host` — выключена вами |
| `RPV_PC` | `1` | `0` — без загрузки «этого ПК» |
| `RPV_PLAIN` | `1` | `0` — без человеческих строк процессов (Haiku) |
| `RPV_PLAIN_MODEL` | `claude-haiku-5-5` | модель для таких строк |
| `RPV_BOARD_DATA` | `~/rpv-board` (в юните `/var/lib/rpv-board`) | данные сервера: команды, сводки |
| `RPV_BOARD_TOKEN_FILE` | `/etc/rpv-board/token`, иначе `<данные>/token` | файл токена сервера |
| `RPV_BOARD_PORT` | `8787` | порт сервера |
| `RPV_BOARD_HOST` | `0.0.0.0` | адрес, на котором слушает сервер |

Подробнее — [dashboard.md](dashboard.md).

## Присмотр

| переменная | по умолчанию | смысл |
|---|---|---|
| `RPV_SUPERVISE_STALE_S` | `600` | возраст сердцебиения, после которого присмотр поднимает службу, с |

## Служебные

`RPV_START_CMD` и `RPV_START_CWD` `start.py` использует внутри себя, чтобы передать команду и каталог процессу WMI на Windows; руками их не задают.
