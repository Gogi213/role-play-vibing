"""TK-003 п.3: SessionEnd запуска диспетчера (`claude -p`) пишет конспект без Haiku — CLI выходит сразу после ответа
и отменяет хук («Hook cancelled» в err.log); Desktop-сессия по-прежнему получает краткий конспект."""
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_role_hooks import rm  # noqa: E402  (проект-пустышка поднимает test_role_hooks при импорте)


class SessionEndWithoutHaikuTests(unittest.TestCase):
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
                    mock.patch.object(rm, "haiku_abstract", return_value="") as haiku, \
                    mock.patch.object(sys, "stdin", mock.Mock(buffer=io.BytesIO(payload.encode()))):
                rm.main()
            return haiku.call_count, os.path.isfile(out)

    def test_dispatcher_run_writes_digest_without_haiku(self):
        calls, written = self.run_hook({"RPV_ROLE": "engineer", "RPV_TICKET": "TK-003"})
        self.assertEqual((calls, written), (0, True))

    def test_owner_session_still_gets_haiku_abstract(self):
        env = {"CLAUDE_CODE_HOST_SESSION_ID": "host-1"}
        with mock.patch.object(rm, "current_role", return_value=("CEO", "ceo")), \
                mock.patch.object(rm, "env_role", return_value=None):
            calls, written = self.run_hook(env)
        self.assertEqual((calls, written), (1, True))


if __name__ == "__main__":
    unittest.main()
