# Раунд ревью 1, проход 3 · проверка до чистоты (TK-022, В-3/В-9)

Снимок: `origin/main` 23c35fe (`git archive` во временный каталог ОС; основная копия не читалась, ничего не правилось). Что смотрели: все `*.py` без `test_*`/`conftest` в `.claude/` и `tools/` (список изменённых с 085f98e файлов целиком входит в этот охват), плюс `docs/`, `agents/`, `commands/` на устаревшие ссылки. Инструменты: `radon cc` (E/F и D), `vulture` (≥ 60), `pyflakes`, AST-проходы (длина функций и файлов, «определено, но не читается в боевом коде», дубли тел функций ≥ 6 строк, одноимённые функции в разных модулях, окна по 8 одинаковых строк, граф импортов), grep по `dispatch.<имя>`, `cfg.py`, `config.py`. Порог Судьи (`docs/reviews/TK-006-2026-10-27.md`): файл > 900 строк, функция > 100 строк, radon E/F.

## Итог (5 строк)

1. **Мёртвый код — 4 находки, мелкие.** pyflakes: 1 неиспользуемый импорт в `.claude/` (`dispatch.py:38 ROLE_EFFORT`) и 1 намеренный (`bench/common.py:99 resource`, отказ с причиной). Обёртка `dispatch._context_tokens_last` не читается боевым кодом; поле `Task.tags` пишется и нигде не читается; 5 устаревших ссылок «`dispatch.<имя>`» на имена, переехавшие в другие модули.
2. **Избыточность — 1.** Четыре копии одного списка статусов тикета (три разных смысла: активные 4, открытые 6).
3. **Сложность и длина — 0 по порогу.** radon E/F: 0 (в `.claude/` и `tools/`); файлов > 900: 0 (макс. `watch.py` 827, `tickets.py` 805, `dispatch.py` 800); функций > 100: 0 (макс. `dispatch.launch_run` 83, в `tools/` — 74). Ниже порога: D — 30 в `.claude/`, 8 в `tools/`.
4. **Дизайн — 0 новых.** Фасад `dispatch.py` больше не нужен чужим модулям: `D.<имя>` в боевом коде вне `dispatch.py` — 0; `import *` в `delete_guard.py` нет; `cfg`/`config` переименованы, старых ссылок нет. Циклов импортов нет (по статическому графу один «цикл» `role_context ↔ role_memory` — обратная сторона lazy-импорта внутри функций, на загрузке не работает).
5. **Дублирование — 2.** `find_bash`/`init_script` в двух файлах `tools/` (копии разошлись); `clean_env` в `haiku_aux` и `bench/common` (почти слово в слово). Дублей тел функций ≥ 6 строк — 0; окон из 8 одинаковых строк между разными файлами — 0.

Всего в таблицах 7 строк, к ним 3 «в работе у другого тикета». «Меняет поведение»: «да» — одна (`find_bash`: версия в `bench/common.py` на Windows берёт заглушку WSL из System32; выравнивание по версии из `e2e_clean_project.py` это чинит).

## Ось 1 · Мёртвый код

