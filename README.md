# Role Play Vibing

Команда ИИ-ролей для Claude Code: владелец говорит с CEO, работу делают Исследователь, Инженер и Судья по тикетам.

## Что даёт

- Контекст не раздувается — одна задача = один тикет = короткая сессия роли.
- Работа не встаёт — диспетчер будит роль, сторож ловит простои.
- Алгоритм оптимальный — узкое место → лучшие способы → замер на малом → боевой прогон.
- Без упрощений ради кода — упрощение только если замер доказал, что результат тот же.
- Выводы проверены — Судья принимает по договору «примут, если …».

## Роли

| роль | делает | сессия |
|---|---|---|
| CEO | говорит с владельцем, заводит тикеты, читает сигналы | долгая |
| Исследователь | протокол → счёт → отчёт | на тикет |
| Инженер | код, тесты, машины | на тикет |
| Судья | проверка и вердикт | на тикет |

## Как работает

1. Владелец → CEO → тикет `.claude/tickets/TK-NNN.md`.
2. Диспетчер `dispatch.py` → запускает роль.
3. Роль → итог в лог тикета: `tickets.py comment <ID> --author <роль> --text "…" --next <роль>`.
4. `--next` → диспетчер будит следующую роль.
5. Сторож `watch.py` → сигналы в `ceo-wake.log`.
6. CEO → ответ владельцу.

## Правила

- Только оптимальный алгоритм; в отчёте строка «Алгоритм: …».
- Данные и модель — как в реальности.
- Новые задачи — только по слову владельца.
- Числа владельцу — после Судьи; оценка ≠ замер.
- Передача работы — только `--next`.
- Удаление — только внутри проекта.

Полный устав — `.claude/roles/README.md`.

## Установка

```
/plugin marketplace add https://github.com/Gogi213/role-play-vibing
/plugin install role-play-vibing@role-play-vibing
```

Дальше, в проекте:

1. `/rpv-init` — создаёт `.claude/roles` (уставы из шаблонов), `.claude/tickets` и строки в `.gitignore`.
2. `/ceo` — помечает сессию как CEO (явная роль).
3. `/rpv-start` — запускает диспетчер и сторож в фоне, печатает pid и пути логов (`<проект>/.claude/dispatcher/dispatch.run.log`, `watch.run.log`). Уже запущены (pid-замок) — останавливает и запускает заново: так подхватывается обновлённый плагин. Службы отвязаны от сессии (Windows — WMI, вне job-объекта приложения; Linux — юнит `systemd-run --user`, чтобы пережил выход из системы: `loginctl enable-linger $USER`), в конце печатается «отвязан: да/нет».

Скрипты работают прямо из папки плагина (`<плагин>` — каталог установленного плагина, `${CLAUDE_PLUGIN_ROOT}`), копировать ничего не нужно. Вручную:

```
python <плагин>/.claude/dispatcher/start.py --project <проект>      # то же, что /rpv-start
python <плагин>/.claude/dispatcher/dispatch.py --project <проект>
python <плагин>/.claude/dispatcher/watch.py --project <проект>
```

Корень проекта — `--project`, иначе `RPV_PROJECT`, иначе `CLAUDE_PROJECT_DIR`, иначе ближайший каталог вверх от текущего с `.claude/roles`; не нашли — ошибка с подсказкой `--project` / `/rpv-init`, каталоги не создаются. Состояние (`state.json`, логи, `ceo-inbox.md`, `ceo-wake.log`) — в `<проект>/.claude/dispatcher/`; тикеты и роли — из проекта. Тикеты заводит CEO: готовая команда с абсолютным путём `python "<плагин>/.claude/dispatcher/tickets.py" --project "<проект>" new …` приходит в начале сессии от хука (для CEO, помеченного `/ceo`, — из команды `/ceo`). Подробнее — `.claude/dispatcher/README.md`.

## Установка по платформам

Сам плагин ставится одинаково везде (`/plugin marketplace add …`, `/plugin install …`, затем `/rpv-init` и `/rpv-start` в проекте). Различается запуск в фоне: `start.py` выбирает способ по ОС. Ниже — то, что делает код (`start.py`, `dispatch.py`): поведение на macOS и Linux выведено из него, а примеры автозапуска на этих системах не запускались. `<плагин>` — каталог плагина, где лежит `.claude/dispatcher/start.py`; `<проект>` — корень проекта (где `.claude/roles`).

