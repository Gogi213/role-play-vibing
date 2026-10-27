"""Тесты tools/eval_common.py. Запуск: python -m pytest -q -p no:cacheprovider tools/test_eval_common.py"""
import json
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import eval_common as EC  # noqa: E402


class JsonlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "rows.jsonl"

    def test_missing_file(self):
        self.assertEqual(EC.read_jsonl(self.path, missing_ok=True), [])
        with self.assertRaises(FileNotFoundError):
            EC.read_jsonl(self.path)

    def test_append_then_read_skips_blank_lines(self):
        EC.append_jsonl(self.path, {"id": "а", "n": 1})
        with self.path.open("a", encoding="utf-8") as w:
            w.write("\n")
        EC.append_jsonl(self.path, {"id": "б", "n": 2})
        self.assertEqual(EC.read_jsonl(self.path), [{"id": "а", "n": 1}, {"id": "б", "n": 2}])
        self.assertIn("а", self.path.read_text(encoding="utf-8"))  # не \u-экранирование

    def test_parallel_appends_do_not_mix_lines(self):
        with ThreadPoolExecutor(max_workers=8) as ex:
            list(ex.map(lambda i: EC.append_jsonl(self.path, {"i": i, "pad": "x" * 2000}), range(40)))
        self.assertEqual(sorted(r["i"] for r in EC.read_jsonl(self.path)), list(range(40)))


class ParseTests(unittest.TestCase):
    def test_json_passes_through(self):
        raw = json.dumps({"result": "ok", "total_cost_usd": 0.1})
        self.assertEqual(EC.parse_claude_json(raw), {"result": "ok", "total_cost_usd": 0.1})

    def test_not_json_is_error_with_tail(self):
        res = EC.parse_claude_json("x" * 5000 + "КОНЕЦ", tail=100)
        self.assertTrue(res["is_error"])
        self.assertEqual(len(res["result"]), 100)
        self.assertTrue(res["result"].endswith("КОНЕЦ"))

    def test_empty_output_is_error(self):
        self.assertEqual(EC.parse_claude_json(""), {"is_error": True, "result": ""})


if __name__ == "__main__":
    unittest.main()
