# TK-038 · вердикт Судьи · PR #150 @ deecdb5 · 2026-10-27

Принято. Сверено с В-13 (обновление одной командой: синк уставов в /rpv-update не стирает правила проекта).

| пункт заказа | да/нет | на чём проверено |
|---|---|---|
| Добавка `.claude/roles/project/<роль>.md` и `project/README.md`: синк не трогает | да | `charter.py` sync пишет только файлы шаблонов; тест `test_addon_survives_sync_on_a_new_template_and_lands_next_to_the_charter` |
| Роль получает устав + добавку | да | `role_context.py` full_context: блок добавки после устава, до блокнота; чужая добавка не попадает; большая — путём (`test_project_addon_follows_the_charter_and_only_for_its_role`, `test_huge_project_addon_is_replaced_by_path_and_total_within_limit`) |
| Писать — только CEO командой с --ref В-N, запись в charter-log.json | да | `charter addon` → `_ref_error(required=True)`, запись `kind: addon`; роли отказ «только CEO» (`test_addon_through_the_command_…`) |
| Страж прямую запись отклоняет | да | `policy.addon_file` — и для нового файла; `charter_cmd` закрывает cp/tee/sed/python/cd в `project/`; Write/Edit — отказ (`test_project_addons_are_written_by_the_command_only`) |
| charter sync без --ref — отказ | да | `run`: `_ref_error(required=True)` до синка; файлов и журнала нет (`test_sync_without_ref_is_refused_and_writes_nothing`) |
| /rpv-update передаёт ref сам | да (в PR #149) | `update.py` из PR #149 (TK-037, принят @474532c) вызывает `charter sync --ref <ref>`, без ref — FAIL шага; совместимо с этим PR |
| Убранные строки — список «перенеси в добавку» | да | `_removed_lines` / `_removed_report`, поле `removed` в журнале; строки таблицы зон README не в списке (`test_lines_dropped_from_the_project_charter_are_listed_and_journaled`, `test_readme_table_rows_of_the_project_are_not_reported_as_removed`) |
| тесты зелёные | да | CI зелёный на deecdb5; целевые тесты — ниже |

Гейт «при умолчании как прежде»: без файлов `project/` вставка роли не меняется; sync без --ref теперь отказ — это заказ (п. 2).

Замечание (не возврат): в договоре у addon было «--text|--file», сделан только `--text`; заказ этого не требует.

| число | файл:строка |
|---|---|
| 86 passed | docs/reviews/TK-038-check-deecdb5-2026-10-27.txt:1 |

E-005, E-006: в файле их проверки (test_charter_guard.py) только добавлен тест, старые не тронуты — отпечаток снят заново (`errors.py fix`, падала на 5955cc2, проходит на deecdb5).
