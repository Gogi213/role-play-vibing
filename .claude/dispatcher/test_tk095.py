"""TK-095 (В-38): Пульс — pulse.json по тику: клетки по 15 минут (work/none/owner/stuck), фраза-итог, коды причин «застряло»."""
from __future__ import annotations

import json
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_tk086 import GoalSandbox, CFG, T  # noqa: E402 — песочница тикетов и целей
import pulse as PU  # noqa: E402
import rpv_config  # noqa: E402

PLUGIN = Path(__file__).resolve().parents[2]


class PulseBase(GoalSandbox):
    def setUp(self):
        super().setUp()
        self.disp = Path(CFG.DISPATCHER_DIR)
        self.at = self.now + timedelta(hours=1)  # 13:00: сутки 2026-09-27, клетка 52
        self.state = {"last_tick": T.now_iso(self.at - timedelta(seconds=15))}

    def run_line(self, minutes_ago, tid="TK-1", status="ok", role="engineer"):
        ts = T.now_iso(self.at - timedelta(minutes=minutes_ago))
        with open(self.disp / "runs.log", "a", encoding="utf-8") as f:
            f.write(f"{ts} {tid} {role} reason=next attempt=0 status={status}\n")

    def settings(self, **kv):
        (Path(CFG.PROJECT_ROOT) / ".claude" / "rpv.json").write_text(json.dumps(kv), encoding="utf-8")

    def pulse(self, at=None):
        at = at or self.at
        data = PU.build(self.state, at)
        self.state["last_tick"] = T.now_iso(at)  # как конец тика диспетчера
        return data

    def codes(self, data):
        return [r["code"] for r in data["reasons"]]

    def cell(self, data, at=None):
        return data["cells"][PU.cell_index(at or self.at)]


class CellClassTests(PulseBase):
    def test_runs_make_work_cells(self):
        self.run_line(95)   # 11:25 — клетка 45
        self.run_line(94)
        data = self.pulse()
        self.assertEqual((data["cells"][45]["c"], data["cells"][45]["n"]), ("work", 2))
        self.assertEqual(len(data["cells"]), 96)

    def test_no_tickets_is_none_not_stuck(self):
        data = self.pulse()
        self.assertEqual((self.cell(data)["c"], data["state"], data["reasons"]), ("none", "ok", []))
        self.assertEqual(data["now"], "очередь пуста")

    def test_ticket_being_served_is_not_stuck(self):
        self.ticket(ref="", status="todo")
        data = self.pulse()  # слот свободен, но флагов простоя нет — диспетчер заберёт в этом же тике
        self.assertEqual((self.cell(data)["c"], data["reasons"]), ("none", []))

    def test_active_run_makes_current_cell_work(self):
        self.state["active_runs"] = {"TK-7": {"role": "engineer", "started": T.now_iso(self.at)}}
        data = self.pulse()
        self.assertEqual(self.cell(data)["c"], "work")
        self.assertEqual(data["now"], "engineer · TK-7")

    def test_class_is_fixed_at_cell_time_not_recomputed(self):
        self.ticket(ref="", status="needs_owner")
        self.pulse(self.at)                                     # клетка 52 — owner
        self.ticket(ref="", status="needs_owner")
        for p in T.list_tickets(self.tickets_dir):               # позже владелец ответил, тикеты закрыты
            T.write_header_updates(p, {"status": "done"}, now=self.at)
        later = self.at + timedelta(minutes=30)
        self.state["last_tick"] = T.now_iso(later - timedelta(seconds=15))  # диспетчер тикал всё это время
        data = self.pulse(later)
        self.assertEqual((data["cells"][52]["c"], data["cells"][54]["c"]), ("owner", "none"))

    def test_worst_state_wins_inside_cell(self):
        self.state["dt_flags"] = {"ready_unserved": True}
        self.state["pulse_since"] = {"unserved": T.now_iso(self.at - timedelta(minutes=40))}
        self.ticket(ref="", status="todo")
        self.assertEqual(self.cell(self.pulse(self.at))["c"], "stuck")
        self.state["dt_flags"] = {}
        self.assertEqual(self.cell(self.pulse(self.at + timedelta(minutes=2)))["c"], "stuck")  # та же клетка, лучше не стало


