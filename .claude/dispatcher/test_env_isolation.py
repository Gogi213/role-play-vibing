"""Тесты не видят живую шину и настройку установки (TK-077): conftest.py убирает RPV_*/ALPHA_*."""
import os
import unittest


class EnvIsolation(unittest.TestCase):
    def test_no_live_settings_in_environment(self):
        leaked = sorted(k for k in os.environ if k.startswith(("RPV_", "ALPHA_")))
        self.assertEqual(leaked, [], "тесты наследуют настройку живой установки — запуск мимо conftest.py?")


if __name__ == "__main__":
    unittest.main()
