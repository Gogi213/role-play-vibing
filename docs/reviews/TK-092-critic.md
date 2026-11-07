# TK-092 — критик по f48b97a (слияние main)
Голова: f48b97a

Отчёт критика (Sonnet), сверено по git show --remerge-diff f48b97a -- alarms.py; тесты в копии .claude/worktrees/TK-092 на f48b97a.
1) Флаг «отправлено» только при доставке — держится: alarms.py:111-113 (повтор не чаще notify.RETRY_S), alarms.py:118-122 (sent только при notify.delivered, иначе owner_try); notify.py:80 delivered, notify.py:29 RETRY_S.
2) Потолок возвратов из main — держится: alarms.py:114 cap по REVIEW_CAP_MARK (импорт alarms.py:16, signals.py:122), alarms.py:118 повод «потолок-возвратов», alarms.py:119 тело [:1500 if cap else 400].
3) Тесты обеих сторон — держится: test_review_cap.py:166-170 патчит notify.send=True, delivered зовёт send; OwnerQuestionTests (test_overseer.py) — флаг и RETRY_S. pytest test_review_cap.py test_notify.py test_overseer.py: 36 passed.
Где не сработает: не найдено. Пп.1-5 договора — по прежнему проходу на bca83f3 (ниже).

---
Тикет: TK-092
Голова: bca83f3

Отчёт критика (Sonnet), сверено по git diff 0abe4c0..bca83f3 (только doctor.py и test_doctor.py) и пробам check_notify через python -I с подменённым opener в копии .claude/worktrees/TK-092.

1. Ротация блокнота — держится (коммит не трогал).
2. ticket_of tk-?0*(\d+) и WARN без тикета — держится (не трогал).
3. doctor.check_notify — держится для всех реальных ответов и сбоев: null/[1]/"x"/1/true/{}/{"ok":false} — FAIL без трейсбека (doctor.py:161-162, isinstance(dict)); пустое тело, битый UTF-8, BOM — WARN; IncompleteRead/RemoteDisconnected/Timeout/SSLError/ConnectionReset — WARN; HTTPError 401/403/404 — FAIL, прочие коды — WARN; тест test_doctor.py:66-68 покрывает null/[1]/"x"/1.
   Не держится (по буквальному «ни на каком ответе», формулировка из запроса Судьи, не из договора): JSON глубиной ~100 тыс. уровней даёт RecursionError — не ловится (doctor.py:167); исключения не из urlopen (TypeError и т.п.) — теория. Смежное: {"ok":"false"} даёт OK (doctor.py:169, проверка по истинности).
4. «Отправлено» только при доставке — держится (не трогал).
5. hooks.json без PostToolUse:Skill — держится (git grep пуст на pr205).

Ответ Судьи на 3: возражаю, не «вернуть». Договор п.3: «401 даёт FAIL, не настроен — WARN, сеть — WARN» — выполнен; прошлые возвраты были на реальные пути (HTTPException от urlopen, корректный JSON не-объект). «Ни на каком ответе» — моя формулировка в запросе критику, в договоре её нет. Ответ api.telegram.org по TLS с вложенностью в сотню тысяч уровней — не сбой, который doctor обязан пережить; TypeError из настоящего urlopen не приходит — критик сам называет теорией. {"ok":"false"}: Bot API отдаёт ok булевым — замечание, не возврат.

Ответ Судьи по f48b97a: слияние main меняет только разрешение конфликта alarms.owner_questions (remerge-diff: потолок возвратов из main + флаг «отправлено» только при delivered из TK-092) — логика пп.1-5 не тронута, повторный проход критика не нужен.
