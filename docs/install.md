# Установка: требования, запуск, платформы

Сам плагин ставится одинаково везде:

```
/plugin marketplace add https://github.com/Gogi213/role-play-vibing
/plugin install role-play-vibing@role-play-vibing
```

Дальше, в проекте: `/rpv-init` — создаёт `.claude/roles` (уставы из шаблонов), `.claude/tickets` и строки в `.gitignore`; `/ceo` — помечает сессию как CEO (явная роль); `/rpv-start` — запускает диспетчер и сторож в фоне.

Различается запуск в фоне: `start.py` выбирает способ по ОС. Ниже — то, что делает код (`start.py`, `dispatch.py`): поведение на macOS и Linux выведено из него, а примеры автозапуска на этих системах не запускались. `<плагин>` — каталог плагина, где лежит `.claude/dispatcher/start.py`; `<проект>` — корень проекта (где `.claude/roles`).

Инструкции по системам: [Windows](install-windows.md) · [macOS](install-macos.md) · [Linux](install-linux.md).

## Нужно на любой ОС

- Python 3.11 (только stdlib) и команда `python` в `PATH`: роли получают команды вида `python <плагин>/.claude/dispatcher/tickets.py …`. Хуки плагина ищут `python3`, затем `python`, затем `py -3`.
- git и Claude Code CLI `claude` в `PATH` службы (или `CLAUDE_BIN=<полный путь>`), с выполненным входом под тем же пользователем: роли запускаются как `claude -p … --permission-mode bypassPermissions`. Служба получает от `start.py` только `RPV_*`, `ALPHA_*`, `CLAUDE_BIN`, `CLAUDE_PROJECT_DIR`, `CLAUDE_CONFIG_DIR` и `PATH` (на POSIX); секреты (`ANTHROPIC_*`, токены) не переносятся — если службе нужен такой ключ, задайте его в её окружении сами.
- ssh-клиент и ключ — только если в тикетах есть `wait_for: host:<алиас>:…`. Хост берётся из `RPV_CALC_HOST` / `RPV_VPS_HOST` / `RPV_DECK_HOST` (что понимает `ssh`: `user@host` или алиас из `~/.ssh/config`), ключ — `RPV_DECK_KEY`, `RPV_DECK_KNOWN_HOSTS`. Вызов идёт с `BatchMode=yes` — пароль не спрашивается, нужен ключ без парольной фразы (или ssh-agent). Удалённая сторона — Linux: проверки выполняются командами `test`, `cat`, `systemctl`, `grep`.

## Что делает `/rpv-start`

Запускает диспетчер и сторож и печатает pid и пути логов (`<проект>/.claude/dispatcher/dispatch.run.log`, `watch.run.log`). Уже запущены (pid-замок) — останавливает и запускает заново: так подхватывается обновлённый плагин. Службы отвязаны от сессии (Windows — WMI, вне job-объекта приложения; Linux — юнит `systemd-run --user`, чтобы пережил выход из системы: `loginctl enable-linger $USER`), в конце печатается «отвязан: да/нет». Если задан `RPV_BOARD`, третьей службой стартует отправка сводки на табло ([dashboard.md](dashboard.md)).

## Запуск вручную

Скрипты работают прямо из папки плагина (`<плагин>` — каталог установленного плагина, `${CLAUDE_PLUGIN_ROOT}`), копировать ничего не нужно:

```
python <плагин>/.claude/dispatcher/start.py --project <проект>      # то же, что /rpv-start
python <плагин>/.claude/dispatcher/dispatch.py --project <проект>
python <плагин>/.claude/dispatcher/watch.py --project <проект>
```

Корень проекта — `--project`, иначе `RPV_PROJECT`, иначе `CLAUDE_PROJECT_DIR`, иначе ближайший каталог вверх от текущего с `.claude/roles`; не нашли — ошибка с подсказкой `--project` / `/rpv-init`, каталоги не создаются. Состояние (`state.json`, логи, `ceo-inbox.md`, `ceo-wake.log`) — в `<проект>/.claude/dispatcher/`; тикеты и роли — из проекта.

Тикеты заводит CEO: готовая команда с абсолютным путём `python "<плагин>/.claude/dispatcher/tickets.py" --project "<проект>" new …` приходит в начале сессии от хука (для CEO, помеченного `/ceo`, — из команды `/ceo`). Подробнее — [`.claude/dispatcher/README.md`](../.claude/dispatcher/README.md).

## Платформы

| | Windows | macOS | Linux |
|---|---|---|---|
| `/rpv-start` запускает через | WMI `Win32_Process.Create` | `Popen` в новой сессии (`start_new_session`) | `systemd-run --user`; нет systemd — как на macOS |
| закрыли окно Claude | работает дальше | работает дальше | работает дальше |
| выход из системы | не проверялось | не проверялось | работает дальше при `loginctl enable-linger $USER` |
| перезагрузка | не переживает | не переживает | не переживает (без постоянного юнита) |
| остановка | `Stop-Process` по `*.pid` | `kill` по `*.pid` | `systemctl --user stop` |
| автозапуск | вручную: `/rpv-start` | LaunchAgent ([пример](install-macos.md#автозапуск-после-входа-в-систему-launchagent)) | user-юнит systemd ([пример](install-linux.md#автозапуск-после-перезагрузки-постоянный-user-юнит)) |

Постоянный присмотр, который сам поднимает упавшие службы, ставится отдельно — [reliability.md](reliability.md).
