"""Проверки `role_context.py` (SessionStart) и `role_memory.py` (UserPromptSubmit/SessionEnd).

Запуск из `.claude/hooks`: `python test_role_hooks.py`. Рабочее состояние (`.state`, `log`, ceo-inbox) не трогается:
каталоги подменяются окружением до импорта.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
# корень проекта для хуков — каталог плагина/проекта над `.claude` (в нём есть .claude/roles); без этого хуки молчат
os.environ["CLAUDE_PROJECT_DIR"] = os.path.dirname(os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="hooks-test-")
os.environ["ALPHA_STATE_DIR"] = os.path.join(TMP, "state")
os.environ["ALPHA_LOG_DIR"] = os.path.join(TMP, "log")
os.environ["ALPHA_DISPATCHER_DIR"] = os.path.join(TMP, "disp")
os.makedirs(os.environ["ALPHA_DISPATCHER_DIR"], exist_ok=True)
for var in ("ALPHA_ROLE", "ALPHA_TICKET", "ALPHA_TICKET_ID", "CLAUDE_CODE_HOST_SESSION_ID"):
    os.environ.pop(var, None)
sys.path.insert(0, HERE)
import role_context as rc  # noqa: E402
import role_memory as rm  # noqa: E402


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)   # свой временный каталог внутри проекта


def run_hook(script, stdin, env_extra):
    env = dict(os.environ)
    env.pop("CLAUDE_CODE_HOST_SESSION_ID", None)
    env.update(env_extra)
    r = subprocess.run([sys.executable, os.path.join(HERE, script)], input=json.dumps(stdin).encode("utf-8"),
                       capture_output=True, env=env)
    return r


def start_text(role, source, ticket=None):
    env = {"ALPHA_ROLE": role}
    if ticket:
        env["ALPHA_TICKET"] = ticket
    r = run_hook("role_context.py", {"hook_event_name": "SessionStart", "source": source, "session_id": "t-1"}, env)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.decode("utf-8"))["hookSpecificOutput"]["additionalContext"], r.stdout


class SessionStart(unittest.TestCase):
    def test_startup_and_clear_within_limit_every_role(self):
        for role in ("researcher", "engineer", "judge", "ceo"):
            for source in ("startup", "clear"):
                text, raw = start_text(role, source)
                self.assertLessEqual(len(text), rc.LIMIT, (role, source))
                self.assertLessEqual(len(raw.decode("utf-8")), rc.LIMIT + 300, (role, source))  # вывод хука ≈ тексту
                self.assertIn(f".claude/roles/{role}.md", text)
                self.assertIn("tickets.py comment", text)

    def test_resume_and_compact_are_short(self):
        for role in ("researcher", "judge"):
            for source in ("resume", "compact"):
                text, _ = start_text(role, source, ticket="TK-025")
                self.assertLess(len(text), 700, (role, source))
                self.assertLessEqual(text.count("\n"), 1)
                self.assertIn(f".claude/roles/{role}.md", text)

    def test_no_obsolete_instructions(self):
        for role in ("researcher", "ceo"):
            text, _ = start_text(role, "startup")
            head = text.split("\n--- ")[0]   # текст самого хука; устав и блокнот — чужие файлы
            for bad in ("SendMessage", "get_session", "TASKS.md", "простаивать", "journal/", "clear_session"):
                self.assertNotIn(bad, head, (role, bad))

    def test_undetermined_role_is_short_and_has_no_get_session(self):
        r = run_hook("role_context.py", {"source": "startup"}, {})
        text = json.loads(r.stdout.decode("utf-8"))["hookSpecificOutput"]["additionalContext"]
        self.assertLess(len(text), 800)
        self.assertNotIn("get_session", text)

    def test_bad_stdin_does_not_crash(self):
        env = dict(os.environ, ALPHA_ROLE="judge")
        r = subprocess.run([sys.executable, os.path.join(HERE, "role_context.py")], input=b"not json", env=env,
                           capture_output=True)
        self.assertEqual(r.returncode, 0)
        json.loads(r.stdout.decode("utf-8"))


class Fit(unittest.TestCase):
    def setUp(self):
        self.roles = os.path.join(TMP, "roles")
        os.makedirs(os.path.join(self.roles, "notes"), exist_ok=True)
        self._saved = rc.ROLES_DIR
        rc.ROLES_DIR = self.roles
        self.addCleanup(lambda: setattr(rc, "ROLES_DIR", self._saved))

    def write(self, rel, text):
        with open(os.path.join(self.roles, rel), "w", encoding="utf-8") as fh:
            fh.write(text)

    def test_huge_charter_replaced_by_path_and_total_within_limit(self):
        self.write("judge.md", "# Судья\n" + "правило " * 5000)
        self.write("notes/judge.md", "# Блокнот\n" + "заметка\n" * 100)
        text = rc.full_context("judge", "Роль: Судья", "startup", {})
        self.assertLessEqual(len(text), rc.LIMIT)
        self.assertIn("прочитай .claude/roles/judge.md", text)

    def test_huge_notebook_keeps_tail(self):
        self.write("judge.md", "# Судья\nкоротко\n")
        self.write("notes/judge.md", "\n".join(f"строка {i}" for i in range(5000)) + "\nПОСЛЕДНЯЯ")
        text = rc.full_context("judge", "Роль: Судья", "startup", {})
        self.assertLessEqual(len(text), rc.LIMIT)
        self.assertTrue(text.rstrip().endswith("ПОСЛЕДНЯЯ"))
        self.assertIn("начало — в файле", text)
        notebook = text.split("--- блокнот", 1)[1]
        self.assertLessEqual(len(notebook), rc.NOTEBOOK_TAIL + 200)

    def test_charter_that_fits_is_inline(self):
        self.write("judge.md", "# Судья\nУСТАВ-МАРКЕР\n")
        self.write("notes/judge.md", "# Блокнот\nмало\n")
        text = rc.full_context("judge", "Роль: Судья", "startup", {})
        self.assertIn("УСТАВ-МАРКЕР", text)
        self.assertIn("мало", text)

    def test_fit_truncates_anything(self):
        self.assertEqual(rc.fit("x" * 100), "x" * 100)
        out = rc.fit("я" * 20000)
        self.assertLessEqual(len(out), rc.LIMIT)
        self.assertIn("обрезана", out)


class Prompts(unittest.TestCase):
    def setUp(self):
        for k in ("ALPHA_ROLE", "ALPHA_TICKET", "CLAUDE_CODE_HOST_SESSION_ID"):
            os.environ.pop(k, None)
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in ("ALPHA_ROLE", "ALPHA_TICKET")])

    def test_state_key_uses_alpha_role_and_ticket(self):
        os.environ["ALPHA_ROLE"] = "researcher"
        self.assertEqual(rm.state_key("researcher"), "researcher")
        os.environ["ALPHA_TICKET"] = "TK-025"
        self.assertEqual(rm.state_key("researcher"), "researcher-TK-025")
        os.environ["ALPHA_TICKET"] = "../../evil"
        self.assertEqual(rm.state_key("researcher"), "researcher")
        os.environ.pop("ALPHA_ROLE")
        self.assertEqual(rm.state_key("judge"), "judge")
        self.assertEqual(rm.state_key(None), "unknown")

    def test_role_prompt_is_silence_line_about_ticket_log(self):
        os.environ["ALPHA_ROLE"] = "engineer"
        text = rm.on_prompt_all({"session_id": "s1"}, "engineer")
        self.assertIn("tickets.py comment", text)
        for bad in ("SendMessage", "clear_session", "блокнот"):
            self.assertNotIn(bad, text)

    def test_inbox_one_line_only_when_new(self):
        inbox = rm.DISPATCHER_CEO_INBOX
        with open(inbox, "w", encoding="utf-8") as fh:
            fh.write("[blocked] TK-1 очень длинная строка " + "x" * 500 + "\n[needs_owner] TK-2 y\n")
        with open(rm.DISPATCHER_CEO_INBOX_SEEN, "w", encoding="utf-8") as fh:
            fh.write("0")
        first = rm.ceo_inbox_alert()
        self.assertEqual(first, "[диспетчер] 2 новых в .claude/dispatcher/ceo-inbox.md")
        self.assertIsNone(rm.ceo_inbox_alert())          # ничего нового — тишина
        with open(inbox, "a", encoding="utf-8") as fh:
            fh.write("[blocked] TK-3\n")
        self.assertEqual(rm.ceo_inbox_alert(), "[диспетчер] 1 новых в .claude/dispatcher/ceo-inbox.md")

    def transcript(self, tokens):
        path = os.path.join(TMP, f"tr-{tokens}.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "assistant", "message": {"usage": {
                "input_tokens": 1, "cache_read_input_tokens": tokens, "cache_creation_input_tokens": 0,
                "output_tokens": 9}}}) + "\n")
        return path

    def test_context_watchdog_threshold_and_repeat(self):
        self.assertEqual(rm.CONTEXT_WARN_TOKENS, 450_000)  # 45 % окна Opus 1 млн (владелец 03.10)
        small = {"transcript_path": self.transcript(440_000)}
        big = {"transcript_path": self.transcript(460_000)}
        self.assertIsNone(rm.context_advice(small, {"n": 1}))
        state = {"n": 1}
        text = rm.context_advice(big, state)
        self.assertEqual(text, "контекст ≈ 460 тыс. — закончи шаг, обнови блокнот и попроси владельца сделать клир")
        state["n"] = 5
        self.assertIsNone(rm.context_advice(big, state))   # повтор не чаще раза в 10 сообщений
        state["n"] = 11
        self.assertIsNotNone(rm.context_advice(big, state))

    def test_context_tokens_single_line_transcript(self):
        self.assertEqual(rm.context_tokens(self.transcript(1000)), 1010)

    def test_no_deck_alert_and_no_reminders(self):
        for name in ("deck_alert", "on_prompt", "consolidate_due", "CONSOLIDATE_TEXT", "on_skill"):
            self.assertFalse(hasattr(rm, name), name)

    def test_throttled(self):
        state = {"n": 1}
        self.assertEqual(rm.throttled(state, "k", "sig", "text"), "text")
        state["n"] = 2
        self.assertIsNone(rm.throttled(state, "k", "sig", "text"))
        self.assertEqual(rm.throttled(state, "k", "other", "text2"), "text2")
        state["n"] = 12
        self.assertEqual(rm.throttled(state, "k", "other", "text2"), "text2")

    def test_digest_labels_dispatcher_prompts(self):
        tr = os.path.join(TMP, "turns.jsonl")
        rows = [{"type": "user", "timestamp": "2026-10-02T00:30:00Z", "message": {"content": "Тикет TK-026"}},
                {"type": "assistant", "timestamp": "2026-10-02T00:31:00Z",
                 "message": {"content": [{"type": "text", "text": "Готово"}]}}]
        with open(tr, "w", encoding="utf-8") as fh:
            fh.write("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")
        os.environ["ALPHA_ROLE"] = "engineer"
        path = rm.write_digest(tr, "engineer", "ALPHA_ROLE=engineer", "cafebabe-1", "тест")
        with open(path, encoding="utf-8") as fh:
            body = fh.read()
        self.assertIn("· диспетчер", body)
        self.assertNotIn("· владелец", body)
        os.environ.pop("ALPHA_ROLE")
        path2 = rm.write_digest(tr, "ceo", "CEO", "cafebabe-2", "тест")
        with open(path2, encoding="utf-8") as fh:
            self.assertIn("· владелец", fh.read())


class TicketScopedDigests(unittest.TestCase):
    """A7 (аудит 03.10): диспетчер передаёт `ALPHA_TICKET`; состояние и «конспект прошлой сессии» — по тикету: роль,
    взявшая новый тикет, не получает ссылку на конспект чужого."""

    def setUp(self):
        for k in ("ALPHA_ROLE", "ALPHA_TICKET", "CLAUDE_CODE_HOST_SESSION_ID"):
            os.environ.pop(k, None)
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in ("ALPHA_ROLE", "ALPHA_TICKET")])
        self.role = "engineer"
        self.transcript = os.path.join(TMP, "ticket-turns.jsonl")
        rows = [{"type": "user", "timestamp": "2026-10-03T00:30:00Z", "message": {"content": "Тикет"}},
                {"type": "assistant", "timestamp": "2026-10-03T00:31:00Z",
                 "message": {"content": [{"type": "text", "text": "Готово"}]}}]
        with open(self.transcript, "w", encoding="utf-8") as fh:
            fh.write("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")
        shutil.rmtree(os.path.join(rm.LOG_DIR, self.role), ignore_errors=True)
        shutil.rmtree(rm.STATE_DIR, ignore_errors=True)

    def digest(self, ticket, cli):
        os.environ["ALPHA_ROLE"] = self.role
        if ticket:
            os.environ["ALPHA_TICKET"] = ticket
        else:
            os.environ.pop("ALPHA_TICKET", None)
        return rm.write_digest(self.transcript, self.role, "t", cli, "тест")

    def test_digest_of_dispatcher_run_lands_in_ticket_folder(self):
        path = self.digest("TK-030", "aaaaaaaa-1")
        self.assertEqual(os.path.basename(os.path.dirname(path)), "TK-030")
        self.assertEqual(os.path.basename(os.path.dirname(os.path.dirname(path))), self.role)

    def test_latest_digest_is_per_ticket(self):
        a = self.digest("TK-030", "aaaaaaaa-1")
        b = self.digest("TK-031", "bbbbbbbb-2")
        os.environ["ALPHA_ROLE"] = self.role
        os.environ["ALPHA_TICKET"] = "TK-030"
        self.assertEqual(rm.latest_digest(self.role, "other-cli"), a)
        os.environ["ALPHA_TICKET"] = "TK-031"
        self.assertEqual(rm.latest_digest(self.role, "other-cli"), b)
        os.environ["ALPHA_TICKET"] = "TK-099"
        self.assertIsNone(rm.latest_digest(self.role, "other-cli"))      # новый тикет — чужого конспекта нет
        os.environ.pop("ALPHA_TICKET")
        self.assertIsNone(rm.latest_digest(self.role, "other-cli"))      # без тикета — только каталог роли

    def test_session_without_ticket_keeps_role_folder(self):
        path = self.digest(None, "cccccccc-3")
        self.assertEqual(os.path.basename(os.path.dirname(path)), self.role)

    def test_catch_up_of_previous_session_goes_to_the_same_ticket(self):
        os.environ["ALPHA_ROLE"] = self.role
        os.environ["ALPHA_TICKET"] = "TK-040"
        rm.on_session_start({"session_id": "dddddddd-4", "transcript_path": self.transcript}, self.role, "t")
        last = rm.on_session_start({"session_id": "eeeeeeee-5", "transcript_path": self.transcript}, self.role, "t")
        self.assertIsNotNone(last)
        self.assertEqual(os.path.basename(os.path.dirname(last)), "TK-040")
        self.assertTrue(last.endswith("-dddddddd.md"))


if __name__ == "__main__":
    unittest.main()
