# Команда ИИ-агентов: рынок, лучшие практики и аудит role-play-vibing (октябрь 2026)

Готовой альтернативы, которая закрывает все 17 критериев, на рынке нет. role-play-vibing (RPV) сделан на верной основе: тикеты лежат в файлах, роли запускаются короткими сессиями, есть отдельный Судья. Бизнес-задачу он не выполняет по двум причинам. Первая: решения владельца и уже исправленные ошибки нигде не хранятся как отдельные данные. Они живут в чате долгой CEO-сессии или вшиваются правкой плагина. Вторая: инструмент сам стал главным проектом, за 5 дней вышло больше 20 версий. Рекомендация: заморозить механику, убрать CEO как «память» и посредника, добавить реестр решений и реестр ошибок, которые агенты могут только читать, и дать Судье право пропускать числа только с источником. Запуск по расписанию и событиям постепенно перенести на готовые примитивы Anthropic (Routines / Managed Agents), а не наращивать свои.

## TL;DR

- **Рынок.** Индустрия сошлась на схеме «один пишущий поток плюс помощники с чистым контекстом: проверяющий, исследователи». Ролевые «рои» на равных её не выиграли. Готовые продукты закрывают механику (расписание, память, проверку по критериям, бюджет), но не управление владельцем: реестр решений, честность сроков, «вопросы только по 4 поводам».
- **RPV.** Из 17 критериев полностью не закрыт ни один, частично закрыто 11, не закрыто 6 (2, 3, 6, 7, 11, 16). Корневые причины: решения не хранятся как данные; CEO — долгая LLM-сессия, которая служит одновременно памятью, маршрутизатором и переводчиком; механика разрослась до табло, шины, CI-сторожа, присмотра ОС, выпусков и Haiku-пересказов.
- **Что делать.** (1) Заморозить новые функции инструмента. (2) Завести `decisions.md` и `errors.md`: правит только владелец, хук вставляет их в каждую сессию, у каждой ошибки есть проверка-регрессия. (3) Судья принимает только числа со ссылкой на артефакт и сам перезапускает проверку. (4) Измерять время «цель → принятый результат». (5) Вырезать табло, шину и пересказы, если их можно заменить одним ежедневным сообщением и тревогой.

## Рынок: кто что делает и что закрывает

**Официальные возможности Anthropic — главный ориентир, потому что их не нужно сопровождать самому.**

- *Subagents* (подагенты внутри сессии) и *Agent Teams*. Agent Teams — экспериментальная функция Claude Code с версии 2.1.32, выключена по умолчанию, включается переменной `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`. Ведущая сессия координирует 2–16 участников, у каждого свой контекст, общий список задач и прямые сообщения. Ограничения: одна команда на сессию, вложенных команд нет. Официальная документация Claude Code («Manage costs effectively») пишет, что команды агентов тратят «approximately 7x more tokens than standard sessions when teammates run in plan mode», потому что у каждого участника свой контекст; оценки в 3–4 раза — из сторонних обзоров. Годится для коротких параллельных рывков.\[1\] Не закрывает память между сессиями, круглосуточность и решения владельца.
- *Routines*. Сохранённая конфигурация Claude Code: промпт, репозитории, коннекторы. Работает в облаке Anthropic и запускается по расписанию, HTTP-вызову или событию GitHub.\[2\] Статус — research preview. Для облачной рутины нижняя граница интервала — час (у локальной задачи Desktop — минута).\[3\] Закрывает критерий 10 и частично 12: конец задания на сервере дергает API-триггер.
- *Claude Managed Agents* (бета). Облачная обвязка: сессии, хранилища памяти (memory stores), хранилище ключей (vaults), запуск по cron, бюджет на прогон.\[4\]\[5\]\[6\] Сюда же входят *outcomes* — рубрика успеха, по которой работу оценивает отдельный проверяющий «в собственном контексте, чтобы рассуждения агента на него не влияли», а также мультиагентная оркестрация с трассировкой каждого шага в консоли, webhooks и *dreaming* — плановый разбор прошлых сессий ради поиска повторяющихся ошибок. По данным Anthropic, outcomes подняли успешность задач «до 10 пунктов» по сравнению с обычным циклом промптов.\[7\] Ограничение: на старте хранилища памяти не работали с self-hosted песочницами, а это мешает, если агенты должны ходить на свои серверы.\[8\]
- Справочная статья Anthropic «Building effective agent automations» (8 октября 2026) даёт шесть правил, и все они прямо ложатся на критерии пользователя. Читать источник от закладки, а не за фиксированное окно. Сбой чтения не выдавать за «тихий день». Перепроверять каждый пункт перед отправкой. Считать отправку состоявшейся только после подтверждения. Перечитывать предпочтения владельца на каждом прогоне из хранилища, которое агент не может править. Ограничивать права и расходы на прогон. Предпочтения хранятся отдельно от состояния агента: «Never write here». Анти-паттерн, названный прямо: копия предпочтений, вшитая в промпт, «продолжает применять правила, которые вы уже изменили».\[6\]

