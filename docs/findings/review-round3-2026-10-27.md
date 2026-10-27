# Раунд ревью 3 (TK-043, В-9)

Снимок: тег `v2.0.10` (2a0ae2e), `git archive` во временный каталог ОС; основная копия не читалась и не правилась. Объём: `git diff 77bfc4b v2.0.10` без `test_*` и `docs/` — 25 файлов, +768 −81 (TK-037…040, TK-042). Плюс быстрая проверка порога по всему `.claude/` и `tools/` (82 не-тестовых файла). Порог — `docs/reviews/TK-006-2026-10-27.md`: файл > 900 строк, функция > 100, radon E/F.

## Итог

Находок: **6** (3 меняют поведение). Раунд **не чист**: пороговое нарушение одно (`push_publication`, radon E).

| # | ось | файл:строка (v2.0.10) | находка | меняет поведение |
|---|---|---|---|---|
| 1 | 1 мёртвый код | `.claude/dispatcher/update.py:14` | `import json` не используется (pyflakes); файл появился в 2.0.10 (TK-037) | нет |
| 2 | 1 мёртвый код | `tools/bench/common.py:21` | константа `HOOKS` нигде не читается (`HOOKS_JSON` в `micro.py:17` — другое имя); вне диффа, найдена быстрой проверкой по всему дереву | нет |
| 3 | 3 сложность | `.claude/hooks/dguard/git_scan.py:233` | `push_publication` — radon **E (32)**, была D (в 77bfc4b); TK-040 добавил сбор пар `pairs` и метку `label` в тот же цикл. Порог TK-006 (E/F) нарушен. Предложение: вынести разбор одной записи refspec (`src/dst → (метка, пара)`) в отдельную функцию | нет |
| 4 | 4 дизайн | `.claude/dispatcher/update.py:33-55` и `plugin_version.py:44-62` | два читателя `installed_plugins.json` с разными правилами выбора записи: `update.install_entry` — запись своего `projectPath` → первая `user` → первая любая; `plugin_version.installed` — самая узкая область local>project>user, только записи с `installPath` и `version`. Один и тот же вопрос («какая установка у проекта») получает разный ответ, если в файле несколько записей. Предложение: `update` берёт `plugin_version.installed` | **да** |
| 5 | 5 дублирование | `update.py:34` ↔ `plugin_version.py:50` ↔ `dispatch.py:165` | каталог настроек Claude (`CLAUDE_CONFIG_DIR` или `~/.claude`) вычисляется в трёх местах; `_same_path` (`update.py:38`) и `_norm` (`plugin_version.py:16`) — две нормализации пути (`abspath+normcase` против `normpath+normcase`) | нет |
| 6 | 5 дублирование | `charter.py:41` ↔ `plugin_version.py:38-42` | версия плагина из `plugin.json` читается двумя функциями (`charter.plugin_version` и `plugin_version.meta(root).get("version")`); `charter` дополнительно подставляет `"?"` | нет |

| ось | находок | итог |
|---|---|---|
| 1 мёртвый код | 2 | #1 (diff), #2 (весь `tools`); `vulture` ≥ 60 без тестов — прочее прежнее: `ctypes` `hide.py:24-25`, `project.py:134`, `conftest.py` (хуки pytest) |
| 2 избыточность | 0 | `start.find_copies` (однострочная обёртка над `find_scripts`) — два живых вызова (`doctor.py:175`, `start.py:330`), оставлена; `supervise.launchd_env/launchd_label/plist_path` — по два и более потребителей (`doctor.py`, `supervise.py`) |
| 3 сложность и длина | 1 | #3. Файлов > 900 — 0 (макс. `watch.py` 827); функций > 100 — 0 (макс. `role_context.full_context` 88, было меньше; `dispatch.launch_run` 83); radon E/F — 1 (было 0); radon D = 41 (было 39: новые в D — `role_context.full_context` 23, `plugin_version.installed` 22; `push_publication` перешла из D в E) |
| 4 дизайн | 1 | #4. Цепочка импортов `dguard` (`… policy → push_scope → ceo_signal → checks`) без циклов; `Publish.main`/`PushRefs` — один носитель данных пуша, второго нет |
| 5 дублирование | 2 | #5, #6. Остальное из диффа (`_ref_error` в `charter`, `push_words`) — одна реализация на правило |

## Числа

| число | источник |
|---|---|
| находок 6 (поведение: 1, #4) | таблица выше |
| radon E/F = 1 (`push_publication` E 32), D = 41 | `radon cc -nE/-nD .claude tools` на v2.0.10; D на 77bfc4b — 39 |
| max файл 827, > 900: 0 | `wc -l`, 82 не-тестовых файла |
| max функция 88, > 100: 0 | AST-проход, те же файлы |
| pyflakes: 2 (`update.py:14`, `bench/common.py:101 resource` — намеренный) | `pyflakes .claude tools` без `test_` |

Алгоритм: варианты — только чтение + статический разбор (секунды); замер времени не нужен; выбран — он, потому что задача — поиск по коду. Тесты не гонялись (код не правился).

Раунд чист: **нет** — #3 нарушает порог TK-006. #4 меняет поведение — решает Судья отдельным тикетом Инженера (README: пометка «да»).
