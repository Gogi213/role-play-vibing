# Раунд ревью 1, проход 2 · повторный поиск (TK-017, В-3/В-9)

Снимок: `origin/main` 085f98e (`git archive` во временный каталог ОС; основная копия не читалась, ничего не правилось). Что смотрели: файлы, изменённые с a8c7d76 (без `test_*`), плюс `tools/` целиком по оси сложности. Инструменты: `radon cc`, `vulture`, `pyflakes`, AST-проходы (длина функций, дубли тел, имена-двойники, граф импортов), чтение `dispatch.py`, `dguard/`, `tools/`. Порог Судьи (`docs/reviews/TK-006-2026-10-27.md`): файл > 900 строк, функция > 100 строк, radon E/F.

## Итог (5 строк)

1. **Мёртвый код — 6 находок, одна из них ошибка (E-003).** `dispatch.py` держит 104 реэкспорта; 32 имени не читает никто (ни dispatch, ни прод-код, ни тесты), ещё 20 — только тесты; pyflakes: 70 «imported but unused» в самом dispatch (из них 9 — обычные модули) и ещё ~30 в других файлах. `tools/bench/bench_dispatch.py:20` — `NameError: procs` (после выноса `procs.py`).
2. **Избыточность — 4.** Прокладки-обёртки в `dispatch.py` (4 однострочника над модулями), `load_agent` в трёх копиях, `ticket_state` в tools — копия.
3. **Сложность и длина — порог в `.claude/` чист; в `tools/` radon E/F: 9 функций** (6 из списка CEO + `bench/e2e.run_task` F54, `bench/e2e.parse_transcript` E31, `bench/judge_ab.render` F50). Файлов > 900 строк: 0 (макс. `dispatch.py` 840); функций > 100 строк: 0 (макс. `compare._e2e` 99). Ещё 29 функций radon D в `.claude/` — ниже порога.
4. **Дизайн — 4.** Циклов импортов нет (проверено графом, lazy-импорты — 13, все однонаправленные). Прокладки-«реэкспорт ради старых путей»: `dispatch.py` (15 блоков) и `delete_guard.py` (14 × `import *`); `legacy.py` — не прокладка (реальная страховка, 56 строк), оставить.
5. **Дублирование — 3** (плюс остатки отклонённого в проходе 1, списком ниже). Шаблоны ролей и `commands/` — дословных повторов > 90 знаков нет.

Всего в таблицах 17 строк. «Меняет поведение»: «да» — только E-003 (чинит падение).

## Ось 1 · Мёртвый код

| файл:строка | что | предложение | меняет поведение | цена |
|---|---|---|---|---|
| `tools/bench/bench_dispatch.py:20` | **ошибка E-003**: `procs.PID_EXPECT_NAME = …` — модуль `procs` не импортирован (имя осталось от кода до выноса `procs.py`, TK-011); при `BENCH_PID_NAME` — `NameError`. Воспроизведено: `BENCH_PID_NAME=x` + загрузка скрипта | `import procs` (или `D.procs`); проверка `tools/…` — запуск с `BENCH_PID_NAME` | да (перестанет падать) | мелко |
| `.claude/dispatcher/dispatch.py:48-52,123,233,258,311,333,468,580,615,643` | 104 реэкспорта «имена для тестов/вызовов». **32 имени не читает никто**: `FIXED_EFFORT_ROLES, _read_mcp_servers, _py, API_PAUSE, API_AUTH_PAUSE, _auth_error, LIMIT_LABEL_SLACK, MECH_KINDS, SIGNAL_SUMMARY_KINDS, _first_line, _ceo_handoff_during_run, JUDGE_GRACE, blocked_since, WAIT_NOTICE_EVERY, WAIT_EMPTY_AFTER, _review_returns, _review_pending, _done_marker, ACCEPT_PR, _last_accept, _executor_waits_after_accept, _model_usage_number, PID_CHECK_TRIES, PID_CHECK_TIMEOUT_S, _block_ticket, _stop_requests, _reset_loop_state, _apply_stop, IDLE_GRACE_S, INVARIANT_GRACE_S` и др. (`_ceo_file_write`, `_pid_state` есть в тестах, но `test_signals`/`test_start` берут их из своих модулей) | убрать из списков импорта | нет | мелко |
| `dispatch.py:48-…` (то же) | 20 имён читают только тесты: `PROMPT_TEMPLATE, tickets_cli, MAX_API_ERRORS, SUMMARY_EVERY_HOURS, classify_signal, flush_pending_summary, closed_without_review, MAX_REVIEW_RETURNS, _ceo_order_after_accept, _tokens_of, _context_tokens_sum, _model_usage_diff, _ps_field, _proc_state, _proc_comm, _pid_alive_posix, _role_logged, _find_pid_by_session, _ceo_handoff_pending, _no_move_reason` | перевести тесты на `import <модуль>`; убрать реэкспорт | нет (правка тестов) | средне |
| `dispatch.py:16,18,19,27,28,37,40,46,47` | неиспользуемые импорты: `json, re, shlex, Counter, dataclass, transcript, notify, ROUTES, REPORT` (+ `signal` затёрт локальным на 838) | удалить | нет | мелко |
| `dispatch.py:262-271` | `_transcript_last_turn_tokens`, `_transcript_usage_since`: обёртки, вызывают только тесты (боевой код ходит в `RL.*`) | убрать, тесты — на `runlog` | нет | мелко |
| `lifewatch.py:13 subprocess`, `registry.py:26 os`, `tickets.py:47 os`, `worktree_hygiene.py:9 subprocess`, `role_memory.py:24 env`; `tools/overseer_eval.py:47 re` + `:57` 26 из `overseer.*` (импорт «на всякий»), `tools/bench/common.py:99 resource`, `judge_ab.py:18 os`, `micro.py:10 sys`, `tasks.py:10,13 shutil, tempfile`, `orchestration.py:188 k`, `tools/bench/common.py:21 HOOKS` (только тест) | неиспользуемые импорты/переменные (pyflakes) | убрать | нет | мелко |

