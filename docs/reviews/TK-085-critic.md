Тикет: TK-085
Голова: b7a4c4a

Отчёт критика (Sonnet), сжато по пунктам:
1. EVENT_OF = события заказа, done не будит — держится: ceo_wake.py:35 (needs_owner, blocked, auth, due, следующий-не-определён, watch-goal-idle, goal-done), signals.py:57; у каждого вида есть источник. test_tk085 17 passed в копии на b7a4c4a.
2. Слияние main не потеряло строк — держится: configuration.md, signals.md, templates/roles/ceo.md, commands/ceo.md — убраны только Monitor-строки, тела строк таблицы целы; main 79eee8a вливается без конфликтов.
3. Удалённые тесты только про Monitor — не держится: test_tk065.py test_no_mechanics удалён целиком, хотя 5 из 7 фраз («Механику не пиши», «PR», «файлы и строки», «пересказ отчётов критика, Инженера, Судьи», ««жду»») живы в templates/roles/ceo.md:7 — правило В-26 больше не проверяется; test_tk062 test_sync_carries_both_rules_into_project_charter удалён вместе с проверкой переноса «**Покрыто решением — делай, не спрашивай:**» (Monitor не касается), импорт charter остался неиспользуемым.
4. Сессия не найдена/сбой — держится: ceo_wake.py:81,86,103,107,112; без исключений.
5. Monitor в ceo.md/commands/signals.md — держится; остаток вне заказанных файлов: docs/img/workflow.svg:3,79 («a Monitor wakes the CEO», «Monitor on ceo-wake.log»).
Упущено (не возврат): побудка синхронна (до 90 с на событие); нет дедупа «ceo-не-разбужен» без ceo-session.json; несовпадение режима разрешений код не видит — пункт живой проверки.

Ответ Судьи на 3: принял — проверил `git diff origin/main...b7a4c4a -- .claude/dispatcher/test_tk065.py test_tk062.py` сам: оба теста удалены целиком. Вернуть: восстановить test_no_mechanics без двух Monitor-фраз и test_sync_… с проверкой только «Покрыто решением».
Ответ Судьи на 5 (остаток): `git show b7a4c4a:docs/img/workflow.svg | grep -i monitor` — строки 3 и 79; схема README противоречит заказу п.3 — в тот же возврат (дёшево).
Ответ Судьи на «упущено»: замечаниями, не возврат — вне заказа; режим разрешений закрывает [вживую] после выпуска (В-37).
