"""Планка приёмки для детекторов хвостов (TK-090, В-36): 0 расхождений с main на корпусе реальных сдач плюс заказанные строки.
Корпус — `fixtures/tail_corpus.json`: настоящие `[итог: done|pr]` исполнителей и ответ main (`tickets.tail_hit`). Правка
`TAIL_NEGATION`/`TAIL_WORDS` не должна менять ответ ни на одной сдаче корпуса; её заказанные строки — в `ordered` с ожидаемым ответом."""
from __future__ import annotations

import atexit
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

_SANDBOX = tempfile.mkdtemp(prefix="rpv-test-proj-")
os.makedirs(os.path.join(_SANDBOX, ".claude", "roles"))
os.environ["CLAUDE_PROJECT_DIR"] = _SANDBOX
atexit.register(shutil.rmtree, _SANDBOX, True)
sys.path.insert(0, str(Path(__file__).resolve().parent))
import tickets as TK  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "tail_corpus.json"
CLASS = {True: "хвост (main: не хвост) — ложный отказ приёмки", False: "не хвост (main: хвост) — пропуск хвоста"}


def divergences(items) -> list:
    """Сдачи, на которых детектор отвечает не так, как записано: (id, класс расхождения, цитата)."""
    out = []
    for it in items:
        now = bool(TK.tail_hit(it["text"])[1])
        if now != it["tail"]:
            out.append((it.get("id", "ordered"), CLASS[now], " ".join(it["text"].split())[:160]))
    return out


class TailCorpusGate(unittest.TestCase):
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def test_corpus_is_real_and_not_trivial(self):
        corpus = self.data["corpus"]
        self.assertGreaterEqual(len(corpus), 150)
        self.assertTrue(any(c["tail"] for c in corpus) and any(not c["tail"] for c in corpus))

    def test_zero_divergences_from_main_on_the_corpus(self):
        bad = divergences(self.data["corpus"])
        self.assertEqual(bad, [], "расхождение с main на реальной сдаче — назови класс и сузь правку, не патчи регэксп за автора:\n"
                         + "\n".join(f"  {i}: {c}: {q}" for i, c, q in bad))

    def test_ordered_lines_answer_as_ordered(self):
        self.assertEqual(divergences(self.data["ordered"]), [])

    def test_gate_sees_a_divergence(self):
        flipped = [dict(self.data["corpus"][0], tail=not self.data["corpus"][0]["tail"])]
        self.assertEqual(len(divergences(flipped)), 1)


if __name__ == "__main__":
    unittest.main()