| файл:строка | что | предложение | меняет поведение | цена |
|---|---|---|---|---|
| `.claude/dispatcher/dispatch.py:38-39` | `ROLE_EFFORT` импортирован из `role_args` и не читается в `dispatch.py` (pyflakes). Снаружи его берут тесты (`D.ROLE_EFFORT`, 3 места) и `tools/bench/bench_dispatch.py` (правит словарь на месте). Судья в TK-020 записал «перевести в TK-021», в тикете TK-021 этого нет | тесты и `bench_dispatch` — на `role_args.ROLE_EFFORT`; убрать из импорта | нет | мелко |
| `.claude/dispatcher/dispatch.py:233-234` (+ комментарий `:164`) | `_context_tokens_last` — обёртка над `RL.context_tokens_last(result, CLAUDE_PROJECTS_DIR)`; боевой код не зовёт (AST: 0 чтений вне тестов), читают её 8 раз `test_dispatch.py:3596-3645` | тесты — на `runlog.context_tokens_last(result, D.CLAUDE_PROJECTS_DIR)`; убрать обёртку, поправить комментарий `:164` | нет | мелко |
| `tools/bench/tasks.py:33` | поле `tags: tuple` у задачи: заполнено в 8 задачах (`:56…:306`), нигде не читается (grep по `tools/`: ни `.tags`, ни `getattr`) | убрать поле и аргументы `tags=` | нет | мелко |
| `.claude/dispatcher/overseer.py:3`, `docs/configuration.md:126`, `docs/overseer.md:81` → `dispatch.OVERSEER_EVERY_S`; `.claude/dispatcher/watch.py:137` → `dispatch.WAIT_RECON_S`; `.claude/dispatcher/errors.py:61` → `dispatch._on_met_argv` | устаревшие ссылки после выноса: `OVERSEER_EVERY_S` теперь в `alarms.py:111`, `WAIT_RECON_S` — в `waits.py:105`, `_on_met_argv` — в `on_met.py` (в `dispatch.py` этих имён нет). `docs/configuration.md` — справочник владельца: ссылка на несуществующее имя | `alarms.OVERSEER_EVERY_S`, `waits.WAIT_RECON_S`, `on_met._on_met_argv` | нет | мелко |

Проверено и пусто: `vulture` ≥ 60 в `.claude/`+`tools/` — кроме полей `ctypes` (`hide.py:24-25`, `project.py:134` — структуры WinAPI, нужны) и `bench/common.py:21 HOOKS` (читают 2 теста, отклонено в проходе 2); «определено на верхнем уровне и не читается ни в боевом коде, ни в других модулях» — кроме `HOOKS` и `_context_tokens_last` пусто; неиспользуемые импорты в `tools/` — только намеренный `resource`.

## Ось 2 · Избыточность

| файл:строка | что | предложение | меняет поведение | цена |
|---|---|---|---|---|
| `ci_core.py:20` `ACTIVE` (4 статуса), `tickets.py:175` (тот же кортеж литералом), `overseer.py:55` `OPEN` (6), `tools/helper_eval.py:36` `OPEN_STATUSES` (те же 6) | один список статусов «тикет в работе / открыт» записан четыре раза; в `ticket.py` есть только `CLOSED_STATUSES` (`:223`) — парные «активные» и «открытые» там не заведены; `tools/bench/e2e.py:22 TERMINAL` и `:58` — ещё одна форма (закрыт/остановлен) | `ticket.ACTIVE_STATUSES`, `ticket.OPEN_STATUSES` рядом с `CLOSED_STATUSES`; четыре места берут оттуда | нет | мелко |

