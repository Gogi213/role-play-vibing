"""CP10: ежедневный отчёт владельцу — результаты, принятые Судьёй, измерения по файлам проекта, срок отправки и доставка.
Без сети и без настоящего claude: пересказ `reporter` подменён, отправка — тоже."""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import dispatch as D  # noqa: E402
import alarms as AL
import project_paths as CFG  # noqa: E402
import registry  # noqa: E402
import report as R  # noqa: E402


def at(day, hour, minute=0):
    return datetime(2026, 10, day, hour, minute).astimezone()


SINCE, UNTIL = at(13, 10), at(14, 10)    # отчёт за сутки: 13.10 10:00 — 14.10 10:00


class ReportCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        env = {k: v for k, v in os.environ.items() if not k.startswith(("RPV_TG_", "RPV_ROLE"))}
        env["CLAUDE_BIN"] = str(self.root / "no-claude")    # настоящего claude тестам не нужно: путь отката
        self.env = mock.patch.dict(os.environ, env, clear=True)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    # --- фикстуры проекта
    def ticket(self, tid, owner="engineer", title="", status="done", updated=None, ref="", log=(), **header):
        """Тикет с логом `[(время, автор, текст)]`; `updated` — когда закрыт."""
        d = self.root / ".claude" / "tickets"
        d.mkdir(parents=True, exist_ok=True)
        head = {"id": tid, "title": title or tid, "owner": owner, "status": status, "reviewer": "judge",
                "updated": (updated or at(13, 9)).isoformat(timespec="seconds"), **header}
        text = "---\n" + "\n".join(f"{k}: {v}" for k, v in head.items()) + "\n---\n\n"
        text += (f"Ссылка на задачу владельца: {ref}\n\n" if ref else "") + "описание\n\n## Лог\n"
        for ts, author, body in log:
            text += f"\n### {ts.isoformat(timespec='seconds')} {author}\n{body}\n"
        (d / f"{tid}.md").write_text(text, encoding="utf-8")

    def runs_log(self, *rows):
        """rows: (конец запуска, тикет, роль, цена, status, reason)."""
        d = self.root / ".claude" / "dispatcher"
        d.mkdir(parents=True, exist_ok=True)
        with (d / "runs.log").open("a", encoding="utf-8") as f:
            for ts, tid, role, cost, status, reason in rows:
                f.write(f"{ts.isoformat(timespec='seconds')} {tid} {role} reason={reason} attempt=0 session=s cost_usd={cost + 9} "
                        f"resolved_cost={cost:.4f} cost_note=diff ticket_spent=0 in_tok=1 out_tok=1 tok_src=json status={status}\n")
        with (d / "runs.log").open("a", encoding="utf-8") as f:
            f.write(f"{at(13, 12).isoformat(timespec='seconds')} TK-001 on_met reason=on_met resolved_cost=0.0000 status=ok\n")

    def write(self, rel, text):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    def reg(self, *args):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(registry.main(["--project", str(self.root), *args]), 0)

    def period(self, since=SINCE, until=UNTIL):
        return R.Period(self.root, since, until, R.load_tickets(self.root))


JUDGE_OK = "[итог: accept] проверка: docs/x.md — расчёт на 12 % быстрее, сверено с отчётом"


