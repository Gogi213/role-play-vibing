Тикет: TK-095
Голова: f7b96909f197ad5cac702a8a2df7fc2254965215

# Критик перед приёмкой TK-095 (PR #209), 2026-11-02

Держится: п.1 (96 клеток, сутки, файл не растёт), п.4 (три фразы), п.5 (тик пишет pulse.json), п.7 (step_uncovered через goal_idle._due, без второго порога), unserved, loop, work/stuck/needs_owner «на момент клетки».

Не держится:
1. none шире заказа: pulse.py:232-233 ставит none при открытых тикетах без причины; docs/pulse.md:19 называет исключением только ожидание на машине.
2. OWNER_WAIT (pulse.py:28) ловит «owner» в пути (host:gpu1:/home/owner/x.done, merged:ownerco/repo#5) → жёлтая клетка, нарушение п.4 заказа; «ответ владельца» не ловит.
3. merged_unreleased: pulse.py:123 ищет только "unreleased:main", сторож пишет `unreleased:<default_branch>` (watch.py:780) — для master/develop не сработает; тест test_tk095.py:167 зашивает main.
4. ci_red: merge-state {"conflict": sha} не снимается после пуша (merge_rule.py:264-278) — Пульс держит конфликт до слияния, не сверяя sha с головой; `pr: 149, 153` (tickets.py:283) даёт ключ `repo#149, 153` — ci_red молчит.
5. dispatcher_dead: last_tick ставится после PULSE.update (dispatch.py:767/771), generated = начало тика — тик дольше ~105 с даёт ложное «Застряло»/«диспетчер не отвечает».
Прочее: запись pulse.json без повторов os.replace на Windows (есть P.atomic_write_text, project.py:304); копии _read_all/read_json/write_json; limit() принимает 0 при rpv_config ≥ 1; docs/pulse.md не называет условий merged_unreleased (watch_unreleased, ci_repo) и ci_red (ci_repo).

Ответ Судьи на 1: принял частично — класс для «очередь есть, причины нет» заказ не даёт; none допустим, но docs/pulse.md:19 привести к коду (todo/blocked/in_review в пределах порога — none). Замечание, правка дока — в возврат.
Ответ Судьи на 2: принял — вернуть. Жёлтое только по слову владельца (п.4, В-38); убрать голое «owner», тест с owner в пути.
Ответ Судьи на 3: принял — вернуть. Любой маркер `unreleased:*` (ветку брать из ключа), тест с master. Плагин универсальный.
Ответ Судьи на 4: принял — вернуть. Конфликт считать, только если merge-state.conflict == текущая голова (ci-state sha); pr списком — каждый номер; тесты на оба.
Ответ Судьи на 5: принял — вернуть. Порог мёртвого диспетчера сверять с временем конца прошлого тика (или порог не жёстче STALE/простоя), generated — время записи; тест на долгий тик.
Ответ Судьи на прочее: запись — через P.atomic_write_text (вернуть, на Windows PermissionError уже ловили); копии функций и limit()≥1 — замечание, исправить заодно.
