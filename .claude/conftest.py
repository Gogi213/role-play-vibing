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
    # токен шины читается ещё и из ~/.rpv-bus-token владельца: без отключения на его ПК тесты находят «шину» и пишут
    # сигналы в очередь, а не в ceo-inbox.md (красные test_watch локально при зелёном CI, TK-101 п.5)
    os.environ["RPV_BUS_DISABLE"] = "1"


_strip_live_env()


def pytest_sessionstart(session):
    _strip_live_env()


def pytest_configure(config):
    config.addinivalue_line("markers", "endurance: раунды выносливости, ~3 мин каждый по часам (TK-095) — гоняются отдельным процессом параллельно основному")


def pytest_collection_modifyitems(items):
    for it in items:
        if it.path.name == "test_endurance.py" and "inbox_pages" not in it.name:
            it.add_marker(pytest.mark.endurance)


@pytest.fixture(autouse=True)
def _isolated_environ():
    # снимок окружения до теста и полный возврат после: тест шины, оставивший RPV_BUS_URL/TOKEN, не течёт в следующие
    # (и страж test_env_isolation не зависит от порядка файлов); спул сессии снова на месте, если тест его снял
    saved = dict(os.environ)
    os.environ.setdefault("RPV_BUS_SPOOL", _SPOOL)
    yield
    os.environ.clear()
    os.environ.update(saved)