class ResultsTests(ReportCase):
    def world(self):
        self.reg("goal", "--title", "Ускорить расчёт", "--quote", "хочу быстрее", "--at", "2026-10-10 12:00")
        self.ticket("TK-001", "researcher", "TK-001: Сравнить способы расчёта", ref="G-001", updated=at(14, 9, 30),
                    log=[(at(14, 9, 20), "judge", JUDGE_OK)])
        self.ticket("TK-002", title="Починить выгрузку (TK-001)", ref="G-001", updated=at(14, 8),
                    log=[(at(13, 12), "judge", "[итог: return] проверка: a.md — не сходится"),
                         (at(14, 8), "judge", "ПРИНЯТО PR #5 на голове abcdef1. Влить."),
                         (at(14, 8), "judge", "[итог: accept] PR #5@abcdef1 — выгрузка за 5 минут")])
        self.ticket("TK-003", title="Убрать лишний шаг", ref="В-1", updated=at(14, 7),
                    log=[(at(14, 6, 55), "judge", "ПРИНЯТО PR #6 на голове abcdef2 (уже влит). Тикет закрыт.")])

    def test_only_closed_and_accepted_by_judge(self):
        self.world()
        self.ticket("TK-004", title="Вернули", status="in_progress", updated=at(14, 9),
                    log=[(at(14, 9), "judge", "[итог: return] проверка: a.md — переделать")])
        self.ticket("TK-005", title="Закрыт вчера", updated=at(13, 9), log=[(at(13, 9), "judge", JUDGE_OK)])
        self.ticket("TK-006", "judge", "Свой тикет Судьи", updated=at(14, 9), log=[(at(14, 9), "judge", JUDGE_OK)])
        self.ticket("TK-007", title="Закрыт CEO без ревью", updated=at(14, 9),
                    log=[(at(14, 9), "ceo", "[итог: done] результат: x.md (закрыто без ревью)")])
        self.ticket("TK-008", title="Принят, но не закрыт", status="waiting", updated=at(14, 9),
                    log=[(at(14, 9), "judge", JUDGE_OK)])
        self.ticket("TK-009", title="Принят, потом возвращён", updated=at(14, 9),
                    log=[(at(14, 8), "judge", JUDGE_OK), (at(14, 9), "judge", "[итог: return] проверка: a.md — нет")])
        groups = R.group_results(self.period())
        self.assertEqual([(g, goal, [i["id"] for i in items]) for g, goal, items in groups],
                         [("G-001", "Ускорить расчёт", ["TK-001", "TK-002"]), ("", "", ["TK-003"])])

    def test_ticket_numbers_stripped_from_titles(self):
        self.world()
        titles = [i["title"] for _, _, items in R.group_results(self.period()) for i in items]
        self.assertEqual(titles, ["Сравнить способы расчёта", "Починить выгрузку", "Убрать лишний шаг"])

    def test_fallback_to_titles_when_claude_is_missing(self):
        self.world()
        text, details = R.build(self.period())        # ask по умолчанию — claude нет (CLAUDE_BIN в окружении теста)
        self.assertIn("• «Ускорить расчёт» — Сравнить способы расчёта; Починить выгрузку", text)
        self.assertIn("• Без цели — Убрать лишний шаг", text)
        self.assertNotRegex(text, r"TK-\d+")
        self.assertIn("G-001: TK-001, TK-002 — строка: названия тикетов", details)

    def test_reporter_summary_used_when_it_passes_the_checks(self):
        self.world()
        prompts = []

        def ask(prompt):
            prompts.append(prompt)
            return {"result": 'Вот ответ:\n```json\n{"1": "Расчёт стал быстрее на 12 %", "2": "Убрали лишний шаг"}\n```',
                    "total_cost_usd": 0.0412}
        text, details = R.build(self.period(), ask=ask)
        self.assertIn("• «Ускорить расчёт» — Расчёт стал быстрее на 12 %", text)
        self.assertIn("• Без цели — Убрали лишний шаг", text)
        self.assertIn("G-001: TK-001, TK-002 — строка: reporter", details)
        self.assertIn("$0.0412", details)
        self.assertEqual(len(prompts), 1)
        self.assertIn("цель «Ускорить расчёт»", prompts[0])
        self.assertIn("(проверяющий: расчёт на 12 % быстрее, сверено с отчётом)", prompts[0])
        self.assertNotRegex(prompts[0], r"TK-\d+")

    def test_invented_number_or_ticket_ref_falls_back_per_group(self):
        self.world()
        # в группе 1 число 40 нет в материале; в группе 2 — номер тикета
        text, _ = R.build(self.period(), ask=lambda p: {"result": json.dumps({"1": "Расчёт быстрее на 40 %", "2": "TK-003 сделан"})})
        self.assertIn("• «Ускорить расчёт» — Сравнить способы расчёта; Починить выгрузку", text)
        self.assertIn("• Без цели — Убрать лишний шаг", text)

    def test_reporter_failure_modes_fall_back(self):
        self.world()
        for bad in (None, {"result": "не json"}, {"result": "[1, 2]"}, {"result": '{"1": ""}'}):
            text, _ = R.build(self.period(), ask=lambda p, b=bad: b)
            self.assertIn("• Без цели — Убрать лишний шаг", text, bad)

    def test_unreadable_results_do_not_stop_the_report(self):
        with mock.patch.object(R, "group_results", side_effect=OSError("диск")):
            text, _ = R.build(self.period())
        self.assertIn("нет данных (принятое не прочитано: OSError)", text)
        self.assertNotIn("за период нет.", text)

    def test_no_results_says_so(self):
        text, details = R.build(self.period())
        self.assertIn("Принятых проверяющим результатов за период нет.", text)
        self.assertIn("принятого нет", details)

    def test_many_goals_keep_message_within_limit(self):
        for n in range(1, 15):
            self.reg("goal", "--title", f"Цель {n}", "--quote", "хочу", "--at", "2026-10-10 12:00")
            self.ticket(f"TK-{n:03d}", title=f"Результат {n}", ref=f"G-{n:03d}", updated=at(14, 9), log=[(at(14, 9), "judge", JUDGE_OK)])
        text, _ = R.build(self.period())
        self.assertLessEqual(len(text.splitlines()), R.MAX_LINES)
        room = R.MAX_LINES - (5 + len(R.MEASURES))  # шапка, две пустые строки, два заголовка частей
        self.assertIn(f"…и ещё {14 - room + 1}", text)


