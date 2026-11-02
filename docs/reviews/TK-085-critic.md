Тикет: TK-085
Голова: 03b2bed

Отчёт критика (Sonnet), сжато по пунктам:
1. События → CW.wake — держится частично: needs_owner/blocked/auth/due/goal-done доходят; done — ноль. «следующий-не-определён» шлёт только TK-087 (PR #200), зависимость не записана. Путь Haiku-отказа (dispatch.py:716-719) ставит blocked и шлёт сигнал без отметки ceo_status_notified (ставит только finish.py:54) — следующий тик notify_status_for_ceo (signals.py:182) шлёт blocked второй раз → второй вызов claude.
2. notify_goal_closed — держится: статусы как в registry.py:195,202; первый обход только запоминает; state сохраняется save_state (dispatch.py:775); вызов обёрнут (dispatch.py:664-667). Пробел: вызов из тика тестом не закрыт.
3. Устав/команда/docs без Monitor — не держится: docs/signals.md:47 называет вид goal-step, которого в коде нет; таблица docs/signals.md:20-33 пишет «done — будит», next-ceo «будит» и т.д. — противоречит строке 47 и заказу п.2; docs/configuration.md:77 обещает, что watch-unreleased будит CEO — его нет в EVENT_OF; остатки «Monitor CEO» в dispatch.py:194, project_paths.py:33, docs/img/ceo-signals.svg (вставлена в docs/signals.md:16).
4. Тесты — держится с пробелами: удаление вызова notify_goal_closed из тика тесты не заметят; связь needs_owner/block_ticket → событие не покрыта.

Ответ Судьи на 1 (двойной blocked при Haiku-отказе): принял — нарушает договор п.1 «один вызов на событие»; вернуть.
Ответ Судьи на 3: принял — docs/signals.md в заказе п.3; документ, где done «будит», и несуществующий вид goal-step противоречат заказу п.2; вернуть. Остатки Monitor в комментариях dispatch.py/project_paths.py и svg — в том же возврате (дёшево).
Ответ Судьи на «следующий-не-определён»: строка совпадает с TK-087 дословно (проверено); зависимость от PR #200 — замечанием, не возвратом.
Ответ Судьи на 4: тест на вызов notify_goal_closed из tick — в возврат (без него гейт не держит место вызова).
