"""Тесты TK-027: закрытие по слиянию не теряет шаг после слияния из приёмки Судьи или заказа.
Живой случай TK-026 (выпуск 2.0.8, 27.10): приёмка «Дальше: влить после CI, тег v2.0.8» → слияние → диспетчер закрыл тикет, тега нет."""
from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import decide as DEC  # noqa: E402
import dispatch as D  # noqa: E402
import merge_rule as M  # noqa: E402
import ticket as T  # noqa: E402
import tickets as TK  # noqa: E402
from test_merge_rule import SHA, MergeRuleBase  # noqa: E402
from test_tk002 import T0, WAIT, Sandbox  # noqa: E402


class AfterMergeStepTests(Sandbox):
    """Последовательность TK-026: pr → accept → слияние; с шагом после слияния тикет не закрывают, будят автора."""

    def accepted_with(self, tail, order=""):
        p = self.ticket(wait_for=WAIT)
        if order:  # строка в заказе — в описании тикета, над «## Лог»
            p.write_text(p.read_text(encoding="utf-8").replace("\n## Лог", f"\n{order}\n\n## Лог", 1), encoding="utf-8")
        T.append_log(p, "engineer", "[итог: pr] PR #44@7a5547b — готово. Дальше: приёмка, влив, тег", now=T0)
        T.append_log(p, "judge", f"[итог: accept] PR #44@7a5547b проверка: docs/reviews/TK-1.md — принято{tail}", now=T0 + timedelta(minutes=1))
        return p

    def tick(self, minutes):
        info = {"head": "7a5547b1111", "base": "main", "default": "main"}
        with mock.patch.object(D, "launch_run") as launch, mock.patch.object(M, "merged_done", return_value=True),                 mock.patch.object(M, "merged_pr", return_value=info):
            D.tick(T0 + timedelta(minutes=minutes))
        return launch

    def assert_author_woken(self, p):
        launch = self.tick(20)
        tkt = T.read_ticket(p)
        self.assertEqual(tkt.status, "waiting")
        self.assertNotEqual(tkt.log[-1].author, "dispatcher")
        launch.assert_called_once()
        self.assertEqual(launch.call_args.args[1], "engineer")
        self.assertEqual(launch.call_args.kwargs["reason"], "after-merge-step")
        self.assertEqual(self.wake_lines(), [])

    def test_step_in_accept_wakes_author_instead_of_closing(self):
        self.assert_author_woken(self.accepted_with(" после слияния: тег v2.0.8, push тега"))

    def test_step_line_in_order_wakes_author(self):
        self.assert_author_woken(self.accepted_with("", order="после слияния: тег v2.0.8"))

    def test_step_in_bulleted_order_line(self):
        self.assert_author_woken(self.accepted_with("", order="- После слияния: выкладка на стенд"))

    def test_free_text_next_step_is_not_a_field(self):  # без явного поля закрытие прежнее: поле — единственный признак
        p = self.accepted_with(". Дальше: влить после CI")
        self.assertEqual(self.tick(20).call_count, 0)
        self.assertEqual(T.read_ticket(p).status, "done")

    def test_no_step_still_closes(self):
        p = self.accepted_with("")
        self.assertEqual(self.tick(20).call_count, 0)
        self.assertEqual(T.read_ticket(p).status, "done")

    def test_empty_step_is_no_step(self):
        self.assertEqual(DEC._step_after_merge(T.read_ticket(self.accepted_with(" после слияния:")), 1), "")


class AcceptWritesAfterMergeTests(MergeRuleBase):
    def accept(self, **extra):
        args = type("A", (), {"id": self.tid, "pr": 7, "sha": SHA, "text": "", "decisions": "-", "numbers": "-", "tail": "-", **extra})()
        with mock.patch.object(TK, "_caller_role", return_value="judge"):  # роль сессии прогона тестов не мешает
            self.assertEqual(TK.cmd_accept(args), 0)
        return self.tkt()

    def test_after_merge_goes_to_verdict_in_one_line(self):
        t = self.accept(after_merge="тег v2.0.8\n  push  тега")
        self.assertTrue(t.log[-1].text.rstrip().endswith("после слияния: тег v2.0.8 push тега"))
        self.assertEqual(DEC._step_after_merge(t, len(t.log) - 1), "тег v2.0.8 push тега")
        self.assertEqual((t.status, t.header.get("wait_for")), ("waiting", "merged:o/r#7"))

    def test_without_after_merge_verdict_has_no_field(self):
        t = self.accept()
        self.assertEqual(DEC._step_after_merge(t, len(t.log) - 1), "")
