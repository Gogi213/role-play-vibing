"""TK-003 п.3 + вырез 2.0.18: SessionEnd пишет конспект кодом, без вызова модели — `claude -p` выходит сразу после ответа и отменяет
хук («Hook cancelled» в err.log), а Haiku-сводка набиралась в 1 конспекте из 532. Ни запуск диспетчера, ни сессия владельца её не просят."""
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_role_hooks import rm  # noqa: E402  (проект-пустышка поднимает test_role_hooks при импорте)

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "dispatcher"))
import haiku_aux  # noqa: E402


class SessionEndWithoutModelTests(unittest.TestCase):
    def run_hook(self, env):
        with tempfile.TemporaryDirectory() as d:
            tr = os.path.join(d, "t.jsonl")
            with open(tr, "w", encoding="utf-8") as f:
                f.write(json.dumps({"type": "user", "message": {"content": "привет"}, "timestamp": ""}) + "\n")
            out = os.path.join(d, "dg.md")
            payload = json.dumps({"hook_event_name": "SessionEnd", "session_id": "abcdef123456",
                                  "transcript_path": tr, "reason": "other"})
            with mock.patch.dict(os.environ, env, clear=False), \
                    mock.patch.object(rm, "digest_path", lambda role, cli: out), \
                    mock.patch.object(haiku_aux, "compact", return_value="СВОДКА") as haiku, \
                    mock.patch.object(sys, "stdin", mock.Mock(buffer=io.BytesIO(payload.encode()))):
                rm.main()
            text = open(out, encoding="utf-8").read() if os.path.isfile(out) else ""
            return haiku.call_count, text

    def test_dispatcher_run_writes_digest_without_model(self):
        calls, text = self.run_hook({"RPV_ROLE": "engineer", "RPV_TICKET": "TK-003"})
        self.assertEqual(calls, 0)
        self.assertIn("привет", text)

    def test_owner_session_digest_is_verbatim_messages_only(self):
        env = {"CLAUDE_CODE_HOST_SESSION_ID": "host-1"}
        with mock.patch.object(rm, "current_role", return_value=("CEO", "ceo")), \
                mock.patch.object(rm, "env_role", return_value=None):
            calls, text = self.run_hook(env)
        self.assertEqual(calls, 0)
        self.assertIn("привет", text)
        self.assertNotIn("СВОДКА", text)
        self.assertNotIn("Краткий конспект", text)

    def test_model_summary_and_notification_machinery_are_gone(self):
        for name in ("haiku_abstract", "HAIKU_HOOK_S", "on_notification", "pending_permissions", "PENDING"):
            self.assertFalse(hasattr(rm, name), name)


if __name__ == "__main__":
    unittest.main()