## Ось 2 · Избыточность

| файл:строка | что | предложение | меняет поведение | цена |
|---|---|---|---|---|
| `dispatch.py:203-210,238-239,372-373` | однострочные обёртки над модулем с подстановкой `_where()`/`check_wait_for`/`RUN_TIMEOUT` (`check_wait_for`, `record_job_outcome`, `note_wait_met`, `decide`, `build_prompt`) | оставить только те, что подменяют тесты; остальные — прямой вызов | нет | мелко/средне |
| `.claude/dispatcher/overseer.py:412`, `tools/helper_eval.py:60`, `report.py:166 _agent_def` | три копии «`agents/<имя>.md` → определение для `--agents`»: все три — обёртка над `haiku_aux.agent_meta` + одинаковый разбор `tools`/`model`/`effort` | одна `haiku_aux.agent_def(name, required=…)` | нет | мелко |
| `tools/e2e_clean_project.py:57` и `tools/bench/common.py:202` | `ticket_state` — одна и та же функция в двух файлах (regex по `status:` и `[итог:`) | оставить в `bench/common.py`, второй импортирует | нет | мелко |
| `dispatch.py:643-644`, `alarms.py` | `STALLED, _stall_title, _guarded, _owner_questions` импортируются в dispatch без `# имена для …` — читаются только `tick`; импорт `from alarms import … ; from alarms import … as` двумя строками | одна строка | нет | мелко |

## Ось 3 · Сложность и длина

Файлов > 900: **0** (`dispatch.py` 840, `watch.py` 827, `tickets.py` 803, `overseer.py` 719). Функций > 100: **0** (`compare._e2e` 99, `e2e.run_task` 96). radon E/F: **9, все в `tools/`** (в `.claude/` ниже порога; D — 29 в `.claude/`, 4 в `tools/`).

| файл:строка | что | предложение | меняет поведение | цена |
|---|---|---|---|---|
| `tools/bench/compare.py:204 _e2e` F(67), `:76 _micro` F(60) | 99 и 84 строки; сравнение двух прогонов в одной функции | метрики — таблицей «имя → как считать», общий цикл вывода | нет | средне |
| `tools/bench/e2e.py:224 run_task` F(54), `:68 parse_transcript` E(31) | запуск задачи, проверки и подсчёт в одной функции; разбор транскрипта с ветвями | выделить проверку задачи и подсчёт событий; типы событий — словарём | нет | средне |
| `tools/bench/judge_ab.py:327 render` F(50), `:244 run_case` D(27) | вывод таблицы A/B одной функцией | колонки списком | нет | средне |
| `tools/helper_eval.py:111 grade_secretary` F(46), `:418 cmd_report` E(37), `:360 cmd_pair` D(24) | оценка цепочкой условий; отчёт — агрегат и вывод в одной функции | критерии — список проверок, отчёт: агрегат отдельно от печати | нет | средне |
| `tools/overseer_eval.py:354 cmd_report` F(48), `:252 cmd_plan` E(35) | то же: агрегат и печать вместе; план — много ветвей | разделить агрегат и печать | нет | средне |
| `.claude/hooks/dguard/{cmd_scan.const_str 30, policy.protected_match 29, cmd_scan.find_scan 28, git_scan.push_publication 27, checks.check 26, lexer.find_close 27}`, `tickets.cmd_new 30` | radon D (21–30) в боевом коде — ниже порога, к сведению | не трогать, пока порог не снижен | — | — |

