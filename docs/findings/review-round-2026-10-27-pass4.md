# Раунд ревью 1, проход 4 · проверка до чистоты (TK-024, В-3/В-9)

Снимок: `origin/main` 8495ba9 (`git archive` во временный каталог ОС; основная копия не читалась, ничего не правилось). Что смотрели: файлы, изменённые с 7112a9f (`git diff --name-only 7112a9f origin/main` без `test_*`: `ci_core`, `ci_watch`, `dispatch`, `errors`, `haiku_aux`, `merge_rule`, `overseer`, `ticket`, `tickets`, `watch`, `docs/configuration.md`, `docs/overseer.md`, `tools/bench/{bench_dispatch,common,tasks}`, `tools/{e2e_clean_project,eval_common,helper_eval,overseer_eval}`) — и, как быстрая проверка порога, все `*.py` без `test_*`/`conftest` в `.claude/` и `tools/`. Инструменты: `radon cc`, `vulture` (≥ 60), `pyflakes`, AST-проходы (длина функций и файлов, «определено, но не читается», одноимённые функции в разных модулях, дубли тел функций ≥ 6 строк, окна по 8 одинаковых строк, граф импортов, в том числе lazy), grep по `dispatch.<имя>`, `D.<имя>`, `cfg`/`config`. Порог Судьи (`docs/reviews/TK-006-2026-10-27.md`): файл > 900 строк, функция > 100 строк, radon E/F.

