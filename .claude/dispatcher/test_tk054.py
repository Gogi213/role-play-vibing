"""TK-054 (В-18): `decide` не запускает роль на waiting с невыполненным условием, пока сторож видит задание живым."""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import decide as DEC  # noqa: E402
import ticket as T  # noqa: E402

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)


def tkt(status="waiting", nxt="", wait_for="host:calc:/d/x.done", owner="researcher"):
    hdr = {"id": "TK-900", "owner": owner, "status": status, "next": nxt, "wait_for": wait_for}
    return T.Ticket(path=Path("TK-900.md"), header=hdr, description="", log=[])


class AliveHoldTests(unittest.TestCase):
    def d(self, t, met=False, alive=True):
        return DEC.decide(t, {}, NOW, lambda spec: met, (lambda _t: alive) if alive is not None else None)

    def test_alive_unmet_wait_gives_no_run_for_any_reason(self):
        self.assertIsNone(self.d(tkt()))                       # ждёт и жив
        self.assertIsNone(self.d(tkt(nxt="researcher")))       # next: поставлен в ожидании — держится в шапке
        self.assertIsNone(self.d(tkt(status="waiting", nxt="researcher"), alive=True))

    def test_met_wait_gives_exactly_one_run(self):
        got = self.d(tkt(), met=True)
        self.assertEqual((got.role, got.reason), ("researcher", "wait_for-met"))
        got = self.d(tkt(nxt="researcher"), met=True)
        self.assertEqual((got.role, got.reason), ("researcher", "next"))  # next сработал и очищается — ровно один запуск

    def test_dead_job_is_not_held(self):
        # упало/пропало: сторож снимает ожидание (in_progress, пустой wait_for) и отметки «жив» у тикета нет
        got = self.d(tkt(status="in_progress", wait_for=""), alive=True)
        self.assertEqual((got.role, got.reason), ("researcher", "in_progress-resume"))
        got = self.d(tkt(nxt="researcher"), alive=False)
        self.assertEqual(got.reason, "next")

    def test_without_alive_hook_old_behaviour(self):
        got = DEC.decide(tkt(nxt="researcher"), {}, NOW, lambda spec: False)
        self.assertEqual(got.reason, "next")

    def test_dispatch_flag_off_restores_old(self):
        import dispatch as D
        import project as P
        import tempfile, json
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".claude").mkdir()
            (Path(tmp) / ".claude" / "rpv.json").write_text(json.dumps({"hold_alive_waits": False}), encoding="utf-8")
            old = P._ROOT
            P.set_root(tmp)
            try:
                self.assertFalse(D._wait_alive(tkt()))
            finally:
                P.set_root(old)


if __name__ == "__main__":
    unittest.main()
