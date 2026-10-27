# TK-034 · раунд ревью 2, проход 2 — чисто

Снимок: голова PR #142 f636a67 (хвост TK-033; принят Судьёй, `TK-033-verdict-f636a67-2026-10-27.md`), `git archive` во временный каталог. Код, изменённый с v2.0.9: `dispatcher/{charter,project,rpv_config}.py`, `hooks/dguard/charter_cmd.py` (+ тесты). Инструменты: `pyflakes`, `vulture` ≥ 60, `radon cc`, AST-длины, сверка соседних модулей.

| ось | итог |
|---|---|
| 1 мёртвый код | pyflakes — пусто (`os`, `CD` удалены, `posixpath`/`json` ещё используются: charter_cmd.py:101, rpv_config.py:86,95). vulture: новых нет (остальное — публичные функции, вызываемые из соседних модулей и тестов). `_write_bytes`, `CD` ссылок не имеют |
| 2 избыточность | новой нет. К сведению: `charter.read_log` и `rpv_config.read_log` — однострочные обёртки над `P.read_log(log_path(root))`, каждая со своим `log_path`; вызывают и тесты — оставлены осознанно |
| 3 сложность и длина | radon E/F = 0; max файл 828 (`watch.py`); max функция 83 (`dispatch.launch_run`); D = 39 (было 39 в проходе 1 при `_sub_hit` D 28→27, число то же). Порог TK-006 не нарушен |
| 4 дизайн | `_atomic_write(path, write, retries)` — общий цикл, обёртки text/bytes; `_cd` использует общий `join_cwd`/`under_cwd` стража; `popd` оставляет `here` (E-006), закреплено тестом |
| 5 дублирование | три копии «читать журнал» → одна `P.read_log`; две копии атомарной записи → одна; `CD`/`_abs` в `charter_cmd` свёрнуты на `dguard.words`/`dguard.paths`. Остатка нет |

Проверка: `pytest .claude/dispatcher/test_tk033.py .claude/hooks/test_charter_guard.py` на снимке — 17 passed.

Вердикт: находок 0, тикет Инженеру не нужен. Раунд 2 чист по коду до f636a67; следующий раунд — после очередного выпуска по новому коду (В-9). Условие: PR #142 влит в том виде, в каком смотрели (f636a67).
