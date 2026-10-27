"""Тексты отказов и классы причин (`Over`, `Forbid`, `Publish`, `BranchDelete`)."""
import re


REASON = ("Удаление запрещено вне своей папки. Можно только явным путём внутри папки проекта или scratchpad сессии; на "
          "удалённых машинах — только каталоги guard_remote_roots. Закрытые хосты (guard_forbidden_hosts) и "
          "защищённые пути (protected_paths) из .claude/rpv.json — никогда (только владелец через CEO). "
          "Непроверяемая цель: {t}")
REASON_OVERWRITE = ("Перезапись/усечение файла (`> файл`, truncate, dd of=, cp/mv поверх существующего) вне своей папки "
                    "запрещены, как и удаление: внутри папки проекта или scratchpad сессии; на удалённых машинах — "
                    "только каталоги guard_remote_roots (.claude/rpv.json); автопамять проекта (~/.claude/projects/<проект>/memory/) "
                    "— можно. Новый локальный файл вне папки можно создать (его ещё нет). Закрытые хосты и защищённые "
                    "пути проекта — никогда. Цель: {t}")
REASON_IRREVERSIBLE = ("Необратимая команда git ({t}): только через CEO — не выполнять самому, передать CEO итогом "
                       "шага (`tickets.py result <ID> blocked --why \"...\"`). `git reset --hard`, "
                       "`git clean -f`, `git checkout -- <путь>|.`, `git restore`, `git switch -f`, `git branch -D|-M|-f`, "
                       "`git stash clear|drop` разрешены только в каталоге вне основного дерева, заданном явным путём "
                       "(`cd <путь>` или `git -C <путь>`): scratchpad сессии, удалённые корни guard_remote_roots, "
                       "/tmp/<подкаталог>, корни хоста, связанный worktree (.claude/worktrees/…); `git push --force`, "
                       "`+ветка`, `--delete`, `:ветка`, `--mirror` — всегда только через CEO.")
REASON_BRANCH_DELETE = ("Удаление веток на удалённом ({t}) без слова владельца закрыто и для CEO: {why}. Слово владельца дословно — "
                        "`registry.py decide` (его пишет `secretary`), затем `tickets.py allow-delete <ветка>… --ref В-N`: согласие на "
                        "названные ветки действует час; main/master удалять нельзя. Запуску роли — только через CEO.")
REASON_DECIDE = ("Решение владельца в реестр (`registry.py decide`) из сессии CEO пишет только помощник `secretary` (слово владельца "
                 "дословно, суть — его): CEO суть своими словами не пишет (В-7: без самодеятельности). Передай секретарю слова "
                 "владельца и команду; слово короче 15 знаков («да») — с вопросом CEO дословно: `--question \"...\"`.")
REASON_CONFIG_LOG = ("Журналы правок настроек и уставов (config-log.json, charter-log.json) пишут только команды `tickets.py config` "
                     "и `tickets.py charter` (ни роль, ни CEO руками).")
REASON_CEO_CONFIG = ("Настройки плагина и уставы ролей ({t}) CEO напрямую не правит (слово владельца В-7: менять можно, но без "
                     "самодеятельности). Настройки — `tickets.py config set <ключ> <значение> --why \"...\"` (закрытый список "
                     "ключей, Судья проверяет, без его приёмки правка через 2 ч откатывается); уставы `.claude/roles/*.md` не "
                     "правятся, кроме строк таблицы зон в README; новые строки уставов из шаблонов плагина — "
                     "`tickets.py charter sync [--file <имя>.md]`. Блокнот `notes/ceo.md` — свободно.")
REASON_CEO_CHARTER = ("Устав ({t}) CEO записью через оболочку не правит: cp, mv, tee, перенаправление, python, sed, install, rsync — "
                      "всё закрыто (слово владельца В-7). Перенос из шаблонов плагина — `tickets.py charter sync "
                      "[--file <имя>.md]` (текст как есть, строки таблицы зон README остаются проектными, правка в журнале и в "
                      "отчёте). Чтение, блокнот `notes/ceo.md` и новый файл (первый `/rpv-init`) — свободно.")
REASON_BRANCH_CONSENT_FILE = ("Согласия на удаление веток пишет только команда `tickets.py allow-delete` после слова владельца: файл "
                              "branch-delete.json руками не правится (ни ролью, ни CEO).")
REASON_PUBLISH = ("Публикация ({t}) запуску роли закрыта без согласия владельца по тикету {who}: выпуск, слияние PR, пуш в "
                  "основную ветку (main/master) и теги, публикация пакетов и образов необратимы. Нужна — `tickets.py result <ID> "
                  "ask-owner --why \"...\" --why-owner необратимое`; слово владельца CEO заносит в реестр (`registry.py decide`) и "
                  "ставит согласие `tickets.py allow <ID> publish --ref В-N`. Пуш рабочей ветки разрешён (ветку назови явно: "
                  "`git push origin <ветка>`); PR вливает программа (merge_rule) после приёмки Судьи.")
REASON_PUSH_SCOPE = ("Пуш в основную ветку ({t}) уносит {n} коммит(ов) вне принятого тикета (приёмка Судьи, `accepted:` в шапке: {who}): "
                     "{shown}. Завести этим коммитам тикет (Судья принимает — sha в `accepted`) или пушить отдельно: рабочая "
                     "ветка и PR (`git push origin <ветка>`), PR вливает программа (merge_rule). Настройка `guard_push_scope` "
                     "(.claude/rpv.json).")
REASON_PUSH_SCOPE_UNKNOWN = ("Пуш в основную ветку ({t}): коммиты не сверить с принятым тикетом — {why} (`guard_push_scope`, "
                             "fail-closed). Пуш рабочей ветки разрешён; пуш в main/master — из репозитория проекта, после "
                             "`git fetch`, с явной веткой (`git push origin main`).")
