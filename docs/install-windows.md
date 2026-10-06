# Windows

Общие требования и таблица платформ — [install.md](install.md). Здесь — то, что относится только к Windows. `<плагин>` — каталог плагина, `<проект>` — корень проекта (где `.claude/roles`).

## Заранее

Python 3.11 (`python` в `PATH`), git (Git for Windows — хукам плагина нужен `sh`), `powershell` (через него `start.py` создаёт процессы в WMI), `ssh.exe` — для `wait_for host:`.

`claude` должен быть в `PATH` из реестра (системного или пользовательского): служба, созданная через WMI, берёт `PATH` оттуда, а не из вашей сессии; иначе задайте `CLAUDE_BIN=<полный путь>` (переносится вместе с `RPV_*`).

## Установка

```
/plugin marketplace add https://github.com/Gogi213/role-play-vibing
/plugin install role-play-vibing@role-play-vibing
```

В проекте — `/rpv-init`.

## Запуск

`/rpv-start` или

```
python "<плагин>\.claude\dispatcher\start.py" --project "<проект>"
```

Служба создаётся через WMI — вне job-объекта приложения. Печатается pid (это `cmd`; pid самой службы — в `<проект>\.claude\dispatcher\dispatch.pid`, `watch.pid`), «отвязан: да» и лог (`dispatch.run.log`, `watch.run.log` там же). Повторный запуск перезапускает уже работающие.

Диспетчер и сторож с выводом в файл игнорируют чужой Ctrl+C общей консоли (иначе тихо умирали); службы стартуют под `python.exe`, а не `pythonw.exe`.

## Остановка

Отдельной команды нет:

```
Stop-Process -Id (Get-Content "<проект>\.claude\dispatcher\dispatch.pid")
Stop-Process -Id (Get-Content "<проект>\.claude\dispatcher\watch.pid")
```

Уже запущенные ролями процессы это не гасит (диспетчер подхватит их по pid при следующем старте); снять роль на тикете — `tickets.py stop <ID> …` (только CEO).

## Переживает ли

Закрытие Claude — да; перезагрузку — нет (код ничего не регистрирует в автозапуске). Поднять снова: `/rpv-start` или строка запуска выше. Присмотр, который раз в 5 минут проверяет сердцебиение и поднимает упавшие службы через Планировщик заданий, — [reliability.md](reliability.md).

## Веб-табло

Сервер Диспетчерской на Windows — служба или задача планировщика, запускающая `python server.py` с нужными переменными: [dashboard.md](dashboard.md).
