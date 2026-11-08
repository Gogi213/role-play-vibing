"""Потолок возвратов ревью (TK-090, В-36): реплей журнала TK-074 — result → route → тик (escalate_review_limit + decide).
`result pr` сам ставит `next: judge` (routes.route), `ci` пишет после каждой сдачи: потолок обязан сработать при обоих."""
from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

_SANDBOX = tempfile.mkdtemp(prefix="rpv-test-proj-")
os.makedirs(os.path.join(_SANDBOX, ".claude", "roles"))
os.environ["CLAUDE_PROJECT_DIR"] = _SANDBOX
atexit.register(shutil.rmtree, _SANDBOX, True)
sys.path.insert(0, str(Path(__file__).resolve().parent))
import alarms as A  # noqa: E402
import decide as D  # noqa: E402
import finish as F  # noqa: E402
import project_paths as CFG  # noqa: E402
import routes as R  # noqa: E402
import signals as SIG  # noqa: E402
import ticket as T  # noqa: E402
from test_review_cap_patch import patch_review_cap  # noqa: E402  (TK-129: малый потолок вместо настоящего)

TZ = timezone(timedelta(hours=4))
T0 = datetime(2026, 10, 31, 22, 30, tzinfo=TZ)

# причины возвратов Судьи в TK-074 (первые слова записей `[итог: return]`): 6 возвратов по существу и 7-й «пересдай той же головой»;
# возвратов до потолка больше (SIG.MAX_REVIEW_RETURNS), причины идут по кругу
TK074_RETURNS = (
    "Ложный отказ на «Осталось: ничего. Следующий шаг…»",
    "Ложный отказ: «Только что прогнал тесты», «Отложенных пунктов нет»",
    "Ложный отказ: «Отложено: нет», «Ничего не отложено»",
    "Ложный отказ: «Ничего не было отложено», «**Отложено:** нет»",
    "Ложный отказ: «не на потом», «Ничего не перенесено в следующий выпуск»",
    "Ложный отказ «Осталось: приёмка. После слияния: тег в следующем выпуске»",
    "Код годен, по существу принято. Пересдай [итог: pr] той же головой",
)