REASON_CONSENT = ("Согласие на публикацию запуск диспетчера в шапку тикета не пишет: строку `allow:` ставит только CEO командой "
                  "`tickets.py allow <ID> publish --ref В-N` после слова владельца. Нужна публикация — `tickets.py result <ID> "
                  "ask-owner --why \"...\" --why-owner необратимое`.")
REASON_FILE = ("Запись в файл вне своей папки ({t}) запрещена, как и перезапись через Bash: Write/Edit/MultiEdit/NotebookEdit "
               "меняют существующие файлы только внутри папки проекта, scratchpad сессии, временного каталога и автопамяти "
               "проекта (~/.claude/projects/<проект>/memory/). Новый файл вне папки создать можно (его ещё нет). "
               "Защищённые пути проекта и закрытые хосты — никогда. Нужна правка вне папки — через владельца/CEO.")
REASON_CRASH = ("Страж удаления упал ({e}) — отказ по умолчанию (fail-closed), а не пропуск. Исправьте "
                ".claude/hooks/delete_guard.py плагина вручную (вне этой сессии) или отключите плагин; не получилось — "
                "сообщите владельцу/CEO.")
REASON_PROTECTED = ("Защищённый путь ({t}): шаблон «{p}» из .claude/rpv.json (protected_paths) — удалять, переносить и "
                    "перезаписывать нельзя никому, ни роли, ни CEO. Нужно изменить — владелец правит список "
                    "protected_paths в .claude/rpv.json сам.")
REASON_PROTECTED_CONFIG = ("Защищённые пути проекта не прочитаны ({e}): .claude/rpv.json, ключ protected_paths — должен быть "
                           "список шаблонов, например [\"data/raw/**\", \"backups\"]. Пока файл не исправлен, удалять и "
                           "перезаписывать нельзя ничего (fail-closed); сам .claude/rpv.json править можно. Исправление — "
                           "через владельца/CEO.")
UNKNOWN_TARGET = "<цель из кода не видна>"
# куда писать можно всегда: не файлы (устройства-стоки, пустышка Windows/PowerShell)
HARMLESS_SINK = re.compile(r"^(?:/dev/(?:null|stdout|stderr|tty|zero|full|fd/\d+)|/proc/self/fd/\d+|nul|con|\$null)$", re.I)
MAX_DEPTH = 8


class Over(str):
    """Цель перезаписи/усечения (не удаления): вне корней отказ, только если файл существует или это неизвестно."""


class Forbid(str):
    """Необратимая команда (git reset --hard и т. п.): отказ «только через CEO», путь не при чём."""


class Publish(Forbid):
    """Публикация (выпуск, слияние PR, пуш в main/master или тега, публикация пакета): запуску роли — только с согласием
    владельца по тикету (`allow: publish` в шапке), сессиям CEO и владельца можно. Подкласс Forbid: те же ветки find/ssh.
    `main` — для пуша в main/master: куда и какие ветки уходят (`PushRefs`), чтобы сверить коммиты (`guard_push_scope`)."""

    def __new__(cls, label, main=None):
        self = super().__new__(cls, label)
        self.main = main
        return self


class BranchDelete(Forbid):
    """Удаление веток на удалённом с названными ветками (`git push --delete <ветка>…` / `:<ветка>`): запуску роли — никогда;
    сессии CEO/владельца — с согласием владельца на каждую ветку (`tickets.py allow-delete`, файл dispatcher/branch-delete.json,
    на час). Ветки не названы (`--mirror`, `--prune`, маска, переменная) — обычный Forbid для всех."""

    def __new__(cls, label, branches=()):
        self = super().__new__(cls, label)
        self.branches = tuple(branches)
        return self
REASON_GIT_DIR = ("Каталог .git ({t}) — история репозитория: удалять или переносить его и файлы внутри нельзя никому "
                  "(`rm -rf .git`, `rm -rf .*`, `mv .git …`); можно только снять зависший замок `*.lock`. Чистка "
                  "истории — только владелец через CEO.")
REASON_GIT_WRITE = ("Запуск диспетчера не пишет внутрь .git ({t}): состояние репозитория меняют команды git, а не правка "
                    "файлов. Нужно иное — через CEO.")
REASON_SETTINGS = ("Настройки Claude Code и плагина ({t}) запуску диспетчера менять нельзя: через них выключаются хуки и "
                   "плагин (`disableAllHooks`, `enabledPlugins`) и снимается защита путей (`protected_paths`), а с ними "
                   "и этот страж. Правка настроек — через CEO.")
REASON_OWNER_RECORD = ("Слово владельца и реестр ошибок ({t}) запуск диспетчера напрямую не правит: решения и цели пишет "
                       "только CEO командой `registry.py` (дословно), CLAUDE.md — только CEO, записи `.claude/errors/` — только "
                       "скрипт `errors.py` (`add` — любая роль, `fix` — Судья после красной проверки на коммите до исправления). "
                       "Нужно изменить — `tickets.py result <ID> ask-owner --why \"...\"`.")
REASON_IDENTITY = ("Запуск роли не меняет и не стирает свою роль и тикет (RPV_ROLE, RPV_TICKET, `env -i`): притвориться CEO "
                   "нельзя (слово владельца 14.10). Нужно действие CEO — `tickets.py result <ID> blocked --why \"...\"` "
                   "или ask-owner через Судью.")
REASON_CEO_SIGNAL = ("Сигналы команды пишет только диспетчер: ceo-inbox.md/ceo-wake.log — это dispatch.append_ceo_inbox. "
                     "Нужно CEO — `tickets.py result <ID> blocked|ask-owner --why \"...\"`; новые строки читает `tickets.py inbox` (docs/signals.md).")
