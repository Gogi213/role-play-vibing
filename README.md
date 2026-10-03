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

Скопировать в проект: `.claude/dispatcher`, `.claude/roles`, `.claude/tickets`. Хуки (`.claude/hooks`) не копировать — их подключает плагин, копия сработает дважды.
В `.gitignore` проекта: `.claude/roles/.state/`, `.claude/roles/log/`, `.claude/dispatcher/*.log`, `.claude/dispatcher/state.json`.
Запуск диспетчера и сторожа — `.claude/dispatcher/README.md`.

## Настроить под проект

- Вторая машина для ssh-проверок — `ALPHA_DECK_HOST` (+ `ALPHA_DECK_KEY`, `ALPHA_DECK_KNOWN_HOSTS`); нет переменной — проверки выключены (или пустой файл `.claude/dispatcher/deck-off`).
- Сессия CEO — в названии сессии Claude Desktop слово `CEO`.
- Зоны и ссылки — `.claude/roles/*.md`.
- Модели ролей — `dispatch.py`: `CLAUDE_MODEL`, `ROLE_MODEL`.
- Защита удаления — хук `PreToolUse` (Bash/PowerShell) в `hooks/hooks.json`; корень проекта — `CLAUDE_PROJECT_DIR`; удалённые каталоги, стадия и закрытые хосты — `ALPHA_GUARD_REMOTE_ROOTS`, `ALPHA_GUARD_HOST_ROOTS` (`хост=корень,корень;хост2=…` — только при ssh на этот хост), `ALPHA_GUARD_STAGE`, `ALPHA_GUARD_FORBIDDEN_HOSTS` (без переменных — на удалённых машинах удалять нельзя нигде).
- Хуки запускает `hooks/run-hook.sh`: `python3`, иначе `python`, иначе `py -3`; в проекте без `.claude/roles` хуки молчат.
- Тесты: `python -m unittest discover -s .claude/dispatcher` и `-s .claude/hooks`.
- Нужно: Python 3.11, `claude` в `PATH`, git.
