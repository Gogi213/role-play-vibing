"""TK-086 (В-33): сторож `goal-idle` — у открытой цели принят шаг, а открытого тикета на неё нет: CEO будят даже при живом
постороннем тикете; тикет без цели сигнала не дает; один раз на событие; причина — в ceo-wake.log."""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_watch import WatchSandbox, CFG, T, W  # noqa: E402 — песочница сторожа
import goal_idle as GI  # noqa: E402
import project as P  # noqa: E402
import rpv_config  # noqa: E402
import signals as SIG  # noqa: E402

PLUGIN = Path(__file__).resolve().parents[2]


class GoalSandbox(WatchSandbox):
    def setUp(self):
        super().setUp()
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        base = Path(self.root.name)
        (base / ".claude" / "goals").mkdir(parents=True)
        old_root, old_p = CFG.PROJECT_ROOT, P._ROOT
        CFG.PROJECT_ROOT = base
        P.set_root(base)
        self.addCleanup(setattr, CFG, "PROJECT_ROOT", old_root)
        self.addCleanup(P.set_root, old_p)
        self.goal("G-001")

    def goal(self, gid, status="open"):
        (Path(self.root.name) / ".claude" / "goals" / f"{gid}.md").write_text(
            f"---\nid: {gid}\nstatus: {status}\nset: 2026-11-01 10:00\naccepted:\n---\n# Цель {gid}\n", encoding="utf-8")

    def ticket(self, ref="G-001", status="in_progress", accepted_at=None, owner="engineer", tail="", desc="", title="шаг", after="", legacy=False):
        path = T.create_ticket(self.tickets_dir, owner, title, reviewer="judge", status=status,
                               description=(f"Ссылка на задачу владельца: {ref}\n\n" if ref else "") + desc, now=self.now)
        if accepted_at is not None:
            shown = tail if legacy else f"«{tail}»"  # как пишет `tickets._tail_ack` (legacy — запись до TK-098, без кавычек)
            note = f" хвост CEO: {shown}" if tail else " хвосты: нет"
            T.append_log(path, "judge", f"[итог: accept] PR #1@abcdef0{note}{after} — принято", now=accepted_at)  # after — поля приёмки после хвоста
        return T.read_ticket(path).id

    def events(self, minutes=0, ws=None):
        ws = {} if ws is None else ws
        return GI.goal_idle_events(ws, self.now + timedelta(minutes=minutes)), ws


