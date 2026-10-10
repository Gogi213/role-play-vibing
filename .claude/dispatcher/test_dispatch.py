"""Юнит-тесты диспетчера — без сети, без настоящего `claude`. stdlib `unittest`.

Запуск:
    python -m unittest discover -s ".claude/dispatcher" -p "test_dispatch.py" -v
или из каталога `.claude/dispatcher`:
    python -m unittest test_dispatch -v
"""
from __future__ import annotations

import atexit
import contextlib
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import types
import tempfile
import textwrap
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

# проект-пустышка с `.claude/roles` (после /rpv-init): тесты не касаются ни папки плагина, ни рабочего проекта выше неё
_SANDBOX = tempfile.mkdtemp(prefix="rpv-test-proj-")
os.makedirs(os.path.join(_SANDBOX, ".claude", "roles"))
os.environ["CLAUDE_PROJECT_DIR"] = _SANDBOX
atexit.register(shutil.rmtree, _SANDBOX, True)
os.environ["RPV_BUS_DISABLE"] = "1"  # тесты не шлют события на живую шину
sys.path.insert(0, str(Path(__file__).resolve().parent))
import dispatch as D  # noqa: E402
import project as P  # noqa: E402
import ticket as T  # noqa: E402
import tickets as TK  # noqa: E402  (CLI — new/comment/start/status)

TZ = timezone(timedelta(hours=4))  # GMT+4 (память проекта)


def python_image_name() -> str:
    """Подстрока имени образа, под которым ОС видит наш python (фейковая «claude» в тестах — это он). Не stem от
    sys.executable: у framework-python на macOS образ — .../Python.app/Contents/MacOS/Python («Python»), не «python3.14»."""
    return (D._proc_comm(os.getpid()) if os.name != "nt" else None) or Path(sys.executable).stem


def dt(s: str) -> datetime:
    return T.parse_dt(s)


class TicketParsingTests(unittest.TestCase):
    def test_parse_header_and_log(self):
        text = textwrap.dedent("""\
            ---
            id: TK-001
            title: Пример задачи
            owner: researcher
            status: todo
            reviewer: judge
            wait_for:
            updated: 2026-09-27T10:00:00+04:00
            ---

            Описание задачи, свободный текст.

            ## Лог

            ### 2026-09-27T10:05:00+04:00 researcher
            Сделал шаг раз. Дальше — шаг два. @judge посмотри план.

            ### 2026-09-27T10:10:00+04:00 judge
            Ок, план принят.
            """)
        tkt = T.parse_text(text, Path("TK-001.md"))
        self.assertEqual(tkt.id, "TK-001")
        self.assertEqual(tkt.owner, "researcher")
        self.assertEqual(tkt.status, "todo")
        self.assertEqual(tkt.reviewer, "judge")
        self.assertEqual(tkt.header.get("wait_for"), "")
        self.assertIn("Описание задачи", tkt.description)
        self.assertEqual(len(tkt.log), 2)
        self.assertEqual(tkt.log[0].author, "researcher")
        self.assertEqual(tkt.log[0].mentions, {"judge"})
        self.assertEqual(tkt.log[1].author, "judge")
        self.assertTrue(tkt.logged_since("judge", dt("2026-09-27T10:06:00+04:00")))
        self.assertFalse(tkt.logged_since("judge", dt("2026-09-27T10:11:00+04:00")))

    def test_no_header_raises(self):
        with self.assertRaises(ValueError):
            T.parse_text("нет шапки тут")

    def test_header_has_no_budget_field_and_stray_budget_line_is_ignored(self):
        """В-173: поля `budget` в формате тикета нет — `create_ticket` его не пишет; шапка без поля и (на всякий
        случай) со случайной старой строкой `budget:` разбирается, поле игнорируется (диспетчер его не читает)."""
        with tempfile.TemporaryDirectory() as d:
            path = T.create_ticket(Path(d), owner="engineer", title="Без бюджета")
            self.assertNotIn("budget", path.read_text(encoding="utf-8").split("\n---\n", 1)[0])
        text = ("---\nid: TK-009\nowner: engineer\nstatus: todo\nbudget: 25\nupdated: 2026-09-27T10:00:00+04:00\n"
                "---\n\nОписание.\n")
        tkt = T.parse_text(text, Path("TK-009.md"))
        self.assertEqual((tkt.id, tkt.owner, tkt.status), ("TK-009", "engineer", "todo"))

    def test_no_log_section_is_empty(self):
        text = ("---\nid: TK-002\nowner: engineer\nstatus: todo\nupdated: 2026-09-27T10:00:00+04:00\n---\n\n"
                "Описание.\n")
        tkt = T.parse_text(text, Path("TK-002.md"))
        self.assertEqual(tkt.log, [])
        self.assertEqual(tkt.description, "Описание.")

    def test_log_raw_captures_whole_section_including_headerless_lines(self):
        """CEO 27.09, TK-005: роли пишут без заголовка `###` — log_raw должен содержать их текст,
        даже если `_parse_log` (по заголовкам) их не разобрал как LogEntry."""
        text = ("---\nid: TK-005\nowner: engineer\nstatus: in_progress\nupdated: 2026-09-27T23:00:00+04:00\n"
                "---\n\n## Лог\n\n- 27.09 ~23:50 (инженер, запуск 1) сделал шаг, дальше доделать\n")
        tkt = T.parse_text(text, Path("TK-005.md"))
        self.assertEqual(tkt.log, [])  # ни одной по-настоящему разобранной записи — заголовка нет
        self.assertIn("сделал шаг", tkt.log_raw)


class RoleEntryKeysTests(unittest.TestCase):
    """v2 (02.10): «роль оставила запись» — по заголовку `### <ts> <роль>`, не по росту секции лога и не по
    @упоминаниям (раньше — MentionsSinceTests/mentions_since: упоминания будили роли, 77 % запусков)."""

    def mk(self, log):
        text = ("---\nid: X\nowner: engineer\nstatus: todo\nupdated: 2026-10-02T10:00:00+04:00\n---\n\nописание\n\n"
                "## Лог\n" + log)
        return T.parse_text(text, Path("X.md"))

    def test_keys_only_for_entries_with_role_heading(self):
        tkt = self.mk("\n### 2026-10-02T10:01:00+04:00 engineer\nсделал\n\n### 2026-10-02T10:02:00+04:00 judge\nок\n"
                      "\n- 02.10 10:03 (инженер) строка без заголовка\n")
        self.assertEqual(T.role_entry_keys(tkt, "engineer"), ["2026-10-02T10:01:00+04:00 engineer"])
        self.assertEqual(T.role_entry_keys(tkt, "judge"), ["2026-10-02T10:02:00+04:00 judge"])
        self.assertEqual(T.role_entry_keys(tkt, "researcher"), [])

    def test_author_with_suffix_still_matches_role(self):
        tkt = self.mk("\n### 2026-10-02T10:01:00+04:00 engineer (запуск 2)\nсделал\n")
        self.assertEqual(len(T.role_entry_keys(tkt, "engineer")), 1)

    def test_mentions_inside_text_are_plain_text_for_keys(self):
        tkt = self.mk("\n### 2026-10-02T10:01:00+04:00 researcher\n@judge глянь, @ceo @engineer\n")
        self.assertEqual(T.role_entry_keys(tkt, "judge"), [])
        self.assertEqual(T.role_entry_keys(tkt, "ceo"), [])

    def test_next_role_and_effort_properties(self):
        text = ("---\nid: X\nowner: engineer\nstatus: waiting\nnext: Judge\neffort: Medium\n"
                "updated: 2026-10-02T10:00:00+04:00\n---\n\n## Лог\n")
        tkt = T.parse_text(text, Path("X.md"))
        self.assertEqual(tkt.next_role, "judge")
        self.assertEqual(tkt.effort, "medium")
        bad = T.parse_text(text.replace("next: Judge", "next: somebody").replace("effort: Medium", "effort: max"),
                           Path("X.md"))
        self.assertEqual((bad.next_role, bad.effort), ("", ""))
        empty = T.parse_text(text.replace("next: Judge", "next:").replace("effort: Medium\n", ""), Path("X.md"))
        self.assertEqual((empty.next_role, empty.effort), ("", ""))

    def test_create_ticket_writes_effort_line(self):
        with tempfile.TemporaryDirectory() as d:
            path = T.create_ticket(Path(d), owner="engineer", title="С усилием", effort="low")
            self.assertEqual(T.read_ticket(path).effort, "low")
            self.assertEqual(T.read_ticket(T.create_ticket(Path(d), owner="engineer", title="Без")).effort, "")


class LogCompactionTests(unittest.TestCase):
    """v2: файл тикета > 20 КБ → всё, кроме последних 8 записей, — дословно в archive/<ID>-log.md; детекция
    «новой записи роли» от компакции не зависит (заголовки, не смещения)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.path = T.create_ticket(self.dir, owner="engineer", title="Длинный лог",
                                    description="Описание задачи.", now=dt("2026-10-02T09:00:00+04:00"))

    def tearDown(self):
        self.tmp.cleanup()

    def add_entries(self, n, size=600, start=0, author="engineer"):
        for i in range(start, start + n):
            T.append_log(self.path, author, f"запись №{i} " + ("текст " * (size // 6)),
                         now=dt("2026-10-02T10:00:00+04:00") + timedelta(minutes=i))

    def test_small_ticket_is_not_touched(self):
        self.add_entries(12, size=100)
        before = self.path.read_text(encoding="utf-8")
        self.assertEqual(T.compact_log(self.path), 0)
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)

    def test_compacts_to_last_8_and_archives_verbatim(self):
        self.add_entries(20)
        full = T.read_ticket(self.path)
        self.assertGreater(len(self.path.read_text(encoding="utf-8").encode("utf-8")), T.LOG_COMPACT_BYTES)
        moved = T.compact_log(self.path)
        self.assertEqual(moved, 12)
        after = T.read_ticket(self.path)
        self.assertEqual([e.ts_raw for e in after.log], [e.ts_raw for e in full.log[-8:]])
        self.assertIn("Описание задачи.", after.description)
        archive = self.dir / "archive" / f"{self.path.stem}-log.md"
        arch_text = archive.read_text(encoding="utf-8")
        for e in full.log[:12]:  # старые записи — дословно, в исходном порядке
            self.assertIn(f"### {e.ts_raw} {e.author}\n{e.text}", arch_text)
        for e in full.log[12:]:
            self.assertNotIn(f"### {e.ts_raw} {e.author}", arch_text)
        # одна строка-указатель в логе; сам файл теперь меньше порога
        self.assertEqual(self.path.read_text(encoding="utf-8").count(T.ARCHIVE_POINTER_PREFIX), 1)
        self.assertLess(len(self.path.read_text(encoding="utf-8").encode("utf-8")), T.LOG_COMPACT_BYTES)

    def test_second_compaction_appends_and_keeps_single_pointer(self):
        self.add_entries(20)
        T.compact_log(self.path)
        self.add_entries(14, start=20)
        moved2 = T.compact_log(self.path)
        self.assertEqual(moved2, 14)
        text = self.path.read_text(encoding="utf-8")
        self.assertEqual(text.count(T.ARCHIVE_POINTER_PREFIX), 1)
        self.assertEqual(len(T.read_ticket(self.path).log), 8)
        arch_text = (self.dir / "archive" / f"{self.path.stem}-log.md").read_text(encoding="utf-8")
        self.assertEqual(arch_text.count("\n### "), 12 + 14)  # дозапись, а не перезапись
        self.assertNotIn(T.ARCHIVE_POINTER_PREFIX, arch_text)  # указатель в архив не уносится
        self.assertIn("запись №0 ", arch_text)
        self.assertIn("запись №25 ", arch_text)

    def test_archive_is_not_listed_as_ticket_and_no_tmp_left(self):
        self.add_entries(20)
        T.compact_log(self.path)
        self.assertEqual([p.name for p in T.list_tickets(self.dir)], [self.path.name])
        self.assertEqual([p.name for p in self.dir.glob("*.tmp")], [])

    def test_comment_cli_compacts_after_append(self):
        self.add_entries(19)
        orig_dir = TK.TICKETS_DIR
        TK.TICKETS_DIR = self.dir
        try:
            rc = TK.main(["comment", self.path.stem, "--author", "engineer", "--text", "новая " + "текст " * 250])
        finally:
            TK.TICKETS_DIR = orig_dir
        self.assertEqual(rc, 0)
        self.assertEqual(len(T.read_ticket(self.path).log), 8)
        self.assertTrue((self.dir / "archive" / f"{self.path.stem}-log.md").exists())

    def test_seen_entries_detection_survives_compaction(self):
        """Снимок ключей записей роли на старте запуска + компакция лога + новая запись роли = «записала»;
        компакция сама (старые записи ушли) «записью» не считается."""
        self.add_entries(20)
        keys_at_launch = T.role_entry_keys(T.read_ticket(self.path), "engineer")
        info = {"role": "engineer", "started": dt("2026-10-02T10:00:00+04:00"), "log_keys_at_launch": keys_at_launch}
        T.compact_log(self.path)  # лог укоротился на 12 записей
        self.assertFalse(D._role_logged(T.read_ticket(self.path), "engineer", info))
        self.add_entries(1, start=40)  # роль дописала новую запись (ей же компакция уже не нужна)
        self.assertTrue(D._role_logged(T.read_ticket(self.path), "engineer", info))
        # запись ДРУГОГО автора ролью не засчитывается
        other = dict(info, log_keys_at_launch=T.role_entry_keys(T.read_ticket(self.path), "engineer"))
        T.append_log(self.path, "ceo", "комментарий", now=dt("2026-10-02T12:00:00+04:00"))
        T.append_log(self.path, "dispatcher", "служебная", now=dt("2026-10-02T12:01:00+04:00"))
        self.assertFalse(D._role_logged(T.read_ticket(self.path), "engineer", other))

    def test_legacy_active_run_without_keys_falls_back_to_start_time(self):
        self.add_entries(2, size=50)  # записи 10:00 и 10:01
        info = {"role": "engineer", "started": dt("2026-10-02T10:00:30+04:00")}  # зеркало до v2: ключей нет
        self.assertTrue(D._role_logged(T.read_ticket(self.path), "engineer", info))  # 10:01 ≥ старта
        info["started"] = dt("2026-10-02T10:30:00+04:00")
        self.assertFalse(D._role_logged(T.read_ticket(self.path), "engineer", info))

    def test_identical_entries_in_same_second_both_count(self):
        T.append_log(self.path, "engineer", "раз", now=dt("2026-10-02T10:00:00+04:00"))
        keys = T.role_entry_keys(T.read_ticket(self.path), "engineer")
        T.append_log(self.path, "engineer", "раз", now=dt("2026-10-02T10:00:00+04:00"))
        self.assertTrue(D._role_logged(T.read_ticket(self.path), "engineer", {"log_keys_at_launch": keys}))


class TicketLockTests(unittest.TestCase):
    """A8 (аудит 03.10): записи тикета — атомарно (tmp + os.replace) и под файловой блокировкой на тикет: диспетчер,
    `tickets.py comment/new/start` и роли пишут один файл из разных процессов — без блокировки правки терялись."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.path = T.create_ticket(self.dir, owner="engineer", title="Гонка", now=dt("2026-10-03T09:00:00+04:00"))

    def tearDown(self):
        self.tmp.cleanup()

    def run_threads(self, targets):
        import threading
        threads = [threading.Thread(target=t) for t in targets]
        for th in threads:
            th.start()
        for th in threads:
            th.join(120)
        self.assertFalse(any(th.is_alive() for th in threads), "поток завис (взаимная блокировка?)")

    def test_concurrent_appends_lose_no_entries(self):
        def writer(n):
            def run():
                for i in range(15):
                    T.append_log(self.path, "engineer", f"запись-{n}-{i}", now=dt("2026-10-03T09:10:00+04:00"))
            return run
        self.run_threads([writer(n) for n in range(6)])
        text = self.path.read_text(encoding="utf-8")
        for n in range(6):
            for i in range(15):
                self.assertEqual(text.count(f"запись-{n}-{i}\n"), 1, (n, i))
        self.assertEqual(len(T.read_ticket(self.path).log), 90)

    def test_concurrent_header_updates_and_appends_keep_both(self):
        def appender():
            for i in range(30):
                T.append_log(self.path, "engineer", f"шаг-{i}", now=dt("2026-10-03T09:10:00+04:00"))

        def header():
            for i in range(30):
                T.write_header_updates(self.path, {"status": "in_progress" if i % 2 else "waiting", "next": ""},
                                        now=dt("2026-10-03T09:11:00+04:00"))
        self.run_threads([appender, header, appender])
        tkt = T.read_ticket(self.path)
        self.assertEqual(len(tkt.log), 60)
        self.assertEqual(tkt.status, "waiting" if 29 % 2 == 0 else "in_progress")

    def test_concurrent_create_gives_unique_ids(self):
        ids = []

        def creator():
            ids.append(T.create_ticket(self.dir, owner="engineer", title="Параллельная").stem)
        self.run_threads([creator for _ in range(8)])
        self.assertEqual(len(set(ids)), 8, ids)
        self.assertEqual(len(list(self.dir.glob("TK-*.md"))), 9)

    def test_writes_go_through_atomic_write_text(self):
        calls = []
        orig = T.atomic_write_text

        def spy(path, text, *a, **kw):
            calls.append(Path(path).name)
            return orig(path, text, *a, **kw)
        T.atomic_write_text = spy
        try:
            T.write_header_updates(self.path, {"status": "waiting"})
            T.append_log(self.path, "engineer", "запись")
            created = T.create_ticket(self.dir, owner="engineer", title="Новая")
        finally:
            T.atomic_write_text = orig
        self.assertEqual(calls.count(self.path.name), 2)
        self.assertIn(created.name, calls)

    def test_failed_replace_leaves_original_intact(self):
        before = self.path.read_text(encoding="utf-8")
        orig_replace = os.replace

        def boom(*a, **kw):
            raise OSError("диск отвалился")
        os.replace = boom
        try:
            with self.assertRaises(OSError):
                T.append_log(self.path, "engineer", "не должно записаться")
        finally:
            os.replace = orig_replace
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)
        self.assertEqual([p.name for p in self.dir.iterdir() if p.suffix == ".tmp" or ".tmp" in p.name], [])

    def test_lock_is_reentrant_in_one_thread(self):
        with T.ticket_lock(self.path):
            with T.ticket_lock(self.path):
                T.append_log(self.path, "engineer", "внутри")
        self.assertEqual(len(T.read_ticket(self.path).log), 1)

    def test_lock_excludes_another_process(self):
        import subprocess
        script = (
            "import sys, time\n"
            f"sys.path.insert(0, {str(Path(T.__file__).parent)!r})\n"
            "import ticket as T\n"
            "from pathlib import Path\n"
            f"p = Path({str(self.path)!r})\n"
            "t0 = time.time()\n"
            "T.append_log(p, 'engineer', 'из дочернего процесса')\n"
            "print(round(time.time() - t0, 2))\n")
        with T.ticket_lock(self.path):
            proc = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
            time.sleep(1.5)
            self.assertIsNone(proc.poll(), "дочерний процесс обязан ждать блокировку")
        out, _ = proc.communicate(timeout=30)
        self.assertGreaterEqual(float(out.strip()), 0.5)  # t0 — после импортов дочернего; на нагруженной машине они долгие
        self.assertIn("из дочернего процесса", self.path.read_text(encoding="utf-8"))

    def test_cli_comment_and_start_use_the_lock(self):
        orig_tickets_dir = TK.TICKETS_DIR
        TK.TICKETS_DIR = self.dir
        self.addCleanup(lambda: setattr(TK, "TICKETS_DIR", orig_tickets_dir))
        held = []
        orig = T.ticket_lock

        def spy(path, *a, **kw):
            held.append(Path(path).name)
            return orig(path, *a, **kw)
        T.ticket_lock = spy
        try:
            self.assertEqual(TK.main(["comment", self.path.stem, "--author", "engineer", "--text", "через CLI"]), 0)
        finally:
            T.ticket_lock = orig
        self.assertIn(self.path.name, held)


class TicketMutationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_create_next_id_write_header_append_log(self):
        p1 = T.create_ticket(self.dir, owner="researcher", title="Первая", now=dt("2026-09-27T09:00:00+04:00"))
        self.assertEqual(p1.stem, "TK-001")
        p2 = T.create_ticket(self.dir, owner="engineer", title="Вторая", now=dt("2026-09-27T09:00:00+04:00"))
        self.assertEqual(p2.stem, "TK-002")

        T.write_header_updates(p1, {"status": "waiting", "wait_for": "file:foo.txt"},
                                now=dt("2026-09-27T09:05:00+04:00"))
        tkt = T.read_ticket(p1)
        self.assertEqual(tkt.status, "waiting")
        self.assertEqual(tkt.header["wait_for"], "file:foo.txt")
        self.assertEqual(tkt.header["updated"], "2026-09-27T09:05:00+04:00")
        self.assertEqual(tkt.header["id"], "TK-001")  # прочие строки шапки не тронуты

        T.append_log(p1, "researcher", "Сделал X. @judge глянь.", now=dt("2026-09-27T09:10:00+04:00"))
        tkt = T.read_ticket(p1)
        self.assertEqual(len(tkt.log), 1)
        self.assertEqual(tkt.log[0].mentions, {"judge"})

    def test_next_id_survives_gaps(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "TK-001.md").write_text("x", encoding="utf-8")
        (self.dir / "TK-007.md").write_text("x", encoding="utf-8")
        self.assertEqual(T.next_ticket_id(self.dir), "TK-008")


class DispatchDecisionTests(unittest.TestCase):
    """Только `decide()` — правила (а)-(г), без запуска процессов."""

    def setUp(self):
        self.state = {}
        self.now = dt("2026-09-27T12:00:00+04:00")

    def ticket_from(self, text):
        return T.parse_text(text, Path("TK-x.md"))

    def test_todo_wakes_owner(self):
        text = "---\nid: TK-1\nowner: researcher\nstatus: todo\nupdated: 2026-09-27T11:00:00+04:00\n---\n\n## Лог\n"
        dec = D.decide(self.ticket_from(text), self.state, self.now)
        self.assertEqual((dec.role, dec.reason), ("researcher", "todo"))

    def test_mention_no_longer_wakes_anyone(self):
        """v2 (02.10): @упоминание в тексте записи не будит роль ни на каком статусе (аудит: 77 % запусков)."""
        for status in ("in_review", "blocked", "needs_owner", "waiting", "done"):
            text = (f"---\nid: TK-2\nowner: researcher\nstatus: {status}\nupdated: 2026-09-27T11:00:00+04:00\n---\n\n"
                    "## Лог\n\n### 2026-09-27T11:05:00+04:00 researcher\nНужна проверка. @judge глянь план. "
                    "@engineer @ceo для сведения.\n")
            self.assertIsNone(D.decide(self.ticket_from(text), self.state, self.now), status)

    def test_mention_in_in_progress_only_resumes_owner_not_mentioned_role(self):
        text = ("---\nid: TK-2\nowner: researcher\nstatus: in_progress\nupdated: 2026-09-27T11:00:00+04:00\n---\n\n"
                "## Лог\n\n### 2026-09-27T11:05:00+04:00 researcher\nНужна проверка. @judge глянь план.\n")
        dec = D.decide(self.ticket_from(text), self.state, self.now)
        self.assertEqual((dec.role, dec.reason), ("researcher", "in_progress-resume"))

    def test_next_wakes_named_role_once_and_clears_field(self):
        text = ("---\nid: TK-2\nowner: researcher\nstatus: waiting\nnext: judge\nupdated: 2026-09-27T11:00:00+04:00\n"
                "---\n\n## Лог\n\n### 2026-09-27T11:05:00+04:00 researcher\nПрошу вердикт.\n")
        dec = D.decide(self.ticket_from(text), self.state, self.now)
        self.assertEqual((dec.role, dec.reason), ("judge", "next"))
        self.assertEqual(dec.header_updates, {"next": ""})

    def test_next_beats_status_rules_and_works_on_waiting_without_wait_for(self):
        for status in ("todo", "in_progress", "waiting", "blocked", "needs_owner", "done"):
            text = (f"---\nid: TK-2\nowner: researcher\nstatus: {status}\nnext: engineer\nwait_for:\n"
                    "updated: 2026-09-27T11:00:00+04:00\n---\n\n## Лог\n")
            dec = D.decide(self.ticket_from(text), self.state, self.now)
            self.assertEqual((dec.role, dec.reason), ("engineer", "next"), status)

    def test_next_ceo_or_garbage_is_not_a_role_run(self):
        for value in ("ceo", "somebody"):
            text = (f"---\nid: TK-2\nowner: researcher\nstatus: waiting\nnext: {value}\n"
                    "updated: 2026-09-27T11:00:00+04:00\n---\n\n## Лог\n")
            self.assertIsNone(D.decide(self.ticket_from(text), self.state, self.now), value)

    def test_waiting_with_text_but_without_wait_for_or_next_stays_silent(self):
        text = ("---\nid: TK-2\nowner: researcher\nstatus: waiting\nwait_for:\nupdated: 2026-09-27T11:00:00+04:00\n---"
                "\n\n## Лог\n\n### 2026-09-27T11:05:00+04:00 judge\n@researcher ответ готов, @engineer глянь.\n")
        self.assertIsNone(D.decide(self.ticket_from(text), self.state, self.now))

    def test_waiting_file_condition_met(self):
        with tempfile.TemporaryDirectory() as d:
            marker = Path(d) / "done.flag"
            marker.write_text("x", encoding="utf-8")
            text = (f"---\nid: TK-3\nowner: engineer\nstatus: waiting\nwait_for: file:{marker}\n"
                    "updated: 2026-09-27T11:00:00+04:00\n---\n\n## Лог\n")
            dec = D.decide(self.ticket_from(text), self.state, self.now)
            self.assertEqual((dec.role, dec.reason), ("engineer", "wait_for-met"))

    def test_waiting_file_condition_not_met(self):
        text = ("---\nid: TK-4\nowner: engineer\nstatus: waiting\nwait_for: file:/no/such/file\n"
                "updated: 2026-09-27T11:00:00+04:00\n---\n\n## Лог\n")
        dec = D.decide(self.ticket_from(text), self.state, self.now)
        self.assertIsNone(dec)

    def test_waiting_deck_condition_uses_check_wait_for(self):
        text = ("---\nid: TK-5\nowner: engineer\nstatus: waiting\nwait_for: host:deck:/home/deck/done\n"
                "updated: 2026-09-27T11:00:00+04:00\n---\n\n## Лог\n")
        orig = D.check_wait_for
        D.check_wait_for = lambda spec: spec == "host:deck:/home/deck/done"
        try:
            dec = D.decide(self.ticket_from(text), self.state, self.now)
        finally:
            D.check_wait_for = orig
        self.assertEqual((dec.role, dec.reason), ("engineer", "wait_for-met"))

    def test_done_with_reviewer_and_no_reviewer_entry_sets_in_review(self):
        text = ("---\nid: TK-6\nowner: researcher\nstatus: done\nreviewer: judge\n"
                "updated: 2026-09-27T11:00:00+04:00\n---\n\n## Лог\n\n"
                "### 2026-09-27T10:59:00+04:00 researcher\nГотово, прошу проверку.\n")
        dec = D.decide(self.ticket_from(text), self.state, self.now)
        self.assertEqual((dec.role, dec.reason), ("judge", "review"))
        self.assertEqual(dec.header_updates, {"status": "in_review"})

    def test_done_with_reviewer_reply_after_update_does_not_rewake(self):
        text = ("---\nid: TK-7\nowner: researcher\nstatus: done\nreviewer: judge\n"
                "updated: 2026-09-27T11:00:00+04:00\n---\n\n## Лог\n\n"
                "### 2026-09-27T11:05:00+04:00 judge\nПроверил, принято.\n")
        dec = D.decide(self.ticket_from(text), self.state, self.now)
        self.assertIsNone(dec)

    def test_in_review_with_reviewer_wakes_reviewer_like_done(self):
        """A4 (аудит 03.10): `status: in_review` при заданном reviewer будит ревьюера (как done) — тикет, чей
        ревьюер упал, не висит вечно."""
        text = ("---\nid: TK-6\nowner: researcher\nstatus: in_review\nreviewer: judge\n"
                "updated: 2026-09-27T11:00:00+04:00\n---\n\n## Лог\n\n"
                "### 2026-09-27T10:59:00+04:00 researcher\nГотово, прошу проверку.\n")
        dec = D.decide(self.ticket_from(text), self.state, self.now)
        self.assertEqual((dec.role, dec.reason), ("judge", "review"))
        self.assertFalse(dec.header_updates)           # статус уже in_review — шапку не трогаем

    def test_in_review_reviewer_already_replied_stays_silent(self):
        for author in ("judge", "judge (запуск 2)", "dispatcher"):
            text = ("---\nid: TK-7\nowner: researcher\nstatus: in_review\nreviewer: judge\n"
                    "updated: 2026-09-27T11:00:00+04:00\n---\n\n## Лог\n\n"
                    f"### 2026-09-27T11:05:00+04:00 {author}\nПроверил.\n")
            self.assertIsNone(D.decide(self.ticket_from(text), self.state, self.now), author)

    def test_in_review_without_reviewer_stays_silent(self):
        text = ("---\nid: TK-7\nowner: researcher\nstatus: in_review\nupdated: 2026-09-27T11:00:00+04:00\n---\n\n"
                "## Лог\n\n### 2026-09-27T11:05:00+04:00 researcher\nГотово.\n")
        self.assertIsNone(D.decide(self.ticket_from(text), self.state, self.now))

    def test_in_progress_without_mention_wakes_owner_to_resume(self):
        """v1.1 (судья 27.09, п.2 «обязательно»): без этого многошаговый тикет замирал после первой
        сессии — устав ролей обещает продолжение другой сессией, диспетчер никого не будил."""
        text = ("---\nid: TK-8\nowner: researcher\nstatus: in_progress\nupdated: 2026-09-27T11:00:00+04:00\n---\n\n"
                "## Лог\n\n### 2026-09-27T11:05:00+04:00 researcher\nРаботаю дальше.\n")
        dec = D.decide(self.ticket_from(text), self.state, self.now)
        self.assertEqual((dec.role, dec.reason), ("researcher", "in_progress-resume"))

    def test_backlog_is_fully_ignored_even_with_mention(self):
        """v1.1: backlog — перенос из TASKS.md, диспетчер её не трогает вообще ни по одному правилу."""
        text = ("---\nid: TK-9\nowner: researcher\nstatus: backlog\nupdated: 2026-09-27T11:00:00+04:00\n---\n\n"
                "## Лог\n\n### 2026-09-27T11:05:00+04:00 researcher\n@judge даже упоминание не должно будить.\n")
        dec = D.decide(self.ticket_from(text), self.state, self.now)
        self.assertIsNone(dec)

    def test_backlog_is_ignored_even_with_next(self):
        text = ("---\nid: TK-9\nowner: researcher\nstatus: backlog\nnext: judge\n"
                "updated: 2026-09-27T11:00:00+04:00\n---\n\n## Лог\n")
        self.assertIsNone(D.decide(self.ticket_from(text), self.state, self.now))


# --- фейковый «claude» для сквозных тестов --------------------------------------------------
#
# dispatch._popen() — единственная точка, где создаётся подпроцесс; тесты подменяют её, чтобы
# вместо настоящего CLAUDE_BIN запускать `<sys.executable> <fake_claude.py>` с теми же аргументами
# (-p <prompt> --output-format json ...). Каталог тикетов фейковый скрипт берёт из переменной
# окружения FAKE_TICKETS_DIR (её пробрасывает launch_run через env=dict(os.environ)).

FAKE_BIN_OK = r"""
import json, os, re, sys
from pathlib import Path
from unittest import mock
TICKETS_DIR = Path(os.environ["FAKE_TICKETS_DIR"])
args = sys.argv[1:]
prompt = args[args.index("-p") + 1]
tid = re.search(r"tickets/([\w-]+)\.md", prompt).group(1)
role = next((c for c in ("researcher", "engineer", "judge")
             if prompt.startswith(f"Ты — {c} ")), "unknown")
path = TICKETS_DIR / f"{tid}.md"
text = path.read_text(encoding="utf-8")
text = re.sub(r"(?m)^status:.*$", "status: done", text, count=1)  # роль сама правит шапку
if not text.endswith("\n"):
    text += "\n"
if "## Лог" not in text:
    text += "\n## Лог\n"
text += f"\n### 2099-01-01T00:00:00+04:00 {role}\nСделал шаг (фейковый прогон). status: done.\n"
path.write_text(text, encoding="utf-8")
print(json.dumps({"session_id": f"sess-{tid}-{role}", "total_cost_usd": 0.01,
                   "usage": {"input_tokens": 10, "output_tokens": 5}}))
"""

FAKE_BIN_SILENT = r"""
import json
print(json.dumps({"session_id": "sess-silent", "total_cost_usd": 0.0}))
"""

# Пишет каждый вызов в calls.jsonl (tid, role, resumed session_id или None, prompt) — для проверки
# SESSION_SCOPE (одна сессия на роль vs на задачу) и ротации по RPV_DISPATCH_ROTATE_TOKENS.
# Размер контекста ответа берёт из FAKE_CTX_TOKENS (по умолчанию 10).
FAKE_BIN_RECORD = r"""
import json, os, re, sys
from pathlib import Path
from unittest import mock
TICKETS_DIR = Path(os.environ["FAKE_TICKETS_DIR"])
args = sys.argv[1:]
prompt = args[args.index("-p") + 1]
tid = re.search(r"tickets/([\w-]+)\.md", prompt).group(1)
role = next((c for c in ("researcher", "engineer", "judge") if prompt.startswith(f"Ты — {c} ")), "unknown")
resume_idx = args.index("--resume") if "--resume" in args else None
resumed = args[resume_idx + 1] if resume_idx is not None else None
with (TICKETS_DIR.parent / "calls.jsonl").open("a", encoding="utf-8") as fh:
    fh.write(json.dumps({"tid": tid, "role": role, "resumed": resumed, "prompt": prompt}) + "\n")
path = TICKETS_DIR / f"{tid}.md"
text = path.read_text(encoding="utf-8")
text = re.sub(r"(?m)^status:.*$", "status: done", text, count=1)  # роль сама правит шапку
if not text.endswith("\n"):
    text += "\n"
if "## Лог" not in text:
    text += "\n## Лог\n"
text += f"\n### 2099-01-01T00:00:00+04:00 {role}\nШаг. status: done.\n"
path.write_text(text, encoding="utf-8")
ctx = int(os.environ.get("FAKE_CTX_TOKENS", "10"))
turns = int(os.environ.get("FAKE_NUM_TURNS", "1"))
print(json.dumps({"session_id": f"sess-{role}-{tid}", "total_cost_usd": 0.01, "num_turns": turns,
                   "usage": {"input_tokens": ctx}}))
"""