| | Windows | macOS | Linux |
|---|---|---|---|
| `/rpv-start` запускает через | WMI `Win32_Process.Create` | `Popen` в новой сессии (`start_new_session`) | `systemd-run --user`; нет systemd — как на macOS |
| закрыли окно Claude | работает дальше | работает дальше | работает дальше |
| выход из системы | не проверялось | не проверялось | работает дальше при `loginctl enable-linger $USER` |
| перезагрузка | не переживает | не переживает | не переживает (без постоянного юнита) |
| остановка | `Stop-Process` по `*.pid` | `kill` по `*.pid` | `systemctl --user stop` |
| автозапуск | вручную: `/rpv-start` | LaunchAgent (пример ниже) | user-юнит systemd (пример ниже) |

**Нужно на любой ОС**

- Python 3.11 (только stdlib) и команда `python` в `PATH`: роли получают команды вида `python <плагин>/.claude/dispatcher/tickets.py …`. Хуки плагина ищут `python3`, затем `python`, затем `py -3`.
- git и Claude Code CLI `claude` в `PATH` службы (или `CLAUDE_BIN=<полный путь>`), с выполненным входом под тем же пользователем: роли запускаются как `claude -p … --permission-mode bypassPermissions`. Служба получает от `start.py` только `RPV_*`, `ALPHA_*`, `CLAUDE_BIN`, `CLAUDE_PROJECT_DIR`, `CLAUDE_CONFIG_DIR` и `PATH` (на POSIX); секреты (`ANTHROPIC_*`, токены) не переносятся — если службе нужен такой ключ, задайте его в её окружении сами.
- ssh-клиент и ключ — только если в тикетах есть `wait_for: host:<алиас>:…`. Хост берётся из `RPV_CALC_HOST` / `RPV_VPS_HOST` / `RPV_DECK_HOST` (что понимает `ssh`: `user@host` или алиас из `~/.ssh/config`), ключ — `RPV_DECK_KEY`, `RPV_DECK_KNOWN_HOSTS`. Вызов идёт с `BatchMode=yes` — пароль не спрашивается, нужен ключ без парольной фразы (или ssh-agent). Удалённая сторона — Linux: проверки выполняются командами `test`, `cat`, `systemctl`, `grep`.

### Windows

- **Заранее:** Python 3.11 (`python` в `PATH`), git (Git for Windows — хукам плагина нужен `sh`), `powershell` (через него `start.py` создаёт процессы в WMI), `ssh.exe` — для `wait_for host:`. `claude` должен быть в `PATH` из реестра (системного или пользовательского): служба, созданная через WMI, берёт `PATH` оттуда, а не из вашей сессии; иначе задайте `CLAUDE_BIN=<полный путь>` (переносится вместе с `RPV_*`).
- **Установка:** `/plugin marketplace add …`, `/plugin install …`; в проекте `/rpv-init`.
- **Запуск:** `/rpv-start` или `python "<плагин>\.claude\dispatcher\start.py" --project "<проект>"`. Служба создаётся WMI — вне job-объекта приложения. Печатается pid (это `cmd`; pid самой службы — в `<проект>\.claude\dispatcher\dispatch.pid`, `watch.pid`), «отвязан: да» и лог (`dispatch.run.log`, `watch.run.log` там же). Повторный запуск перезапускает уже работающие.
- **Остановка:** отдельной команды нет: `Stop-Process -Id (Get-Content "<проект>\.claude\dispatcher\dispatch.pid")` и то же для `watch.pid`. Уже запущенные ролями процессы это не гасит (диспетчер подхватит их по pid при следующем старте); снять роль на тикете — `tickets.py stop <ID> …` (только CEO).
- **Переживает:** закрытие Claude — да; перезагрузку — нет (код ничего не регистрирует в автозапуске). Поднять снова: `/rpv-start` или строка запуска выше.

### macOS

- **Заранее:** Python 3.11 (`python3`; ролям ещё нужна команда `python` в `PATH` — если в системе только `python3`, добавьте `python` ссылкой или менеджером версий), git, `claude` в `PATH` (`command -v claude`) с выполненным входом, `ssh` — есть в системе.
- **Установка:** `/plugin marketplace add …`, `/plugin install …`; в проекте `/rpv-init`.
- **Запуск:** `/rpv-start`. Ни WMI, ни systemd нет — `start.py` берёт запасной путь: `Popen` в новой сессии (`start_new_session`), stdin закрыт, вывод дописывается в `<проект>/.claude/dispatcher/dispatch.run.log`, `watch.run.log`. Строка «отвязан: нет (проверить не удалось)» здесь ожидаема: проверка читает `/proc/<pid>/cgroup`, которого на macOS нет.
- **Остановка:** `kill "$(cat <проект>/.claude/dispatcher/dispatch.pid)"` и то же для `watch.pid`. Запущенные ролями процессы это не гасит (диспетчер подхватит их при следующем старте); снять роль — `tickets.py stop <ID> …` (только CEO).
- **Переживает:** закрытие окна Claude — да; перезагрузку — нет. После перезагрузки удалите `<проект>/.claude/dispatcher/*.pid`: без `/proc` код не может проверить, чьё имя у процесса с pid из файла, и устаревший файл (pid уже занят чужим процессом) даёт ложное «уже запущен», а `/rpv-start` пошлёт этому чужому процессу SIGTERM (а если тот не завершится за 10 с — SIGKILL).

