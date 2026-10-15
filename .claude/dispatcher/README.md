# Диспетчер

Раз в 15 с читает `<проект>/.claude/tickets/*.md` и будит роль (`claude -p`) или CEO (строка в `ceo-inbox.md`). Только stdlib, Python 3.11.

Скрипты запускаются прямо из папки плагина (`<плагин>`), копировать их в проект не нужно. Корень проекта — `--project <путь>`
(у `tickets.py` — перед подкомандой), иначе `RPV_PROJECT`, иначе `CLAUDE_PROJECT_DIR`, иначе ближайший каталог вверх от
текущего с `.claude/roles` (не расположение файла); не нашли — ошибка с подсказкой `--project` / `/rpv-init`, каталоги не
создаются. Состояние (`state.json`, `runs.log`, `runs/`, pid, `ceo-inbox.md`, `ceo-wake.log`, сердцебиение сторожа) — в
`<проект>/.claude/dispatcher/` (создаётся при старте); тикеты и роли — из проекта.

```
python <плагин>/.claude/dispatcher/start.py [--project <проект>]    # диспетчер + сторож в фоне, уже запущенные перезапускает (/rpv-start)
python <плагин>/.claude/dispatcher/dispatch.py --project <проект>   # диспетчер, боевой цикл (--once — один тик; --help — справка)
python <плагин>/.claude/dispatcher/watch.py --project <проект>      # сторож: сироты, blocked (--once — один цикл)
python <плагин>/.claude/dispatcher/tickets.py --project <проект> new --owner engineer --title "..." --ref "<цитата владельца или В-##>" [--effort high]   # ревьюер — Судья по умолчанию; Haiku-задача (--executor haiku --kind …) — без него
python <плагин>/.claude/dispatcher/tickets.py --project <проект> comment TK-001 --author engineer --text "..." [--next judge|ceo]
python <плагин>/.claude/dispatcher/tickets.py --project <проект> start TK-001 | status
python <плагин>/.claude/dispatcher/tickets.py --project <проект> stop TK-001 --text "<новая постановка>" [--next judge]   # только CEO
python -m unittest discover -s <плагин>/.claude/dispatcher && python -m unittest discover -s <плагин>/.claude/hooks   # тесты
```

`start.py` запускает службы отвязанными от сессии (Windows — WMI `Win32_Process.Create`, вне job-объекта приложения; Linux с systemd — `systemd-run --user`, юнит `rpv-<служба>-<хеш пути проекта>`; иначе `Popen`) и печатает «отвязан: да/нет»; чтобы юниты пережили выход из системы — `loginctl enable-linger $USER` (перезапуск юнита гасит и запущенные роли).

Роль, запущенная диспетчером, получает в окружении `RPV_ROLE`, `RPV_TICKET`, `RPV_PROJECT`;
в промпте — команда `tickets.py` с абсолютным путём из папки плагина (то же хук
`role_context.py` вставляет в начало каждой сессии роли и CEO).

Статусы: backlog, todo, in_progress, waiting (+ `wait_for: file:… | ticket:… | host:…`), in_review, done, blocked,
needs_owner, stopped (остановлено CEO — не будит, пока CEO не вернёт в `todo`). Будят: `todo`, `in_progress`, выполненный `wait_for` — владельца; `done`/`in_review` с `reviewer` —
ревьюера (Судья — на каждом тикете по умолчанию; `done` тикета без ревьюера в шапке уходит Судье и дописывает `reviewer: judge`, кроме `executor: haiku`); `--next` — названную роль один раз. Запуск без новой записи — один повтор, затем `blocked`. Траты считаются
(`runs.log`, `tickets.py status`), ничего не ограничивают.

