"""Тесты не видят настройку живой установки: RPV_*/ALPHA_* (шина, машины, ключи ssh, лимиты) убираются из окружения
до сбора тестов. Иначе pytest на машине с проектом пишет фикстуры в живую шину и в очередь CEO (TK-077, 08.10).
Без URL post шины уходит в spool, а он по умолчанию общий — %TEMP%/rpv-bus-spool.jsonl, его сливает живой диспетчер:
поэтому spool тестов — файл во временном каталоге сессии (тесты шины задают свой RPV_BUS_SPOOL сами)."""
import atexit
import os
import shutil
import tempfile

import pytest

_PREFIXES = ("RPV_", "ALPHA_")
_SPOOL_DIR = tempfile.mkdtemp(prefix="rpv-test-spool-")
_SPOOL = os.path.join(_SPOOL_DIR, "spool.jsonl")
atexit.register(shutil.rmtree, _SPOOL_DIR, True)


def _strip_live_env():
    for k in [k for k in os.environ if k.startswith(_PREFIXES)]:
        del os.environ[k]
    os.environ["RPV_BUS_SPOOL"] = _SPOOL


_strip_live_env()


def pytest_sessionstart(session):
    _strip_live_env()


@pytest.fixture(autouse=True)
def _own_spool():
    # тест шины мог снять RPV_BUS_SPOOL (endurance) — к следующему тесту вернуть спул сессии
    os.environ.setdefault("RPV_BUS_SPOOL", _SPOOL)
    yield