#### Автозапуск после входа в систему — LaunchAgent

На macOS не проверялось. Два агента — диспетчер и сторож — запускают `dispatch.py` и `watch.py` напрямую, `KeepAlive` перезапускает упавшие; launchd не раскрывает `~` и переменные в plist, поэтому пути подставляет оболочка при создании файлов:

```sh
PROJECT=/path/to/project                 # корень проекта (где .claude/roles)
PLUGIN=/path/to/role-play-vibing         # каталог плагина (где .claude/dispatcher/start.py)
PY="$(command -v python3)"               # Python 3.11+
AGENT_PATH="$(dirname "$(command -v claude)"):$(dirname "$PY"):/usr/local/bin:/usr/bin:/bin"
mkdir -p "$PROJECT/.claude/dispatcher" "$HOME/Library/LaunchAgents"   # каталог лога должен существовать до запуска
for s in dispatch watch; do
cat > "$HOME/Library/LaunchAgents/local.rpv.$s.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>local.rpv.$s</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PY</string>
    <string>-u</string>
    <string>$PLUGIN/.claude/dispatcher/$s.py</string>
    <string>--project</string>
    <string>$PROJECT</string>
  </array>
  <key>WorkingDirectory</key><string>$PROJECT</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>$AGENT_PATH</string>
    <key>PYTHONUTF8</key><string>1</string>
    <key>PYTHONIOENCODING</key><string>utf-8</string>
    <!-- по необходимости добавьте RPV_* из README (RPV_DISPATCH_ROLE_PARALLEL, RPV_BUS_URL, RPV_CALC_HOST, RPV_DECK_KEY) -->
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$PROJECT/.claude/dispatcher/$s.run.log</string>
  <key>StandardErrorPath</key><string>$PROJECT/.claude/dispatcher/$s.run.log</string>
</dict>
</plist>
EOF
done
plutil -lint "$HOME/Library/LaunchAgents/local.rpv."{dispatch,watch}.plist
for s in dispatch watch; do launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/local.rpv.$s.plist"; done
```

Проверить: `launchctl print "gui/$(id -u)/local.rpv.dispatch"` и `tail <проект>/.claude/dispatcher/dispatch.run.log`. Перезапуск (например, после обновления плагина): `launchctl kickstart -k "gui/$(id -u)/local.rpv.dispatch"`. Остановка и снятие с автозапуска: `launchctl bootout "gui/$(id -u)/local.rpv.dispatch"` (то же для `watch`) и удалить plist. Старый синтаксис: `launchctl load -w <plist>` / `launchctl unload -w <plist>`. С LaunchAgent не пользуйтесь `/rpv-start`: он остановит процесс по pid-файлу, `launchd` тут же поднимет его снова (`KeepAlive`), `start.py` запустит ещё один — один из двух выйдет по pid-замку. Если после перезагрузки в логе «уже запущен (pid …)», а диспетчера нет, — удалите `dispatch.pid` / `watch.pid` (причина — в пункте «Переживает» выше). Секрет, нужный службе (`ANTHROPIC_API_KEY` при входе по ключу), добавляйте в `EnvironmentVariables` только в своём plist (`chmod 600`), не в репозиторий.

### Linux