class MeasureTests(ReportCase):
    def test_goal_time_and_trend(self):
        self.reg("goal", "--title", "Ускорить расчёт", "--quote", "хочу", "--at", "2026-10-10 12:00")
        self.reg("goal", "--title", "Старая цель", "--quote", "хочу", "--at", "2026-10-01 09:00")
        self.reg("accept-goal", "G-001", "--at", "2026-10-13 16:00")      # 3 дн 4 ч — в периоде
        self.reg("accept-goal", "G-002", "--at", "2026-10-06 09:00")      # 5 дн — раньше
        self.assertEqual(R.m_goal_time(self.period()), "«Ускорить расчёт» — 3 дн 4 ч; раньше в среднем 5 дн 0 ч — быстрее на 37%")

    def test_goal_time_slower_first_goal_and_no_data(self):
        self.assertIn("нет данных", R.m_goal_time(self.period()))
        self.reg("goal", "--title", "Первая", "--quote", "хочу", "--at", "2026-10-12 10:00")
        self.reg("accept-goal", "G-001", "--at", "2026-10-13 12:00")
        self.assertEqual(R.m_goal_time(self.period()), "«Первая» — 1 дн 2 ч; раньше целей не принималось")
        self.reg("goal", "--title", "Вторая", "--quote", "хочу", "--at", "2026-10-13 10:00")
        self.reg("accept-goal", "G-002", "--at", "2026-10-13 12:00")      # 2 ч; обе цели в периоде — раньше целей нет
        self.assertIn("в среднем 14 ч 00 мин по 2 целям", R.m_goal_time(self.period()))

    def test_deadlines_share(self):
        self.ticket("TK-001", due_first=at(14, 12).isoformat(), log=[(at(14, 9), "judge", JUDGE_OK)])      # в срок
        self.ticket("TK-002", due_first=at(13, 8).isoformat(), log=[(at(14, 8), "judge", JUDGE_OK)])       # принято позже
        self.ticket("TK-003", status="in_progress", due_first=at(13, 9).isoformat())                       # срок прошёл, принятия нет
        self.ticket("TK-004", status="in_progress", due_first=at(20, 9).isoformat())                       # срок впереди — не в счёте
        self.ticket("TK-005", log=[(at(14, 9), "judge", JUDGE_OK)])                                       # срока не называли
        self.assertEqual(R.m_deadlines(self.period()), "в срок 1 из 3 (33%)")

    def test_deadlines_no_data(self):
        self.ticket("TK-001", log=[(at(14, 9), "judge", JUDGE_OK)])
        self.assertIn("нет данных", R.m_deadlines(self.period()))

    def test_owner_questions_by_reason(self):
        self.ticket("TK-001", status="needs_owner", log=[
            (at(13, 11), "judge", "[итог: ask-owner] повод: деньги — купить сервер?"),
            (at(13, 12), "judge", "[итог: ask-owner] повод: бизнес-правило — как считать?"),
            (at(13, 13), "judge", "[итог: ask-owner] повод: деньги — ещё сервер?"),
            (at(13, 14), "judge", "[итог: ask-owner] — без повода"),
            (at(12, 9), "judge", "[итог: ask-owner] повод: направление — вчера"),      # вне периода
            (at(13, 15), "engineer", "[итог: ask-owner] — вопрос Судье, не владельцу")])
        self.assertEqual(R.m_questions(self.period()), "4 (деньги 2, бизнес-правило 1, без повода 1)")

    def test_archived_log_entries_are_counted_too(self):
        self.ticket("TK-001", status="needs_owner", log=[(at(13, 15), "judge", "[итог: ask-owner] повод: направление — куда?")])
        self.write(".claude/tickets/archive/TK-001-log.md",
                   "# TK-001 — архив лога\n\nЗаписи, перенесённые из `.claude/tickets/TK-001.md`.\n\n"
                   f"### {at(13, 11).isoformat(timespec='seconds')} judge\n[итог: ask-owner] повод: деньги — сколько?\n")
        self.assertEqual(R.m_questions(self.period()), "2 (деньги 1, направление 1)")

    def test_owner_questions_zero_and_no_tickets(self):
        self.assertIn("нет данных", R.m_questions(self.period()))
        self.ticket("TK-001")
        self.assertEqual(R.m_questions(self.period()), "0")

    def test_decision_repeats(self):
        self.assertIn("нет данных", R.m_repeats(self.period()))
        self.reg("decide", "--quote", "Только после проверки", "--gist", "проверка", "--at", "2026-10-01 10:00")
        self.reg("decide", "--quote", "Без выходных и праздников", "--gist", "выходные", "--at", "2026-10-02 10:00")
        self.assertEqual(R.m_repeats(self.period()), "0")
        self.reg("repeat", "В-1", "--quote", "ещё раз: только после проверки", "--at", "2026-10-05 10:00")   # раньше периода
        self.reg("repeat", "В-2", "--quote", "я же говорил про выходные", "--at", "2026-10-13 15:00")
        self.reg("repeat", "В-2", "--quote", "опять про выходные", "--at", "2026-10-14 09:00")
        self.assertEqual(R.m_repeats(self.period()), "2 (В-2)")

    def test_regressions_from_refusals_and_re_added_errors(self):
        self.assertIn("нет данных", R.m_regressions(self.period()))
        (self.root / ".claude" / "errors").mkdir(parents=True)
        for eid, title, found in (("E-001", "Цена считается с ошибкой", "2026-10-01 10:00"),
                                  ("E-002", "Цена считается с ошибкой!", "2026-10-13 15:00"),       # заведено заново
                                  ("E-003", "Совсем другое", "2026-10-13 16:00")):
            self.write(f".claude/errors/{eid}.md", f"---\nid: {eid}\ntitle: {title}\nfound: {found}\nstatus: open\n---\n## Что случилось\nх\n")
        self.assertEqual(R.m_regressions(self.period()), "1 (отказов приёмки из-за регресса 0, заведено заново 1)")
        self.write(".claude/dispatcher/regressions.log",
                   f"{at(13, 11).isoformat()}\tTK-002\tE-001,E-004\n{at(12, 11).isoformat()}\tTK-002\tE-001\n{at(14, 9).isoformat()}\tTK-003\tE-001\n")
        self.assertEqual(R.m_regressions(self.period()), "3 (отказов приёмки из-за регресса 2, заведено заново 1)")
        with (self.root / ".claude" / "dispatcher" / "regressions.log").open("a", encoding="utf-8") as f:   # «не запустилась» — не регресс
            f.write(f"{at(13, 12).isoformat()}\tTK-002\tE-001\tinfra\n{at(13, 13).isoformat()}\tTK-002\t\tok\n")
        self.assertEqual(R.m_regressions(self.period()), "3 (отказов приёмки из-за регресса 2, заведено заново 1)")

    def test_idle_minutes_from_state(self):
        self.assertIn("нет данных", R.m_idle(self.period()))
        rec = {"idle_s": 240, "wait_s": 0, "stall_s": 0, "throttle_s": 60, "limit_s": 3600, "alerted": False}
        self.write(".claude/dispatcher/state.json", json.dumps({"downtime": {"2026-10-13": rec, "2026-10-14": {**rec, "idle_s": 120, "throttle_s": 0}}}))
        self.assertEqual(R.m_idle(self.period()), "7 мин (13.10: 5, 14.10: 2)")      # пауза лимита (limit_s) вне счёта
        self.assertEqual(R.m_idle(self.period(at(14, 1), at(14, 10))), "2 мин")
        self.assertIn("нет данных", R.m_idle(self.period(at(20, 1), at(20, 10))))

    def test_jobs_met_and_overdue(self):
        self.assertIn("нет данных", R.m_jobs(self.period()))
        rows = [{"ts": at(13, 11).isoformat(), "ticket": "TK-001", "wait_for": "host:gpu1:/x.rc", "outcome": "met", "rc": 0},
                {"ts": at(13, 12).isoformat(), "ticket": "TK-001", "wait_for": "host:gpu1:/y.rc", "outcome": "met", "rc": 1},
                {"ts": at(13, 13).isoformat(), "ticket": "TK-002", "wait_for": "host:gpu1:/z.rc", "outcome": "overdue", "rc": None},
                {"ts": at(12, 13).isoformat(), "ticket": "TK-002", "wait_for": "host:gpu1:/z.rc", "outcome": "overdue", "rc": None}]
        self.write(".claude/dispatcher/jobs.jsonl", "\n".join(json.dumps(r) for r in rows) + "\nне json\n")
        self.assertEqual(R.m_jobs(self.period()), "кончились и подхвачены 2, срок вышел без итога 1")
        self.assertEqual(R.m_jobs(self.period(at(20, 1), at(20, 10))), "за период заданий не было")

    def test_parallel_conflicts(self):
        self.assertIn("нет данных", R.m_conflicts(self.period()))
        self.ticket("TK-001", log=[
            (at(13, 11), "judge", "[итог: return] проверка: a.md — конфликт с main, сделай rebase"),
            (at(13, 12), "ci", "PR #5 принят на abcdef1, но конфликтует с main: слей main в ветку."),
            (at(13, 13), "engineer", "[итог: blocked] — merge conflict в README"),
            (at(13, 14), "judge", "Заметка: конфликт интересов не усматриваю"),                 # не итог return/blocked и не ci
            (at(12, 11), "judge", "[итог: return] проверка: a.md — конфликт"),                   # вне периода
            (at(13, 15), "judge", "[итог: return] проверка: a.md — числа не сходятся")])        # без слов о конфликте
        self.assertEqual(R.m_conflicts(self.period()), "3")

    def test_plugin_versions_from_claude_cache(self):
        cache = self.root / "cache" / "role-play-vibing"
        for name, days in (("1.0.0", 30), ("1.1.0", 2), ("1.2.0", 1)):
            d = cache / name
            d.mkdir(parents=True)
            old = (UNTIL - timedelta(days=days)).timestamp()
            os.utime(d, (old, old))
        (cache / "не-версия").mkdir()
        with mock.patch.object(R, "PLUGIN_ROOT", cache / "1.2.0"):
            self.assertEqual(R.m_plugin(self.period()), "новых версий: 2 (1.1.0, 1.2.0); сейчас 1.2.0")
        with mock.patch.object(R, "PLUGIN_ROOT", self.root / "role-play-vibing"):
            self.assertIn("нет данных", R.m_plugin(self.period()))

    def test_broken_measure_does_not_stop_report(self):
        boom = mock.Mock(side_effect=RuntimeError("x"))
        with mock.patch.object(R, "MEASURES", (("Сроки", boom, "все"),)):
            text, _ = R.build(self.period())
        self.assertIn("• Сроки · нет данных (RuntimeError) · норма: все", text)

    def test_every_line_with_no_source_says_no_data(self):
        with mock.patch.object(R, "PLUGIN_ROOT", self.root / "role-play-vibing"):
            text, _ = R.build(self.period())      # пустой проект
        self.assertEqual(text.count("нет данных"), len(R.MEASURES))
        for ln in text.splitlines():
            if ln.startswith("• ") and "норма" in ln:
                self.assertRegex(ln, r"^• [^·]+ · \S.* · норма: \S")