class GoalIdleTests(GoalSandbox):
    def test_accepted_step_without_next_ticket_signals_despite_foreign_live_ticket(self):
        step = self.ticket(status="done", accepted_at=self.now)
        self.ticket(ref="", status="in_progress")  # посторонний живой тикет без цели
        out, _ = self.events(30)
        self.assertEqual([(g, t) for g, t, _ in out], [("G-001", step)])
        self.assertIn("следующего шага нет", out[0][2])

    def test_next_ticket_of_the_goal_covers_it(self):
        self.ticket(status="done", accepted_at=self.now)
        self.ticket(status="todo")
        self.assertEqual(self.events(30)[0], [])

    def test_done_without_goal_gives_no_signal(self):
        self.ticket(ref="", status="done", accepted_at=self.now)
        self.assertEqual(self.events(30)[0], [])

    def test_goal_without_accepted_step_gives_no_signal(self):
        self.ticket(status="done")  # закрыт без приёмки Судьи
        self.assertEqual(self.events(30)[0], [])

    def test_closed_goal_gives_no_signal(self):
        self.goal("G-002", status="accepted")
        self.ticket(ref="G-002", status="done", accepted_at=self.now)
        self.assertEqual(self.events(30)[0], [])

    def test_below_threshold_silent_then_fires(self):
        self.ticket(status="done", accepted_at=self.now)
        self.assertEqual(self.events(GI.GOAL_IDLE_MIN - 1)[0], [])
        self.assertEqual(len(self.events(GI.GOAL_IDLE_MIN)[0]), 1)

    def test_threshold_key(self):
        (Path(self.root.name) / ".claude" / "rpv.json").write_text(json.dumps({"goal_idle_min": 45}), encoding="utf-8")
        self.ticket(status="done", accepted_at=self.now)
        self.assertEqual(self.events(30)[0], [])
        self.assertEqual(len(self.events(45)[0]), 1)

    def test_once_per_event_no_timer_repeat(self):
        self.ticket(status="done", accepted_at=self.now)
        out, ws = self.events(30)
        self.assertEqual(len(out), 1)
        self.assertEqual(self.events(31, ws)[0], [])
        self.assertEqual(self.events(60 * 30, ws)[0], [])

    def test_new_accepted_step_is_a_new_event(self):
        self.ticket(status="done", accepted_at=self.now)
        _, ws = self.events(30)
        second = self.ticket(status="done", accepted_at=self.now + timedelta(minutes=40))
        out, _ = self.events(60, ws)
        self.assertEqual([t for _, t, _ in out], [second])

    def test_unreadable_ticket_is_silent(self):
        self.ticket(status="done", accepted_at=self.now)
        (self.tickets_dir / "TK-999.md").write_bytes(b"\xff\xfe\x00 not a ticket")
        self.assertEqual(self.events(30)[0], [])

    def test_run_once_posts_reason_in_wake_log(self):
        step = self.ticket(status="done", accepted_at=self.now)
        posted = W.run_once(self.now + timedelta(minutes=30), probes=False)
        self.assertEqual([f.kind for f in posted], ["goal-idle"])
        line = next(ln for ln in CFG.CEO_WAKE_LOG.read_text(encoding="utf-8").splitlines() if "watch-goal-idle" in ln)
        for must in ("G-001", step, "следующего шага нет"):
            self.assertIn(must, line)
        self.assertIn("[watch-goal-idle]", CFG.CEO_INBOX.read_text(encoding="utf-8"))
        self.assertEqual(SIG.classify_signal("watch-goal-idle"), "wake")
        self.assertEqual(W.run_once(self.now + timedelta(minutes=31), probes=False), [])  # второго сигнала нет

    def test_other_wake_lines_stay_short(self):
        SIG.append_ceo_inbox("TK-1", "done", "длинное описание", self.now)
        self.assertTrue(CFG.CEO_WAKE_LOG.read_text(encoding="utf-8").rstrip().endswith("TK-1 done"))


