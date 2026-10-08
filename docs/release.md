# Выпуск, обновление, откат

Все четыре действия — один скрипт `release.py` из папки плагина:

```
python tools/release_bump.py X.Y.Z [--push]
python .claude/dispatcher/release.py update
python .claude/dispatcher/release.py rollback [X.Y.Z]
python .claude/dispatcher/release.py check
```

| команда | что делает |
|---|---|
| `tools/release_bump.py X.Y.Z [--push]` | версия в `plugin.json` и `marketplace.json`; раздел в `CHANGELOG.md` обязателен; коммит и тег `vX.Y.Z` (`--push` — и отправить) |
| `update` | обновить установленный плагин до последнего выпуска (печатает версию до и после; прежняя запоминается) |
| `rollback [X.Y.Z]` | вернуть прошлую версию одной командой: маркетплейс с тегом `vX.Y.Z`; без аргумента — версия, записанная перед последним `update` |
| `check` | версии в `plugin.json`, `marketplace.json` и `CHANGELOG.md` совпадают (то же проверяет тест в CI) |

После `update` и `rollback` — перезапустить Claude Code.

## Как устроен откат

Тег проверяется на GitHub до любых изменений: нет тега — ничего не тронуто. Маркетплейс прибивается к `owner/repo#vX.Y.Z`; при сбое возвращается прежний. `update` снимает прибивку к тегу и возвращает маркетплейс на `main`.

## Как выпустить версию

1. Допишите раздел в [`CHANGELOG.md`](../CHANGELOG.md) — самым верхним, формат Keep a Changelog: заголовок `## X.Y.Z — ГГГГ-ММ-ДД` (`release.py` ищет версию в начале заголовка) и подразделы `### Добавлено` / `### Изменено` / `### Исправлено`.
2. `python tools/release_bump.py X.Y.Z --push`.

`release_bump.py` сам проверяет согласованность версий после правки файлов; отдельно `check` удобен перед слиянием (его же проверяет тест в [CI](../README.md#разработка-и-тесты)).
