# Веб-табло и Диспетчерская

Диспетчерская — веб-страница: ход всех подключённых команд (процессы и шаги, вопросы владельцу, машины, лента событий) на компьютере и с телефона. Сервер ничего не исполняет и ничего не знает о проектах: он принимает сводки и отдаёт страницу. Адрес защищён секретным префиксом `/<токен>/`. Сервер входит в плагин (`.claude/board/`: `server.py`, страница `dispetcher.html` на `phosphor.css` / `phosphor.js`, юнит `rpv-board.service`) — его можно поднять у себя, а не только слать сводки на чужой.

Отправка сводки — `python .claude/dispatcher/board_push.py [--loop 5] [--dry]`: сводка тикетов на табло по `RPV_BOARD` — одной строке подключения из окна «+» (`https://host/<токен>/#<ключ>`); без ssh, ключ уходит только в заголовке и в вывод не попадает. `--dry` — только показать сводку.

Что попадает на табло: прогресс с оценкой срока, лента событий, вопросы владельцу (`ask.py`, `<проект>/.claude/pulse/questions/`), машины и шаги ролей. Роли пишут план командой `plan.py set/step` (правило в промпте диспетчера; план — `<проект>/.claude/pulse/plans/<ТК>.json`); шаги берутся и из журнала тикета, если плана нет. Сборка кадра — `view2.py`, контракт данных — [`.claude/board/VIEW2.md`](../.claude/board/VIEW2.md).

Красная плашка «КОМАНДА МОЛЧИТ С HH:MM» появляется, когда сводка команды не приходила дольше 10 минут: её считает сервер табло, не сторож и не сессия CEO.

## Подключиться к чужой (или к своей уже поднятой)

На странице нажмите «+», введите имя команды — окно выдаст строку подключения `https://<хост>/<токен>/#<ключ>` (ключ виден один раз). Положите её в окружение проекта: `export RPV_BOARD='https://…/#…'` (Windows: `setx RPV_BOARD "…"`) и выполните `/rpv-start` — он запустит `python .claude/dispatcher/board_push.py --loop 5` (при перезапуске служба перезапускается, сама гаснет, когда диспетчера нет дольше 2 минут); вручную то же самое — командой выше.

Необязательное:

- `RPV_MACHINES=pc2=user@host,srv=алиас` — машины (ssh в режиме BatchMode, ключ входа должен быть без пароля; `id=!host` — машина выключена вами и не опрашивается). Нужны `ssh` на этом компьютере и Linux на машине (`/proc`); недоступная машина — «нет связи». Ключ — `RPV_DECK_KEY`, файл known_hosts — `RPV_DECK_KNOWN_HOSTS` (те же, что у проверки второй машины). Ход заданий — `*.json` свежее 30 минут в каталоге `RPV_PROGRESS_DIR` на машине: `{"done": 231, "total": 492, "step": "сверка", "unit": "ед."}`. Метки `ПК/VPS/СЧЁТ/КОЛ` — у машин с id `pc/vps/calc/col` (только они принимаются как «где» шага плана `plan.py`); остальные видны в списке машин.
- `RPV_PC=0` — выключить загрузку «этого ПК» (по умолчанию она включена, в том числе на macOS и Windows без `/proc`).
- `RPV_PLAIN` (по умолчанию включено; `0` — выкл.) — человеческие строки процессов: `claude -p` (Haiku) из временного каталога вне проекта, кэш `<проект>/.claude/pulse/plain-auto.json`; работает и для задач без плана (шаги по журналу тикета). Без `claude` в `PATH` (или `CLAUDE_BIN`) — строка по фактам плана («сделано: … N из M»). Модель: `RPV_PLAIN_MODEL`.
- Рядом с проектом: кадр всегда пишется в `<проект>/.claude/pulse/status.json` — даже если `RPV_BOARD` не задан. `.claude/board/mcp_server.py` — MCP-сервер «rpv-pulse»: `claude mcp add rpv-pulse -- python "<проект>/.claude/board/mcp_server.py" --project "<проект>"` (инструменты `pulse_status` и `pulse_answer`).

## Поднять свою

Нужен Python 3.11+ без пакетов, открытый порт (по умолчанию 8787; для интернета поставьте перед сервером HTTPS-прокси — токен в адресе не должен ходить открытым текстом).

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

Данные (команды, сводки) — `RPV_BOARD_DATA` (в юните `/var/lib/rpv-board`, без юнита `~/rpv-board`); порт — `RPV_BOARD_PORT` (8787), адрес — `RPV_BOARD_HOST` (0.0.0.0), файл токена — `RPV_BOARD_TOKEN_FILE`. Команд — до 20; ключ команды хранится только хэшем, удалить команду можно на странице.

Без systemd: macOS — LaunchAgent с `python3 /путь/server.py` (по образцу [автозапуска](install-macos.md#автозапуск-после-входа-в-систему-launchagent)) и теми же переменными в `EnvironmentVariables`; Windows — служба или задача планировщика, запускающая `python server.py` с этими переменными (токен — файл `RPV_BOARD_TOKEN_FILE`).

Тесты: `python -m unittest discover -s .claude/board`.
