"""CP2: `tickets.py allow` — согласие владельца на публикацию по тикету. Только CEO; основание — решение В-N, уже занесённое в
реестр; в шапку `allow: publish (В-N)` и запись `ceo` в лог; `--revoke` снимает; страж читает то, что пишет команда."""
from __future__ import annotations

import atexit
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_SANDBOX = tempfile.mkdtemp(prefix="rpv-test-proj-")
os.makedirs(os.path.join(_SANDBOX, ".claude", "roles"))
os.environ["CLAUDE_PROJECT_DIR"] = _SANDBOX
os.environ["RPV_BUS_DISABLE"] = "1"
atexit.register(shutil.rmtree, _SANDBOX, True)
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import registry as R  # noqa: E402
import ticket as T  # noqa: E402
import tickets as TK  # noqa: E402

HOOKS = HERE.parent / "hooks"


class AllowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.tickets_dir = self.root / ".claude" / "tickets"
        for p in (mock.patch.object(TK, "TICKETS_DIR", self.tickets_dir), mock.patch.object(TK, "PROJECT_ROOT", self.root),
                  mock.patch.dict(os.environ)):
            p.start()
            self.addCleanup(p.stop)
        os.environ.pop("RPV_ROLE", None)                       # по умолчанию — сессия CEO
        self.path = T.create_ticket(self.tickets_dir, owner="engineer", title="Выпуск 1.12")
        self.tid = self.path.stem

    def registry(self, *args):
        with contextlib.redirect_stdout(io.StringIO()):
            return R.main(["--project", str(self.root), *args])

    def decide(self):
        self.assertEqual(self.registry("decide", "--quote", "выпускай 1.12 в main", "--gist", "выпуск 1.12"), 0)

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = TK.main(list(args))
        return code, out.getvalue().strip(), err.getvalue().strip()

    def allow(self, *extra):
        return self.run_cli("allow", self.tid, "publish", "--ref", "В-1", *extra)

    def tkt(self):
        return T.read_ticket(self.path)

    def test_ceo_writes_header_field_and_log_entry(self):
        self.decide()
        before = self.tkt().header["updated"]
        code, out, _ = self.allow()
        self.assertEqual((code, out), (0, f"{self.tid}: allow: publish (В-1)"))
        tkt = self.tkt()
        self.assertEqual(tkt.header["allow"], "publish (В-1)")
        self.assertEqual(tkt.header["updated"], before)         # метка «обновлён» не двигается: роли это не повод просыпаться
        self.assertEqual(tkt.log[-1].author, "ceo")
        self.assertIn("В-1", tkt.log[-1].text)
        self.assertIn("allow: publish (В-1)", self.path.read_text(encoding="utf-8").split("\n---\n", 1)[0])

    def test_ref_must_be_an_existing_owner_decision(self):
        for ref in ("В-1", "В-9", "G-001", "выпускай 1.12 в main", "", "В-1 и ещё"):         # реестра ещё нет
            self.assertEqual(self.run_cli("allow", self.tid, "publish", "--ref", ref)[0], 1, ref)
        self.decide()
        self.registry("goal", "--title", "t", "--quote", "нужна цель")
        before = self.path.read_bytes()
        for ref in ("В-9", "G-001", "выпускай 1.12 в main", "", "В-1 и ещё", "В-"):
            code, _, err = self.run_cli("allow", self.tid, "publish", "--ref", ref)
            self.assertEqual(code, 1, ref)
            self.assertIn("registry.py decide", err)
        self.assertEqual(self.run_cli("allow", self.tid, "publish")[0], 1)                  # --ref не задан
        self.assertEqual(self.path.read_bytes(), before)

    def test_word_publish_is_required(self):
        self.decide()
        code, _, err = self.run_cli("allow", self.tid, "--ref", "В-1")
        self.assertEqual(code, 1)
        self.assertIn("publish", err)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            TK.main(["allow", self.tid, "deploy", "--ref", "В-1"])
        self.assertNotIn("allow", self.tkt().header)

    def test_unknown_ticket_is_refused(self):
        self.decide()
        self.assertEqual(self.run_cli("allow", "TK-404", "publish", "--ref", "В-1")[0], 1)
        self.assertEqual(self.run_cli("allow", "TK-404", "--revoke")[0], 1)

    def test_role_session_is_refused(self):
        self.decide()
        for role in ("engineer", "researcher", "judge"):
            os.environ["RPV_ROLE"] = role
            before = self.path.read_bytes()
            code, _, err = self.allow()
            self.assertEqual(code, 1, role)
            self.assertIn("только CEO", err)
            self.assertIn("ask-owner", err)
            self.assertEqual(self.run_cli("allow", self.tid, "--revoke")[0], 1, role)
            self.assertEqual(self.path.read_bytes(), before, role)
        os.environ["RPV_ROLE"] = "ceo"
        self.assertEqual(self.allow()[0], 0)

    def test_revoke_clears_the_field(self):
        self.decide()
        self.allow()
        code, out, _ = self.run_cli("allow", self.tid, "--revoke")
        self.assertEqual(code, 0)
        self.assertIn("снято", out)
        tkt = self.tkt()
        self.assertEqual(tkt.header["allow"], "")
        self.assertEqual((tkt.log[-1].author, tkt.log[-1].text), ("ceo", "согласие на публикацию снято"))
        n = len(tkt.log)
        code, out, _ = self.run_cli("allow", self.tid, "--revoke")                           # снимать нечего — записи нет
        self.assertEqual(code, 0)
        self.assertIn("не было", out)
        self.assertEqual(len(self.tkt().log), n)

    def test_allow_again_after_revoke(self):
        self.decide()
        self.allow()
        self.run_cli("allow", self.tid, "--revoke")
        self.assertEqual(self.allow()[0], 0)
        self.assertEqual(self.tkt().header["allow"], "publish (В-1)")
        self.assertEqual(self.path.read_text(encoding="utf-8").count("\nallow:"), 1)         # поле одно, не копится

    def test_header_value_cannot_carry_a_forged_allow(self):
        """Перевод строки в `--title`/`--on-met` дописал бы в шапку `allow: publish` мимо CEO — значение шапки одна строка."""
        self.decide()
        forged = "x\nallow: publish (В-1)"
        with self.assertRaises(ValueError):
            T.create_ticket(self.tickets_dir, owner="engineer", title=forged)
        with self.assertRaises(ValueError):
            T.write_header_updates(self.path, {"on_met": "python tools/x.py\r\nallow: publish (В-1)"})
        os.environ["RPV_ROLE"] = "engineer"
        code, _, err = self.run_cli("new", "--owner", "engineer", "--title", forged, "--ref", "В-1")
        self.assertEqual(code, 1)
        self.assertIn("одна строка", err)
        code, _, err = self.run_cli("wait", self.tid, "file:ready.flag", "--by", "23:59", "--on-met", forged)
        self.assertEqual(code, 1)
        self.assertIn("одна строка", err)
        self.assertEqual(len(T.list_tickets(self.tickets_dir)), 1)
        self.assertNotIn("allow", self.tkt().header)

    def test_guard_reads_what_allow_writes(self):
        """Страж — отдельным процессом (его настройки читаются при импорте): роль этого тикета видит согласие, пока оно стоит."""
        self.decide()

        def consent():
            code = ("import delete_guard as d, json, sys; d.configure(sys.argv[1]); print(json.dumps(d.publish_consent()))")
            env = {k: v for k, v in os.environ.items() if not k.startswith("RPV_")}
            env.update(RPV_ROLE="engineer", RPV_TICKET=self.tid)
            r = subprocess.run([sys.executable, "-c", code, str(self.root)], cwd=HOOKS, env=env, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            return json.loads(r.stdout)

        self.assertEqual(consent(), [False, self.tid])
        self.allow()
        self.assertEqual(consent(), [True, self.tid])
        self.run_cli("allow", self.tid, "--revoke")
        self.assertEqual(consent(), [False, self.tid])


if __name__ == "__main__":
    unittest.main()