class OwnerTests(PulseBase):
    def test_needs_owner_and_owner_wait_are_owner(self):
        a = self.ticket(ref="", status="needs_owner")
        data = self.pulse()
        self.assertEqual((self.cell(data)["c"], data["state"], data["owner"]), ("owner", "owner", [{"ticket": a, "action": "ответить на вопрос"}]))
        for p in T.list_tickets(self.tickets_dir):
            T.write_header_updates(p, {"status": "waiting", "wait_for": "file:слово владельца"}, now=self.at)
        self.assertEqual(self.pulse()["owner"][0]["action"], "дать слово")

    def test_other_waits_and_blocked_are_not_owner(self):
        self.ticket(ref="", status="blocked")
        for wait in ("host:gpu1:/tmp/x.done", "host:gpu1:/home/owner/x.done", "merged:ownerco/repo#5", "file:/srv/owner/answer.txt"):
            for p in T.list_tickets(self.tickets_dir):
                T.write_header_updates(p, {"status": "waiting", "wait_for": wait}, now=self.at)
            data = self.pulse()
            self.assertEqual((self.cell(data)["c"], data["owner"], data["reasons"]), ("none", [], []), wait)  # «owner» в пути — не слово владельца

    def test_answer_of_owner_is_owner(self):
        self.ticket(ref="", status="todo")
        for p in T.list_tickets(self.tickets_dir):
            T.write_header_updates(p, {"status": "waiting", "wait_for": "file:ответ владельца"}, now=self.at)
        self.assertEqual(self.pulse()["owner"][0]["action"], "дать слово")


