"""TK-052 (В-15): порог контекста сессии CEO — вставка с 350 тыс., хардстоп с 400 тыс.; другие роли не затронуты.

Хук гоняется отдельным процессом на проекте-пустышке (как плагином): модуль не импортирует `role_context`/`role_memory` —
корень проекта они запоминают при импорте, и чужой импорт сломал бы `test_role_hooks`."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(HERE)), ".claude", "dispatcher"))
import ceo_context as cc  # noqa: E402
import transcript as tr  # noqa: E402

HOOK = os.path.join(HERE, "role_memory.py")


def row(msg_id, ctx_in=0, cache_create=0, cache_read=0, out=7, **extra):
    usage = {"input_tokens": ctx_in, "cache_creation_input_tokens": cache_create,
             "cache_read_input_tokens": cache_read, "output_tokens": out}
    return {"type": "assistant", "message": {"id": msg_id, "role": "assistant", "usage": usage}, **extra}


class Sandbox(unittest.TestCase):
    """Проект-пустышка и транскрипты во временном каталоге."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ceo-context-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.project = os.path.join(self.tmp, "proj")
        os.makedirs(os.path.join(self.project, ".claude", "roles", ".state"))
        os.makedirs(os.path.join(self.project, ".claude", "dispatcher"))

    def transcript(self, *rows, name="tr.jsonl"):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")
        return path

    def at(self, tokens, name="tr.jsonl"):
        """Транскрипт, где контекст последнего ответа — ровно `tokens` (вход 1 + кэш)."""
        return self.transcript(row("m0", cache_read=10), row("m1", ctx_in=1, cache_read=tokens - 1), name=name)

    def label(self, role, session):
        with open(os.path.join(self.project, ".claude", "roles", ".state", f"session-{session}.role"), "w") as fh:
            fh.write(role)

    def settings(self, **kv):
        with open(os.path.join(self.project, ".claude", "rpv.json"), "w", encoding="utf-8") as fh:
            json.dump(kv, fh)

    def prompt(self, path, text="привет", session="s1", **env):
        """Хук UserPromptSubmit отдельным процессом → (код выхода, объект JSON вывода | None)."""
        e = {k: v for k, v in os.environ.items() if not k.startswith("RPV_") and k != "CLAUDE_CODE_HOST_SESSION_ID"}
        e.update(CLAUDE_PROJECT_DIR=self.project, CLAUDE_CONFIG_DIR=os.path.join(self.tmp, "cfg"), **env)
        stdin = json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": session, "transcript_path": path,
                            "prompt": text})
        r = subprocess.run([sys.executable, HOOK], input=stdin.encode("utf-8"), capture_output=True, env=e, timeout=60)
        out = r.stdout.decode("utf-8")
        return r.returncode, (json.loads(out) if out.strip() else None)

    def quiet(self, path, **kw):
        """Сообщение прошло без отказа и без строк порога/совета о контексте (сторож и «молчание» ролей — не в счёт)."""
        code, out = self.prompt(path, **kw)
        text = ((out or {}).get("hookSpecificOutput") or {}).get("additionalContext", "")
        return code == 0 and "decision" not in (out or {}) and "контекст" not in text


class CountTests(Sandbox):
    def test_count_is_in_plus_cache_create_plus_cache_read_of_last_reply(self):
        path = self.transcript(row("m1", 5, 5, 5), row("m2", ctx_in=3, cache_create=100, cache_read=2000, out=50))
        self.assertEqual(tr.last_turn_tokens(path), 2103)   # вывод (50) в контекст не входит

    def test_count_message_id_rows_counted_once(self):
        """Один ответ пишется несколькими строками (мысль, текст, вызовы) с одним id и одним usage — не складываются."""
        path = self.transcript(row("m1", 1, 0, 9000), row("m2", 1, 0, 9500), row("m2", 1, 0, 9500), row("m2", 1, 0, 9500))
        self.assertEqual(tr.last_turn_tokens(path), 9501)

    def test_count_skips_sidechain_and_rows_without_usage(self):
        path = self.transcript(row("m1", 1, 0, 400), row("m9", 1, 0, 900_000, isSidechain=True),
                               {"type": "user", "message": {"role": "user", "content": "x"}},
                               row("m2", 0, 0, 0))   # синтетический ответ без usage — не контекст
        self.assertEqual(tr.last_turn_tokens(path), 401)

    def test_count_no_transcript_is_zero(self):
        self.assertEqual(tr.last_turn_tokens(os.path.join(self.tmp, "none.jsonl")), 0)


