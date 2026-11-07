Тикет: TK-085
Голова: 0a38cce

Отчёт критика (Sonnet) по 0a38cce, сжато:
1. test_no_mechanics возвращён — **держится**: test_tk065.py, те же фразы, что в origin/main, кроме двух Monitor-фраз; все пять живы в templates/roles/ceo.md:7-8.
2. Перенос charter.sync для «Покрыто решением» — **держится**: test_tk062.py:75-83 = origin/main:test_tk062.py:87-96 без проверки удалённого по заказу правила «Наблюдение держишь».
3. Monitor нет в docs/img/*.svg, docs/signals.md, commands/ceo.md, templates/roles/ceo.md — **держится**; workflow.svg валидный XML, метка заменена на ceo_wake.py: SendMessage.
4. Слит main 79eee8a; diff ace7911..0a38cce — только три файла правки — **держится**. origin/main ушёл вперёд (853ba09), merge-tree без конфликтов.
5. test_tk085 не закрепляет отсутствие Monitor в docs/img — **не хватает** (только grep).
Упущено: Monitor в докстрингах/сообщениях тестов (test_tk062.py:2, test_signals.py:1,46, test_dispatch.py:1764, test_overseer.py:213, test_tk075.py:1); мёртвая CEO_COMMAND в test_tk062.py:22.

«Не держится» нет.
Ответ Судьи на 4: gh pr view 199 — MERGEABLE CLEAN, CI зелёный на голове; ветка вливается без конфликтов — не возврат.
Ответ Судьи на 5: замечаниями, не возврат — заказ п.3 называет устав, команду и signals.md, они закреплены TextTests; комментарии тестов и мёртвая константа не меняют поведения.
Прогон Судьи в копии на 0a38cce: test_tk065, test_tk062, test_tk085 — все зелёные.