- **Заранее:** Python 3.11 (`python` в `PATH`; в Debian/Ubuntu `python` даёт пакет `python-is-python3`), git, `claude` в `PATH` с выполненным входом, `ssh`. Для `/rpv-start` через systemd — работающий `systemctl --user`; на сервере без интерактивной сессии включите `loginctl enable-linger $USER`.
- **Установка:** `/plugin marketplace add …`, `/plugin install …`; в проекте `/rpv-init`.
- **Запуск:** `/rpv-start`. Если есть `systemd-run` и отвечает `systemctl --user`, каждая служба — юнит `rpv-<dispatch|watch>-<8 знаков хеша пути проекта>` (`systemd-run --user --collect`, вывод в `<проект>/.claude/dispatcher/<имя>.run.log`), «отвязан: да» — по cgroup юнита. Нет systemd (контейнер и т. п.) — запасной путь как на macOS: `Popen` в новой сессии, остановка по `*.pid`.
- **Остановка:** `systemctl --user list-units 'rpv-*'` — имена, затем `systemctl --user stop <юнит>`. Остановка юнита гасит и запущенные ролями процессы этого проекта.
- **Переживает:** закрытие Claude — да; выход из системы — да при `loginctl enable-linger $USER`; перезагрузку — нет: юниты `systemd-run` временные и после неё исчезают.

#### Автозапуск после перезагрузки — постоянный user-юнит

На Linux не проверялось. Юнит вызывает тот же `start.py`: службы поднимаются как по `/rpv-start` (временные юниты `rpv-*`, переменные `RPV_*`, `CLAUDE_BIN` и `PATH` переносятся), а `/rpv-start` и `systemctl --user restart rpv-autostart` остаются перезапуском. Для второго проекта — другое имя файла юнита:

```sh
PROJECT=/path/to/project                 # корень проекта (где .claude/roles)
PLUGIN=/path/to/role-play-vibing         # каталог плагина (где .claude/dispatcher/start.py)
PY="$(command -v python3)"               # Python 3.11+
UNIT_PATH="$(dirname "$(command -v claude)"):$(dirname "$PY"):/usr/local/bin:/usr/bin:/bin"
mkdir -p "$HOME/.config/systemd/user"
cat > "$HOME/.config/systemd/user/rpv-autostart.service" <<EOF
[Unit]
Description=Role Play Vibing - dispatcher and watcher of the project

[Service]
Type=oneshot
RemainAfterExit=yes
Environment="PATH=$UNIT_PATH"
# по необходимости - RPV_* из README, start.py перенесёт их в службы:
#Environment="RPV_DISPATCH_ROLE_PARALLEL=engineer:3"
ExecStart="$PY" "$PLUGIN/.claude/dispatcher/start.py" --project "$PROJECT"

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
systemctl --user enable --now rpv-autostart.service
loginctl enable-linger "$USER"           # менеджер пользовательских юнитов стартует при загрузке, без входа в систему
```

Вывод `start.py` — `journalctl --user -u rpv-autostart`; убрать автозапуск — `systemctl --user disable --now rpv-autostart.service` (службы `rpv-*` остановите отдельно).

#### Сервер шины (необязательно, только Linux)

Поставляемые юниты — системные (`WantedBy=multi-user.target`, `/usr/bin/python3`, каталог `/opt/rpv-bus`); сторож машины опрашивает `systemctl`. Диспетчер на любой ОС подключается к шине клиентом (`RPV_BUS_URL`, `RPV_BUS_TOKEN_FILE`).

```sh
PLUGIN=/path/to/role-play-vibing         # каталог плагина
sudo mkdir -p /opt/rpv-bus
sudo cp "$PLUGIN"/.claude/bus/{bus.py,busclient.py,watcher.py,routes.json} /opt/rpv-bus/
sudo sh -c 'umask 077; python3 -c "import secrets; print(secrets.token_urlsafe(32))" > /opt/rpv-bus/token'
sudo cp "$PLUGIN"/.claude/bus/rpv-bus.service "$PLUGIN"/.claude/bus/rpv-bus-watcher.service /etc/systemd/system/
```

Перед запуском поправьте в `rpv-bus-watcher.service` `HOSTNAME` на имя машины, а порт (`--port 8788`) и пути — в обоих юнитах под свою машину. Сторожу машины нужны адрес и токен шины: добавьте в его `[Service]` строки `Environment=RPV_BUS_URL=http://<адрес шины>:8788` и `Environment=RPV_BUS_TOKEN_FILE=/opt/rpv-bus/token`. Затем `sudo systemctl daemon-reload && sudo systemctl enable --now rpv-bus rpv-bus-watcher`. На остальные Linux-машины с заданиями ставятся только `watcher.py`, `busclient.py` и `rpv-bus-watcher.service` (с `RPV_BUS_URL` на машину шины и файлом токена); `rpv-bus` — только на машину шины.

Веб-табло (`board_push.py --loop 5`) `/rpv-start` запускает сам, если в окружении задан `RPV_BOARD`.

## Шина событий (1.3.0, необязательно)