## Ось 4 · Дизайн

| файл:строка | что | предложение | меняет поведение | цена |
|---|---|---|---|---|
| `dispatch.py` целиком (15 блоков `from … import (… # noqa: F401 — … имена для тестов)`) | `dispatch.py` после разбиения — фасад: 15 блоков реэкспорта и обёртки; `tickets.py`, `watch.py`, `tickets_cli.py` и тесты ходят в `D.<имя>` за вещами из других модулей | чтобы не плодить «старые пути»: вызывающие — прямо в модуль-хозяин, фасад оставить только для `main/tick/launch_run` | нет (рефакторинг импортов) | средне/дорого |
| `.claude/hooks/delete_guard.py:61-76` | 14 × `from dguard.X import *` ради тестов и входа хука; `__all__` нигде нет — публичность определяется выбором, что звёздочка притянет | тесты — на `dguard.*`; в `delete_guard.py` только `main`/`check_tool` | нет | средне |
| `.claude/dispatcher/cfg.py` (232) и `config.py` (63) | два модуля с почти одинаковыми именами: `cfg` — правка `rpv.json` командой, `config` — пути проекта; путает читателя и `grep` | `cfg` → `rpv_config`/`settings`; `config` → `paths` | нет (переименование; тесты и 4 импорта) | мелко/средне |
| `tools/bench/bench_dispatch.py:15-24` | правит атрибуты `D.POLL_INTERVAL`, `D.ROLE_MODEL`, `procs.PID_EXPECT_NAME` на фасаде — сломалось при выносе (E-003): запись в атрибут реэкспорта молча не доходит до хозяина | после чистки реэкспортов правки — на модуле-хозяине | да (вместе с E-003) | мелко |

Циклы импортов: **нет** (граф по всем `import`, включая lazy). `legacy.py` — не прокладка, не трогать.

## Ось 5 · Дублирование

| файл:строка | что | предложение | меняет поведение | цена |
|---|---|---|---|---|
| см. ось 2: `load_agent`/`_agent_def` ×3, `ticket_state` ×2 | — | — | нет | мелко |
| `overseer.py:432`, `report.py:180`, `tools/helper_eval.py` (`claude -p --agents … --agent X --tools ""`) | запуск `claude -p` с одним помощником и без инструментов — три сборки командной строки с разными наборами флагов (`--max-budget-usd`, `--setting-sources`, `--strict-mcp-config`) | один `haiku_aux.build_cmd(agent, flags)`; в проходе 1 слияние `ask_reporter` отклонено («параметризация пятью флагами») — теперь трёх копий | нет | средне |
| `tools/helper_eval.py` ↔ `tools/overseer_eval.py` | одна схема «план → прогон → оценка → отчёт» (`cmd_plan/cmd_run/cmd_report`, `norm`, подсчёт интервалов) в двух файлах по 450–500 строк | общий `tools/eval_common.py` (`norm`, печать таблиц, агрегат) | нет | средне/дорого |

**Ноль находок:** шаблоны ролей и `commands/` (дословных повторов > 90 знаков нет, проверено по строкам); дубли тел функций в `.claude/` и `tools/` (AST-хэш, функции ≥ 6 строк) — точных нет; циклы импортов — нет; недостижимый код и константные `if` — не искали заново (проход 1: нет).

## Отклонённое в проходе 1 (не находить заново)

- `delete_guard`: `isinstance(data, dict)` и `GUARDED_TOOLS` проверяются дважды (`main` и `check_tool`) — отказ с причиной (TK-009, 06:16): `main` проверяет до `configure`/`has_roles`, `check_tool` — публичная точка для тестов.
- `ask_reporter` → общий запуск `claude -p` в `haiku_aux` — отказ (TK-009): параметризация пятью флагами дороже выигрыша. (См. ось 5: уже три копии — пересмотр на усмотрение Судьи.)
- `_popen` (обёртка «для тестов») — отказ (TK-009): 69 упоминаний в 6 тестовых файлах.
- Env только из тестов (`test_start.py`, `test_supervise.py`) — довод принят (TK-009); `RPV_BENCH_HOME` — опция, не мёртвое.
- 57 × `except …: pass` — «для разбора, не для удаления»; намеренные (`role_memory.py:13`).
