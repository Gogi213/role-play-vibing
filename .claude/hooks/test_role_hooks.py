"""Проверки `role_context.py` (SessionStart) и `role_memory.py` (UserPromptSubmit/SessionEnd).

Запуск из `.claude/hooks`: `python test_role_hooks.py`. Рабочее состояние (`.state`, `log`, ceo-inbox) не трогается:
каталоги подменяются окружением до импорта.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="hooks-test-")
# проект-пустышка после `/rpv-init`: `.claude/roles` из шаблонов плагина (без него хуки молчат)
PROJECT = os.path.join(TMP, "proj")
shutil.copytree(os.path.join(PLUGIN, "templates", "roles"), os.path.join(PROJECT, ".claude", "roles"))
os.makedirs(os.path.join(PROJECT, ".claude", "tickets"))
os.environ["CLAUDE_PROJECT_DIR"] = PROJECT
os.environ["RPV_STATE_DIR"] = os.path.join(TMP, "state")
os.environ["RPV_LOG_DIR"] = os.path.join(TMP, "log")
os.environ["RPV_DISPATCHER_DIR"] = os.path.join(TMP, "disp")
os.makedirs(os.environ["RPV_DISPATCHER_DIR"], exist_ok=True)
for var in ("RPV_ROLE", "RPV_TICKET", "RPV_TICKET_ID", "ALPHA_ROLE", "ALPHA_TICKET", "ALPHA_TICKET_ID",
            "ALPHA_STATE_DIR", "ALPHA_LOG_DIR", "ALPHA_DISPATCHER_DIR", "CLAUDE_CODE_HOST_SESSION_ID"):
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
    env = {"RPV_ROLE": role}
    if ticket:
        env["RPV_TICKET"] = ticket
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
                self.assertIn("tickets.py\" --project", text)
                self.assertIn(" comment <ID> --author " + role, text)

    def test_startup_gives_ready_tickets_command_with_absolute_path(self):
        """Блокер: в проекте после /rpv-init нет tickets.py — хук вставляет готовую команду с абсолютным путём плагина."""
        for role in ("researcher", "engineer", "judge", "ceo"):
            for source in ("startup", "resume"):
                text, _ = start_text(role, source)
                m = re.search(r'"([^"]+)" "([^"]+tickets\.py)" --project "([^"]+)"', text)
                self.assertIsNotNone(m, (role, source, text))
                exe, script, project = m.group(1), m.group(2), m.group(3)
                self.assertEqual(os.path.normpath(exe), os.path.normpath(sys.executable))  # TK-110 В-1
                self.assertTrue(os.path.isabs(script), script)
                self.assertTrue(os.path.isfile(script), script)
                self.assertEqual(os.path.normpath(script),
                                 os.path.normpath(os.path.join(PLUGIN, ".claude", "dispatcher", "tickets.py")))
                self.assertEqual(os.path.normpath(project), os.path.normpath(PROJECT))
                self.assertNotIn("python .claude/dispatcher/tickets.py", text)

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
        env = dict(os.environ, RPV_ROLE="judge")
        r = subprocess.run([sys.executable, os.path.join(HERE, "role_context.py")], input=b"not json", env=env,
                           capture_output=True)
        self.assertEqual(r.returncode, 0)
        json.loads(r.stdout.decode("utf-8"))


class RoleDetection(unittest.TestCase):
    """Роль: RPV_ROLE, запасная ALPHA_ROLE, метка сессии (`/ceo`), последним — название сессии Desktop."""

    def setUp(self):
        for k in ("RPV_ROLE", "RPV_TICKET", "ALPHA_ROLE", "ALPHA_TICKET", "CLAUDE_CODE_HOST_SESSION_ID"):
            os.environ.pop(k, None)
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in ("RPV_ROLE", "ALPHA_ROLE")])
        shutil.rmtree(rc.STATE_DIR, ignore_errors=True)

    def context(self, session_id, extra_env=None, source="startup"):
        r = run_hook("role_context.py", {"hook_event_name": "SessionStart", "source": source, "session_id": session_id},
                     extra_env or {})
        return json.loads(r.stdout.decode("utf-8"))["hookSpecificOutput"]["additionalContext"]

    def test_rpv_role_wins_over_alpha_role_and_alpha_is_fallback(self):
        os.environ["ALPHA_ROLE"] = "judge"
        self.assertEqual(rc.env_role(), "judge")
        os.environ["RPV_ROLE"] = "engineer"
        self.assertEqual(rc.env_role(), "engineer")
        os.environ["RPV_ROLE"] = "nonsense"   # неизвестное значение — не роль, запасной путь не подменяет его
        self.assertIsNone(rc.env_role())

    def test_no_label_means_role_undetermined_and_hint_names_ceo_command(self):
        text = self.context("no-label-1")
        self.assertIn("не определена", text)
        self.assertIn("/ceo", text)

    def test_label_makes_session_ceo(self):
        self.assertTrue(rc.write_label("ceo", "lab-1"))
        self.assertTrue(os.path.isfile(os.path.join(rc.STATE_DIR, "session-lab-1.role")))
        text = self.context("lab-1")
        self.assertIn("«CEO»", text)
        self.assertIn(".claude/roles/ceo.md", text)
        short = self.context("lab-1", source="resume")
        self.assertIn("«CEO»", short)
        self.assertNotIn("«CEO»", self.context("other-session"))   # метка — своей сессии

    def test_env_role_wins_over_label(self):
        rc.write_label("ceo", "lab-2")
        text = self.context("lab-2", {"RPV_ROLE": "judge"})
        self.assertIn("«Судья»", text)

    def test_host_label_survives_clear(self):
        os.environ["CLAUDE_CODE_HOST_SESSION_ID"] = "local_abc"
        self.addCleanup(lambda: os.environ.pop("CLAUDE_CODE_HOST_SESSION_ID", None))
        rc.write_label("ceo", "before-clear")
        self.assertTrue(os.path.isfile(os.path.join(rc.STATE_DIR, "host-local_abc.role")))
        self.assertEqual(rc.read_label("after-clear"), "ceo")     # новый session_id, тот же id сессии приложения

    def test_label_rejects_unknown_role_and_garbage(self):
        self.assertFalse(rc.write_label("owner", "lab-3"))
        self.assertFalse(rc.write_label("ceo", ""))
        os.makedirs(rc.STATE_DIR, exist_ok=True)
        with open(os.path.join(rc.STATE_DIR, "session-lab-4.role"), "w", encoding="utf-8") as fh:
            fh.write("кто-то")
        self.assertIsNone(rc.read_label("lab-4"))
        self.assertIsNone(rc.read_label("../../x"))               # id сессии не выходит из каталога меток

    def run_prompt(self, prompt, session_id):
        return run_hook("role_memory.py", {"hook_event_name": "UserPromptSubmit", "session_id": session_id,
                                           "prompt": prompt}, {})

    def test_ceo_command_in_prompt_sets_label(self):
        for prompt in ("/ceo", "/role-play-vibing:ceo", "  /ceo привет"):
            sid = "cmd-" + str(abs(hash(prompt)))
            self.run_prompt(prompt, sid)
            self.assertEqual(rc.read_label(sid), "ceo", prompt)
        self.run_prompt("/ceoish", "cmd-no")
        self.run_prompt("расскажи про /ceo", "cmd-no2")
        self.assertIsNone(rc.read_label("cmd-no"))
        self.assertIsNone(rc.read_label("cmd-no2"))

    def test_labelled_ceo_gets_ceo_prompts_not_silence_line(self):
        rc.write_label("ceo", "lab-5")
        self.assertEqual(rm.current_role({"session_id": "lab-5"}), ("CEO", "ceo"))
        self.assertEqual(rm.current_role({"session_id": "unlabelled"}), (None, None))
        r = self.run_prompt("привет", "lab-5")
        self.assertEqual(r.returncode, 0)
        self.assertNotIn("молчание", r.stdout.decode("utf-8"))

    def test_hook_texts_are_neutral(self):
        for name in ("role_context.py", "role_memory.py", "delete_guard.py"):
            with open(os.path.join(HERE, name), encoding="utf-8") as fh:
                body = fh.read()
            for bad in ("Steam Deck", "SETTLED", "«СОСТОЯНИЕ»", "alpha-compute", "alpha-role", "команды alpha"):
                self.assertNotIn(bad, body, (name, bad))
        for source in ("startup", "resume"):
            for role in ("ceo", "engineer"):
                text, _ = start_text(role, source)
                head = text.split("\n--- ")[0].replace(rc.tickets_command(), "")  # абсолютные пути — не текст хука
                self.assertNotIn("alpha", head.lower(), (role, source))
        self.assertNotIn("alpha", rc.MANUAL.lower())
        self.assertNotIn("alpha", rc.OUTSIDER.lower())


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
        for k in ("RPV_ROLE", "RPV_TICKET", "ALPHA_ROLE", "ALPHA_TICKET", "CLAUDE_CODE_HOST_SESSION_ID"):
            os.environ.pop(k, None)
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in ("RPV_ROLE", "RPV_TICKET", "ALPHA_ROLE", "ALPHA_TICKET")])

    def test_state_key_uses_role_and_ticket(self):
        os.environ["RPV_ROLE"] = "researcher"
        self.assertEqual(rm.state_key("researcher"), "researcher")
        os.environ["RPV_TICKET"] = "TK-025"
        self.assertEqual(rm.state_key("researcher"), "researcher-TK-025")
        os.environ["RPV_TICKET"] = "../../evil"
        self.assertEqual(rm.state_key("researcher"), "researcher")
        os.environ.pop("RPV_ROLE")
        self.assertEqual(rm.state_key("judge"), "judge")
        self.assertEqual(rm.state_key(None), "unknown")

    def test_role_prompt_is_silence_line_about_ticket_log(self):
        os.environ["RPV_ROLE"] = "engineer"
        text = rm.on_prompt_all({"session_id": "s1"}, "engineer")
        self.assertIn("tickets.py result", text)
        self.assertNotIn("tickets.py comment", text)
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
        os.environ["RPV_ROLE"] = "engineer"
        path = rm.write_digest(tr, "engineer", "RPV_ROLE=engineer", "cafebabe-1", "тест")
        with open(path, encoding="utf-8") as fh:
            body = fh.read()
        self.assertIn("· диспетчер", body)
        self.assertNotIn("· владелец", body)
        os.environ.pop("RPV_ROLE")
        path2 = rm.write_digest(tr, "ceo", "CEO", "cafebabe-2", "тест")
        with open(path2, encoding="utf-8") as fh:
            self.assertIn("· владелец", fh.read())


class TicketScopedDigests(unittest.TestCase):
    """A7 (аудит 03.10): диспетчер передаёт `RPV_TICKET`; состояние и «конспект прошлой сессии» — по тикету: роль,
    взявшая новый тикет, не получает ссылку на конспект чужого."""

    def setUp(self):
        for k in ("RPV_ROLE", "RPV_TICKET", "ALPHA_ROLE", "ALPHA_TICKET", "CLAUDE_CODE_HOST_SESSION_ID"):
            os.environ.pop(k, None)
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in ("RPV_ROLE", "RPV_TICKET", "ALPHA_ROLE", "ALPHA_TICKET")])
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
        os.environ["RPV_ROLE"] = self.role
        if ticket:
            os.environ["RPV_TICKET"] = ticket
        else:
            os.environ.pop("RPV_TICKET", None)
        return rm.write_digest(self.transcript, self.role, "t", cli, "тест")

    def test_digest_of_dispatcher_run_lands_in_ticket_folder(self):
        path = self.digest("TK-030", "aaaaaaaa-1")
        self.assertEqual(os.path.basename(os.path.dirname(path)), "TK-030")
        self.assertEqual(os.path.basename(os.path.dirname(os.path.dirname(path))), self.role)

    def test_latest_digest_is_per_ticket(self):
        a = self.digest("TK-030", "aaaaaaaa-1")
        b = self.digest("TK-031", "bbbbbbbb-2")
        os.environ["RPV_ROLE"] = self.role
        os.environ["RPV_TICKET"] = "TK-030"
        self.assertEqual(rm.latest_digest(self.role, "other-cli"), a)
        os.environ["RPV_TICKET"] = "TK-031"
        self.assertEqual(rm.latest_digest(self.role, "other-cli"), b)
        os.environ["RPV_TICKET"] = "TK-099"
        self.assertIsNone(rm.latest_digest(self.role, "other-cli"))      # новый тикет — чужого конспекта нет
        os.environ.pop("RPV_TICKET")
        self.assertIsNone(rm.latest_digest(self.role, "other-cli"))      # без тикета — только каталог роли

    def test_session_without_ticket_keeps_role_folder(self):
        path = self.digest(None, "cccccccc-3")
        self.assertEqual(os.path.basename(os.path.dirname(path)), self.role)

    def test_catch_up_of_previous_session_goes_to_the_same_ticket(self):
        os.environ["RPV_ROLE"] = self.role
        os.environ["RPV_TICKET"] = "TK-040"
        rm.on_session_start({"session_id": "dddddddd-4", "transcript_path": self.transcript}, self.role, "t")
        last = rm.on_session_start({"session_id": "eeeeeeee-5", "transcript_path": self.transcript}, self.role, "t")
        self.assertIsNotNone(last)
        self.assertEqual(os.path.basename(os.path.dirname(last)), "TK-040")
        self.assertTrue(last.endswith("-dddddddd.md"))


class RoleInstructionsUseResult(unittest.TestCase):
    """Под RPV_STOP_STRICT=1 итог шага — только `tickets.py result`: ни одна инструкция роли не велит `comment` для итога."""

    def test_no_instruction_sends_role_to_comment_for_result(self):
        import delete_guard as dg
        for text in (rm.SILENT_TEXT, dg.REASON_IRREVERSIBLE, dg.REASON_CEO_SIGNAL):
            self.assertNotIn("tickets.py comment", text)
            self.assertIn("tickets.py result", text)

    def test_prompt_and_retry_note_use_result(self):
        sys.path.insert(0, os.path.join(os.path.dirname(HERE), "dispatcher"))
        import dispatch as D
        self.assertIn(" result TK-1 <", D.build_prompt("engineer", "TK-1"))
        src = open(D.__file__, encoding="utf-8").read()
        self.assertIn("командой tickets.py result", src)
        self.assertNotIn("командой tickets.py comment и обнови", src)


class StopResultTest(unittest.TestCase):
    TID = "TK-901"
    OLD = "### 2026-10-07T01:00:00+04:00 engineer\nстарая запись\n\n"
    NEW = "### 2026-10-07T02:00:00+04:00 engineer\nитог запуска\n\n"
    RES = "### 2026-10-07T02:00:00+04:00 engineer\n[итог: pr] — готово\n\n"
    HEAD = "---\nid: TK-901\ntitle: t\nowner: engineer\nstatus: in_progress\nreviewer: judge\n---\n\nописание\n\n## Лог\n\n"

    def _setup(self, launch_keys, log_lines):
        disp = os.environ["RPV_DISPATCHER_DIR"]
        with open(os.path.join(disp, "state.json"), "w", encoding="utf-8") as f:
            json.dump({"active_runs": {self.TID: {"log_keys_at_launch": launch_keys}}}, f)
        tdir = os.path.join(PROJECT, ".claude", "tickets")
        with open(os.path.join(tdir, self.TID + ".md"), "w", encoding="utf-8") as f:
            f.write(self.HEAD + "".join(log_lines))

    def _stop(self, extra=None, stdin=None):
        env = {"RPV_ROLE": "engineer", "RPV_TICKET": self.TID}
        env.update(extra or {})
        r = run_hook("stop_result.py", stdin or {"hook_event_name": "Stop"}, env)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout.decode("utf-8")) if r.stdout.strip() else None

    def test_no_new_entry_blocks(self):
        self._setup(["2026-10-07T01:00:00+04:00 engineer"], [self.OLD])
        out = self._stop()
        self.assertEqual(out["decision"], "block")
        self.assertIn(self.TID, out["reason"])

    def test_new_entry_allows(self):
        self._setup(["2026-10-07T01:00:00+04:00 engineer"], [self.OLD, self.NEW])
        self.assertIsNone(self._stop())

    def test_other_role_entry_does_not_count(self):
        self._setup([], ["### 2026-10-07T02:00:00+04:00 judge\nчужая\n\n".replace("\n", chr(92) + "n")])
        self.assertEqual(self._stop()["decision"], "block")

    def test_strict_needs_result_entry(self):
        self._setup(["2026-10-07T01:00:00+04:00 engineer"], [self.OLD, self.NEW])
        self.assertEqual(self._stop({"RPV_STOP_STRICT": "1"})["decision"], "block")
        self._setup(["2026-10-07T01:00:00+04:00 engineer"], [self.OLD, self.RES])
        self.assertIsNone(self._stop({"RPV_STOP_STRICT": "1"}))

    def test_second_stop_is_not_blocked(self):
        self._setup([], [])
        self.assertIsNone(self._stop(stdin={"stop_hook_active": True}))

    def test_not_a_dispatcher_run_is_silent(self):
        self._setup([], [])
        r = run_hook("stop_result.py", {}, {"RPV_ROLE": "", "RPV_TICKET": ""})
        self.assertEqual((r.returncode, r.stdout.strip()), (0, b""))
        os.remove(os.path.join(os.environ["RPV_DISPATCHER_DIR"], "state.json"))
        self.assertIsNone(self._stop())


class CeoSignalGuardTest(unittest.TestCase):
    def test_denied(self):
        import delete_guard as g
        d = g.ceo_signal_write
        self.assertTrue(d("Write", {"file_path": "C:/x/.claude/dispatcher/ceo-inbox.md"}))
        self.assertTrue(d("Edit", {"file_path": r"C:\x\ceo-wake.log"}))
        self.assertTrue(d("Bash", {"command": "echo hi >> .claude/dispatcher/ceo-wake.log"}))
        self.assertTrue(d("Bash", {"command": "cd x && rm .claude/dispatcher/ceo-inbox.md"}))
        self.assertFalse(d("Bash", {"command": "tail -20 .claude/dispatcher/ceo-wake.log"}))
        self.assertFalse(d("Bash", {"command": "grep urgent ceo-inbox.md | head"}))
        self.assertFalse(d("Write", {"file_path": "C:/x/other.md"}))
        self.assertFalse(d("Bash", {"command": 'python tickets.py comment TK-1 --author judge --text "проба >> ceo-inbox.md; rm ceo-wake.log"'}))
        self.assertTrue(d("Bash", {"command": 'python tickets.py comment TK-1 --text "x" >> ceo-inbox.md'}))
        self.assertFalse(d("Bash", {"command": 'python tickets.py result TK-1 done --why "читал ceo-inbox.md через open( и > "'}))  # TK-110 Ж-3
        self.assertTrue(d("Bash", {"command": 'python tickets.py result TK-1 done --why "x" > ceo-inbox.md'}))


class GuardJournalTest(unittest.TestCase):
    def test_journal_lines_and_silent_failure(self):
        import delete_guard as g
        with tempfile.TemporaryDirectory() as t:
            os.makedirs(os.path.join(t, ".claude", "dispatcher"))
            g.journal_guard(t, "Bash", [("temp", "allow"), ("rm", "deny")], "Запрет:  rm\n-rf " + "x" * 200)
            lines = open(os.path.join(t, ".claude", "dispatcher", "guard.log"), encoding="utf-8").read().splitlines()
            self.assertEqual([l.split("\t")[1:4] for l in lines], [["Bash", "temp", "allow"], ["Bash", "rm", "deny"]])
            self.assertEqual(lines[0].split("\t")[4], "")
            why = lines[1].split("\t")[4]
            self.assertEqual(why[:14], "Запрет: rm -rf")
            self.assertLessEqual(len(why), 90)
            g.journal_guard(None, "Bash", [("rm", "deny")], "x")           # нет проекта — тихо


class HaikuAbstractTest(unittest.TestCase):
    def test_digest_written_before_haiku_and_env_clean(self):  # TK-110 В-2 + Ж-1
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "dispatcher"))
        import haiku_aux
        seen = {}

        def runner(cmd, **kw):
            seen["env"] = kw["env"]
            return type("R", (), {"returncode": 0, "stdout": b"X"})()
        old = {k: os.environ.get(k) for k in ("RPV_ROLE", "CLAUDE_PROJECT_DIR")}
        os.environ["RPV_ROLE"], os.environ["CLAUDE_PROJECT_DIR"] = "judge", "/p"
        try:
            self.assertEqual(haiku_aux.ask("s", "t", runner=runner, claude="claude"), "X")
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.assertFalse([k for k in seen["env"] if k.startswith("RPV_")] or "CLAUDE_PROJECT_DIR" in seen["env"])
        orig = haiku_aux.compact
        with tempfile.TemporaryDirectory() as d:
            tr = os.path.join(d, "t.jsonl")
            with open(tr, "w", encoding="utf-8") as f:
                f.write(json.dumps({"type": "user", "message": {"content": "привет"}, "timestamp": ""}) + "\n")
            out = os.path.join(d, "dg.md")
            orig_dp, orig_ro = rm.digest_path, os.environ.get("RPV_HAIKU_COMPACT")
            rm.digest_path = lambda role, cli: out
            os.environ["RPV_HAIKU_COMPACT"] = "1"

            def boom(t, **kw):
                self.assertIn("привет", open(out, encoding="utf-8").read())      # файл уже на диске до Haiku
                self.assertEqual(kw.get("timeout"), rm.HAIKU_HOOK_S)
                raise KeyboardInterrupt
            haiku_aux.compact = boom
            try:
                with self.assertRaises(KeyboardInterrupt):
                    rm.write_digest(tr, "judge", "t", "abcdef123", "why", dispatcher=True)
            finally:
                haiku_aux.compact, rm.digest_path = orig, orig_dp
                if orig_ro is None:
                    os.environ.pop("RPV_HAIKU_COMPACT", None)
                else:
                    os.environ["RPV_HAIKU_COMPACT"] = orig_ro

    def test_abstract_on_off_and_failure(self):
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "dispatcher"))
        import haiku_aux
        orig, old = haiku_aux.compact, os.environ.get("RPV_HAIKU_COMPACT")
        try:
            haiku_aux.compact = lambda t, **kw: "KONSPEKT"
            os.environ["RPV_HAIKU_COMPACT"] = "1"
            self.assertIn("KONSPEKT", rm.haiku_abstract(["a"]))
            os.environ["RPV_HAIKU_COMPACT"] = "0"
            self.assertEqual(rm.haiku_abstract(["a"]), "")
            os.environ["RPV_HAIKU_COMPACT"] = "1"
            haiku_aux.compact = lambda t, **kw: None
            self.assertEqual(rm.haiku_abstract(["a"]), "")
        finally:
            haiku_aux.compact = orig
            if old is None:
                os.environ.pop("RPV_HAIKU_COMPACT", None)
            else:
                os.environ["RPV_HAIKU_COMPACT"] = old


if __name__ == "__main__":
    unittest.main()
