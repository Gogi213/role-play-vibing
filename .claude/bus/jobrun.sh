#!/bin/bash
# Запуск задания на машине (Linux, systemd) с уникальным именем юнита <метка>-<runid> и записью InvocationID.
#   jobrun.sh <метка> [-p СВОЙСТВО…] -- <команда…>
# Пишет $RPV_RUNS_DIR/<unit>.json {unit, runid, invocation, started} (по умолчанию /var/lib/rpv/runs) и печатает «UNIT=… INV=…».
# wait_for в тикете: host:<машина>:unit:<unit> — конец придёт событием шины (watcher.py: «юнит.запущен/остановлен» с тем же
# InvocationID), ssh-опрос остаётся запасным (раз в 5 мин). Два запуска с одной меткой не путаются: имя и InvocationID разные.
label=$1; shift
props=()
while [ $# -gt 0 ] && [ "$1" != "--" ]; do props+=("$1"); shift; done
[ "$1" = "--" ] && shift
[ -n "$label" ] && [ $# -ge 1 ] || { echo "jobrun.sh <метка> [-p …] -- <команда…>" >&2; exit 2; }
runs=${RPV_RUNS_DIR:-/var/lib/rpv/runs}
runid=$(date +%m%d%H%M%S)-$(head -c2 /dev/urandom | od -An -tx1 | tr -d ' \n')
unit="$label-$runid"
systemd-run --unit "$unit" --collect "${props[@]}" "$@" >/dev/null 2>&1 || { echo "systemd-run: отказ" >&2; exit 1; }
inv=""
for _ in 1 2 3 4 5 6 7 8 9 10; do inv=$(systemctl show -p InvocationID --value "$unit.service" 2>/dev/null); [ -n "$inv" ] && break; sleep 0.3; done
mkdir -p "$runs" && printf '{"unit":"%s","runid":"%s","invocation":"%s","started":"%s"}\n' "$unit" "$runid" "$inv" "$(date -Is)" > "$runs/$unit.json"
echo "UNIT=$unit INV=$inv"
