---
description: Подготовить проект к команде ролей — .claude/roles из шаблонов плагина, notes/, tickets/, строки в .gitignore
allowed-tools: Bash
disable-model-invocation: true
---

Подготовь текущий проект к команде ролей. Существующие файлы не перезаписывать. Одной командой Bash, из корня проекта:

```
T="${CLAUDE_PLUGIN_ROOT}/templates/roles"
[ -d "$T" ] || { echo "нет шаблонов: $T"; exit 1; }
mkdir -p .claude/roles/notes .claude/tickets
for f in "$T"/*.md; do b=$(basename "$f"); if [ -e ".claude/roles/$b" ]; then echo "есть, пропущен: .claude/roles/$b"; else cp "$f" ".claude/roles/$b" && echo "создан: .claude/roles/$b"; fi; done
for k in .claude/roles/notes .claude/tickets; do [ -e "$k/.gitkeep" ] || : > "$k/.gitkeep"; done
touch .gitignore
[ -n "$(tail -c1 .gitignore)" ] && echo >> .gitignore
for l in ".claude/roles/.state/" ".claude/roles/log/" ".claude/dispatcher/"; do if grep -qxF "$l" .gitignore; then echo ".gitignore уже есть: $l"; else printf '%s\n' "$l" >> .gitignore && echo ".gitignore +$l"; fi; done
```

`${CLAUDE_PLUGIN_ROOT}` пуст (путь не подставился) — найди каталог `templates/roles` плагина `role-play-vibing` под
`~/.claude/plugins` и подставь его в `T`.

Потом ответь владельцу коротко: что создано, что уже было и пропущено; дальше — поправить пути зон в
`.claude/roles/*.md` под проект и открыть сессию CEO командой `/ceo`.
