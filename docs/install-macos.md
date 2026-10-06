# macOS

Общие требования и таблица платформ — [install.md](install.md). Здесь — то, что относится только к macOS. `<плагин>` — каталог плагина, `<проект>` — корень проекта (где `.claude/roles`).

## Заранее

Python 3.11 (`python3`; ролям ещё нужна команда `python` в `PATH` — если в системе только `python3`, добавьте `python` ссылкой или менеджером версий), git, `claude` в `PATH` (`command -v claude`) с выполненным входом, `ssh` — есть в системе.

## Установка

```
/plugin marketplace add https://github.com/Gogi213/role-play-vibing
/plugin install role-play-vibing@role-play-vibing
```

В проекте — `/rpv-init`.

## Запуск

`/rpv-start`. Ни WMI, ни systemd нет — `start.py` берёт запасной путь: `Popen` в новой сессии (`start_new_session`), stdin закрыт, вывод дописывается в `<проект>/.claude/dispatcher/dispatch.run.log`, `watch.run.log`. Строка «отвязан: нет (проверить не удалось)» здесь ожидаема: проверка читает `/proc/<pid>/cgroup`, которого на macOS нет.

## Остановка

```
kill "$(cat <проект>/.claude/dispatcher/dispatch.pid)"
kill "$(cat <проект>/.claude/dispatcher/watch.pid)"
```

Запущенные ролями процессы это не гасит (диспетчер подхватит их при следующем старте); снять роль — `tickets.py stop <ID> …` (только CEO).

## Переживает ли

Закрытие окна Claude — да; перезагрузку — нет. После перезагрузки удалите `<проект>/.claude/dispatcher/*.pid`: без `/proc` код не может проверить, чьё имя у процесса с pid из файла, и устаревший файл (pid уже занят чужим процессом) даёт ложное «уже запущен», а `/rpv-start` пошлёт этому чужому процессу SIGTERM (а если тот не завершится за 10 с — SIGKILL).

## Автозапуск после входа в систему (LaunchAgent)

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
    <!-- по необходимости добавьте RPV_* из docs/configuration.md (RPV_DISPATCH_ROLE_PARALLEL, RPV_BUS_URL, RPV_CALC_HOST, RPV_DECK_KEY) -->
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

Проверить: `launchctl print "gui/$(id -u)/local.rpv.dispatch"` и `tail <проект>/.claude/dispatcher/dispatch.run.log`. Перезапуск (например, после обновления плагина): `launchctl kickstart -k "gui/$(id -u)/local.rpv.dispatch"`. Остановка и снятие с автозапуска: `launchctl bootout "gui/$(id -u)/local.rpv.dispatch"` (то же для `watch`) и удалить plist. Старый синтаксис: `launchctl load -w <plist>` / `launchctl unload -w <plist>`.

С LaunchAgent не пользуйтесь `/rpv-start`: он остановит процесс по pid-файлу, `launchd` тут же поднимет его снова (`KeepAlive`), `start.py` запустит ещё один — один из двух выйдет по pid-замку. Если после перезагрузки в логе «уже запущен (pid …)», а диспетчера нет, — удалите `dispatch.pid` / `watch.pid` (причина — в разделе «Переживает ли» выше). Секрет, нужный службе (`ANTHROPIC_API_KEY` при входе по ключу), добавляйте в `EnvironmentVariables` только в своём plist (`chmod 600`), не в репозиторий.

## Веб-табло

Сервер Диспетчерской на macOS — LaunchAgent с `python3 /путь/server.py` (по образцу выше) и теми же переменными в `EnvironmentVariables`: [dashboard.md](dashboard.md).