class CostTests(ReportCase):
    def world(self):
        """12.00$ за сутки: работа 8.00, переделка 2.00, сбой 1.00, холостой 0.50, повтор 0.50."""
        self.ticket("TK-001", "researcher", log=[
            (at(13, 10, 30), "researcher", "[итог: done] результат: a.md — готово"),
            (at(13, 15), "researcher", "Продолжаю после сбоя")])
        self.ticket("TK-002", "engineer", log=[
            (at(13, 11), "engineer", "[итог: pr] PR #5@abcdef1 — сдано"),
            (at(13, 12), "judge", "[итог: return] проверка: a.md — переделать"),
            (at(13, 13), "engineer", "[итог: pr] PR #5@abcdef2 — исправлено"),
            (at(14, 8), "judge", JUDGE_OK)])
        self.ticket("TK-003", "engineer", log=[(at(14, 7), "judge", "ПРИНЯТО PR #6 на голове abcdef2. Влить.")])

        def end(day, hour, minute):
            return at(day, hour, minute) + timedelta(seconds=20)      # запуск кончается чуть позже своей записи
        self.runs_log(
            (end(13, 10, 30), "TK-001", "researcher", 4.0, "ok", "todo"),        # работа
            (end(13, 11, 30), "TK-001", "researcher", 1.0, "timeout", "next"),   # сбой
            (end(13, 11, 0), "TK-002", "engineer", 1.0, "ok", "todo"),           # работа
            (end(13, 12, 0), "TK-002", "judge", 1.5, "ok", "next"),              # работа Судьи
            (end(13, 13, 0), "TK-002", "engineer", 2.0, "ok", "next"),           # переделка: после возврата, дала итог
            (end(13, 14, 0), "TK-003", "engineer", 0.5, "ok", "todo"),           # холостой: записи роли в тикете нет
            (end(13, 15, 0), "TK-001", "researcher", 0.5, "ok", "retry"),        # повтор
            (end(14, 8, 0), "TK-002", "judge", 1.5, "ok", "next"))               # работа Судьи

    def test_mechanics_share_by_class(self):
        self.world()
        self.assertEqual(R.m_cost(self.period()),
                         "$12.00, на механику 33% (сбои 8%, холостые запуски 4%, повторы 4%, переделки после возврата Судьи 17%)")

    def test_cost_is_resolved_cost_not_session_total(self):
        self.world()
        runs = R.read_runs(self.root)
        self.assertEqual(sum(r["cost"] for r in runs), 12.0)      # в строках cost_usd на 9 больше: итог сессии, не запуска
        self.assertNotIn("on_met", {r["role"] for r in runs})

    def test_rework_ends_with_owners_next_result_and_ignores_continue(self):
        self.ticket("TK-001", "engineer", log=[
            (at(13, 10, 5), "judge", "[итог: return] проверка: a.md — переделать"),
            (at(13, 11), "engineer", "[итог: continue] — дальше"),
            (at(13, 12), "engineer", "[итог: pr] PR #5@abcdef1 — сдано"),
            (at(13, 13), "engineer", "[итог: pr] PR #6@abcdef2 — ещё")])
        self.runs_log(*((at(13, h, 0) + timedelta(seconds=20), "TK-001", "engineer", 1.0, "ok", "next") for h in (11, 12, 13)))
        runs = R.read_runs(self.root)
        R.label_runs(runs, R.load_tickets(self.root))
        self.assertEqual([r["kind"] for r in runs], ["переделки после возврата Судьи"] * 2 + ["работа"])

    def test_no_data_cases(self):
        self.assertIn("нет данных", R.m_cost(self.period()))
        self.runs_log((at(10, 10), "TK-001", "researcher", 1.0, "ok", "todo"))
        self.assertIn("нет данных", R.m_cost(self.period()))      # запуск не в периоде
        self.runs_log((at(13, 20), "TK-001", "researcher", 0.0, "ok", "todo"))
        self.assertEqual(R.m_cost(self.period()), "$0.00, запусков: 1")


