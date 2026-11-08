#!/bin/bash
# Запуск задания на машине с файлом итога (CP6): уникальное имя <метка>-<runid> и $RPV_RUNS_DIR/<unit>.rc.
#   jobrun.sh <метка> [-p СВОЙСТВО…] -- <команда…>
# Печатает «UNIT=<unit> RC=<путь к файлу итога>». Рядом с итогом лежит запись регистрации $RPV_RUNS_DIR/<unit>.job (mode=, unit=, pid=):
# по ней диспетчер отличает живое задание от мёртвого (TK-141); `tickets.py wait <ID> file:<RC>` без неё отказывает. Когда задание кончилось (успех, код выхода, сигнал), файл появляется целиком
# (tmp + mv), в нём одна строка: SERVICE_RESULT=<success|exit-code|signal|…> EXIT_STATUS=<код или имя сигнала>.
# Тикет ждёт этот файл: `tickets.py wait <ID> host:<ssh-алиас>:<путь RC> --by ЧЧ:ММ`; упало задание или нет — видно по содержимому.
# С systemd — транзиентный юнит (`systemd-run --collect`), итог пишет ExecStopPost (скрипт зовёт сам себя: --stop-post).
# Без systemd (macOS, контейнер, Git Bash) — фоновая оболочка запускает команду и пишет итог сама, вывод задания — в $RPV_RUNS_DIR/<unit>.log.
# Режим выбирается сам; RPV_JOBRUN_MODE=systemd|trap задаёт его явно. Каталог итогов — RPV_RUNS_DIR (по умолчанию /var/lib/rpv/runs),
# пользователю юнита нужна запись в него. Убитое вместе с машиной задание итога не оставляет — срок `--by` тикета его и поймает.
write_rc() {  # <файл> <результат> <статус>
    printf 'SERVICE_RESULT=%s EXIT_STATUS=%s\n' "$2" "$3" > "$1.tmp" && mv -f "$1.tmp" "$1"
}
write_job() {  # <файл> <режим> <unit> [pid]: запись регистрации (tmp + mv) — раньше, чем уйдёт строка UNIT=…
    { printf 'mode=%s\n' "$2"; printf 'unit=%s\n' "$3"; [ -z "$4" ] || printf 'pid=%s\n' "$4"; } > "$1.tmp" && mv -f "$1.tmp" "$1"
}
if [ "$1" = "--stop-post" ]; then  # хук ExecStopPost: SERVICE_RESULT и EXIT_STATUS кладёт в окружение сам systemd
    write_rc "$2" "$SERVICE_RESULT" "$EXIT_STATUS"
    exit
fi
label=$1; shift
props=()
while [ $# -gt 0 ] && [ "$1" != "--" ]; do props+=("$1"); shift; done
[ "$1" = "--" ] && shift
[ -n "$label" ] && [ $# -ge 1 ] || { echo "jobrun.sh <метка> [-p …] -- <команда…>" >&2; exit 2; }
runs=${RPV_RUNS_DIR:-/var/lib/rpv/runs}
runid=$(date +%m%d%H%M%S)-$(head -c2 /dev/urandom | od -An -tx1 | tr -d ' \n')
unit="$label-$runid"
rc="$runs/$unit.rc"
job="$runs/$unit.job"
mkdir -p "$runs" || { echo "нет каталога итогов: $runs" >&2; exit 1; }
mode=${RPV_JOBRUN_MODE:-auto}
if [ "$mode" = auto ]; then
    if [ -d /run/systemd/system ] && command -v systemd-run >/dev/null 2>&1; then mode=systemd; else mode=trap; fi
fi
if [ "$mode" = systemd ]; then
    self=$(cd "$(dirname "$0")" && pwd)/$(basename "$0")
    write_job "$job" systemd "$unit"
    systemd-run --unit "$unit" --collect -p "ExecStopPost=\"$self\" --stop-post \"$rc\"" "${props[@]}" "$@" >/dev/null 2>&1 \
        || { rm -f "$job"; echo "systemd-run: отказ" >&2; exit 1; }
else
    (
        trap '' HUP  # как nohup: конец ssh-сессии задание не трогает
        "$@" &
        pid=$!
        trap 'kill "$pid" 2>/dev/null' TERM INT
        wait "$pid"; st=$?
        while kill -0 "$pid" 2>/dev/null; do wait "$pid"; st=$?; done  # прерванный сигналом wait ждём заново: нужен код самого задания
        if [ "$st" -eq 0 ]; then
            write_rc "$rc" success 0
        elif [ "$st" -gt 128 ]; then
            sig=$(kill -l $((st - 128)) 2>/dev/null)
            write_rc "$rc" signal "${sig:-$st}"
        else
            write_rc "$rc" exit-code "$st"
        fi
    ) </dev/null >"$runs/$unit.log" 2>&1 &
    pid=$!
    wpid=$(cat "/proc/$pid/winpid" 2>/dev/null)  # Git Bash: pid оболочки и номер процесса Windows различаются — сторож (Python) видит второй
    write_job "$job" trap "$unit" "${wpid:-$pid}"
fi
echo "UNIT=$unit RC=$rc"
