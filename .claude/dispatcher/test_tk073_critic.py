"""TK-073 (В-31, п.2 и п.5): ворота критика — формы номера и вердикта, дубли, неразобранные «не держится», привязка отчёта к тикету
и голове, ответ Судьи по существу, «принял» — это return, «не хватает» требует ответа."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
import critic_gate as CG  # noqa: E402

PLUGIN = Path(__file__).resolve().parents[2]


class ParseTests(unittest.TestCase):
    def test_capitalised_verdict_is_read(self):
        items, _, _ = CG.parse_report("1. «а» — **Держится**\n2. «б» — **Не держится**\n3. «в» — **НЕ ХВАТАЕТ**\n")
        self.assertEqual(items, {"1": "держится", "2": "не держится", "3": "не хватает"})

    def test_capitalised_not_held_still_needs_an_answer(self):
        items, answered, _ = CG.parse_report("1. «а» — **Не держится**\n")
        self.assertEqual([n for n, v in items.items() if v == "не держится" and n not in answered], ["1"])

    def test_number_forms_are_read(self):
        for ln in ("1. «а» — **не держится**", "1) «а» — **не держится**", "**1.** «а» — **не держится**",
                   "`1.` «а» — **не держится**", "### 1. «а» — **не держится**", "- 1. «а» — **не держится**",
                   "**1)** «а» — **не держится**"):
            with self.subTest(line=ln):
                self.assertEqual(CG.parse_report(ln + "\n")[0], {"1": "не держится"})

    def test_verdict_on_the_next_line(self):
        items, _, _ = CG.parse_report("1. «а»\n   — **не держится**\n2. «б»\n\n**держится**: сверено\n")
        self.assertEqual(items, {"1": "не держится", "2": "держится"})

    def test_repeated_number_is_reported_and_keeps_worst(self):
        items, _, dups = CG.parse_report("1. «а» — **не держится**\n\n1. «а, ещё раз» — **держится**\n")
        self.assertEqual((items, dups), ({"1": "не держится"}, ["1"]))
        self.assertEqual(CG.parse_report("2. «б» — **держится**\n2. «б» — **не хватает**\n")[0], {"2": "не хватает"})

    def test_plain_report_unchanged(self):
        items, answered, dups = CG.parse_report(
            "1. «а» — **держится**\n2. «б» — **не держится**\nОтвет Судьи на 2: принял правку\n")
        self.assertEqual((items, answered, dups), ({"1": "держится", "2": "не держится"}, {"2"}, []))

    def test_template_has_no_backticks_around_the_item_line(self):
        line = next(ln for ln in (PLUGIN / "agents" / "critic.md").read_text(encoding="utf-8").splitlines()
                    if "«утверждение дословно»" in ln)
        self.assertNotIn("`", line)


class GateTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / ".claude").mkdir()
        (self.root / ".claude" / "rpv.json").write_text('{"critic_before_accept": true}', encoding="utf-8")
        (self.root / "docs" / "reviews").mkdir(parents=True)

    def ack(self, body, head="Тикет: TK-9\nГолова: abcdef1\n", sha="abcdef1234"):
        (self.root / "docs" / "reviews" / "r.md").write_text(head + body, encoding="utf-8")
        return CG.critic_ack(SimpleNamespace(critic="docs/reviews/r.md", critic_skip=None), "TK-9", self.root, sha,
                             head_ts=lambda r, s: 0.0)[0]

    def test_duplicate_number_refused(self):
        err = self.ack("1. «а» — **не держится**\n1. «а» — **держится**\nОтвет Судьи на 1: возражение по существу дела\n")
        self.assertIn("повторён", err)

    def test_unparsed_not_held_refused(self):
        err = self.ack("1. «а» — **держится**\nпункт 2 — **не держится**, см. файл.py:3\n")
        self.assertIn("«не держится» в файле 1, разобрано пунктов 0", err)

    def test_ticket_and_head_lines_are_required(self):
        ok = "1. «а» — **держится**\n"
        self.assertIn("Тикет: TK-9", self.ack(ok, head=""))
        self.assertIn("Тикет: TK-9", self.ack(ok, head="Тикет: TK-8\nГолова: abcdef1\n"))
        self.assertIn("Голова: abcdef1", self.ack(ok, head="Тикет: TK-9\nГолова: 1234567\n"))
        self.assertEqual(self.ack(ok), "")

    def test_empty_answer_refused(self):
        self.assertIn("без содержания", self.ack("1. «а» — **не держится**\nОтвет Судьи на 1: ок ок.\n"))
        ok = "1. «а» — **не держится**\nОтвет Судьи на 1: возражение — файл.py:3 это тест, не код\n"
        self.assertEqual(self.ack(ok), "")

    def test_accepting_the_remark_means_return_not_accept(self):
        err = self.ack("1. «а» — **не держится**\nОтвет Судьи на 1: принял замечание, правку делает Инженер\n")
        self.assertIn("верни тикет", err)

    def test_not_enough_needs_an_answer(self):
        self.assertIn("пунктам 1", self.ack("1. «а» — **не хватает**\n"))
        ok = "1. «а» — **не хватает**\nОтвет Судьи на 1: данные есть в docs/reviews/x.md строка 4\n"
        self.assertEqual(self.ack(ok), "")


if __name__ == "__main__":
    unittest.main()
