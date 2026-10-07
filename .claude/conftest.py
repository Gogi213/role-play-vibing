"""Тесты не видят настройку живой установки: RPV_*/ALPHA_* (шина, машины, ключи ssh, лимиты) убираются из окружения
до сбора тестов. Иначе pytest на машине с проектом пишет фикстуры в живую шину и в очередь CEO (TK-077, 08.10)."""
import os

_PREFIXES = ("RPV_", "ALPHA_")


def _strip_live_env():
    for k in [k for k in os.environ if k.startswith(_PREFIXES)]:
        del os.environ[k]


_strip_live_env()


def pytest_sessionstart(session):
    _strip_live_env()