Транспортная развязка без модели и без внешних пакетов (Python stdlib, sqlite): задание на удалённой машине кончилось — диспетчер просыпается за секунды, а не на следующем тике. Выключена, пока не задан `RPV_BUS_URL`.

- **Шина** `.claude/bus/bus.py` — на любой машине с адресом: журнал событий с номерами (sqlite, WAL), HTTP с токеном, `POST /event`, `GET /q/<получатель>?after=&wait=` (long-poll), `POST /ack`, `/stats`, `/stale`. До ack событие остаётся в очереди; повтор с тем же `id` не дублируется. Юнит-шаблоны — `.claude/bus/rpv-bus.service`, `rpv-bus-watcher.service` (пути и порт поправить под машину); токен — файл `0600`.
- **Маршруты** — `.claude/bus/routes.json`: шаблон адреса (`задача.*.задание.упало`) → получатели `dispatcher` / `ceo` (получателя `board` добавляйте, когда у вас есть потребитель: неподтверждённое событие через N мин шлёт сигнал «не обработано»); «задача.<TK>.к_ceo» — запись с `--next ceo`, «вопрос_владельцу» — только запись, начинающаяся с «ВОПРОС ВЛАДЕЛЬЦУ»; `hold` — получатели, которым событие держится, пока тикет заблокирован. Блокеры: `задача.<TK>.блокер.поставлен` / `.снят`; диспетчер раз в 5 минут шлёт полный снимок (blocked, needs_owner, waiting на незакрытый `ticket:<ID>`).
- **Сторож машины** `.claude/bus/watcher.py --host <имя>` — сам видит остановку юнитов `rpv-*` (успех/падение с кодом) и файлы хода `*.json`, шлёт события; юниты править не нужно.
- **Диспетчер** — два исходящих long-poll (очереди `dispatcher` и `ceo`): событие → тик сразу, ack после тика; события CEO → строки `ceo-inbox.md`/`ceo-wake.log`; шина недоступна → одна строка `bus-down`, диспетчер работает по таймеру и `wait_for`, после возврата — `bus-up`.
- **Отправители** — `.claude/bus/busclient.py send <адрес> [--payload JSON]`; недоступная шина складывается в spool и дошлётся позже. `tickets.py`-события: добавлены в проекте alpha, в плагин — по запросу.
- **Настройка:** `RPV_BUS_URL`, токен — `RPV_BUS_TOKEN` или `RPV_BUS_TOKEN_FILE` (запасной `~/.rpv-bus-token`), `RPV_BUS_DISABLE=1` — выключить, `RPV_BUS_SNAPSHOT_S` (300). Прежние `ALPHA_BUS_*` — запасные. Клиентам — только Python/utf-8 (curl на Windows шлёт кириллицу не в utf-8).
- Тесты: `python -m unittest discover -s .claude/bus` и `-s .claude/dispatcher`.

## Что нового в 1.7.1

- **Уникальный запуск + InvocationID** — `.claude/bus/jobrun.sh <метка> -- <команда>` (Linux, systemd): юнит `<метка>-<MMDDhhmmss>-<4hex>`, запись `{unit, runid, invocation, started}` в `$RPV_RUNS_DIR` (по умолчанию `/var/lib/rpv/runs`). `wait_for: host:<машина>:unit:<имя>`.
- **Конец задания — событием шины, без опроса**: `watcher.py` шлёт `машина.<хост>.юнит.запущен` и `…остановлен/упал` с `invocation`; диспетчер засчитывает остановку без ssh, если InvocationID совпал с запуском; остановка старого экземпляра с тем же именем игнорируется. Запасной ssh-опрос — раз в 300 с (`RPV_DISPATCH_WAIT_POLL_S`).
- **Присмотр за диспетчером и сторожем** — `python .claude/dispatcher/supervise.py --install [--project P]`: раз в 5 мин задание ОС (Windows — Планировщик заданий, Linux — `systemd --user` timer, macOS — launchd) проверяет сердцебиение (`state.json`/`watch-heartbeat.json` старше 10 мин или процесса нет) и поднимает службу через `start.py` (WMI/systemd/Popen); строки — в `.claude/dispatcher/supervise.log`. Окружение `RPV_*`/`CLAUDE_*` запоминается в `supervise.env.json`. Снять — `--uninstall`.
- **Плашка «КОМАНДА МОЛЧИТ С HH:MM»** (красная) в Диспетчерской, когда сводка команды не приходила > 10 мин, — считает сервер табло, не сторож и не сессия CEO.
- Диспетчер и сторож с выводом в файл игнорируют чужой Ctrl+C общей консоли (иначе тихо умирали); службы стартуют под `python.exe`, а не `pythonw.exe`.

