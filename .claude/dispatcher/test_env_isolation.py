"""Тесты не видят живую шину и настройку установки (TK-077): conftest.py убирает RPV_*/ALPHA_*, spool — во временный каталог."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bus"))
import busclient  # noqa: E402

# ключи живой установки; прочие RPV_* (RPV_BUS_DISABLE и др.) ставят сами тесты при импорте — по ним порядок файлов не гадаем
_LIVE = ("RPV_BUS_URL", "RPV_BUS_TOKEN", "RPV_BUS_TOKEN_FILE")


class EnvIsolation(unittest.TestCase):
    def test_no_live_settings_in_environment(self):
        leaked = sorted(k for k in os.environ if k in _LIVE or k.startswith("ALPHA_"))
        self.assertEqual(leaked, [], "тесты наследуют настройку живой установки — запуск мимо conftest.py?")

    def _need_conftest(self):
        if "PYTEST_CURRENT_TEST" not in os.environ:
            self.skipTest("спул подменяет conftest.py — только под pytest; под unittest задай RPV_BUS_SPOOL сам")

    def test_spool_is_not_the_shared_live_file(self):
        self._need_conftest()
        shared = os.path.join(tempfile.gettempdir(), "rpv-bus-spool.jsonl")
        self.assertNotEqual(os.path.abspath(busclient.spool_path()), os.path.abspath(shared),
                            "spool тестов совпал с общим %TEMP%/rpv-bus-spool.jsonl живой установки")

    def test_post_without_bus_does_not_touch_shared_spool(self):
        self._need_conftest()
        shared = os.path.join(tempfile.gettempdir(), "rpv-bus-spool.jsonl")
        before = os.path.getsize(shared) if os.path.exists(shared) else None
        busclient.post("тест.изоляция", {"x": 1})
        after = os.path.getsize(shared) if os.path.exists(shared) else None
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
