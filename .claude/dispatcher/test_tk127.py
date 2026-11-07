"""TK-127 (В-63): «стоит молча» диспетчер сам отдаёт роли, которая может действовать, — вместо строки CEO.
По тесту на вид: next-ceo, ждёт-ceo, нет-хода, цикл-ожиданий, хвост; плюс передача от Судьи, потолок, выключатель."""
from __future__ import annotations

import contextlib
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import handoff as HO  # noqa: E402
import project as P  # noqa: E402
import project_paths as CFG  # noqa: E402
import signals as SIG  # noqa: E402
import ticket as T  # noqa: E402
import tickets as TK  # noqa: E402
import turn as TURN  # noqa: E402

T0 = T.parse_dt("2026-11-07T12:00:00+04:00")


@contextlib.contextmanager
def turns_off():
    """Прежнее поведение: ключ `role_turns: false` в rpv.json (остальные ключи читаются как есть)."""
    real = P.setting
    with mock.patch.object(P, "setting", lambda key, default=None, root=None: False if key == "role_turns" else real(key, default, root)):
        yield


class TurnBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.dir = base / "tickets"
        self.dir.mkdir()
        (base / "dispatcher").mkdir()
        keys = ("TICKETS_DIR", "PROJECT_ROOT", "DISPATCHER_DIR", "STATE_FILE", "CEO_INBOX", "CEO_WAKE_LOG")
        self._orig = {k: getattr(CFG, k) for k in keys}
        self._orig_tk = TK.TICKETS_DIR
        CFG.TICKETS_DIR, CFG.PROJECT_ROOT, CFG.DISPATCHER_DIR = self.dir, base, base / "dispatcher"
        CFG.STATE_FILE = base / "dispatcher" / "state.json"
        CFG.CEO_INBOX, CFG.CEO_WAKE_LOG = base / "dispatcher" / "ceo-inbox.md", base / "dispatcher" / "ceo-wake.log"
        TK.TICKETS_DIR = self.dir

    def tearDown(self):
        for k, v in self._orig.items():
            setattr(CFG, k, v)
        TK.TICKETS_DIR = self._orig_tk
        self.tmp.cleanup()

    def mk(self, owner="engineer", status="waiting", **header) -> Path:
        path = T.create_ticket(self.dir, owner=owner, title="Тикет", reviewer="judge", now=T0)
        T.write_header_updates(path, {"status": status, **header}, now=T0)
        return path

    def inbox(self) -> str:
        return CFG.CEO_INBOX.read_text(encoding="utf-8") if CFG.CEO_INBOX.exists() else ""

    def turn_entries(self, path, kind):
        return [e for e in T.read_ticket(path).log if e.author == "dispatcher" and e.text.startswith(f"{TURN.MARK} ({kind}):")]


