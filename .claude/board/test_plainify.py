import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import plainify as P

PROC = {"id": "TK-1", "title": "Счёт", "summary": "сделано: код — 1 из 2",
        "steps": [{"title": "код", "state": "done"}, {"title": "счёт", "state": "run"}]}


def view2():
    return {"processes": [json.loads(json.dumps(PROC)), {"id": "TK-2", "title": "без плана", "summary": None, "steps": []}]}


class FactsTest(unittest.TestCase):
    def test_fallback_line(self):
        self.assertEqual(P.fallback_line(PROC), "1 из 2 шагов готово")
        self.assertIsNone(P.fallback_line({"steps": []}))

    def test_key_ignores_details_and_numbers(self):
        a = P.payload(PROC)
        b = P.payload({**PROC, "n": 7, "summary": "другое", "steps": [dict(s, detail="x", n=i) for i, s in enumerate(PROC["steps"])]})
        self.assertEqual(P.key_of(a), P.key_of(b))
        self.assertNotEqual(P.key_of(a), P.key_of({**a, "title": "Иное"}))

    def test_norm(self):
        self.assertEqual(P.norm('"Код готов, идёт счёт."\nлишнее'), "Код готов, идёт счёт")
        self.assertIsNone(P.norm(""))
        self.assertIsNone(P.norm("я" * 141))


class NoClaudeTest(unittest.TestCase):
    def test_without_claude_summary_stays_facts(self):
        with tempfile.TemporaryDirectory() as d:
            pl = P.Plain(Path(d) / "plain-auto.json", runner=lambda *a, **k: self.fail("claude вызван"), claude="")
            v = P.apply(view2(), Path(d) / "plain-auto.json", wait=True, plain=pl)
            self.assertEqual(v["processes"][0]["summary"], "сделано: код — 1 из 2")
            self.assertIsNone(v["processes"][1]["summary"])
            self.assertFalse((Path(d) / "plain-auto.json").exists())


class ModelTest(unittest.TestCase):
    def test_call_cache_and_reuse(self):
        calls = []

        def runner(cmd, **kw):
            calls.append((cmd, kw))
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"result": "Код готов, идёт счёт."}), stderr="")

        with tempfile.TemporaryDirectory() as d:
            cache = Path(d) / ".claude" / "pulse" / "plain-auto.json"
            pl = P.Plain(cache, runner=runner, claude="claude")
            v = P.apply(view2(), cache, wait=False, plain=pl)
            self.assertEqual(v["processes"][0]["summary"], "сделано: код — 1 из 2")  # ответа ещё нет — факты плана
            pl.flush(10)
            v = P.apply(view2(), cache, plain=pl)
            self.assertEqual(v["processes"][0]["summary"], "Код готов, идёт счёт — 1 из 2 готово")
            self.assertEqual(len(calls), 1)
            cmd, kw = calls[0]
            self.assertEqual(cmd[:4], ["claude", "-p", "--model", "claude-haiku-5-5"])
            self.assertIn("--no-session-persistence", cmd)
            self.assertNotIn(d, kw["cwd"])  # временный каталог вне проекта
            self.assertEqual(json.loads(kw["input"])["title"], "Счёт")
            self.assertEqual(list(json.loads(cache.read_text(encoding="utf-8"))["items"].values()), ["Код готов, идёт счёт"])
            P.apply(view2(), cache, plain=P.Plain(cache, runner=lambda *a, **k: self.fail("повторный вызов"), claude="claude"))
            self.assertEqual(len(calls), 1)

    def test_errors_fall_back_and_back_off(self):
        n = []

        def bad(cmd, **kw):
            n.append(1)
            return subprocess.CompletedProcess(cmd, 1, stdout="oops", stderr="")

        with tempfile.TemporaryDirectory() as d:
            cache = Path(d) / "plain-auto.json"
            pl = P.Plain(cache, runner=bad, claude="claude")
            v = P.apply(view2(), cache, wait=True, plain=pl)
            self.assertEqual(v["processes"][0]["summary"], "сделано: код — 1 из 2")
            P.apply(view2(), cache, wait=True, plain=pl)  # повтор раньше паузы не зовёт модель
            self.assertEqual(len(n), 1)
            self.assertFalse(cache.exists())

    def test_timeout_and_missing_binary(self):
        def timeout(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, 60)

        def missing(cmd, **kw):
            raise FileNotFoundError(cmd[0])

        for runner in (timeout, missing):
            with tempfile.TemporaryDirectory() as d:
                pl = P.Plain(Path(d) / "c.json", runner=runner, claude="claude")
                v = P.apply(view2(), Path(d) / "c.json", wait=True, plain=pl)
                self.assertEqual(v["processes"][0]["summary"], "сделано: код — 1 из 2")


if __name__ == "__main__":
    unittest.main()