Все 7 находок прохода 3 исправлены (PR #133) и перепроверены: `ROLE_EFFORT` и `_context_tokens_last` в `dispatch.py` нет, `Task.tags` нет, четыре списка статусов сведены в `ticket.py`, `find_bash`/`init_script` — одна копия в `bench/common.py`, `bench.clean_env` строится на `haiku_aux.clean_env`.

## Итог (5 строк)

1. **Мёртвый код — 2 находки, мелкие.** Два устаревших упоминания в docstring-ах (`project.py:53`, `alarms.py:3`) на имена, которых в указанном месте больше нет. pyflakes: только намеренный `bench/common.py:101 resource`; vulture ≥ 60: только поля `ctypes` и `HOOKS` (отклонены раньше).
2. **Избыточность — 0.**
3. **Сложность и длина — 0 по порогу.** radon E/F: 0; файлов > 900: 0 (макс. `watch.py` 827); функций > 100: 0 (макс. `dispatch.launch_run` 83).
4. **Дизайн — 0.** `D.<имя>` вне `dispatch.py` — 0; `import *` — 0; циклов на загрузке нет (одна петля `role_memory ↔ role_context` — только через lazy-импорт внутри функций, как в проходе 3).
5. **Дублирование — 2, мелкие.** `run_jsons` (слово в слово, `e2e_clean_project.py:38` и `bench/e2e.py:30`); `load_agent` (`overseer.py:411` и `helper_eval.py:60` — одно тело).

Всего в таблицах 4 строки; «меняет поведение» — ни одной. Проход не чист: по В-3 нужна правка и проход 5.

## Ось 1 · Мёртвый код

| файл:строка | что | предложение | меняет поведение | цена |
|---|---|---|---|---|
| `.claude/dispatcher/project.py:53` | docstring «ставит `dispatch.configure_project`»: в `dispatch.py` такого имени нет; живут `project_paths.configure_project` (`:21`) и свой `watch.configure_project` (`:59`) | поправить ссылку на `project_paths.configure_project` | нет | мелко |
| `.claude/dispatcher/alarms.py:3` | docstring «пути проекта — `config`»: модуль переименован в `project_paths` (проход 3 поправил другие ссылки, эту пропустил) | `project_paths` | нет | мелко |

Проверено и пусто: `pyflakes` — 1 намеренное (`resource`); `vulture` ≥ 60 — поля `ctypes` (`hide.py:24-25`, `project.py:134`) и `HOOKS` (отклонено в проходе 2/TK-019); AST «определено на верхнем уровне и нигде не читается» — только `HOOKS`; остальные `dispatch.<имя>` в коде и `docs/` указывают на существующие имена (`POLL_INTERVAL`, `RUN_TIMEOUT`, `IDLE_SLO_MIN`, `decide`, `launch_run`, `_popen`, `append_ceo_inbox` — последнее через реэкспорт `signals`).

## Ось 2 · Избыточность

Находок нет. Проверено: списки статусов тикета — одна копия в `ticket.py` (проход 3 исправил); `eval_common.py` — 39 строк, 4 функции (radon A), все читаются; обёртки `dispatch.py` — оставлены Судьёй (TK-020).

## Ось 3 · Сложность и длина

Порог: файлов > 900 — **0**; функций > 100 — **0**; radon E/F — **0**. Находок по порогу нет.

Ниже порога, к сведению: самые длинные файлы `watch.py` 827, `tickets.py` 805, `dispatch.py` 796, `overseer.py` 711, `errors.py` 690; самые длинные функции `dispatch.launch_run` 83, `role_context.full_context` 76, `e2e_clean_project.main` 74, `bench/micro.guard_cases` 74, `tickets.cmd_new` 68; radon D — 38 (как в проходе 3). Порог не снижали — не трогать, пока Судья не снизит.

## Ось 4 · Дизайн

Находок нет. `D.<имя>` вне `dispatch.py` — 0; `import *` — 0; старых имён модулей `cfg`/`config` в боевом коде (кроме собственных `dguard/config.py` и локальных переменных `cfg`) нет; циклы — см. итог.

## Ось 5 · Дублирование

| файл:строка | что | предложение | меняет поведение | цена |
|---|---|---|---|---|
| `tools/e2e_clean_project.py:38` и `tools/bench/e2e.py:30` | `run_jsons(runs_dir)` — одна и та же строка-выражение (итоги запусков без `*.mcp.json`) | одна копия в `bench/common.py` (или `bench/e2e.py`); `e2e_clean_project` импортирует | нет | мелко |
| `.claude/dispatcher/overseer.py:411` и `tools/helper_eval.py:60` | `load_agent`: тело — `HA.agent_def(ROOT/"agents"/f"{name}.md", with_tools=True)` плюс `except ValueError → SystemExit`, слово в слово (у `overseer` значение по умолчанию `"overseer"`) | `haiku_aux.load_agent_or_exit(name)` (или `agent_def(..., exit_on_error=True)`); оба вызывают | нет | мелко |

Ноль находок: `clean_env` — теперь делегирование; `find_bash`/`init_script` — одна копия; тела функций ≥ 6 строк (AST-хэш) — точных дублей нет; окон из 8 одинаковых строк между файлами — только шапки импортов (`doctor.py`↔`supervise.py`, `judge_ab.py`↔`helper_eval.py`) — не находка.

## Отклонённое в проходах 1–3 (не находить заново)

- `delete_guard`: двойные `isinstance(data, dict)`/`GUARDED_TOOLS` (TK-009); `_popen` — обёртка для тестов (TK-009); env только из тестов, `RPV_BENCH_HOME` (TK-009).
- 57 × `except …: pass` — намеренные (`role_memory.py:13`).
- `bench/common.py:101 import resource` — намеренный; `common.HOOKS` — тестовая константа (TK-019).
- Обёртки `check_wait_for`/`decide`/`build_prompt` в `dispatch.py` и блоки реэкспорта `noqa F401` — тесты подменяют `D.<имя>` (TK-020).
- `bench_dispatch` правит атрибуты фасада `D` — менять нечего (TK-019).
- `load_state`/`save_state` ×4 (`ci_core`, `overseer`, `project_paths`, `hooks/role_memory`), `_git` ×3, `_now` ×2 — разные контракты (проход 3).
- Одноимённые функции с разными телами — не дубли: `_tail` (`errors`/`on_met`), `_norm` (`errors`/`start`), `_load`, `_str_list`, `norm`, `sh` — разные контракты.
- `cmd_regrade`/`cmd_report` в `helper_eval` ↔ `overseer_eval`, `run_case`/`_misses` в `helper_eval` ↔ `judge_ab` — формы строк и метрики разные; общее уже вынесено в `eval_common` (TK-021).
- `_dispatcher_dir` в `overseer.py:448` и `report.py:95` — две строки `root/.claude/dispatcher`; ниже порога внимания.
