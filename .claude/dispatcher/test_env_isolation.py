"""Тесты не видят настройку живой установки (TK-077): conftest.py убирает RPV_*/ALPHA_* до сбора тестов."""
import os
import unittest

# ключи живой установки (CP8: в окружении остались только они): проект, бот владельца, папка плагина
_LIVE = ("RPV_PROJECT", "RPV_TG_TOKEN", "RPV_TG_CHAT", "RPV_PLUGIN_DIR")


class EnvIsolation(unittest.TestCase):
    def test_no_live_settings_in_environment(self):
        leaked = sorted(k for k in os.environ if k in _LIVE or k.startswith("ALPHA_"))
        self.assertEqual(leaked, [], "тесты наследуют настройку живой установки — запуск мимо conftest.py?")


if __name__ == "__main__":
    unittest.main()
