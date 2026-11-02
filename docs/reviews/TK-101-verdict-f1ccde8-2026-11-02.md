# TK-101 · вердикт Судьи по PR #214 @ f1ccde8 — вернуть

| пункт договора | да/нет | на чём проверено |
|---|---|---|
| 1. ## 2.0.18 по v2.0.17..origin/main | нет | `git log v2.0.17..origin/main`: после базы PR (6d043d7) в main влит PR #207 (TK-094, merge 6688754: tickets.py, tickets_gates.py, тесты). В CHANGELOG его нет; заказ: «если к сборке влито ещё что-то — включить и перечислить» |
| 1. текст раздела | нет | «Что поменялось…»: «новых файлов в `.claude/dispatcher/` нет» — ложь: `git diff --name-status v2.0.17 origin/main` даёт A `tickets_gates.py` (сам раздел зовёт его «Новый модуль»), A `fixtures/tail_corpus.json`, A test_*.py |
| 2. release check, версии | да | diff: plugin.json, marketplace.json = 2.0.18 |
| 3. test_release | да | CI зелёный на f1ccde8 |
| 4. ровно три файла | да | `gh pr view 214 --json files` |
| 5. тега нет до слияния | да | тег v2.0.18 не стоит |

Что сделать: влить origin/main в ветку PR; добавить TK-094 (PR #207) в состав и в разделы (хвост TK-089: подсказка FP только на эвристику, --sha без регистра, dry-run без репо списком, --false-positive по «ложное срабатывание»); исправить строку про новые файлы (назвать tickets_gates.py и fixtures/tail_corpus.json). Перед сдачей ещё раз сверить `git log <голова>..origin/main` — пусто.