class ClaudeCallTests(ReportCase):
    def test_no_claude_means_none(self):
        self.assertIsNone(R.ask_reporter("x"))      # CLAUDE_BIN указывает в никуда

    def test_command_uses_agent_definition_from_plugin(self):
        seen = {}

        def runner(cmd, **kw):
            seen["cmd"], seen["kw"] = cmd, kw
            seen["agents"] = json.loads(Path(cmd[cmd.index("--agents") + 1]).read_text(encoding="utf-8"))
            return mock.Mock(returncode=0, stdout=json.dumps({"result": '{"1": "ок"}', "total_cost_usd": 0.05}).encode())
        out = R.ask_reporter("запрос", runner=runner, claude="fake-claude")
        self.assertEqual(out["result"], '{"1": "ок"}')
        cmd = seen["cmd"]
        self.assertEqual(cmd[:2], ["fake-claude", "-p"])
        self.assertEqual(cmd[cmd.index("--agent") + 1], "reporter")
        self.assertEqual(cmd[cmd.index("--tools") + 1], "")
        self.assertEqual(seen["kw"]["input"], "запрос".encode("utf-8"))
        agent = seen["agents"]["reporter"]
        self.assertEqual(cmd[cmd.index("--model") + 1], agent["model"])       # модель — из agents/reporter.md
        self.assertTrue(agent["model"].startswith("claude-"))
        self.assertIn("ежедневный отчёт", agent["prompt"])
        self.assertFalse(any(k.startswith("RPV_") for k in seen["kw"]["env"]))

    def test_failures_mean_none(self):
        import subprocess
        for runner in (mock.Mock(side_effect=subprocess.TimeoutExpired("claude", 1)), mock.Mock(side_effect=OSError("нет")),
                       mock.Mock(return_value=mock.Mock(returncode=1, stdout=b"{}")),
                       mock.Mock(return_value=mock.Mock(returncode=0, stdout="не json".encode("utf-8"))),
                       mock.Mock(return_value=mock.Mock(returncode=0, stdout=json.dumps({"is_error": True, "result": "x"}).encode()))):
            self.assertIsNone(R.ask_reporter("x", runner=runner, claude="fake-claude"))