class TurnKindsTests(TurnBase):
    def test_next_ceo_handoff_gives_turn_to_judge(self):
        p = self.mk(next="ceo")
        T.append_log(p, "engineer", "Нужно решение: брать ли второй сервер.", now=T0 + timedelta(seconds=30))
        state = {}
        SIG.handle_next_ceo(p, T.read_ticket(p), state, T0 + timedelta(minutes=1))
        tkt = T.read_ticket(p)
        self.assertEqual(tkt.next_role, "judge")
        self.assertEqual(len(self.turn_entries(p, "next-ceo")), 1)
        self.assertIn("второй сервер", self.turn_entries(p, "next-ceo")[0].text)
        self.assertEqual(self.inbox().count("[next-ceo]"), 1)
        self.assertIn("к сведению", self.inbox())
        self.assertNotIn(tkt.id, state.get("ceo_handoffs", {}))  # CEO мяч не держит

    def test_next_ceo_from_judge_stays_with_ceo(self):
        p = self.mk(next="ceo")
        T.append_log(p, "judge", "Нужен ответ CEO.", now=T0 + timedelta(seconds=30))
        state = {}
        SIG.handle_next_ceo(p, T.read_ticket(p), state, T0 + timedelta(minutes=1))
        self.assertEqual(T.read_ticket(p).next_role, "")
        self.assertEqual(self.turn_entries(p, "next-ceo"), [])
        self.assertIn(T.read_ticket(p).id, state["ceo_handoffs"])
        self.assertNotIn("к сведению", self.inbox())

    def test_next_ceo_handoff_flag_off_keeps_old_behaviour(self):
        p = self.mk(next="ceo")
        T.append_log(p, "engineer", "Нужно решение.", now=T0 + timedelta(seconds=30))
        state = {}
        with turns_off():
            SIG.handle_next_ceo(p, T.read_ticket(p), state, T0 + timedelta(minutes=1))
        self.assertEqual(T.read_ticket(p).next_role, "")
        self.assertEqual(self.turn_entries(p, "next-ceo"), [])
        self.assertIn(T.read_ticket(p).id, state["ceo_handoffs"])

    def test_next_ceo_handoff_cap_per_kind(self):
        p = self.mk(next="ceo")
        for i in range(TURN.MAX_PER_KIND):
            T.append_log(p, "dispatcher", TURN.note("next-ceo", f"раз {i}."), now=T0 + timedelta(seconds=i))
        T.append_log(p, "engineer", "Опять нужно решение.", now=T0 + timedelta(minutes=1))
        state = {}
        SIG.handle_next_ceo(p, T.read_ticket(p), state, T0 + timedelta(minutes=2))
        self.assertEqual(T.read_ticket(p).next_role, "")  # дальше — прежний путь, строка CEO
        self.assertEqual(len(self.turn_entries(p, "next-ceo")), TURN.MAX_PER_KIND)

    def test_ceo_handoff_unanswered_gives_turn_to_judge(self):
        p = self.mk()
        T.append_log(p, "engineer", "Передал CEO.", now=T0 + timedelta(seconds=10))
        tkt = T.read_ticket(p)
        state = {"ceo_handoffs": {tkt.id: T.now_iso(T0)}}
        self.assertTrue(HO._ceo_handoff_pending(tkt, state, T0 + timedelta(minutes=11), p))
        self.assertEqual(T.read_ticket(p).next_role, "judge")
        self.assertEqual(len(self.turn_entries(p, "ждёт-ceo")), 1)
        self.assertEqual(self.inbox().count("[ждёт-ceo]"), 1)
        self.assertNotIn(tkt.id, state["ceo_handoffs"])

    def test_ceo_handoff_unanswered_from_judge_stays_with_ceo(self):
        p = self.mk()
        T.append_log(p, "judge", "Передал CEO.", now=T0 + timedelta(seconds=10))
        tkt = T.read_ticket(p)
        state = {"ceo_handoffs": {tkt.id: T.now_iso(T0)}}
        self.assertTrue(HO._ceo_handoff_pending(tkt, state, T0 + timedelta(minutes=11), p))
        self.assertEqual(T.read_ticket(p).next_role, "")
        self.assertEqual(self.inbox().count("[ждёт-ceo]"), 1)
        self.assertIn(tkt.id, state["ceo_handoffs"])

    def test_no_move_gives_turn_to_owner_with_next(self):
        p = self.mk("engineer", "waiting")  # waiting без wait_for и без next
        out = HO.enforce_move_invariant(p, T.read_ticket(p), {}, T0 + timedelta(minutes=12))
        self.assertEqual((out.status, out.next_role), ("in_progress", "engineer"))
        self.assertEqual(len(self.turn_entries(p, "нет-хода")), 1)
        self.assertIn("waiting без wait_for", self.turn_entries(p, "нет-хода")[0].text)

    def test_no_move_flag_off_wakes_owner_without_next(self):
        p = self.mk("engineer", "waiting")
        with turns_off():
            out = HO.enforce_move_invariant(p, T.read_ticket(p), {}, T0 + timedelta(minutes=12))
        self.assertEqual((out.status, out.next_role), ("in_progress", ""))
        self.assertEqual(self.turn_entries(p, "нет-хода"), [])

    def test_wait_cycle_gives_turn_to_judge(self):
        a, b = self.mk(), self.mk()
        T.write_header_updates(a, {"wait_for": f"ticket:{b.stem}"}, now=T0)
        T.write_header_updates(b, {"wait_for": f"ticket:{a.stem}"}, now=T0)
        HO.notify_wait_cycle(T.read_ticket(a), {}, T0 + timedelta(minutes=30), a)
        self.assertEqual(T.read_ticket(a).next_role, "judge")
        self.assertEqual(len(self.turn_entries(a, "цикл-ожиданий")), 1)
        self.assertIn(b.stem, self.turn_entries(a, "цикл-ожиданий")[0].text)
        self.assertEqual(T.read_ticket(b).next_role, "")  # ход один — на тикете с наименьшим номером
        self.assertIn("цикл-ожиданий", (CFG.DISPATCHER_DIR / "mechanics.log").read_text(encoding="utf-8"))

    def test_tail_after_accept_gives_turn_to_judge(self):
        p = self.mk("engineer", "in_review", reviewer="judge")
        T.append_log(p, "engineer", "[итог: done] сделано.", now=T0 + timedelta(seconds=10))
        args = SimpleNamespace(id=T.read_ticket(p).id, result="accept", pr=None, sha="", left=None)
        TK._write_result(args, p, "judge", T.read_ticket(p), "принято", "", "поменять настройку pulse_push в rpv.json")
        tkt = T.read_ticket(p)
        self.assertEqual((tkt.status, tkt.next_role), ("done", "judge"))
        entries = self.turn_entries(p, "хвост")
        self.assertEqual(len(entries), 1)
        self.assertIn("pulse_push", entries[0].text)
        self.assertEqual(self.inbox().count("[хвост]"), 1)

    def test_tail_empty_or_owner_continues_gives_no_turn(self):
        p = self.mk("engineer", "in_review", reviewer="judge")
        T.append_log(p, "engineer", "[итог: done] сделано.", now=T0 + timedelta(seconds=10))
        args = SimpleNamespace(id=T.read_ticket(p).id, result="accept", pr=None, sha="", left=None)
        TK._write_result(args, p, "judge", T.read_ticket(p), "принято", "", "")
        self.assertEqual((T.read_ticket(p).next_role, self.turn_entries(p, "хвост")), ("", []))
        q = self.mk("engineer", "in_review", reviewer="judge")
        T.append_log(q, "engineer", "[итог: continue] шаг 1.", now=T0 + timedelta(seconds=10))
        TK._write_result(args, q, "judge", T.read_ticket(q), "принято шаг", "", "поменять настройку")
        self.assertEqual(T.read_ticket(q).next_role, "engineer")  # владелец продолжает — ход не перебит
        self.assertEqual(self.turn_entries(q, "хвост"), [])


if __name__ == "__main__":
    unittest.main()
