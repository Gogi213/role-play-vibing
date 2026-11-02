# Пульс — страница и закреплённое сообщение (В-38, шаг 2)

Макет: `docs/findings/pulse-mockup-2026-11-02.html`. Читать нужно только фразу сверху. Данные — `.claude/dispatcher/pulse.json`
каждого проекта (шаг 1, TK-095); здесь — как их показать.

## Что читает шаг 2 из pulse.json

| поле | смысл |
|---|---|
| `phrase` | фраза-итог проекта: «Всё идёт. От вас ничего.» / «Нужны вы: <проект> — <действие> по TK-N.» / «Застряло: <проект> — <причина>.» |
| `state` | `ok` \| `owner` \| `stuck`; нет поля — по началу фразы («Нужны вы», «Застряло») |
| `now`, `pause` | строки справа от клеток: «что сейчас» и самая длинная пауза суток |
| `cells` | 96 клеток по 15 минут за сутки, каждая `{"runs": N, "cls": "work\|none\|owner\|stuck"}` |
| `ts` | время записи (ISO с поясом): старше 2 минут — страница сама пишет «Застряло: <имя> — диспетчер не отвечает N мин.» |

Файл не читается — «Застряло: <имя> — пульс не читается.». Остальных полей шага 1 страница не показывает: машин, процессов, тикетов
на верхнем уровне, истории дальше суток нет.

## Страница

Один файл `.claude/board/pulse_server.py` (stdlib `http.server`) и шаблон `.claude/board/pulse.html`.

```
PULSE_TOKEN=<длинный-ключ> python <плагин>/.claude/board/pulse_server.py --project <проект> --host 0.0.0.0 --port 8787
```

- Адрес `http://<сервер>:8787/<ключ>/`; другой путь — 404. `/<ключ>/body` — только содержимое (его берёт автообновление раз в 30 с:
  подменяется, лишь если изменилось, мерцания нет). Ключ без `--token`/`PULSE_TOKEN` — сервер не стартует.
- Проекты — ключ `pulse_projects` в `<проект>/.claude/rpv.json`: `{"rpv": "C:/proj/rpv/.claude/dispatcher/pulse.json",
  "invoice": "https://host.example/invoice/pulse.json"}`. Путь — файл на этой машине, `http(s)://` — простой GET (5 с).
- Широкий экран: последние 8 часов (32 клетки), правый край — текущая клетка. Уже 480 px: сутки по 2 часа в клетке (12 клеток).
  Тёмная тема — по `prefers-color-scheme`.
- Юнит systemd (вместо старого табло на VPS):

```ini
[Unit]
Description=Пульс (rpv)
After=network-online.target

[Service]
Environment=PULSE_TOKEN=<длинный-ключ>
ExecStart=/usr/bin/python3 /opt/rpv/.claude/board/pulse_server.py --project /srv/hub --host 127.0.0.1 --port 8787
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

Замена старого табло: `systemctl disable --now rpv-board`, положить юнит в `/etc/systemd/system/rpv-pulse.service`,
`systemctl enable --now rpv-pulse`; снаружи — прежний обратный прокси на порт 8787.

## Закреплённое сообщение в Telegram

В rpv.json проекта-ведущего: `"pulse_telegram": true` и `pulse_projects` (тот же список). Диспетчер ведущего:

- первая строка — фраза-итог (застряло важнее «нужны вы»), дальше по строке на проект: «имя — идёт» / «имя — нужны вы: …» /
  «имя — застряло: …»;
- сообщения нет — создаёт и закрепляет один раз, `message_id` — в `state.json` (`pulse_tg`); дальше только `editMessageText`;
- правка — только при смене текста и не чаще раза в минуту; удалили сообщение — создастся новое;
- «отправлено» в `notify.log` — только при ответе API `ok`.

Нужны `RPV_TG_TOKEN` и `RPV_TG_CHAT` ([configuration.md](configuration.md)). Каждый проект на этой машине пишет pulse.json в свой
`.claude/dispatcher/`; удалённый отдаёт его по URL.
