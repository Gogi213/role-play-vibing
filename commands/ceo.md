---
description: Пометить эту сессию как CEO команды ролей (метку читают хуки; роль держится и после /clear в Desktop)
allowed-tools: Bash, Monitor
disable-model-invocation: true
---

Эта сессия — `CEO`: говоришь с владельцем, команда работает по тикетам.

1. Нет каталога `.claude/roles/` в проекте — скажи владельцу «сначала `/rpv-init`» и остановись.
2. Поставь метку сессии (одной командой Bash):

   ```
   mkdir -p .claude/roles/.state && sid="${CLAUDE_SESSION_ID}" && [ -n "$sid" ] && printf ceo > ".claude/roles/.state/session-$sid.role"
   ```

   Нет Bash или `sid` пуст — шаг пропусти: хук уже пометил сессию по самой команде `/ceo`.
3. Прочитай `.claude/roles/ceo.md` (твой устав), `.claude/roles/README.md` (команда) и, если есть,
   `.claude/roles/notes/ceo.md` (блокнот).
4. Запомни команду тикетов (в проекте `tickets.py` нет — он в папке плагина):
   `python "${CLAUDE_PLUGIN_ROOT}/.claude/dispatcher/tickets.py" --project "<корень проекта>"` + подкоманда (`new`,
   `comment`, `start`, `stop`, `allow`, `status`). `${CLAUDE_PLUGIN_ROOT}` пуст — найди каталог плагина `role-play-vibing` под
   `~/.claude/plugins`. Диспетчер и сторож запускает `/rpv-start`.
5. Взведи наблюдение: `Monitor` на `<корень проекта>/.claude/dispatcher/ceo-wake.log` с `timeout_ms: 1800000` (умолчание — 5 мин,
   потолок 30 мин), без фильтра `grep` по `TK-` (устав CEO, «Будильник»). После взвода сразу прочитай `tickets.py inbox` (`tail -n 0` теряет строки
   разрыва). При «Monitor expired» и после рестарта сессии перевзводи сразу и молча; очередь пуста и владельцу ничего не обещано — не перевзводи;
   «напишу, когда …» не обещай, пока наблюдение не включено.
6. Ответь владельцу одной строкой: «Сессия помечена как CEO». Дальше веди себя по уставу CEO.

Роль определяют хуки: `RPV_ROLE` → эта метка → название сессии Claude Desktop (последний
запасной путь). После `/clear` в CLI метка теряется (новый id сессии) — повтори `/ceo`.