class TailIdleTests(GoalSandbox):
    """Хвост `--tail ceo:` без цели: принято, а тикета на хвост нет — событие (запрос Судьи к TK-086)."""
    TAIL = "поставить настройку в rpv.json"

    def setUp(self):
        super().setUp()
        self.step = self.ticket(ref="В-33", status="done", accepted_at=self.now, tail=self.TAIL)

    def test_tail_without_ticket_signals_without_goal(self):
        out, _ = self.events(30)
        self.assertEqual([(g, t) for g, t, _ in out], [(GI.TAIL_GOAL, self.step)])
        self.assertIn("тикета на хвост нет", out[0][2])
        self.assertIn(self.TAIL, out[0][2])

    def test_tail_signals_despite_foreign_live_ticket(self):
        self.ticket(ref="", status="in_progress", desc="посторонний тикет")
        self.assertEqual(len(self.events(30)[0]), 1)

    def test_tail_below_threshold_silent_then_fires(self):
        self.assertEqual(self.events(GI.GOAL_IDLE_MIN - 1)[0], [])
        self.assertEqual(len(self.events(GI.GOAL_IDLE_MIN)[0]), 1)

    def test_tail_covered_by_ticket_naming_it(self):
        self.ticket(ref="В-33", status="todo", desc=f"Хвост {self.step}: {self.TAIL}")
        self.assertEqual(self.events(30)[0], [])

    def test_tail_not_covered_by_ticket_naming_another(self):
        self.ticket(ref="В-33", status="todo", desc=f"Хвост {self.step}0: другое")  # TK-N0 — не TK-N
        self.assertEqual(len(self.events(30)[0]), 1)

    def test_tail_cleared_by_ceo_entry_after_accept(self):
        path = self.tickets_dir / f"{self.step}.md"
        T.append_log(path, "ceo", "хвост снят: владелец отказался", now=self.now + timedelta(minutes=5))
        self.assertEqual(self.events(30)[0], [])

    def test_ceo_entry_without_word_does_not_clear(self):
        T.append_log(self.tickets_dir / f"{self.step}.md", "ceo", "смотрю", now=self.now + timedelta(minutes=5))
        self.assertEqual(len(self.events(30)[0]), 1)

    def test_tail_dash_is_not_a_tail(self):
        self.ticket(ref="В-33", status="done", accepted_at=self.now)  # `--tail -`: хвостов нет
        out, _ = self.events(30)
        self.assertEqual([t for _, t, _ in out], [self.step])

    def test_tail_once_per_event_and_forgotten_when_covered(self):
        out, ws = self.events(30)
        self.assertEqual(len(out), 1)
        self.assertEqual(self.events(31, ws)[0], [])
        self.assertEqual(self.events(60 * 30, ws)[0], [])
        self.ticket(ref="В-33", status="todo", desc=f"Хвост {self.step}")
        self.assertEqual(self.events(60 * 31, ws)[0], [])
        self.assertEqual(ws[GI.SEEN_KEY], {})

    def test_tail_reason_in_wake_log(self):
        posted = W.run_once(self.now + timedelta(minutes=30), probes=False)
        self.assertEqual([f.kind for f in posted], ["goal-idle"])
        line = next(ln for ln in CFG.CEO_WAKE_LOG.read_text(encoding="utf-8").splitlines() if "watch-goal-idle" in ln)
        for must in (self.step, "хвост CEO", self.TAIL, "тикета на хвост нет"):
            self.assertIn(must, line)
        self.assertEqual(W.run_once(self.now + timedelta(minutes=31), probes=False), [])


