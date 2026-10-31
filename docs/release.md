# Выпуск плагина

Выпуск — ручной: его делает владелец или мейнтейнер плагина. Автовыпуска нет: слияние PR в репозиторий плагина ничего не ставит, не перезапускает и не откатывает — инструмент не правит и не выпускает сам себя.

## Как выпустить версию

1. Допишите раздел в [`CHANGELOG.md`](../CHANGELOG.md) — самым верхним, формат Keep a Changelog: заголовок `## X.Y.Z — ГГГГ-ММ-ДД` (`release.py` ищет версию в начале заголовка) и подразделы `### Добавлено` / `### Изменено` / `### Исправлено`.
2. `python .claude/dispatcher/release.py bump X.Y.Z` — версия в `plugin.json` и `marketplace.json`, проверка записи в `CHANGELOG.md`, коммит «Выпуск X.Y.Z» и тег `vX.Y.Z` (локально, ничего не отправляется). Тег ставят на коммит слияния (merge-коммит PR) в основной ветке, не на ветку работы.
3. Отправьте руками: `git push origin HEAD vX.Y.Z` (или в п. 2 добавьте `--push`). Это публикация — необратимая, поэтому только по решению владельца.

`python .claude/dispatcher/release.py check` проверяет, что версии в `plugin.json`, `marketplace.json` и `CHANGELOG.md` совпадают; то же проверяет тест в [CI](../README.md#разработка-и-тесты).

## Как обновиться пользователю

Одной командой: `/rpv-update` (в сессии CEO; слово владельца об обновлении — сначала в реестр, номер `В-N` — в `--ref`; из терминала — `python <плагин>/.claude/dispatcher/update.py --ref В-N`). Она делает `claude plugin marketplace update role-play-vibing`, `claude plugin update role-play-vibing@role-play-vibing`, затем из новой версии: `start.py` (службы), `supervise.py --install` (присмотр ОС; на macOS plist с `PATH` и `CLAUDE_BIN`), `tickets.py charter sync --ref В-N` (уставы) и `doctor.py`, и печатает одну таблицу. Любой FAIL назван как есть. Версия уже последняя — скрипт так и скажет и всё равно проверит службы. Последняя строка — «перезапустите Claude Code»: до перезапуска открытая сессия работает на хуках старой версии.

По частям, штатно средствами Claude Code: меню `/plugin` или те же две команды `claude plugin …`, затем перезапуск Claude Code, `/rpv-start` и `tickets.py charter sync`.

Нужна прежняя версия — переподключите маркетплейс на её тег и поставьте плагин заново: `/plugin marketplace remove role-play-vibing`, `/plugin marketplace add Gogi213/role-play-vibing#vX.Y.Z`, `/plugin install role-play-vibing@role-play-vibing`.