## Что нового в 1.7.0

- **По умолчанию включены** строки Haiku (`RPV_PLAIN=0` — выключить; без `claude` — строка по фактам плана) и загрузка «этого ПК» (`RPV_PC=0` — без неё), в том числе на macOS/Windows без `/proc`.
- **Своя «Диспетчерская»:** в плагин перенесён сервер веб-табло (`.claude/board/`: `server.py`, страница `dispetcher.html` на `phosphor.css` / `phosphor.js`,
  юнит `rpv-board.service`) — его можно поднять у себя (раздел «Диспетчерская» ниже), а не только слать на чужой.
- **Машины:** `RPV_MACHINES` — загрузка ЦП, памяти и диска машин по ssh и ход их заданий (`machines.py`); недоступная машина — «нет связи».
- **Строки процессов:** короткие человеческие строки от Haiku (`plainify.py`) — по умолчанию (`RPV_PLAIN=0` — выкл.), в том числе для задач без плана (шаги по логу тикета); без `claude` или при ошибке остаётся строка по фактам.
- **Кадр на диск:** `board_push.py` пишет `<проект>/.claude/pulse/status.json`, даже если `RPV_BOARD` не задан; его читают TUI `board.py` и MCP `mcp_server.py`.
- Тесты: `python -m unittest discover -s .claude/board`.
- Не перенесено: старый TUI v1 (`pulse.py`) — он целиком завязан на сборщик проекта-источника.
## Что нового в 1.6.2

- **Судью не снимает CEO.** В шаблонах ролей (`templates/roles/ceo.md`, README «Судья и договор») записано правило
  владельца: Судья — единственный кросс-аудитор и валидатор качества; CEO запрещено убирать его с задачи, останавливать
  его запуск или закрывать/вливать работу мимо его приёмки. Снять Судью может только владелец прямой цитатой.

## Что нового в 1.6.1

- **macOS: проверка «наш ли живой процесс» по pid без `/proc` доведена.** В 1.5.0 `start.py` не читал командную строку без `/proc` и принимал
  любой живой pid из устаревшего замка за свой — `/rpv-start` убивал чужой процесс; теперь она берётся из `ps`. Тесты диспетчера на macOS
  ждали имя образа по `sys.executable`, а у framework-python оно «Python» — теперь имя спрашивается у ОС. Linux и Windows не менялись.

## Что нового в 1.6.0

- **Табло с мака наполняется как у alpha:** `board_push.py` строит полный view2 — прогресс с оценкой срока, ленту событий, вопросы владельцу
  (`ask.py`, `<проект>/.claude/pulse/questions/`), машины и шаги ролей; сборка — `view2.py`. Короткие строки Haiku (plainify) не переносились:
  сводка процесса — по фактам плана («сделано: … N из M»).

## Что нового в 1.5.0

- **Табло наполняется шагами:** роли пишут план командой `plan.py set/step` (правило в промпте диспетчера), `board_push.py`
  шлёт на табло шаги, сводку и машины (ПК/VPS/СЧЁТ/КОЛ/ВЫ из поля «где» шага), а не один шаг на тикет. План — `<проект>/.claude/pulse/plans/<ТК>.json`.
- **Отправка стартует вместе с диспетчером:** задан `RPV_BOARD` — `/rpv-start` запускает и `board_push.py --loop 5` (Windows, macOS, Linux),
  при перезапуске он перезапускается, сам гаснет, когда диспетчера нет дольше 2 минут.
- **macOS:** проверка живого pid без `/proc` — через `ps` (состояние и имя образа), чужой процесс с живым pid больше не принимается за наш.

Командам на macOS: обновить плагин до 1.5.0, задать `RPV_BOARD` (строка из окна «+»), выполнить `/rpv-start`.

## Что нового в 1.4.2

- **Шина без `RPV_BUS_URL` выключается молча.** В `busclient.py` не была задана `DEFAULT_URL` (с 1.3.0), и диспетчер при каждом старте писал в лог `шина не запущена: NameError: name 'DEFAULT_URL' is not defined`. Теперь `DEFAULT_URL = ""`: без адреса шины `config()` отдаёт пустой адрес, диспетчер работает по таймеру без строки об ошибке, как и описано в «Шина событий».

## Что нового в 1.4.1

