# Шина событий

Необязательная транспортная развязка без модели и без внешних пакетов (Python stdlib, sqlite): задание на удалённой машине кончилось — диспетчер просыпается за секунды, а не на следующем тике. Выключена, пока не задан `RPV_BUS_URL`.

## Состав

- **Шина** `.claude/bus/bus.py` — на любой машине с адресом: журнал событий с номерами (sqlite, WAL), HTTP с токеном, `POST /event`, `GET /q/<получатель>?after=&wait=` (long-poll), `POST /ack`, `/stats`, `/stale`. До ack событие остаётся в очереди; повтор с тем же `id` не дублируется. Юнит-шаблоны — `.claude/bus/rpv-bus.service`, `rpv-bus-watcher.service` (пути и порт поправить под машину); токен — файл `0600`.
- **Маршруты** — `.claude/bus/routes.json`: шаблон адреса (`задача.*.задание.упало`) → получатели `dispatcher` / `ceo` (получателя `board` добавляйте, когда у вас есть потребитель: неподтверждённое событие через N мин шлёт сигнал «не обработано»); «задача.<TK>.к_ceo» — запись с `--next ceo`, «вопрос_владельцу» — только запись, начинающаяся с «ВОПРОС ВЛАДЕЛЬЦУ»; `hold` — получатели, которым событие держится, пока тикет заблокирован. Блокеры: `задача.<TK>.блокер.поставлен` / `.снят`; диспетчер раз в 5 минут шлёт полный снимок (blocked, needs_owner, waiting на незакрытый `ticket:<ID>`).
- **Сторож машины** `.claude/bus/watcher.py --host <имя>` — сам видит остановку юнитов `rpv-*` (успех/падение с кодом) и файлы хода `*.json`, шлёт события; юниты править не нужно.
- **Диспетчер** — long-poll очереди `dispatcher`: событие → тик сразу, ack после тика; очередь `ceo` слушает без ack, только ради будильника (строка в `ceo-wake.log`), а подтверждает её CEO командой `tickets.py inbox` ([signals.md](signals.md)); шина недоступна → одна строка `bus-down`, диспетчер работает по таймеру и `wait_for`, сигналы CEO идут запасным путём в `ceo-inbox.md`, после возврата шины — `bus-up`.
- **Отправители** — `.claude/bus/busclient.py send <адрес> [--payload JSON]`; недоступная шина складывается в spool и дошлётся позже. Сам `tickets.py` шлёт события `задача.<ID>.сдано`, `.статус` и `.вопрос_владельцу` (шина не настроена или недоступна — команда не ломается); сигналы CEO — по [стандарту сигналов](signals.md).

## Настройка

`RPV_BUS_URL`, токен — `RPV_BUS_TOKEN` или `RPV_BUS_TOKEN_FILE` (запасной `~/.rpv-bus-token`), `RPV_BUS_DISABLE=1` — выключить, `RPV_BUS_SNAPSHOT_S` (300). Клиентам — только Python/utf-8 (curl на Windows шлёт кириллицу не в utf-8).

Порог «не обработано»: у очереди `ceo` — 2 часа, у остальных — `--stale-after` шины (600 с). Сигналы к CEO при заданном `RPV_BUS_URL` идут через очередь `ceo` — [signals.md](signals.md).

Тесты: `python -m unittest discover -s .claude/bus` и `-s .claude/dispatcher`.

## Задания на удалённых машинах

- **Уникальный запуск + InvocationID** — `.claude/bus/jobrun.sh <метка> -- <команда>` (Linux, systemd): юнит `<метка>-<MMDDhhmmss>-<4hex>`, запись `{unit, runid, invocation, started}` в `$RPV_RUNS_DIR` (по умолчанию `/var/lib/rpv/runs`). Ждать такой юнит — `wait_for: host:<машина>:unit:<имя>` ([wait-for.md](wait-for.md)).
- **Конец задания — событием шины, без опроса:** `watcher.py` шлёт `машина.<хост>.юнит.запущен` и `…остановлен/упал` с `invocation`; диспетчер засчитывает остановку без ssh, если InvocationID совпал с запуском; остановка старого экземпляра с тем же именем игнорируется. Запасной ssh-опрос — раз в 300 с (`RPV_DISPATCH_WAIT_POLL_S`).

## Установка сервера шины

Только Linux, необязательно. Поставляемые юниты — системные (`WantedBy=multi-user.target`, `/usr/bin/python3`, каталог `/opt/rpv-bus`); сторож машины опрашивает `systemctl`. Диспетчер на любой ОС подключается к шине клиентом (`RPV_BUS_URL`, `RPV_BUS_TOKEN_FILE`).

```sh
PLUGIN=/path/to/role-play-vibing         # каталог плагина
sudo mkdir -p /opt/rpv-bus
sudo cp "$PLUGIN"/.claude/bus/{bus.py,busclient.py,watcher.py,routes.json} /opt/rpv-bus/
sudo sh -c 'umask 077; python3 -c "import secrets; print(secrets.token_urlsafe(32))" > /opt/rpv-bus/token'
sudo cp "$PLUGIN"/.claude/bus/rpv-bus.service "$PLUGIN"/.claude/bus/rpv-bus-watcher.service /etc/systemd/system/
```

Перед запуском поправьте в `rpv-bus-watcher.service` `HOSTNAME` на имя машины, а порт (`--port 8788`) и пути — в обоих юнитах под свою машину. Сторожу машины нужны адрес и токен шины: добавьте в его `[Service]` строки `Environment=RPV_BUS_URL=http://<адрес шины>:8788` и `Environment=RPV_BUS_TOKEN_FILE=/opt/rpv-bus/token`. Затем:

```sh
sudo systemctl daemon-reload && sudo systemctl enable --now rpv-bus rpv-bus-watcher
```

На остальные Linux-машины с заданиями ставятся только `watcher.py`, `busclient.py` и `rpv-bus-watcher.service` (с `RPV_BUS_URL` на машину шины и файлом токена); `rpv-bus` — только на машину шины.

Веб-табло (`board_push.py --loop 5`) `/rpv-start` запускает сам, если в окружении задан `RPV_BOARD` — [dashboard.md](dashboard.md).
