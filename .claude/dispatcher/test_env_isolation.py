"""Тесты не видят настройку живой установки (TK-077): conftest.py убирает RPV_*/ALPHA_* до сбора тестов."""
import os
import unittest

# ключи живой установки; прочие RPV_* (RPV_CI_REPO и др.) ставят сами тесты при импорте — по ним порядок файлов не гадаем
_LIVE = ("RPV_WATCHED_ALIASES", "RPV_JOB_STATE_CMD", "RPV_JOB_OWNERS_CMD", "RPV_STRAY_CMD", "RPV_PROJECT")


class EnvIsolation(unittest.TestCase):
    def test_no_live_settings_in_environment(self):
        leaked = sorted(k for k in os.environ if k in _LIVE or k.startswith("ALPHA_"))
        self.assertEqual(leaked, [], "тесты наследуют настройку живой установки — запуск мимо conftest.py?")


if __name__ == "__main__":
    unittest.main()