class TailRealEntryTests(GoalSandbox):
    """Хвост в записи приёмки, как её пишет Судья (TK-082, TK-068): после текста идут поля приёмки; замечания критика к TK-086."""
    TAIL82 = "синк устава проекта .claude/roles/ceo.md с шаблоном и выпуск с CHANGELOG для всех проектов где стоит плагин сразу везде"  # 120 знаков, как предел `tickets._tail_ack`
    FIELDS82 = " критик: docs/reviews/TK-082-critic.md (пунктов 5, не держится 0) проверки договора: 2 кмд, код 0"

    def wake_line(self, minutes=30):
        W.run_once(self.now + timedelta(minutes=minutes), probes=False)
        return next(ln for ln in CFG.CEO_WAKE_LOG.read_text(encoding="utf-8").splitlines() if "watch-goal-idle" in ln)

    def test_text_is_the_tail_only_and_wake_log_keeps_minutes(self):
        step = self.ticket(ref="В-33", status="done", accepted_at=self.now, tail=self.TAIL82, after=self.FIELDS82)
        out, _ = self.events(30)
        self.assertIn(f"«{self.TAIL82}»", out[0][2])
        self.assertNotIn("критик", out[0][2])
        self.assertNotIn("проверки договора", out[0][2])
        line = self.wake_line()
        for must in (step, "тикета на хвост нет", "30 мин"):  # срез причины в ceo-wake.log (150 знаков) их не теряет
            self.assertIn(must, line)

    def test_live_field_ends_the_tail_and_dash_inside_stays(self):
        tail = "сверить, что токен бота отозван (E-012; исследователь пишет — отозван)"
        self.ticket(ref="В-33", status="done", accepted_at=self.now, tail=tail, after=" вживую: docs/reviews/tk-068-live.md")
        self.assertIn(f"«{tail}»", self.events(30)[0][0][2])

    TAIL59 = "ложный хвост: accept считает «Осталось: ничего» хвостом — завести тикет Инженеру на детектор"  # TK-059.md:41

    def test_dash_inside_tail_without_fields_keeps_full_text(self):
        """TK-098: запись без полей приёмки, в хвосте « — » — режет не по тире, а по кавычкам; вывод Судьи после них не входит."""
        tail = self.TAIL59.replace("«Осталось: ничего»", "'Осталось: ничего'")
        step = self.ticket(ref="В-33", status="done", accepted_at=self.now, tail=tail)
        out, _ = self.events(30)
        self.assertIn(f"«{tail}»", out[0][2])
        self.assertNotIn("принято", out[0][2])
        self.assertEqual(GI._tail_text(T.read_ticket(self.tickets_dir / f"{step}.md").log[-1].text), tail)

    def test_tail_text_round_trip_with_tail_ack(self):
        """Что пишет `tickets._tail_ack`, то читает `_tail_text`: «»-кавычки внутри заменены, срез 120 знаков, поля и вывод Судьи — снаружи."""
        import tickets as TK
        what = self.TAIL59 + " и ещё слов, чтобы текст перешёл за сто двадцать знаков — хвост после тире"
        args = type("A", (), {"tail": f'ceo:"{what}"'})()
        with mock.patch.object(TK.registry_mod, "decisions_file", return_value=Path(__file__)):
            err, note, full = TK._tail_ack(args, "TK-999")
        self.assertEqual((err, full), ("", what))
        entry = f"[итог: accept] PR #1@abcdef0{note} вживую: docs/x.md — принято"
        self.assertEqual(GI._tail_text(entry), what[:120].replace("«", '"').replace("»", '"').strip())

    def test_legacy_entry_without_quotes_still_cut_by_dash_and_fields(self):
        self.ticket(ref="В-33", status="done", accepted_at=self.now, tail="поставить настройку в rpv.json", legacy=True)
        self.assertIn("«поставить настройку в rpv.json»", self.events(30)[0][0][2])
        self.assertEqual(GI._tail_text("[итог: accept] PR #1@abc хвост CEO: сделать А — и Б — принято"), "сделать А")

    def test_mention_of_the_ticket_is_not_a_tail_ticket(self):
        step = self.ticket(ref="В-33", status="done", accepted_at=self.now, tail="поставить настройку в rpv.json")
        self.ticket(ref="В-33", status="todo", title=f"Релиз по итогам {step}", desc=f"выпуск после {step}")  # упомянут, но не хвост
        self.assertEqual([t for _, t, _ in self.events(30)[0]], [step])

    def test_title_form_covers_the_tail(self):
        step = self.ticket(ref="В-33", status="done", accepted_at=self.now, tail="поставить настройку в rpv.json")
        self.ticket(ref="В-33", status="todo", title=f"Хвост {step}: настройка в rpv.json")
        self.assertEqual(self.events(30)[0], [])

    def test_only_tail_cleared_or_closed_by_ceo_clears(self):
        step = self.ticket(ref="В-33", status="done", accepted_at=self.now, tail="поставить настройку в rpv.json")
        path = self.tickets_dir / f"{step}.md"
        for i, text in enumerate(("хвостов нет", "хвост остаётся, думаю", "хвост не снят")):
            T.append_log(path, "ceo", text, now=self.now + timedelta(minutes=1 + i))
        self.assertEqual(len(self.events(30)[0]), 1)
        T.append_log(path, "ceo", "Хвост закрыт: сделано вручную", now=self.now + timedelta(minutes=10))
        self.assertEqual(self.events(30)[0], [])


class GoalIdleDocsTests(WatchSandbox):
    def test_key_is_valid_and_documented(self):
        check, _ = rpv_config.KEYS["goal_idle_min"]
        self.assertTrue(check(10))
        self.assertFalse(check(0))
        self.assertFalse(check(True))
        self.assertIn("goal_idle_min", (PLUGIN / "docs" / "configuration.md").read_text(encoding="utf-8"))

    def test_signals_doc_row(self):
        text = (PLUGIN / "docs" / "signals.md").read_text(encoding="utf-8")
        row = next(ln for ln in text.splitlines() if ln.startswith("| `watch-goal-idle`"))
        for must in ("`goal_idle_min`", "один раз на событие", "ceo-wake.log", "будит", "`--tail ceo:`", "тикета на хвост", "«Хвост TK-N»", "«хвост снят»"):
            self.assertIn(must, row)