# Дописывает запись в «## Лог», но НЕ трогает status в шапке — воспроизводит роль, забывшую увести
# задачу с todo (защита v1.1: логировано, но status остался todo — тоже ошибка роли).
FAKE_BIN_STUCK_TODO = r"""
import json, os, re, sys
from pathlib import Path
from unittest import mock
TICKETS_DIR = Path(os.environ["FAKE_TICKETS_DIR"])
args = sys.argv[1:]
prompt = args[args.index("-p") + 1]
tid = re.search(r"tickets/([\w-]+)\.md", prompt).group(1)
role = next((c for c in ("researcher", "engineer", "judge") if prompt.startswith(f"Ты — {c} ")), "unknown")
path = TICKETS_DIR / f"{tid}.md"
text = path.read_text(encoding="utf-8")
if not text.endswith("\n"):
    text += "\n"
if "## Лог" not in text:
    text += "\n## Лог\n"
text += f"\n### 2099-01-01T00:00:00+04:00 {role}\nСделал шаг, но забыл поправить статус.\n"
path.write_text(text, encoding="utf-8")
print(json.dumps({"session_id": f"sess-{tid}-{role}", "total_cost_usd": 0.05, "usage": {"input_tokens": 5}}))
"""

# Как FAKE_BIN_OK, но с задержкой — чтобы поймать процесс «на лету» для теста recover_active_runs.
FAKE_BIN_SLOW_OK = r"""
import json, os, re, sys, time
from pathlib import Path
from unittest import mock
time.sleep(1.5)
TICKETS_DIR = Path(os.environ["FAKE_TICKETS_DIR"])
args = sys.argv[1:]
prompt = args[args.index("-p") + 1]
tid = re.search(r"tickets/([\w-]+)\.md", prompt).group(1)
role = next((c for c in ("researcher", "engineer", "judge") if prompt.startswith(f"Ты — {c} ")), "unknown")
path = TICKETS_DIR / f"{tid}.md"
text = path.read_text(encoding="utf-8")
text = re.sub(r"(?m)^status:.*$", "status: done", text, count=1)
if not text.endswith("\n"):
    text += "\n"
if "## Лог" not in text:
    text += "\n## Лог\n"
text += f"\n### 2099-01-01T00:00:00+04:00 {role}\nШаг (медленный). status: done.\n"
path.write_text(text, encoding="utf-8")
print(json.dumps({"session_id": f"sess-{tid}-{role}", "total_cost_usd": 0.01, "usage": {"input_tokens": 5}}))
"""


# Роль с потомком (оба «висят»): для остановки деревом процессов (`tickets.py stop`). pid потомка — в `child.pid` рядом с
# каталогом тикетов; потомок наследует выходные файлы запуска, после остановки они должны закрыться.
FAKE_BIN_TREE = r"""
import os, subprocess, sys, time
from pathlib import Path
from unittest import mock
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
(Path(os.environ["FAKE_TICKETS_DIR"]).parent / "child.pid").write_text(str(child.pid))
time.sleep(120)
"""


