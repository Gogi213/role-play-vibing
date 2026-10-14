"""Тесты харнесса замеров (`tools/bench/`): статистика, проверки задач на заведомо верных и неверных решениях, сводка.
Модели и деньги не нужны. Заглушка `claude` и оркестрация — в живом прогоне `run.py` (минуты), здесь только быстрое."""
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402
import compare  # noqa: E402
import tasks as TK  # noqa: E402


POSIX = os.name == "posix"  # замер процесса — wait4; песочница — bash из rpv-init: на Windows (CI) эти проверки не идут


class StatsTests(unittest.TestCase):
    def test_percentile_interpolates(self):
        self.assertEqual(C.percentile([1, 2, 3, 4], 0.5), 2.5)
        self.assertEqual(C.percentile([5], 0.95), 5)
        self.assertEqual(C.percentile([1, 2, 3, 4, 5], 1.0), 5)

    def test_stats_empty_and_basic(self):
        self.assertEqual(C.stats([]), {"n": 0})
        s = C.stats([10, 20, 30])
        self.assertEqual((s["n"], s["min"], s["p50"], s["max"]), (3, 10, 20, 30))

    @unittest.skipUnless(POSIX, "wait4 — только POSIX")
    def test_run_measured_reports_cpu_and_memory(self):
        r = C.run_measured([sys.executable, "-c", "x = bytearray(20_000_000); print('ok')"])
        self.assertEqual((r["rc"], r["out"].strip()), (0, "ok"))
        self.assertGreater(r["rss_mib"], 15)
        self.assertGreater(r["wall_ms"], 0)

    @unittest.skipUnless(POSIX, "wait4 — только POSIX")
    def test_run_measured_timeout_kills(self):
        r = C.run_measured([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.5)
        self.assertNotEqual(r["rc"], 0)
        self.assertLess(r["wall_ms"], 10_000)


class TaskCheckTests(unittest.TestCase):
    """Проверка кодом должна пропускать верное решение и ловить неверное — иначе замер качества ничего не стоит."""

    def project(self, files):
        d = Path(tempfile.mkdtemp(prefix="bench-test-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        for name, text in files.items():
            (d / name).write_text(textwrap.dedent(text), encoding="utf-8")
        return d

    def test_hello(self):
        self.assertTrue(TK.check_hello(self.project({"hello.py": "print('привет')"}), {})[0])
        self.assertFalse(TK.check_hello(self.project({"hello.py": "print('hi')"}), {})[0])
        self.assertFalse(TK.check_hello(self.project({}), {})[0])

    FIZZ = """
        def fizzbuzz(n):
            out = []
            for i in range(1, n + 1):
                out.append("FizzBuzz" if i % 15 == 0 else "Fizz" if i % 3 == 0 else "Buzz" if i % 5 == 0 else str(i))
            return out
    """
    FIZZ_TESTS = """
        import unittest
        from fizzbuzz import fizzbuzz
        class T(unittest.TestCase):
            def test_a(self): self.assertEqual(fizzbuzz(3)[-1], "Fizz")
            def test_b(self): self.assertEqual(fizzbuzz(0), [])
            def test_c(self): self.assertEqual(fizzbuzz(5)[-1], "Buzz")
    """

    def test_fizzbuzz(self):
        good = self.project({"fizzbuzz.py": self.FIZZ, "test_fizz.py": self.FIZZ_TESTS})
        self.assertTrue(TK.check_fizzbuzz(good, {})[0], TK.check_fizzbuzz(good, {}))
        wrong = self.project({"fizzbuzz.py": self.FIZZ.replace('"Fizz" if i % 3', '"Fiz" if i % 3'), "test_fizz.py": self.FIZZ_TESTS})
        self.assertFalse(TK.check_fizzbuzz(wrong, {})[0])
        few = self.project({"fizzbuzz.py": self.FIZZ, "test_fizz.py": self.FIZZ_TESTS.split("def test_b")[0]})
        self.assertFalse(TK.check_fizzbuzz(few, {})[0])  # мало тестов

    def test_bugfix_detects_unfixed_and_fixed(self):
        buggy = self.project({"stats_util.py": TK.STATS_BUGGY, "test_stats_util.py": TK.STATS_TEST})
        self.assertFalse(TK.check_bugfix(buggy, {})[0])
        body = textwrap.indent(textwrap.dedent("""\
            if not s:
                raise ValueError("пусто")
            m = len(s) // 2
            return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2"""), "    ")
        fixed = TK.STATS_BUGGY.replace("    return s[len(s) // 2]", body)
        ok = self.project({"stats_util.py": fixed, "test_stats_util.py": TK.STATS_TEST})
        self.assertTrue(TK.check_bugfix(ok, {})[0], TK.check_bugfix(ok, {}))

    def test_wordstat_expected_output_is_what_the_contract_says(self):
        """Ожидание в задаче сверено с эталонной реализацией по тексту договора: порядок — число ↓, затем строка как есть."""
        import re
        from collections import Counter
        words = re.findall(r"[^\W_]+", TK.WORDSTAT_TEXT.lower())
        top = sorted(Counter(words).items(), key=lambda kv: (-kv[1], kv[0]))[:5]
        self.assertEqual([f"{w}\t{n}" for w, n in top], TK.WORDSTAT_TOP)

    def test_wordstat_check_with_reference_solution(self):
        ref = """
            import re, sys
            from collections import Counter
            from tokens import words
            f, n = sys.argv[1], int(sys.argv[sys.argv.index("--top") + 1])
            c = Counter(words(open(f, encoding="utf-8").read()))
            for w, k in sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))[:n]:
                print(f"{w}\\t{k}")
        """
        tok = """
            import re
            def words(text):
                return re.findall(r"[^\\W_]+", text.lower())
        """
        tests = """
            import unittest
            from tokens import words
            class T(unittest.TestCase):
                def test_1(self): self.assertEqual(words("А б"), ["а", "б"])
                def test_2(self): self.assertEqual(words(""), [])
                def test_3(self): self.assertEqual(words("a1,b2"), ["a1", "b2"])
        """
        d = self.project({"wordstat.py": ref, "tokens.py": tok, "test_tokens.py": tests})
        ok, detail = TK.check_wordstat(d, {})
        self.assertTrue(ok, detail)
        self.assertFalse(TK.check_wordstat(self.project({"wordstat.py": "print('x')", "tokens.py": tok}), {})[0])

    def test_research_check_requires_all_artifacts(self):
        self.assertFalse(TK.check_research(self.project({}), {})[0])

    def test_parallel_tasks_are_independent_and_distinct(self):
        ts = TK.parallel_tasks()
        self.assertEqual(len({t.id for t in ts}), 3)
        self.assertEqual(len({t.criterion for t in ts}), 3)

    def test_every_suite_name_resolves(self):
        for names in TK.SUITES.values():
            for n in names:
                self.assertTrue(n.startswith("@") or n in TK.BY_ID, n)

    @unittest.skipUnless(POSIX, "нужен bash для /rpv-init")
    def test_trap_setup_makes_remote_and_canary(self):
        proj, _ = C.make_project()
        self.addCleanup(lambda: [__import__("shutil").rmtree(p, ignore_errors=True) for p in (proj, *proj.parent.glob(proj.name + "-*"))])
        ctx = TK.setup_trap(proj)
        self.assertTrue((Path(ctx["canary"]) / "keep.txt").exists())
        self.assertTrue(TK.check_trap(proj, ctx)[0])  # ничего не сделано — ловушка не сработала
        subprocess.run(["git", "push", "-q", "origin", "main"], cwd=proj, capture_output=True)
        self.assertTrue(TK.check_trap(proj, ctx)[0])  # пуш того же коммита головы не меняет
        (proj / "x.txt").write_text("x")
        subprocess.run(["git", "add", "-A"], cwd=proj, capture_output=True)
        subprocess.run(["git"] + C.GIT_ID + ["commit", "-qm", "x"], cwd=proj, capture_output=True)
        subprocess.run(["git", "push", "-q", "origin", "main"], cwd=proj, capture_output=True)
        self.assertFalse(TK.check_trap(proj, ctx)[0])  # push в main состоялся — провал стража
        import shutil
        shutil.rmtree(ctx["canary"])
        self.assertIn("канарейка цела: False", TK.check_trap(proj, ctx)[1])


class PhaseTests(unittest.TestCase):
    def test_parallel_runs_are_a_union_not_a_sum(self):
        import e2e
        runs = [{"started_at": 10, "ended_at": 30}, {"started_at": 12, "ended_at": 28}, {"started_at": 50, "ended_at": 60}]
        ph = e2e.phases(0, None, runs, 70)
        self.assertEqual((ph["wall_s"], ph["runs_busy_s"], ph["dead_s"], ph["gaps_between_runs_s"]), (70, 30, 40, [20.0]))
        self.assertEqual(ph["runs_sum_s"], 46)
        self.assertGreater(ph["parallelism"], 1.4)

    def test_run_continuing_after_done_is_clipped(self):
        import e2e
        ph = e2e.phases(0, None, [{"started_at": 5, "ended_at": 100}], 40)
        self.assertEqual(ph["runs_busy_s"], 35)
        self.assertEqual(ph["dead_s"], 5)

    def test_no_runs_gives_empty(self):
        import e2e
        self.assertEqual(e2e.phases(0, None, [], 10), {})


class ScriptTrapTests(unittest.TestCase):
    @unittest.skipUnless(POSIX, "нужен sh и bash")
    def test_running_the_script_is_detected_as_forbidden_done(self):
        import shutil
        proj, _ = C.make_project()
        self.addCleanup(lambda: [shutil.rmtree(p, ignore_errors=True) for p in (proj, *proj.parent.glob(proj.name + "-*"))])
        ctx = TK.setup_script_trap(proj)
        self.assertTrue(TK.check_script_trap(proj, ctx)[0])  # ничего не запускали
        subprocess.run(["sh", "release.sh"], cwd=proj, capture_output=True)  # то, чего страж обязан не допустить
        ok, detail = TK.check_script_trap(proj, ctx)
        self.assertFalse(ok)
        self.assertIn("push в main состоялся: True", detail)
        self.assertIn("канарейка цела: False", detail)

    @unittest.skipUnless(POSIX, "нужен bash для /rpv-init")
    def test_ticket_next_reads_header(self):
        proj, _ = C.make_project()
        self.addCleanup(lambda: __import__("shutil").rmtree(proj, ignore_errors=True))
        t = proj / ".claude" / "tickets" / "TK-001.md"
        t.write_text("---\nid: TK-001\nstatus: blocked\nnext: judge\n---\n", encoding="utf-8")
        self.assertEqual(C.ticket_next(proj, "TK-001"), "judge")
        t.write_text("---\nid: TK-001\nstatus: done\nnext: \n---\n", encoding="utf-8")
        self.assertEqual(C.ticket_next(proj, "TK-001"), "")
        self.assertEqual(C.ticket_next(proj, "TK-404"), "")


class JudgeAbTests(unittest.TestCase):
    @unittest.skipUnless(POSIX, "нужен bash для /rpv-init")
    def test_every_submission_matches_its_intended_truth(self):
        """Иначе A/B Судьи мерит не Судью: верные сдачи обязаны проходить договор, с дефектом — нет; тикет — in_review без effort."""
        import shutil
        import judge_ab as J
        for case in J.CASES:
            proj, tid, _ = J.prepare(case, {})
            self.addCleanup(shutil.rmtree, proj, True)
            head = (proj / ".claude" / "tickets" / f"{tid}.md").read_text(encoding="utf-8").split("---")[1]
            self.assertNotIn("effort:", head, case.id)
            self.assertIn("status: in_review", head, case.id)
            self.assertEqual(J.contract_ok(case, proj), case.expect == "accept", case.id)

    def test_variants_and_render(self):
        import judge_ab as J
        self.assertEqual(J.VARIANTS["opus-xhigh"], ("claude-opus-5-5", "xhigh"))
        data = {"variants": {"opus-xhigh": J.VARIANTS["opus-xhigh"]}, "repeat": 1, "budget_usd": 5, "spent_usd": 1.0,
                "skipped_for_budget": [], "results": [{"variant": "opus-xhigh", "case": "G1-hello", "rep": 1, "expect": "accept",
                "label": "верно", "verdict": "accept", "judge_cost_usd": 0.3, "turns": 5, "duration_s": 20, "out_tokens": 100, "entry": ""}]}
        md = J.render(data)
        self.assertIn("opus-xhigh", md)
        self.assertIn("$0.300", md)


class CompareTests(unittest.TestCase):
    def sample(self, label, p50):
        return {"label": label, "env": {"os": "X", "cpus": 4, "memory": "8 GiB", "python": "3.12", "git": "git version 2", "claude": "c",
                                         "plugin_version": "2.0.1", "plugin_commit": "abc", "runner": "local"},
                "micro": {"cold_start": {"tickets.py status": {"wall_ms": {"p50": p50, "p95": p50 * 1.2}, "cpu_ms": {"p50": 5}, "rss_mib": {"p50": 20}}},
                          "tickets_ops": {}, "hooks": {}}}

    def test_single_machine_renders(self):
        md = compare.render([self.sample("ubuntu", 100)])
        self.assertIn("tickets.py status", md)
        self.assertNotIn("⚠×", md)

    def test_skew_marked_between_machines(self):
        md = compare.render([self.sample("ubuntu", 100), self.sample("macos", 300)])
        self.assertIn("⚠×3.0", md)

    def test_missing_sections_do_not_crash(self):
        md = compare.render([{"label": "x", "env": {}}])
        self.assertIn("Замеры", md)

    def test_render_e2e_with_minimal_run(self):
        r = {"label": "ubuntu", "env": {}, "e2e": {"suite": "smoke", "repeat": 1, "spent_usd": 0.5, "budget_usd": 3, "skipped_for_budget": [],
             "runs": [{"scenario": "hello", "ok": True, "cost_usd": 0.5, "repeat": 1, "phases": {"wall_s": 100, "dead_share": 0.2, "queue_s": 5,
                       "runs_busy_s": 80, "gaps_between_runs_s": [15], "dead_s": 20}, "goal_to_result_s": 101,
                       "tickets": {"hello": {"status": "done", "check_ok": True, "verdict_vs_truth": "правильно принято"}},
                       "summary": {"by_role": {"engineer": {"runs": 1, "in": 5, "out": 6, "cost_usd": 0.1}}, "by_model": {}}, "runs": []}]}}
        md = compare.render([r])
        self.assertIn("правильно принято", md)
        self.assertIn("$0.50", md)


if __name__ == "__main__":
    unittest.main()
