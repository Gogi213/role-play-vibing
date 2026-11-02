Тикет: TK-085
Голова: 59c77d6

Отчёт критика (Sonnet), сжато по пунктам:
1. Двойной blocked при Haiku-отказе — держится: dispatch.py:719 ставит `blocked@{now_iso(now)}`, маркер signals.py:183 `status@updated`, updated пишет write_header_updates (ticket.py:557-558) тем же now; test_tk085.py:131-141 без строки 719 падает.
2. docs/signals.md по EVENT_OF — не держится: таблица docs/signals.md:21-33 и configuration.md:77 совпадают с ceo_wake.py:41-48, но схема docs/img/ceo-signals.svg (вставлена в docs/signals.md:16) в блоке «будит» держит done · blocked · needs_owner / next-ceo · ждёт-ceo · wait-for / watch-* · работа-стоит/идёт / хвост, стрелка «событие» ведёт к «CEO просыпается: ceo_wake.py»; auth, due, goal-done, следующий-не-определён не названы. Тест проверяет только отсутствие слова Monitor.
3. Monitor из dispatch.py/project_paths.py/svg — держится (остатки — докстринги тестов, на поведение не влияют).
4. Тест вызова notify_goal_closed — держится: test_tk085.py:147-158 падает при удалении вызова из _collect_candidates.
5. Прежнее не сломано — держится: поведение меняет одна строка dispatch.py:719.
Упущено: вид ceo-не-разбужен не в таблице docs/signals.md (есть в тексте:53); watch-unreleased (В-24) без побудки стал строкой очереди — следует из заказа, сказать CEO; watch-goal-idle (TK-086) нет в EVENT_OF — записано в лог.

Ответ Судьи на 2: принял — проверил svg:59-62 сам; схема над таблицей повторяет то, за что вернули 03b2bed («done — будит»), и противоречит заказу п.2; вернуть: блок «будит» привести к видам EVENT_OF, остальные — «строка очереди».
Ответ Судьи на «упущено»: ceo-не-разбужен строкой таблицы — в тот же возврат (дёшево). watch-unreleased — замечанием, по заказу. watch-goal-idle — связка с PR #198, не возврат.