class ReviewCapReplay(unittest.TestCase):
    def setUp(self):
        patch_review_cap(self)
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self._cfg = {k: getattr(CFG, k) for k in ("TICKETS_DIR", "PROJECT_ROOT", "DISPATCHER_DIR", "STATE_FILE", "CEO_INBOX", "CEO_WAKE_LOG")}
        CFG.TICKETS_DIR, CFG.PROJECT_ROOT, CFG.DISPATCHER_DIR = base / "tickets", base, base / "dispatcher"
        CFG.STATE_FILE, CFG.CEO_INBOX, CFG.CEO_WAKE_LOG = base / "dispatcher" / "state.json", base / "dispatcher" / "ceo-inbox.md", base / "dispatcher" / "ceo-wake.log"
        CFG.TICKETS_DIR.mkdir(parents=True)
        CFG.DISPATCHER_DIR.mkdir(parents=True)
        self.path = T.create_ticket(CFG.TICKETS_DIR, owner="engineer", title="Хвосты", reviewer="judge", now=T0)
        self.state = {}
        self.clock = T0

    def tearDown(self):
        for k, v in self._cfg.items():
            setattr(CFG, k, v)
        self.tmp.cleanup()

    def tick_time(self):
        self.clock += timedelta(minutes=5)
        return self.clock

    def result(self, role, res, text):
        """Запись роли + правка шапки по `routes.route` (как `tickets.py result`)."""
        now = self.tick_time()
        T.append_log(self.path, role, f"[итог: {res}] {text}", now=now)
        T.write_header_updates(self.path, R.route(role, res, "engineer", "judge"), now=now)

    def ci(self):
        T.append_log(self.path, "ci", "CI зелёный: проверь голову", now=self.tick_time())

    def judge_returns(self):
        """Запуск Судьи по `next` и его возврат: тот же учёт, что делает диспетчер после запуска ревьюера."""
        tkt = T.read_ticket(self.path)
        self.assertEqual(D.decide(tkt, self.state, self.clock, lambda s: False).role, "judge")
        T.write_header_updates(self.path, {"next": ""}, now=self.clock)  # tick съедает next при запуске
        n = len([e for e in tkt.log if e.author == "judge"])
        self.result("judge", "return", f"проверка: docs/reviews/r{n}.md — {TK074_RETURNS[n % len(TK074_RETURNS)]}")
        F._count_review_return(self.state, tkt.id, T.read_ticket(self.path), "judge", {"status_at_launch": "in_review"}, True)

    def engineer_resubmits(self, n):
        T.write_header_updates(self.path, {"next": ""}, now=self.clock)  # tick съедает next=engineer при запуске
        self.result("engineer", "pr", f"PR #184@sha{n}")
        self.ci()

    def tick(self):
        tkt = SIG.escalate_review_limit(self.path, T.read_ticket(self.path), self.state, self.tick_time())
        return tkt, D.decide(tkt, self.state, self.clock, lambda s: False)

    def test_cap_fires_after_second_return_even_though_result_pr_sets_next_judge(self):
        self.result("engineer", "pr", "PR #184@sha0")
        self.ci()
        for n in range(SIG.MAX_REVIEW_RETURNS):
            tkt, decision = self.tick()
            self.assertEqual((tkt.status, decision.role), ("in_review", "judge"), f"до потолка Судья получает круг {n + 1}")
            self.judge_returns()
            self.engineer_resubmits(n + 1)
        self.assertEqual(SIG._review_returns(self.state, self.path.stem), SIG.MAX_REVIEW_RETURNS)
        self.assertEqual(T.read_ticket(self.path).next_role, "judge", "result pr поставил next: judge — потолок не за ним")
        tkt, decision = self.tick()
        self.assertEqual(tkt.status, "needs_owner", "после возврата на потолке — вопрос владельцу, не тихое закрытие")
        self.assertIsNone(decision)
        entry = tkt.log[-1]
        self.assertEqual(entry.author, "dispatcher")
        self.assertIn("Потолок возвратов", entry.text)
        for reason in TK074_RETURNS[:2]:
            self.assertIn(reason[:40], entry.text, "в вопросе — причины возвратов, не только последняя")
        self.assertEqual(tkt.next_role, "")

    def test_escalation_is_not_repeated_and_counter_restarts_after_the_owner_answers(self):
        self.result("engineer", "pr", "PR #184@sha0")
        for n in range(SIG.MAX_REVIEW_RETURNS):
            self.ci()
            self.judge_returns()
            self.engineer_resubmits(n + 1)
        self.tick()
        size = len(T.read_ticket(self.path).log)
        tkt, decision = self.tick()
        self.assertEqual((len(tkt.log), decision), (size, None), "повторов записи нет")
        self.assertNotIn(self.path.stem, self.state.get("review_returns", {}), "ответ владельца — новая серия возвратов")

    def _to_cap(self):
        """MAX_REVIEW_RETURNS возвратов и сдача после них: тикет на пороге потолка, `next: judge` от `result pr`."""
        self.result("engineer", "pr", "PR #184@sha0")
        for n in range(SIG.MAX_REVIEW_RETURNS):
            self.ci()
            self.judge_returns()
            self.engineer_resubmits(n + 1)

    def test_ceo_explicit_order_after_submission_is_not_overridden(self):
        self._to_cap()
        T.write_header_updates(self.path, {"next": ""}, now=self.clock)  # тик съел остаток `next: judge` от result pr
        T.append_log(self.path, "ceo", "Судье: проверь ещё раз", now=self.tick_time())
        T.write_header_updates(self.path, {"next": "judge"}, now=self.clock, stamp_updated=False)  # `comment --author ceo --next judge`
        tkt, decision = self.tick()
        self.assertEqual(tkt.status, "in_review")
        self.assertEqual(decision.role, "judge", "заказ CEO потолок не перебивает")

    def test_ceo_comment_without_next_does_not_hide_the_cap(self):
        self._to_cap()
        T.write_header_updates(self.path, {"next": ""}, now=self.clock)
        T.append_log(self.path, "ceo", "наблюдаю", now=self.tick_time())
        tkt, decision = self.tick()
        self.assertEqual((tkt.status, decision), ("in_review", None), "последний ход CEO — не сдача владельца: потолок молчит")

    def test_owner_next_ceo_survives_the_cap_and_the_cap_fires_after_it(self):
        self._to_cap()
        T.write_header_updates(self.path, {"next": "ceo"}, now=self.clock, stamp_updated=False)  # `comment --author engineer --next ceo`
        tkt, decision = self.tick()
        self.assertEqual((tkt.status, tkt.next_role), ("in_review", "ceo"), "позвал CEO — потолок `next` не стирает")
        SIG.handle_next_ceo(self.path, tkt, self.state, self.tick_time())
        self.assertIn("next-ceo", CFG.CEO_INBOX.read_text(encoding="utf-8"), "CEO получил сигнал владельца")
        tkt, decision = self.tick()
        self.assertEqual((tkt.status, decision), ("needs_owner", None), "съеденный `next` — потолок срабатывает следом")

    def test_cap_question_reaches_the_owner_and_the_ceo_line_has_reasons(self):
        self._to_cap()
        tkt, _ = self.tick()
        line = CFG.CEO_INBOX.read_text(encoding="utf-8").strip().splitlines()[-1]
        self.assertIn("[needs_owner]", line)
        for reason in TK074_RETURNS[:2]:
            self.assertIn(reason[:30], line, "в строке CEO — причины возвратов")
        SIG.notify_status_for_ceo(tkt, self.state, self.tick_time())
        self.assertEqual(sum("[needs_owner]" in x for x in CFG.CEO_INBOX.read_text(encoding="utf-8").splitlines()), 1, "одна строка CEO")
        with mock.patch.object(A.notify, "send", return_value=True) as send:
            A.owner_questions(self.state, self.clock)
            A.owner_questions(self.state, self.clock)
        self.assertEqual(send.call_count, 1, "вопрос владельцу — один раз")
        text = send.call_args[0][0]
        self.assertIn("потолок-возвратов", text)
        for reason in TK074_RETURNS[:2]:
            self.assertIn(reason[:30], text, "владелец видит причины возвратов")


if __name__ == "__main__":
    unittest.main()
