"""Тесты не видят настройку живой установки: RPV_*/ALPHA_* (машины, ключи, лимиты) убираются из окружения до сбора тестов.
Иначе pytest на машине с проектом читает чужие пороги и пишет фикстуры в живую очередь CEO (TK-077, 08.10)."""
import atexit
import os
import shutil
import subprocess
import tempfile

import pytest

_PREFIXES = ("RPV_", "ALPHA_")
_TMP_DIR = tempfile.mkdtemp(prefix="rpv-test-")
atexit.register(shutil.rmtree, _TMP_DIR, True)


def _strip_live_env():
    for k in [k for k in os.environ if k.startswith(_PREFIXES)]:
        del os.environ[k]
    # настоящий `claude` тестам не нужен и стоит денег: без этого сжатие конспекта (haiku_aux) звало живой `claude -p`
    os.environ["CLAUDE_BIN"] = os.path.join(_TMP_DIR, "no-claude")


_strip_live_env()

# Алиасы машин живут в ~/.ssh/config, а не в окружении, которое чистится выше: тест, не подменивший subprocess.run, ходил бы
# по ssh на настоящую машину. Настоящий ssh из тестов — OSError; тесты ожиданий подменяют subprocess.run сами.
_REAL_RUN = subprocess.run


def _no_real_ssh(cmd, *args, **kwargs):
    if isinstance(cmd, (list, tuple)) and cmd and os.path.basename(str(cmd[0])).lower() in ("ssh", "ssh.exe"):
        raise OSError("тест вызвал настоящий ssh: подмени subprocess.run (алиасы машин из ~/.ssh/config живые)")
    return _REAL_RUN(cmd, *args, **kwargs)


subprocess.run = _no_real_ssh


def pytest_sessionstart(session):
    _strip_live_env()


@pytest.fixture(autouse=True)
def _isolated_environ():
    # снимок окружения до теста и полный возврат после: тест, оставивший RPV_*, не течёт в следующие
    # (и страж test_env_isolation не зависит от порядка файлов)
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)