- **README: «Установка по платформам»** — Windows, macOS, Linux: что нужно заранее, как поставить и запустить, как остановить, переживает ли закрытие Claude и перезагрузку; готовые примеры автозапуска — LaunchAgent (macOS), постоянный user-юнит systemd (Linux), установка сервера шины (Linux). Код не менялся.

## Что нового в 1.4.0

- **`wait_for` v4** — формы `host:<алиас>:<путь | unit:<имя>>` (алиасы `calc`, `vps`, `deck`), `file:<путь>`, `ticket:<ID>`; файл хода `*.json` считается готовым при `done >= total`. Команда `tickets.py wait <ID> <условие> [--on-met ...]` ставит `status: waiting` и условие одной записью. Адреса машин — только из окружения: `RPV_CALC_HOST`, `RPV_VPS_HOST`, `RPV_DECK_HOST` (+ `RPV_DECK_KEY`, `RPV_DECK_KNOWN_HOSTS`); каталог файлов хода — `RPV_PROGRESS_DIR` (по умолчанию `~/rpv/progress`). Не задан хост — условие «не выполнено» и строка в `dispatch.err.log`.
- **Ожидания по событию** (с шиной): `юнит.остановлен/упал`, `задание.готово`, `файл.появился` закрывают `wait_for` сразу; ssh-опрос идёт в отдельном потоке `wait-poller`, тик не блокируется; `RPV_DISPATCH_WAIT_POLL_S`.
- **Шина:** выдача `held → pending` ниже курсора, ack с повтором; сторож машины читает `watch.list` из каталога файлов хода и шлёт `файл.появился`.
- **`on_met` по семантике Судьи**; после таймаута запуска роль продолжает **ту же сессию** (`--session-id`, `RPV_DISPATCH_ON_MET_TIMEOUT_S`).
- **Сторож:** триаж мёртвых целей `wait_for` (хост недоступен, юнита/файла нет) и счётчик застоя.
- **Учёт в токенах:** `cr_tok`/`cw_tok`/`tok_src` в `runs.log`, сводка `usage.py`; запуски без JSON (таймаут) — из транскрипта сессии.
- **Предел параллельности по ролям** — `RPV_DISPATCH_ROLE_PARALLEL=engineer:3`.
- Устав: правило основного дерева и слияния через rebase + проверку + fast-forward.
- Не перенесено (специфика проекта-источника): защита удаления по спискам проекта, автопередача Судье по правилам проекта.

## Настроить под проект

- Переменные — `RPV_*`; прежние `ALPHA_*` — запасные (работают, если `RPV_*` не задана).
- Проверка второй машины (сторож, ssh) по умолчанию выключена; включается `RPV_DECK_HOST` (+ `RPV_DECK_KEY`, `RPV_DECK_KNOWN_HOSTS`, корень очереди на ней — `RPV_DECK_ROOT`, по умолчанию `~/rpv`); пустой файл `<проект>/.claude/dispatcher/deck-off` выключает её и при заданном хосте.
- Сессия CEO — команда `/ceo` (метка сессии); запасной путь — слово `CEO` в названии сессии Claude Desktop.
- Зоны и ссылки — `.claude/roles/*.md`.
- Модели ролей — `RPV_DISPATCH_MODEL`, `RPV_DISPATCH_ROLE_MODEL`, `RPV_DISPATCH_EFFORT` (умолчания — в `dispatch.py`: `CLAUDE_MODEL`, `ROLE_MODEL`).
- Защита удаления — хук `PreToolUse` (страж ловит Bash, PowerShell, Write, Edit, MultiEdit, NotebookEdit) в `hooks/hooks.json`; корень проекта — `CLAUDE_PROJECT_DIR`; удалённые каталоги, стадия и закрытые хосты — `RPV_GUARD_REMOTE_ROOTS`, `RPV_GUARD_HOST_ROOTS` (`хост=корень,корень;хост2=…` — только при ssh на этот хост), `RPV_GUARD_STAGE`, `RPV_GUARD_FORBIDDEN_HOSTS` (без переменных — на удалённых машинах удалять нельзя нигде; прежние `ALPHA_GUARD_*` — запасные).
- Хуки запускает `hooks/run-hook.sh`: `python3`, иначе `python`, иначе `py -3`; в проекте без `.claude/roles` хуки молчат.
- Тесты: `python -m unittest discover -s .claude/dispatcher`, `-s .claude/hooks` и `-s .claude/board`.
- Нужно: Python 3.11, `claude` в `PATH`, git (по ОС — «Установка по платформам»).

## Веб-табло (необязательно)

