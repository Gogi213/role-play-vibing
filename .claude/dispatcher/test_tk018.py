"""Тесты TK-018: после закрытия по слиянию роли не запускаются, повторное закрытие не шлёт второй done, текст закрытия честный.
Живой случай TK-011 (2.0.7): accept → CI зелёный → merge → закрытие диспетчером → запись merge → Судья «Повтор приёмки» → второе закрытие."""
from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dispatch as D  # noqa: E402
import signals as S  # noqa: E402
import ticket as T  # noqa: E402
from test_tk002 import Sandbox, T0, WAIT  # noqa: E402

MERGED_NOTE = "PR #44 влит в main на голове 7a5547b (проверено: merged)."


class SequenceTests(Sandbox):
    def accepted(self):
        p = self.ticket(wait_for=WAIT)
        T.append_log(p, "engineer", "[итог: pr] PR #44@7a5547b — готово", now=T0)
        T.append_log(p, "judge", "[итог: accept] PR #44@7a5547b проверка: docs/reviews/TK-1.md — принято", now=T0 + timedelta(minutes=1))
        return p

    def tick(self, minutes, merged=True):
        from unittest import mock
        import merge_rule as M
        info = {"head": "7a5547b1111", "base": "main", "default": "main"} if merged else {}
        with mock.patch.object(D, "launch_run") as launch, mock.patch.object(M, "merged_done", return_value=merged), \
                mock.patch.object(M, "merged_pr", return_value=info):
            D.tick(T0 + timedelta(minutes=minutes))
        return launch

    def test_merge_note_after_close_does_not_wake_judge(self):
        p = self.accepted()
        self.tick(2, merged=False)  # первый тик ставит базу done
        self.tick(20).assert_not_called()
        self.assertEqual(T.read_ticket(p).status, "done")
        T.append_log(p, "merge", MERGED_NOTE, now=T0 + timedelta(minutes=21))  # merge_rule пишет следом за слиянием
        for m in (22, 23, 40):
            self.tick(m).assert_not_called()
        tkt = T.read_ticket(p)
        self.assertEqual(tkt.status, "done")
        self.assertTrue(S.closed_without_review(tkt))
        self.assertFalse(S._review_pending(tkt))
        self.assertEqual(len(self.wake_lines()), 1)

    def test_repeat_accept_closes_again_without_second_done_signal(self):
        p = self.accepted()
        self.tick(2, merged=False)
        self.tick(20)
        T.append_log(p, "merge", MERGED_NOTE, now=T0 + timedelta(minutes=21))
        # Судья всё же получил запуск и повторил приёмку: ожидание merged снова стоит
        T.append_log(p, "judge", "[итог: accept] PR #44@7a5547b проверка: docs/reviews/TK-1.md — Повтор приёмки: PR уже влит",
                     now=T0 + timedelta(minutes=22))
        T.write_header_updates(p, {"status": "waiting", "wait_for": WAIT}, now=T0 + timedelta(minutes=22))
        self.tick(22, merged=False).assert_not_called()  # тик между приёмкой и закрытием видит waiting и забывает о done
        for m in (23, 24):
            self.tick(m).assert_not_called()
        tkt = T.read_ticket(p)
        self.assertEqual((tkt.status, tkt.header.get("wait_for")), ("done", ""))
        self.assertIn("Повтор", tkt.log[-1].text)
        self.assertEqual(len(self.wake_lines()), 1)

    def test_first_close_is_not_marked_repeat(self):
        p = self.accepted()
        self.tick(20)
        self.assertNotIn("Повтор", T.read_ticket(p).log[-1].text)

    def test_close_text_names_judge_acceptance_not_no_review(self):
        p = self.accepted()
        self.tick(20)
        text = T.read_ticket(p).log[-1].text
        self.assertIn("приёмка Судьи 7a5547b есть", text)
        self.assertNotIn(S.NO_REVIEW_MARK, text)
        self.assertIn(S.CLOSED_BY_MERGE_MARK, text)


class LastMoveTests(Sandbox):
    def done_ticket(self, *entries):
        p = self.ticket(status="done")
        for i, (author, text) in enumerate(entries):
            T.append_log(p, author, text, now=T0 + timedelta(minutes=i))
        return T.read_ticket(p)

    def test_merge_note_after_judge_verdict_does_not_wake_judge(self):
        tkt = self.done_ticket(("engineer", "[итог: done] результат: x — готово"), ("judge", "Вердикт: принято"), ("merge", MERGED_NOTE))
        self.assertEqual(S.last_move(tkt).author, "judge")
        self.assertIsNone(D.decide(tkt, {}, T0))

    def test_merge_note_after_owner_done_still_wakes_judge(self):
        tkt = self.done_ticket(("engineer", "[итог: done] результат: x — готово"), ("merge", MERGED_NOTE))
        decision = D.decide(tkt, {}, T0)
        self.assertEqual((decision.role, decision.reason), ("judge", "review"))

    def test_only_merge_entries(self):
        tkt = self.done_ticket(("merge", MERGED_NOTE))
        self.assertIsNone(S.last_move(T.read_ticket(self.ticket(status="done"))))
        self.assertFalse(S.closed_without_review(tkt))
