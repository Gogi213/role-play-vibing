# Раунд ревью 2 · поиск по изменённому с v2.0.8 (TK-032, В-9)

Снимок: тег `v2.0.9` = `origin/main` 84ca237 (`git archive` во временный каталог ОС; основная копия не читалась и не правилась). Что смотрели: `git diff --name-only v2.0.8 v2.0.9` без `test_*` — правки TK-027 (шаг «после слияния») и TK-029 (`charter sync` + страж записи в уставы): `dispatcher/{charter(новый),decide,report,role_args,tickets,tickets_cli}`, `hooks/{delete_guard,dguard/charter_cmd(новый),dguard/git_scan,dguard/messages}`, `templates/roles/judge.md`; плюс быстрая проверка порога по всем `*.py` без `test_*`/`conftest` в `.claude/` и `tools/` (82 файла). Инструменты: `radon cc`, `vulture` ≥ 60, `pyflakes`, AST-проход длин функций, сверка новых функций с соседними модулями (`project`, `rpv_config`, `dguard/*`). Порог Судьи (`docs/reviews/TK-006-2026-10-27.md`): файл > 900 строк, функция > 100 строк, radon E/F.

## Итог (5 строк)

1. **Мёртвый код — 1 находка, мелкая:** `charter.py:148` — `entry` не читается.
2. **Избыточность — 1, мелкая:** `charter_cmd.py:104` — лишний списочный цикл над готовым списком.
3. **Сложность и длина — 0 по порогу.** radon E/F — 0; файлов > 900 — 0 (макс. `watch.py` 827); функций > 100 — 0 (макс. `dispatch.launch_run` 83). Новое: `charter_cmd._sub_hit` D (28, 33 строки) — ниже порога.
4. **Дизайн — 1:** `charter_cmd` считает рабочий каталог после `cd` сам, рядом с двумя уже имеющимися способами в `dguard`.
5. **Дублирование — 2:** `charter._write_bytes` (запись без повторов на Windows) против `project.atomic_write_text`; `charter.log_path/read_log` против `rpv_config.log_path/read_log`.

Всего в таблицах 5 строк; «меняет поведение — да»: 1 (`_write_bytes` → повторы при `PermissionError`). Раунд не чист: по В-9 нужна правка и проход 2.

## Ось 1 · Мёртвый код

| файл:строка | что | предложение | меняет поведение | цена |
|---|---|---|---|---|
| `.claude/dispatcher/charter.py:148` | `run`: `entry, msg, err = sync(...)` — `entry` дальше не читается | `_, msg, err = …` | нет | мелко |

Проверено и пусто: `pyflakes` — только намеренный `tools/bench/common.py:101 resource`; `vulture` ≥ 60 — поля `ctypes` (`hide.py:24-25`, `project.py:134`) и `HOOKS` (отклонено в раунде 1); `charter.py`/`charter_cmd.py`: все импорты и константы читаются (`PLUGIN_ROOT`, `LOG_NAME`, `TABLE_FILE`, `COPY_TO`, `ALL_ARGS`, `DIRY_SOURCE`, `TOKEN`, `CD`); ветка `dd` и `of=` в `_copy_targets` живая.

## Ось 2 · Избыточность

| файл:строка | что | предложение | меняет поведение | цена |
|---|---|---|---|---|
| `.claude/hooks/dguard/charter_cmd.py:104` | `[w for w in re.findall(r"[\w.\-]+\.md\b", text)]` — `findall` уже список | `re.findall(...)` | нет | мелко |

Проверено и пусто: `AFTER_MERGE_FIELD` и `AFTER_MERGE_ORDER` (`decide.py:49-50`) — два шаблона, но не копия: второй привязан к началу строки (описание тикета), первый ищет в записи приёмки; оставить. `_after_merge_note` (`tickets.py`) читается из двух мест (`cmd_accept`, `_accept_in_pr`).

## Ось 3 · Сложность и длина

Порог: файлов > 900 — **0**; функций > 100 — **0**; radon E/F — **0**. Находок по порогу нет.

К сведению, ниже порога: самые длинные файлы `watch.py` 827, `tickets.py` 820 (+15 к 8495ba9: `charter`, `after-merge`), `dispatch.py` 796, `overseer.py` 702; самые длинные функции `dispatch.launch_run` 83, `role_context.full_context` 76, `e2e_clean_project.main` 74, `bench/micro.guard_cases` 74; radon D — 39 (было 38: добавилась `charter_cmd._sub_hit`, 28). Новые `charter.sync` C (19), `merge_table_rows` C (14), `_copy_targets` C (15). Порог не снижали.

## Ось 4 · Дизайн

| файл:строка | что | предложение | меняет поведение | цена |
|---|---|---|---|---|
| `.claude/hooks/dguard/charter_cmd.py:15,26,123` | собственные `CD` (регэксп) и `_abs` для слежения за каталогом после `cd`; в `dguard` уже есть `cmd_scan.py:386` (`cd/pushd/chdir/set-location/sl` → `join_cwd`, учитывает переменные и `popd`) и `paths.under_cwd` — три способа посчитать cwd | взять `join_cwd`/`under_cwd` из `dguard`; различие `popd`/переменных сверить тестом | возможно (`popd`, `cd -`, `$VAR` в пути сейчас считаются по-разному) | средне |

Проверено и пусто: `D.<имя>` вне `dispatch.py` — 0; `import *` — 0; `charter.py` зависит только от `project`, `registry`, `rpv_config` (циклов нет); `report.py` берёт `charter_mod.report_lines` рядом с `cfg_mod.report_lines` — тот же вид, что у соседей.

## Ось 5 · Дублирование

| файл:строка | что | предложение | меняет поведение | цена |
|---|---|---|---|---|
| `.claude/dispatcher/charter.py:67` | `_write_bytes`: временный файл + `os.replace`, без повторов; `project.atomic_write_text` (`project.py:279`) делает то же с 8 повторами на `PermissionError` (Windows: `replace` падает, пока роль читает файл — а `_write_bytes` создан именно для запущенных ролей) | байтовый вариант в `project.py` рядом с текстовым, общий цикл повторов; `charter` зовёт его | да (повторы вместо падения) | средне |
| `.claude/dispatcher/charter.py:27,31` ↔ `.claude/dispatcher/rpv_config.py:64,72` | `log_path` и `read_log` — одно тело (список словарей из журнала, иначе `[]`), разные только `LOG_NAME`; ещё и `rpv_config.read_log` не использует `P.read_json`, а свой `json.loads` | общий `read_log(path)` в `project.py` (через `read_json`), оба журнала зовут его | нет | мелко |

Проверено и пусто: `report_lines` обоих журналов — разная раскладка строки (поля, состояния), не копия; `charter_cmd.WORD/_words` — единственный разбор слов команды в `dguard`; окна по 8 одинаковых строк между новыми и старыми файлами — 0.

## Числа

| число | файл:строка |
|---|---|
| radon E/F = 0 | вывод `radon cc -nE` на снимке 84ca237, 82 файла |
| max файл 827 (`watch.py`), > 900: 0 | `wc -l` на снимке |
| max функция 83 (`dispatch.launch_run`), > 100: 0 | AST-проход на снимке |
| radon D = 39 | `radon cc -nD` на снимке |