`python .claude/dispatcher/board_push.py [--loop 5] [--dry]` — сводка тикетов на табло по `RPV_BOARD` — одной строке подключения
из окна «+» (`https://host/<токен>/#<ключ>`); без ssh, ключ уходит только в заголовке и в вывод не попадает.

## Диспетчерская: поднять свою / подключиться к чужой

Диспетчерская — веб-страница «Диспетчерская»: ход всех подключённых команд (процессы и шаги, вопросы владельцу, машины, лента) на компьютере и с телефона.
Сервер ничего не исполняет и ничего не знает о проектах: он принимает сводки и отдаёт страницу. Адрес защищён секретным префиксом `/<токен>/`.

**Подключиться к чужой (или к своей уже поднятой).** На странице нажмите «+», введите имя команды — окно выдаст строку подключения
`https://<хост>/<токен>/#<ключ>` (ключ виден один раз). Положите её в окружение проекта: `export RPV_BOARD='https://…/#…'`
(Windows: `setx RPV_BOARD "…"`) и выполните `/rpv-start` — он запустит `python .claude/dispatcher/board_push.py --loop 5`; вручную то же самое
(`--dry` — только показать сводку). Необязательное:

- `RPV_MACHINES=pc2=user@host,srv=алиас` — машины (ssh в режиме BatchMode, ключ входа должен быть без пароля; `id=!host` — машина выключена вами и не опрашивается).
  Нужны `ssh` на этом компьютере и Linux на машине (`/proc`). Ключ — `RPV_DECK_KEY`, файл known_hosts — `RPV_DECK_KNOWN_HOSTS` (те же, что у проверки второй машины).
  Ход заданий — `*.json` свежее 30 минут в каталоге `RPV_PROGRESS_DIR` на машине: `{"done": 231, "total": 492, "step": "сверка", "unit": "ед."}`.
  Метки `ПК/VPS/СЧЁТ/КОЛ` — у машин с id `pc/vps/calc/col` (только они принимаются как «где» шага плана `plan.py`); остальные видны в списке машин.
- `RPV_PLAIN` (по умолчанию включено; `0` — выкл.) — человеческие строки процессов: `claude -p` (Haiku) из временного каталога вне проекта, кэш `<проект>/.claude/pulse/plain-auto.json`;
  без `claude` в `PATH` (или `CLAUDE_BIN`) — строка по фактам плана. Модель: `RPV_PLAIN_MODEL`.
- Рядом с проектом: кадр лежит в `<проект>/.claude/pulse/status.json` — `python .claude/board/board.py [--project <путь>]` рисует его в терминале
  (нужен `pip install textual`; `--sample` — пример), `.claude/board/mcp_server.py` — MCP-сервер «rpv-pulse»: `claude mcp add rpv-pulse -- python "<проект>/.claude/board/mcp_server.py" --project "<проект>"`
  (инструменты `pulse_status` и `pulse_answer`).

**Поднять свою.** Нужен Python 3.11+ без пакетов, открытый порт (по умолчанию 8787; для интернета поставьте перед сервером HTTPS-прокси — токен в адресе не должен ходить открытым текстом).

```sh
PLUGIN=/path/to/role-play-vibing          # каталог плагина
sudo useradd -r -s /usr/sbin/nologin rpv-board
sudo mkdir -p /opt/rpv-board /etc/rpv-board
sudo cp "$PLUGIN"/.claude/board/{server.py,dispetcher.html,phosphor.css,phosphor.js} /opt/rpv-board/
sudo sh -c 'umask 077; python3 -c "import secrets; print(secrets.token_urlsafe(32))" > /etc/rpv-board/token'
sudo chown rpv-board /etc/rpv-board/token
sudo cp "$PLUGIN"/.claude/board/rpv-board.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now rpv-board
cat /etc/rpv-board/token                  # адрес страницы: http://<хост>:8787/<токен>/
```

Данные (команды, сводки) — `RPV_BOARD_DATA` (в юните `/var/lib/rpv-board`, без юнита `~/rpv-board`); порт — `RPV_BOARD_PORT`, адрес — `RPV_BOARD_HOST`,
файл токена — `RPV_BOARD_TOKEN_FILE`. Команд — до 20; ключ команды хранится только хэшем, удалить команду можно на странице.
Без systemd: macOS — LaunchAgent с `python3 /путь/server.py` (по образцу автозапуска выше) и теми же переменными в `EnvironmentVariables`;
Windows — служба или задача планировщика, запускающая `python server.py` с этими переменными (токен — файл `RPV_BOARD_TOKEN_FILE`).
Контракт данных — `.claude/board/VIEW2.md`.
