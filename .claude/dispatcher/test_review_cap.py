"""Потолок возвратов ревью (TK-090, В-36): реплей журнала TK-074 — result → route → тик (escalate_review_limit + decide).
`result pr` сам ставит `next: judge` (routes.route), `ci` пишет после каждой сдачи: потолок обязан сработать при обоих."""
from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_SANDBOX = tempfile.mkdtemp(prefix="rpv-test-proj-")
os.makedirs(os.path.join(_SANDBOX, ".claude", "roles"))
os.environ["CLAUDE_PROJECT_DIR"] = _SANDBOX
atexit.register(shutil.rmtree, _SANDBOX, True)
sys.path.insert(0, str(Path(__file__).resolve().parent))
import decide as D  # noqa: E402
import finish as F  # noqa: E402
import project_paths as CFG  # noqa: E402
import routes as R  # noqa: E402
import signals as SIG  # noqa: E402
import ticket as T  # noqa: E402

TZ = timezone(timedelta(hours=4))
T0 = datetime(2026, 10, 31, 22, 30, tzinfo=TZ)

# причины возвратов Судьи в TK-074 (первые слова записей `[итог: return]`): 6 возвратов по существу и 7-й «пересдай той же головой»
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
        self.result("judge", "return", f"проверка: docs/reviews/r{n}.md — {TK074_RETURNS[n]}")
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
        for n in range(2):
            tkt, decision = self.tick()
            self.assertEqual((tkt.status, decision.role), ("in_review", "judge"), f"до потолка Судья получает круг {n + 1}")
            self.judge_returns()
            self.engineer_resubmits(n + 1)
        self.assertEqual(SIG._review_returns(self.state, self.path.stem), 2)
        self.assertEqual(T.read_ticket(self.path).next_role, "judge", "result pr поставил next: judge — потолок не за ним")
        tkt, decision = self.tick()
        self.assertEqual(tkt.status, "needs_owner", "после 2-го возврата — вопрос владельцу, не тихое закрытие")
        self.assertIsNone(decision)
        entry = tkt.log[-1]
        self.assertEqual(entry.author, "dispatcher")
        self.assertIn("Потолок возвратов", entry.text)
        for reason in TK074_RETURNS[:2]:
            self.assertIn(reason[:40], entry.text, "в вопросе — причины возвратов, не только последняя")
        self.assertEqual(tkt.next_role, "")

    def test_escalation_is_not_repeated_and_counter_restarts_after_the_owner_answers(self):
        self.result("engineer", "pr", "PR #184@sha0")
        for n in range(2):
            self.ci()
            self.judge_returns()
            self.engineer_resubmits(n + 1)
        self.tick()
        size = len(T.read_ticket(self.path).log)
        tkt, decision = self.tick()
        self.assertEqual((len(tkt.log), decision), (size, None), "повторов записи нет")
        self.assertNotIn(self.path.stem, self.state.get("review_returns", {}), "ответ владельца — новая серия возвратов")

    def test_ceo_explicit_order_after_submission_is_not_overridden(self):
        self.result("engineer", "pr", "PR #184@sha0")
        for n in range(2):
            self.judge_returns()
            self.engineer_resubmits(n + 1)
        T.append_log(self.path, "ceo", "Судье: проверь ещё раз", now=self.tick_time())
        tkt, decision = self.tick()
        self.assertEqual(tkt.status, "in_review")
        self.assertEqual(decision.role, "judge", "заказ CEO потолок не перебивает")


if __name__ == "__main__":
    unittest.main()
