# Role Play Vibing (RPV)

Плагин Claude Code: команда ролей (CEO, Исследователь, Инженер, Судья), которая работает через **тикеты** и
**диспетчер**, со сторожем и хуками. Ставится в любой проект; всё проектное — в `.claude/rpv.json`.

> **Статус: работа в процессе (0.1.0).** Репозиторий заливается по частям.
>
> | часть | состояние |
> |---|---|
> | манифесты плагина и маркетплейса, `agents/` (coder, reader) | готово |
> | `scripts/rpv_config.py` (корень проекта, `rpv.json`, роли), `scripts/ticket.py` (формат тикета) | готово |
> | `scripts/tickets.py`, `dispatch.py`, `watch.py`, `hooks/`, `hooks.json` | в работе (обобщение кода комплекта) |
> | `commands/` (`/rpv-init`, `/rpv-doctor`), `templates/` (устав, роли), навык «протокол до счёта» | в работе |
> | профили хуков, Stop-хук исполнителя, детектор зацикливания | в работе |
> | тесты и дымовая проверка в пустом каталоге | в работе |
>
> Пока не готово всё, ставить плагин в боевой проект рано.

## Установка (когда будет готово)

```
/plugin marketplace add https://github.com/Gogi213/role-play-vibing
/plugin install role-play-vibing@role-play-vibing
```

Затем в проекте: `/rpv-init`, проверка — `/rpv-doctor`.

## Идея

```
владелец ⇄ CEO (долгая сессия)
             │ tickets.py new …                 ← CEO заводит тикет только по слову владельца
             ▼
   .claude/tickets/TK-001.md   (шапка: owner, status, next, wait_for, effort, reviewer; тело; «## Лог»)
             │ роль пишет итог только так:  tickets.py comment <ID> --author <роль> --text "…" [--next <роль>]
             ▼
   dispatch.py (цикл): будит роль ОДИН раз по `next:` или по статусу → `claude -p …`
             ▼
   роль (Исследователь | Инженер | Судья) работает над одним тикетом, итог — в лог тикета
             │ нужен CEO: `--next ceo`, статус blocked/needs_owner/done, исчерпанный бюджет
             ▼
   ceo-inbox.md + ceo-wake.log  ←  watch.py (сторож без модели)
```
