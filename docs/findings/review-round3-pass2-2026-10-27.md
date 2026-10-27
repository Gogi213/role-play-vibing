# Раунд ревью 3, проход 2 (TK-046, В-9)

Снимок: `origin/main` 9b2b88e (слияние PR #156, TK-044), отдельная копия `.claude/worktrees/TK-046`. Объём: `git diff ad4c936 9b2b88e` без `test_*` и `docs/` — 7 файлов (`charter`, `dispatch`, `plugin_version`, `update`, `git_scan`, `bench/common`, `bench/e2e`); пороги и быстрая проверка — по всему `.claude/` и `tools/` (85 не-тестовых файлов). Порог — `docs/reviews/TK-006-2026-10-27.md`: файл > 900 строк, функция > 100, radon E/F.

## Итог

Находки #1–#6 прохода 1: **закрыты 6 из 6**. Новых: **3** (все без изменения поведения), порог не нарушен. Раунд чист: **нет** — по пунктам 7 и 8 есть дубль и мёртвая обёртка; пункт 7 уже лежит в PR #157 (TK-045, не влит).

## Закрытие находок прохода 1

| # | была | теперь на 9b2b88e | закрыта |
|---|---|---|---|
| 1 | `update.py:14` `import json` | строки нет; pyflakes по `update.py` пуст | да |
| 2 | `bench/common.py:21` `HOOKS` | константы нет, `grep -w HOOKS` по `tools`, `.claude` — пусто | да |
| 3 | `push_publication` radon E (32) | вынесены `refspec_publication`, `current_branch_publication` (`git_scan.py:233,244`); `push_publication` — D; radon E/F по дереву — 0 строк | да |
| 4 | два читателя `installed_plugins.json` | один: `plugin_version.plugin_entry` (`:49`); `update.install_entry` зовёт его (`update.py:34-36`), `read_installed`/`installed_json` убраны | да |
| 5 | каталог настроек в 3 местах, две нормализации пути | `PV.claude_dir()` (`plugin_version.py:18`) — в `dispatch.py:166`, `bench/e2e.py:27`, `plugin_version.py`; `_same_path` убран, `PV.norm` один в `plugin_version` и `update` | да (остаток — #7) |
| 6 | версия из `plugin.json` двумя функциями | `charter.plugin_version` → `PV.meta` (`charter.py:43`) | да (остаток — #8) |

## Новые находки

| # | ось | файл:строка (9b2b88e) | находка | меняет поведение |
|---|---|---|---|---|
| 7 | 5 дублирование | `role_args.py:62` ↔ `plugin_version.py:15` | `os.path.normcase(os.path.normpath(…))` дословно повторяет `PV.norm`; правка уже сделана — `a03acb7` в PR #157 (`eng/tk-045-norm`), в main её нет | нет |
| 8 | 5 дублирование | `tools/bench/common.py:66` ↔ `plugin_version.py:43-46` | версия плагина — третье чтение `plugin.json` вручную (`json.loads(...).get("version")`); `PV.meta(ROOT)` даёт то же, `DISP` уже в `sys.path` (`common.py:23`), `e2e.py` уже импортирует `PV`. `release.py:26,43` читает файл целиком для записи версии — не дубль | нет |
| 9 | 2 избыточность | `update.py:30` | `PLUGIN_NAME = MARKETPLACE` — алиас с одним потребителем (`update.py:36`); `PLUGIN` на `:29` строится из `MARKETPLACE` дважды, имя плагина и маркетплейса совпадают — вторая константа лишняя | нет |

## По осям

| ось | находок | итог |
|---|---|---|
| 1 мёртвый код | 0 | pyflakes: единственная строка — `tools/bench/common.py:100 resource` (намеренный, как в проходе 1); vulture ≥ 60: `ctypes` `hide.py:24-25`, `project.py:134` — прежние |
| 2 избыточность | 1 | #9 |
| 3 сложность и длина | 0 | radon E/F = 0; D = 41 (на v2.0.10 было 40 D + `push_publication` E; новые в D: `plugin_version.plugin_entry` 22; `push_publication` D); файлов > 900 — 0 (макс. `watch.py` 827); функций > 100 — 0 (макс. `role_context.full_context` 88, `dispatch.launch_run` 83) |
| 4 дизайн | 0 | `update.update(installed=install_entry)` — внедрение зависимости сохранено, форма вызова `installed(project)` одна; `plugin_version` — единственный читатель `installed_plugins.json`; циклов импортов нет (`charter`, `update`, `dispatch`, `bench/e2e` → `plugin_version` без обратных) |
| 5 дублирование | 2 | #7, #8 |

## Числа

| число | источник |
|---|---|
| находок прохода 1 закрыто 6 из 6; новых 3 (поведение: 0) | таблицы выше |
| radon E/F = 0; D = 41 (v2.0.10: 40 D + 1 E) | `python -m radon cc -nD .claude tools -e '*test_*'` на 9b2b88e и на `git archive 2a0ae2e` |
| max файл 827, > 900: 0; max функция 88, > 100: 0 | AST-проход и `splitlines`, 85 файлов |
| pytest: 1430 passed, 8 skipped | `python -m pytest -q -n auto` в копии на 9b2b88e, 198 с |

Алгоритм: варианты — только чтение диффа + radon/pyflakes/vulture/AST + полный pytest (3 мин); замер времени не нужен; выбран — он, потому что задача — проверка кода на пороги и дубли, а не производительность.

Раунд чист: **нет** — #7 закроет PR #157; #8 и #9 — мелкие правки без поведения (один тикет Инженера, проход 3). Решения владельца не нужно.
