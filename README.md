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
3. Диспетчер и сторож запускаются прямо из папки плагина (`<плагин>` — каталог установленного плагина, `${CLAUDE_PLUGIN_ROOT}`), копировать ничего не нужно:

```
python <плагин>/.claude/dispatcher/dispatch.py --project <проект>
python <плагин>/.claude/dispatcher/watch.py --project <проект>
```

Корень проекта — `--project`, иначе `RPV_PROJECT`, иначе `CLAUDE_PROJECT_DIR`, иначе текущий каталог. Состояние (`state.json`, логи, `ceo-inbox.md`, `ceo-wake.log`) — в `<проект>/.claude/dispatcher/`; тикеты и роли — из проекта. Тикеты заводит CEO: `python <плагин>/.claude/dispatcher/tickets.py --project <проект> new …`. Подробнее — `.claude/dispatcher/README.md`.

## Настроить под проект

- Переменные — `RPV_*`; прежние `ALPHA_*` — запасные (работают, если `RPV_*` не задана).
- Вторая машина для ssh-проверок — `RPV_DECK_HOST` (+ `RPV_DECK_KEY`, `RPV_DECK_KNOWN_HOSTS`, корень очереди на ней — `RPV_DECK_ROOT`, по умолчанию `~/rpv`); нет переменной — проверки выключены (или пустой файл `<проект>/.claude/dispatcher/deck-off`).
- Сессия CEO — команда `/ceo` (метка сессии); запасной путь — слово `CEO` в названии сессии Claude Desktop.
- Зоны и ссылки — `.claude/roles/*.md`.
- Модели ролей — `RPV_DISPATCH_MODEL`, `RPV_DISPATCH_ROLE_MODEL`, `RPV_DISPATCH_EFFORT` (умолчания — в `dispatch.py`: `CLAUDE_MODEL`, `ROLE_MODEL`).
- Защита удаления — хук `PreToolUse` (Bash/PowerShell) в `hooks/hooks.json`; корень проекта — `CLAUDE_PROJECT_DIR`; удалённые каталоги, стадия и закрытые хосты — `RPV_GUARD_REMOTE_ROOTS`, `RPV_GUARD_HOST_ROOTS` (`хост=корень,корень;хост2=…` — только при ssh на этот хост), `RPV_GUARD_STAGE`, `RPV_GUARD_FORBIDDEN_HOSTS` (без переменных — на удалённых машинах удалять нельзя нигде; прежние `ALPHA_GUARD_*` — запасные).
- Хуки запускает `hooks/run-hook.sh`: `python3`, иначе `python`, иначе `py -3`; в проекте без `.claude/roles` хуки молчат.
- Тесты: `python -m unittest discover -s .claude/dispatcher` и `-s .claude/hooks`.
- Нужно: Python 3.11, `claude` в `PATH`, git.
