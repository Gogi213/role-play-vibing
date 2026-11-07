"""Малый потолок возвратов для тестов цикла «n возвратов → потолок» (TK-129, хвост TK-128): настоящие 500 кругов шли по ~40 с на тест.
`patch_review_cap(test)` подменяет `MAX_REVIEW_RETURNS` во всех модулях диспетчера, где он есть (`signals` и копии через
`from signals import` в `decide`, `handoff`); модули ищем по тексту файлов, чтобы новая копия не осталась с настоящим числом.
Настоящая константа — один тест `test_default_review_returns_limit_is_500` в test_dispatch.py, без подмены."""
from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
NAME = "MAX_REVIEW_RETURNS"
SMALL_CAP = 3  # больше двух: возвраты 1 и 2 ниже потолка, третий — на нём


def cap_holders() -> list:
    """Модули диспетчера (не тесты), в тексте которых есть MAX_REVIEW_RETURNS и у которых он есть как имя."""
    mods = []
    for path in sorted(HERE.glob("*.py")):
        if path.name.startswith("test_") or NAME not in path.read_text(encoding="utf-8"):
            continue
        mod = importlib.import_module(path.stem)
        if hasattr(mod, NAME):
            mods.append(mod)
    return mods


def patch_review_cap(test: unittest.TestCase, n: int = SMALL_CAP) -> int:
    """Потолок n вместо 500 на время теста; возвращает n. Снимается сам по окончании теста."""
    for mod in cap_holders():
        patcher = mock.patch.object(mod, NAME, n)
        patcher.start()
        test.addCleanup(patcher.stop)
    return n


class PatchReviewCap(unittest.TestCase):
    def test_every_copy_of_the_cap_is_patched_and_restored(self):
        names = {m.__name__ for m in cap_holders()}
        self.assertTrue({"signals", "decide", "handoff"} <= names, names)
        before = {m.__name__: getattr(m, NAME) for m in cap_holders()}
        self.assertEqual(set(before.values()), {500}, before)
        probe = unittest.TestCase()
        patch_review_cap(probe, 7)
        self.assertEqual({getattr(m, NAME) for m in cap_holders()}, {7})
        probe.doCleanups()
        self.assertEqual({getattr(m, NAME) for m in cap_holders()}, {500})


if __name__ == "__main__":
    unittest.main()
