#!/usr/bin/env python3
"""Запуск диспетчера для замеров: тот же `dispatch.main`, но с двумя подменами констант, которых иначе не задать снаружи.

  BENCH_POLL_S      период тика вместо 15 с (не задана — боевое значение: так меряется настоящая задержка передачи хода)
  BENCH_PID_NAME    подстрока имени образа роли для подхвата после перезапуска (заглушка — Python, а не `claude`)
  BENCH_JUDGE_MODEL / BENCH_JUDGE_EFFORT   модель и усилие Судьи вместо ROLE_MODEL/ROLE_EFFORT (сравнение вариантов Судьи;
                    поле `effort:` тикета по-прежнему перекрывает усилие — в сравнении тикеты без него)

Больше ничего не меняется: учёт, троттлинг, замки — боевые."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / ".claude" / "dispatcher"))
import dispatch as D  # noqa: E402

if os.environ.get("BENCH_POLL_S"):
    D.POLL_INTERVAL = float(os.environ["BENCH_POLL_S"])
if os.environ.get("BENCH_PID_NAME"):
    procs.PID_EXPECT_NAME = os.environ["BENCH_PID_NAME"]
if os.environ.get("BENCH_JUDGE_MODEL"):
    D.ROLE_MODEL["judge"] = os.environ["BENCH_JUDGE_MODEL"]  # словарь меняется на месте: launch_run читает его при каждом запуске
if os.environ.get("BENCH_JUDGE_EFFORT"):
    D.ROLE_EFFORT["judge"] = os.environ["BENCH_JUDGE_EFFORT"]

if __name__ == "__main__":
    if not sys.stdout.isatty():
        import signal
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    sys.exit(D.main())
