# Linux

Общие требования и таблица платформ — [install.md](install.md). Здесь — то, что относится только к Linux. `<плагин>` — каталог плагина, `<проект>` — корень проекта (где `.claude/roles`).

## Заранее

Python 3.11 (`python` в `PATH`; в Debian/Ubuntu `python` даёт пакет `python-is-python3`), git, `claude` в `PATH` с выполненным входом, `ssh`. Для `/rpv-start` через systemd — работающий `systemctl --user`; на сервере без интерактивной сессии включите `loginctl enable-linger $USER`.

## Установка

```
/plugin marketplace add https://github.com/Gogi213/role-play-vibing
/plugin install role-play-vibing@role-play-vibing
```

В проекте — `/rpv-init`.

## Запуск

`/rpv-start`. Если есть `systemd-run` и отвечает `systemctl --user`, каждая служба — юнит `rpv-<dispatch|watch>-<8 знаков хеша пути проекта>` (`systemd-run --user --collect`, вывод в `<проект>/.claude/dispatcher/<имя>.run.log`), «отвязан: да» — по cgroup юнита. Нет systemd (контейнер и т. п.) — запасной путь как на macOS: `Popen` в новой сессии, остановка по `*.pid`.

## Остановка

`systemctl --user list-units 'rpv-*'` — имена, затем `systemctl --user stop <юнит>`. Остановка юнита гасит и запущенные ролями процессы этого проекта.

## Переживает ли

Закрытие Claude — да; выход из системы — да при `loginctl enable-linger $USER`; перезагрузку — нет: юниты `systemd-run` временные и после неё исчезают.

## Автозапуск после перезагрузки (постоянный user-юнит)

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
# по необходимости - RPV_* из docs/configuration.md, start.py перенесёт их в службы:
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

## Сервер шины и Диспетчерская

Сервер шины событий (необязательно, только Linux) — [bus.md](bus.md#установка-сервера-шины); сервер Диспетчерской с юнитом `rpv-board` — [dashboard.md](dashboard.md#поднять-свою).