class BoundsTests(Sandbox):
    def test_bounds_verdict(self):
        w, s = cc.WARN_DEFAULT, cc.STOP_DEFAULT
        self.assertEqual((w, s), (350_000, 400_000))
        self.assertIsNone(cc.verdict(349_999, "x", w, s))
        self.assertEqual(cc.verdict(350_000, "x", w, s)[0], "warn")
        self.assertEqual(cc.verdict(399_999, "x", w, s)[0], "warn")
        self.assertEqual(cc.verdict(400_000, "x", w, s)[0], "stop")

    def test_bounds_hook_below_warn_is_silent(self):
        self.label("ceo", "s1")
        self.assertTrue(self.quiet(self.at(349_999)))

    def test_bounds_hook_warn_inserts_notebook_and_clear(self):
        self.label("ceo", "s1")
        for tokens in (350_000, 399_999):
            code, out = self.prompt(self.at(tokens))
            self.assertEqual(code, 0)
            self.assertNotIn("decision", out)                              # сообщение уходит в модель
            text = out["hookSpecificOutput"]["additionalContext"]
            self.assertIn(f"контекст {tokens // 1000} тыс. ≥ 350", text)
            self.assertIn("допиши блокнот .claude/roles/notes/ceo.md", text)
            self.assertIn("/clear", text)
            self.assertIn("первой строкой", text)

    def test_bounds_hook_warn_on_every_message(self):
        """Не раз в 10 сообщений, а на каждое сообщение владельца."""
        self.label("ceo", "s1")
        path = self.at(360_000)
        for _ in range(3):
            self.assertIn("допиши блокнот", self.prompt(path)[1]["hookSpecificOutput"]["additionalContext"])

    def test_bounds_hook_stop_blocks_with_reason_and_clear(self):
        self.label("ceo", "s1")
        code, out = self.prompt(self.at(400_000))
        self.assertEqual(code, 0)
        self.assertEqual(out["decision"], "block")
        self.assertIn("/clear", out["reason"])
        self.assertIn("400 тыс.", out["reason"])
        self.assertIn("сообщение не отправлено", out["reason"])
        self.assertIn(".claude/roles/notes/ceo.md", out["reason"])
        self.assertNotIn("hookSpecificOutput", out)

    def test_bounds_after_clear_new_session_works(self):
        """/clear — новая сессия с малым транскриптом: сообщение проходит без отказа и без вставки."""
        self.label("ceo", "s2")
        self.assertTrue(self.quiet(self.at(30_000, name="new.jsonl"), session="s2"))

    def test_bounds_clear_and_compact_pass_at_stop(self):
        self.label("ceo", "s1")
        for text in ("/clear", "  /compact сожми", "/CLEAR"):
            out = self.prompt(self.at(500_000), text=text)[1]
            self.assertNotIn("decision", out, text)
        self.assertEqual(self.prompt(self.at(500_000), text="/clearly not")[1]["decision"], "block")


class OtherRolesTests(Sandbox):
    def test_other_roles_not_blocked_and_no_ceo_line(self):
        big = self.at(450_000)
        for role in ("engineer", "judge", "researcher"):
            self.label(role, "s1")
            _, out = self.prompt(big)
            self.assertNotIn("decision", out or {}, role)                  # не блокируются
            text = out["hookSpecificOutput"]["additionalContext"]
            self.assertNotIn("[контекст CEO]", text, role)

    def test_other_roles_old_advice_unchanged_at_400k(self):
        self.label("judge", "s1")
        text = self.prompt(self.at(410_000))[1]["hookSpecificOutput"]["additionalContext"]
        self.assertIn("контекст ≈ 410 тыс. — закончи шаг", text)
        self.assertTrue(self.quiet(self.at(399_999)))

    def test_other_roles_dispatcher_run_untouched(self):
        """Запуск диспетчера (RPV_ROLE) — никакого порога CEO: просить клир некого."""
        _, out = self.prompt(self.at(450_000), RPV_ROLE="engineer", RPV_TICKET="TK-052")
        self.assertNotIn("decision", out)
        self.assertNotIn("[контекст CEO]", out["hookSpecificOutput"]["additionalContext"])

    def test_other_roles_no_label_session_untouched(self):
        self.assertEqual(self.prompt(self.at(450_000), session="nobody"), (0, None))      # роли нет — хук молчит


class SettingsTests(Sandbox):
    def test_settings_override_thresholds(self):
        self.settings(ceo_context_warn=100_000, ceo_context_stop=200_000)
        self.assertEqual(cc.limits(self.project), (100_000, 200_000))
        self.label("ceo", "s1")
        self.assertIn("≥ 100", self.prompt(self.at(150_000))[1]["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(self.prompt(self.at(200_000))[1]["decision"], "block")

    def test_settings_default_without_file_or_key(self):
        self.assertEqual(cc.limits(self.project), (350_000, 400_000))
        self.settings(report_at="10:00")
        self.assertEqual(cc.limits(self.project), (350_000, 400_000))

    def test_settings_invalid_values_fall_back_to_defaults(self):
        for bad in ({"ceo_context_warn": "350"}, {"ceo_context_warn": True}, {"ceo_context_stop": 0},
                    {"ceo_context_stop": -5}, {"ceo_context_warn": 500_000},          # предупреждение не ниже остановки
                    {"ceo_context_warn": 300_000, "ceo_context_stop": 300_000}, {"ceo_context_stop": None}):
            self.settings(**bad)
            self.assertEqual(cc.limits(self.project), (350_000, 400_000), bad)

    def test_settings_broken_file_gives_defaults(self):
        with open(os.path.join(self.project, ".claude", "rpv.json"), "w") as fh:
            fh.write("{не json")
        self.assertEqual(cc.limits(self.project), (350_000, 400_000))

    def test_settings_keys_are_in_rpv_config_and_doc(self):
        import rpv_config
        for key in ("ceo_context_warn", "ceo_context_stop"):
            self.assertIn(key, rpv_config.KEYS)


if __name__ == "__main__":
    unittest.main()