**Сообщество вокруг Claude Code.**

- *Ruflo* (бывший claude-flow): «рои» с «королевой», консенсусом Raft/Byzantine/Gossip и векторной памятью. На странице GitHub ruvnet/ruflo — 74,5 тыс. звёзд и 8,9 тыс. форков, README заявляет «Ecosystem npm downloads: 12.52M over 12 months through October 4, 2026»; релиз v3.52.0 вышел 5 октября 2026. Заявления о 84.8% на SWE-bench и «75% экономии API» исходят от проекта и лояльных обзоров, независимой проверки я не нашёл.\[9\]\[10\] Есть публичные признаки трения: в issue #1476 Claude игнорирует навязанные правила роя («~60% claude-flow boilerplate»).\[11\] Релиз v3.51.1 исправлял ошибку, из-за которой «запрещённые» команды фактически не блокировались.\[12\] Подход противоположен вашим критериям 16 и 14.
- *Claude Squad*, *ccpm*, *Mission Control*: менеджеры параллельных сессий на git worktree.\[10\] Решают критерий 13, остальные не трогают.

**Не-Claude фреймворки.** *LangGraph* даёт то, чего не хватает большинству: контрольную точку состояния на каждом шаге, `interrupt()` для паузы до решения человека с возобновлением через часы или дни и режимы надёжности записи.\[13\]\[14\] Это эталон для критериев 14 и 15, но это библиотека, а не готовая команда. CrewAI, AutoGen/AG2, MetaGPT и ChatDev строят команды из ролей. Именно на таких системах (MetaGPT, ChatDev и др.) снималась таксономия отказов MAST. *Devin* (Cognition) — коммерческий агент: менеджер-Devin нарезает задачу дочерним Devin, а связка кодер/ревьюер с раздельным контекстом встроена в продукт.\[15\]\[16\] Подробных первичных данных по OpenAI Agents SDK/Codex, OpenHands, Factory и Sweep в этой работе я не собрал, поэтому сравнивать их не берусь.

**Итог по рынку.** Механику (критерии 10, 12, 14, 15, 17) лучше всех закрывают Managed Agents + Routines. Слой «владельца» (критерии 1, 4, 5, 6, 7) не закрывает никто: его придётся строить самому — тонким слоем файлов и правил, а не сервисами.

## Лучшие практики: что подтверждено

**Мультиагентность окупается только на параллельном чтении.** Многоагентная исследовательская система Anthropic (ведущий Opus 4, подагенты Sonnet 4) обошла одиночный Opus 4 на 90.2% на внутренней оценке, но, по статье Anthropic Engineering «How we built our multi-agent research system» (13 июня 2025), «agents typically use about 4× more tokens than chat interactions, and multi-agent systems use about 15× more tokens than chats». Сами авторы пишут, что объём токенов объясняет 80% разброса качества.\[17\] Плохо подходят задачи, где агентам нужен общий контекст и много зависимостей между ними.\[18\]

**Писать должен один поток.** Cognition в 2025 году писала «Don't Build Multi-Agents»: действия несут неявные решения, а параллельные писатели принимают противоречащие друг другу решения.\[19\] Walden Yan в посте Cognition «Multi-Agents: What's Actually Working» от 22.04.2026 уточнил: работает «a narrower class of patterns … setups where multiple agents contribute intelligence to a task while writes stay single-threaded». Неструктурированные рои — «mostly a distraction». Рабочая форма: менеджер делит работу, исполнители делают, менеджер сводит.\[20\]

