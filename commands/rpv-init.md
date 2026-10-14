---
description: Подготовить проект к команде ролей — .claude/roles из шаблонов плагина, notes/, tickets/, реестр владельца (decisions.md, goals/), реестр ошибок errors/, строки в .gitignore
allowed-tools: Bash
disable-model-invocation: true
---

Подготовь текущий проект к команде ролей. Существующие файлы не перезаписывать. Одной командой Bash, из корня проекта:

```
T="${CLAUDE_PLUGIN_ROOT}/templates/roles"
[ -d "$T" ] || { echo "нет шаблонов: $T"; exit 1; }
mkdir -p .claude/roles/notes .claude/tickets .claude/goals .claude/errors
for f in "$T"/*.md; do b=$(basename "$f"); if [ -e ".claude/roles/$b" ]; then echo "есть, пропущен: .claude/roles/$b"; else cp "$f" ".claude/roles/$b" && echo "создан: .claude/roles/$b"; fi; done
for k in .claude/roles/notes .claude/tickets .claude/goals .claude/errors; do [ -e "$k/.gitkeep" ] || : > "$k/.gitkeep"; done
if [ -e .claude/decisions.md ]; then echo "есть, пропущен: .claude/decisions.md"; else printf '# Решения владельца\n' > .claude/decisions.md && echo "создан: .claude/decisions.md"; fi
touch CLAUDE.md
if grep -qxF "@.claude/decisions.md" CLAUDE.md; then echo "CLAUDE.md уже подключает .claude/decisions.md"; else [ -n "$(tail -c1 CLAUDE.md)" ] && echo >> CLAUDE.md; printf '\n@.claude/decisions.md\n' >> CLAUDE.md && echo "CLAUDE.md +@.claude/decisions.md"; fi
touch .gitignore
[ -n "$(tail -c1 .gitignore)" ] && echo >> .gitignore
for l in ".claude/roles/.state/" ".claude/roles/log/" ".claude/dispatcher/"; do if grep -qxF "$l" .gitignore; then echo ".gitignore уже есть: $l"; else printf '%s\n' "$l" >> .gitignore && echo ".gitignore +$l"; fi; done
```

`${CLAUDE_PLUGIN_ROOT}` пуст (путь не подставился) — найди каталог `templates/roles` плагина `role-play-vibing` под
`~/.claude/plugins` и подставь его в `T`.

Потом ответь владельцу коротко: что создано, что уже было и пропущено (реестр решений `.claude/decisions.md` подключён к
`CLAUDE.md` строкой `@.claude/decisions.md` — его видят все сессии); дальше — поправить пути зон в
`.claude/roles/*.md` под проект и открыть сессию CEO командой `/ceo`. Одной строкой напомни: что в проекте нельзя
удалять и перезаписывать никому (данные, резервные копии, ключи), владелец вписывает в `.claude/rpv.json`, ключ
`protected_paths` (список шаблонов, например `{"protected_paths": ["data/raw/**", "backups"]}`); файл не создавай сам.
Ещё одной строкой: ежедневный отчёт придёт владельцу в Telegram в 10:00 (`report_at` в `.claude/rpv.json`), когда на машине заданы `RPV_TG_TOKEN`
и `RPV_TG_CHAT` (docs/configuration.md); без них отчёт ложится файлом в `.claude/dispatcher/reports/`.