Не находки (проверено): `load_state`/`save_state` ×4 (`ci_core`, `overseer`, `project_paths`, `hooks/role_memory`) — разные файлы состояния и контракты (хук не может импортировать диспетчер); `_git` ×3 (`errors`, `release`, `worktree_hygiene`) — разные контракты (`None` / код 124 / исключение); `_now` ×2 — разные (с `--at` и без); обёртки `dispatch.py` (`decide`, `check_wait_for`, `build_prompt`, …, 20 строк-прокладок) — оставлены Судьёй в TK-020 («тесты подменяют `D.check_wait_for` и `RUN_TIMEOUT»).

## Ось 3 · Сложность и длина

Порог: файлов > 900 — **0**; функций > 100 — **0**; radon E/F — **0**. Находок по порогу нет.

Ниже порога, к сведению: самые длинные файлы `watch.py` 827, `tickets.py` 805, `dispatch.py` 800, `overseer.py` 712, `errors.py` 690; самые длинные функции `dispatch.launch_run` 83, `role_context.full_context` 76, `e2e_clean_project.main` 74, `bench/micro.guard_cases` 74, `tickets.cmd_new` 68; radon D: 30 в `.claude/` (макс. `tickets.cmd_new` 30, `dguard/cmd_scan.const_str` 30, `dguard/policy.protected_match` 29), 8 в `tools/` (макс. `bench/judge_ab.run_case` 27). Порог не снижали — не трогать, пока Судья не снизит.

## Ось 4 · Дизайн

Находок нет. Что проверено:

- `D.<имя>` (фасад `dispatch`) вне `dispatch.py` в боевом коде — 0 (в проходе 2 — `tickets`, `watch`, `tickets_cli`); `import *` — 0; блоков `noqa F401` реэкспорта в `dispatch.py` — 1 (`role_args`, нужен тестам: см. ось 1).
- Имена `cfg`/`config` → `rpv_config`/`project_paths`: `grep` по репозиторию без `docs/reviews/`, `docs/findings/`, `CHANGELOG.md` — старых ссылок на модули нет.
- Циклы импортов: граф по всем `import` (включая lazy) даёт единственную петлю `role_context → (lazy) role_memory → role_context`; на загрузке модуля она не работает (`role_context.py:254,306` — внутри функций). Не находка.
- `legacy.py` — не прокладка (проход 2), оставить.

## Ось 5 · Дублирование

| файл:строка | что | предложение | меняет поведение | цена |
|---|---|---|---|---|
| `tools/e2e_clean_project.py:39,53` и `tools/bench/common.py:142,149` | `find_bash`, `init_script` — две копии, разошлись: в `e2e_clean_project` `find_bash` обходит заглушку `bash` из System32 (Git Bash), в `bench/common.py` — просто `shutil.which("bash")`; `init_script` — слово в слово | одна копия в `bench/common.py` (версия из `e2e_clean_project`), второй файл импортирует | да (на Windows `bench` перестанет брать заглушку WSL) | мелко |
| `.claude/dispatcher/haiku_aux.py:36` и `tools/bench/common.py:31` | `clean_env` — «окружение без `RPV_*` и `CLAUDE_PROJECT_DIR`»: тело совпадает (у `bench` плюс `**extra`) | `bench.clean_env(**extra)` строить на `haiku_aux.clean_env()` | нет | мелко |

Ноль находок: шаблоны ролей, `commands/`, `agents/` (дословных повторов > 90 знаков нет); тела функций ≥ 6 строк (AST-хэш) — точных дублей нет; окна по 8 одинаковых строк между файлами — нет (единственные два попадания — шапка импортов `overseer.py`↔`e2e_clean_project.py`).

## В работе у другого тикета (не находки этого прохода)

- `tools/helper_eval.py:60 load_agent` и `claude -p` в `helper_eval` на `haiku_aux.agent_def`/`agent_cmd`; `eval_common` (`emit`, замок, `jsonl`, разбор JSON `claude`) — PR #131, TK-021 (ждёт влива #129, влит: 23c35fe).
- `helper_eval.run_case` ↔ `judge_ab.run_case`, `cmd_regrade`/`cmd_report` в `helper_eval` ↔ `overseer_eval` — часть `eval_common` (TK-021).

## Отклонённое в проходах 1–2 (не находить заново)

- `delete_guard`: `isinstance(data, dict)` и `GUARDED_TOOLS` проверяются дважды (`main` и `check_tool`) — отказ с причиной (TK-009).
- `_popen` (обёртка «для тестов») — отказ (TK-009): 69 упоминаний в 6 тестовых файлах.
- Env только из тестов (`test_start.py`, `test_supervise.py`), `RPV_BENCH_HOME` — довод принят (TK-009).
- 57 × `except …: pass` — намеренные (`role_memory.py:13`).
- `bench/common.py:99 import resource` — намеренный (noqa F401, отказ рано на не-POSIX); `common.HOOKS` — тестовая константа (TK-019).
- Обёртки `check_wait_for`/`decide`/`build_prompt` в `dispatch.py` — тесты подменяют `D.check_wait_for` и `RUN_TIMEOUT` (TK-020).
- `bench_dispatch` правит атрибуты фасада `D` — менять нечего: `POLL_INTERVAL`, `ROLE_MODEL` живут в `dispatch.py` (TK-019).
- `build_cmd claude -p` в `helper_eval`/`overseer_eval`/`report` — сделано в TK-020 (`haiku_aux.agent_cmd`), остаток helper_eval — TK-021.