**Независимая проверка — с чистым контекстом.** По тому же посту Cognition, «even on PRs written by Devin, Devin Review catches an average of 2 bugs per PR, of which roughly 58% are severe (logic errors, missing edge cases, security vulnerabilities)» — и ревью-агент работает лучше всего без общего с кодером контекста. Важна и обратная связь: кодер должен фильтровать замечания с учётом решений пользователя — Cognition прямо называет это способом «preventing looping, disobeying the user, doing work that is out of scope». Это прямо ваш риск «чиню → ломаю → чиню». Anthropic в работе о долгих обвязках отмечает, что агенты «reliably skew positive when grading their own work».\[21\]

**Отказы — от устройства системы, а не от модели.** MAST (UC Berkeley): 14 видов отказов на 1 642 размеченных прогонах 7 фреймворков. Доли: спецификация и дизайн 41.8%, рассогласование агентов 36.9%, проверка 21.3%.\[22\]\[23\] Встроенные проверяющие часто проверяют поверхностно.\[24\]

**Ролевая метафора не добавляет знаний.** Wharton (Prompting Science Report 4, декабрь 2025): экспертные персоны («ты эксперт-физик») не повышают точность на сложных фактических вопросах.\[25\]\[26\] Zheng et al. (EMNLP 2024, 162 персоны, 2 410 вопросов) приходят к тому же выводу.\[27\]\[28\] Делить работу стоит по контексту и правам: читает / пишет / проверяет. Должности вроде «CEO/инженер» для этого не нужны.

**Долгие задачи держатся на файлах.** Anthropic в «Effective harnesses for long-running agents» описывает такую схему: список требований в JSON, `claude-progress.txt`, git-история, одна фича за сессию, end-to-end проверка перед отметкой «готово». Состояние живёт в артефактах, а не в контексте.\[29\]\[30\]

## Аудит role-play-vibing

**Что это по факту (README).** Владелец говорит с CEO — долгой сессией в Claude Desktop. CEO заводит тикеты `.claude/tickets/TK-NNN.md`.\[31\] `dispatch.py` раз в 15 с читает тикеты и запускает роль (`claude -p`, таймаут 1200 с, до 3 параллельно). Роль сдаёт шаг через `tickets.py result … --why`. Кого будить следующим, решает `routes.py`. Судья (по умолчанию `claude-opus-5-5`, effort `xhigh`) принимает работу по договору «примут, если …».\[31\] `watch.py` ищет сирот и простои. Поверх этого есть необязательная шина HTTP+sqlite, веб-табло «Диспетчерская» с пересказом шагов через Haiku, `ci_watch`, присмотр ОС (`supervise.py`), `release.py` для выпуска и отката, `/rpv-doctor`, хук-страж удаления и счётчик простоя со SLO 10 мин/сутки. Правила устава: «Новые задачи — только по слову владельца», «Числа владельцу — после Судьи; оценка ≠ замер», «Судью не снимает CEO».\[31\]

**Частота правок инструмента (критерий 16).** На main 426 коммитов.\[31\] PR с номерами от #1 до #89 влиты с 6 по 10 октября 2026.\[32\]\[33\] Только по заголовкам PR подтверждены 22 разные версии за 6–10 октября: от 1.4.2 до 1.8.29. PR #89 уже выпускает 1.8.35.\[32\]\[34\] Отдельные тикеты заведены на вырезание лишнего: TK-100 удалил TUI и тестовые страницы (−2 235 строк, PR #65), мёртвый код (PR #74), старые переменные `ALPHA_*` (PR #88).\[35\]\[36\]\[37\] Это и есть критерий 16 в провале: механика правится десятки раз в неделю.