class ScheduleTests(unittest.TestCase):
    def test_due_fires_once_at_or_after_the_time(self):
        yesterday = at(13, 10) + timedelta(seconds=10)
        self.assertFalse(R.due(yesterday.isoformat(), at(14, 9, 59), "10:00"))                 # раньше срока — нет
        self.assertTrue(R.due(yesterday.isoformat(), at(14, 10, 0), "10:00"))                  # в срок — да
        sent = at(14, 10, 0) + timedelta(seconds=5)
        self.assertFalse(R.due(sent.isoformat(), at(14, 10, 1), "10:00"))                     # уже ушёл — не повторяется
        self.assertFalse(R.due(sent.isoformat(), at(14, 23, 59), "10:00"))
        self.assertFalse(R.due(sent.isoformat(), at(15, 9, 59), "10:00"))
        self.assertTrue(R.due(sent.isoformat(), at(15, 10, 0), "10:00"))                      # завтра — снова

    def test_catch_up_after_downtime_is_sent_once(self):
        last = (at(13, 10) + timedelta(seconds=5)).isoformat()      # вчерашний отчёт; сегодня в 10:00 диспетчера не было
        self.assertTrue(R.due(last, at(14, 13, 0), "10:00"))
        self.assertFalse(R.due(at(14, 13, 0).isoformat(), at(14, 13, 1), "10:00"))             # ушёл на первом тике, дальше нет

    def test_time_setting(self):
        last = at(13, 12).isoformat()
        self.assertFalse(R.due(last, at(14, 7, 59), "08:00"))
        self.assertTrue(R.due(last, at(14, 8, 0), "8:00"))
        self.assertTrue(R.due(last, at(14, 10, 0), "25:99"))      # не разобрать — 10:00
        self.assertFalse(R.due(last, at(14, 9, 59), "мусор"))