class StuckReasonTests(PulseBase):
    def test_dispatcher_dead(self):
        self.state["last_tick"] = T.now_iso(self.at - timedelta(minutes=50))
        data = self.pulse()
        self.assertEqual(self.codes(data), ["dispatcher_dead"])
        self.assertEqual([data["cells"][i].get("why") for i in (49, 50, 51)], ["dispatcher_dead"] * 3)  # молчавшие клетки

    def test_long_tick_is_not_dead(self):
        self.state["last_tick"] = T.now_iso(self.at - timedelta(minutes=4))      # начало прошлого тика, он шёл 4 минуты
        self.state["last_tick_end"] = T.now_iso(self.at - timedelta(seconds=15))
        self.assertEqual(self.pulse()["reasons"], [])
        self.state["last_tick_end"] = T.now_iso(self.at - timedelta(minutes=3))  # а вот тишина после конца тика — мёртв
        self.assertEqual(self.codes(self.pulse()), ["dispatcher_dead"])

    def test_dispatcher_alive_within_threshold(self):
        self.state["last_tick"] = T.now_iso(self.at - timedelta(minutes=1))
        self.assertEqual(self.pulse()["reasons"], [])

    def test_unserved_after_timeout_with_subcause(self):
        self.ticket(ref="", status="todo")
        self.state["dt_flags"] = {"limit_paused": True}
        self.assertEqual(self.pulse()["reasons"], [])            # только что началось — порог не вышел
        self.state["pulse_since"]["unserved"] = T.now_iso(self.at - timedelta(minutes=21))
        data = self.pulse()
        self.assertEqual(self.codes(data), ["unserved"])
        self.assertIn("лимит сессии", data["reasons"][0]["detail"])
        self.assertEqual(self.cell(data), {"i": 52, "n": 0, "c": "stuck", "why": "unserved"})

    def test_unserved_clears_when_flag_drops(self):
        self.state["dt_flags"] = {"throttled": True}
        self.pulse()
        self.state["dt_flags"] = {}
        self.pulse()
        self.assertEqual(self.state["pulse_since"], {})

    def test_step_uncovered_alpha_accepted_step_without_next(self):
        step = self.ticket(status="done", accepted_at=self.at - timedelta(minutes=40))  # alpha 02.11: принят TK-479, следующего нет
        data = self.pulse()
        self.assertEqual(self.codes(data), ["step_uncovered"])
        self.assertIn(step, data["reasons"][0]["detail"])
        self.assertEqual(self.cell(data)["why"], "step_uncovered")

    def test_step_covered_by_next_ticket_is_not_stuck(self):
        self.ticket(status="done", accepted_at=self.at - timedelta(minutes=40))
        self.ticket(status="todo")
        self.assertEqual(self.pulse()["reasons"], [])

    def test_step_uncovered_tail_ceo(self):
        done = self.ticket(ref="", status="done", accepted_at=self.at - timedelta(minutes=40), tail="поправить хвост")
        data = self.pulse()
        self.assertEqual(self.codes(data), ["step_uncovered"])
        self.assertIn(done, data["reasons"][0]["detail"])

    def test_loop_by_returns(self):
        tid = self.ticket(ref="", status="in_progress")
        self.state["review_returns"] = {tid: 2}
        self.assertEqual(self.codes(self.pulse()), ["loop"])
        self.state["review_returns"] = {tid: 1}
        self.assertEqual(self.pulse()["reasons"], [])

    def test_loop_by_failing_restarts(self):
        tid = self.ticket(ref="", status="in_progress")
        self.run_line(20, tid=tid, status="error")
        self.assertEqual(self.pulse()["reasons"], [])
        self.run_line(10, tid=tid, status="error")
        self.assertEqual(self.codes(self.pulse()), ["loop"])

    def test_merged_unreleased_any_default_branch(self):
        for branch in ("main", "master", "develop"):
            (self.disp / "watch-state.json").write_text(json.dumps({"notified": {f"unreleased:{branch}": {"ts": T.now_iso(self.at)}}}), encoding="utf-8")
            data = self.pulse()
            self.assertEqual(self.codes(data), ["merged_unreleased"], branch)
            self.assertIn(branch, data["reasons"][0]["detail"])
        (self.disp / "watch-state.json").write_text(json.dumps({"notified": {"blocked:TK-1": {}}}), encoding="utf-8")
        self.assertEqual(self.pulse()["reasons"], [])

    def test_ci_red_after_threshold_and_only_for_open_ticket_with_pr(self):
        self.settings(ci_repo="o/r")
        tid = self.ticket(ref="", status="in_review")
        T.write_header_updates(self.tickets_dir / f"{tid}.md", {"pr": "9"}, now=self.at)
        (self.disp / "ci-state.json").write_text(json.dumps({"o/r#9": {"sha": "a", "state": "failure", "red": ["pytest"]},
                                                             "o/r#1": {"sha": "b", "state": "failure", "red": ["old"]}}), encoding="utf-8")
        self.assertEqual(self.pulse()["reasons"], [])            # красный только что
        self.state["pulse_since"][f"ci_red:{tid}:9"] = T.now_iso(self.at - timedelta(minutes=31))
        data = self.pulse()
        self.assertEqual(self.codes(data), ["ci_red"])
        self.assertIn("#9", data["reasons"][0]["detail"])        # старый красный PR #1 без тикета не в счёт

    def test_ci_red_on_every_pr_of_list(self):
        self.settings(ci_repo="o/r")
        tid = self.ticket(ref="", status="in_review")
        T.write_header_updates(self.tickets_dir / f"{tid}.md", {"pr": "149, 153"}, now=self.at)   # как пишет tickets.py result pr
        (self.disp / "ci-state.json").write_text(json.dumps({"o/r#149": {"sha": "a", "state": "success", "red": []},
                                                             "o/r#153": {"sha": "b", "state": "failure", "red": ["pytest"]}}), encoding="utf-8")
        self.pulse()
        self.state["pulse_since"][f"ci_red:{tid}:153"] = T.now_iso(self.at - timedelta(minutes=31))
        data = self.pulse()
        self.assertEqual(self.codes(data), ["ci_red"])
        self.assertIn("#153", data["reasons"][0]["detail"])

    def test_ci_conflict(self):
        self.settings(ci_repo="o/r")
        tid = self.ticket(ref="", status="in_review")
        T.write_header_updates(self.tickets_dir / f"{tid}.md", {"pr": "9"}, now=self.at)
        (self.disp / "ci-state.json").write_text(json.dumps({"o/r#9": {"sha": "abc", "state": "success", "red": []}}), encoding="utf-8")
        (self.disp / "merge-state.json").write_text(json.dumps({"o/r#9": {"conflict": "abc"}}), encoding="utf-8")
        self.pulse()
        self.state["pulse_since"][f"ci_red:{tid}:9"] = T.now_iso(self.at - timedelta(minutes=31))
        self.assertIn("конфликт", self.pulse()["reasons"][0]["detail"])

    def test_ci_conflict_names_branch_of_mark(self):
        self.settings(ci_repo="o/r")
        tid = self.ticket(ref="", status="in_review")
        T.write_header_updates(self.tickets_dir / f"{tid}.md", {"pr": "9"}, now=self.at)
        (self.disp / "ci-state.json").write_text(json.dumps({"o/r#9": {"sha": "abc", "state": "success", "red": []}}), encoding="utf-8")
        (self.disp / "merge-state.json").write_text(json.dumps({"o/r#9": {"conflict": "abc", "base": "develop"}}), encoding="utf-8")
        self.pulse()
        self.state["pulse_since"][f"ci_red:{tid}:9"] = T.now_iso(self.at - timedelta(minutes=31))
        detail = self.pulse()["reasons"][0]["detail"]
        self.assertIn("с develop", detail)
        self.assertNotIn("main", detail)

    def test_ci_conflict_old_mark_without_branch_names_no_branch(self):
        self.settings(ci_repo="o/r")
        tid = self.ticket(ref="", status="in_review")
        T.write_header_updates(self.tickets_dir / f"{tid}.md", {"pr": "9"}, now=self.at)
        (self.disp / "ci-state.json").write_text(json.dumps({"o/r#9": {"sha": "abc", "state": "success", "red": []}}), encoding="utf-8")
        (self.disp / "merge-state.json").write_text(json.dumps({"o/r#9": {"conflict": "abc"}}), encoding="utf-8")
        self.pulse()
        self.state["pulse_since"][f"ci_red:{tid}:9"] = T.now_iso(self.at - timedelta(minutes=31))
        detail = self.pulse()["reasons"][0]["detail"]
        self.assertIn("веткой по умолчанию", detail)
        self.assertNotIn("main", detail)

    def test_closed_pr_in_ticket_list_is_not_ci_red(self):
        self.settings(ci_repo="o/r")
        tid = self.ticket(ref="", status="in_review")
        T.write_header_updates(self.tickets_dir / f"{tid}.md", {"pr": "149, 153"}, now=self.at)   # 149 закрыт без влития, 153 — живой
        (self.disp / "ci-state.json").write_text(json.dumps({
            "o/r#149": {"sha": "a", "state": "failure", "red": ["pytest"], "closed": True},
            "o/r#153": {"sha": "b", "state": "success", "red": []}}), encoding="utf-8")
        (self.disp / "merge-state.json").write_text(json.dumps({"o/r#149": {"conflict": "a", "base": "main"}}), encoding="utf-8")
        self.pulse()
        self.state["pulse_since"] = {f"ci_red:{tid}:149": T.now_iso(self.at - timedelta(minutes=99))}
        self.assertEqual(self.pulse()["reasons"], [])

    def test_old_conflict_mark_after_push_is_not_conflict(self):
        self.settings(ci_repo="o/r")
        tid = self.ticket(ref="", status="in_review")
        T.write_header_updates(self.tickets_dir / f"{tid}.md", {"pr": "9"}, now=self.at)
        (self.disp / "ci-state.json").write_text(json.dumps({"o/r#9": {"sha": "new", "state": "success", "red": []}}), encoding="utf-8")
        (self.disp / "merge-state.json").write_text(json.dumps({"o/r#9": {"conflict": "abc"}}), encoding="utf-8")  # метка про старую голову
        self.pulse()
        self.state["pulse_since"] = {f"ci_red:{tid}:9": T.now_iso(self.at - timedelta(minutes=99))}
        self.assertEqual(self.pulse()["reasons"], [])


