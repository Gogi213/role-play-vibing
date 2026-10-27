"""Тесты tools/overseer_eval.py на чистых функциях и на выдуманном «проекте» во временном каталоге. Настоящий `claude` не зовётся:
`subprocess.run` в модуле подменён на падение, вызов модели — на заглушку. Запуск: python -m pytest -q -n 4 -p no:cacheprovider tools/test_overseer_eval.py"""
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import overseer_eval as ov  # noqa: E402

TZ = timezone(timedelta(hours=4))


def at(h, m=0, day=8):
    return datetime(2026, 10, day, h, m, tzinfo=TZ)


def iso(h, m=0, s=0, day=8):
    return f"2026-10-{day:02d}T{h:02d}:{m:02d}:{s:02d}+04:00"


def E(h, m, role="engineer", text="текст", day=8):
    return ov.core.Entry(at(h, m, day), role, text)


class NoClaude(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(ov.core.subprocess, "run", side_effect=AssertionError("тест вызвал настоящий claude"))
        p.start()
        self.addCleanup(p.stop)


class ParseTests(NoClaude):
    def test_log_entries_and_stray_headings(self):
        text = ("# TK-1 — архив\n\nпредисловие\n\n### 2026-10-08T01:55:06+04:00 ceo\nпервая\nстрока\n\n"
                "### Не запись\nэто часть первой\n\n### 2026-10-08T02:02:25+04:00 judge  \n[итог: return] ещё\n")
        es = ov.core.parse_log(text)
        self.assertEqual([(e.role, e.ts) for e in es], [("ceo", at(1, 55, day=8).replace(second=6)),
                                                          ("judge", at(2, 2).replace(second=25))])
        self.assertEqual(es[0].text, "первая\nстрока\n\n### Не запись\nэто часть первой")
        self.assertEqual(es[1].text, "[итог: return] ещё")
        self.assertEqual(es[0].norm, "первая строка ### Не запись это часть первой")

    def test_naive_timestamp_is_owner_time(self):
        es = ov.core.parse_log("### 2026-10-08T01:00:00 ceo\nx")
        self.assertEqual(es[0].ts.utcoffset(), timedelta(hours=4))

    def test_split_ticket(self):
        head, desc, log = ov.core.split_ticket("---\nid: TK-1\ntitle: Заголовок: с двоеточием\nstatus: done\n---\n\nЦель владельца.\nВторая.\n\n"
                                          "## Лог\n\n### 2026-10-08T01:00:00+04:00 ceo\nх\n")
        self.assertEqual((head["title"], head["status"]), ("Заголовок: с двоеточием", "done"))
        self.assertEqual(desc, "Цель владельца.\nВторая.")
        self.assertIn("### 2026-10-08T01:00:00+04:00 ceo", log)
        self.assertEqual(ov.core.split_ticket("---\nid: TK-2\n---\nтолько описание\n")[1:], ("только описание", ""))

    def test_merge_sorts_and_dedupes(self):
        a, b = [E(2, 0, text="a"), E(1, 0, text="старая")], [E(2, 0, text="a"), E(3, 0, text="b")]
        self.assertEqual([e.text for e in ov.core.merge_entries(a, b)], ["старая", "a", "b"])

    def test_runs_cost_resolved_then_cumulative_then_dash(self):
        log = "\n".join([
            f"{iso(1)} TK-1 engineer reason=todo attempt=0 session=s cost_usd=1.0 resolved_cost=1.0000 cost_note=asis status=ok",
            f"{iso(2)} TK-1 engineer reason=next attempt=0 session=s cost_usd=1.5 resolved_cost=0.5000 cost_note=diff status=ok",
            f"{iso(3)} TK-1 judge reason=todo attempt=0 session=- cost_usd=- status=no_output",
            f"{iso(4)} TK-1 judge reason=todo attempt=0 session=s cost_usd=2.0 status=ok",
            f"{iso(5)} TK-1 on_met reason=on_met attempt=0 session=- cost_usd=- resolved_cost=0.0000",
            f"{iso(6)} TK-2 engineer reason=todo cost_usd=0.2 resolved_cost=0.0000 status=ok",
            "мусор", ""])
        rs = ov.core.parse_runs(log)
        self.assertEqual([r.cost for r in rs["TK-1"]], [1.0, 0.5, 0.0, 2.0])  # 1.0 + 1.5 из cost_usd посчитали бы 2.5 вместо 1.5
        self.assertEqual([r.raw for r in rs["TK-1"]], [1.0, 1.5, 0.0, 2.0])
        self.assertEqual(rs["TK-2"][0].cost, 0.0)  # resolved_cost=0 — это ноль, а не «берём cost_usd»
        self.assertNotIn("on_met", [r.role for r in rs["TK-1"]])


class WindowTests(NoClaude):
    def test_grid_from_first_entry_only_windows_with_entries(self):
        es = [E(10, 0), E(10, 14), E(10, 15), E(11, 40), E(11, 44)]
        ws = ov.make_windows(es, 15)
        self.assertEqual([(w.n, w.index, len(w.new)) for w in ws], [(1, 0, 2), (2, 1, 1), (3, 6, 2)])
        self.assertEqual((ws[0].start, ws[0].end), (at(10, 0), at(10, 15)))
        self.assertEqual(ws[2].end, at(11, 45))
        self.assertEqual(len(ov.make_windows(es, 60)), 2)
        self.assertEqual(ov.make_windows([], 15), [])

    def test_skip_roles_do_not_open_windows_but_keep_grid(self):
        es = [E(10, 0, "ceo"), E(10, 20, "watch"), E(10, 40, "engineer")]
        ws = ov.make_windows(es, 15, skip_roles=("watch",))
        self.assertEqual([(w.index, [e.role for e in w.new]) for w in ws], [(0, ["ceo"]), (2, ["engineer"])])

    def test_stats_count_only_up_to_the_moment(self):
        es = [E(10, 0, "ceo"), E(10, 5, "judge", "[итог: return] нет"), E(10, 20, "judge", "[итог: accept] да"),
              E(10, 25, "engineer", "[итог: return] инженер не Судья"), E(11, 0, "judge", "[итог: return] позже")]
        rs = ov.core.parse_runs("\n".join([
            f"{iso(10, 1)} T engineer reason=a resolved_cost=1.0000 status=ok",
            f"{iso(10, 10)} T judge reason=a resolved_cost=0.5000 status=ok",
            f"{iso(16, 30)} T engineer reason=a resolved_cost=2.0000 status=ok",
            f"{iso(10, 59, 59)} T engineer reason=a resolved_cost=4.0000 status=ok"]))["T"]
        s = ov.stats_at(at(10, 30), es, rs)
        self.assertEqual((s.runs, round(s.cost, 2), s.returns), (2, 1.5, 1))
        self.assertAlmostEqual(s.hours, 0.5)
        s2 = ov.stats_at(at(17, 0), es, rs)  # 10:59:59 старше 6 ч, 16:30 — нет
        self.assertEqual((s2.runs, round(s2.cost, 2), s2.runs_recent, round(s2.cost_recent, 2), s2.returns), (4, 7.5, 1, 2.0, 2))
        self.assertAlmostEqual(s2.hours, 7.0)

    def test_start_is_earlier_of_first_entry_and_first_run(self):
        rs = [ov.core.Run(at(9, 30), "engineer", 1.0)]
        self.assertEqual(ov.ticket_start([E(10, 0)], rs), at(9, 30))
        self.assertEqual(ov.stats_at(at(10, 30), [E(10, 0)], rs).hours, 1.0)

    def test_stats_text_and_days(self):
        rs = [ov.core.Run(at(10, 0, day=d), "engineer", 1.5) for d in range(1, 10)]
        s = ov.stats_at(at(12, 0, day=9), [E(10, 0, day=1)], rs)
        self.assertEqual(len(s.days), ov.core.DAYS_SHOWN)
        txt = ov.core.stats_text(s)
        self.assertTrue(txt.startswith("к этому моменту: запусков 9, потрачено $13.50, часов от начала 194.0, возвратов Судьи 0."))
        self.assertIn("2026-10-09 — запусков 1, $1.50", txt)
        self.assertNotIn("2026-10-02", txt)


class PromptTests(NoClaude):
    def setUp(self):
        super().setUp()
        self.t = ov.core.Ticket("TK-9", "Заголовок", "done", "Владелец: «сделай быстрее».", [
            E(10, 0, "ceo", "старая запись про цель"), E(10, 20, "engineer", "ход инженера " * 40),
            E(10, 40, "judge", "[итог: return] вернул"), E(12, 0, "engineer", "ПОЗЖЕ окна")])
        self.w = ov.make_windows(self.t.entries, 15)[2]  # окно 10:30–10:45 с записью судьи
        self.s = ov.stats_at(self.w.end, self.t.entries, [])

    def test_sections_numbers_and_no_future(self):
        p = ov.build_prompt(self.t, self.w, self.s)
        self.assertIn("Тикет TK-9 — Заголовок", p)
        self.assertIn("«сделай быстрее»", p)
        self.assertIn("возвратов Судьи 1", p)
        self.assertIn("10-08T10:00 ceo: старая запись про цель", p)
        self.assertIn("### 2026-10-08T10:40:00+04:00 judge\n[итог: return] вернул", p)
        self.assertNotIn("ПОЗЖЕ окна", p)
        self.assertNotIn("10:40 judge", p.split("## Новые записи")[0])  # новая запись не повторяется в ходе тикета

    def test_context_is_truncated_with_note(self):
        t = ov.core.Ticket("TK-9", "З", "done", "д", [E(10, i, text=f"запись {i}") for i in range(50)] + [E(11, 30, text="новая")])
        w = ov.make_windows(t.entries, 15)[-1]
        p = ov.build_prompt(t, w, ov.stats_at(w.end, t.entries, []), ctx_lines=5)
        self.assertIn("показаны последние 5 из 50", p)
        self.assertIn("10-08T10:49 engineer: запись 49", p)
        self.assertNotIn("запись 44", p)
        self.assertIn("Записей до этого окна нет.", ov.build_prompt(t, ov.make_windows(t.entries, 15)[0], ov.stats_at(at(10, 15), t.entries, []), ctx_lines=0))

    def test_long_entry_and_new_part_are_cut_with_marks(self):
        self.assertEqual(ov.core.cut("коротко", 100), "коротко")
        c = ov.core.cut("а" * 1000, 100)
        self.assertIn("⟦обрезано: показано 100 из 1000 знаков⟧", c)
        self.assertEqual((len(c.split(" ⟦")[0]), len(c.split("⟧ ")[1])), (60, 40))  # начало и конец, не только начало
        new = [E(10, 0, text=f"запись {i} " + "я" * 200) for i in range(10)]
        blocks, dropped = ov.core.pick_new(new, 1200, 2500)
        self.assertEqual(len(blocks) + dropped, 10)
        self.assertGreater(dropped, 0)
        self.assertIn("запись 9", blocks[-1])  # остались последние
        w = ov.make_windows(new, 15)[0]
        p = ov.build_prompt(ov.core.Ticket("TK-9", "З", "done", "д", new), w, ov.stats_at(w.end, new, []), max_kb=1.2)
        self.assertIn("⟦пропущено старых записей окна:", p)
        self.assertEqual(ov.core.gist("а" * 300, 20), "а" * 20 + " ⟦обрезано⟧")

    def test_one_huge_entry_still_shown(self):
        blocks, dropped = ov.core.pick_new([E(10, 0, text="х" * 50000)], 100, 500)
        self.assertEqual((len(blocks), dropped), (1, 0))


class AnswerTests(NoClaude):
    ENTRIES = [E(10, 0, "engineer", "Замер:   ускорение 1,2×\nне растёт, тесты зелёные"), E(10, 30, "judge", "[итог: return] ещё раз"),
               E(12, 0, "engineer", "ПОЗЖЕ запись из будущего окна")]

    def finding(self, **kw):
        d = {"verdict": "находка", "kind": "не-движемся", "ticket": "TK-9", "quote": "ускорение 1,2× не растёт", "action": "остановить"}
        d.update(kw)
        return json.dumps(d, ensure_ascii=False)

    def g(self, raw, until=at(11, 0)):
        return ov.grade(raw, "TK-9", self.ENTRIES, until, "к этому моменту: запусков 7, потрачено $3.00")

    def test_parse_answer_variants(self):
        self.assertEqual(ov.core.parse_answer('{"verdict":"тихо"}')["verdict"], "тихо")
        self.assertEqual(ov.core.parse_answer('Вот:\n```json\n{"verdict":"тихо"}\n```\nвсё')["verdict"], "тихо")
        self.assertEqual(ov.core.parse_answer('{"a":1} {"verdict":"находка","kind":"x"}')["kind"], "x")
        self.assertIsNone(ov.core.parse_answer("тихо"))
        self.assertIsNone(ov.core.parse_answer('{"verdict": '))
        self.assertIsNone(ov.core.parse_answer(""))

    def test_quiet(self):
        self.assertEqual(self.g('{"verdict":"Тихо"}')["outcome"], "quiet")

    def test_accepted_with_whitespace_normalized(self):
        r = self.g(self.finding())
        self.assertEqual((r["outcome"], r["kind"]), ("accepted", "не-движемся"))
        self.assertEqual(self.g(self.finding(quote="  ускорение   1,2×\n не растёт, тесты"))["outcome"], "accepted")

    def test_rejections(self):
        cases = [({"quote": "ускорение вообще не растёт"}, "цитаты нет в логе дословно"),
                 ({"quote": "Ускорение 1,2× не растёт"}, "цитаты нет в логе дословно"),  # регистр — не дословно
                 ({"quote": "ПОЗЖЕ запись из будущего окна"}, "цитаты нет в логе дословно"),  # запись позже конца окна
                 ({"quote": "растёт, [итог: return] ещё раз"}, "цитаты нет в логе дословно"),  # склейка двух записей
                 ({"quote": "тесты"}, "нет цитаты"), ({"quote": ""}, "нет цитаты"),
                 ({"kind": "срочно"}, "вид не из четырёх"), ({"ticket": "TK-1"}, "тикет не тот"), ({"action": " "}, "нет действия")]
        for kw, reason in cases:
            r = self.g(self.finding(**kw))
            self.assertEqual((r["outcome"], r["reason"]), ("rejected", reason), kw)

    def test_errors(self):
        self.assertEqual(self.g("что-то не то")["outcome"], "error")
        self.assertEqual(self.g('{"verdict":"может"}')["outcome"], "error")

    def test_invented_numbers_flagged_but_not_rejected(self):
        r = self.g(self.finding(action="остановить: уже 7 запусков и $99 впустую"))
        self.assertEqual((r["outcome"], r["invented_numbers"]), ("accepted", ["99"]))
        self.assertEqual(self.g(self.finding(action="вернуть роли с В-183 по TK-12, запусков 7"))["invented_numbers"], [])
        self.assertEqual(ov.core.invented_numbers("1,2× и 3.00", "потрачено $3.00; ускорение 1,2×"), [])
        self.assertEqual(ov.core.invented_numbers("ускорение 1.2 при 3,00", "потрачено $3.00; ускорение 1,2×"), [])  # запятая и точка — одно
        self.assertEqual(ov.core.invented_numbers("33 ч и $3.5", "часов от начала 33.0, потрачено $3.50"), [])


class SetTests(NoClaude):
    def test_pick_normal_rule_and_seed(self):
        info = [(f"TK-{i:03d}", "done", 1 + i % 8, 0 if i % 3 else 1) for i in range(1, 200)]
        info += [("TK-300", "stopped", 2, 0), ("TK-301", "done", 0, 0), ("TK-048", "done", 2, 0)]
        pool = ov.normal_pool(info)
        self.assertNotIn("TK-300", pool)
        self.assertNotIn("TK-301", pool)
        self.assertNotIn("TK-048", pool)
        got = ov.pick_normal(info)
        self.assertEqual(len(got), ov.NORMAL_N)
        self.assertEqual(got, ov.pick_normal(list(reversed(info))))  # порядок входа не влияет
        self.assertEqual(got, sorted(got))
        self.assertTrue(set(got) <= set(pool))
        self.assertNotEqual(got, ov.pick_normal(info, seed=15))
        self.assertEqual(len(ov.pick_normal(info, n=1)), 1)
        self.assertEqual(len(ov.pick_normal(info[:3], n=20)), len(ov.normal_pool(info[:3])))

    def test_summary_first_accepted_and_daily_suppression(self):
        def row(tid, group, h, outcome, spent, day=8, total=10.0):
            e = iso(h, day=day)
            return {"key": f"{tid}|15|{e}", "ticket": tid, "group": group, "window": h, "end": e, "outcome": outcome, "kind": "не-движемся",
                    "spent": spent, "ticket_total": total, "hours": float(h), "cost": 0.01, "reason": ""}
        rows = [row("TK-1", "bad", 10, "quiet", 1.0), row("TK-1", "bad", 11, "accepted", 3.0), row("TK-1", "bad", 12, "accepted", 4.0),
                row("TK-1", "bad", 11, "accepted", 3.5, day=9), row("TK-1", "bad", 13, "rejected", 5.0),
                row("TK-2", "normal", 10, "quiet", 0.1, total=0.5)]
        s = ov.summarize(rows)
        self.assertEqual((s["TK-1"]["windows"], s["TK-1"]["accepted"], s["TK-1"]["accepted_daily"], s["TK-1"]["rejected"]), (5, 3, 2, 1))
        self.assertEqual((s["TK-1"]["first"]["window"], s["TK-1"]["share"]), (11, 0.3))
        self.assertIsNone(s["TK-2"]["first"])
        self.assertIsNone(s["TK-2"]["share"])
        again = rows + [dict(rows[0], outcome="accepted", spent=1.0)]  # то же окно повторили — берётся последняя строка
        self.assertEqual(ov.summarize(again)["TK-1"]["windows"], 5)

    def test_fit_cost(self):
        rows = [{"prompt_chars": c, "cost": 0.001 + 0.000002 * c} for c in (1000, 2000, 4000, 8000)]
        a, b = ov.fit_cost(rows)
        self.assertAlmostEqual(a, 0.001, places=9)
        self.assertAlmostEqual(b, 0.000002, places=12)
        a2, b2 = ov.fit_cost([{"prompt_chars": 5, "cost": 0.3}, {"prompt_chars": 7, "cost": 0.1}])  # мало точек — просто среднее
        self.assertAlmostEqual(a2, 0.2)
        self.assertEqual(b2, 0.0)
        self.assertEqual(ov.fit_cost([]), (0.0, 0.0))
        self.assertEqual(ov.fit_cost([{"prompt_chars": 5, "cost": None}, {"prompt_chars": 5, "cost": 1, "error": True}]), (0.0, 0.0))


class AgentTests(NoClaude):
    def test_overseer_definition(self):
        d = ov.HA.load_agent("overseer")
        self.assertEqual((d["model"], d["effort"], d["tools"]), ("claude-haiku-5-5", "xhigh", []))
        for kind in ov.core.KINDS:
            self.assertIn(kind, d["prompt"])
        self.assertIn('{"verdict":"тихо"}', d["prompt"])

    def test_claude_bin_follows_env(self):
        with mock.patch.dict(ov.core.os.environ, {"CLAUDE_BIN": "/нет/такого/claude"}):
            self.assertEqual(ov.core.claude_bin(), "/нет/такого/claude")
        with mock.patch.dict(ov.core.os.environ, {}, clear=False):
            ov.core.os.environ.pop("CLAUDE_BIN", None)
            self.assertTrue(ov.core.claude_bin())


class FakeProject(NoClaude):
    """Выдуманный проект: тикет с архивом и логом, runs.log, три обычных тикета."""

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        tdir = self.root / ".claude" / "tickets"
        (tdir / "archive").mkdir(parents=True)
        (self.root / ".claude" / "dispatcher").mkdir(parents=True)

        def ticket(tid, status, entries, archive=()):
            log = "".join(f"\n### {ts} {role}\n{text}\n" for ts, role, text in entries)
            (tdir / f"{tid}.md").write_text(f"---\nid: {tid}\ntitle: Тикет {tid}\nstatus: {status}\n---\n\nЦель {tid}.\n\n## Лог\n{log}", encoding="utf-8")
            if archive:
                (tdir / "archive" / f"{tid}-log.md").write_text(f"# {tid} — архив лога\n\n" + "".join(
                    f"### {ts} {role}\n{text}\n\n" for ts, role, text in archive), encoding="utf-8")
        ticket("TK-087", "done", [(iso(10, 20), "engineer", "Сдал PR, замер ускорения 1,2× не растёт"), (iso(10, 50), "judge", "[итог: return] вернул"),
                                  (iso(11, 40), "judge", "[итог: accept] принял")],
               archive=[(iso(10, 0), "ceo", "Цель поставлена: сделать быстрее")])
        for i in (1, 2, 3):
            ticket(f"TK-10{i}", "done", [(iso(9, 0), "ceo", "поставил"), (iso(9, 5), "judge", "[итог: accept] принял")])
        lines = [f"{iso(10, 15)} TK-087 engineer reason=todo resolved_cost=1.0000 status=ok",
                 f"{iso(10, 45)} TK-087 judge reason=next resolved_cost=0.5000 status=ok",
                 f"{iso(11, 35)} TK-087 judge reason=next resolved_cost=0.5000 status=ok"]
        lines += [f"{iso(9, 4)} TK-10{i} engineer reason=todo resolved_cost=0.2000 status=ok" for i in (1, 2, 3)]
        (self.root / ".claude" / "dispatcher" / "runs.log").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def args(self, out, **kw):
        d = dict(project=str(self.root), out=str(out), tickets="TK-087", group="all", step_min=15, skip_roles="", max_kb=12, ctx_lines=40,
                 entry_chars=2500, desc_chars=3000, model="claude-haiku-5-5", effort="xhigh", jobs=1, budget=1.0, per_call=0.25,
                 timeout=30, fit="")
        d.update(kw)
        return SimpleNamespace(**d)

    def test_load_ticket_merges_archive_and_file(self):
        t = ov.load_ticket(self.root, "TK-087")
        self.assertEqual([e.role for e in t.entries], ["ceo", "engineer", "judge", "judge"])
        self.assertEqual((t.title, t.status, t.desc), ("Тикет TK-087", "done", "Цель TK-087."))

    def test_default_set_has_bad_and_normal(self):
        tickets, runs = ov.load_all(self.root)
        self.assertEqual(ov.choose(tickets, runs, None, "bad"), [("TK-087", "bad")])
        self.assertEqual(sorted(t for t, g in ov.choose(tickets, runs, None, "normal")), ["TK-101", "TK-102", "TK-103"])

    def test_plan_jobs_numbers_per_window(self):
        tickets, runs = ov.load_all(self.root)
        jobs = ov.plan_jobs(tickets, runs, [("TK-087", "bad")], self.args(self.root / "o"))
        self.assertEqual([(j["window"], j["runs"], j["spent"], j["returns"]) for j in jobs], [(1, 1, 1.0, 0), (2, 1, 1.0, 0), (3, 2, 1.5, 1), (4, 3, 2.0, 1)])
        self.assertTrue(all(j["ticket_total"] == 2.0 and j["ticket_runs"] == 3 for j in jobs))
        self.assertIn("запусков 2, потрачено $1.50", jobs[2]["prompt"])
        self.assertNotIn("принял", jobs[2]["prompt"])  # accept в 11:40 — после окна

    def test_run_budget_resume_and_report(self):
        out = self.root / "out"
        calls = []

        def fake(prompt, agents_file, workdir, model, effort, per_call, timeout):
            calls.append(prompt)
            quote = "замер ускорения 1,2× не растёт" if "Сдал PR" in prompt else "нет такого"
            ans = ('{"verdict":"находка","kind":"не-движемся","ticket":"TK-087","quote":"%s","action":"остановить"}' % quote) if "Сдал PR" in prompt \
                else '{"verdict":"тихо"}'
            return {"answer": ans, "cost": 0.6, "dur": 1.0, "error": False, "in_tok": 100, "out_tok": 10, "think_tok": 5}
        with mock.patch.object(ov, "call_overseer", fake), redirect_stdout(io.StringIO()) as buf:
            ov.cmd_run(self.args(out, budget=1.0))
            first = len(calls)
            self.assertEqual(first, 2)  # 0.6 + 0.6 ≥ 1.0 — третий вызов не начат
            ov.cmd_run(self.args(out, budget=10.0))  # продолжение: готовые окна не повторяются
        self.assertEqual(len(calls), 4)
        rows = ov.read_rows(out)
        self.assertEqual(len(rows), 4)
        self.assertEqual([r["outcome"] for r in rows], ["quiet", "accepted", "accepted", "accepted"])
        self.assertIn("бюджет исчерпан: пропущено окон 2", buf.getvalue())
        self.assertTrue((out / "prompts" / "TK-087-s15-0002.txt").exists())
        s = ov.summarize(rows)["TK-087"]
        self.assertEqual((s["first"]["window"], s["share"]), (2, 0.5))
        table = [ln for ln in (out / "summary.md").read_text(encoding="utf-8").splitlines() if ln.startswith("|")]
        self.assertEqual(len(table), 3)  # шапка, разделитель, тикет
        self.assertEqual({ln.count("|") for ln in table}, {14})  # 13 столбцов у всех строк
        self.assertIn("TK-087", table[2])
        agents = json.loads((out / "agents.json").read_text(encoding="utf-8"))["overseer"]
        self.assertEqual((agents["model"], agents["effort"], agents["tools"]), ("claude-haiku-5-5", "xhigh", []))

    def test_regrade_without_calls(self):
        out = self.root / "out"

        def fake(prompt, *a, **k):
            return {"answer": '{"verdict":"находка","kind":"не-движемся","ticket":"TK-087","quote":"замер ускорения 1,2× не растёт","action":"стоп"}',
                    "cost": 0.01, "dur": 1.0, "error": False, "in_tok": 1, "out_tok": 1, "think_tok": 0}
        with mock.patch.object(ov, "call_overseer", fake), redirect_stdout(io.StringIO()):
            ov.cmd_run(self.args(out))
        self.assertEqual({r["outcome"] for r in ov.read_rows(out)}, {"accepted", "rejected"})  # в окне 1 записи с цитатой ещё нет
        with mock.patch.object(ov.core, "MIN_QUOTE", 100), redirect_stdout(io.StringIO()):
            ov.cmd_regrade(self.args(out))
        rows = ov.read_rows(out)
        self.assertEqual(len(rows), 4)
        self.assertEqual({(r["outcome"], r["reason"]) for r in rows}, {("rejected", "нет цитаты")})

    def test_error_window_is_retried_once_and_rerun_later(self):
        out = self.root / "out"
        n = {"c": 0}

        def flaky(*a, **k):
            n["c"] += 1
            return {"answer": "", "cost": 0.01, "dur": 1.0, "error": True, "in_tok": 0, "out_tok": 0, "think_tok": 0}
        with mock.patch.object(ov, "call_overseer", flaky), redirect_stdout(io.StringIO()):
            ov.cmd_run(self.args(out, tickets="TK-101"))
        self.assertEqual(n["c"], 2)  # окно одно, вызова два
        rows = ov.read_rows(out)
        self.assertEqual((rows[0]["outcome"], round(rows[0]["cost"], 2)), ("error", 0.02))

    def test_plan_prints_windows_without_claude(self):
        with redirect_stdout(io.StringIO()) as buf:
            ov.cmd_plan(self.args(self.root / "o", tickets="TK-087,TK-101", per_call=0.01))
        txt = buf.getvalue()
        self.assertIn("| TK-087 | bad | done | 3 |", txt)
        self.assertIn("окон 5 → $0.05", txt)
        self.assertIn("Все запуски проекта: 6; цена по resolved_cost $3;", txt)

    def test_unknown_ticket_is_clear_error(self):
        tickets, runs = ov.load_all(self.root)
        with self.assertRaises(SystemExit) as cm:
            ov.choose(tickets, runs, ["TK-087", "TK-999"], "all")
        self.assertIn("TK-999", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