class DispatchRunTests(unittest.TestCase):
    """Сквозные тесты `tick()` с подменённым `CLAUDE_BIN` (без сети и без настоящего claude)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.tickets_dir = self.base / "tickets"
        self.tickets_dir.mkdir(parents=True)
        self.dispatcher_dir = self.base / "dispatcher"
        self.dispatcher_dir.mkdir(parents=True)

        self._orig = {k: getattr(D, k) for k in
                      ("TICKETS_DIR", "PROJECT_ROOT", "STATE_FILE", "RUNS_DIR", "RUNS_LOG",
                       "CEO_INBOX", "CEO_WAKE_LOG", "CLAUDE_BIN", "MAX_PARALLEL", "RUN_TIMEOUT",
                       "PID_EXPECT_NAME", "STOP_DIR", "STOP_VERIFY_S", "ROLE_PARALLEL")}
        D.ROLE_PARALLEL = {}  # предел по умолчанию (1 на роль), независимо от RPV_DISPATCH_ROLE_PARALLEL в окружении
        D.TICKETS_DIR = self.tickets_dir
        D.PROJECT_ROOT = self.base
        D.STATE_FILE = self.dispatcher_dir / "state.json"
        D.RUNS_DIR = self.dispatcher_dir / "runs"
        D.RUNS_LOG = self.dispatcher_dir / "runs.log"
        D.CEO_INBOX = self.dispatcher_dir / "ceo-inbox.md"
        D.CEO_WAKE_LOG = self.dispatcher_dir / "ceo-wake.log"
        D.STOP_DIR = self.dispatcher_dir / "stop"
        # фейковый "claude" в тестах — это sys.executable (python.exe/python3), не claude.exe
        D.PID_EXPECT_NAME = python_image_name()
        D.RUNNING.clear()
        self._orig_popen = D._popen
        self._orig_scope = dict(D.SESSION_SCOPE)
        # по умолчанию подпроцесс — безвредный `python -c pass`: тесты, доходящие до launch_run (повтор в
        # _finish_run и т. п.), не запускают настоящий `claude`; set_fake_bin() подменяет на фейк роли
        D._popen = lambda cmd, **kwargs: subprocess.Popen([sys.executable, "-c", "pass"], **kwargs)
        os.environ["FAKE_TICKETS_DIR"] = str(self.tickets_dir)

    def tearDown(self):
        for tid, info in list(D.RUNNING.items()):
            try:
                info["popen"].kill()
                info["popen"].wait(timeout=30)
            except Exception:
                pass
            for fh in (info.get("out_fh"), info.get("err_fh")):
                try:
                    fh.close()
                except Exception:
                    pass
        D.RUNNING.clear()
        D._popen = self._orig_popen
        D.SESSION_SCOPE.clear()
        D.SESSION_SCOPE.update(self._orig_scope)
        del os.environ["FAKE_TICKETS_DIR"]
        for k, v in self._orig.items():
            setattr(D, k, v)
        for attempt in range(20):   # Windows под xdist: потомок ещё держит err.log (WinError 32), TK-095
            try:
                self.tmp.cleanup()
                break
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(0.5)

    def set_fake_bin(self, body: str) -> Path:
        """Пишет фейковый `claude` и подменяет `D._popen`, чтобы CLAUDE_BIN подменялся на python-скрипт."""
        script = self.base / "fake_claude.py"
        script.write_text(body, encoding="utf-8")

        def fake_popen(cmd, **kwargs):
            return subprocess.Popen([sys.executable, str(script)] + list(cmd[1:]), **kwargs)

        D._popen = fake_popen
        return script

    def wait_running(self, timeout=60.0):   # предел, не пауза: выходит по завершении; 10 с не хватало ПК под xdist (TK-095)
        deadline = time.time() + timeout
        while D.RUNNING and time.time() < deadline:
            state = D.load_state()
            D._poll_running(state, datetime.now().astimezone())
            D.save_state(state)
            time.sleep(0.05)
        self.assertFalse(D.RUNNING, "фейковый прогон не завершился за отведённое время")

    def test_downtime_counts_waiting_without_condition_and_alerts_once(self):
        t0 = dt("2026-10-06T12:00:00+04:00")
        path = T.create_ticket(self.tickets_dir, owner="engineer", title="Ждёт", now=t0 - timedelta(hours=1))
        T.write_header_updates(path, {"status": "waiting", "wait_for": ""}, now=t0 - timedelta(hours=1))
        with mock.patch.dict(os.environ, {"RPV_BUS_DISABLE": "1"}),                 mock.patch.object(D, "enforce_move_invariant", lambda p, t, s, n: t):  # учёт — отдельно от пробуждения п.6
            D.tick(t0)
            D.tick(t0 + timedelta(seconds=300))
            D.tick(t0 + timedelta(seconds=900))  # 15 мин > SLO 10
            D.tick(t0 + timedelta(seconds=1000))
        rec = D.load_state()["downtime"]["2026-10-06"]
        self.assertEqual(rec["wait_s"], 1000)
        self.assertTrue(rec["alerted"])
        self.assertEqual(self.dispatcher_dir.joinpath("ceo-inbox.md").read_text(encoding="utf-8").count("[idle-slo]"), 1)

    def test_downtime_throttle_in_slo_and_limit_pause_outside(self):
        t0 = dt("2026-10-06T12:00:00+04:00")
        T.create_ticket(self.tickets_dir, owner="engineer", title="Тормоз", now=t0 - timedelta(hours=1))
        with mock.patch.dict(os.environ, {"RPV_BUS_DISABLE": "1"}), mock.patch.object(D, "_rate_limited", lambda *a: True):
            D.tick(t0)
            D.tick(t0 + timedelta(seconds=300))
            rec = D.load_state()["downtime"]["2026-10-06"]
            self.assertEqual((rec["throttle_s"], rec["idle_s"]), (300, 0))
            st = D.load_state()
            st["limit_pause_until"] = (t0 + timedelta(hours=2)).isoformat()
            D.save_state(st)
            D.tick(t0 + timedelta(seconds=600))
            D.tick(t0 + timedelta(seconds=1800))
        rec = D.load_state()["downtime"]["2026-10-06"]
        self.assertEqual((rec["throttle_s"], rec["limit_s"]), (300 + 300, 1200))

    def mk(self, owner, status, wait_for="", now=None, reviewer=None):
        now = now or dt("2026-10-06T12:00:00+04:00")
        p = T.create_ticket(self.tickets_dir, owner=owner, title="t", reviewer=reviewer, now=now)
        T.write_header_updates(p, {"status": status, "wait_for": wait_for}, now=now)
        return p

    def test_waiting_without_condition_woken_after_grace_and_signalled_once(self):
        t0 = dt("2026-10-06T12:00:00+04:00")
        p = self.mk("engineer", "waiting", now=t0)
        state = {}
        early = D.enforce_move_invariant(p, T.read_ticket(p), state, t0 + timedelta(minutes=9))
        self.assertEqual(early.status, "waiting")
        late = D.enforce_move_invariant(p, T.read_ticket(p), state, t0 + timedelta(minutes=11))
        self.assertEqual(late.status, "in_progress")
        self.assertEqual(D.decide(late, state, t0 + timedelta(minutes=11)).role, "engineer")
        self.assertTrue(any(e.author == "dispatcher" and "инвариант" in e.text for e in late.log))
        self.assertIn("нет-хода", D.CEO_INBOX.read_text(encoding="utf-8"))

    def test_waiting_with_good_wait_for_is_a_move(self):
        t0 = dt("2026-10-06T12:00:00+04:00")
        p = self.mk("engineer", "waiting", "file:/tmp/x", now=t0)
        self.assertEqual(D.enforce_move_invariant(p, T.read_ticket(p), {}, t0 + timedelta(hours=5)).status, "waiting")

    def test_in_review_after_reviewer_entry_without_status_is_woken(self):
        t0 = dt("2026-10-06T12:00:00+04:00")
        p = self.mk("engineer", "in_review", reviewer="judge", now=t0)
        T.append_log(p, "judge", "Дальше: Инженер", now=t0 + timedelta(seconds=30))
        self.assertIsNone(D.decide(T.read_ticket(p), {}, t0))
        out = D.enforce_move_invariant(p, T.read_ticket(p), {}, t0 + timedelta(minutes=12))
        self.assertEqual(out.status, "in_progress")

    def test_ceo_handoff_is_a_move_until_ceo_answers(self):
        t0 = dt("2026-10-06T12:00:00+04:00")
        p = self.mk("engineer", "waiting", now=t0)
        state = {"ceo_handoffs": {T.read_ticket(p).id: T.now_iso(t0)}}
        for m in (11, 12, 30):
            self.assertEqual(D.enforce_move_invariant(p, T.read_ticket(p), state, t0 + timedelta(minutes=m)).status, "waiting")
        inbox = D.CEO_INBOX.read_text(encoding="utf-8") if D.CEO_INBOX.exists() else ""
        self.assertEqual(inbox.count("[ждёт-ceo]"), 1)
        self.assertNotIn("[нет-хода]", inbox)
        T.append_log(p, "ceo", "принято, думаю", now=t0 + timedelta(minutes=31))
        T.write_header_updates(p, {}, now=t0 + timedelta(minutes=31))  # comment обновляет updated
        self.assertEqual(D.enforce_move_invariant(p, T.read_ticket(p), state, t0 + timedelta(minutes=33)).status, "waiting")
        out = D.enforce_move_invariant(p, T.read_ticket(p), state, t0 + timedelta(minutes=45))
        self.assertEqual(out.status, "in_progress")
        self.assertEqual(state["ceo_handoffs"], {})

    def test_ceo_comment_without_status_change_gets_grace_from_the_entry(self):
        t0 = dt("2026-10-06T12:00:00+04:00")
        p = self.mk("engineer", "waiting", now=t0)
        state = {"ceo_handoffs": {T.read_ticket(p).id: T.now_iso(t0)}}
        T.append_log(p, "ceo", "принято", now=t0 + timedelta(minutes=30))  # как tickets.py comment: updated не двигается
        self.assertIsNone(D._no_move_reason(T.read_ticket(p), t0 + timedelta(minutes=30, seconds=5), state))
        out = D.enforce_move_invariant(p, T.read_ticket(p), state, t0 + timedelta(minutes=45))
        self.assertEqual(out.status, "in_progress")

    def _handoff_state(self, p, t0):
        return {"ceo_handoffs": {T.read_ticket(p).id: T.now_iso(t0)}}

    def test_ceo_handoff_ends_when_ceo_returns_by_header_only(self):
        t0 = dt("2026-10-06T12:00:00+04:00")
        p = self.mk("engineer", "waiting", now=t0)
        state = self._handoff_state(p, t0)
        T.write_header_updates(p, {"status": "todo"}, now=t0 + timedelta(minutes=5))
        D._expire_ceo_handoff(T.read_ticket(p), state)
        self.assertEqual(state["ceo_handoffs"], {})
        T.write_header_updates(p, {"status": "waiting"}, now=t0 + timedelta(minutes=20))
        out = D.enforce_move_invariant(p, T.read_ticket(p), state, t0 + timedelta(minutes=31))
        self.assertEqual(out.status, "in_progress")

    def test_ceo_handoff_ends_when_any_role_is_launched_later(self):
        t0 = dt("2026-10-06T12:00:00+04:00")
        p = self.mk("engineer", "waiting", now=t0)
        state = self._handoff_state(p, t0)
        state["ceo_handoff_reminded"] = {T.read_ticket(p).id: "x"}
        fake = mock.MagicMock(pid=1)
        with mock.patch.object(D, "_popen", return_value=fake), mock.patch.object(D, "RUNS_DIR", self.tickets_dir.parent / "runs"):
            try:
                D.launch_run(p, "judge", state, t0 + timedelta(minutes=2), reason="next")
            finally:
                for info in D.RUNNING.values():
                    for fh in (info.get("out_fh"), info.get("err_fh")):
                        if fh:
                            fh.close()
                D.RUNNING.clear()
        self.assertEqual((state["ceo_handoffs"], state["ceo_handoff_reminded"]), ({}, {}))
        T.write_header_updates(p, {"status": "waiting", "wait_for": ""}, now=t0 + timedelta(minutes=20))
        out = D.enforce_move_invariant(p, T.read_ticket(p), state, t0 + timedelta(hours=3))
        self.assertEqual(out.status, "in_progress")

    def test_ceo_handoff_survives_tick_between_next_ceo_and_waiting(self):
        t0 = dt("2026-10-06T12:00:00+04:00")
        p = self.mk("engineer", "in_progress", now=t0)
        state = self._handoff_state(p, t0 + timedelta(seconds=2))  # метка поставлена тиком после `comment --next ceo`
        D._expire_ceo_handoff(T.read_ticket(p), state)
        self.assertIn(T.read_ticket(p).id, state["ceo_handoffs"])
        T.write_header_updates(p, {"status": "waiting"}, now=t0 + timedelta(seconds=9))
        out = D.enforce_move_invariant(p, T.read_ticket(p), state, t0 + timedelta(minutes=12))
        self.assertEqual(out.status, "waiting")

    def test_ceo_handoff_dropped_on_done(self):
        t0 = dt("2026-10-06T12:00:00+04:00")
        p = self.mk("engineer", "waiting", now=t0)
        state = self._handoff_state(p, t0)
        T.write_header_updates(p, {"status": "done"}, now=t0 + timedelta(minutes=5))
        D._expire_ceo_handoff(T.read_ticket(p), state)
        self.assertEqual(state["ceo_handoffs"], {})

    def test_ceo_handoff_early_is_silent(self):
        t0 = dt("2026-10-06T12:00:00+04:00")
        p = self.mk("engineer", "waiting", now=t0)
        state = {"ceo_handoffs": {T.read_ticket(p).id: T.now_iso(t0)}}
        self.assertIsNone(D._no_move_reason(T.read_ticket(p), t0 + timedelta(minutes=5), state))
        self.assertFalse(D.CEO_INBOX.exists() and "ждёт-ceo" in D.CEO_INBOX.read_text(encoding="utf-8"))

    def test_open_owner_question_is_a_move_answer_ends_it(self):
        t0 = dt("2026-10-06T12:00:00+04:00")
        p = self.mk("engineer", "waiting", now=t0)
        tid = T.read_ticket(p).id
        qdir = self.tickets_dir.parent / "pulse" / "questions"
        qdir.mkdir(parents=True, exist_ok=True)
        qf = qdir / f"q-{tid}-1.json"
        qf.write_text(json.dumps({"id": f"q-{tid}-1", "process": tid, "answered_at": None}), encoding="utf-8")
        self.assertIsNone(D._no_move_reason(T.read_ticket(p), t0 + timedelta(minutes=11), {}))
        self.assertEqual(D.enforce_move_invariant(p, T.read_ticket(p), {}, t0 + timedelta(minutes=30)).status, "waiting")
        qf.write_text(json.dumps({"id": f"q-{tid}-1", "process": tid, "answered_at": "2026-10-06T12:20:00+04:00"}), encoding="utf-8")
        self.assertIsNotNone(D._no_move_reason(T.read_ticket(p), t0 + timedelta(minutes=31), {}))

    def test_done_and_blocked_are_not_violations(self):
        t0 = dt("2026-10-06T12:00:00+04:00")
        for st in ("done", "blocked", "needs_owner", "stopped"):
            p = self.mk("engineer", st, now=t0)
            self.assertIsNone(D._no_move_reason(T.read_ticket(p), t0 + timedelta(days=1)), st)

    def test_wait_cycle_two_and_three_tickets_refused_by_cli(self):
        a, b, c = (self.mk("engineer", "todo") for _ in range(3))
        ia, ib, ic = a.stem, b.stem, c.stem
        T.write_header_updates(a, {"status": "waiting", "wait_for": f"ticket:{ib}"})
        self.assertEqual(T.wait_cycle(self.tickets_dir, ib, ia), [ib, ia, ib])  # 2 тикета
        T.write_header_updates(b, {"status": "waiting", "wait_for": f"ticket:{ic}"})
        self.assertEqual(T.wait_cycle(self.tickets_dir, ic, ia), [ic, ia, ib, ic])  # 3 тикета
        self.assertIsNone(T.wait_cycle(self.tickets_dir, ic, "TK-9999"))
        self.assertEqual(T.wait_cycle(self.tickets_dir, ic, ic), [ic, ic])  # сам на себя
        orig = TK.TICKETS_DIR
        TK.TICKETS_DIR = self.tickets_dir
        try:
            self.assertEqual(TK.main(["wait", ic, f"ticket:{ia}"]), 1)
            self.assertEqual(T.read_ticket(c).status, "todo")  # отказ — шапка не тронута
            self.assertEqual(TK.main(["wait", ic, "file:flags/ok", "--by", "2099-01-01T00:00+04:00"]), 0)
        finally:
            TK.TICKETS_DIR = orig

    def test_existing_cycle_signals_ceo_once_from_min_id(self):
        a, b = (self.mk("engineer", "todo") for _ in range(2))
        T.write_header_updates(a, {"status": "waiting", "wait_for": f"ticket:{b.stem}"})
        T.write_header_updates(b, {"status": "waiting", "wait_for": f"ticket:{a.stem}"})
        state, now = {}, dt("2026-10-06T12:30:00+04:00")
        for p in (b, a, a):
            D.notify_wait_cycle(T.read_ticket(p), state, now)
        self.assertEqual(D.CEO_INBOX.read_text(encoding="utf-8").count("цикл-ожиданий"), 1)

    def test_limit_pause_capped_to_one_hour(self):
        self.assertEqual(D.LIMIT_PAUSE_MAX, timedelta(hours=1))

    def test_todo_ticket_runs_logs_and_saves_session(self):
        self.set_fake_bin(FAKE_BIN_OK)
        path = T.create_ticket(self.tickets_dir, owner="researcher", title="Тест",
                                now=dt("2026-09-27T12:00:00+04:00"))
        n = D.tick()
        self.assertEqual(n, 1)
        self.assertIn(path.stem, D.RUNNING)
        self.wait_running()

        tkt = T.read_ticket(path)
        self.assertEqual(len(tkt.log), 1)
        self.assertEqual(tkt.log[0].author, "researcher")

        state = D.load_state()
        self.assertEqual(state["ticket_sessions"][f"{path.stem}::researcher"]["session_id"],
                          f"sess-{path.stem}-researcher")
        self.assertTrue(D.RUNS_LOG.exists())
        self.assertIn(path.stem, D.RUNS_LOG.read_text(encoding="utf-8"))

    def test_waiting_without_condition_returns_to_in_progress(self):
        """TK-070 п.3: владелец ушёл в waiting без wait_for и без next — диспетчер возвращает in_progress и пишет причину."""
        now = dt("2026-10-06T12:00:00+04:00")
        path = T.create_ticket(self.tickets_dir, owner="engineer", title="Ожидание", now=now)
        T.write_header_updates(path, {"status": "waiting", "wait_for": ""}, now=now)
        state = D.load_state()
        D._finish_role_part(path.stem, {"role": "engineer", "status_at_launch": "in_progress"}, state, now, False, {})
        tkt = T.read_ticket(path)
        self.assertEqual(tkt.status, "in_progress")
        self.assertTrue(any(e.author == "dispatcher" and "без wait_for" in e.text for e in tkt.log))

    def test_waiting_after_next_ceo_consumed_by_tick_is_not_refused(self):
        """TK-076 п.2: `--next ceo`, съеденный тиком при живой роли, не делает её waiting «без условия»."""
        t0 = dt("2026-10-06T12:00:00+04:00")
        path = T.create_ticket(self.tickets_dir, owner="engineer", title="Передача CEO", now=t0)
        T.append_log(path, "engineer", "нужно решение", now=t0 + timedelta(seconds=5))
        T.write_header_updates(path, {"next": "ceo"}, now=t0 + timedelta(seconds=5))
        state = D.load_state()
        D.handle_next_ceo(path, T.read_ticket(path), state, t0 + timedelta(seconds=6))
        T.write_header_updates(path, {"status": "waiting", "wait_for": ""}, now=t0 + timedelta(seconds=8))
        D._finish_role_part(path.stem, {"role": "engineer", "status_at_launch": "in_progress", "started": t0},
                            state, t0 + timedelta(seconds=9), False, {})
        tkt = T.read_ticket(path)
        self.assertEqual(tkt.status, "waiting")
        self.assertFalse(any("без wait_for" in e.text for e in tkt.log))

    def test_next_ceo_mark_reaches_disk_before_next_is_cleared(self):
        """TK-076: диспетчер убит между очисткой `next` и сохранением state — метка передачи не теряется (bus-раунд macOS)."""
        t0 = dt("2026-10-06T12:00:00+04:00")
        path = T.create_ticket(self.tickets_dir, owner="engineer", title="Падение посреди передачи", now=t0)
        T.append_log(path, "engineer", "нужно решение", now=t0 + timedelta(seconds=5))
        T.write_header_updates(path, {"next": "ceo"}, now=t0 + timedelta(seconds=5))
        state = D.load_state()
        with mock.patch.object(T, "write_header_updates", side_effect=RuntimeError("kill")):
            with self.assertRaises(RuntimeError):
                D.handle_next_ceo(path, T.read_ticket(path), state, t0 + timedelta(seconds=6))
        self.assertIn(path.stem, D.load_state().get("ceo_handoffs", {}))
        self.assertEqual(T.read_ticket(path).next_role, "ceo")

    def test_waiting_with_condition_untouched(self):
        now = dt("2026-10-06T12:00:00+04:00")
        path = T.create_ticket(self.tickets_dir, owner="engineer", title="Ожидание", now=now)
        T.write_header_updates(path, {"status": "waiting", "wait_for": "file:/tmp/x"}, now=now)
        state = D.load_state()
        D._finish_role_part(path.stem, {"role": "engineer", "status_at_launch": "in_progress"}, state, now, False, {})
        self.assertEqual(T.read_ticket(path).status, "waiting")

    def test_unblock_limit_victims(self):
        """TK-070: blocked по холостым ходам, чей последний запуск — 429, снимается сам; чужой блок остаётся."""
        now = dt("2026-10-06T12:00:00+04:00")
        victim = T.create_ticket(self.tickets_dir, owner="engineer", title="Жертва лимита", now=now)
        other = T.create_ticket(self.tickets_dir, owner="engineer", title="Не жертва", now=now)
        for p in (victim, other):
            T.write_header_updates(p, {"status": "blocked"}, now=now)
            T.append_log(p, "dispatcher", "холостой ход ×2: роль не оставила запись", now=now)
        D.RUNS_DIR.mkdir(parents=True, exist_ok=True)
        (D.RUNS_DIR / f"20261006-080000-{victim.stem}-engineer.json").write_text(
            json.dumps({"api_error_status": 429, "result": "You've hit your session limit · resets 5am"}), encoding="utf-8")
        (D.RUNS_DIR / f"20261006-080000-{other.stem}-engineer.json").write_text(
            json.dumps({"result": "ok"}), encoding="utf-8")
        D.unblock_limit_victims(now)
        self.assertEqual(T.read_ticket(victim).status, "in_progress")
        self.assertEqual(T.read_ticket(other).status, "blocked")

    def test_launch_run_strips_host_session_env(self):
        """(4) обязательно: без этого дочерний claude наследует CLAUDE_CODE_HOST_SESSION_ID сессии CEO —
        role_context.py/role_memory.py принимают роль за CEO (судья 27.09, пилот TK-001)."""
        os.environ["CLAUDE_CODE_HOST_SESSION_ID"] = "local_ceo-host-id-fake"
        self.addCleanup(lambda: os.environ.pop("CLAUDE_CODE_HOST_SESSION_ID", None))
        self.set_fake_bin(FAKE_BIN_SILENT)
        captured_env = {}
        orig_popen = D._popen

        def spy_popen(cmd, **kwargs):
            captured_env.update(kwargs.get("env") or {})
            return orig_popen(cmd, **kwargs)

        D._popen = spy_popen
        try:
            path = T.create_ticket(self.tickets_dir, owner="researcher", title="Утечка env")
            D.tick()
        finally:
            D._popen = orig_popen
        self.assertNotIn("CLAUDE_CODE_HOST_SESSION_ID", captured_env)
        self.assertNotIn("ALPHA_ROLE", captured_env)
        self.assertNotIn("ALPHA_TICKET", captured_env)
        self.assertEqual(captured_env.get("RPV_ROLE"), "researcher")   # A7: хуки ведут состояние по тикету
        self.assertEqual(captured_env.get("RPV_TICKET"), path.stem)
        self.assertEqual(captured_env.get("RPV_PROJECT"), str(self.base))
        for info in list(D.RUNNING.values()):
            info["popen"].wait(timeout=10)
            for fh in (info.get("out_fh"), info.get("err_fh")):
                if fh:
                    fh.close()
        D.RUNNING.clear()

    def test_launch_run_sets_model_and_effort_and_no_budget_flag(self):
        """v1.3 (владелец 27.09): модель/усилие ВСЕГДА явно — пилот на умолчаниях CLI стоил $6,8. Денежного
        потолка запуска нет (владелец 03.10: «бюджет до конца убирай») — `--max-budget-usd` не передаётся."""
        self.set_fake_bin(FAKE_BIN_SILENT)
        captured_cmd = []
        orig_popen = D._popen

        def spy_popen(cmd, **kwargs):
            captured_cmd.extend(cmd)
            return orig_popen(cmd, **kwargs)

        D._popen = spy_popen
        try:
            path = T.create_ticket(self.tickets_dir, owner="judge", title="Проверка флагов")
            D.tick()
        finally:
            D._popen = orig_popen
        # v1.6.1: модель — по роли (ROLE_MODEL[judge], по умолчанию opus), не общий CLAUDE_MODEL
        self.assertEqual(captured_cmd[captured_cmd.index("--model") + 1], D.ROLE_MODEL["judge"])
        self.assertEqual(captured_cmd[captured_cmd.index("--effort") + 1], "xhigh")  # ROLE_EFFORT[judge]
        self.assertNotIn("--strict-mcp-config", captured_cmd)  # TK-171: умолчание RPV_ROLE_MCP=all — как до 1.8.26
        self.assertNotIn("--mcp-config", captured_cmd)
        self.assertNotIn("--max-budget-usd", captured_cmd)
        for info in list(D.RUNNING.values()):
            info["popen"].wait(timeout=10)
            for fh in (info.get("out_fh"), info.get("err_fh")):
                if fh:
                    fh.close()
        D.RUNNING.clear()

    def test_launch_run_model_follows_role_not_global(self):
        """v1.6.1: --model запуска — ROLE_MODEL[роль]; инженер не получает модель Судьи и наоборот."""
        self.set_fake_bin(FAKE_BIN_SILENT)
        captured_cmd = []
        orig_popen = D._popen
        orig_role_model = D.ROLE_MODEL

        def spy_popen(cmd, **kwargs):
            captured_cmd.extend(cmd)
            return orig_popen(cmd, **kwargs)

        D._popen = spy_popen
        D.ROLE_MODEL = {"judge": "claude-opus-5-5", "engineer": "claude-sonnet-5-5-test",
                        "researcher": "claude-sonnet-5-5-test"}
        try:
            T.create_ticket(self.tickets_dir, owner="engineer", title="Модель инженера")
            D.tick()
        finally:
            D._popen = orig_popen
            D.ROLE_MODEL = orig_role_model
        self.assertEqual(captured_cmd[captured_cmd.index("--model") + 1], "claude-sonnet-5-5-test")
        for info in list(D.RUNNING.values()):
            info["popen"].wait(timeout=10)
            for fh in (info.get("out_fh"), info.get("err_fh")):
                if fh:
                    fh.close()
        D.RUNNING.clear()

    def test_launch_run_uses_haiku_model_for_allowed_executor(self):
        """v1.4, судья TK-002 п.5: executor: haiku подменяет --model, остальное (эффорт, лимиты) как обычно."""
        self.set_fake_bin(FAKE_BIN_SILENT)
        path = T.create_ticket(self.tickets_dir, owner="engineer", title="Механическая")  # reviewer=None по умолчанию
        T.write_header_updates(path, {"executor": "haiku", "kind": "file-move"})
        captured_cmd = []
        orig_popen = D._popen

        def spy_popen(cmd, **kwargs):
            captured_cmd.extend(cmd)
            return orig_popen(cmd, **kwargs)

        D._popen = spy_popen
        try:
            D.tick()
        finally:
            D._popen = orig_popen
        self.assertEqual(captured_cmd[captured_cmd.index("--model") + 1], D.CLAUDE_HAIKU_MODEL)
        self.assertEqual(captured_cmd[captured_cmd.index("--effort") + 1], "xhigh")
        for info in list(D.RUNNING.values()):
            info["popen"].wait(timeout=10)
            for fh in (info.get("out_fh"), info.get("err_fh")):
                if fh:
                    fh.close()
        D.RUNNING.clear()

    def test_tick_blocks_ticket_with_forbidden_haiku_combo(self):
        """Вторая защита в диспетчере (условие г) — на случай ручной правки шапки в обход tickets.py."""
        path = T.create_ticket(self.tickets_dir, owner="researcher", title="Обход")
        T.write_header_updates(path, {"executor": "haiku", "kind": "file-move"})
        n = D.tick()
        self.assertEqual(n, 0)
        tkt = T.read_ticket(path)
        self.assertEqual(tkt.status, "blocked")
        self.assertIn("researcher", tkt.log[-1].text)
        self.assertIn("researcher", D.CEO_INBOX.read_text(encoding="utf-8"))

    def test_finish_run_flags_haiku_ticket_actually_run_on_other_model(self):
        path = T.create_ticket(self.tickets_dir, owner="engineer", title="Подмена модели")
        T.write_header_updates(path, {"executor": "haiku", "kind": "file-move"})
        tid = path.stem
        state = D.load_state()
        run_file = self.dispatcher_dir / "wrongmodel.json"
        run_file.write_text(json.dumps({"session_id": "s-haiku-1", "total_cost_usd": 0.01,
                                         "modelUsage": {"claude-opus-5-5": {"costUSD": 0.01}}}),
                             encoding="utf-8")
        info = {"role": "engineer", "popen": None, "pid": None, "started": dt("2026-09-27T12:00:00+04:00"),
                "attempt": 0, "run_file": run_file, "err_file": run_file, "out_fh": None, "err_fh": None,
                "reason": "todo", "status_at_launch": "todo", "executor": "haiku"}
        D._finish_run(tid, info, state, dt("2026-09-27T12:01:00+04:00"), timed_out=False)
        self.assertIn("opus", D.CEO_INBOX.read_text(encoding="utf-8"))

    def test_no_log_entry_retries_once_then_blocks(self):
        self.set_fake_bin(FAKE_BIN_SILENT)
        path = T.create_ticket(self.tickets_dir, owner="engineer", title="Молчун",
                                now=dt("2026-09-27T12:00:00+04:00"))
        D.tick()
        self.wait_running()  # первая попытка молчит → внутри неё должен стартовать повтор
        # если повтор ещё не успел завершиться к моменту выхода из wait_running — исключение сработало бы там;
        # раз дошли сюда, все попытки для этого тикета исчерпаны и RUNNING пуст.
        tkt = T.read_ticket(path)
        self.assertEqual(tkt.status, "blocked")
        self.assertTrue(D.CEO_INBOX.exists())
        inbox = D.CEO_INBOX.read_text(encoding="utf-8")
        self.assertIn(path.stem, inbox)
        self.assertIn("blocked", inbox)

    def test_max_parallel_respected(self):
        self.set_fake_bin(FAKE_BIN_SILENT)
        D.MAX_PARALLEL = 1
        T.create_ticket(self.tickets_dir, owner="researcher", title="Первая", now=dt("2026-09-27T12:00:00+04:00"))
        T.create_ticket(self.tickets_dir, owner="engineer", title="Вторая", now=dt("2026-09-27T12:00:00+04:00"))
        D.tick()
        self.assertEqual(len(D.RUNNING), 1)

    def test_one_run_per_role_globally(self):
        """v2 (02.10): не больше ОДНОГО запуска на роль по всем тикетам; разные роли — параллельно (≤ MAX_PARALLEL);
        вторая задача роли не теряется — стартует, когда роль освободилась."""
        self.set_fake_bin(FAKE_BIN_OK)
        D.MAX_PARALLEL = 3
        first = T.create_ticket(self.tickets_dir, owner="researcher", title="Первая")
        second = T.create_ticket(self.tickets_dir, owner="researcher", title="Вторая")
        T.create_ticket(self.tickets_dir, owner="engineer", title="Третья")
        D.tick()
        self.assertEqual(sorted(i["role"] for i in D.RUNNING.values()), ["engineer", "researcher"])
        self.assertIn(first.stem, D.RUNNING)
        self.assertNotIn(second.stem, D.RUNNING)
        self.wait_running()
        D.tick()
        self.assertIn(second.stem, D.RUNNING, "роль освободилась — вторая задача берётся")

    def test_role_parallel_limit_two_allows_second_ticket_not_third(self):
        """RPV_DISPATCH_ROLE_PARALLEL=engineer:2 (CEO 05.10): две задачи роли идут параллельно, третья ждёт;
        на тот же тикет второй запуск роли не стартует никогда (даже при большом пределе)."""
        self.set_fake_bin(FAKE_BIN_SLOW_OK)
        D.MAX_PARALLEL = 5
        D.ROLE_PARALLEL = {"engineer": 2}
        first = T.create_ticket(self.tickets_dir, owner="engineer", title="Первая")
        second = T.create_ticket(self.tickets_dir, owner="engineer", title="Вторая")
        third = T.create_ticket(self.tickets_dir, owner="engineer", title="Третья")
        D.tick()
        self.assertEqual(len(D.RUNNING), 2)
        self.assertIn(first.stem, D.RUNNING)
        self.assertIn(second.stem, D.RUNNING)
        self.assertNotIn(third.stem, D.RUNNING, "предел роли 2 — третья задача ждёт")
        pids = {tid: info["pid"] for tid, info in D.RUNNING.items()}
        D.ROLE_PARALLEL = {"engineer": 5}
        D.tick()  # предел больше числа задач: свободных тикетов роли — один (третий), уже идущие второй раз не берутся
        self.assertEqual(sorted(D.RUNNING), sorted([first.stem, second.stem, third.stem]))
        for tid, pid in pids.items():
            self.assertEqual(D.RUNNING[tid]["pid"], pid, "на тот же тикет второй запуск роли не стартует")
        self.wait_running()

    def test_role_parallel_parse(self):
        self.assertEqual(D._parse_role_parallel("engineer:3,researcher:1"), {"engineer": 3, "researcher": 1})
        self.assertEqual(D._parse_role_parallel(""), {})
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            parsed = D._parse_role_parallel("engineer:abc,judge:0,researcher,:2,engineer:2")
        self.assertEqual(parsed, {"engineer": 2}, "мусор игнорируется, остальное разбирается")
        self.assertEqual(err.getvalue().count("RPV_DISPATCH_ROLE_PARALLEL"), 4, "по предупреждению на каждую пару")

    def test_role_scope_serializes_and_shares_session(self):
        """judge со scope "role" (только по env/правке словаря): вторая задача продолжает ту же сессию;
        запуск на роль — один (теперь так у всех ролей, см. test_one_run_per_role_globally)."""
        D.SESSION_SCOPE["judge"] = "role"
        self.set_fake_bin(FAKE_BIN_RECORD)
        os.environ["FAKE_CTX_TOKENS"] = "10"
        self.addCleanup(lambda: os.environ.pop("FAKE_CTX_TOKENS", None))
        D.MAX_PARALLEL = 2
        T.create_ticket(self.tickets_dir, owner="judge", title="Проверка A")
        T.create_ticket(self.tickets_dir, owner="judge", title="Проверка B")

        D.tick()
        self.assertEqual(len(D.RUNNING), 1, "одна роль — один запуск: вторая задача не должна стартовать сразу")
        self.wait_running()

        D.tick()
        self.assertEqual(len(D.RUNNING), 1)
        self.wait_running()

        calls = [json.loads(l) for l in (self.tickets_dir.parent / "calls.jsonl").read_text(encoding="utf-8")
                 .splitlines()]
        self.assertEqual(len(calls), 2)
        self.assertNotEqual(calls[0]["tid"], calls[1]["tid"], "вторая задача должна была дождаться своей очереди")
        self.assertIsNone(calls[0]["resumed"], "первый запуск роли — новая сессия, без --resume")
        first_sid = f"sess-judge-{calls[0]['tid']}"
        self.assertEqual(calls[1]["resumed"], first_sid, "вторая задача должна продолжить ту же сессию judge")
        state = D.load_state()
        self.assertIn("judge", state.get("role_sessions", {}))

    def test_role_scope_rotates_after_big_context(self):
        """Контекст прошлого запуска роли выше ROTATE_TOKENS → следующий запуск без --resume + напоминание."""
        D.SESSION_SCOPE["judge"] = "role"
        self.set_fake_bin(FAKE_BIN_RECORD)
        os.environ["FAKE_CTX_TOKENS"] = str(D.ROTATE_TOKENS + 1)
        self.addCleanup(lambda: os.environ.pop("FAKE_CTX_TOKENS", None))
        T.create_ticket(self.tickets_dir, owner="judge", title="Проверка A")
        T.create_ticket(self.tickets_dir, owner="judge", title="Проверка B")

        D.tick()
        self.wait_running()
        D.tick()
        self.wait_running()

        calls = [json.loads(l) for l in (self.tickets_dir.parent / "calls.jsonl").read_text(encoding="utf-8")
                 .splitlines()]
        self.assertEqual(len(calls), 2)
        self.assertNotEqual(calls[0]["tid"], calls[1]["tid"], "вторая задача должна была дождаться своей очереди")
        self.assertIsNone(calls[1]["resumed"], "контекст первой сессии превысил порог — вторая начинает новую")
        self.assertIn("Начинаем новую сессию", calls[1]["prompt"])
        self.assertIn(".claude/roles/notes/judge.md", calls[1]["prompt"])

    def test_multiturn_run_does_not_rotate_on_summed_usage(self):
        """CEO 27.09: usage — сумма по ходам запуска. Многоходовой прогон с большой суммой, но
        нормальным контекстом последнего хода, НЕ должен рвать долгую сессию (раньше рвал — баг)."""
        D.SESSION_SCOPE["judge"] = "role"
        self.set_fake_bin(FAKE_BIN_RECORD)
        # сумма по 5 ходам (≈ 1,25×порога) за порогом, но на ход — четверть порога, сильно меньше
        per_turn = D.ROTATE_TOKENS // 4
        os.environ["FAKE_CTX_TOKENS"] = str(per_turn * 5)
        os.environ["FAKE_NUM_TURNS"] = "5"
        self.assertGreater(per_turn * 5, D.ROTATE_TOKENS, "сумма должна была бы превышать порог")
        self.assertLess(per_turn, D.ROTATE_TOKENS, "а контекст хода — нет")
        self.addCleanup(lambda: os.environ.pop("FAKE_CTX_TOKENS", None))
        self.addCleanup(lambda: os.environ.pop("FAKE_NUM_TURNS", None))
        T.create_ticket(self.tickets_dir, owner="judge", title="Проверка A")
        T.create_ticket(self.tickets_dir, owner="judge", title="Проверка B")

        D.tick()
        self.wait_running()
        D.tick()
        self.wait_running()

        calls = [json.loads(l) for l in (self.tickets_dir.parent / "calls.jsonl").read_text(encoding="utf-8")
                 .splitlines()]
        self.assertEqual(len(calls), 2)
        self.assertIsNotNone(calls[1]["resumed"], "контекст ХОДА не превышен — сессия должна продолжиться")
        self.assertNotIn("Начинаем новую сессию", calls[1]["prompt"])

    def test_stuck_todo_after_log_retries_then_blocks(self):
        """TK-070 п.6: роль оставила запись и не трогала status — диспетчер уже поставил in_progress при старте: ни повтора, ни blocked."""
        self.set_fake_bin(FAKE_BIN_STUCK_TODO)
        path = T.create_ticket(self.tickets_dir, owner="researcher", title="Забывчивый")
        D.tick()
        self.wait_running()
        tkt = T.read_ticket(path)
        self.assertEqual(tkt.status, "in_progress")
        # 2 записи роли (исходная + повтор) + 1 запись dispatcher про блокировку
        self.assertEqual(len(tkt.log), 1)
        inbox = D.CEO_INBOX.read_text(encoding="utf-8") if D.CEO_INBOX.exists() else ""
        self.assertNotIn("todo дважды подряд", inbox)

    def test_min_gap_prevents_immediate_relaunch_via_tick(self):
        """v1.1: MIN_GAP_S — троттлинг, не ошибка; ticket остаётся todo, просто не запускается сразу."""
        self.set_fake_bin(FAKE_BIN_SILENT)
        path = T.create_ticket(self.tickets_dir, owner="researcher", title="Слишком часто")
        state = D.load_state()
        D._record_launch(state, path.stem, datetime.now().astimezone())
        D.save_state(state)
        n = D.tick()
        self.assertEqual(n, 0, "MIN_GAP_S должен был не дать перезапуститься сразу")
        self.assertEqual(D.RUNNING, {})

    def _assert_no_money_signals(self):
        """В-173: ни одна строка про деньги не попадает ни в ceo-inbox, ни в ceo-wake.log."""
        for f in (D.CEO_INBOX, D.CEO_WAKE_LOG):
            text = f.read_text(encoding="utf-8") if f.exists() else ""
            for needle in ("budget", "бюджет", "суточный", "скорость трат", "потолок стоимости"):
                self.assertNotIn(needle, text, f"{f.name}: {needle}")

    def test_daily_cost_never_blocks_launches(self):
        """В-149/В-173: суточный расход только учитывается — запуск идёт, строки про деньги нет."""
        self.set_fake_bin(FAKE_BIN_SILENT)
        state = D.load_state()
        D._add_cost(state, datetime.now().astimezone(), 100000.0)
        D.save_state(state)
        T.create_ticket(self.tickets_dir, owner="researcher", title="Сутки дорогие")
        n = D.tick()
        self.assertEqual(n, 1)
        self.assertEqual(len(D.RUNNING), 1)
        self._assert_no_money_signals()

    def test_hour_cost_never_blocks_launches(self):
        """В-149/В-173: скорость трат за час только учитывается — запуск идёт, строки про деньги нет."""
        self.set_fake_bin(FAKE_BIN_SILENT)
        state = D.load_state()
        D._record_cost_event(state, datetime.now().astimezone(), 100000.0)
        D.save_state(state)
        T.create_ticket(self.tickets_dir, owner="researcher", title="Час дорогой")
        n = D.tick()
        self.assertEqual(n, 1)
        self.assertEqual(len(D.RUNNING), 1)
        self._assert_no_money_signals()

    def test_ticket_spend_never_blocks_launch_nor_signals_ceo(self):
        """В-173: сколько бы ни было потрачено по задаче — запуск идёт, статус не меняется, сигнала `budget`
        нет (раньше: новые запуски стояли + одна строка CEO)."""
        self.set_fake_bin(FAKE_BIN_SILENT)
        expensive = T.create_ticket(self.tickets_dir, owner="researcher", title="Дорогая")
        cheap = T.create_ticket(self.tickets_dir, owner="engineer", title="Обычная")
        state = D.load_state()
        D.add_ticket_cost(state, expensive.stem, 500.0)
        D.save_state(state)
        D.MAX_PARALLEL = 2
        n = D.tick()
        self.assertEqual(n, 2, "запускаются обе — расход задачи ничего не блокирует")
        self.assertIn(expensive.stem, D.RUNNING)
        self.assertIn(cheap.stem, D.RUNNING)
        self.assertEqual(T.read_ticket(expensive).status, "in_progress", "TK-070 п.6: старт владельца ставит in_progress, не blocked")
        self._assert_no_money_signals()
        self.wait_running()
        D.tick()
        D.tick()
        self._assert_no_money_signals()

    def test_money_no_json_is_undercount_not_run_cap(self):
        """v1.4 (судья TK-002 п.1д): запуск без JSON (убит) не досчитывается потолком запуска — иначе
        двойной счёт, если та же сессия потом продолжится (разница на resume уже подберёт реальное).
        Принимаем недоучёт на этот раз, не гадаем числом (было — списывали потолок запуска, v1.3)."""
        state = D.load_state()
        run_file = self.dispatcher_dir / "nocost.json"
        run_file.write_text("", encoding="utf-8")
        info = {"role": "engineer", "popen": None, "pid": None, "started": dt("2026-09-27T12:00:00+04:00"),
                "attempt": 0, "run_file": run_file, "err_file": run_file, "out_fh": None, "err_fh": None,
                "reason": "todo", "status_at_launch": "todo"}
        T.create_ticket(self.tickets_dir, owner="engineer", title="Без JSON")
        tid = T.list_tickets(self.tickets_dir)[0].stem
        info_by_tid = dict(info)
        D._finish_run(tid, info_by_tid, state, dt("2026-09-27T12:05:00+04:00"), timed_out=True)
        self.assertAlmostEqual(D.ticket_cost_spent(state, tid), 0.0)
        self.assertAlmostEqual(state.get("daily_cost", {}).get("2026-09-27", 0.0), 0.0)

    def drop_running(self, tid):
        """Остановить и забыть запущенный фейковый повтор (закрыть дескрипторы — иначе Windows не удалит каталог)."""
        info = D.RUNNING.pop(tid)
        try:
            info["popen"].kill()
            info["popen"].wait(timeout=5)
        except Exception:
            pass
        for fh in (info.get("out_fh"), info.get("err_fh")):
            try:
                fh.close()
            except Exception:
                pass

    def idle_finish(self, path, cost, *, attempt=0, status_at_launch="todo"):
        run_file = self.dispatcher_dir / f"idle-{attempt}-{cost}.json"
        run_file.write_text(json.dumps({"session_id": "s1", "total_cost_usd": cost}), encoding="utf-8")
        info = {"role": "engineer", "popen": None, "pid": None, "started": dt("2026-09-27T12:00:00+04:00"),
                "attempt": attempt, "run_file": run_file, "err_file": run_file, "out_fh": None, "err_fh": None,
                "reason": "todo", "status_at_launch": status_at_launch}
        state = D.load_state()
        D._finish_run(path.stem, info, state, dt("2026-09-27T12:01:00+04:00"), timed_out=False)
        D.save_state(state)

    def test_idle_run_limit_is_a_number_not_money(self):
        """Холостой ход (запуск без записи и без смены статуса) — по ЧИСЛУ подряд (MAX_IDLE_RUNS, назначено, умолч. 2),
        не по долларам: при пределе 1 блокирует сразу, хоть запуск и стоил копейки; денежной доли потолка нет."""
        self.assertFalse(hasattr(D, "IDLE_RUN_CAP_FRACTION"))
        self.assertTrue(hasattr(D, "MAX_IDLE_RUNS"))
        D.MAX_IDLE_RUNS = 1
        self.addCleanup(lambda: setattr(D, "MAX_IDLE_RUNS", 2))
        path = T.create_ticket(self.tickets_dir, owner="engineer", title="Холостой", now=dt("2026-09-27T12:00:00+04:00"))
        self.idle_finish(path, 0.01)
        tkt = T.read_ticket(path)
        self.assertEqual(tkt.status, "blocked")
        self.assertIn("холостой ход", tkt.log[-1].text)
        self.assertIn("холостой ход", D.CEO_INBOX.read_text(encoding="utf-8"))
        self.assertEqual(D.RUNNING, {}, "без повтора")

    def test_default_idle_limit_is_two_retry_once_then_block(self):
        if P.env("DISPATCH_MAX_IDLE_RUNS"):
            self.skipTest("RPV_DISPATCH_MAX_IDLE_RUNS задан в окружении")
        self.assertEqual(D.MAX_IDLE_RUNS, 2)
        path = T.create_ticket(self.tickets_dir, owner="engineer", title="Дважды холостой",
                                now=dt("2026-09-27T12:00:00+04:00"))
        self.idle_finish(path, 0.01)
        self.assertEqual(T.read_ticket(path).status, "in_progress")   # первый холостой — обычный повтор (старт владельца ставит in_progress, TK-070 п.6)
        self.assertEqual(D.RUNNING[path.stem]["reason"], "retry")
        self.drop_running(path.stem)
        self.idle_finish(path, 0.02, attempt=1, status_at_launch="in_progress")
        tkt = T.read_ticket(path)
        self.assertEqual(tkt.status, "blocked")
        self.assertIn("холостой ход", tkt.log[-1].text)

    def test_idle_streak_resets_when_run_leaves_entry(self):
        path = T.create_ticket(self.tickets_dir, owner="engineer", title="Не подряд",
                                now=dt("2026-09-27T12:00:00+04:00"))
        T.write_header_updates(path, {"status": "in_progress"}, now=dt("2026-09-27T12:00:00+04:00"))
        self.idle_finish(path, 0.01, status_at_launch="in_progress")           # холостой 1 → повтор
        self.drop_running(path.stem)
        keys = T.role_entry_keys(T.read_ticket(path), "engineer")
        T.append_log(path, "engineer", "шаг", now=dt("2026-09-27T12:00:30+04:00"))
        run_file = self.dispatcher_dir / "logged.json"
        run_file.write_text(json.dumps({"session_id": "s1", "total_cost_usd": 0.01}), encoding="utf-8")
        info = {"role": "engineer", "popen": None, "pid": None, "started": dt("2026-09-27T12:00:00+04:00"),
                "attempt": 1, "run_file": run_file, "err_file": run_file, "out_fh": None, "err_fh": None,
                "reason": "retry", "status_at_launch": "in_progress", "log_keys_at_launch": keys}
        state = D.load_state()
        D._finish_run(path.stem, info, state, dt("2026-09-27T12:02:00+04:00"), timed_out=False)
        D.save_state(state)
        self.assertEqual(state.get("idle_runs", {}).get(f"{path.stem}::engineer", 0), 0)
        self.assertEqual(T.read_ticket(path).status, "in_progress")

    def test_idle_run_does_not_fire_when_status_changed(self):
        """Статус изменился (роль что-то сделала) — не холостой ход, предел не срабатывает даже при 1."""
        D.MAX_IDLE_RUNS = 1
        self.addCleanup(lambda: setattr(D, "MAX_IDLE_RUNS", 2))
        path = T.create_ticket(self.tickets_dir, owner="engineer", title="Не холостой",
                                now=dt("2026-09-27T12:00:00+04:00"))
        T.write_header_updates(path, {"status": "waiting", "wait_for": "file:/nope"},
                                now=dt("2026-09-27T12:00:30+04:00"))
        self.idle_finish(path, 0.01)
        self.assertEqual(T.read_ticket(path).status, "waiting")  # не blocked — статус роль таки сменила

    def test_money_model_usage_warning_reaches_ceo_inbox(self):
        path = T.create_ticket(self.tickets_dir, owner="engineer", title="Не opus",
                                now=dt("2026-09-27T12:00:00+04:00"))
        tid = path.stem
        T.append_log(path, "engineer", "готово", now=dt("2026-09-27T12:00:30+04:00"))
        state = D.load_state()
        run_file = self.dispatcher_dir / "badmodel.json"
        run_file.write_text(json.dumps({"session_id": "s1", "total_cost_usd": 0.1,
                                         "modelUsage": {"fable-5-1": {"cost": 0.1}}}), encoding="utf-8")
        info = {"role": "engineer", "popen": None, "pid": None, "started": dt("2026-09-27T12:00:00+04:00"),
                "attempt": 0, "run_file": run_file, "err_file": run_file, "out_fh": None, "err_fh": None,
                "reason": "todo", "status_at_launch": "todo"}
        D._finish_run(tid, info, state, dt("2026-09-27T12:01:00+04:00"), timed_out=False)
        self.assertIn("fable-5-1", D.CEO_INBOX.read_text(encoding="utf-8"))

    def test_finish_run_expects_model_family_of_the_runs_role(self):
        """v1.6.1: Судья на opus — тишина в ceo-inbox; тот же opus у инженера (ждём sonnet) — тревога."""
        orig_role_model = D.ROLE_MODEL
        D.ROLE_MODEL = {"judge": "claude-opus-5-5", "engineer": "claude-sonnet-5-5",
                        "researcher": "claude-sonnet-5-5"}
        try:
            for role, sid, warns in (("judge", "s-j", False), ("engineer", "s-e", True)):
                path = T.create_ticket(self.tickets_dir, owner=role, title=f"Модель {role}",
                                        now=dt("2026-09-27T12:00:00+04:00"))
                tid = path.stem
                T.append_log(path, role, "готово", now=dt("2026-09-27T12:00:30+04:00"))
                state = D.load_state()
                run_file = self.dispatcher_dir / f"model-{role}.json"
                run_file.write_text(json.dumps({"session_id": sid, "total_cost_usd": 0.1,
                                                 "modelUsage": {"claude-opus-5-5": {"costUSD": 0.1}}}),
                                     encoding="utf-8")
                info = {"role": role, "popen": None, "pid": None, "started": dt("2026-09-27T12:00:00+04:00"),
                        "attempt": 0, "run_file": run_file, "err_file": run_file, "out_fh": None, "err_fh": None,
                        "reason": "todo", "status_at_launch": "todo"}
                D._finish_run(tid, info, state, dt("2026-09-27T12:01:00+04:00"), timed_out=False)
                inbox = D.CEO_INBOX.read_text(encoding="utf-8") if D.CEO_INBOX.exists() else ""
                self.assertEqual("modelUsage" in inbox, warns, f"{role}: {inbox!r}")
        finally:
            D.ROLE_MODEL = orig_role_model

    def test_recover_active_runs_adopts_alive_process(self):
        """v1.1: перезапуск диспетчера во время прогона — живой pid подхватывается, не запускается повторно."""
        self.set_fake_bin(FAKE_BIN_SLOW_OK)
        path = T.create_ticket(self.tickets_dir, owner="researcher", title="Долгий")
        D.tick()
        self.assertIn(path.stem, D.RUNNING)
        pid = D.RUNNING[path.stem]["pid"]
        real_popen = D.RUNNING[path.stem]["popen"]
        # закрываем родительские файловые дескрипторы сразу (не через addCleanup — тот выполняется
        # ПОСЛЕ tearDown, а tearDown уже пытается удалить временный каталог на Windows)
        D.RUNNING[path.stem]["out_fh"].close()
        D.RUNNING[path.stem]["err_fh"].close()

        # "перезапуск диспетчера": теряем всё, что жило только в памяти процесса
        D.RUNNING.clear()
        state = D.load_state()
        self.assertIn(path.stem, state.get("active_runs", {}), "зеркало в state.json должно было остаться")

        D.recover_active_runs(state, datetime.now().astimezone())
        self.assertIn(path.stem, D.RUNNING, "живой pid должен быть подхвачен, не потерян")
        self.assertIsNone(D.RUNNING[path.stem]["popen"])
        self.assertEqual(D.RUNNING[path.stem]["pid"], pid)
        D.save_state(state)

        self.wait_running()  # доиграть до конца по pid (_pid_alive/_finish_run), без второго запуска
        real_popen.wait(timeout=5)  # реап собственного дочернего процесса (уже завершился)
        tkt = T.read_ticket(path)
        self.assertEqual(len(tkt.log), 1, "recover не должен был запустить процесс повторно")
        self.assertEqual(tkt.status, "done")

    def test_recover_adopts_role_killed_before_pid_was_mirrored(self):
        """Диспетчер убит между Popen роли и записью pid: зеркало-намерение без pid — роль находят по session_id."""
        path = T.create_ticket(self.tickets_dir, owner="researcher", title="Сирота")
        sid = "orphan-sid-4242"
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "--session-id", sid])
        self.addCleanup(lambda: (proc.kill(), proc.wait(timeout=10)))
        state = {"active_runs": {path.stem: {
            "role": "researcher", "pid": None, "started": T.now_iso(datetime.now().astimezone()), "attempt": 0,
            "run_file": str(self.tickets_dir.parent / "r.json"), "err_file": str(self.tickets_dir.parent / "r.err"),
            "reason": "todo", "status_at_launch": "in_progress", "session_id": sid}}}
        orig = D.PID_EXPECT_NAME
        D.PID_EXPECT_NAME = ""
        try:
            D.recover_active_runs(state, datetime.now().astimezone())
            self.assertEqual(D.RUNNING[path.stem]["pid"], proc.pid)
            self.assertEqual(state["active_runs"][path.stem]["pid"], proc.pid)
        finally:
            D.PID_EXPECT_NAME = orig
            D.RUNNING.clear()

    def test_recover_drops_intent_mirror_when_role_never_started(self):
        path = T.create_ticket(self.tickets_dir, owner="researcher", title="Не стартовала")
        state = {"active_runs": {path.stem: {
            "role": "researcher", "pid": None, "started": T.now_iso(datetime.now().astimezone()), "attempt": 0,
            "run_file": str(self.tickets_dir.parent / "r.json"), "err_file": "", "reason": "todo",
            "status_at_launch": "todo", "session_id": "never-started-sid-1"}}}
        from unittest import mock
        with mock.patch.object(D, "_find_pid_by_session", return_value=None), mock.patch.object(D, "_finish_run") as fin:
            D.recover_active_runs(state, datetime.now().astimezone())
        fin.assert_not_called()
        self.assertEqual((state["active_runs"], dict(D.RUNNING)), ({}, {}))

    def _find_by_session(self, os_name, stdout, sid="sid-tree-1"):
        from unittest import mock
        res = mock.Mock(stdout=stdout)
        with mock.patch.object(D.os, "name", os_name), mock.patch.object(D.os, "getpid", return_value=1),                 mock.patch.object(D.subprocess, "run", return_value=res):
            return D._find_pid_by_session(sid)

    def test_find_pid_by_session_picks_tree_root_posix(self):
        out = ("  100 1 supervisor\n"
               "  200 999 sh -c claude --session-id sid-tree-1\n"
               "  300 200 node claude --session-id sid-tree-1\n"
               "  400 300 node worker --session-id sid-tree-1\n")
        self.assertEqual(self._find_by_session("posix", out), 200)

    def test_find_pid_by_session_picks_tree_root_windows(self):
        self.assertEqual(self._find_by_session("nt", "5000 4000\n4000 1\n"), 4000)

    def test_find_pid_by_session_two_independent_roots_takes_min(self):
        out = "  700 1 claude --session-id sid-tree-1\n  300 1 claude --resume sid-tree-1\n"
        self.assertEqual(self._find_by_session("posix", out), 300)

    def test_recover_no_process_but_output_is_finished_as_completed(self):
        from unittest import mock
        path = T.create_ticket(self.tickets_dir, owner="researcher", title="Отработала без диспетчера")
        run_file = self.tickets_dir.parent / "r-done.json"
        run_file.write_text('{"result": "ok"}', encoding="utf-8")
        state = {"active_runs": {path.stem: {
            "role": "researcher", "pid": None, "started": T.now_iso(datetime.now().astimezone()), "attempt": 0,
            "run_file": str(run_file), "err_file": "", "reason": "todo",
            "status_at_launch": "todo", "session_id": "finished-sid-1"}}}
        with mock.patch.object(D, "_find_pid_by_session", return_value=None),                 mock.patch.object(D, "_finish_run") as fin:
            D.recover_active_runs(state, datetime.now().astimezone())
        self.assertEqual(fin.call_count, 1)
        self.assertEqual((state["active_runs"], dict(D.RUNNING)), ({}, {}))

    def test_launch_run_mirrors_intent_before_popen(self):
        path = T.create_ticket(self.tickets_dir, owner="researcher", title="Намерение")
        seen = {}

        def boom(cmd, **kw):
            seen["mirror"] = dict(D.load_state().get("active_runs", {}).get(path.stem) or {})
            raise OSError("popen failed")
        state = D.load_state()
        with mock.patch.object(D, "_popen", boom), self.assertRaises(OSError):
            D.launch_run(path, "researcher", state, datetime.now().astimezone(), reason="todo")
        self.assertIsNone(seen["mirror"].get("pid", "x"))
        self.assertTrue(seen["mirror"].get("session_id"))
        self.assertNotIn(path.stem, D.load_state().get("active_runs", {}))

    def test_recover_active_runs_processes_finished_while_down(self):
        """v1.1: процесс успел закончиться, пока диспетчер не работал — recover доводит его до конца сам."""
        self.set_fake_bin(FAKE_BIN_OK)
        path = T.create_ticket(self.tickets_dir, owner="researcher", title="Быстрый")
        D.tick()
        self.assertIn(path.stem, D.RUNNING)
        real_popen = D.RUNNING[path.stem]["popen"]
        real_popen.wait(timeout=10)  # дождались настоящего завершения процесса
        D.RUNNING[path.stem]["out_fh"].close()
        D.RUNNING[path.stem]["err_fh"].close()
        D.RUNNING.clear()  # "перезапуск" — без вызова _poll_running/_finish_run

        state = D.load_state()
        self.assertIn(path.stem, state.get("active_runs", {}), "зеркало должно остаться, раз мы не поллили")
        D.recover_active_runs(state, datetime.now().astimezone())
        D.save_state(state)

        self.assertEqual(D.RUNNING, {}, "процесс уже мёртв — не должен попасть в RUNNING")
        self.assertNotIn(path.stem, D.load_state().get("active_runs", {}))
        tkt = T.read_ticket(path)
        self.assertEqual(len(tkt.log), 1)
        self.assertEqual(tkt.header.get("status"), "done")

    def _slow_role_mirror(self, title):
        self.set_fake_bin(FAKE_BIN_SLOW_OK)
        path = T.create_ticket(self.tickets_dir, owner="researcher", title=title)
        D.tick()
        run = D.RUNNING[path.stem]
        run["out_fh"].close()
        run["err_fh"].close()
        D.RUNNING.clear()
        return path, run, D.load_state()

    def test_recover_adopts_role_under_foreign_image_name_by_pid_and_start(self):
        """#16: роль под другим именем образа (npm-установка — node, не claude) после рестарта диспетчера жива: подхват по
        pid + времени старта, повторного запуска и конца прогона нет."""
        orig = D.PID_EXPECT_NAME
        D.PID_EXPECT_NAME = "claude-image-that-this-process-does-not-have"
        try:
            path, run, state = self._slow_role_mirror("Чужое имя образа")
            saved = state["active_runs"][path.stem]
            self.assertTrue(saved.get("pstart"), "метка старта записана при запуске")
            D.recover_active_runs(state, datetime.now().astimezone())
            self.assertIn(path.stem, D.RUNNING, "живая роль не должна считаться мёртвой из-за имени образа")
            D.save_state(state)
            D.PID_EXPECT_NAME = python_image_name()
            self.wait_running()
            run["popen"].wait(timeout=5)
            self.assertEqual(len(T.read_ticket(path).log), 1, "второго запуска поверх живой роли нет")
        finally:
            D.PID_EXPECT_NAME = orig

    def test_recover_treats_pid_with_other_start_time_as_finished(self):
        """#16: pid переиспользован другим процессом (метка старта другая) — роль мертва, как бы ни звался образ."""
        path, run, state = self._slow_role_mirror("Pid занят чужим")
        state["active_runs"][path.stem]["pstart"] = "1"
        D.recover_active_runs(state, datetime.now().astimezone())
        self.assertNotEqual(D.RUNNING.get(path.stem, {}).get("pid"), run["pid"], "старый pid не отслеживается как живой")
        run["popen"].wait(timeout=10)
        for info in D.RUNNING.values():
            _kill = D._kill_proc
            _kill(info)

    def test_pid_alive_start_mark_overrides_image_name_and_old_mirror_falls_back_to_name(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            mark = D._proc_start(child.pid)
            self.assertTrue(mark)
            self.assertTrue(D._pid_alive(child.pid, "no-such-image", start=mark))
            self.assertFalse(D._pid_alive(child.pid, "no-such-image", start=mark + "0"))
            self.assertFalse(D._pid_alive(child.pid, "no-such-image"), "зеркало без метки — прежняя проверка по имени")
            self.assertTrue(D._pid_alive(child.pid, python_image_name()))
        finally:
            child.kill()
            child.wait(timeout=10)
        self.assertFalse(D._pid_alive(child.pid, "", start=mark))

    @unittest.skipIf(os.name == "nt", "ps lstart — только Linux/macOS")
    def test_ps_start_mark_does_not_depend_on_locale_or_timezone(self):
        """#16: диспетчер под launchd (локаль C) и из терминала (ru_RU, свой пояс) получают одну метку старта."""
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        saved = {k: os.environ.get(k) for k in ("LANG", "LC_ALL", "LC_TIME", "TZ")}
        try:
            os.environ.update(LANG="ru_RU.UTF-8", LC_TIME="ru_RU.UTF-8", TZ="Asia/Dubai")
            os.environ.pop("LC_ALL", None)
            first = D._ps_field(child.pid, "lstart")
            for k in saved:
                os.environ.pop(k, None)
            second = D._ps_field(child.pid, "lstart")
            self.assertTrue(first)
            self.assertEqual(first, second)
        finally:
            for k, v in saved.items():
                os.environ.pop(k, None)
                if v is not None:
                    os.environ[k] = v
            child.kill()
            child.wait(timeout=10)

    def test_pid_alive_name_windows_tasklist_oem_bytes_do_not_break_decode(self):
        """Windows: tasklist печатает в OEM-кодировке (cp866), а под PYTHONUTF8=1 text=True читал её как utf-8 — поток
        чтения падал, stdout пустой, живой pid считался мёртвым (doctor: ложный FAIL диспетчера, подхват «как завершённый»)."""
        raw = b'"python.exe","4444","Console","1","35\xa0176 \x8a"\r\n'
        real_run = subprocess.run

        def fake_run(cmd, **kw):
            if cmd and cmd[0] == "tasklist":
                if kw.get("text") and not kw.get("errors"):
                    raw.decode(kw.get("encoding") or "utf-8")  # как _readerthread: строгая декодировка
                return subprocess.CompletedProcess(cmd, 0, stdout=raw.decode(kw.get("encoding") or "utf-8", kw.get("errors") or "strict"))
            return real_run(cmd, **kw)

        with mock.patch.object(D.os, "name", "nt"), mock.patch.object(D.subprocess, "run", fake_run):
            self.assertTrue(D._pid_alive_name(4444, "python"))
            self.assertFalse(D._pid_alive_name(4445, "python"))

    def test_pid_alive_unreadable_start_mark_with_live_process_is_alive(self):
        """#16: метка записана, но сейчас её не прочитать (сбой ps) — процесс есть, имя образа не судья."""
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        orig = D._proc_start
        try:
            D._proc_start = lambda pid: None
            self.assertTrue(D._pid_alive(child.pid, "no-such-image", start="123"))
        finally:
            D._proc_start = orig
            child.kill()
            child.wait(timeout=10)
        self.assertFalse(D._pid_alive(child.pid, "", start="123"))

    def test_ceo_mention_in_text_is_plain_text_status_signals_still_work(self):
        """v2: @ceo в записи — обычный текст, строки CEO нет и роль не стартует; сигнал даёт статус needs_owner."""
        path = T.create_ticket(self.tickets_dir, owner="researcher", title="Для CEO",
                                now=dt("2026-09-27T12:00:00+04:00"))
        T.write_header_updates(path, {"status": "waiting"}, now=dt("2026-09-27T12:00:30+04:00"))
        T.append_log(path, "researcher", "Нужно решение владельца. @ceo подскажи.",
                     now=dt("2026-09-27T12:01:00+04:00"))
        D.tick(now=dt("2026-09-27T12:02:00+04:00"))
        self.assertFalse(D.CEO_INBOX.exists() and D.CEO_INBOX.read_text(encoding="utf-8").strip())
        self.assertEqual(D.RUNNING, {})
        T.write_header_updates(path, {"status": "needs_owner"}, now=dt("2026-09-27T12:03:00+04:00"))
        D.tick(now=dt("2026-09-27T12:04:00+04:00"))
        inbox = D.CEO_INBOX.read_text(encoding="utf-8")
        self.assertIn("needs_owner", inbox)
        self.assertEqual(D.RUNNING, {})  # ceo не запускается диспетчером как роль

    def test_ceo_wake_log_mirrors_inbox(self):
        """v1.1: каждая запись ceo-inbox.md дублируется короткой строкой в ceo-wake.log (Monitor CEO)."""
        path = T.create_ticket(self.tickets_dir, owner="researcher", title="Для CEO",
                                now=dt("2026-09-27T12:00:00+04:00"))
        T.append_log(path, "researcher", "Нужно решение владельца.", now=dt("2026-09-27T12:01:00+04:00"))
        T.write_header_updates(path, {"status": "needs_owner"}, now=dt("2026-09-27T12:01:00+04:00"))
        D.tick(now=dt("2026-09-27T12:02:00+04:00"))
        self.assertTrue(D.CEO_WAKE_LOG.exists())
        wake = D.CEO_WAKE_LOG.read_text(encoding="utf-8")
        self.assertIn(path.stem, wake)
        self.assertIn("needs_owner", wake)
        # столько же строк, сколько записей ушло в ceo-inbox.md за этот тик
        inbox_lines = [ln for ln in D.CEO_INBOX.read_text(encoding="utf-8").splitlines() if ln.strip()]
        wake_lines = [ln for ln in wake.splitlines() if ln.strip()]
        self.assertEqual(len(wake_lines), len(inbox_lines))

    def test_sim5_parse_error_flood_is_deduped(self):
        """(5) обязательно: сломанный тикет — одна строка в ceo-inbox, не строка на каждый тик."""
        (self.tickets_dir / "X5.md").write_text("нет шапки тут\n", encoding="utf-8")
        for i in range(3):
            D.tick(now=dt("2026-09-27T12:00:00+04:00") + timedelta(seconds=15 * i))
        inbox = D.CEO_INBOX.read_text(encoding="utf-8") if D.CEO_INBOX.exists() else ""
        self.assertEqual(inbox.count("parse-error"), 1, "3 тика с одной и той же ошибкой — одна строка")

    def test_tick_reports_waiting_with_unknown_wait_for_once_and_launches_nobody(self):
        """v4: свободный текст в wait_for — одна строка CEO за несколько тиков, роль не запускается."""
        path = T.create_ticket(self.tickets_dir, owner="engineer", title="Ждёт непонятно",
                                now=dt("2026-09-27T12:00:00+04:00"))
        T.write_header_updates(path, {"status": "waiting"}, now=dt("2026-09-27T12:00:10+04:00"))
        text = path.read_text(encoding="utf-8").replace(
            "wait_for: \n", "wait_for: прогон на сервере — готов, когда done=total\n")  # роль правит шапку руками
        path.write_text(text, encoding="utf-8")
        for i in range(3):
            self.assertEqual(D.tick(now=dt("2026-09-27T12:00:30+04:00") + timedelta(seconds=15 * i)), 0)
        inbox = D.CEO_INBOX.read_text(encoding="utf-8")
        self.assertEqual(inbox.count("wait_for не понят"), 1)
        self.assertEqual(D.RUNNING, {})

    def test_notify_parse_error_renotifies_on_different_text(self):
        """Дедуп ключом (тикет, ТЕКСТ ошибки) — сменился текст ошибки, значит сменилась причина."""
        state = {}
        D.notify_parse_error("X5", "ValueError: тикет без шапки", state, dt("2026-09-27T12:00:00+04:00"))
        D.notify_parse_error("X5", "ValueError: тикет без шапки", state, dt("2026-09-27T12:00:15+04:00"))
        D.notify_parse_error("X5", "UnicodeDecodeError: 'utf-8' codec can't decode byte", state,
                              dt("2026-09-27T12:00:30+04:00"))
        self.assertEqual(D.CEO_INBOX.read_text(encoding="utf-8").count("parse-error"), 2)

    def test_done_without_reviewer_signals_ceo_one_line(self):
        """v2: done без reviewer (теперь норма) — ровно одна строка CEO «done»; дедуп по `updated`."""
        path = T.create_ticket(self.tickets_dir, owner="researcher", title="Без ревью",
                                now=dt("2026-09-27T12:00:00+04:00"))
        T.write_header_updates(path, {"status": "waiting"}, now=dt("2026-09-27T12:00:10+04:00"))
        D.tick(now=dt("2026-09-27T12:00:20+04:00"))  # первый тик задаёт базу «известных» done
        T.append_log(path, "researcher", "готово, числа: KPI 0,097", now=dt("2026-09-27T12:01:00+04:00"))
        T.write_header_updates(path, {"status": "done"}, now=dt("2026-09-27T12:01:00+04:00"))
        D.tick(now=dt("2026-09-27T12:02:00+04:00"))
        inbox = D.CEO_INBOX.read_text(encoding="utf-8")
        self.assertEqual(inbox.count("[done]"), 1)
        self.assertIn("Без ревью", inbox)
        self.assertIn("KPI 0,097", inbox)
        self.assertEqual(D.RUNNING, {})  # без reviewer некого запускать
        D.tick(now=dt("2026-09-27T12:02:30+04:00"))  # дедуп: тот же updated — второй строки нет
        self.assertEqual(D.CEO_INBOX.read_text(encoding="utf-8").count("[done]"), 1)

    # --- `tickets.py stop`: остановка роли посреди запуска (CEO, 03.10) ------------------------------------------------

    def _alive(self, pid):
        return D._pid_alive(pid, python_image_name())

    def start_tree_run(self, owner="researcher"):
        """Фейковая роль с потомком, оба «висят»: (путь тикета, pid потомка)."""
        self.set_fake_bin(FAKE_BIN_TREE)
        path = T.create_ticket(self.tickets_dir, owner=owner, title="Долгий запуск")
        T.write_header_updates(path, {"status": "in_progress"})
        D.tick()
        self.assertIn(path.stem, D.RUNNING)
        pid_file = self.base / "child.pid"
        deadline = time.time() + 30
        while not (pid_file.exists() and pid_file.read_text().strip()) and time.time() < deadline:
            time.sleep(0.05)
        child = int(pid_file.read_text().strip())
        self.addCleanup(D._kill_tree, child)  # тест упал — потомок не остаётся жить
        return path, child

    def _stop_next_launch_cmd(self, tid):
        """Заявка `--next researcher` + тик: команда запуска, который диспетчер сделал в этом же тике."""
        calls = []
        D._popen = lambda cmd, **kw: (calls.append(list(cmd)), subprocess.Popen([sys.executable, "-c", "pass"], **kw))[1]
        D.write_stop_request(tid, "researcher", "Новая постановка")
        D.tick()
        self.assertEqual(len(calls), 1, "роль стартует в том же тике")
        return calls[0]

    def test_stop_kills_role_and_its_child_and_removes_request(self):
        path, child = self.start_tree_run()
        popen = D.RUNNING[path.stem]["popen"]
        D.write_stop_request(path.stem, "", "Новая постановка")
        D.tick()
        self.assertIsNotNone(popen.poll(), "роль снята")
        self.assertFalse(self._alive(child), "потомок роли снят вместе с ней")
        self.assertEqual(D.RUNNING, {})
        self.assertNotIn(path.stem, D.load_state().get("active_runs", {}))
        self.assertFalse((D.STOP_DIR / f"{path.stem}.json").exists(), "заявка разобрана")

    def test_stop_without_next_parks_ticket_as_stopped_and_is_not_a_failure(self):
        path, _ = self.start_tree_run()
        D.write_stop_request(path.stem, "", "Новая постановка: сделай X")
        D.tick()
        tkt = T.read_ticket(path)
        self.assertEqual(tkt.status, "stopped")
        self.assertEqual(tkt.next_role, "")
        trace, entry = tkt.log[-2], tkt.log[-1]
        self.assertEqual(trace.author, "dispatcher")
        self.assertRegex(trace.text, r"^остановлен CEO в \d\d:\d\d")
        self.assertEqual((entry.author, entry.text), ("ceo", "Новая постановка: сделай X"))
        self.assertIn("status=stopped", D.RUNS_LOG.read_text(encoding="utf-8"))
        for _ in range(3):
            self.assertEqual(D.tick(), 0, "stopped никого не будит")
        self.assertEqual(D.RUNNING, {}, "ни повтора, ни запуска")
        inbox = D.CEO_INBOX.read_text(encoding="utf-8") if D.CEO_INBOX.exists() else ""
        self.assertNotIn("blocked", inbox)
        self.assertFalse(any("не оставил" in e.text or "заблокирована" in e.text for e in tkt.log))

    def test_stop_with_next_starts_role_at_once_in_new_session_with_note(self):
        path, _ = self.start_tree_run()
        tid = path.stem
        cmd = self._stop_next_launch_cmd(tid)
        self.assertNotIn("--resume", cmd, "новая сессия, не продолжение прежней")
        prompt = cmd[cmd.index("-p") + 1]
        self.assertIn("рошлый запуск оборван CEO — проверь git status и недописанные правки, начни с новой постановки",
                      prompt)
        self.assertEqual(D.RUNNING[tid]["reason"], "next")
        tkt = T.read_ticket(path)
        self.assertEqual((tkt.status, tkt.next_role), ("in_progress", ""), "next погашен запуском")
        self.assertEqual((tkt.log[-1].author, tkt.log[-1].text), ("ceo", "Новая постановка"))
        self.assertNotIn(tid, D.load_state().get("stopped_runs", {}), "пометка снята одним запуском")

    def test_stop_new_session_also_when_session_scope_is_role(self):
        """scope «role»: сессия общая на роль и у тикета её не стереть — новая сессия идёт по пометке запуска."""
        D.SESSION_SCOPE["researcher"] = "role"
        path, _ = self.start_tree_run()
        state = D.load_state()
        state.setdefault("role_sessions", {})["researcher"] = {"session_id": "shared-sess", "last_context_tokens": 5}
        D.save_state(state)
        cmd = self._stop_next_launch_cmd(path.stem)
        self.assertNotIn("--resume", cmd)
        self.assertIn("оборван CEO", cmd[cmd.index("-p") + 1])

    def test_stop_resets_loop_counters_rotation_and_launch_history(self):
        path, _ = self.start_tree_run()
        tid, key = path.stem, f"{path.stem}::researcher"
        state = D.load_state()
        state["idle_runs"] = {key: 1}
        state["same_status_runs"] = {key: 5}
        state.setdefault("sessions", {}).setdefault(key, {})["retries"] = 1
        state["ticket_sessions"] = {key: {"session_id": "old", "last_context_tokens": D.ROTATE_TOKENS + 1}}
        D.save_state(state)
        self.assertIn(tid, state["launch_history"])
        D.write_stop_request(tid, "", "x")
        D.tick()
        state = D.load_state()
        self.assertNotIn(key, state["idle_runs"])
        self.assertNotIn(key, state["same_status_runs"])
        self.assertEqual(state["sessions"][key]["retries"], 0)
        self.assertEqual(state["ticket_sessions"][key], {}, "сессия и контекст — с нуля")
        self.assertNotIn(tid, state["launch_history"], "часовой лимит запусков по тикету — с нуля")

    def test_stop_without_running_role_still_applies_status_and_entry(self):
        path = T.create_ticket(self.tickets_dir, owner="engineer", title="Не запущена")
        T.write_header_updates(path, {"status": "waiting"})
        D.write_stop_request(path.stem, "", "Новая постановка")
        D.tick()
        tkt = T.read_ticket(path)
        self.assertEqual(tkt.status, "stopped")
        self.assertIn("запущенной роли не было", tkt.log[-2].text)
        self.assertEqual(tkt.log[-1].author, "ceo")
        self.assertNotIn(path.stem, D.load_state().get("stopped_runs", {}), "прерывать было нечего")

    def test_stop_that_cannot_kill_keeps_request_and_tells_ceo_once(self):
        path, _ = self.start_tree_run()
        tid = path.stem
        real_kill = D._kill_tree
        D._kill_tree = lambda pid: None
        D.STOP_VERIFY_S = 0.3
        self.addCleanup(setattr, D, "_kill_tree", real_kill)
        D.write_stop_request(tid, "", "x")
        D.tick()
        D.tick()
        self.assertIn(tid, D.RUNNING)
        self.assertTrue((D.STOP_DIR / f"{tid}.json").exists(), "заявка ждёт следующего тика")
        self.assertEqual(T.read_ticket(path).status, "in_progress")
        self.assertEqual(D.CEO_INBOX.read_text(encoding="utf-8").count("[stop-failed]"), 1)
        D._kill_tree = real_kill
        D.tick()
        self.assertNotIn(tid, D.RUNNING)
        self.assertEqual(T.read_ticket(path).status, "stopped")

    def test_stop_reaches_run_restored_from_state_after_dispatcher_restart(self):
        path, child = self.start_tree_run()
        info = D.RUNNING[path.stem]
        real_popen = info["popen"]
        info["out_fh"].close()
        info["err_fh"].close()
        D.RUNNING.clear()  # «перезапуск диспетчера»: в памяти пусто, зеркало — в state.json
        D.write_stop_request(path.stem, "", "x")
        D.tick()  # recover_active_runs подхватывает по pid, затем заявка снимает его
        self.assertFalse(self._alive(child))
        real_popen.wait(timeout=5)
        self.assertEqual(T.read_ticket(path).status, "stopped")
        self.assertEqual(D.RUNNING, {})

    def test_stop_request_for_unknown_ticket_is_left_untouched(self):
        D.write_stop_request("TK-404", "", "x")
        D.tick()
        self.assertTrue((D.STOP_DIR / "TK-404.json").exists())

    def test_role_is_launched_in_its_own_process_group(self):
        self.set_fake_bin(FAKE_BIN_SILENT)
        seen = {}
        orig = D._popen
        D._popen = lambda cmd, **kw: (seen.update(kw), orig(cmd, **kw))[1]
        T.create_ticket(self.tickets_dir, owner="researcher", title="Группа")
        D.tick()
        if os.name == "nt":  # TK-105 п.6: скрытая консоль, а не её отсутствие
            self.assertTrue(seen["creationflags"] & 0x10)
            self.assertEqual(seen["startupinfo"].wShowWindow, 0)
        else:
            self.assertIs(seen.get("start_new_session"), True)

    def test_kill_tree_dispatches_by_platform(self):
        from unittest import mock
        with mock.patch.object(D, "_kill_tree_nt") as nt, mock.patch.object(D, "_kill_tree_posix") as px:
            with mock.patch.object(os, "name", "nt"):
                D._kill_tree(11)
            with mock.patch.object(os, "name", "posix"):
                D._kill_tree(22)
            D._kill_tree(0)
        nt.assert_called_once_with(11)
        px.assert_called_once_with(22)

    def test_kill_tree_nt_is_taskkill_tree_force(self):
        from unittest import mock
        with mock.patch.object(D.subprocess, "run") as run:
            D._kill_tree_nt(777)
        self.assertEqual(run.call_args[0][0], ["taskkill", "/T", "/F", "/PID", "777"])

    def test_kill_tree_posix_kills_group_of_leader_and_only_pid_of_non_leader(self):
        from unittest import mock
        sig = getattr(D.signal, "SIGKILL", 9)
        with mock.patch.object(os, "getpgid", create=True, return_value=4242) as gp, \
                mock.patch.object(os, "killpg", create=True) as kg, mock.patch.object(os, "kill") as kill:
            D._kill_tree_posix(4242)  # запущен со start_new_session: группа = pid
            kg.assert_called_once_with(4242, sig)
            kill.assert_not_called()
            kg.reset_mock()
            gp.return_value = 1  # сидит в чужой группе (запущен до правки): killpg снял бы и диспетчер
            D._kill_tree_posix(4242)
            kg.assert_not_called()
            kill.assert_called_once_with(4242, sig)

    def test_stopped_status_wakes_nobody_but_explicit_next_still_works(self):
        path = T.create_ticket(self.tickets_dir, owner="engineer", title="Остановлена")
        T.write_header_updates(path, {"status": "stopped"})
        now = datetime.now().astimezone()
        self.assertIsNone(D.decide(T.read_ticket(path), {}, now))
        T.write_header_updates(path, {"next": "judge"})
        dec = D.decide(T.read_ticket(path), {}, now)
        self.assertEqual((dec.role, dec.reason), ("judge", "next"))

    def test_sim6_timeout_with_logged_progress_is_not_a_failure(self):
        """(6) обязательно: RUN_TIMEOUT назван в промпте, а прогресс до таймаута — не провал."""
        self.assertIn(str(int(D.RUN_TIMEOUT // 60)), D.build_prompt("engineer", "X6"))
        path = T.create_ticket(self.tickets_dir, owner="engineer", title="Долгий шаг",
                                now=dt("2026-09-27T12:00:00+04:00"))
        T.write_header_updates(path, {"status": "in_progress"}, now=dt("2026-09-27T12:00:00+04:00"))
        started = dt("2026-09-27T12:00:00+04:00")
        T.append_log(path, "engineer", "сделал шаг 1 (артефакт a.csv), дальше шаг 2",
                     now=started + timedelta(minutes=20))

        class FakeTimedOutPopen:
            pid = 424242

            def poll(self):
                return None  # «висит» до самого таймаута

            def kill(self):
                pass

            def wait(self, timeout=None):
                pass

        run_file = self.dispatcher_dir / "x6.json"
        run_file.write_text("", encoding="utf-8")  # процесс убит — JSON не дописан
        D.RUNNING["X6"] = {"role": "engineer", "popen": FakeTimedOutPopen(), "pid": 424242, "started": started,
                            "attempt": 0, "run_file": run_file, "err_file": run_file, "out_fh": None,
                            "err_fh": None, "reason": "todo"}
        state = D.load_state()
        D._poll_running(state, started + timedelta(minutes=D.RUN_TIMEOUT // 60 + 1))
        D.save_state(state)
        self.assertEqual(D.RUNNING, {}, "прогресс есть — не должно быть повтора")
        self.assertEqual(T.read_ticket(path).status, "in_progress")  # роль сама решит дальше, не blocked

    def test_headerless_bullet_is_not_an_entry_owner_gets_one_retry_not_blocked(self):
        """TK-005 (27.09) → v2 (02.10): запись роли — только новый заголовок `### <ts> <роль>` (промпт велит
        писать командой tickets.py comment); безголовая строка записью не считается — владелец получает
        один повтор, но задача при этом НЕ блокируется."""
        path = T.create_ticket(self.tickets_dir, owner="engineer", title="TK-005",
                                now=dt("2026-09-27T23:00:00+04:00"))
        T.write_header_updates(path, {"status": "in_progress"}, now=dt("2026-09-27T23:00:00+04:00"))
        keys_at_launch = T.role_entry_keys(T.read_ticket(path), "engineer")
        started = dt("2026-09-27T23:00:00+04:00")
        content = path.read_text(encoding="utf-8")
        content += "\n- 27.09 ~23:50 (инженер, запуск 1) сделал шаг, дальше доделать\n"
        path.write_text(content, encoding="utf-8")

        run_file = self.dispatcher_dir / "tk005.json"
        run_file.write_text(json.dumps({"session_id": "s-tk005", "total_cost_usd": 0.2}), encoding="utf-8")
        info = {"role": "engineer", "popen": None, "pid": None, "started": started, "attempt": 0,
                "run_file": run_file, "err_file": run_file, "out_fh": None, "err_fh": None, "reason": "todo",
                "status_at_launch": "in_progress", "log_keys_at_launch": keys_at_launch}
        state = D.load_state()
        D._finish_run(path.stem, info, state, started + timedelta(minutes=5), timed_out=False)
        self.assertEqual(T.read_ticket(path).status, "in_progress", "не blocked: повтор, а не блокировка")
        self.assertIn(path.stem, D.RUNNING)
        self.assertEqual(D.RUNNING[path.stem]["reason"], "retry")

    def test_entry_with_heading_counts_even_if_its_timestamp_is_older_than_launch(self):
        """Роль пишет заголовок вручную с выдуманным временем (раньше старта запуска) — запись всё равно
        новая (ключи заголовков, а не часы и не смещения)."""
        path = T.create_ticket(self.tickets_dir, owner="engineer", title="TK-005b",
                                now=dt("2026-09-27T23:00:00+04:00"))
        T.write_header_updates(path, {"status": "in_progress"}, now=dt("2026-09-27T23:00:00+04:00"))
        keys_at_launch = T.role_entry_keys(T.read_ticket(path), "engineer")
        T.append_log(path, "engineer", "сделал шаг 1, status: in_progress", now=dt("2026-09-27T20:00:00+04:00"))
        run_file = self.dispatcher_dir / "tk005b.json"
        run_file.write_text(json.dumps({"session_id": "s-tk005b", "total_cost_usd": 0.2}), encoding="utf-8")
        info = {"role": "engineer", "popen": None, "pid": None, "started": dt("2026-09-27T23:50:00+04:00"),
                "attempt": 0, "run_file": run_file, "err_file": run_file, "out_fh": None, "err_fh": None,
                "reason": "in_progress-resume", "status_at_launch": "in_progress",
                "log_keys_at_launch": keys_at_launch}
        state = D.load_state()
        D._finish_run(path.stem, info, state, dt("2026-09-28T00:10:00+04:00"), timed_out=False)
        self.assertEqual(D.RUNNING, {}, "запись есть — повтора нет")
        self.assertEqual(T.read_ticket(path).status, "in_progress")

    def finish_info(self, role, keys=None, attempt=0, status_at_launch="todo", reason="todo"):
        run_file = self.dispatcher_dir / f"fin-{role}-{attempt}.json"
        run_file.write_text(json.dumps({"session_id": f"s-{role}", "total_cost_usd": 0.05}), encoding="utf-8")
        return {"role": role, "popen": None, "pid": None, "started": dt("2026-10-02T10:00:00+04:00"),
                "attempt": attempt, "run_file": run_file, "err_file": run_file, "out_fh": None, "err_fh": None,
                "reason": reason, "status_at_launch": status_at_launch,
                "log_keys_at_launch": [] if keys is None else keys}

    def test_entry_by_other_author_does_not_count_for_owner(self):
        """Лог вырос, но записал CEO/dispatcher, а не роль — это не «роль оставила запись» (раньше считался
        любой рост секции)."""
        path = T.create_ticket(self.tickets_dir, owner="engineer", title="Чужая запись",
                                now=dt("2026-10-02T10:00:00+04:00"))
        T.append_log(path, "ceo", "комментарий владельца", now=dt("2026-10-02T10:01:00+04:00"))
        T.append_log(path, "dispatcher", "служебное", now=dt("2026-10-02T10:02:00+04:00"))
        state = D.load_state()
        D._finish_run(path.stem, self.finish_info("engineer"), state, dt("2026-10-02T10:05:00+04:00"),
                      timed_out=False)
        self.assertIn(path.stem, D.RUNNING)
        self.assertEqual(D.RUNNING[path.stem]["reason"], "retry")

    def test_non_owner_run_without_entry_is_a_failure_like_owner(self):
        """A4 (аудит 03.10): запуск НЕ владельца (ревьюер, адресат --next) без новой записи (таймаут/падение) —
        провал как у владельца: один повтор, затем blocked + строка CEO (раньше — молча, тикет `in_review` висел
        вечно)."""
        path = T.create_ticket(self.tickets_dir, owner="researcher", title="Чужой запуск",
                                now=dt("2026-10-02T10:00:00+04:00"))
        T.write_header_updates(path, {"status": "waiting"}, now=dt("2026-10-02T10:00:30+04:00"))
        state = D.load_state()
        D._finish_run(path.stem, self.finish_info("judge", attempt=0, status_at_launch="waiting", reason="next"),
                      state, dt("2026-10-02T10:05:00+04:00"), timed_out=True)
        D.save_state(state)
        self.assertEqual(D.RUNNING[path.stem]["reason"], "retry")
        self.assertEqual(D.RUNNING[path.stem]["role"], "judge")
        self.drop_running(path.stem)
        state = D.load_state()
        D._finish_run(path.stem, self.finish_info("judge", attempt=1, status_at_launch="waiting", reason="retry"),
                      state, dt("2026-10-02T10:30:00+04:00"), timed_out=True)
        D.save_state(state)
        self.assertEqual(D.RUNNING, {})
        self.assertEqual(T.read_ticket(path).status, "blocked")
        self.assertIn("blocked", D.CEO_INBOX.read_text(encoding="utf-8"))

    def test_non_owner_run_with_entry_is_fine(self):
        path = T.create_ticket(self.tickets_dir, owner="researcher", title="Чужой с записью",
                                now=dt("2026-10-02T10:00:00+04:00"))
        T.write_header_updates(path, {"status": "waiting"}, now=dt("2026-10-02T10:00:30+04:00"))
        T.append_log(path, "judge", "вердикт", now=dt("2026-10-02T10:01:00+04:00"))
        state = D.load_state()
        D._finish_run(path.stem, self.finish_info("judge", status_at_launch="waiting", reason="next"), state,
                      dt("2026-10-02T10:05:00+04:00"), timed_out=False)
        self.assertEqual(D.RUNNING, {})
        self.assertEqual(T.read_ticket(path).status, "waiting")

    def test_reviewer_crash_on_in_review_ticket_retries_then_blocks(self):
        """A4 сквозной: done + reviewer → in_review, ревьюер падает без записи → повтор → blocked (не вечный in_review)."""
        self.set_fake_bin(FAKE_BIN_SILENT)
        path = T.create_ticket(self.tickets_dir, owner="researcher", title="На ревью", reviewer="judge",
                                now=dt("2026-10-02T10:00:00+04:00"))
        T.append_log(path, "researcher", "готово, прошу проверку", now=dt("2026-10-02T10:01:00+04:00"))
        T.write_header_updates(path, {"status": "done"}, now=dt("2026-10-02T10:02:00+04:00"))
        D.tick()
        self.assertEqual(D.RUNNING[path.stem]["role"], "judge")
        self.wait_running()
        tkt = T.read_ticket(path)
        self.assertEqual(tkt.status, "blocked")
        self.assertIn("judge", D.CEO_INBOX.read_text(encoding="utf-8"))

    def test_retry_runs_regardless_of_ticket_spend(self):
        """В-173: расход по задаче повтор не отменяет (раньше при остатке бюджета < $1 — одна строка CEO вместо
        повтора); повтор — один, как и прежде."""
        path = T.create_ticket(self.tickets_dir, owner="engineer", title="Много потрачено",
                                now=dt("2026-10-02T10:00:00+04:00"))
        state = D.load_state()
        D.add_ticket_cost(state, path.stem, 500.0)
        D._finish_run(path.stem, self.finish_info("engineer"), state, dt("2026-10-02T10:05:00+04:00"),
                      timed_out=False)
        self.assertIn(path.stem, D.RUNNING)
        self.assertEqual(D.RUNNING[path.stem]["attempt"], 1)
        self.assertEqual(D.RUNNING[path.stem]["reason"], "retry")
        self._assert_no_money_signals()

    def test_launch_never_passes_budget_flag_whatever_the_ticket_spend(self):
        """Денежных потолков нет: ни остаток копейки, ни огромный расход задачи не добавляют `--max-budget-usd`."""
        for spent in (0.0, 9.96, 500.0):
            self.set_fake_bin(FAKE_BIN_SILENT)
            path = T.create_ticket(self.tickets_dir, owner="engineer", title=f"Потрачено {spent}")
            state = D.load_state()
            D.add_ticket_cost(state, path.stem, spent)
            D.save_state(state)
            captured = []
            orig_popen = D._popen
            D._popen = lambda cmd, _o=orig_popen, **kw: (captured.extend(cmd), _o(cmd, **kw))[1]
            try:
                D.tick()
            finally:
                D._popen = orig_popen
            self.assertNotIn("--max-budget-usd", captured, spent)
            self.wait_running()
            T.write_header_updates(path, {"status": "done"})  # закрыть, чтобы не мешал следующему кругу
            D.RUNNING.clear()

    def test_effort_defaults_by_role_and_ticket_header_overrides(self):
        """v2: умолчания — исследователь/инженер high, Судья xhigh; `effort:` в шапке тикета — приоритетнее."""
        if P.env("DISPATCH_EFFORT"):
            self.skipTest("RPV_DISPATCH_EFFORT задан в окружении")
        self.assertEqual(D.ROLE_EFFORT, {"judge": "xhigh", "engineer": "high", "researcher": "high"})
        cases = [("researcher", None, "high"), ("engineer", None, "high"), ("judge", None, "xhigh"),
                 ("engineer", "low", "low"), ("researcher", "medium", "medium"), ("judge", "high", "high"),
                 ("engineer", "xhigh", "xhigh")]
        for role, effort, expected in cases:
            self.set_fake_bin(FAKE_BIN_SILENT)
            path = T.create_ticket(self.tickets_dir, owner=role, title=f"Усилие {role} {effort}", effort=effort)
            captured = []
            orig_popen = D._popen
            D._popen = lambda cmd, _o=orig_popen, **kw: (captured.extend(cmd), _o(cmd, **kw))[1]
            try:
                D.launch_run(path, role, D.load_state(), datetime.now().astimezone(), reason="todo")
            finally:
                D._popen = orig_popen
            self.assertEqual(captured[captured.index("--effort") + 1], expected, f"{role}/{effort}")
            for info in list(D.RUNNING.values()):
                info["popen"].wait(timeout=10)
                for fh in (info.get("out_fh"), info.get("err_fh")):
                    if fh:
                        fh.close()
            D.RUNNING.clear()
            path.unlink()

    def test_next_launches_role_once_and_clears_field(self):
        self.set_fake_bin(FAKE_BIN_RECORD)
        path = T.create_ticket(self.tickets_dir, owner="researcher", title="Передача")
        T.write_header_updates(path, {"status": "waiting", "next": "judge"})
        D.tick()
        self.assertIn(path.stem, D.RUNNING)
        self.assertEqual(D.RUNNING[path.stem]["role"], "judge")
        self.assertEqual(D.RUNNING[path.stem]["reason"], "next")
        self.assertEqual(T.read_ticket(path).next_role, "", "поле очищено при запуске")
        self.wait_running()
        D.tick()
        D.tick()
        calls = (self.tickets_dir.parent / "calls.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(calls), 1, "next сработал один раз")
        self.assertIn("Запуск по явной передаче", json.loads(calls[0])["prompt"])

    def test_next_waits_while_target_role_is_busy_and_keeps_field(self):
        self.set_fake_bin(FAKE_BIN_SLOW_OK)
        a = T.create_ticket(self.tickets_dir, owner="researcher", title="Занимает исследователя")
        D.MAX_PARALLEL = 3
        D.tick()
        self.assertEqual({t: i["role"] for t, i in D.RUNNING.items()}, {a.stem: "researcher"})
        b = T.create_ticket(self.tickets_dir, owner="engineer", title="Просит исследователя")
        T.write_header_updates(b, {"status": "waiting", "next": "researcher"})
        D.tick()
        self.assertEqual(list(D.RUNNING), [a.stem], "исследователь занят — запуск ждёт")
        self.assertEqual(T.read_ticket(b).next_role, "researcher", "ждёт следующего тика, не потеряно")

    def test_next_has_priority_over_in_progress_resume(self):
        self.set_fake_bin(FAKE_BIN_SLOW_OK)
        a = T.create_ticket(self.tickets_dir, owner="researcher", title="Продолжение")
        T.write_header_updates(a, {"status": "in_progress"})
        b = T.create_ticket(self.tickets_dir, owner="engineer", title="Передача")
        T.write_header_updates(b, {"status": "waiting", "next": "researcher"})
        D.tick()
        self.assertEqual({t: i["role"] for t, i in D.RUNNING.items()}, {b.stem: "researcher"})

    def test_longest_waiting_ticket_goes_first_within_role(self):
        """Один запуск на роль + порядок по имени вытеснял бы поздние тикеты; при равном приоритете первым
        идёт тот, кого роль дольше не запускала."""
        self.set_fake_bin(FAKE_BIN_SLOW_OK)
        a = T.create_ticket(self.tickets_dir, owner="researcher", title="Недавно запускали")
        b = T.create_ticket(self.tickets_dir, owner="researcher", title="Давно не запускали")
        for p in (a, b):
            T.write_header_updates(p, {"status": "in_progress"})
        state = D.load_state()
        now = datetime.now().astimezone()
        state["sessions"] = {f"{a.stem}::researcher": {"last_woken": T.now_iso(now - timedelta(minutes=10))},
                             f"{b.stem}::researcher": {"last_woken": T.now_iso(now - timedelta(hours=3))}}
        D.save_state(state)
        D.tick()
        self.assertEqual(list(D.RUNNING), [b.stem])

    def test_next_ceo_writes_one_inbox_line_and_clears_next(self):
        path = T.create_ticket(self.tickets_dir, owner="researcher", title="Нужен CEO",
                                now=dt("2026-09-27T12:00:00+04:00"))
        T.write_header_updates(path, {"status": "waiting", "next": "ceo"}, now=dt("2026-09-27T12:00:30+04:00"))
        T.append_log(path, "researcher", "Нужно решение владельца по бюджету.", now=dt("2026-09-27T12:01:00+04:00"))
        D.tick(now=dt("2026-09-27T12:02:00+04:00"))
        inbox = D.CEO_INBOX.read_text(encoding="utf-8")
        self.assertEqual(inbox.count("[next-ceo]"), 1)
        self.assertIn("решение владельца по бюджету", inbox)
        self.assertEqual(T.read_ticket(path).next_role, "")
        self.assertEqual(T.read_ticket(path).header["updated"], "2026-09-27T12:00:30+04:00",
                         "очистка next поле updated не двигает")
        D.tick(now=dt("2026-09-27T12:02:20+04:00"))
        self.assertEqual(D.CEO_INBOX.read_text(encoding="utf-8").count("[next-ceo]"), 1)
        self.assertEqual(D.RUNNING, {})

    def test_done_history_is_not_resignalled_on_first_tick(self):
        """Первый тик после перехода на v2: закрытые заранее тикеты — известные, CEO по ним строк не получает."""
        for i in range(3):
            p = T.create_ticket(self.tickets_dir, owner="researcher", title=f"Старая {i}",
                                 now=dt("2026-09-27T12:00:00+04:00"))
            T.write_header_updates(p, {"status": "done"}, now=dt("2026-09-27T12:01:00+04:00"))
        D.tick(now=dt("2026-09-27T12:02:00+04:00"))
        self.assertFalse(D.CEO_INBOX.exists() and "[done]" in D.CEO_INBOX.read_text(encoding="utf-8"))

    def test_done_with_pending_review_is_signalled_only_after_verdict(self):
        self.set_fake_bin(FAKE_BIN_SILENT)
        path = T.create_ticket(self.tickets_dir, owner="researcher", title="С ревью", reviewer="judge",
                                now=dt("2026-10-02T10:00:00+04:00"))
        T.write_header_updates(path, {"status": "waiting"}, now=dt("2026-10-02T10:00:10+04:00"))
        D.tick(now=dt("2026-10-02T10:00:20+04:00"))  # база
        T.append_log(path, "researcher", "готово, прошу вердикт", now=dt("2026-10-02T10:01:00+04:00"))
        T.write_header_updates(path, {"status": "done"}, now=dt("2026-10-02T10:01:00+04:00"))
        D.tick(now=dt("2026-10-02T10:02:00+04:00"))  # правило (г): ревьюер запущен, статус in_review
        self.assertEqual(T.read_ticket(path).status, "in_review")
        self.assertEqual(D.RUNNING[path.stem]["role"], "judge")
        self.assertFalse(D.CEO_INBOX.exists() and "[done]" in D.CEO_INBOX.read_text(encoding="utf-8"))
        T.append_log(path, "judge", "принято", now=dt("2026-10-02T10:10:00+04:00"))
        T.write_header_updates(path, {"status": "done"}, now=dt("2026-10-02T10:10:00+04:00"))
        D.RUNNING[path.stem]["popen"].kill()
        D.RUNNING[path.stem]["popen"].wait(timeout=5)
        for fh in (D.RUNNING[path.stem].get("out_fh"), D.RUNNING[path.stem].get("err_fh")):
            fh.close()
        D.RUNNING.clear()
        D.tick(now=dt("2026-10-02T10:11:00+04:00"))
        self.assertEqual(D.CEO_INBOX.read_text(encoding="utf-8").count("[done]"), 1)

    def test_done_line_has_no_budget_note_whatever_the_spend(self):
        """В-173: строка `done` без «потрачено N % бюджета» — бюджетов нет; строка одна, `budget-check` нет."""
        path = T.create_ticket(self.tickets_dir, owner="researcher", title="Дорогая",
                                now=dt("2026-10-02T10:00:00+04:00"))
        T.write_header_updates(path, {"status": "waiting"}, now=dt("2026-10-02T10:00:10+04:00"))
        D.tick(now=dt("2026-10-02T10:00:20+04:00"))
        state = D.load_state()
        D.add_ticket_cost(state, path.stem, 500.0)
        D.save_state(state)
        T.write_header_updates(path, {"status": "done"}, now=dt("2026-10-02T10:01:00+04:00"))
        D.tick(now=dt("2026-10-02T10:02:00+04:00"))
        inbox = D.CEO_INBOX.read_text(encoding="utf-8")
        self.assertEqual(inbox.count("[done]"), 1)
        self.assertNotIn("бюджет", inbox)
        self.assertNotIn("соразмерность", inbox)
        self.assertNotIn("budget-check", inbox)

    def test_prompt_has_next_rules_and_no_mention_encouragement(self):
        prompt = D.build_prompt("researcher", "TK-005")
        self.assertIn(" result TK-005 <done|pr|accept|return|blocked|ask-owner|wait|continue> --why", prompt)
        self.assertIn("--next и шапку руками не правь", prompt)
        self.assertIn("только промежуточная заметка", prompt)
        self.assertIn("@упоминания в тексте никого не будят", prompt)
        self.assertNotIn("Упоминай @", prompt)
        self.assertIn("blocked (задача встала)", prompt)
        self.assertIn(".claude/tickets/archive/TK-005-log.md", prompt)
        self.assertIn("последние записи", prompt)
        self.assertIn("фоновых помощников", prompt)
        self.assertIn("systemd-run", prompt)
        self.assertIn("status: waiting + wait_for", prompt)

    def test_v2_constants(self):
        for var, expected in (("DISPATCH_MAX_PARALLEL", 3), ("DISPATCH_TIMEOUT", 1200.0),
                              ("DISPATCH_ROTATE_TOKENS", 120000)):
            if P.env(var):
                self.skipTest(f"{var} задан в окружении")
        self.assertEqual((D.MAX_PARALLEL, D.RUN_TIMEOUT, D.ROTATE_TOKENS), (3, 1200.0, 120000))
        self.assertEqual(set(D.SESSION_SCOPE.values()), {"ticket"})

    def test_no_money_limit_machinery_left(self):
        """В-173 (на тикет), В-149 (в час/в сутки), 03.10 («бюджет до конца убирай»): денежных ограничений в
        диспетчере нет вовсе — ни потолка запуска, ни `--max-budget-usd`, ни доли потолка для холостого хода.
        Остался только учёт (ticket_cost_spent/add_ticket_cost/_add_cost/_record_cost_event)."""
        for name in ("DAILY_COST_USD", "HOUR_COST_USD", "MIN_RUN_CAP_USD", "MIN_RETRY_BUDGET_USD",
                     "BUDGET_PRESETS", "DEFAULT_TICKET_BUDGET_USD", "parse_budget_arg", "set_ticket_budget",
                     "ticket_budget_usd", "ticket_budget_exceeded", "notify_ticket_budget_exceeded",
                     "notify_retry_skipped_low_budget", "run_cap_for", "_daily_budget_exceeded",
                     "_hour_budget_exceeded", "_notify_budget_once", "_notify_hour_budget",
                     "RUN_CAP_USD", "IDLE_RUN_CAP_FRACTION"):
            self.assertFalse(hasattr(D, name), name)
        for name in ("ticket_cost_spent", "add_ticket_cost", "_add_cost", "_record_cost_event"):
            self.assertTrue(hasattr(D, name), name)
        source = (Path(D.__file__)).read_text(encoding="utf-8")
        for needle in ("RUN_CAP", "max-budget", "run_cap_usd", "ALPHA_DISPATCH_RUN_CAP_USD"):
            self.assertNotIn(needle, source, needle)

    def test_env_run_cap_variable_is_ignored(self):
        """Переменная ALPHA_DISPATCH_RUN_CAP_USD в окружении процесса (осталась от прежнего запуска) ни на что не влияет."""
        self.set_fake_bin(FAKE_BIN_SILENT)
        os.environ["ALPHA_DISPATCH_RUN_CAP_USD"] = "0.01"
        self.addCleanup(lambda: os.environ.pop("ALPHA_DISPATCH_RUN_CAP_USD", None))
        captured = []
        orig_popen = D._popen
        D._popen = lambda cmd, _o=orig_popen, **kw: (captured.extend(cmd), _o(cmd, **kw))[1]
        try:
            T.create_ticket(self.tickets_dir, owner="engineer", title="Переменная окружения")
            D.tick()
        finally:
            D._popen = orig_popen
        self.assertNotIn("--max-budget-usd", captured)
        self.assertNotIn("0.01", captured)

    def test_roles_and_dispatcher_docs_mention_no_money_ceiling(self):
        """README диспетчера и устав ролей («Расход»): без потолков и денег, кроме «траты считаются»."""
        docs = [Path(D.__file__).parent / "README.md", Path(D.__file__).parent.parent.parent / "templates" / "roles" / "README.md"]
        for doc in docs:
            if not doc.exists():
                continue
            text = doc.read_text(encoding="utf-8")
            for needle in ("RUN_CAP", "max-budget", "IDLE_RUN_CAP", "потолок на один запуск", "Потолок одного запуска"):
                self.assertNotIn(needle, text, f"{doc.name}: {needle}")

    def test_prompt_tells_role_to_use_tickets_comment(self):
        prompt = D.build_prompt("engineer", "TK-005")
        self.assertIn("comment TK-005 --author engineer", prompt)
        self.assertNotIn("alpha", D.PROMPT_TEMPLATE.lower())  # плагин общий: слова проекта в шаблоне нет
        self.assertIn((D.CODE_DIR / "tickets.py").as_posix(), prompt)  # запуск прямо из папки плагина

    # --- A5: тормоз цикла «запись есть, статус не меняется»
    def same_status_step(self, path, n, status_at_launch, reason="in_progress-resume"):
        """Один завершённый запуск владельца: запись оставлена, статус остался прежним."""
        tid = path.stem
        keys = T.role_entry_keys(T.read_ticket(path), "engineer")
        T.append_log(path, "engineer", f"шаг {n}", now=dt("2026-10-03T10:00:00+04:00") + timedelta(minutes=n))
        run_file = self.dispatcher_dir / f"same-{tid}-{n}.json"
        run_file.write_text(json.dumps({"session_id": "s-same", "total_cost_usd": 0.01}), encoding="utf-8")
        info = {"role": "engineer", "popen": None, "pid": None, "started": dt("2026-10-03T10:00:00+04:00"),
                "attempt": 0, "run_file": run_file, "err_file": run_file, "out_fh": None, "err_fh": None,
                "reason": reason, "status_at_launch": status_at_launch, "log_keys_at_launch": keys}
        state = D.load_state()
        D._finish_run(tid, info, state, dt("2026-10-03T10:00:00+04:00") + timedelta(minutes=n, seconds=30),
                      timed_out=False)
        D.save_state(state)

    def make_in_progress(self, title="Цикл"):
        path = T.create_ticket(self.tickets_dir, owner="engineer", title=title, now=dt("2026-10-03T09:00:00+04:00"))
        T.write_header_updates(path, {"status": "in_progress"}, now=dt("2026-10-03T09:00:00+04:00"))
        return path

    def limits(self, max_runs):
        """Порог тормоза цикла на время теста."""
        old = D.MAX_SAME_STATUS_RUNS
        D.MAX_SAME_STATUS_RUNS = max_runs
        self.addCleanup(lambda: setattr(D, "MAX_SAME_STATUS_RUNS", old))

    def test_default_same_status_limit_is_twelve_and_env_name(self):
        if P.env("DISPATCH_MAX_SAME_STATUS_RUNS"):
            self.skipTest("порог тормоза цикла задан в окружении")
        self.assertEqual(D.MAX_SAME_STATUS_RUNS, 12)
        source = Path(D.__file__).read_text(encoding="utf-8")
        self.assertIn("DISPATCH_MAX_SAME_STATUS_RUNS", source)
        self.assertNotIn("SAME_STATUS_WARN", source, "предупреждение на полпути снято (TK-100 №27)")

    def test_silent_until_max_then_block_with_exactly_one_line(self):
        self.limits(6)
        path = self.make_in_progress("Без предупреждения")
        tid = path.stem

        def lines():
            if not D.CEO_INBOX.exists():
                return []
            return [ln for ln in D.CEO_INBOX.read_text(encoding="utf-8").splitlines() if tid in ln]
        for n in range(1, 6):
            self.same_status_step(path, n, "in_progress")
        self.assertEqual(lines(), [], "до порога тихо — предупреждения нет")
        self.same_status_step(path, 6, "in_progress")
        got = lines()
        self.assertEqual(T.read_ticket(path).status, "blocked")
        self.assertEqual(len(got), 1, got)
        self.assertIn("[blocked]", got[0])
        D.tick()
        D.tick()
        self.assertEqual(len(lines()), 1, "после блока строк больше нет")

    def test_status_change_restarts_the_series(self):
        self.limits(3)
        path = self.make_in_progress("Серия с нуля")
        for n in (1, 2):
            self.same_status_step(path, n, "in_progress")
        T.write_header_updates(path, {"status": "waiting", "wait_for": "file:/no/such"},
                                now=dt("2026-10-03T10:05:00+04:00"))
        self.same_status_step(path, 3, "in_progress")            # запуск сменил статус — серия с нуля
        T.write_header_updates(path, {"status": "in_progress", "wait_for": ""}, now=dt("2026-10-03T10:06:00+04:00"))
        for n in (4, 5):
            self.same_status_step(path, n, "in_progress")
        self.assertEqual(T.read_ticket(path).status, "in_progress", "после смены статуса счёт начат заново")

    def test_n_runs_with_entry_but_same_status_block_the_ticket_with_one_ceo_line(self):
        self.limits(3)
        path = self.make_in_progress()
        for n in (1, 2):
            self.same_status_step(path, n, "in_progress")
            self.assertEqual(T.read_ticket(path).status, "in_progress", n)
        self.same_status_step(path, 3, "in_progress")
        tkt = T.read_ticket(path)
        self.assertEqual(tkt.status, "blocked")
        self.assertIn("3", tkt.log[-1].text)
        self.assertEqual(tkt.log[-1].author, "dispatcher")
        D.tick()                                  # следующий тик: тот же blocked второй строкой не повторяется
        D.tick()
        lines = [ln for ln in D.CEO_INBOX.read_text(encoding="utf-8").splitlines() if path.stem in ln]
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("[blocked]", lines[0])
        self.assertEqual(D.RUNNING, {})

    def test_status_change_resets_same_status_counter(self):
        self.limits(3)
        path = self.make_in_progress()
        self.same_status_step(path, 1, "in_progress")
        self.same_status_step(path, 2, "in_progress")
        # запуск сменил статус (in_progress → waiting без выполненного wait_for) — счёт с нуля
        T.write_header_updates(path, {"status": "waiting", "wait_for": "file:/no/such"},
                                now=dt("2026-10-03T10:05:00+04:00"))
        self.same_status_step(path, 3, "in_progress")
        T.write_header_updates(path, {"status": "in_progress", "wait_for": ""}, now=dt("2026-10-03T10:06:00+04:00"))
        self.same_status_step(path, 4, "in_progress")
        self.same_status_step(path, 5, "in_progress")
        self.assertEqual(T.read_ticket(path).status, "in_progress")

    def test_waiting_with_met_wait_for_counts_unmet_does_not(self):
        self.limits(3)
        flag = self.base / "ready.flag"
        flag.write_text("x", encoding="utf-8")
        met = T.create_ticket(self.tickets_dir, owner="engineer", title="Условие выполнено")
        T.write_header_updates(met, {"status": "waiting", "wait_for": f"file:{flag}"})
        unmet = T.create_ticket(self.tickets_dir, owner="engineer", title="Условие не выполнено")
        T.write_header_updates(unmet, {"status": "waiting", "wait_for": "file:/no/such/flag"})
        for n in (1, 2, 3):
            self.same_status_step(met, n, "waiting", reason="wait_for-met")
        for n in (1, 2, 3, 4, 5):
            self.same_status_step(unmet, n, "waiting", reason="next")
        self.assertEqual(T.read_ticket(met).status, "blocked")
        self.assertEqual(T.read_ticket(unmet).status, "waiting")

    # --- аудит-2 п.2: пинг-понг ревью
    def review_setup(self, title="Ревью"):
        path = T.create_ticket(self.tickets_dir, owner="engineer", title=title, reviewer="judge",
                               now=dt("2026-10-03T09:00:00+04:00"))
        T.write_header_updates(path, {"status": "in_review"}, now=dt("2026-10-03T09:00:00+04:00"))
        T.append_log(path, "engineer", "готово", now=dt("2026-10-03T09:05:00+04:00"))
        return path

    def review_round(self, path, n, new_status, next_role=None):
        """Один завершённый запуск ревьюера (judge) на статусе in_review: запись + новый статус [+ next]."""
        tid = path.stem
        keys = T.role_entry_keys(T.read_ticket(path), "judge")
        base = dt("2026-10-03T10:00:00+04:00") + timedelta(minutes=10 * n)
        T.append_log(path, "judge", f"круг {n}", now=base)
        updates = {"status": new_status}
        if next_role:
            updates["next"] = next_role
        T.write_header_updates(path, updates, now=base)
        run_file = self.dispatcher_dir / f"review-{tid}-{n}.json"
        run_file.write_text(json.dumps({"session_id": "s-rev", "total_cost_usd": 0.01}), encoding="utf-8")
        info = {"role": "judge", "popen": None, "pid": None, "started": base, "attempt": 0, "run_file": run_file,
                "err_file": run_file, "out_fh": None, "err_fh": None, "reason": "review",
                "status_at_launch": "in_review", "log_keys_at_launch": keys}
        state = D.load_state()
        D._finish_run(tid, info, state, base + timedelta(seconds=30), timed_out=False)
        D.save_state(state)

    def owner_resubmits(self, path, n, status="done"):
        T.append_log(path, "engineer", f"доработал {n}", now=dt("2026-10-03T12:00:00+04:00") + timedelta(minutes=n))
        T.write_header_updates(path, {"status": status, "next": ""},     # диспетчер очищает next при запуске владельца
                               now=dt("2026-10-03T12:00:00+04:00") + timedelta(minutes=n))

    def ceo_lines(self, tid, kind=None):
        if not D.CEO_INBOX.exists():
            return []
        return [ln for ln in D.CEO_INBOX.read_text(encoding="utf-8").splitlines()
                if tid in ln and (kind is None or f"[{kind}]" in ln)]

    def recording_popen(self):
        calls = []
        D._popen = lambda cmd, **kw: (calls.append(list(cmd)), subprocess.Popen([sys.executable, "-c", "pass"], **kw))[1]
        return calls

    def test_default_review_returns_limit_is_three_with_env_name(self):
        if P.env("DISPATCH_MAX_REVIEW_RETURNS"):
            self.skipTest("порог задан в окружении")
        self.assertEqual(D.MAX_REVIEW_RETURNS, 3)
        self.assertIn("DISPATCH_MAX_REVIEW_RETURNS", Path(D.__file__).read_text(encoding="utf-8"))

    def test_reviewer_return_counts_and_approval_resets_the_series(self):
        path = self.review_setup()
        tid = path.stem
        self.review_round(path, 1, "in_progress")                       # вернул владельцу
        self.assertEqual(D.load_state()["review_returns"][tid], 1)
        self.owner_resubmits(path, 1, "in_review")
        self.review_round(path, 2, "in_review", next_role="engineer")   # вернул через --next владельцу
        self.assertEqual(D.load_state()["review_returns"][tid], 2)
        self.owner_resubmits(path, 2, "in_review")
        self.review_round(path, 3, "done")                              # принял — серия кончилась
        self.assertNotIn(tid, D.load_state().get("review_returns", {}))

    def test_blocked_or_next_ceo_by_reviewer_is_not_a_return(self):
        path = self.review_setup()
        self.review_round(path, 1, "in_progress")
        self.owner_resubmits(path, 1, "in_review")
        self.review_round(path, 2, "in_review", next_role="ceo")
        self.assertNotIn(path.stem, D.load_state().get("review_returns", {}))

    def test_three_returns_then_resubmit_goes_to_owner_and_reviewer_is_not_woken(self):
        path = self.review_setup()
        tid = path.stem
        for n in (1, 2, 3):
            self.review_round(path, n, "in_progress")
            self.owner_resubmits(path, n, "in_review" if n < 3 else "done")
        self.assertEqual(D.load_state()["review_returns"][tid], 3)
        calls = self.recording_popen()
        D.tick()
        tkt = T.read_ticket(path)
        self.assertEqual(calls, [], "ревьюера на четвёртый круг не будим")
        self.assertEqual(tkt.status, "needs_owner")  # TK-094: владельцу («ждёт вас»), не next: ceo
        self.assertEqual(tkt.log[-1].author, "dispatcher")
        self.assertIn("3", tkt.log[-1].text)
        self.assertEqual(tkt.next_role, "")
        self.assertEqual(len(self.ceo_lines(tid)), 1, self.ceo_lines(tid))
        self.assertIn("[needs_owner]", self.ceo_lines(tid)[0])
        D.tick()
        D.tick()
        self.assertEqual(len(self.ceo_lines(tid)), 1, "повторов нет")
        self.assertEqual(calls, [])
        self.assertEqual(D.RUNNING, {})

    def test_below_the_limit_reviewer_is_still_woken(self):
        path = self.review_setup()
        for n in (1, 2):
            self.review_round(path, n, "in_progress")
            self.owner_resubmits(path, n, "done")
        calls = self.recording_popen()
        D.tick()
        self.assertEqual(len(calls), 1, "после двух возвратов ревьюер ещё получает третий круг")
        self.assertEqual(T.read_ticket(path).status, "in_review")
        self.assertEqual(self.ceo_lines(path.stem), [])

    def test_ceo_next_wakes_reviewer_even_over_the_limit_and_counter_keeps_going(self):
        path = self.review_setup()
        for n in (1, 2, 3):
            self.review_round(path, n, "in_progress")
            self.owner_resubmits(path, n, "in_review")
        tkt = T.read_ticket(path)
        T.append_log(path, "ceo", "ещё один круг", now=dt("2026-10-03T15:00:00+04:00"))
        T.write_header_updates(path, {"next": "judge"}, now=dt("2026-10-03T15:00:00+04:00"))
        calls = self.recording_popen()
        D.tick()
        self.assertEqual(len(calls), 1, "явный next — раньше предела")
        self.assertEqual(self.ceo_lines(path.stem), [])

    def test_limit_is_judged_only_after_owner_resubmits_not_on_ceo_or_reviewer_entries(self):
        path = self.review_setup()
        for n in (1, 2, 3):
            self.review_round(path, n, "in_progress")
            self.owner_resubmits(path, n, "in_review")
        T.append_log(path, "ceo", "пока думаю", now=dt("2026-10-03T15:00:00+04:00"))     # последняя запись — CEO
        calls = self.recording_popen()
        D.tick()
        D.tick()
        self.assertEqual(calls, [])
        self.assertEqual(self.ceo_lines(path.stem), [], "запись CEO — не повод для строки CEO и не будит ревьюера")
        self.assertEqual(T.read_ticket(path).log[-1].author, "ceo")

    def test_no_escalation_while_owner_run_is_still_going(self):
        """Аудит-3 п.4: после 3 возвратов промежуточная запись владельца ПОСРЕДИ его запуска — не «сдал на ревью»:
        эскалация ждёт конца запуска (тикет в RUNNING), потом срабатывает один раз."""
        path = self.review_setup()
        tid = path.stem
        for n in (1, 2, 3):
            self.review_round(path, n, "in_progress")
            self.owner_resubmits(path, n, "in_review")
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])   # запуск владельца ещё идёт
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        D.RUNNING[tid] = {"role": "engineer", "popen": proc, "pid": proc.pid, "started": dt("2026-10-03T15:00:00+04:00")}
        self.addCleanup(D.RUNNING.clear)
        T.append_log(path, "engineer", "промежуточно: правлю п.2", now=dt("2026-10-03T15:01:00+04:00"))
        D.tick(now=dt("2026-10-03T15:02:00+04:00"))
        tkt = T.read_ticket(path)
        self.assertEqual(tkt.log[-1].author, "engineer", "пока запуск идёт, записи dispatcher нет")
        self.assertEqual(self.ceo_lines(tid), [])
        proc.kill()
        proc.wait()
        D.RUNNING.clear()                                               # конец запуска (сам разбор итога — в других тестах)
        self.recording_popen()
        D.tick(now=dt("2026-10-03T15:20:00+04:00"))                     # запуск кончился — эскалация, одна строка
        self.assertEqual(T.read_ticket(path).log[-1].author, "dispatcher")
        self.assertEqual(len(self.ceo_lines(tid, "needs_owner")), 1, self.ceo_lines(tid))

    def test_ceo_closing_done_at_the_limit_still_gets_the_done_line(self):
        path = self.review_setup()
        for n in (1, 2, 3):
            self.review_round(path, n, "in_progress")
            self.owner_resubmits(path, n, "in_review")
        self.recording_popen()
        D.tick()                                       # эскалация + первый тик (метка «историю done не пересказываем»)
        self.assertEqual(len(self.ceo_lines(path.stem, "needs_owner")), 1)
        T.append_log(path, "ceo", "принимаю как есть", now=dt("2026-10-03T15:00:00+04:00"))
        T.write_header_updates(path, {"status": "done"}, now=dt("2026-10-03T15:00:00+04:00"))
        D.tick()
        self.assertEqual(len(self.ceo_lines(path.stem, "done")), 1, self.ceo_lines(path.stem))
        D.tick()
        self.assertEqual(len(self.ceo_lines(path.stem, "done")), 1, "дедуп")

    # --- A6: ротация контекста
    def test_timeout_without_json_keeps_last_known_context_and_rotation_still_fires(self):
        path = self.make_in_progress("Таймаут")
        tid = path.stem
        state = D.load_state()
        store = D._resume_store(state, tid, "engineer")
        store.update({"session_id": "sess-big", "last_context_tokens": D.ROTATE_TOKENS + 30_000})
        D.save_state(state)
        run_file = self.dispatcher_dir / "timeout.json"
        run_file.write_text("", encoding="utf-8")           # убит по таймауту — JSON не дописан
        info = {"role": "engineer", "popen": None, "pid": None, "started": dt("2026-10-03T10:00:00+04:00"),
                "attempt": 0, "run_file": run_file, "err_file": run_file, "out_fh": None, "err_fh": None,
                "reason": "in_progress-resume", "status_at_launch": "in_progress", "log_keys_at_launch": []}
        captured = []
        orig_popen = D._popen
        D._popen = lambda cmd, _o=orig_popen, **kw: (captured.extend(cmd), _o(cmd, **kw))[1]
        try:
            D._finish_run(tid, info, state, dt("2026-10-03T10:20:00+04:00"), timed_out=True)
        finally:
            D._popen = orig_popen
        saved = D.load_state()["ticket_sessions"][f"{tid}::engineer"]
        self.assertEqual(saved["last_context_tokens"], D.ROTATE_TOKENS + 30_000, "таймаут не обнуляет счётчик")
        self.assertEqual(saved["session_id"], "sess-big")
        self.assertTrue(captured, "повтор запущен")
        self.assertNotIn("--resume", captured, "контекст за порогом — повтор идёт в новой сессии")

    # --- аудит-2 п.4: ротация после таймаута — последний ход из транскрипта убитой сессии
    def write_session_transcript(self, root, session_id, turns):
        proj = Path(root) / "any-project-slug"
        proj.mkdir(parents=True, exist_ok=True)
        lines = [json.dumps({"type": "assistant", "message": {"role": "assistant", "usage": u}}) for u in turns]
        lines.append(json.dumps({"type": "user", "message": {"role": "user", "content": "x"}}))
        (proj / f"{session_id}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def killed_run(self, previous_tokens, turns, sid="sess-killed", stored_sid="sess-killed", result_text="", info_sid=None):
        """Запуск убит по таймауту (JSON пуст): возвращает (сохранённый контекст, команда повторного запуска)."""
        path = self.make_in_progress("Убит")
        tid = path.stem
        state = D.load_state()
        store = D._resume_store(state, tid, "engineer")
        store.update({"last_context_tokens": previous_tokens})
        if stored_sid:
            store["session_id"] = stored_sid
        D.save_state(state)
        run_file = self.dispatcher_dir / "killed.json"
        run_file.write_text(result_text, encoding="utf-8")
        info = {"role": "engineer", "popen": None, "pid": None, "started": dt("2026-10-03T10:00:00+04:00"),
                "attempt": 0, "run_file": run_file, "err_file": run_file, "out_fh": None, "err_fh": None,
                "reason": "in_progress-resume", "status_at_launch": "in_progress", "log_keys_at_launch": [],
                "session_id": info_sid}
        captured = []
        orig_popen, orig_dir = D._popen, D.CLAUDE_PROJECTS_DIR
        D._popen = lambda cmd, _o=orig_popen, **kw: (captured.extend(cmd), _o(cmd, **kw))[1]
        with tempfile.TemporaryDirectory() as root:
            if turns is not None:
                self.write_session_transcript(root, sid, turns)
            D.CLAUDE_PROJECTS_DIR = Path(root)
            try:
                D._finish_run(tid, info, state, dt("2026-10-03T10:20:00+04:00"), timed_out=True)
            finally:
                D._popen, D.CLAUDE_PROJECTS_DIR = orig_popen, orig_dir
        saved = D.load_state()["ticket_sessions"][f"{tid}::engineer"]["last_context_tokens"]
        return saved, captured

    def test_killed_run_takes_last_turn_from_its_transcript_not_the_old_number(self):
        small = [{"input_tokens": 5, "cache_read_input_tokens": 100_000, "cache_creation_input_tokens": 0},
                 {"input_tokens": 7, "cache_read_input_tokens": 20_000, "cache_creation_input_tokens": 3_000}]
        saved, captured = self.killed_run(150_000, small)
        self.assertEqual(saved, 7 + 20_000 + 3_000, "последний ход из транскрипта, не прежние 150000")
        self.assertIn("--resume", captured, "контекст под порогом — повтор продолжает ту же сессию")
        self.assertIn("sess-killed", captured)

    def test_killed_fresh_session_is_resumed_by_preassigned_id(self):
        """TK-056 п.3′: новая сессия убита по таймауту до JSON — id назначен при запуске, повтор идёт --resume ею."""
        turns = [{"input_tokens": 5, "cache_read_input_tokens": 1_000, "cache_creation_input_tokens": 0}]
        _, captured = self.killed_run(0, turns, sid="sess-new", stored_sid=None, info_sid="sess-new")
        self.assertIn("--resume", captured)
        self.assertIn("sess-new", captured)
        _, captured = self.killed_run(0, None, sid="sess-new", stored_sid=None, info_sid="sess-new")
        self.assertNotIn("--resume", captured, "транскрипта нет — резюмировать нечего, новая сессия")

    def test_killed_run_with_big_transcript_makes_rotation_fire_even_if_old_number_was_small(self):
        big = [{"input_tokens": 5, "cache_read_input_tokens": D.ROTATE_TOKENS + 10_000, "cache_creation_input_tokens": 0}]
        saved, captured = self.killed_run(30_000, big)
        self.assertEqual(saved, 5 + D.ROTATE_TOKENS + 10_000)
        self.assertTrue(captured, "повтор запущен")
        self.assertNotIn("--resume", captured, "по транскрипту контекст за порогом — новая сессия")

    def test_killed_run_without_transcript_or_session_id_keeps_the_old_number(self):
        saved, _ = self.killed_run(150_000, None)                              # транскрипта нет
        self.assertEqual(saved, 150_000)
        turns = [{"input_tokens": 1, "cache_read_input_tokens": 500, "cache_creation_input_tokens": 0}]
        saved, _ = self.killed_run(150_000, turns, sid="sess-other", stored_sid=None)   # id сессии неизвестен
        self.assertEqual(saved, 150_000)

    def test_error_json_with_zero_usage_also_uses_the_session_transcript(self):
        turns = [{"input_tokens": 2, "cache_read_input_tokens": 41_000, "cache_creation_input_tokens": 0}]
        text = json.dumps({"is_error": True, "session_id": "sess-err2", "total_cost_usd": 0.0,
                           "usage": {"input_tokens": 0, "output_tokens": 0}})
        saved, _ = self.killed_run(150_000, turns, sid="sess-err2", stored_sid="sess-old", result_text=text)
        self.assertEqual(saved, 41_002)

    def test_error_result_with_zero_usage_keeps_last_known_context(self):
        path = self.make_in_progress("Ошибка")
        tid = path.stem
        state = D.load_state()
        D._resume_store(state, tid, "engineer").update({"session_id": "sess-big", "last_context_tokens": 140_000})
        D.save_state(state)
        run_file = self.dispatcher_dir / "err.json"
        run_file.write_text(json.dumps({"is_error": True, "subtype": "success", "num_turns": 1, "session_id": "sess-err",
                                         "total_cost_usd": 0.0, "usage": {"input_tokens": 0, "output_tokens": 0}}),
                            encoding="utf-8")
        info = {"role": "engineer", "popen": None, "pid": None, "started": dt("2026-10-03T10:00:00+04:00"),
                "attempt": 1, "run_file": run_file, "err_file": run_file, "out_fh": None, "err_fh": None,
                "reason": "retry", "status_at_launch": "in_progress", "log_keys_at_launch": []}
        D._finish_run(tid, info, state, dt("2026-10-03T10:20:00+04:00"), timed_out=False)
        saved = D.load_state()["ticket_sessions"][f"{tid}::engineer"]
        self.assertEqual(saved["last_context_tokens"], 140_000)

    def test_run_with_usage_updates_context(self):
        path = self.make_in_progress("Обычный")
        tid = path.stem
        state = D.load_state()
        D._resume_store(state, tid, "engineer").update({"session_id": "s0", "last_context_tokens": 140_000})
        T.append_log(path, "engineer", "шаг", now=dt("2026-10-03T10:01:00+04:00"))
        run_file = self.dispatcher_dir / "ok.json"
        run_file.write_text(json.dumps({"session_id": "s1", "total_cost_usd": 0.1, "num_turns": 2,
                                         "usage": {"input_tokens": 50, "cache_read_input_tokens": 30_000,
                                                   "iterations": [{"input_tokens": 10, "cache_read_input_tokens": 20_000,
                                                                   "cache_creation_input_tokens": 0}]}}), encoding="utf-8")
        info = {"role": "engineer", "popen": None, "pid": None, "started": dt("2026-10-03T10:00:00+04:00"),
                "attempt": 0, "run_file": run_file, "err_file": run_file, "out_fh": None, "err_fh": None,
                "reason": "in_progress-resume", "status_at_launch": "in_progress", "log_keys_at_launch": []}
        D._finish_run(tid, info, state, dt("2026-10-03T10:20:00+04:00"), timed_out=False)
        self.assertEqual(D.load_state()["ticket_sessions"][f"{tid}::engineer"]["last_context_tokens"], 20_010)


class TicketsCliStartTests(unittest.TestCase):
    """v1.1: `tickets.py start` — backlog → todo, и только backlog.
    `cmd_status` читает state.json (траты) — песочница и на TK.TICKETS_DIR, и на D.STATE_FILE, иначе тест
    читает/пишет боевой state.json (поймано на живом файле 27.09 — TK-001/TK-002 утекли в state)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tickets_dir = Path(self.tmp.name) / "tickets"
        self._orig_tickets_dir = TK.TICKETS_DIR
        self._orig_state_file = D.STATE_FILE
        TK.TICKETS_DIR = self.tickets_dir
        D.STATE_FILE = Path(self.tmp.name) / "state.json"

    def tearDown(self):
        TK.TICKETS_DIR = self._orig_tickets_dir
        D.STATE_FILE = self._orig_state_file
        self.tmp.cleanup()

    def test_start_moves_backlog_to_todo(self):
        path = T.create_ticket(self.tickets_dir, owner="researcher", title="Из TASKS.md", status="backlog")
        rc = TK.main(["start", path.stem])
        self.assertEqual(rc, 0)
        self.assertEqual(T.read_ticket(path).status, "todo")

    def test_start_refuses_non_backlog(self):
        path = T.create_ticket(self.tickets_dir, owner="researcher", title="Уже todo", status="todo")
        rc = TK.main(["start", path.stem])
        self.assertEqual(rc, 1)
        self.assertEqual(T.read_ticket(path).status, "todo")

    def test_new_backlog_flag(self):
        rc = TK.main(["new", "--owner", "engineer", "--title", "Перенесено", "--backlog"])
        self.assertEqual(rc, 0)
        tickets = T.list_tickets(self.tickets_dir)
        self.assertEqual(len(tickets), 1)
        self.assertEqual(T.read_ticket(tickets[0]).status, "backlog")

    def test_new_has_no_default_reviewer(self):
        """v2 (02.10): ревьюера по умолчанию нет — Судья только по явному `--reviewer judge`."""
        TK.main(["new", "--owner", "researcher", "--title", "А"])
        TK.main(["new", "--owner", "engineer", "--title", "Б"])
        tickets = {t.header["owner"]: t for t in (T.read_ticket(p) for p in T.list_tickets(self.tickets_dir))}
        self.assertEqual(tickets["researcher"].reviewer, "")
        self.assertEqual(tickets["engineer"].reviewer, "")

    def test_new_no_reviewer_flag_is_accepted_noop(self):
        rc = TK.main(["new", "--owner", "researcher", "--title", "Без ревью", "--no-reviewer"])
        self.assertEqual(rc, 0)
        tkt = T.read_ticket(T.list_tickets(self.tickets_dir)[0])
        self.assertEqual(tkt.reviewer, "")

    def test_new_explicit_reviewer_judge_is_written(self):
        TK.main(["new", "--owner", "researcher", "--title", "С Судьёй", "--reviewer", "judge"])
        tkt = T.read_ticket(T.list_tickets(self.tickets_dir)[0])
        self.assertEqual(tkt.reviewer, "judge")

    def test_new_judge_owner_has_no_default_reviewer(self):
        TK.main(["new", "--owner", "judge", "--title", "Судейское"])
        tkt = T.read_ticket(T.list_tickets(self.tickets_dir)[0])
        self.assertEqual(tkt.reviewer, "")

    def test_new_effort_flag_writes_header_and_rejects_garbage(self):
        TK.main(["new", "--owner", "engineer", "--title", "Лёгкая", "--effort", "medium"])
        self.assertEqual(T.read_ticket(T.list_tickets(self.tickets_dir)[0]).effort, "medium")
        with self.assertRaises(SystemExit):
            import contextlib
            import io
            with contextlib.redirect_stderr(io.StringIO()):
                TK.main(["new", "--owner", "engineer", "--title", "Странная", "--effort", "ultra"])

    def test_comment_next_writes_header_without_touching_updated(self):
        path = T.create_ticket(self.tickets_dir, owner="researcher", title="Передача",
                                now=dt("2026-10-02T10:00:00+04:00"))
        rc = TK.main(["comment", path.stem, "--author", "researcher", "--text", "Прошу вердикт. @judge", "--next",
                      "judge"])
        self.assertEqual(rc, 0)
        tkt = T.read_ticket(path)
        self.assertEqual(tkt.next_role, "judge")
        self.assertEqual(tkt.header["updated"], "2026-10-02T10:00:00+04:00")
        self.assertEqual(tkt.log[-1].author, "researcher")
        # без --next поле не трогаем
        TK.main(["comment", path.stem, "--author", "researcher", "--text", "ещё запись"])
        self.assertEqual(T.read_ticket(path).next_role, "judge")

    def test_comment_next_rejects_unknown_role(self):
        path = T.create_ticket(self.tickets_dir, owner="researcher", title="Передача")
        import contextlib
        import io
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                TK.main(["comment", path.stem, "--author", "researcher", "--text", "x", "--next", "everyone"])

    def test_budget_command_and_flag_are_removed(self):
        """В-173: `tickets.py budget` удалена, `new --budget` не принимается; `new` не пишет в state.json."""
        import contextlib
        import io
        TK.main(["new", "--owner", "engineer", "--title", "Без бюджета"])
        path = T.list_tickets(self.tickets_dir)[0]
        self.assertFalse(D.STATE_FILE.exists(), "new больше не трогает state.json")
        self.assertNotIn("budget", T.read_ticket(path).header)
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                TK.main(["budget", path.stem, "L"])
            with self.assertRaises(SystemExit):
                TK.main(["new", "--owner", "engineer", "--title", "С флагом", "--budget", "L"])
        self.assertEqual(len(T.list_tickets(self.tickets_dir)), 1)

    def test_status_shows_only_spent_for_ceo(self):
        TK.main(["new", "--owner", "engineer", "--title", "С тратами"])
        path = T.list_tickets(self.tickets_dir)[0]
        state = D.load_state()
        D.add_ticket_cost(state, path.stem, 1.5)
        D.save_state(state)
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            TK.main(["status"])
        out = buf.getvalue()
        self.assertIn("потрачено", out)
        self.assertIn("$1.50", out)
        self.assertNotIn("/$", out, "колонки лимита нет — только потрачено")
        self.assertNotIn("бюджет", out)

    def test_new_haiku_requires_kind(self):
        rc = TK.main(["new", "--owner", "engineer", "--title", "Без kind", "--executor", "haiku"])
        self.assertEqual(rc, 1)
        self.assertEqual(T.list_tickets(self.tickets_dir), [])

    def test_new_haiku_without_reviewer_flag_is_allowed_but_explicit_judge_is_refused(self):
        """v2: ревьюера по умолчанию нет, так что `--executor haiku` проходит без `--no-reviewer`; явный
        `--reviewer judge` с haiku по-прежнему запрещён (условие г судьи TK-002)."""
        rc = TK.main(["new", "--owner", "engineer", "--title", "Механическая без флага",
                      "--executor", "haiku", "--kind", "file-move"])
        self.assertEqual(rc, 0)
        rc = TK.main(["new", "--owner", "engineer", "--title", "Обход", "--reviewer", "judge",
                      "--executor", "haiku", "--kind", "file-move"])
        self.assertEqual(rc, 1)
        self.assertEqual(len(T.list_tickets(self.tickets_dir)), 1)

    def test_new_haiku_researcher_owner_is_refused(self):
        rc = TK.main(["new", "--owner", "researcher", "--title", "Не Haiku", "--no-reviewer",
                      "--executor", "haiku", "--kind", "publish"])
        self.assertEqual(rc, 1)
        self.assertEqual(T.list_tickets(self.tickets_dir), [])

    def test_new_haiku_valid_combo_succeeds(self):
        rc = TK.main(["new", "--owner", "engineer", "--title", "Механическая", "--no-reviewer",
                      "--executor", "haiku", "--kind", "table-format"])
        self.assertEqual(rc, 0)
        tkt = T.read_ticket(T.list_tickets(self.tickets_dir)[0])
        self.assertEqual(tkt.executor, "haiku")
        self.assertEqual(tkt.kind, "table-format")
        self.assertEqual(tkt.reviewer, "")

    def test_new_kind_without_executor_is_refused(self):
        rc = TK.main(["new", "--owner", "engineer", "--title", "Странно", "--kind", "publish"])
        self.assertEqual(rc, 1)


class TicketsCliStopTests(unittest.TestCase):
    """`tickets.py stop` (CEO, 03.10): заявка диспетчеру. Сам тикет команда не трогает — статус, след и запись CEO пишет
    диспетчер после снятия процесса (иначе ещё живая роль перетёрла бы статус)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.tickets_dir = self.base / "tickets"
        self._orig = (TK.TICKETS_DIR, D.STOP_DIR)
        TK.TICKETS_DIR, D.STOP_DIR = self.tickets_dir, self.base / "stop"
        self._env = {k: os.environ.pop(k, None) for k in ("RPV_ROLE",)}

    def tearDown(self):
        TK.TICKETS_DIR, D.STOP_DIR = self._orig
        for k, v in self._env.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v
        self.tmp.cleanup()

    def request(self, tid):
        return json.loads((D.STOP_DIR / f"{tid}.json").read_text(encoding="utf-8"))

    def test_stop_writes_request_and_leaves_ticket_untouched(self):
        path = T.create_ticket(self.tickets_dir, owner="engineer", title="Идёт")
        T.write_header_updates(path, {"status": "in_progress"})
        before = path.read_bytes()
        rc = TK.main(["stop", path.stem, "--text", "Новая постановка", "--next", "engineer"])
        self.assertEqual(rc, 0)
        req = self.request(path.stem)
        self.assertEqual((req["next"], req["text"]), ("engineer", "Новая постановка"))
        T.parse_dt(req["at"])
        self.assertEqual(path.read_bytes(), before)

    def test_stop_without_next_writes_empty_next(self):
        path = T.create_ticket(self.tickets_dir, owner="engineer", title="Идёт")
        self.assertEqual(TK.main(["stop", path.stem, "--text", "Стоп"]), 0)
        self.assertEqual(self.request(path.stem)["next"], "")

    def test_stop_refuses_unknown_ticket_and_empty_text(self):
        path = T.create_ticket(self.tickets_dir, owner="engineer", title="Идёт")
        self.assertEqual(TK.main(["stop", "TK-404", "--text", "x"]), 1)
        self.assertEqual(TK.main(["stop", path.stem, "--text", "   "]), 1)
        self.assertFalse(D.STOP_DIR.exists(), "заявки нет")

    def test_stop_is_for_ceo_only(self):
        path = T.create_ticket(self.tickets_dir, owner="engineer", title="Идёт")
        os.environ["RPV_ROLE"] = "engineer"  # диспетчер ставит роль в окружение запуска роли
        self.assertEqual(TK.main(["stop", path.stem, "--text", "x"]), 1)
        self.assertFalse(D.STOP_DIR.exists())
        os.environ["RPV_ROLE"] = "ceo"
        self.assertEqual(TK.main(["stop", path.stem, "--text", "x"]), 0)

    def test_stop_next_accepts_only_roles_the_dispatcher_starts(self):
        import contextlib
        import io
        path = T.create_ticket(self.tickets_dir, owner="engineer", title="Идёт")
        with contextlib.redirect_stderr(io.StringIO()):
            for bad in ("ceo", "everyone"):
                with self.assertRaises(SystemExit):
                    TK.main(["stop", path.stem, "--text", "x", "--next", bad])
        self.assertFalse(D.STOP_DIR.exists())

    def test_second_stop_replaces_the_first(self):
        path = T.create_ticket(self.tickets_dir, owner="engineer", title="Идёт")
        TK.main(["stop", path.stem, "--text", "Первая"])
        TK.main(["stop", path.stem, "--text", "Вторая", "--next", "judge"])
        req = self.request(path.stem)
        self.assertEqual((req["text"], req["next"]), ("Вторая", "judge"))
        self.assertEqual(len(list(D.STOP_DIR.glob("*.json"))), 1)

    def test_start_returns_stopped_ticket_to_todo_and_only_backlog_or_stopped(self):
        stopped = T.create_ticket(self.tickets_dir, owner="engineer", title="Остановлена", status="stopped")
        done = T.create_ticket(self.tickets_dir, owner="engineer", title="Готова", status="done")
        self.assertEqual(TK.main(["start", stopped.stem]), 0)
        self.assertEqual(T.read_ticket(stopped).status, "todo")
        self.assertEqual(TK.main(["start", done.stem]), 1)
        self.assertEqual(T.read_ticket(done).status, "done")


class JudgeSimulationDecideTests(unittest.TestCase):
    """Переложение симуляций Судьи (TK-001, 27.09) на unittest — pure `decide()`, без процессов.
    Источник: `.claude/tickets/TK-001.md` «## Лог» (запись judge 21:39) и
    `docs/research/reviews/scripts/dispatcher-sim-2026-09-27.py` (номера симуляций совпадают)."""

    def setUp(self):
        self.t0 = dt("2026-09-27T22:00:00+04:00")

    def mk(self, tid, hdr, log=""):
        text = "---\n" + "\n".join(f"{k}: {v}" for k, v in hdr.items()) + "\n---\n\nописание\n\n## Лог\n" + log
        return T.parse_text(text, Path(f"{tid}.md"))

    def test_sim1_blocked_after_dispatcher_own_entry_no_longer_rewakes(self):
        """(1) обязательно: своя запись dispatcher про blocked не должна выглядеть упоминанием роли."""
        tkt = self.mk("X1", dict(id="X1", title="t", owner="researcher", status="blocked",
                                  updated=iso(self.t0)))
        # append_log пишет ЧЕРЕЗ файл — соберём текст руками, как делает симуляция Судьи
        entry_text = "Запуск роли researcher — дважды не оставил запись в «## Лог» — задача заблокирована, нужен @ceo."
        tkt.log.append(T.LogEntry(ts=self.t0, ts_raw=iso(self.t0), author="dispatcher", text=entry_text))
        state = {"sessions": {"X1::researcher": {"last_woken": iso(self.t0 - timedelta(minutes=5))}}}
        dec = D.decide(tkt, state, self.t0 + timedelta(minutes=2))
        self.assertIsNone(dec, "запись dispatcher не должна снова будить researcher на blocked-тикете")

    def test_sim2_in_progress_orphan_now_resumed(self):
        """(2) обязательно: in_progress без активного запуска и без упоминания — раньше замирал навсегда."""
        tkt = self.mk("X2", dict(id="X2", title="t", owner="engineer", status="in_progress",
                                  updated=iso(self.t0)), f"### {iso(self.t0)} engineer\nсделал шаг 1, дальше шаг 2\n")
        dec = D.decide(tkt, {}, self.t0 + timedelta(hours=3))
        self.assertEqual((dec.role, dec.reason), ("engineer", "in_progress-resume"))

    def test_sim3_review_accepted_then_done_no_longer_rewakes_judge(self):
        """(3) обязательно: ревьюер написал «принято» и поставил done — не будить его снова."""
        tkt = self.mk("X3", dict(id="X3", title="t", owner="researcher", status="in_review",
                                  reviewer="judge", updated=iso(self.t0)))
        t1 = self.t0 + timedelta(minutes=10)
        tkt.log.append(T.LogEntry(ts=t1, ts_raw=iso(t1), author="judge", text="принято @ceo"))
        tkt.header["status"] = "done"  # write_header_updates(now=t1+30s) в реальности — updated новее записи
        state = {"sessions": {"X3::judge": {"last_woken": iso(self.t0)}}}
        dec = D.decide(tkt, state, t1 + timedelta(minutes=2))
        self.assertIsNone(dec, "последняя запись лога — самого ревьюера, повторный вызов не нужен")

    def test_sim3b_review_left_in_review_is_not_touched_by_rule_g(self):
        """(3b) судья прокомментировал, но не поставил done — правило (г) не про этот статус вообще."""
        tkt = self.mk("X3b", dict(id="X3b", title="t", owner="researcher", status="in_review",
                                   reviewer="judge", updated=iso(self.t0)))
        t1 = self.t0 + timedelta(minutes=10)
        tkt.log.append(T.LogEntry(ts=t1, ts_raw=iso(t1), author="judge", text="принято"))
        state = {"sessions": {"X3b::judge": {"last_woken": iso(self.t0)}}}
        dec = D.decide(tkt, state, t1 + timedelta(minutes=2))
        self.assertIsNone(dec)

    def test_sim7_self_mention_does_not_rewake_author(self):
        """(7) можно потом: роль напоминает сама себе — не должна запускать сама себя повторно."""
        tkt = self.mk("X7", dict(id="X7", title="t", owner="researcher", status="in_review",
                                  reviewer="judge", updated=iso(self.t0)),
                       f"### {iso(self.t0 + timedelta(minutes=5))} researcher\n"
                       "сделал; напоминание себе: @researcher завтра проверить\n")
        state = {"sessions": {"X7::researcher": {"last_woken": iso(self.t0)}}}
        dec = D.decide(tkt, state, self.t0 + timedelta(minutes=6))
        # самоупоминание не будит researcher; А4 (аудит 03.10): in_review при заданном reviewer будит ревьюера
        # (judge) — по статусу, не по упоминанию
        self.assertEqual((dec.role, dec.reason), ("judge", "review"))

    def test_ticket_wait_for_condition(self):
        """«Можно потом»: `wait_for: ticket:<ID>` — зависимость от другого тикета (раньше жила прозой)."""
        with tempfile.TemporaryDirectory() as d:
            tdir = Path(d)
            orig_dir = D.TICKETS_DIR
            D.TICKETS_DIR = tdir
            try:
                T.create_ticket(tdir, owner="engineer", title="Блокер", status="in_progress")
                blocker = T.list_tickets(tdir)[0]
                text = (f"---\nid: X8\nowner: researcher\nstatus: waiting\nwait_for: ticket:{blocker.stem}\n"
                        f"updated: {iso(self.t0)}\n---\n\n## Лог\n")
                tkt = T.parse_text(text, Path("X8.md"))
                self.assertIsNone(D.decide(tkt, {}, self.t0))  # блокер ещё не done
                T.write_header_updates(blocker, {"status": "done"})
                self.assertEqual(D.decide(tkt, {}, self.t0).reason, "wait_for-met")
            finally:
                D.TICKETS_DIR = orig_dir


def iso(d):
    return d.isoformat(timespec="seconds")


class RoleMcpTests(unittest.TestCase):
    """TK-171: RPV_ROLE_MCP — all (умолчание, без флагов) | none | список имён."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        home, proj = self.tmp / "home", self.tmp / "proj"
        home.mkdir()
        proj.mkdir()
        (home / ".claude.json").write_text(json.dumps({
            "mcpServers": {"figma": {"type": "http", "url": "https://f"}, "pulse": {"command": "p"}},
            "projects": {str(proj): {"mcpServers": {"local": {"command": "l"}}}}}), encoding="utf-8")
        (proj / ".mcp.json").write_text(json.dumps({"mcpServers": {"xcodebuildmcp": {"command": "x"}}}), encoding="utf-8")
        self.runs = self.tmp / "runs"
        self.runs.mkdir()
        env = {"HOME": str(home), "USERPROFILE": str(home)}
        for patcher in (mock.patch.dict(os.environ, env), mock.patch.object(D, "PROJECT_ROOT", proj),
                        mock.patch.object(D, "RUNS_DIR", self.runs)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _args(self, mode):
        env = {"RPV_ROLE_MCP": mode} if mode is not None else {}
        with mock.patch.dict(os.environ, env):
            if mode is None:
                os.environ.pop("RPV_ROLE_MCP", None)
            return D.role_mcp_args("run1")

    def test_default_and_all_have_no_flags(self):
        self.assertEqual(self._args(None), [])
        self.assertEqual(self._args("all"), [])

    def test_none_is_strict_without_config(self):
        self.assertEqual(self._args("none"), ["--strict-mcp-config"])

    def test_list_builds_config_from_user_project_and_mcp_json(self):
        args = self._args("figma, xcodebuildmcp,local,ghost")
        self.assertEqual(args[:2], ["--strict-mcp-config", "--mcp-config"])
        cfg = json.loads(Path(args[2]).read_text(encoding="utf-8"))
        self.assertEqual(sorted(cfg["mcpServers"]), ["figma", "local", "xcodebuildmcp"])  # pulse не просили, ghost нет

class RateLimitTests(unittest.TestCase):
    """v1.1, чистые функции — без процессов и без сети."""

    def setUp(self):
        self.state = {}
        self.now = dt("2026-09-27T12:00:00+04:00")

    def test_min_gap_blocks_then_clears(self):
        D._record_launch(self.state, "TK-1", self.now)
        soon = self.now + timedelta(seconds=10)
        self.assertTrue(D._rate_limited(self.state, "TK-1", soon))
        later = self.now + timedelta(seconds=D.MIN_GAP_S + 1)
        self.assertFalse(D._rate_limited(self.state, "TK-1", later))

    def test_other_ticket_not_affected(self):
        D._record_launch(self.state, "TK-1", self.now)
        self.assertFalse(D._rate_limited(self.state, "TK-2", self.now + timedelta(seconds=1)))

    def test_max_runs_per_hour(self):
        step = timedelta(seconds=D.MIN_GAP_S + 1)
        for i in range(D.MAX_RUNS_PER_TICKET_HOUR):
            D._record_launch(self.state, "TK-1", self.now + i * step)
        probe = self.now + D.MAX_RUNS_PER_TICKET_HOUR * step
        self.assertTrue(D._rate_limited(self.state, "TK-1", probe), "часовой лимит должен был сработать")
        far_later = self.now + timedelta(hours=2)
        self.assertFalse(D._rate_limited(self.state, "TK-1", far_later), "час прошёл — лимит снят")

    def test_daily_cost_is_only_accounted_per_day(self):
        """Суточный учёт — по календарной дате; никакого «превышено» нет (В-149)."""
        D._add_cost(self.state, self.now, 12.5)
        D._add_cost(self.state, self.now, 1.5)
        self.assertAlmostEqual(self.state["daily_cost"][D._today(self.now)], 14.0)
        tomorrow = self.now + timedelta(days=1)
        self.assertNotIn(D._today(tomorrow), self.state["daily_cost"])


class MoneyControlsTests(unittest.TestCase):
    """v1.3 (владелец 27.09): модель/усилие, учёт трат, потолок запуска, холостой ход. В-173: лимитов денег на
    тикет/час/сутки нет — остались учёт и потолок одного запуска. Чистые функции — без процессов и без сети;
    сквозные (--max-budget-usd, cost-фолбэк, blocked) — в DispatchRunTests
    (test_launch_run_sets_model_effort_and_run_cap, test_money_* ниже)."""

    def setUp(self):
        self.state = {}
        self.now = dt("2026-09-27T12:00:00+04:00")
        self.tmp = tempfile.TemporaryDirectory()
        self._orig_inbox, self._orig_wake = D.CEO_INBOX, D.CEO_WAKE_LOG
        D.CEO_INBOX = Path(self.tmp.name) / "ceo-inbox.md"
        D.CEO_WAKE_LOG = Path(self.tmp.name) / "ceo-wake.log"

    def tearDown(self):
        D.CEO_INBOX, D.CEO_WAKE_LOG = self._orig_inbox, self._orig_wake
        self.tmp.cleanup()

    # --- v1.4, судья TK-002 п.3 (взамен привратника TypeSafe): таблица правил кодом ---

    def test_classify_signal_defaults_to_wake(self):
        for kind in ("blocked", "needs_owner", "no-reviewer",
                     "mention", "parse-error", "какой-то-новый-вид-никто-не-обновил-таблицу"):
            self.assertEqual(D.classify_signal(kind), "wake", kind)

    def test_classify_signal_model_is_summary(self):
        self.assertEqual(D.classify_signal("model"), "summary")

    def test_route_ceo_signal_wake_kind_appears_immediately(self):
        D.route_ceo_signal("TK-1", "blocked", "тест", self.state, self.now)
        self.assertIn("тест", D.CEO_INBOX.read_text(encoding="utf-8"))

    def test_route_ceo_signal_summary_kind_not_lost_flushes_eventually(self):
        """Ничего не выкидывается в «только журнал» — просто уходит не сразу, а пачкой."""
        D.route_ceo_signal("TK-1", "model", "не-opus модель X", self.state, self.now)
        # первый флаш случается сразу (нет last_summary_flush) — но проверим, что текст ГДЕ-ТО есть
        self.assertTrue(D.CEO_INBOX.exists())
        self.assertIn("не-opus модель X", D.CEO_INBOX.read_text(encoding="utf-8"))

    def test_route_ceo_signal_summary_batches_within_window(self):
        D.flush_pending_summary(self.state, self.now)  # задаём точку отсчёта окна (пусто — просто маркер)
        D.route_ceo_signal("TK-1", "model", "первая", self.state, self.now + timedelta(minutes=1))
        before = D.CEO_INBOX.read_text(encoding="utf-8") if D.CEO_INBOX.exists() else ""
        D.route_ceo_signal("TK-2", "model", "вторая", self.state, self.now + timedelta(minutes=2))
        after_immediate = D.CEO_INBOX.read_text(encoding="utf-8") if D.CEO_INBOX.exists() else ""
        self.assertEqual(before, after_immediate, "вторая копится, не уходит немедленно внутри окна")
        D.route_ceo_signal("TK-3", "model", "третья", self.state,
                            self.now + timedelta(hours=D.SUMMARY_EVERY_HOURS, minutes=5))
        final = D.CEO_INBOX.read_text(encoding="utf-8")
        self.assertIn("вторая", final)
        self.assertIn("третья", final)

    # --- v1.4, судья TK-002 п.5: executor: haiku ---

    def test_haiku_refused_reason_none_for_non_haiku(self):
        text = ("---\nid: X1\nowner: engineer\nstatus: todo\nupdated: 2026-09-27T12:00:00+04:00\n---\n\n## Лог\n")
        self.assertIsNone(D.haiku_refused_reason(T.parse_text(text, Path("X1.md"))))

    def test_haiku_refused_reason_bad_kind(self):
        text = ("---\nid: X2\nowner: engineer\nstatus: todo\nexecutor: haiku\nkind: something-else\n"
                "updated: 2026-09-27T12:00:00+04:00\n---\n\n## Лог\n")
        reason = D.haiku_refused_reason(T.parse_text(text, Path("X2.md")))
        self.assertIsNotNone(reason)
        self.assertIn("kind", reason)

    def test_haiku_refused_reason_reviewer_judge(self):
        text = ("---\nid: X3\nowner: engineer\nstatus: todo\nexecutor: haiku\nkind: file-move\nreviewer: judge\n"
                "updated: 2026-09-27T12:00:00+04:00\n---\n\n## Лог\n")
        reason = D.haiku_refused_reason(T.parse_text(text, Path("X3.md")))
        self.assertIsNotNone(reason)
        self.assertIn("judge", reason)

    def test_haiku_refused_reason_owner_researcher(self):
        text = ("---\nid: X4\nowner: researcher\nstatus: todo\nexecutor: haiku\nkind: publish\n"
                "updated: 2026-09-27T12:00:00+04:00\n---\n\n## Лог\n")
        reason = D.haiku_refused_reason(T.parse_text(text, Path("X4.md")))
        self.assertIsNotNone(reason)
        self.assertIn("researcher", reason)

    def test_haiku_refused_reason_allowed_case(self):
        text = ("---\nid: X5\nowner: engineer\nstatus: todo\nexecutor: haiku\nkind: table-format\n"
                "updated: 2026-09-27T12:00:00+04:00\n---\n\n## Лог\n")
        self.assertIsNone(D.haiku_refused_reason(T.parse_text(text, Path("X5.md"))))

    # --- v1.4, судья TK-002 п.1: разница по session_id, не кумулятивный итог ---

    def test_resolve_run_cost_first_call_takes_total_as_is(self):
        """(б) нет прошлого итога → берём итог как есть, помечаем."""
        result = {"session_id": "s1", "total_cost_usd": 6.7985, "modelUsage": {"claude-sonnet-5-5": {"costUSD": 6.7985}}}
        cost, diff, note = D.resolve_run_cost(self.state, result)
        self.assertAlmostEqual(cost, 6.7985)
        self.assertTrue(note)
        self.assertEqual(self.state["session_cost_seen"]["s1"], 6.7985)

    def test_resolve_run_cost_second_call_is_diff_not_cumulative(self):
        """Живой случай 27.09: сессия судьи $6,80 → $8,29 → $8,74 кумулятивных, реально — $1,49 и $0,45."""
        r1 = {"session_id": "s1", "total_cost_usd": 6.798510249999997,
              "modelUsage": {"claude-fable-5-1": {"costUSD": 6.249625249999998},
                              "claude-sonnet-5-5": {"costUSD": 0.5488850000000001}}}
        r2 = {"session_id": "s1", "total_cost_usd": 8.289092449999998,
              "modelUsage": {"claude-fable-5-1": {"costUSD": 6.249625249999998},
                              "claude-sonnet-5-5": {"costUSD": 2.0394672000000003}}}
        r3 = {"session_id": "s1", "total_cost_usd": 8.735297849999998,
              "modelUsage": {"claude-fable-5-1": {"costUSD": 6.249625249999998},
                              "claude-sonnet-5-5": {"costUSD": 2.4856726000000005}}}
        cost1, diff1, note1 = D.resolve_run_cost(self.state, r1)
        cost2, diff2, note2 = D.resolve_run_cost(self.state, r2)
        cost3, diff3, note3 = D.resolve_run_cost(self.state, r3)
        self.assertAlmostEqual(cost2, 1.490582200000001, places=6)
        self.assertAlmostEqual(cost3, 0.446205400000000, places=6)
        self.assertFalse(note2)
        self.assertFalse(note3)
        # инвариант судьи (г): сумма разниц = последний итог
        self.assertAlmostEqual(cost1 + cost2 + cost3, r3["total_cost_usd"], places=6)
        # (в) fable не рос — не в разнице; sonnet рос — в разнице (ложной тревоги «не-sonnet» нет)
        self.assertNotIn("claude-fable-5-1", diff2)
        self.assertNotIn("claude-fable-5-1", diff3)
        self.assertIn("claude-sonnet-5-5", diff2)
        self.assertIsNone(D._model_usage_warning(diff2))
        self.assertIsNone(D._model_usage_warning(diff3))

    def test_resolve_run_cost_negative_diff_takes_total_as_is(self):
        """(б) разница < 0 (например счётчик сброшен на стороне API) → берём итог как есть, помечаем."""
        D.resolve_run_cost(self.state, {"session_id": "s1", "total_cost_usd": 5.0})
        cost, diff, note = D.resolve_run_cost(self.state, {"session_id": "s1", "total_cost_usd": 1.0})
        self.assertAlmostEqual(cost, 1.0)
        self.assertTrue(note)

    def test_resolve_run_cost_rotation_starts_fresh(self):
        """(а) разница — по session_id, не «хранилищу роли»: новая сессия после ротации начинает с нуля."""
        D.resolve_run_cost(self.state, {"session_id": "old-sid", "total_cost_usd": 20.0})
        cost, diff, note = D.resolve_run_cost(self.state, {"session_id": "new-sid-after-rotation",
                                                             "total_cost_usd": 0.5})
        self.assertAlmostEqual(cost, 0.5)  # не 0.5 - 20.0 — это другая сессия
        self.assertTrue(note)

    # --- п.1: модель/усилие ---

    def test_role_effort_mapping(self):
        # v2 (02.10): исследователь/инженер — high, Судья — xhigh (тест в окружении без RPV_DISPATCH_EFFORT)
        if not P.env("DISPATCH_EFFORT"):
            self.assertEqual(D.ROLE_EFFORT, {"judge": "xhigh", "engineer": "high", "researcher": "high"})

    def test_role_effort_env_override(self):
        base = {"judge": "xhigh", "engineer": "xhigh", "researcher": "xhigh"}
        self.assertEqual(D._parse_role_map("judge:xhigh,engineer:high", base),
                         {"judge": "xhigh", "engineer": "high", "researcher": "xhigh"})
        self.assertEqual(D._parse_role_map("high", base),
                         {"judge": "high", "engineer": "high", "researcher": "high"})
        self.assertEqual(D._parse_role_map("", base), base)
        self.assertEqual(base["engineer"], "xhigh")  # исходный словарь не меняется

    def test_role_model_defaults_and_env_override(self):
        # v1.6.1: Судья — Opus 5.5 (проверка всех не ослабляется), остальные — CLAUDE_MODEL
        if not P.env("DISPATCH_ROLE_MODEL"):
            self.assertEqual(D.ROLE_MODEL, {"judge": "claude-opus-5-5", "engineer": D.CLAUDE_MODEL,
                                            "researcher": D.CLAUDE_MODEL})
        base = {"judge": "claude-opus-5-5", "engineer": "claude-sonnet-5-5", "researcher": "claude-sonnet-5-5"}
        self.assertEqual(D._parse_role_map("judge:claude-opus-5-5,engineer:claude-opus-5-5", base),
                         {"judge": "claude-opus-5-5", "engineer": "claude-opus-5-5",
                          "researcher": "claude-sonnet-5-5"})
        self.assertEqual(D._parse_role_map("claude-sonnet-5-5", base),
                         {"judge": "claude-sonnet-5-5", "engineer": "claude-sonnet-5-5",
                          "researcher": "claude-sonnet-5-5"})
        self.assertEqual(base["judge"], "claude-opus-5-5")  # исходный словарь не меняется

    def test_expected_model_family_follows_role_model(self):
        orig_role_model = D.ROLE_MODEL
        D.ROLE_MODEL = {"judge": "claude-opus-5-5", "engineer": "claude-sonnet-5-5",
                        "researcher": "claude-sonnet-5-5"}
        try:
            self.assertEqual(D._expected_model_family({"role": "judge"}), "opus")
            self.assertEqual(D._expected_model_family({"role": "engineer"}), "sonnet")
            self.assertEqual(D._expected_model_family({"role": "researcher"}), "sonnet")
            # роль вне словаря — семейство общего CLAUDE_MODEL; executor haiku — «haiku» при любой роли
            self.assertEqual(D._expected_model_family({"role": "ceo"}), D.model_family(D.CLAUDE_MODEL))
            self.assertEqual(D._expected_model_family({"role": "judge", "executor": "haiku"}), "haiku")
            # на практике: судья на opus — тишина; тот же opus у инженера — тревога (и наоборот)
            usage = {"claude-opus-5-5": {"cost": 1.0}}
            self.assertIsNone(D._model_usage_warning(usage, D._expected_model_family({"role": "judge"})))
            self.assertIsNotNone(D._model_usage_warning(usage, D._expected_model_family({"role": "engineer"})))
            usage = {"claude-sonnet-5-5": {"cost": 1.0}}
            self.assertIsNone(D._model_usage_warning(usage, D._expected_model_family({"role": "engineer"})))
            self.assertIsNotNone(D._model_usage_warning(usage, D._expected_model_family({"role": "judge"})))
        finally:
            D.ROLE_MODEL = orig_role_model

    def test_model_family_from_id(self):
        self.assertEqual(D.model_family("claude-sonnet-5-5"), "sonnet")
        self.assertEqual(D.model_family("claude-opus-5-5"), "opus")
        self.assertEqual(D.model_family("claude-haiku-5-5"), "haiku")
        self.assertEqual(D.model_family("Fable-5-1"), "fable-5-1")
        # ожидаемое по умолчанию — семейство CLAUDE_MODEL, а не жёстко «opus»
        fam = D.model_family(D.CLAUDE_MODEL)
        self.assertIsNone(D._model_usage_warning({f"claude-{fam}-x": {"cost": 1.0}}))
        other = "opus" if fam != "opus" else "sonnet"
        self.assertIsNotNone(D._model_usage_warning({f"claude-{other}-x": {"cost": 1.0}}))

    def test_model_usage_warning_none_when_absent_or_empty(self):
        # v1.4: принимает уже РАЗНИЦУ modelUsage (_model_usage_diff), не сырой результат
        self.assertIsNone(D._model_usage_warning({}))
        self.assertIsNone(D._model_usage_warning(None))
        self.assertIsNone(D._model_usage_warning("not-a-dict"))

    def test_model_usage_warning_silent_for_expected_family_only(self):
        self.assertIsNone(D._model_usage_warning({"claude-sonnet-5-5": {"cost": 1.2}}))

    def test_model_usage_warning_flags_other_family(self):
        warn = D._model_usage_warning({"fable-5-1": {"cost": 6.8}})
        self.assertIsNotNone(warn)
        self.assertIn("fable-5-1", warn)

    def test_model_usage_diff_ignores_unchanged_historical_model(self):
        """v1.4 (судья TK-002 п.1в, живой прогон 27.09): модель, не выросшая с прошлого раза —
        историческая примесь (например Fable из первого домодельного вызова сессии), не тревога."""
        current = {"claude-fable-5-1": {"costUSD": 6.25}, "claude-sonnet-5-5": {"costUSD": 2.49}}
        previous = {"claude-fable-5-1": {"costUSD": 6.25}, "claude-sonnet-5-5": {"costUSD": 2.04}}
        diff = D._model_usage_diff(current, previous)
        self.assertNotIn("claude-fable-5-1", diff)
        self.assertIn("claude-sonnet-5-5", diff)
        self.assertIsNone(D._model_usage_warning(diff))  # sonnet вырос — тревоги нет, это ожидаемая модель

    def test_model_usage_diff_flags_new_other_family_model(self):
        current = {"claude-fable-5-1": {"costUSD": 1.0}}
        diff = D._model_usage_diff(current, {})
        self.assertIn("claude-fable-5-1", diff)
        self.assertIsNotNone(D._model_usage_warning(diff))

    # --- учёт трат (В-173: показатель, не лимит) ---

    def test_ticket_cost_accumulates_per_ticket(self):
        self.assertEqual(D.ticket_cost_spent(self.state, "TK-1"), 0.0)
        D.add_ticket_cost(self.state, "TK-1", 3.0)
        D.add_ticket_cost(self.state, "TK-1", 2.5)
        D.add_ticket_cost(self.state, "TK-2", 100.0)
        self.assertAlmostEqual(D.ticket_cost_spent(self.state, "TK-1"), 5.5)
        self.assertAlmostEqual(D.ticket_cost_spent(self.state, "TK-2"), 100.0)

    def test_done_line_has_no_budget_note_even_when_spend_is_high(self):
        """Единственная строка `done` — без доли бюджета (бюджетов нет, В-173)."""
        for spent in (0.5, 8.0, 500.0):
            state = {}
            D.add_ticket_cost(state, "TK-1", spent)
            tkt = T.parse_text("---\nid: TK-1\ntitle: Закрытая\nowner: researcher\nstatus: done\n"
                               "updated: 2026-09-27T11:00:00+04:00\n---\n\n## Лог\n", Path("TK-1.md"))
            if D.CEO_INBOX.exists():
                D.CEO_INBOX.unlink()
            D.notify_done(tkt, state, self.now)
            text = D.CEO_INBOX.read_text(encoding="utf-8")
            self.assertNotIn("бюджет", text, spent)
            self.assertEqual(text.count("[done]"), 1)

    def test_rolling_hour_cost_is_accounting_only(self):
        """Скользящий час по всем ролям — показатель для `tickets.py status`; никого не блокирует."""
        D._record_cost_event(self.state, self.now, 5.0)
        D._record_cost_event(self.state, self.now + timedelta(minutes=1), 7.0)
        self.assertAlmostEqual(D._rolling_hour_cost(self.state, self.now + timedelta(minutes=2)), 12.0)
        later = self.now + timedelta(hours=1, minutes=2)
        self.assertAlmostEqual(D._rolling_hour_cost(self.state, later), 0.0, msg="час прошёл — окно очистилось")
        self.assertFalse(D.CEO_INBOX.exists(), "учёт ничего не пишет CEO")


class ContextTokensTests(unittest.TestCase):
    """v1.1 (CEO 27.09): usage в JSON `claude -p` — сумма по всем ходам запуска, не контекст одного
    хода. Ротация должна смотреть на последний ход (usage.iterations[-1] или ctx_sum // num_turns),
    иначе долгий многоходовый запуск рвёт долгую сессию сразу же (живой прогон: ctx_sum=333886 при
    реальном контексте хода ~52 тыс.)."""

    def test_sum_is_plain_total_of_usage_dict(self):
        usage = {"input_tokens": 100, "cache_read_input_tokens": 200, "cache_creation_input_tokens": 50}
        self.assertEqual(D._context_tokens_sum(usage), 350)
        self.assertEqual(D._context_tokens_sum(None), 0)

    def test_last_uses_final_iteration_when_present(self):
        result = {
            "num_turns": 3,
            "usage": {
                "input_tokens": 300, "cache_read_input_tokens": 300_000, "cache_creation_input_tokens": 33_586,
                "iterations": [
                    {"input_tokens": 50, "cache_read_input_tokens": 10_000, "cache_creation_input_tokens": 20_000},
                    {"input_tokens": 60, "cache_read_input_tokens": 15_000, "cache_creation_input_tokens": 5_000},
                    {"input_tokens": 162, "cache_read_input_tokens": 50_000, "cache_creation_input_tokens": 2_000},
                ],
            },
        }
        # контекст последнего хода — только третья итерация, не сумма usage целиком (333 886)
        self.assertEqual(D._context_tokens_last(result), 162 + 50_000 + 2_000)
        self.assertEqual(D._context_tokens_sum(result["usage"]), 300 + 300_000 + 33_586)

    def test_last_without_iterations_and_transcript_is_only_a_lower_estimate(self):
        # живой смоук 27.09 (без iterations в реальном выводе на тот момент): ctx_sum=333886, ходов не 1; запасной
        # путь, когда нет ни iterations, ни транскрипта сессии (см. тесты транскрипта ниже)
        result = {"num_turns": 5, "usage": {"input_tokens": 162, "cache_read_input_tokens": 300_000,
                                             "cache_creation_input_tokens": 33_724}}
        total = D._context_tokens_sum(result["usage"])
        self.assertEqual(D._context_tokens_last(result), total // 5)
        self.assertLess(D._context_tokens_last(result), total)  # не завышен суммой всех ходов

    def write_transcript(self, root, session_id, turns):
        proj = Path(root) / "any-project-slug"
        proj.mkdir(parents=True, exist_ok=True)
        lines = []
        for u in turns:
            lines.append(json.dumps({"type": "assistant", "message": {"role": "assistant", "usage": u}}))
        lines.append(json.dumps({"type": "user", "message": {"role": "user", "content": "x"}}))
        (proj / f"{session_id}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_last_without_iterations_takes_last_turn_from_session_transcript(self):
        """A6: нет `usage.iterations` — контекст ПОСЛЕДНЕГО хода берётся из транскрипта сессии, не среднее по ходам."""
        result = {"session_id": "sess-t1", "num_turns": 5,
                  "usage": {"input_tokens": 162, "cache_read_input_tokens": 300_000, "cache_creation_input_tokens": 33_724}}
        with tempfile.TemporaryDirectory() as d:
            self.write_transcript(d, "sess-t1", [
                {"input_tokens": 5, "cache_read_input_tokens": 10_000, "cache_creation_input_tokens": 0},
                {"input_tokens": 7, "cache_read_input_tokens": 70_000, "cache_creation_input_tokens": 3_000}])
            orig = D.CLAUDE_PROJECTS_DIR
            D.CLAUDE_PROJECTS_DIR = Path(d)
            try:
                self.assertEqual(D._context_tokens_last(result), 7 + 70_000 + 3_000)
            finally:
                D.CLAUDE_PROJECTS_DIR = orig
        self.assertNotEqual(7 + 70_000 + 3_000, (162 + 300_000 + 33_724) // 5)

    def test_context_for_store_keeps_previous_when_nothing_known_and_never_drops_below_it(self):
        self.assertEqual(D._context_tokens_for_store({}, 150_000), 150_000)                      # нет JSON — прежнее
        self.assertEqual(D._context_tokens_for_store({"usage": {}}, 150_000), 150_000)
        self.assertEqual(D._context_tokens_for_store({"usage": {"input_tokens": 0}}, 150_000), 150_000)
        iters = {"usage": {"input_tokens": 9, "iterations": [{"input_tokens": 1, "cache_read_input_tokens": 400}]}}
        self.assertEqual(D._context_tokens_for_store(iters, 150_000), 401)                       # есть ход — берём его
        no_iter = {"num_turns": 4, "usage": {"input_tokens": 400}}                               # среднее 100 < известного
        self.assertEqual(D._context_tokens_for_store(no_iter, 150_000), 150_000)
        self.assertEqual(D._context_tokens_for_store(no_iter, 0), 100)

    def test_last_falls_back_to_sum_when_num_turns_missing_or_zero(self):
        result = {"usage": {"input_tokens": 100}}
        self.assertEqual(D._context_tokens_last(result), 100)
        result_zero = {"num_turns": 0, "usage": {"input_tokens": 100}}
        self.assertEqual(D._context_tokens_last(result_zero), 100)  # 0 ходов — не делить на ноль

    def test_last_handles_empty_result(self):
        self.assertEqual(D._context_tokens_last({}), 0)

    def test_single_turn_run_sum_equals_last(self):
        """Однократный запуск (как в большинстве фейковых тестов) — сумма и последний ход совпадают."""
        result = {"usage": {"input_tokens": 250_001}}
        self.assertEqual(D._context_tokens_last(result), D._context_tokens_sum(result["usage"]))


class DeckSshTests(unittest.TestCase):
    """v1.1: умолчания ssh на вторую машину (кириллический HOME ломает ~/.ssh по умолчанию)."""

    def setUp(self):
        D._WAIT_CACHE.clear()
        for a in ("CALC", "VPS", "DECK"):  # хосты — только из окружения, умолчаний нет
            os.environ[f"RPV_{a}_HOST"] = f"user@{a.lower()}-test"
            self.addCleanup(lambda a=a: os.environ.pop(f"RPV_{a}_HOST", None))

    def tearDown(self):
        D._WAIT_CACHE.clear()

    def test_repeated_checks_within_cache_window_hit_ssh_once(self):
        """«Можно потом»: без кэша ssh дёргается на каждый ждущий тикет каждые 15 с."""
        calls = []

        class FakeResult:
            returncode = 0

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return FakeResult()

        fake_clock = [1000.0]
        orig_run, orig_time = D.subprocess.run, D.time.time
        D.subprocess.run = fake_run
        D.time.time = lambda: fake_clock[0]
        try:
            self.assertTrue(D._host_wait_met("deck", "path", "~/rpv/queue/STATUS"))
            fake_clock[0] += D.WAIT_CHECK_CACHE_S / 2  # ещё внутри окна кэша
            self.assertTrue(D._host_wait_met("deck", "path", "~/rpv/queue/STATUS"))
            self.assertEqual(len(calls), 1, "второй вызов внутри окна кэша не должен дёргать ssh")
            fake_clock[0] += D.WAIT_CHECK_CACHE_S + 1  # окно истекло
            self.assertTrue(D._host_wait_met("deck", "path", "~/rpv/queue/STATUS"))
            self.assertEqual(len(calls), 2, "после истечения окна кэша — новый вызов")
        finally:
            D.subprocess.run = orig_run
            D.time.time = orig_time

    def test_tilde_path_not_quoted_away(self):
        """Живой прогон 27.09 поймал: shlex.quote('~/x') = "'~/x'" — remote-шелл её не раскрывает."""
        self.assertEqual(D._remote_test_arg("~/rpv/queue/STATUS"), "~/rpv/queue/STATUS")
        self.assertEqual(D._remote_test_arg("~"), "~")

    def test_tilde_nested_job_marker_path(self):
        """CEO 27.09: wait_for: deck: с маркером ~/rpv/queue/done/<id>.job — вложенный путь, фикс
        v1.1 общий для любой глубины после ~/, не только однокомпонентных путей."""
        arg = D._remote_test_arg("~/rpv/queue/done/T-38.job")
        self.assertEqual(arg, "~/rpv/queue/done/T-38.job")  # безопасные символы — без кавычек

    def test_wait_for_deck_job_marker_used_via_check_wait_for(self):
        calls = []

        def fake_host_wait_met(alias, what, arg):
            calls.append((alias, what, arg))
            return True

        orig = D._host_wait_met
        D._host_wait_met = fake_host_wait_met
        try:
            self.assertTrue(D.check_wait_for("host:deck:~/rpv/queue/done/T-38.job"))
        finally:
            D._host_wait_met = orig
        self.assertEqual(calls, [("deck", "path", "~/rpv/queue/done/T-38.job")])

    def test_tilde_path_rest_still_escaped(self):
        import shlex
        raw = "~/rpv/queue/a b;rm -rf /"
        arg = D._remote_test_arg(raw)
        self.assertEqual(arg, "~" + shlex.quote(raw[1:]))
        self.assertTrue(arg.startswith("~'") or arg.startswith("~/"))  # тильда сама не в кавычках

    def test_absolute_path_quoted_as_before(self):
        self.assertIn("'", D._remote_test_arg("/tmp/a b"))

    def test_command_uses_expected_key_host_and_options(self):
        captured = {}

        class FakeResult:
            returncode = 0

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return FakeResult()

        env = {"RPV_DECK_HOST": "deck@test-host", "RPV_DECK_KEY": "/k/id", "RPV_DECK_KNOWN_HOSTS": "/k/known"}
        for var, val in env.items():
            os.environ[var] = val
            self.addCleanup(lambda v=var: os.environ.pop(v, None))
        orig_run = D.subprocess.run
        D.subprocess.run = fake_run
        try:
            ok = D._host_wait_met("deck", "path", "~/rpv/queue/STATUS")
        finally:
            D.subprocess.run = orig_run

        self.assertTrue(ok)
        cmd = captured["cmd"]
        self.assertEqual(cmd[0], "ssh")
        self.assertEqual(cmd[cmd.index("-i") + 1], "/k/id")
        self.assertIn("UserKnownHostsFile=/k/known", cmd)
        self.assertIn("BatchMode=yes", cmd)
        self.assertIn("ConnectTimeout=8", cmd)
        self.assertEqual(cmd[-2], "deck@test-host")
        self.assertTrue(cmd[-1].startswith("test -e "))
        self.assertIn("~/rpv/queue/STATUS", cmd[-1])

    def test_without_deck_host_variable_the_check_is_off_and_ssh_not_called(self):
        for var in ("RPV_DECK_KEY", "RPV_DECK_HOST", "RPV_DECK_KNOWN_HOSTS"):
            os.environ.pop(var, None)
        calls = []
        orig_run = D.subprocess.run
        D.subprocess.run = lambda *a, **kw: calls.append(a)
        try:
            self.assertFalse(D._host_wait_met("deck", "path", "~/rpv/queue/STATUS"))
        finally:
            D.subprocess.run = orig_run
        self.assertEqual(calls, [])


class WaitForHostTests(unittest.TestCase):
    """v4 (04.10): `wait_for: host:<calc|vps|deck>:<путь>` / `host:<…>:unit:<имя>` (deck: — синоним), прогресс-json,
    проверка формы. ssh подменён, сети нет."""

    def setUp(self):
        D._WAIT_CACHE.clear()
        D._WAIT_ERR_LAST.clear()
        self.cmds = []
        self.reply = (0, b"", b"")
        self._orig_run = D.subprocess.run
        D.subprocess.run = self._fake_run
        self._env = {k: os.environ.pop(k, None) for k in
                     ("RPV_CALC_HOST", "RPV_VPS_HOST", "RPV_DECK_HOST", "RPV_DECK_KEY", "RPV_DECK_KNOWN_HOSTS")}
        os.environ.update({"RPV_CALC_HOST": "root@203.0.113.10", "RPV_VPS_HOST": "root@203.0.113.20",
                           "RPV_DECK_HOST": "deck@203.0.113.30",
                           "RPV_DECK_KEY": "/home/user/.ssh/id_rsa",
                           "RPV_DECK_KNOWN_HOSTS": "/home/user/.ssh/known_hosts"})

    def tearDown(self):
        D.subprocess.run = self._orig_run
        D._WAIT_CACHE.clear()
        D._WAIT_ERR_LAST.clear()
        for k, v in self._env.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)

    def _fake_run(self, cmd, **kwargs):
        self.cmds.append(cmd)
        rc, out, err = self.reply

        class R:
            returncode = rc
            stdout = out
            stderr = err
        return R()

    def met(self, spec):
        D._WAIT_CACHE.clear()
        return D.check_wait_for(spec)

    # --- разбор формы ---
    def test_parse_wait_for_forms(self):
        ok = {
            "file:data/x": ("file", "data/x"),
            "ticket:TK-044": ("ticket", "TK-044"),
            "host:deck:~/rpv/q/done": ("host", "deck", "path", "~/rpv/q/done"),
            "host:calc:/var/rpv/progress/tk044.json": ("host", "calc", "path", "/var/rpv/progress/tk044.json"),
            "host:vps:/opt/compute/done": ("host", "vps", "path", "/opt/compute/done"),
            "host:calc:unit:tk044-run3": ("host", "calc", "unit", "tk044-run3"),
            "host:vps:unit:tk044.service": ("host", "vps", "unit", "tk044.service"),
        }
        for spec, want in ok.items():
            self.assertEqual(T.parse_wait_for(spec), want, spec)
        bad = ["", "mention", "ceo — решение владельца", "прогон окон на сервере счёта (…) — готов, когда done=total",
               "file:", "ticket:", "ticket:TK 1", "deck:", "host:calc", "host:calc:", "host:calc:unit:",
               "host:calc:unit:a b", "host:nas:/x", "host:calc/x", "calc:/x",
               "host:calc:file:/data/x.done", "host:calc:data/x", "deck:rel/path", "deck:~/rpv/q/done"]  # TK-117 К1.1: путь только абсолютный
        for spec in bad:
            self.assertIsNone(T.parse_wait_for(spec), spec)

    # --- путь на машине ---
    def test_host_path_exists_uses_alias_host_and_same_ssh_options(self):
        for spec, host in (("host:calc:/var/x/DONE", "root@203.0.113.10"), ("host:vps:/opt/x/DONE", "root@203.0.113.20"),
                           ("host:deck:~/rpv/x", "deck@203.0.113.30")):
            self.cmds.clear()
            self.reply = (0, b"", b"")
            self.assertTrue(self.met(spec), spec)
            cmd = self.cmds[0]
            self.assertEqual(cmd[0], "ssh")
            self.assertEqual(cmd[cmd.index("-i") + 1], r"/home/user/.ssh/id_rsa")
            self.assertIn("UserKnownHostsFile=/home/user/.ssh/known_hosts", cmd)
            self.assertIn("BatchMode=yes", cmd)
            self.assertIn("ConnectTimeout=8", cmd)
            self.assertEqual(cmd[-2], host, spec)
            self.assertTrue(cmd[-1].startswith("test -e "), cmd[-1])

    def test_host_path_missing_is_not_met_and_not_an_error(self):
        self.reply = (1, b"", b"")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertFalse(self.met("host:calc:/var/x/DONE"))
        self.assertEqual(err.getvalue(), "")  # «файла нет» — штатно, не сбой

    def test_host_env_override(self):
        os.environ["RPV_CALC_HOST"] = "me@10.0.0.9"
        self.met("host:calc:/x")
        self.assertEqual(self.cmds[0][-2], "me@10.0.0.9")

    # --- файл хода (.json с done/total) ---
    def test_progress_json_done_vs_total(self):
        cases = [(b'{"done": 3, "total": 10}', False), (b'{"done": 10, "total": 10, "step": "x"}', True),
                 (b'{"done": 11, "total": 10}', True), (b'{"done": "10", "total": "10"}', True),
                 (b'{"done": 0, "total": 0}', False),               # заготовка с нулями — не конец
                 ("{\"ticket\": \"TK-1\", \"note\": \"готово\"}".encode(), True),   # не файл хода — достаточно существования
                 (b"", True)]
        for body, want in cases:
            self.cmds.clear()
            self.reply = (0, body, b"")
            self.assertEqual(self.met("host:calc:/var/rpv/progress/tk044.json"), want, body)
            self.assertTrue(self.cmds[0][-1].startswith("cat /var/rpv/progress/tk044.json"), self.cmds[0][-1])

    def test_progress_json_missing_not_met(self):
        self.reply = (1, b"", b"cat: No such file")
        self.assertFalse(self.met("host:calc:/var/rpv/progress/tk044.json"))

    # --- юнит ---
    def test_unit_met_when_not_active(self):
        for out, want in ((b"active\n", False), (b"activating\n", False), (b"inactive\n", True), (b"failed\n", True),
                          (b"unknown\n", True)):
            self.cmds.clear()
            self.reply = (0 if out.startswith(b"active") else 3, out, b"")
            self.assertEqual(self.met("host:calc:unit:tk044-run3"), want, out)
            self.assertEqual(self.cmds[0][-1], "systemctl is-active tk044-run3")
            self.assertEqual(self.cmds[0][-2], "root@203.0.113.10")

    def test_unit_ssh_error_is_not_met(self):
        self.reply = (255, b"", b"ssh: connect to host ... timed out\n")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertFalse(self.met("host:calc:unit:tk044-run3"))
        self.assertIn("host:calc:unit:tk044-run3", err.getvalue())

    # --- кэш и частота ошибок ---
    def test_cache_60s_per_condition(self):
        clock = [1000.0]
        orig_time = D.time.time
        D.time.time = lambda: clock[0]
        try:
            self.reply = (3, b"inactive\n", b"")
            self.assertTrue(D.check_wait_for("host:calc:unit:u1"))
            clock[0] += 30
            self.assertTrue(D.check_wait_for("host:calc:unit:u1"))
            self.assertEqual(len(self.cmds), 1)
            self.assertTrue(D.check_wait_for("host:calc:unit:u2"))  # другое условие — свой ssh
            self.assertEqual(len(self.cmds), 2)
            clock[0] += 31
            self.assertTrue(D.check_wait_for("host:calc:unit:u1"))
            self.assertEqual(len(self.cmds), 3)
        finally:
            D.time.time = orig_time

    def test_ssh_errors_logged_once_per_10_min_per_condition(self):
        clock = [1000.0]
        orig_time = D.time.time
        D.time.time = lambda: clock[0]
        err = io.StringIO()
        try:
            self.reply = (255, b"", b"Connection timed out")
            with contextlib.redirect_stderr(err):
                for _ in range(5):                       # 5 проверок с интервалом 61 с (~5 мин): строка одна
                    self.assertFalse(D.check_wait_for("host:calc:/var/x/DONE"))
                    clock[0] += 61
                self.assertFalse(D.check_wait_for("host:vps:/var/x/DONE"))  # другое условие — своя строка
                clock[0] += 600
                self.assertFalse(D.check_wait_for("host:calc:/var/x/DONE"))  # прошло > 10 мин — снова
        finally:
            D.time.time = orig_time
        lines = [ln for ln in err.getvalue().splitlines() if ln]
        self.assertEqual(len(lines), 3, lines)
        self.assertEqual(sum("host:calc:/var/x/DONE" in ln for ln in lines), 2)

    def test_ssh_exception_is_not_met_and_logged(self):
        def boom(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, 15)
        D.subprocess.run = boom
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertFalse(self.met("host:calc:unit:u1"))
        self.assertIn("TimeoutExpired", err.getvalue())


class WaitForNoticeTests(unittest.TestCase):
    """v4: `waiting` с непонятным или пустым `wait_for` — строка в ceo-inbox (раз в сутки), не молчание; запись формы."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self._orig = {k: getattr(D, k) for k in ("TICKETS_DIR", "CEO_INBOX", "CEO_WAKE_LOG")}
        D.TICKETS_DIR = self.base / "tickets"
        D.CEO_INBOX = self.base / "ceo-inbox.md"
        D.CEO_WAKE_LOG = self.base / "ceo-wake.log"
        self._orig_tk_dir = TK.TICKETS_DIR
        TK.TICKETS_DIR = D.TICKETS_DIR
        D.RUNNING.clear()
        self.now = dt("2026-10-04T12:00:00+04:00")
        self.state = {}

    def tearDown(self):
        for k, v in self._orig.items():
            setattr(D, k, v)
        TK.TICKETS_DIR = self._orig_tk_dir
        D.RUNNING.clear()
        self.tmp.cleanup()

    def tkt(self, wait_for, status="waiting", updated="2026-10-04T11:00:00+04:00", extra=""):
        text = (f"---\nid: TK-9\nowner: engineer\nstatus: {status}\n{extra}wait_for: {wait_for}\n"
                f"updated: {updated}\n---\n\n## Лог\n")
        return T.parse_text(text, Path("TK-9.md"))

    def inbox(self):
        return D.CEO_INBOX.read_text(encoding="utf-8").splitlines() if D.CEO_INBOX.exists() else []

    def test_unknown_format_reported_once_a_day(self):
        tkt = self.tkt("прогон окон на сервере счёта — готов, когда done=total")
        D.notify_wait_for_problem(tkt, self.state, self.now)
        D.notify_wait_for_problem(tkt, self.state, self.now + timedelta(hours=5))
        lines = self.inbox()
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("TK-9", lines[0])
        self.assertIn("wait_for не понят: прогон окон", lines[0])
        self.assertIn("host:<calc|vps|deck>", lines[0])  # подсказка форм
        D.notify_wait_for_problem(tkt, self.state, self.now + timedelta(hours=25))
        self.assertEqual(len(self.inbox()), 2)           # через сутки — напоминание
        D.notify_wait_for_problem(self.tkt("ещё другой текст"), self.state, self.now + timedelta(hours=26))
        self.assertEqual(len(self.inbox()), 3)           # текст сменился — новая строка сразу

    def test_empty_wait_for_reported_only_after_30_min(self):
        D.notify_wait_for_problem(self.tkt("", updated="2026-10-04T11:45:00+04:00"), self.state, self.now)  # 15 мин
        self.assertEqual(self.inbox(), [])
        D.notify_wait_for_problem(self.tkt("", updated="2026-10-04T11:20:00+04:00"), self.state, self.now)  # 40 мин
        lines = self.inbox()
        self.assertEqual(len(lines), 1)
        self.assertIn("ждёт, но не сказано чего", lines[0])
        D.notify_wait_for_problem(self.tkt("", updated="2026-10-04T11:20:00+04:00"), self.state,
                                  self.now + timedelta(hours=1))
        self.assertEqual(len(self.inbox()), 1)

    def test_no_notice_for_valid_forms_other_statuses_next_or_running(self):
        for spec in ("host:calc:/var/rpv/progress/x.json", "host:vps:unit:u", "host:deck:~/x", "file:x", "ticket:TK-1"):
            D.notify_wait_for_problem(self.tkt(spec), self.state, self.now)
        D.notify_wait_for_problem(self.tkt("мусор", status="in_progress"), self.state, self.now)
        D.notify_wait_for_problem(self.tkt("мусор", extra="next: judge\n"), self.state, self.now)
        D.RUNNING["TK-9"] = {}
        D.notify_wait_for_problem(self.tkt("мусор"), self.state, self.now)
        self.assertEqual(self.inbox(), [])

    def test_empty_waiting_without_updated_is_silent(self):
        text = "---\nid: TK-9\nowner: engineer\nstatus: waiting\nwait_for:\n---\n\n## Лог\n"
        D.notify_wait_for_problem(T.parse_text(text, Path("TK-9.md")), self.state, self.now)
        self.assertEqual(self.inbox(), [])

    def test_unknown_format_never_wakes_owner(self):
        self.assertIsNone(D.decide(self.tkt("мусор"), {}, self.now))

    # --- запись формы ---
    def test_write_header_updates_refuses_unknown_wait_for(self):
        path = T.create_ticket(D.TICKETS_DIR, owner="engineer", title="t")
        with self.assertRaises(ValueError) as cm:
            T.write_header_updates(path, {"status": "waiting", "wait_for": "готов, когда done=total"})
        self.assertIn("host:<calc|vps|deck>", str(cm.exception))
        self.assertEqual(T.read_ticket(path).status, "todo")  # ничего не записано
        T.write_header_updates(path, {"wait_for": "host:calc:/var/rpv/progress/tk044.json"})
        T.write_header_updates(path, {"status": "in_progress", "wait_for": ""})  # снять ожидание можно

    def test_cli_new_refuses_unknown_wait_for_without_creating_file(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = TK.main(["new", "--owner", "engineer", "--title", "t", "--wait-for", "потом посмотрю"])
        self.assertEqual(rc, 1)
        self.assertIn("wait_for не понят", err.getvalue())
        self.assertEqual(list(D.TICKETS_DIR.glob("TK-*.md")) if D.TICKETS_DIR.exists() else [], [])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(TK.main(["new", "--owner", "engineer", "--title", "t", "--wait-for", "host:calc:unit:u"]), 0)

    def test_cli_wait_sets_status_and_checks_form(self):
        path = T.create_ticket(D.TICKETS_DIR, owner="engineer", title="t", status="in_progress")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(TK.main(["wait", path.stem, "host:calc:/var/rpv/progress/tk044.json", "--by", "2099-01-01T00:00+04:00"]), 0)
        tkt = T.read_ticket(path)
        self.assertEqual((tkt.status, tkt.header["wait_for"]), ("waiting", "host:calc:/var/rpv/progress/tk044.json"))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(TK.main(["wait", path.stem, "когда закончится"]), 1)
            self.assertEqual(TK.main(["wait", "TK-777", "file:x"]), 1)
        self.assertIn("host:<calc|vps|deck>", err.getvalue())
        self.assertEqual(T.read_ticket(path).header["wait_for"], "host:calc:/var/rpv/progress/tk044.json")


HOOKS_DIR = Path(__file__).resolve().parent.parent / "hooks"


class RoleMemoryHookTests(unittest.TestCase):
    """v1.1 (судья 27.09, п.4 «обязательно»): current_role() — сначала RPV_ROLE, как в
    role_context.py, иначе (если launch_run не снял CLAUDE_CODE_HOST_SESSION_ID) все роли считаются
    за CEO — тревоги/inbox/«молчание» ломаются на всех."""

    def setUp(self):
        sys.path.insert(0, str(HOOKS_DIR))
        import role_memory as rm
        self.rm = rm
        self._orig_alpha_role = os.environ.get("RPV_ROLE")
        self._orig_host_id = os.environ.get("CLAUDE_CODE_HOST_SESSION_ID")

    def tearDown(self):
        for key, val in (("RPV_ROLE", self._orig_alpha_role), ("CLAUDE_CODE_HOST_SESSION_ID", self._orig_host_id)):
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val

    def test_alpha_role_wins_even_with_ceo_host_session_id_present(self):
        os.environ["RPV_ROLE"] = "judge"
        os.environ["CLAUDE_CODE_HOST_SESSION_ID"] = "local_ceo-host-id-fake"
        title, role = self.rm.current_role()
        self.assertEqual(role, "judge")
        self.assertIn("judge", title)

    def test_no_alpha_role_falls_back_to_host_session_lookup(self):
        os.environ.pop("RPV_ROLE", None)
        os.environ.pop("CLAUDE_CODE_HOST_SESSION_ID", None)
        # без host_id find_title() не находит ничего — (None, None), не падает
        self.assertEqual(self.rm.current_role(), (None, None))

    def test_unknown_alpha_role_value_falls_back(self):
        os.environ["RPV_ROLE"] = "not-a-real-role"
        os.environ.pop("CLAUDE_CODE_HOST_SESSION_ID", None)
        self.assertEqual(self.rm.current_role(), (None, None))


class ZombieAndInstanceLockTests(unittest.TestCase):
    """Аудит-2 п.7: на Linux зомби — мёртв (`/proc/<pid>/status`: State Z); п.5: второй диспетчер/сторож не стартует."""

    def test_zombie_state_counts_as_dead_and_normal_state_as_alive(self):
        orig_kill, orig_state = D.os.kill, D._proc_state
        D.os.kill = lambda pid, sig: None                      # kill -0 «успешен» — как для зомби
        try:
            D._proc_state = lambda pid: "Z"
            self.assertFalse(D._pid_alive_posix(4242, ""))
            D._proc_state = lambda pid: "X"
            self.assertFalse(D._pid_alive_posix(4242, ""))
            D._proc_state = lambda pid: "S"
            self.assertTrue(D._pid_alive_posix(4242, ""))
            D._proc_state = lambda pid: None                    # /proc недоступен — по kill -0
            self.assertTrue(D._pid_alive_posix(4242, ""))
        finally:
            D.os.kill, D._proc_state = orig_kill, orig_state

    def test_without_proc_name_and_state_come_from_ps(self):
        """macOS: нет /proc — имя образа и состояние берутся из ps; чужой процесс с живым pid — не наш."""
        o_kill, o_ps, o_open = D.os.kill, D._ps_field, D.open if hasattr(D, "open") else None
        D.os.kill = lambda pid, sig: None
        D.open = lambda *a, **k: (_ for _ in ()).throw(OSError("нет /proc"))
        try:
            vals = {"stat": "S", "comm": "/usr/bin/vim"}
            D._ps_field = lambda pid, field: vals.get(field)
            self.assertFalse(D._pid_alive_posix(4242, "claude"))
            vals["comm"] = "/opt/homebrew/bin/claude"
            self.assertTrue(D._pid_alive_posix(4242, "claude"))
            vals["stat"] = "Z"
            self.assertFalse(D._pid_alive_posix(4242, "claude"))
            vals.clear()                                          # ps ничего не знает — как раньше, по kill -0
            self.assertTrue(D._pid_alive_posix(4242, "claude"))
        finally:
            D.os.kill, D._ps_field = o_kill, o_ps
            del D.open

    @unittest.skipUnless(sys.platform.startswith("linux"), "зомби и /proc — только Linux")
    def test_real_zombie_child_is_not_alive(self):
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        try:
            deadline = time.time() + 10
            while time.time() < deadline and D._proc_state(child.pid) != "Z":   # завершился, но не реапнут (wait не звали)
                time.sleep(0.05)
            self.assertEqual(D._proc_state(child.pid), "Z")
            self.assertFalse(D._pid_alive(child.pid, expect_name=""))
        finally:
            child.wait(timeout=10)

    def test_first_instance_takes_lock_second_is_refused_with_message(self):
        with tempfile.TemporaryDirectory() as d:
            pid_file = Path(d) / "x.pid"
            holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
            try:
                pid_file.write_text(str(holder.pid), encoding="utf-8")        # «первый экземпляр» — живой чужой процесс
                ok, why = D.acquire_instance_lock(pid_file, expect_name="")
                self.assertFalse(ok)
                self.assertIn(str(holder.pid), why)
                self.assertIn("уже запущен", why)
                self.assertEqual(pid_file.read_text(encoding="utf-8"), str(holder.pid), "чужой замок не тронут")
            finally:
                holder.kill()
                holder.wait(timeout=10)

    def test_stale_or_broken_lock_is_taken_over_and_own_lock_is_ok(self):
        with tempfile.TemporaryDirectory() as d:
            pid_file = Path(d) / "x.pid"
            dead = subprocess.Popen([sys.executable, "-c", "pass"])
            dead.wait(timeout=10)
            pid_file.write_text(str(dead.pid), encoding="utf-8")              # процесса нет — замок осиротел
            self.assertEqual(D.acquire_instance_lock(pid_file, expect_name=""), (True, ""))
            self.assertEqual(pid_file.read_text(encoding="utf-8"), str(os.getpid()))
            self.assertEqual(D.acquire_instance_lock(pid_file, expect_name=""), (True, ""))   # свой pid (запускатель записал)
            pid_file.write_text("мусор", encoding="utf-8")                   # битый файл
            self.assertTrue(D.acquire_instance_lock(pid_file, expect_name="")[0])
            D.release_instance_lock(pid_file)
            self.assertFalse(pid_file.exists(), "свой замок снимается")
            pid_file.write_text(str(os.getpid() + 1), encoding="utf-8")
            D.release_instance_lock(pid_file)
            self.assertTrue(pid_file.exists(), "чужой замок не снимается")

    def test_simultaneous_acquire_exactly_one_wins(self):
        """TK-093: N процессов берут замок в один момент — ровно один (раньше второй видел пустой файл и забирал)."""
        code = chr(10).join([
            "import sys, time",
            "sys.path.insert(0, sys.argv[1])",
            "import dispatch as D",
            "t = float(sys.argv[3])",
            "while time.time() < t: pass",
            "ok, _ = D.acquire_instance_lock(sys.argv[2], expect_name='')",
            "print('OK' if ok else 'NO', flush=True)",
            "time.sleep(30)",
        ])
        here = str(Path(D.__file__).resolve().parent)
        for rnd in range(15):
            with tempfile.TemporaryDirectory() as d:
                pid_file = str(Path(d) / "x.pid")
                t0 = time.time() + 1.0
                procs = [subprocess.Popen([sys.executable, "-c", code, here, pid_file, str(t0)],
                                          stdout=subprocess.PIPE, text=True) for _ in range(4)]
                try:
                    res = [p.stdout.readline().strip() for p in procs]
                finally:
                    for p in procs:
                        p.kill()
                        p.wait(timeout=10)
                self.assertEqual(res.count("OK"), 1, f"раунд {rnd}: {res}")

    def test_main_refuses_second_dispatcher_and_does_not_tick(self):
        with tempfile.TemporaryDirectory() as d:
            orig = (D.PID_FILE, D.TICKETS_DIR, D.tick)
            holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
            ticks = []
            D.PID_FILE, D.TICKETS_DIR = Path(d) / "dispatch.pid", Path(d) / "tickets"
            D.tick = lambda *a, **kw: ticks.append(1) or 0
            D.PID_FILE.write_text(str(holder.pid), encoding="utf-8")
            orig_alive = D._pid_alive
            D._pid_alive = lambda pid, expect_name=None: orig_alive(pid, "")
            try:
                self.assertEqual(D.main(["--once"]), 1)
                self.assertEqual(ticks, [], "второй экземпляр не должен тикать")
            finally:
                D._pid_alive = orig_alive
                D.PID_FILE, D.TICKETS_DIR, D.tick = orig
                holder.kill()
                holder.wait(timeout=10)


class ProjectRootTests(unittest.TestCase):
    """Запуск из папки плагина: корень — аргумент/окружение/текущий каталог, не расположение файла."""

    ENV_KEYS = ("RPV_PROJECT", "CLAUDE_PROJECT_DIR", "RPV_DISPATCH_MAX_PARALLEL", "ALPHA_DISPATCH_MAX_PARALLEL")

    def setUp(self):
        self.saved = {k: os.environ.get(k) for k in self.ENV_KEYS}
        for k in self.ENV_KEYS:
            os.environ.pop(k, None)
        self.tmp = tempfile.TemporaryDirectory()
        self.proj = Path(self.tmp.name).resolve()

    def tearDown(self):
        for k, v in self.saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v
        self.tmp.cleanup()

    def test_resolve_order_arg_then_rpv_then_claude_then_cwd(self):
        a, b, c = (self.proj / n for n in "abc")
        for d in (a, b, c):
            d.mkdir()
        (d := self.proj / "d" / ".claude" / "roles").mkdir(parents=True)
        with self.chdir(d.parent.parent):
            self.assertEqual(P.resolve_project([]), d.parent.parent)       # без флага и окружения — поиск вверх
        os.environ["CLAUDE_PROJECT_DIR"] = str(c)
        self.assertEqual(P.resolve_project([]), c)
        os.environ["RPV_PROJECT"] = str(b)
        self.assertEqual(P.resolve_project([]), b)
        self.assertEqual(P.resolve_project(["--once", "--project", str(a)]), a)
        self.assertEqual(P.resolve_project([f"--project={a}"]), a)
        self.assertEqual(P.strip_project_arg(["--project", str(a), "--once"]), ["--once"])

    def chdir(self, path):
        """Контекст: текущий каталог процесса — `path` (возврат — в конце)."""
        import contextlib

        @contextlib.contextmanager
        def cm():
            prev = os.getcwd()
            os.chdir(path)
            try:
                yield
            finally:
                os.chdir(prev)
        return cm()

    def make_project(self, root):
        (root / ".claude" / "roles").mkdir(parents=True)
        return root

    def run_script(self, script, args, cwd):
        env = {k: v for k, v in os.environ.items() if k not in self.ENV_KEYS}
        return subprocess.run([sys.executable, str(D.CODE_DIR / script), *args], cwd=str(cwd), env=env,
                              capture_output=True, text=True, encoding="utf-8", timeout=120)

    def test_resolve_walks_up_to_nearest_dir_with_claude_roles(self):
        outer = self.make_project(self.proj / "outer")
        inner = self.make_project(outer / "pkg" / "inner")
        deep = inner / "a" / "b"
        deep.mkdir(parents=True)
        with self.chdir(deep):
            self.assertEqual(P.find_project_root(), inner)                  # ближайший, не внешний
            self.assertEqual(P.resolve_project([]), inner)
        with self.chdir(outer / "pkg"):
            self.assertEqual(P.resolve_project([]), outer)

    def test_resolve_outside_project_raises_with_hint_and_creates_nothing(self):
        empty = self.proj / "plain" / "sub"
        empty.mkdir(parents=True)
        with self.chdir(empty):
            with self.assertRaises(P.ProjectNotFound) as cm:
                P.resolve_project([])
        self.assertIn("--project", str(cm.exception))
        self.assertIn("/rpv-init", str(cm.exception))
        self.assertEqual([p.name for p in self.proj.rglob(".claude")], [])

    def test_cli_hints_use_sys_executable(self):  # TK-110 В-1
        with mock.patch("shutil.which", return_value="/usr/bin/python"):
            self.assertTrue(D.tickets_cli().startswith("python ") and D.plan_cli().startswith("python "))
        with mock.patch("shutil.which", return_value=None):
            for cli in (D.tickets_cli(), D.plan_cli()):
                self.assertTrue(cli.startswith(shlex.quote(Path(sys.executable).as_posix()) + " "), cli)

    def test_tickets_cli_from_subfolder_creates_ticket_in_project_root(self):
        proj = self.make_project(self.proj / "work")
        sub = proj / "src" / "deep"
        sub.mkdir(parents=True)
        done = self.run_script("tickets.py", ["new", "--owner", "engineer", "--title", "Из подпапки"], sub)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual([p.name for p in (proj / ".claude" / "tickets").glob("TK-*.md")], ["TK-001.md"])
        self.assertFalse((sub / ".claude").exists())

    def test_scripts_outside_project_fail_with_hint_and_create_no_directories(self):
        empty = self.proj / "nowhere"
        empty.mkdir()
        for script, args in (("tickets.py", ["new", "--owner", "engineer", "--title", "x"]),
                             ("dispatch.py", ["--once"]), ("watch.py", ["--once"])):
            done = self.run_script(script, args, empty)
            self.assertEqual(done.returncode, 2, (script, done.stdout, done.stderr))
            self.assertIn("--project", done.stderr, script)
            self.assertIn("/rpv-init", done.stderr, script)
        self.assertEqual(list(empty.iterdir()), [])

    def test_env_reads_rpv_only_else_default(self):
        self.assertEqual(P.env("DISPATCH_MAX_PARALLEL", "3"), "3")
        os.environ["ALPHA_DISPATCH_MAX_PARALLEL"] = "5"
        self.assertEqual(P.env("DISPATCH_MAX_PARALLEL", "3"), "3")
        os.environ["RPV_DISPATCH_MAX_PARALLEL"] = "7"
        self.assertEqual(P.env("DISPATCH_MAX_PARALLEL", "3"), "7")

    def test_configure_project_moves_all_state_paths_into_project(self):
        import watch as W
        orig = D.PROJECT_ROOT
        self.addCleanup(lambda: W.configure_project(orig))
        W.configure_project(self.proj)
        state = self.proj / ".claude" / "dispatcher"
        self.assertEqual(D.TICKETS_DIR, self.proj / ".claude" / "tickets")
        for path in (D.STATE_FILE, D.PID_FILE, D.RUNS_DIR, D.RUNS_LOG, D.CEO_INBOX, D.CEO_WAKE_LOG,
                     W.WATCH_HEARTBEAT_FILE, W.WATCH_PID_FILE, W.WATCH_STATE_FILE):
            self.assertEqual(path.parent, state, path)
        self.assertNotEqual(D.CODE_DIR, state)  # код диспетчера и состояние проекта — разные каталоги

    def test_tickets_cli_project_flag_writes_into_that_project(self):
        orig = (TK.TICKETS_DIR, TK.PROJECT_ROOT, D.PROJECT_ROOT)
        self.addCleanup(lambda: (D.configure_project(orig[2]), setattr(TK, "TICKETS_DIR", orig[0]),
                                 setattr(TK, "PROJECT_ROOT", orig[1])))
        self.assertEqual(TK.main(["--project", str(self.proj), "new", "--owner", "engineer", "--title", "Проект"]), 0)
        self.assertEqual([p.name for p in (self.proj / ".claude" / "tickets").glob("TK-*.md")], ["TK-001.md"])

    def test_scripts_run_from_plugin_folder_keep_state_in_project(self):
        """dispatch.py / watch.py --once с чужим cwd: состояние появляется в проекте, не рядом с кодом."""
        env = {k: v for k, v in os.environ.items() if k not in self.ENV_KEYS and k != "CLAUDE_PROJECT_DIR"}
        with tempfile.TemporaryDirectory() as other_cwd:
            for script, expect in (("dispatch.py", "state.json"), ("watch.py", "watch-heartbeat.json")):
                done = subprocess.run([sys.executable, str(D.CODE_DIR / script), "--project", str(self.proj), "--once"],
                                      cwd=other_cwd, env=env, capture_output=True, text=True, timeout=120)
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertTrue((self.proj / ".claude" / "dispatcher" / expect).exists(), script)
        self.assertTrue((self.proj / ".claude" / "tickets").is_dir())
class WaitByEventTest(unittest.TestCase):
    """TK-055: wait_for host:… закрывается событием шины; в асинхронном режиме основной поток ssh не зовёт."""

    def setUp(self):
        D._EVENT_MET.clear(); D._WAIT_CACHE.clear(); D._WAIT_WATCH.clear(); D._UNIT_START.clear(); D._EVENT_VERIFIED.clear()
        self._run, self._async, self._sshcmd = D.subprocess.run, D.WAIT_ASYNC, D._ssh_cmd

        def boom(*a, **k):
            raise AssertionError("ssh в основном потоке")
        D.subprocess.run = boom

    def tearDown(self):
        D.subprocess.run, D.WAIT_ASYNC, D._ssh_cmd = self._run, self._async, self._sshcmd
        D._EVENT_MET.clear(); D._WAIT_CACHE.clear(); D._WAIT_WATCH.clear(); D._UNIT_START.clear(); D._EVENT_VERIFIED.clear()

    def _probe_state(self, state: bytes):
        class R:
            returncode, stdout, stderr = 0, state, b""
        D.subprocess.run = lambda *a, **k: R()
        D._ssh_cmd = lambda alias, cmd: ["ssh", alias]

    def test_unit_stopped_event(self):
        """Событие «остановлен» засчитывается только после ssh-подтверждения (поллером) «не active»."""
        D.WAIT_ASYNC = True
        self.assertFalse(D.check_wait_for("host:calc:unit:tk1-a"))
        D.record_wait_event({"addr": "машина.calc.юнит.остановлен", "payload": {"unit": "tk1-a.service", "host": "calc"}})
        self.assertFalse(D.check_wait_for("host:calc:unit:tk1-a"))  # подтверждения ещё нет
        self.assertIn(("calc", "unit", "tk1-a"), D._WAIT_WATCH)
        self._probe_state(b"inactive")
        D._host_probe("calc", "unit", "tk1-a")  # тело поллера
        self.assertTrue(D.check_wait_for("host:calc:unit:tk1-a"))
        self.assertFalse(D.check_wait_for("host:vps:unit:tk1-a"))

    def test_stale_unit_event_before_wait_is_not_met(self):
        """TK-070 (БАГ 84 побудок): событие от прежнего экземпляра пришло ДО постановки wait_for; юнит снова работает."""
        D.WAIT_ASYNC = True
        D.record_wait_event({"addr": "машина.calc.юнит.остановлен", "payload": {"unit": "tk065-gate2.service", "host": "calc"}})
        for _ in range(5):
            self.assertFalse(D.check_wait_for("host:calc:unit:tk065-gate2"))
        self._probe_state(b"active")
        D._host_probe("calc", "unit", "tk065-gate2")  # поллер увидел работающий юнит → событие сброшено
        for _ in range(5):
            self.assertFalse(D.check_wait_for("host:calc:unit:tk065-gate2"))
        self.assertIsNone(D._event_ts("calc", "unit", "tk065-gate2"))
        D.record_wait_event({"addr": "машина.calc.юнит.остановлен", "payload": {"unit": "tk065-gate2.service", "host": "calc"}})
        self._probe_state(b"inactive")
        D._host_probe("calc", "unit", "tk065-gate2")
        self.assertTrue(D.check_wait_for("host:calc:unit:tk065-gate2"))

    def test_unit_stop_with_matching_invocation_needs_no_ssh(self):
        """TK-072: «запущен» (inv A) + «остановлен» (inv A) — wait_for выполнен без ssh; остановка старого запуска (inv A) после
        нового запуска (inv B) — игнорируется."""
        D.WAIT_ASYNC = True
        def boom(*a, **k):
            raise AssertionError("ssh не нужен")
        D.subprocess.run = boom
        ev = lambda kind, inv: D.record_wait_event({"addr": f"машина.calc.юнит.{kind}", "payload": {"unit": "tk9-x.service", "host": "calc", "invocation": inv}})
        ev("запущен", "A")
        self.assertFalse(D.check_wait_for("host:calc:unit:tk9-x"))
        ev("остановлен", "A")
        self.assertTrue(D.check_wait_for("host:calc:unit:tk9-x"))
        ev("запущен", "B")  # второй запуск с тем же именем
        self.assertFalse(D.check_wait_for("host:calc:unit:tk9-x"))
        ev("остановлен", "A")  # запоздавшее событие старого запуска
        self.assertFalse(D.check_wait_for("host:calc:unit:tk9-x"))
        ev("остановлен", "B")
        self.assertTrue(D.check_wait_for("host:calc:unit:tk9-x"))

    def test_stale_unit_event_sync_mode(self):
        D.WAIT_ASYNC = False
        D.record_wait_event({"addr": "машина.calc.юнит.остановлен", "payload": {"unit": "u1.service", "host": "calc"}})
        self._probe_state(b"active")
        self.assertFalse(D.check_wait_for("host:calc:unit:u1"))
        self.assertFalse(D.check_wait_for("host:calc:unit:u1"))


    def test_job_done_and_file_events(self):
        D.WAIT_ASYNC = True
        D.record_wait_event({"addr": "задача.TK-1.задание.готово", "payload": {"job": "j1", "host": "calc"}})
        self.assertTrue(D.check_wait_for(f"host:calc:{D.PROGRESS_DIR}/j1.json"))
        D.record_wait_event({"addr": "машина.vps.файл.появился", "payload": {"path": "/x/DONE", "host": "vps"}})
        self.assertTrue(D.check_wait_for("host:vps:/x/DONE"))
        self.assertFalse(D.check_wait_for("host:calc:/x/DONE"))


class TokenAccountingTest(unittest.TestCase):
    """TK-055: токены запуска без JSON (таймаут) — из транскрипта сессии; сводка usage.py читает и старые строки."""

    def test_transcript_usage_since_dedups_by_message_id(self):
        from datetime import datetime, timezone
        d = Path(tempfile.mkdtemp()) / "proj"
        d.mkdir()
        rows = [
            {"timestamp": "2026-10-05T10:00:00Z", "message": {"id": "old", "usage": {"input_tokens": 99}}},
            {"timestamp": "2026-10-05T12:00:01Z", "message": {"id": "m1", "usage": {"input_tokens": 1, "output_tokens": 5}}},
            {"timestamp": "2026-10-05T12:00:02Z", "message": {"id": "m1", "usage": {
                "input_tokens": 1, "cache_read_input_tokens": 700, "cache_creation_input_tokens": 30, "output_tokens": 9}}},
            {"timestamp": "2026-10-05T12:01:00Z", "message": {"id": "m2", "usage": {"input_tokens": 2, "output_tokens": 1}}},
        ]
        (d / "sess-1.jsonl").write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
        orig = D.CLAUDE_PROJECTS_DIR
        D.CLAUDE_PROJECTS_DIR = d.parent
        try:
            u = D._transcript_usage_since("sess-1", datetime(2026, 10, 5, 12, 0, 0, tzinfo=timezone.utc))
            self.assertEqual(u, {"input_tokens": 3, "cache_read_input_tokens": 700,
                                 "cache_creation_input_tokens": 30, "output_tokens": 10})
            self.assertEqual(D._transcript_usage_since("nope", "2026-10-05T12:00:00+00:00"), {})
        finally:
            D.CLAUDE_PROJECTS_DIR = orig

    def test_usage_parse_old_and_new_lines(self):
        import usage as U
        old = U.parse("2026-10-05T23:10:30+04:00 TK-1 engineer reason=todo in_tok=16 out_tok=100 ctx_sum=1016 status=ok")
        self.assertEqual((old["in"], old["cache_all"], old["cr"]), (16, 1000, None))
        new = U.parse("2026-10-05T23:10:30+04:00 TK-1 engineer reason=todo in_tok=1 out_tok=2 cr_tok=30 cw_tok=4 status=timeout")
        self.assertEqual((new["cr"], new["cw"], new["cache_all"], new["status"]), (30, 4, 34, "timeout"))


class OnMetTests(unittest.TestCase):
    """TK-056 п.2: on_met — команда вместо пробуждения LLM (семантика — запись Судьи 05.10 23:34)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "tools").mkdir()
        self.tickets = self.root / "tickets"
        self.tickets.mkdir()
        self.orig = (D.PROJECT_ROOT, D.RUNS_LOG, D._git_tracked, D.ON_MET_TIMEOUT_S)
        D.PROJECT_ROOT, D.RUNS_LOG = self.root, self.root / "runs.log"
        D._git_tracked = lambda rel: (self.root / rel).exists()
        disp = str(Path(D.__file__).resolve().parent).replace("\\", "/")
        (self.root / "tools" / "hdr.py").write_text(
            f"import sys, json; sys.path.insert(0, {disp!r}); import ticket as T\n"
            "print('hdr done'); T.write_header_updates(sys.argv[1], json.loads(sys.argv[2]))\n", encoding="utf-8")
        (self.root / "tools" / "fail.py").write_text("import sys; sys.stderr.write('boom'); sys.exit(3)\n", encoding="utf-8")
        (self.root / "tools" / "sleep.py").write_text("import time; time.sleep(30)\n", encoding="utf-8")
        self.state = {}
        self.now = dt("2026-10-05T12:00:00+04:00")

    def tearDown(self):
        D.PROJECT_ROOT, D.RUNS_LOG, D._git_tracked, D.ON_MET_TIMEOUT_S = self.orig
        self.tmp.cleanup()

    def ticket(self, on_met, wait_for="file:/x/y"):
        p = T.create_ticket(self.tickets, owner="engineer", title="Ждёт", status="todo", now=self.now)
        T.write_header_updates(p, {"status": "waiting", "wait_for": wait_for, "on_met": on_met}, now=self.now)
        return p

    def run_met(self, p):
        return D.run_on_met(p, T.read_ticket(p), self.state, self.now)

    def hdr(self, p, upd):
        return f"python tools/hdr.py {str(p).replace(chr(92), '/')} '{json.dumps(upd)}'"

    def test_ok_with_new_wait_for_stays_silent(self):
        p = self.ticket("")
        T.write_header_updates(p, {"on_met": self.hdr(p, {"wait_for": "file:/x/next"})})
        self.assertTrue(self.run_met(p))
        t = T.read_ticket(p)
        self.assertEqual((t.status, t.header["wait_for"], t.header["on_met"]), ("waiting", "file:/x/next", ""))
        self.assertIn("hdr done", t.log[-1].text)
        self.assertIn(" on_met reason=on_met", D.RUNS_LOG.read_text(encoding="utf-8"))

    def test_ok_without_new_wait_for_wakes_owner(self):
        p = self.ticket("")
        T.write_header_updates(p, {"on_met": self.hdr(p, {"updated": "x"})})
        self.assertTrue(self.run_met(p))
        t = T.read_ticket(p)
        self.assertEqual((t.status, t.header["wait_for"]), ("in_progress", ""))

    def test_nonzero_logs_stderr_and_falls_back(self):
        p = self.ticket("python tools/fail.py")
        self.assertFalse(self.run_met(p))
        t = T.read_ticket(p)
        self.assertEqual((t.status, t.header["on_met"]), ("waiting", ""))
        self.assertIn("boom", t.log[-1].text)
        self.assertIn("код 3", t.log[-1].text)

    def test_timeout_kills_tree(self):
        D.ON_MET_TIMEOUT_S = 1.0
        p = self.ticket("python tools/sleep.py")
        t0 = time.time()
        self.assertFalse(self.run_met(p))
        self.assertLess(time.time() - t0, 20)
        self.assertIn("таймаут", T.read_ticket(p).log[-1].text)
        self.assertIn("status=timeout", D.RUNS_LOG.read_text(encoding="utf-8"))

    def test_command_cannot_close_ticket(self):
        for upd in ({"status": "in_review"}, {"status": "done"}, {"next": "judge"}):
            p = self.ticket("")
            T.write_header_updates(p, {"on_met": self.hdr(p, upd)})
            self.assertTrue(self.run_met(p))
            t = T.read_ticket(p)
            self.assertEqual((t.status, t.next_role), ("in_progress", ""), upd)

    def test_fourth_in_a_row_wakes_owner(self):
        p = self.ticket("")
        tid = T.read_ticket(p).id
        for i in range(3):
            T.write_header_updates(p, {"status": "waiting", "wait_for": "file:/x/a", "on_met": self.hdr(p, {"wait_for": f"file:/x/n{i}"})})
            self.assertTrue(self.run_met(p))
        T.write_header_updates(p, {"status": "waiting", "wait_for": "file:/x/a", "on_met": self.hdr(p, {"wait_for": "file:/x/z"})})
        self.assertFalse(self.run_met(p))
        t = T.read_ticket(p)
        self.assertEqual(t.header["wait_for"], "file:/x/a")
        self.assertIn("подряд", t.log[-1].text)
        self.assertNotIn(tid, self.state["on_met_chain"])

    def test_untracked_or_foreign_script_refused(self):
        for spec in ("python tools/nope.py", "bash -c 'rm -rf /'", "python src/main.py", "python tools\\hdr.py x",
                     "python ../evil.py", "cargo build", "python tools/hdr.py 'unterminated"):
            p = self.ticket(spec)
            self.assertFalse(self.run_met(p), spec)
            t = T.read_ticket(p)
            self.assertEqual(t.header["on_met"], "", spec)
            self.assertIn("on_met отклонён", t.log[-1].text, spec)

    def test_cleared_before_run_no_repeat_after_crash(self):
        p = self.ticket("python tools/hdr.py")
        orig = D.subprocess.Popen
        D.subprocess.Popen = lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt())
        try:
            with self.assertRaises(KeyboardInterrupt):
                self.run_met(p)
        finally:
            D.subprocess.Popen = orig
        self.assertEqual(T.read_ticket(p).header["on_met"], "")


if __name__ == "__main__":
    unittest.main()


class LimitAndWaitingTK070Test(unittest.TestCase):
    """TK-070: пауза по лимиту сессии (429), ожидание без условия — отказ."""

    def test_limit_detect_and_reset_parse(self):
        from datetime import datetime, timezone, timedelta
        r = {"api_error_status": 429, "result": "You've hit your session limit · resets 5am (Asia/Tbilisi)"}
        self.assertTrue(D._limit_hit(r))
        self.assertFalse(D._limit_hit({"result": "ok"}))
        now = datetime(2026, 10, 6, 4, 46, tzinfo=timezone(timedelta(hours=4)))
        at = D._limit_reset_at(r, now)
        self.assertEqual((at.hour, at.minute, at.day), (5, 1, 6))
        late = datetime(2026, 10, 6, 6, 0, tzinfo=timezone(timedelta(hours=4)))
        self.assertEqual(D._limit_reset_at(r, late).day, 7)
        self.assertEqual(D._limit_reset_at({"result": "?"}, now), now + timedelta(hours=1))

    def test_limit_reset_reference_is_run_end(self):
        from datetime import datetime, timezone, timedelta
        tz = timezone(timedelta(hours=4))
        r = {"api_error_status": 429, "result": "You've hit your session limit · resets 1:33pm"}
        # метка = минута ответа, разбор на тик позже: метка уже прошла — пауза ≤ 1 мин (запас), не сутки
        ref = datetime(2026, 10, 6, 13, 33, 0, 500000, tzinfo=tz)
        now = datetime(2026, 10, 6, 13, 33, 6, tzinfo=tz)
        self.assertLessEqual(D._limit_reset_at(r, now, ref), now + timedelta(minutes=1))
        # метка на минуту впереди ответа, разбор уже после метки (сирота): паузы нет
        ref = datetime(2026, 10, 6, 13, 32, 30, tzinfo=tz)
        late = datetime(2026, 10, 6, 13, 40, tzinfo=tz)
        self.assertLessEqual(D._limit_reset_at(r, late, ref), late)
        # сирота, разобранная через 10 мин после resetsAt при ответе за час до метки
        ref = datetime(2026, 10, 6, 12, 30, tzinfo=tz)
        late = datetime(2026, 10, 6, 13, 43, tzinfo=tz)
        self.assertLessEqual(D._limit_reset_at(r, late, ref), late)
        # метка раньше ответа на часы — завтра
        ref = datetime(2026, 10, 6, 22, 0, tzinfo=tz)
        r5 = {"result": "resets 5am"}
        self.assertEqual(D._limit_reset_at(r5, ref, ref).day, 7)

    def test_limit_pause_flag(self):
        from datetime import datetime, timezone, timedelta
        now = datetime(2026, 10, 6, 4, 46, tzinfo=timezone(timedelta(hours=4)))
        st = {"limit_pause_until": T.now_iso(now + timedelta(minutes=10))}
        self.assertTrue(D._limit_paused(st, now))
        self.assertFalse(D._limit_paused(st, now + timedelta(minutes=11)))
        self.assertFalse(D._limit_paused({}, now))


class WaitReconcileTest(unittest.TestCase):
    """Сверка wait_for: один ssh на машину, регистрация пути у сторожа, тревога «пропуск»."""

    def setUp(self):
        D._EVENT_MET.clear(); D._WAIT_CACHE.clear(); D._WAIT_WATCH.clear(); D._UNIT_START.clear(); D._EVENT_VERIFIED.clear()
        D._RECON_MISS.clear(); D._WL_REG.clear()
        self._run, self._async, self._sshcmd = D.subprocess.run, D.WAIT_ASYNC, D._ssh_cmd
        self._alarm, self.alarms = D.append_ceo_inbox, []
        self._link = D._LINK
        D._LINK = types.SimpleNamespace(down_since=None)  # шина жива: пропуск события сверкой виден
        D.append_ceo_inbox = lambda *a, **k: self.alarms.append(a)
        D._ssh_cmd = lambda alias, cmd: ["ssh", alias, cmd]
        D.WAIT_ASYNC = True

        def boom(*a, **k):
            raise AssertionError("ssh вне теста")
        D.subprocess.run = boom

    def tearDown(self):
        D.subprocess.run, D.WAIT_ASYNC, D._ssh_cmd = self._run, self._async, self._sshcmd
        D.append_ceo_inbox, D._LINK = self._alarm, self._link
        D._EVENT_MET.clear(); D._WAIT_CACHE.clear(); D._WAIT_WATCH.clear(); D._UNIT_START.clear(); D._EVENT_VERIFIED.clear()
        D._RECON_MISS.clear(); D._WL_REG.clear()

    def _ssh(self, out: bytes, rc: int = 0):
        calls = []

        class R:
            returncode, stderr, stdout = rc, b"", out
        D.subprocess.run = lambda cmd, **k: (calls.append(cmd[-1]), R())[1]
        return calls

    def test_json_outside_progress_is_registered_with_watcher(self):
        calls = self._ssh(b"")
        D._host_probe("calc", "path", "/data/work/drill.json")
        D._host_probe("calc", "path", "/data/progress/j1.json")
        self.assertIn(D.WATCH_LIST, calls[0])
        self.assertIn("cat ", calls[0])
        self.assertIn(D.WATCH_LIST, calls[1])

    def test_progress_json_in_default_dir_is_not_registered(self):
        """Каталог хода по умолчанию плагина (~/rpv/progress): файл хода не попадает в watch.list — иначе «файл появился» закрыл бы ожидание на старте."""
        self.assertTrue(D.PROGRESS_DIR.startswith("~/"))
        calls = self._ssh(b"")
        D._host_probe("calc", "path", "/home/u/rpv/progress/j1.json")
        D._host_probe("calc", "path", "/home/u/rpv/other/j1.json")
        self.assertNotIn(D.WATCH_LIST, calls[0])
        self.assertIn(D.WATCH_LIST, calls[1])
        self.assertTrue(D._is_progress_json("/home/u/rpv/progress/j1.json"))
        self.assertFalse(D._is_progress_json("/home/u/rpv/progress/j1.done"))

    def test_progress_json_in_absolute_dir_is_not_registered(self):
        old = D.PROGRESS_DIR
        D.PROGRESS_DIR = "/data/progress"
        try:
            self.assertTrue(D._is_progress_json("/data/progress/j1.json"))
            self.assertFalse(D._is_progress_json("/data/work/j1.json"))
        finally:
            D.PROGRESS_DIR = old

    def test_probe_wl_marker_confirms_registration(self):
        self._ssh(b"@@WL\n")
        self.assertTrue(D._host_probe("calc", "path", "/data/a.done"))
        self.assertIn(("calc", "/data/a.done"), D._WL_REG)

    def test_reconcile_one_ssh_per_machine_closes_missed_event(self):
        """Первая проверка сорвана таймаутом, регистрация не дошла, события нет — сверка одним ssh закрывает все ждущие."""
        def timeout(*a, **k):
            raise D.subprocess.TimeoutExpired("ssh", 15)
        D.subprocess.run = timeout
        D._host_probe("calc", "path", "/data/w/chain.done")
        self.assertNotIn(("calc", "/data/w/chain.done"), D._WL_REG)
        for k in (("calc", "path", "/data/w/chain.done"), ("calc", "unit", "u1"), ("calc", "path", "/data/p/x.json")):
            D._WAIT_WATCH.add(k)
        calls = self._ssh(b"@@0\n@@reg\n{\"done\": 1, \"total\": 5}\n@@rc 0\n@@1\n@@reg\n@@rc 0\n@@2\ninactive\n@@rc 0\n")
        D._reconcile()
        self.assertEqual(len(calls), 1)
        self.assertIn(D.WATCH_LIST, calls[0])
        self.assertTrue(D._WAIT_CACHE[("calc", "path", "/data/w/chain.done")][1])
        self.assertTrue(D._WAIT_CACHE[("calc", "unit", "u1")][1])
        self.assertFalse(D._WAIT_CACHE[("calc", "path", "/data/p/x.json")][1])
        self.assertIn(("calc", "/data/w/chain.done"), D._WL_REG)
        self.assertTrue(D.check_wait_for("host:calc:/data/w/chain.done"))

    def test_reconcile_miss_alarm_once_per_key_and_none_with_event(self):
        logged = []
        orig, D._log_ssh_call = D._log_ssh_call, lambda *a: logged.append(a)
        self._ssh(b"@@0\n@@reg\n@@rc 0\n@@1\n@@reg\n@@rc 0\n")
        try:
            for k in (("calc", "path", "/data/a.done"), ("calc", "path", "/data/b.done")):
                D._WAIT_WATCH.add(k)
            D.record_wait_event({"addr": "машина.calc.файл.появился", "payload": {"path": "/data/b.done", "host": "calc"}})
            D._reconcile()
            D._reconcile()
        finally:
            D._log_ssh_call = orig
        self.assertEqual([a for a in logged if a[3] == "пропуск"], [("calc", "path", "/data/a.done", "пропуск")])
        self.assertEqual(len(self.alarms), 1)
        self.assertEqual(self.alarms[0][1], "recon-miss")
        self.assertEqual(D.signal_prio("recon-miss"), "urgent")

    def test_reconcile_unwatched_machine_no_miss_alarm(self):
        D._WAIT_WATCH.add(("vps", "path", "/opt/x.done"))
        self._ssh(b"@@0\n@@rc 0\n")
        D._reconcile()
        self.assertEqual(self.alarms, [])

    def test_recon_script_keeps_tilde_unquoted(self):
        script = D._recon_script([("calc", "path", "~/x.done"), ("calc", "path", "~/rpv/progress/a.json")])
        self.assertIn("test -e ~/x.done", script)
        self.assertIn("cat ~/rpv/progress/a.json", script)
        self.assertNotIn("'~/", script)

    def test_reconcile_missing_file_stays_unmet(self):
        D._WAIT_WATCH.add(("calc", "path", "/data/a.done"))
        self._ssh(b"@@0\n@@reg\n@@rc 1\n")
        D._reconcile()
        self.assertFalse(D._WAIT_CACHE[("calc", "path", "/data/a.done")][1])
        self.assertIn(("calc", "/data/a.done"), D._WL_REG)

    def test_reconcile_ssh_failure_changes_nothing(self):
        D._WAIT_WATCH.add(("calc", "path", "/data/a.done"))
        self._ssh(b"", rc=255)
        D._reconcile()
        self.assertNotIn(("calc", "path", "/data/a.done"), D._WAIT_CACHE)

    def test_ssh_calls_are_logged_with_reason(self):
        import tempfile as _tf
        old = D.DISPATCHER_DIR
        D.DISPATCHER_DIR = Path(_tf.mkdtemp())
        try:
            self._ssh(b"")
            D._host_probe("calc", "path", "/data/a.done")
            line = (D.DISPATCHER_DIR / D.SSH_CALLS_LOG_NAME).read_text(encoding="utf-8").splitlines()[0].split("\t")
            self.assertEqual(line[1:], ["calc", "path", "/data/a.done", "первая"])
        finally:
            D.DISPATCHER_DIR = old

    def test_needs_probe_only_first(self):
        key = ("calc", "path", "/data/a.done")
        self.assertTrue(D._needs_probe(key))  # первая проверка нового условия
        D._WAIT_CACHE[key] = (time.time(), False)
        self.assertFalse(D._needs_probe(key))  # дальше — события и сверка, не ssh