class PhraseAndFileTests(PulseBase):
    def test_three_phrases(self):
        name = Path(CFG.PROJECT_ROOT).name
        self.assertEqual(self.pulse()["phrase"], "Всё идёт. От вас ничего.")
        tid = self.ticket(ref="", status="needs_owner")
        self.assertEqual(self.pulse()["phrase"], f"Нужны вы: {name} — ответить на вопрос по {tid}.")
        self.ticket(status="done", accepted_at=self.at - timedelta(minutes=40))
        data = self.pulse()
        self.assertTrue(data["phrase"].startswith(f"Застряло: {name} — цель G-001"), data["phrase"])
        self.assertEqual(data["state"], "stuck")

    def test_pause_longest_run_with_reason(self):
        for m in list(range(779, 209, -60)) + [210, 209]:   # работа каждый час до 09:30 включительно; дальше до 13:00 пауза
            self.run_line(m)
        self.state["pulse_cells"] = {"day": self.at.date().isoformat(), "cells": {str(i): "owner" for i in range(39, 52)}}
        pause = self.pulse()["pause"]
        self.assertTrue(pause.startswith("09:45–13:00 · 195 мин"), pause)
        self.assertIn("ждали слова владельца", pause)

    def test_update_writes_one_bounded_file(self):
        PU.update(self.state, self.at)
        size = (self.disp / "pulse.json").stat().st_size
        for h in range(1, 5):  # новые тики и сутки не растят файл: только текущие сутки
            self.state["last_tick"] = T.now_iso(self.at + timedelta(days=h, seconds=-15))
            PU.update(self.state, self.at + timedelta(days=h))
        data = json.loads((self.disp / "pulse.json").read_text(encoding="utf-8"))
        self.assertEqual(len(data["cells"]), 96)
        self.assertLess((self.disp / "pulse.json").stat().st_size, size * 2)
        self.assertEqual(len(self.state["pulse_cells"]["cells"]), 1)

    def test_generated_is_write_time_and_atomic_file_is_valid(self):
        PU.update(self.state, self.at, self.at + timedelta(minutes=3))   # тик шёл 3 минуты, запись в конце
        data = json.loads((self.disp / "pulse.json").read_text(encoding="utf-8"))
        self.assertEqual(T.parse_dt(data["generated"]), self.at + timedelta(minutes=3))
        self.assertFalse(list(self.disp.glob("pulse.json*.tmp")) + list(self.disp.glob("*.tmp")))

    def test_reader_sees_dead_dispatcher_by_stale_file(self):
        PU.update(self.state, self.at)
        self.assertEqual(PU.read(self.at + timedelta(seconds=30))["state"], "ok")
        self.assertEqual(PU.read(self.at + timedelta(minutes=5))["state"], "ok")   # долгий тик — ещё не мёртв
        stale = PU.read(self.at + timedelta(minutes=11))
        self.assertEqual(stale["state"], "stuck")
        self.assertIn("диспетчер не отвечает", stale["phrase"])


class ConfigTests(PulseBase):
    def test_limit_below_one_falls_back_to_default(self):
        self.settings(pulse_dead_min=0, pulse_loop_returns=-3, pulse_ci_red_min=45)
        self.assertEqual((PU.limit("pulse_dead_min"), PU.limit("pulse_loop_returns"), PU.limit("pulse_ci_red_min")), (2, 2, 45))

    def test_keys_known_and_documented(self):
        doc = (PLUGIN / "docs" / "configuration.md").read_text(encoding="utf-8")
        for key in PU.LIMITS:
            self.assertIn(key, rpv_config.SCHEMA if hasattr(rpv_config, "SCHEMA") else doc)
            self.assertIn(f"`{key}`", doc)
        self.assertTrue((PLUGIN / "docs" / "pulse.md").is_file())