Остановка роли — `tickets.py stop TK-NN --text "<новая постановка>" [--next <роль>]` (только CEO; из сессии роли, где стоит `RPV_ROLE`, отказ): заявка `<проект>/.claude/dispatcher/stop/<ID>.json`; на ближайшем тике диспетчер снимает запущенную роль этого тикета ВСЕМ деревом процессов (Windows — `taskkill /T /F /PID`, иначе — `killpg` группы: роль запускается с `start_new_session=True`) и проверяет, что процесс умер (не умер — строка CEO `[stop-failed]`, заявка остаётся).
Остановка — не провал: ни повтора, ни пометки «запуск не оставил запись»; счётчики холостых и «запись есть, статус тот же» запусков, повторов, ротации (сессия и контекст тикета) и часовой лимит запусков по тикету — с нуля.
След: в лог — запись `dispatcher` «остановлен CEO в ЧЧ:ММ», затем запись CEO с постановкой (последняя в логе); следующий запуск тикета — НОВАЯ сессия (без `--resume`), в промпте «прошлый запуск оборван CEO — проверь git status и недописанные правки, начни с новой постановки».
Статус: с `--next` — `todo` + `next: <роль>` (роль стартует в этом же тике); без `--next` — `stopped`: диспетчер не будит, сторож не считает сиротой, в `todo` возвращает CEO (`tickets.py start TK-NN` или правка шапки).

Публикации — `tickets.py allow TK-NN publish --ref В-N` (только CEO; из сессии роли отказ): в шапку тикета `allow: publish (В-N)`, в лог запись `ceo`; `--ref` — решение владельца, уже занесённое в реестр (`registry.py decide`). Пока поля нет, страж запуску роли (`RPV_ROLE`, тикет — `RPV_TICKET`) отказывает: `gh release create|upload|edit`, `gh pr merge` (PR вливает `merge_rule.py` после Судьи), `git push` в main/master, тега, `--all` и пуш без названной ветки, пока текущая — main/master, `npm|pnpm|yarn|cargo|poetry|uv publish`, `twine upload`, `docker push`, `gem push`, а также запись в GitHub мимо них: `gh api` с записью (`-X POST|PUT|PATCH|DELETE` либо `-f/-F/--field/--input`) на слияние и выпуск (`/pulls/<n>/merge`, `/merges`, `/releases`, `/git/refs`, `/git/tags`, `/contents/`) и `curl`/`wget`/`Invoke-WebRequest`/`Invoke-RestMethod` с записью на `api.github.com`/`uploads.github.com`. Чтение (`gh api` без записи, `curl` GET) свободно. Выкатка по ssh на чужие сервера, произвольные HTTP-запросы к другим сервисам и обходы через скрипты не ловятся — их держат согласие по тикету и проверка Судьи. Пуш рабочей ветки свободен. Шапка не прочитана — отказ. Запуск роли строку `allow:` в файл тикета не пишет (страж отказывает). `allow TK-NN --revoke` снимает согласие. Сессии CEO и владельца не ограничены.

Удаление веток на удалённом (`git push --delete`, `:ветка`) страж запрещает запуску роли всегда, а сессии CEO — пока нет согласия владельца: `tickets.py allow-delete <ветка>… --ref В-N` (только CEO; решение В-N уже в реестре) пишет `.claude/dispatcher/branch-delete.json` — согласие на названные ветки, час; main/master — никогда; `--revoke` снимает. Файл руками не правится (страж).
Ограничение: задания на машинах (`systemd-run`, ssh) диспетчер не останавливает — их снимает CEO; запущенный диспетчер видит заявку только после перезапуска (`/rpv-start`).

Настройки — [docs/configuration.md](../../docs/configuration.md): в окружении машины только `RPV_PROJECT`, `RPV_TG_TOKEN`, `RPV_TG_CHAT`, `RPV_PLUGIN_DIR`; настройки проекта — ключи `.claude/rpv.json` (пределы параллели, время отчёта, машины, каталоги стража); пороги механики (тик 15 с, таймаут запуска 20 мин, тормоза цикла 12/2, 2 возврата ревьюера, сверка ожиданий 300 с) — константы в коде.

`git reset --hard` и `git clean -f` страж пропускает только в каталоге вне основного дерева, заданном явным путём (scratchpad, `guard_remote_roots`, `/tmp/<подкаталог>`, связанный worktree); `git push --force` — никогда; публикации (выпуск, слияние PR, пуш в main/master и теги, пакеты) запуску роли — только с `allow: publish` в тикете (выше). Перезапись файлов в автопамяти проекта (`~/.claude/projects/<проект>/memory/`) разрешена.