class DispatcherTriggerTests(unittest.TestCase):
    """`_daily_report` диспетчера: метка в state.json, один процесс `report.py`, тик его не ждёт."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.orig = CFG.PROJECT_ROOT
        CFG.configure_project(self.root)
        AL._REPORT_PROC = None
        self.procs = []

        class Proc:
            def __init__(s, cmd, kw):
                s.cmd, s.kw, s.code = cmd, kw, None

            def poll(s):
                return s.code
        self.patch = mock.patch.object(D, "_popen", side_effect=lambda cmd, **kw: self.procs.append(Proc(cmd, kw)) or self.procs[-1])
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        AL._REPORT_PROC = None
        CFG.configure_project(self.orig)
        self.tmp.cleanup()

    def test_first_tick_only_sets_the_mark(self):
        state = {}
        D._daily_report(state, at(14, 12))
        self.assertEqual(state["last_report_at"], at(14, 12).isoformat(timespec="seconds"))
        self.assertEqual(self.procs, [])
        D._daily_report(state, at(15, 9, 59))           # и до срока завтрашнего дня — тоже ничего
        self.assertEqual(self.procs, [])

    def test_launches_once_at_the_time_as_a_separate_process(self):
        last = (at(13, 10) + timedelta(seconds=5)).isoformat(timespec="seconds")
        state = {"last_report_at": last}
        D._daily_report(state, at(14, 9, 59))
        self.assertEqual(self.procs, [])
        now = at(14, 10) + timedelta(seconds=3)
        D._daily_report(state, now)
        self.assertEqual(len(self.procs), 1)
        cmd = self.procs[0].cmd
        self.assertEqual(Path(cmd[1]).name, "report.py")
        self.assertEqual(cmd[cmd.index("--project") + 1], str(self.root))
        self.assertEqual((cmd[cmd.index("--since") + 1], cmd[cmd.index("--until") + 1]), (last, now.isoformat(timespec="seconds")))
        self.assertEqual(state["last_report_at"], now.isoformat(timespec="seconds"))      # метка — сразу, не по концу процесса
        D._daily_report(state, now + timedelta(seconds=15))
        D._daily_report(state, at(14, 18))
        self.assertEqual(len(self.procs), 1)

    def test_catch_up_launches_once(self):
        state = {"last_report_at": (at(13, 10) + timedelta(seconds=5)).isoformat(timespec="seconds")}
        D._daily_report(state, at(14, 13))      # диспетчер стоял в 10:00 — отчёт на первом тике после
        self.procs[0].code = 0                  # отчёт кончился; метка уже позже срока — снова не уйдёт
        D._daily_report(state, at(14, 13, 1))
        D._daily_report(state, at(14, 14))
        self.assertEqual(len(self.procs), 1)

    def test_running_report_is_not_started_twice(self):
        state = {"last_report_at": at(13, 9).isoformat(timespec="seconds")}
        D._daily_report(state, at(14, 10, 1))
        state["last_report_at"] = at(13, 9).isoformat(timespec="seconds")      # метку откатили — процесс всё ещё идёт
        D._daily_report(state, at(14, 10, 2))
        self.assertEqual(len(self.procs), 1)

    def test_tick_survives_report_failure_and_keeps_the_mark(self):
        now = at(14, 12)
        with mock.patch.object(D, "_daily_report", side_effect=RuntimeError("x")), redirect_stderr(io.StringIO()) as err:
            D.tick(now)
        self.assertIn("отчёт: RuntimeError", err.getvalue())
        self.assertTrue(D.load_state()["last_tick"])
        D.tick(now + timedelta(seconds=15))
        self.assertEqual(D.load_state()["last_report_at"], (now + timedelta(seconds=15)).isoformat(timespec="seconds"))


class DeliveryTests(ReportCase):
    def world(self):
        self.ticket("TK-001", title="Результат", updated=at(14, 9), log=[(at(14, 9), "judge", JUDGE_OK)])

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = R.main(["--project", str(self.root), "--since", SINCE.isoformat(), "--until", UNTIL.isoformat(), *args])
        return code, out.getvalue(), err.getvalue()

    def test_dry_run_prints_only(self):
        self.world()
        with mock.patch.object(R.notify, "send", side_effect=AssertionError("dry-run не шлёт")):
            code, out, _ = self.cli("--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("Принято проверяющим:", out)
        self.assertIn("• Без цели — Результат", out)
        self.assertFalse((self.root / ".claude" / "dispatcher" / "reports").exists())
        self.assertFalse((self.root / ".claude" / "dispatcher" / "state.json").exists())      # метку отчёта скрипт не двигает

    def test_telegram_not_configured_still_writes_the_file(self):
        self.world()
        code, out, _ = self.cli()
        self.assertEqual(code, 1)
        self.assertIn("не отправлено", out)
        f = self.root / ".claude" / "dispatcher" / "reports" / "2026-10-14.md"
        body = f.read_text(encoding="utf-8")
        self.assertIn("• Без цели — Результат", body)
        self.assertIn("Источники принятого", body)
        self.assertIn("без цели: TK-001", body)      # номера тикетов — только в файле, для запроса владельца
        self.assertIn("не отправлено (нет RPV_TG_TOKEN/RPV_TG_CHAT)", (self.root / ".claude" / "dispatcher" / "notify.log").read_text(encoding="utf-8"))

    def test_sends_the_message_text_once_when_configured(self):
        self.world()
        sent = []
        with mock.patch.object(R.notify, "send", side_effect=lambda text, project=None, **kw: sent.append((text, project)) or True):
            code, out, _ = self.cli()
        self.assertEqual(code, 0)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][1], self.root)
        self.assertNotIn("Источники принятого", sent[0][0])
        self.assertLessEqual(len(sent[0][0].splitlines()), R.MAX_LINES)
        self.assertTrue((self.root / ".claude" / "dispatcher" / "reports" / "2026-10-14.md").exists())

    def test_goes_out_through_telegram_with_the_whole_text(self):
        self.world()
        calls = []

        class Resp(io.BytesIO):
            def __enter__(s):
                return s

            def __exit__(s, *a):
                return False

        def opener(req, timeout):
            calls.append(json.loads(req.data.decode("utf-8")))
            return Resp(b'{"ok": true}')
        text, details = R.build(self.period())
        with mock.patch.dict(os.environ, {"RPV_TG_TOKEN": "1:abc", "RPV_TG_CHAT": "251"}):
            f, ok = R.deliver(self.root, text, details, UNTIL, send=lambda t, p: R.notify.send(t, p, opener=opener))
        self.assertTrue(ok)
        self.assertEqual(calls[0]["chat_id"], "251")
        self.assertEqual(calls[0]["text"], text)

    def test_unwritable_file_does_not_stop_sending(self):
        self.world()
        self.write(".claude/dispatcher/reports", "это файл, а не каталог")      # каталог отчётов не создать
        sent = []
        with mock.patch.object(R.notify, "send", side_effect=lambda text, project=None, **kw: sent.append(text) or True):
            code, out, err = self.cli()
        self.assertEqual((code, len(sent)), (0, 1))
        self.assertIn("файл отчёта не записан", err)
        self.assertIn("файл: не записан", out)

    def test_default_period_starts_at_last_report_mark(self):
        self.assertEqual(R.default_since(self.root, UNTIL), UNTIL - timedelta(days=1))
        self.write(".claude/dispatcher/state.json", json.dumps({"last_report_at": at(12, 10, 5).isoformat()}))
        self.assertEqual(R.default_since(self.root, UNTIL), at(12, 10, 5))

    def test_bad_period_is_refused(self):
        code, _, err = self.cli("--since", UNTIL.isoformat())
        self.assertEqual(code, 2)
        self.assertIn("начало раньше конца", err)

    def test_message_is_plain_and_short(self):
        self.world()
        text, _ = R.build(self.period(), ask=lambda p: {"result": json.dumps({"1": "Сделан результат"})})
        self.assertLessEqual(len(text.splitlines()), R.MAX_LINES)
        self.assertLess(len(text), 4000)       # влезает в одно сообщение Telegram
        self.assertTrue(text.startswith("Отчёт за 13.10 10:00 — 14.10 10:00"))
        self.assertNotRegex(text, r"TK-\d+")


if __name__ == "__main__":
    unittest.main()
