#!/bin/sh
# Запускатель хуков плагина: один интерпретатор — python3, иначе python, иначе py -3 (без повторного запуска хука при
# сбое самого хука). Нет ни одного — тихо выходим: хук не ломает сессию. stdin и аргументы передаются как есть.
for py in python3 python; do
  if command -v "$py" >/dev/null 2>&1 && "$py" -c "import sys; sys.exit(0 if sys.version_info[0] >= 3 else 1)" >/dev/null 2>&1; then
    exec "$py" "$@"
  fi
done
if command -v py >/dev/null 2>&1; then
  exec py -3 "$@"
fi
exit 0