**Дефекты самой механики из PR — то, что владелец видит как «команда стоит».**
- `ci_watch` 9 раз получил HTTP 500 и «молчал 2 ч», принятый PR стал конфликтным (PR #36).\[38\]\[39\]
- Тесты на живой машине писали фикстуры в живую шину и очередь CEO (PR #45).\[40\]
- Роль-сирота появлялась после убийства диспетчера примерно в 3% раундов стенда на macOS (PR #18).\[41\]
- `host:calc:file:/x` с неабсолютным путём «молча ждал вечно» (PR #79).\[42\]
- Ложная «смерть» служб (PR #78), ложные блоки сторожа (PR #64).\[43\]\[44\]
- Без `RPV_BUS_URL` в лог при каждом старте шла ложная тревога `NameError` (PR #1).\[33\]
Каждый дефект закрыт, но каждый появился из-за механики, которой могло не быть.

**Покрытие 17 критериев**

| № | Критерий | Статус | Что в репозитории это вызывает |
|---|---|---|---|
| 1 | Движение к цели | Частично | Правило «новые задачи — только по слову владельца» есть, но это текст в уставе. Принудительной ссылки тикета на цель или решение не видно. Цикл «Судья вернул → исправили → вернул» ограничен только «тормозами циклов» диспетчера.\[31\] |
| 2 | Только проверенный результат | Нет | Судья есть, и он реально принимает в чистом окружении (PR #5, #7).\[45\]\[46\] Но ему доступны только тикеты с `reviewer: judge`.\[31\] Отчёт владельцу пишет CEO — LLM, которая пересказывает. Табло показывает Haiku-пересказ шагов и «сделано N из M» (PR #3), то есть число задач, а не принятые результаты.\[47\] |
| 3 | Скорость измеряется | Нет | Измеряется только простой (`downtime`). Времени «цель → принятый результат» нет.\[31\] |
| 4 | Честность сроков и чисел | Частично | «Оценка ≠ замер» записано в уставе.\[31\] При этом табло показывает «прогресс с оценкой срока» (PR #3).\[47\] Механизма, который сверяет названные сроки с фактом или требует у числа ссылку на артефакт, не найдено. |
| 5 | Без микроменеджмента | Частично | Есть статус `ask-owner` и `needs_owner`. Фильтра «только деньги / направление / необратимое / бизнес-правила» в коде не видно. При этом сторож сам будит владельца на сбоях механики: 3 сбоя GitHub подряд (PR #36), ожидание замка больше 20 мин (PR #8), нарушение инварианта (PR #15).\[39\]\[48\]\[49\] Это вопросы про механику, а не про бизнес. |
| 6 | Решения соблюдаются | Нет | Отдельного реестра решений не найдено. Решение владельца «CEO не снимает Судью» пришлось вшить в шаблон `ceo.md` выпуском 1.6.2 со словами владельца «Это должно быть в плагине» (PR #5).\[46\] Решение, сказанное в чате, живёт в контексте долгой CEO-сессии и теряется при сжатии или `/clear`. Против этого прямо предупреждает Anthropic.\[6\] |
| 7 | Ошибка не повторяется | Нет | Есть «конспекты сессий и блокнот роли», но реестра ошибок с проверкой-регрессией нет.\[31\] Исправление ошибки агента сейчас — это правка плагина и новый выпуск. |
| 8 | Механика невидима | Частично | Владелец формально говорит только с CEO. Но сигналы и тревоги называют механику («цикл-ожиданий», «нет-хода», «idle-slo», «КОМАНДА МОЛЧИТ»), а README требует знать `RPV_*`, шину, `wait_for`, `supervise`.\[31\]\[49\]\[50\] |
| 9 | Ход в одном месте | Частично | Есть веб-табло с телефона, тревога «КОМАНДА МОЛЧИТ» при молчании сводки больше 10 мин (PR #9) и шаблон ежедневного отчёта `templates/brief/`.\[31\]\[50\] Но мест несколько: табло, CEO-чат, `ceo-inbox.md`. Табло показывает механику, а не результат. |
| 10 | Работа без владельца | Частично | Диспетчер, присмотр ОС, пауза при лимите 429 с потолком в 1 ч (PR #15), SLO простоя. Минус: на Windows автозапуска после перезагрузки нет.\[31\]\[49\] Работа идёт только при включённом ПК. |
| 11 | Платить за результат | Нет (данных нет) | Учёт токенов есть, отчёта о доле «работа против механики» нет. Структурно дорого: Судья на Opus с `xhigh` на каждом тикете, Haiku-пересказы для табло, CEO-пробуждения, циклы возвратов.\[31\] |
| 12 | Долгие внешние задания | Частично | `wait_for: host:…:unit:` с InvocationID и событиями шины (PR #9) — сильная часть.\[50\] Но был баг «ждал вечно» (PR #79), а без шины остаётся ssh-опрос раз в 300 с.\[42\]\[50\] |
| 13 | Параллель без столкновений | Частично | Есть worktree на тикет и их уборка (PR #68), пределы по ролям.\[31\]\[51\] Но до 3 пишущих параллельно — это ровно то, от чего предостерегает Cognition.\[15\] Слияние идёт через PR и CI, конфликт попадает владельцу (PR #36).\[39\]\[51\] |
| 14 | Ничего необратимого без согласия | Частично | Страж удаления в `PreToolUse` ловит Bash, PowerShell, Write, Edit и др.\[31\] Денег и публикаций он не касается. Готовится автовлитие после зелёного CI и Судьи (PR #23: «Автовлитие — следующий PR»).\[52\] |
| 15 | Сбои не теряют работу | Частично | Тикеты, `state.json` и журналы лежат в файлах.\[31\] Гонки при убийстве диспетчера чинились по одной (PR #17, #18).\[41\]\[53\] Контекст CEO не восстанавливается. |
| 16 | Инструмент не стал проектом | Нет | Больше 20 версий за 5 дней, 89 PR, отдельные тикеты на вырезание лишнего. |
| 17 | Подключение к любому проекту | Частично | `/rpv-init` за минуты. Но нужны Python в PATH, ssh-алиасы, десятки `RPV_*`, на каждой ОС свой запуск.\[31\] Следы сращения с исходным проектом «alpha» вычищались только к 1.8.34 (PR #88, PR #3 «как у alpha»).\[36\]\[47\] |

**Корневые причины.**

1. **Решения и ошибки не являются данными.** Есть только тикеты и устав. Всё, что владелец сказал в чате, держит в контексте долгая CEO-сессия. Отсюда провал критериев 6 и 7 и необходимость повторять сказанное. Правильная форма описана у Anthropic: хранилище предпочтений только для чтения, которое перечитывается на каждом прогоне.\[6\]
2. **CEO — LLM-посредник.** Одна долгая сессия переводит цели в тикеты, решает, что сообщить владельцу, и служит памятью. Это нарушает собственный принцип RPV «одна задача = короткая сессия» и создаёт ровно те отказы MAST, которые составляют 41.8% (спецификация) и 36.9% (рассогласование).\[23\] Отчёт CEO — пересказ, а не принятый результат: здесь корень «вранья о цифрах».
3. **Ролевая рамка вместо разделения по правам.** Должности (CEO, Исследователь, Инженер) знаний не добавляют. При этом сразу несколько ролей пишут. Сильная часть RPV — Судья с чистым контекстом и обязательный договор приёмки.\[31\] Это совпадает с лучшей практикой, и её надо сохранить.
4. **Борьба со следствиями механикой.** Простои лечатся сторожем, сторож — присмотром, присмотр — doctor, сбои шины — запасным файлом. Каждый слой даёт свои дефекты (PR #1, #36, #45, #64, #78, #79) и свои тревоги владельцу. Получается замкнутый круг «инструмент чинит инструмент».

## Рекомендации (по отдаче на единицу механики)

1. **Заморозка.** Новые функции RPV — только по одному из 17 критериев, который без них проваливается. Правка инструмента — отдельный тикет с заявленной метрикой. Цель: не больше одного выпуска в неделю.
2. **`owner/decisions.md`.** Правит только владелец (запрет для агентов — тем же стражем `PreToolUse`), SessionStart-хук вставляет файл в каждую сессию каждой роли. Каждая запись: дата, решение, область. Судья отклоняет работу, если она противоречит записи. Закрывает критерий 6.
3. **`owner/errors.md` + регрессии.** Ошибку считают исправленной, только когда к записи приложен тест или проверочный скрипт, а Судья прогоняет весь набор при каждой приёмке. Закрывает критерий 7.
4. **Убрать CEO как память и маршрутизатор.** Владелец пишет цель в `goals/G-NNN.md`: цель, критерии приёмки, бюджет. Тикеты обязаны ссылаться на `G-` или запись в `decisions.md`, иначе `tickets.py new` откажет (критерий 1). Отчёт владельцу собирается программно из вердиктов Судьи, без LLM-пересказа (критерий 2).
5. **Правило чисел и сроков.** Число принимается только со ссылкой на артефакт: файл результата, лог прогона, URL. Срок — только как измеренное время прошлых похожих тикетов, иначе пишется «не знаю». Судья проверяет это механически (критерий 4). Время «цель → принятие» записывать в `goals/` (критерий 3).
6. **Один писатель.** `engineer:1` по умолчанию, параллель — только для чтения и исследования (критерий 13). По данным Cognition и Anthropic, это снижает конфликты без потери ценности.
7. **Резать механику.** Haiku-пересказы, TUI/MCP-табло, отдельную шину — заменить одним ежедневным сообщением и одной тревогой «стоит готовая работа больше N минут». По образцу Anthropic: сбой источника называется словами, а не «тихим днём» (критерии 8, 9, 11, 16).\[6\]
8. **Пилот переноса на примитивы Anthropic.** Для исследований без доступа к своим серверам — Managed Agents: outcomes вместо самописного Судьи, хранилище предпочтений только для чтения, бюджет на прогон, webhooks. Для кода — Routines с API-триггером от `jobrun.sh` по завершении задания (критерии 10, 12, 15, 17). Свой код остаётся тонким: файлы целей, решений и ошибок.

**Что придётся доделать в любом случае.** Фильтр вопросов «только 4 повода» (критерий 5) и сверку названных сроков с фактом (критерий 4) не даёт ни один продукт. Учёт доли токенов на механику (критерий 11) придётся собирать самому из логов прогонов.

## Оговорки

- Файлы `templates/roles/*.md`, `hooks/hooks.json`, `CHANGELOG.md` и `.claude/dispatcher/README.md` напрямую прочитать не удалось. Аудит опирается на README и описания влитых PR. Утверждение «реестра решений и ошибок нет» значит «не найден в доступных источниках». Как именно Судья проверяет (перезапускает ли тесты сам), подтверждено только косвенно.
- Счёт открытых issues и выпусков за 11–12 октября не проверен.
- Цифры Ruflo (SWE-bench, экономия) — заявления проекта.\[9\] Цифры Managed Agents («до 10 пунктов», Harvey ~6x, Wisedocs 50%) — данные Anthropic и её клиентов, без независимой проверки.\[7\]
- Подробных первичных данных по OpenAI Agents SDK/Codex, OpenHands, Factory, Sweep в этой работе нет. Их место в сравнении не оценено.

## Источники

1. [Claude Code Agent Teams: What It Does and Where It Stops](https://aidenapp.org/claude-code-agent-teams)
2. [Claude Code Routines: Schedule, API, and GitHub-Trigger Your AI Agents](https://aicatchup.com/news/claude-code-routines)
3. [Automate work with routines - Claude Code Docs](https://code.claude.com/docs/en/routines)
4. [Claude Managed Agents: get to production 10x faster](https://claude.com/resources/articles/claude-managed-agents)
5. [Claude Managed Agents: Scheduled Deployments Guide](https://www.getclaudeskills.com/blog/claude-managed-agents-scheduled-deployments)
6. [Building effective agent automations / claude.dev Blog](https://claude.dev/blog/building-effective-agent-automations/)
7. [New in Claude Managed Agents: dreaming, outcomes, and multiagent orchestration | Claude by Anthropic](https://claude.com/blog/new-in-claude-managed-agents)
8. [Running Claude Managed Agents on your own infrastructure — AI Codex](https://www.aicodex.to/articles/claude-managed-agents-self-hosted)
9. [Claude Flow (Ruflo): Complete Guide to Multi-Agent Orchestration for Claude Code](https://pasqualepillitteri.it/en/news/774/claude-flow-ruflo-multi-agent-orchestration-guide)
10. [Claude Multi-Agent: 6 Frameworks vs ClaudeFast Code Kit](https://claudefa.st/blog/tools/orchestrators/multi-agent-orchestrators)
11. [Claude refusing to use ROFLO/Claude-Flow framework · Issue #1476 · ruvnet/ruflo](https://github.com/ruvnet/ruflo/issues/1476)
12. [How to use ruflo. An OSS that turns Claude Code into an AI team: What happens when you install it? Two ways to install it and your first task｜オープンソース研究所](https://note.com/opensourcelab/n/n3d8553cf4f47?hl=en)
13. [Architecting Human-in-the-Loop Agents: Interrupts, Persistence, and State Management in LangGraph](https://medium.com/data-science-collective/architecting-human-in-the-loop-agents-interrupts-persistence-and-state-management-in-langgraph-fa36c9663d6f)
14. [Checkpointers - Docs by LangChain](https://docs.langchain.com/oss/python/langgraph/checkpointers)
15. [Multi-Agents: What's Actually Working](https://cognition.com/blog/multi-agents-working)
16. [Multi-Agents: What's Actually Working](https://x.com/walden_yan/article/2047054401341370639)
17. [Multi-Agent Orchestration: What Works and What Just Multiplies Your Problems](https://www.13labs.au/guides/multi-agent-orchestration-what-works)
18. [GenAI The Arguments for Single or Multi Agents: Deja Vu, All Over Again](https://medium.com/@jason.croucher/genai-the-arguments-for-single-or-multi-agents-deja-vu-all-over-again-35775b45032e)
19. [Multi-agent is a context-engineering decision, not a smarter architecture · Learn](https://valdemird.com/learn/agentic-systems/multi-agent/)
20. [Walden on X: "A year ago, I'd tell people to not build multi-agents and to focus on context engineering fundamentals Today, many sexy ideas are still impractical, but we've found some setups that actually work" / X](https://x.com/walden_yan/status/2047054554433462360)
21. [Engineering at Anthropic](https://anthropic.com/engineering/harness-design-long-running-apps)
22. [Why do multi agent LLM systems fail (and how to fix)- 2026 Guide](https://futureagi.substack.com/p/why-do-multi-agent-llm-systems-fail)
23. [MAST](https://sites.google.com/berkeley.edu/mast)
24. [Why Do Multi-Agent LLM Systems Fail?](https://thegrigorian.medium.com/why-do-multi-agent-llm-systems-fail-14dc34e0f3cb)
25. [Technical Report: Playing Pretend: Expert Personas Don't Improve Factual Accuracy - Wharton Generative AI Labs](https://gail.wharton.upenn.edu/research-and-insights/playing-pretend-expert-personas/)
26. [Prompting Science Report 4: Playing Pretend: Expert Personas Don't Improve Factual Accuracy](https://arxiv.org/pdf/2512.05858)
27. [mimeo: Compiling Public Expert Corpora into Agent Skills and Testing What Transfers](https://arxiv.org/pdf/2609.00453)
28. [When "A Helpful Assistant" Is Not Really Helpful: Personas in System Prompts Do Not Improve Performances of Large Language Models](https://www.alphaxiv.org/abs/2311.10054)
29. [Effective harnesses for long-running agents](https://daily.dev/posts/effective-harnesses-for-long-running-agents-7clzunmmu)
30. [Engineering at Anthropic](https://anthropic.com/engineering/effective-harnesses-for-long-running-agents)
31. [GitHub - Gogi213/role-play-vibing: Команда ИИ-ролей для Claude Code: CEO говорит с владельцем, Исследователь/Инженер/Судья работают по тикетам; диспетчер будит роли, сторож следит за простоями, хуки держат правила. Плагин для любого проекта. · GitHub](https://github.com/Gogi213/role-play-vibing)
32. [TK-171: RPV\_ROLE\_MCP — MCP-серверы ролей (1.8.35) by Gogi213 · Pull Request #89 · Gogi213/role-play-vibing](https://github.com/Gogi213/role-play-vibing/pull/89)
33. [1.4.2: шина без RPV\_BUS\_URL выключается молча (DEFAULT\_URL в busclient.py) by Gogi213 · Pull Request #1 · Gogi213/role-play-vibing](https://github.com/Gogi213/role-play-vibing/pull/1)
34. [TK-100 шаг 5: \_reconcile CC 42 → 18 (1.8.29) by Gogi213 · Pull Request #83 · Gogi213/role-play-vibing](https://github.com/Gogi213/role-play-vibing/pull/83)
35. [TK-100 №2/№3: удалить TUI board.py, sample-view2.json, page2.html (−2 235) by Gogi213 · Pull Request #65 · Gogi213/role-play-vibing](https://github.com/Gogi213/role-play-vibing/pull/65)
36. [TK-100 №28: убрать ALPHA\_\* (1.8.34) by Gogi213 · Pull Request #88 · Gogi213/role-play-vibing](https://github.com/Gogi213/role-play-vibing/pull/88)
37. [TK-100 №20: мёртвый код (1.8.19) by Gogi213 · Pull Request #74 · Gogi213/role-play-vibing](https://github.com/Gogi213/role-play-vibing/pull/74)
38. [TK-090 Д-1/Д-5: один список служб, снимок env без ключей settings.json by Gogi213 · Pull Request #52 · Gogi213/role-play-vibing](https://github.com/Gogi213/role-play-vibing/pull/52)
39. [merge\_rule/ci\_watch: сбой GitHub и конфликт — событие владельцу (TK-077) by Gogi213 · Pull Request #36 · Gogi213/role-play-vibing](https://github.com/Gogi213/role-play-vibing/pull/36)
40. [Тесты не видят живую настройку RPV\_\*/ALPHA\_\* (conftest) by Gogi213 · Pull Request #45 · Gogi213/role-play-vibing](https://github.com/Gogi213/role-play-vibing/pull/45)
41. [fix(dispatch): роль-сирота после kill диспетчера и потеря метки передачи CEO by Gogi213 · Pull Request #18 · Gogi213/role-play-vibing](https://github.com/Gogi213/role-play-vibing/pull/18)
42. [TK-117 К1.1: путь у host:/deck: только абсолютный (1.8.25) by Gogi213 · Pull Request #79 · Gogi213/role-play-vibing](https://github.com/Gogi213/role-play-vibing/pull/79)
43. [TK-111 (В-209): ложная смерть служб, pid-файл, проверка выпуска (1.8.22) by Gogi213 · Pull Request #78 · Gogi213/role-play-vibing](https://github.com/Gogi213/role-play-vibing/pull/78)
44. [Релиз 1.8.12: TK-101 сторож жизни без ложных блоков by Gogi213 · Pull Request #64 · Gogi213/role-play-vibing](https://github.com/Gogi213/role-play-vibing/pull/64)
45. [1.7.0: своя Диспетчерская в плагине — сервер, машины, строки Haiku, шаги из лога, лента, TUI, MCP by Gogi213 · Pull Request #7 · Gogi213/role-play-vibing](https://github.com/Gogi213/role-play-vibing/pull/7)
46. [1.6.2: CEO не снимает Судью by Gogi213 · Pull Request #5 · Gogi213/role-play-vibing](https://github.com/Gogi213/role-play-vibing/pull/5)
47. [1.6.0: табло с мака наполняется как у alpha (view2) by Gogi213 · Pull Request #3 · Gogi213/role-play-vibing](https://github.com/Gogi213/role-play-vibing/pull/3)
48. [Диспетчер без простоев: лимит 429, простой сервера, ложное событие юнита, waiting без условия by Gogi213 · Pull Request #8 · Gogi213/role-play-vibing](https://github.com/Gogi213/role-play-vibing/pull/8)
49. [TK-076 п.6/п.7: инвариант «есть ход», циклы ожиданий, потолок паузы 429 by Gogi213 · Pull Request #15 · Gogi213/role-play-vibing](https://github.com/Gogi213/role-play-vibing/pull/15)
50. [1.7.1: события конца задания с InvocationID, присмотр за диспетчером, плашка «команда молчит» by Gogi213 · Pull Request #9 · Gogi213/role-play-vibing](https://github.com/Gogi213/role-play-vibing/pull/9)
51. [TK-104: уборка worktree закрытых тикетов (тик диспетчера), doctor WARN на копии, тест by Gogi213 · Pull Request #68 · Gogi213/role-play-vibing](https://github.com/Gogi213/role-play-vibing/pull/68)
52. [feat(ci\_watch): события CI в шину, wait\_for ci: (TK-079 п.1) by Gogi213 · Pull Request #23 · Gogi213/role-play-vibing](https://github.com/Gogi213/role-play-vibing/pull/23)
53. [fix(dispatch): живость роли по pid и времени старта процесса by Gogi213 · Pull Request #17 · Gogi213/role-play-vibing](https://github.com/Gogi213/role-play-vibing/pull/17)
