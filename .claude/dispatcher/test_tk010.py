"""Тесты TK-010 (раунд ревью 1, правки Б): места, где копии одного и того же расходились, теперь читают одно значение."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ceo_inbox as CI  # noqa: E402
import dispatch as D  # noqa: E402
import doctor as DOC  # noqa: E402
import heartbeat as HB  # noqa: E402
import overseer as OV  # noqa: E402
import project as P  # noqa: E402
import report as RP  # noqa: E402
import routes as RT  # noqa: E402
import runs as RL  # noqa: E402
import supervise as SV  # noqa: E402
import ticket as T  # noqa: E402
import transcript as TR  # noqa: E402
import usage as US  # noqa: E402
import watch as W  # noqa: E402
import worktree_hygiene as WH  # noqa: E402

STATUSES = ("backlog", "todo", "in_progress", "waiting", "in_review", "blocked", "needs_owner", "stopped", "done", "cancelled")


class ClosedStatuses(unittest.TestCase):
    """«Закрыт»: watch.py знал done/cancelled, worktree_hygiene.py — done/stopped (`cancelled` как статус тикета нигде не ставится)."""

    def test_one_definition(self):
        self.assertEqual(T.CLOSED_STATUSES, ("done", "stopped"))
        for mod in (W, WH):
            self.assertFalse(hasattr(mod, "CLOSED") or hasattr(mod, "CLOSED_TICKET_STATUSES"), mod.__name__)

    def test_worktree_hygiene_uses_it(self):
        for st in STATUSES:
            with self.subTest(status=st):
                self.assertEqual(WH.is_closed(Path("x/tk007"), None, {"TK-007": st}), st in T.CLOSED_STATUSES)


class InboxMark(unittest.TestCase):
    """Отметка «прочитано» очереди CEO: хук читал `.ceo-inbox-seen`, `tickets.py inbox` — `.ceo-inbox-fallback-seen`."""

    def test_one_marker_name_in_code_and_gitignore(self):
        here = Path(__file__).resolve().parent
        self.assertEqual(CI.SEEN_NAME, ".ceo-inbox-seen")
        for src in (here / "tickets.py", here.parent / "hooks" / "role_memory.py"):
            self.assertNotIn("fallback-seen", src.read_text(encoding="utf-8"), src.name)
        self.assertIn(CI.SEEN_NAME, (here / ".gitignore").read_text(encoding="utf-8").split())

    def test_read_and_mark(self):
        d = Path(tempfile.mkdtemp())
        inbox, seen_f = d / "ceo-inbox.md", d / CI.SEEN_NAME
        self.assertEqual(CI.read(inbox, seen_f), ([], 0))
        inbox.write_text("a" + chr(10) + "b" + chr(10), encoding="utf-8")
        self.assertEqual(CI.read(inbox, seen_f), (["a", "b"], 0))
        CI.mark(seen_f, 2)
        self.assertEqual(CI.read(inbox, seen_f), (["a", "b"], 2))
        inbox.write_text("c" + chr(10), encoding="utf-8")  # файл усечён: отметка больше длины — с начала
        self.assertEqual(CI.read(inbox, seen_f), (["c"], 0))


def _row(ctx_in, out=0, side=False):
    row = {"type": "assistant", "message": {"usage": {
        "input_tokens": ctx_in, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "output_tokens": out}}}
    if side:
        row["isSidechain"] = True
    return json.dumps(row)


class UsageKeys(unittest.TestCase):
    """Контекст хода: диспетчер суммировал 3 ключа usage и пропускал `isSidechain`, хук — 4 (с выводом) и не пропускал."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        proj = self.dir / "slug"
        proj.mkdir()
        self.path = proj / "sess-1.jsonl"

    def write(self, *rows):
        self.path.write_text(chr(10).join(rows) + chr(10), encoding="utf-8")

    def test_one_definition_of_context(self):
        self.assertEqual(TR.CONTEXT_KEYS, ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
        self.assertIs(D._tokens_of, TR.context_of)
        self.assertEqual(TR.context_of({"input_tokens": 1, "cache_read_input_tokens": 2, "cache_creation_input_tokens": 4,
                                        "output_tokens": 800}), 7)

    def test_output_not_counted_and_sidechain_skipped(self):
        self.write(_row(100, out=7), _row(5000, out=1, side=True))
        self.assertEqual(TR.last_turn_tokens(self.path), 100)

    def test_dispatch_reads_through_it(self):
        self.write(_row(100, out=7), _row(5000, out=1, side=True))
        old, D.CLAUDE_PROJECTS_DIR = D.CLAUDE_PROJECTS_DIR, self.dir
        try:
            self.assertEqual(D._transcript_last_turn_tokens("sess-1"), 100)
            self.assertEqual(D._transcript_last_turn_tokens("../sess-1"), 0)
            self.assertEqual(D._transcript_last_turn_tokens("nope"), 0)
        finally:
            D.CLAUDE_PROJECTS_DIR = old

    def test_broken_rows_are_skipped(self):
        self.write(_row(100), '{"usage": [', '[1, 2, "usage"]', '{"message": "usage"}')
        self.assertEqual(TR.last_turn_tokens(self.path), 100)
        self.assertEqual(TR.last_turn_tokens(self.dir / "none.jsonl"), 0)


class HeartbeatStale(unittest.TestCase):
    """Свежесть сердцебиения: сторож служб ждал 600 с, хук подсказок — 240 с. Теперь порог один — `heartbeat.STALE_S`."""

    def test_one_threshold(self):
        self.assertEqual(HB.STALE_S, 600.0)
        self.assertEqual(SV.STALE_S, HB.STALE_S)
        self.assertIs(SV.heartbeat_age, HB.read_age)
        src = (Path(__file__).resolve().parent.parent / "hooks" / "role_memory.py").read_text(encoding="utf-8")
        self.assertNotIn("WATCH_STALE_S", src)
        self.assertIn("heartbeat.STALE_S", src)

    def test_age(self):
        now = datetime.now().astimezone()
        f = Path(tempfile.mkdtemp()) / "hb.json"
        f.write_text(json.dumps({"ts": (now - timedelta(seconds=90)).isoformat()}), encoding="utf-8")
        self.assertAlmostEqual(HB.read_age(f, "ts", now.timestamp()), 90, delta=1)
        f.write_text(json.dumps({"ts": (now - timedelta(seconds=90)).isoformat().replace("+00:00", "Z")}), encoding="utf-8")
        self.assertIsNotNone(HB.read_age(f, "ts", now.timestamp()))
        self.assertIsNone(HB.read_age(f.with_name("none.json"), "ts", now.timestamp()))

    def test_decide_and_doctor_share_it(self):
        self.assertEqual(SV.decide(HB.STALE_S - 1, True), "ok")
        self.assertEqual(SV.decide(HB.STALE_S + 1, True), "restart")
        self.assertIs(DOC.SV, SV)


class RunsLogLine(unittest.TestCase):
    """Четыре читателя runs.log (overseer, report, usage, watch) разбирают строку одним `runs.split`/`runs.costs`."""
    TS = "2026-10-27T05:00:00+04:00"

    def test_one_threshold_and_cost_rule(self):
        short = f"{self.TS} TK-1 engineer"
        self.assertIsNone(RL.split(short))
        self.assertEqual(OV.parse_runs(short), {})
        self.assertEqual(RP.read_runs(self._root(short)), [])
        self.assertIsNone(US.parse(short))
        # resolved_cost=0 — законный ноль запуска (при --resume), а не «нет цены»: итог сессии cost_usd не подставляется
        line = f"{self.TS} TK-1 engineer reason=new status=ok cost_usd=5.0 resolved_cost=0.0"
        self.assertEqual(RL.costs(RL.split(line)[3]), (0.0, 5.0))
        self.assertEqual(OV.parse_runs(line)["TK-1"][0].cost, 0.0)
        self.assertEqual(RP.read_runs(self._root(line))[0]["cost"], 0.0)
        broken = f"{self.TS} TK-1 engineer reason=new status=ok cost_usd=2.5 resolved_cost=abc"
        self.assertEqual(OV.parse_runs(broken)["TK-1"][0].cost, 2.5)
        self.assertEqual(RP.read_runs(self._root(broken))[0]["cost"], 2.5)

    def test_on_met_is_not_a_run(self):
        line = f"{self.TS} TK-1 on_met reason=x status=ok"
        self.assertEqual(OV.parse_runs(line), {})
        self.assertEqual(RP.read_runs(self._root(line)), [])

    def test_no_private_copies(self):
        for name in ("overseer", "report", "usage", "watch"):
            src = (Path(__file__).resolve().parent / f"{name}.py").read_bytes().decode("utf-8")
            self.assertNotIn("RUN_KV", src, name)
            self.assertNotIn("_KV = re", src, name)

    @staticmethod
    def _root(line: str) -> Path:
        root = Path(tempfile.mkdtemp())
        d = root / ".claude" / "dispatcher"
        d.mkdir(parents=True)
        (d / "runs.log").write_text(line + "\n", encoding="utf-8")
        return root


class RolesOneTuple(unittest.TestCase):
    """Роли, которых запускает диспетчер, — кортеж `project.ROLES`; в остальных модулях его литерала нет."""

    def test_role_tokens_follow_roles(self):
        self.assertEqual(T.ROLE_TOKENS, (*P.ROLES, "ceo"))

    def test_routes_follow_roles(self):
        for role in P.ROLES:
            self.assertTrue(RT.verbs_for(role), role)
        self.assertEqual(RT.verbs_for("ceo"), list(RT.RESULTS))

    def test_no_literal_copies(self):
        here = Path(__file__).resolve().parent
        literal = '"researcher", "engineer", "judge"'
        for path in sorted(here.glob("*.py")) + sorted((here.parent / "hooks").glob("*.py")):
            if path.name.startswith("test_") or path.name in ("project.py", "delete_guard.py"):
                continue  # страж — отдельный файл с отказом по умолчанию, без импорта диспетчера
            self.assertNotIn(literal, path.read_bytes().decode("utf-8"), path.name)


class IsoTime(unittest.TestCase):
    """Отметка времени и разбор ISO — в project.py; пояс для времени без пояса назван у каждого читателя."""

    def test_now_iso_one_format(self):
        stamp = P.now_iso()
        self.assertRegex(stamp, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d$")
        self.assertIs(T.now_iso, P.now_iso)
        at = datetime(2026, 10, 27, 5, 0, 0).astimezone()
        self.assertEqual(P.now_iso(at), at.isoformat(timespec="seconds"))

    def test_naive_zone_is_explicit(self):
        naive = "2026-10-27T05:00:00"
        self.assertEqual(T.parse_dt(naive).utcoffset(), timedelta(0))                 # записи тикета: UTC
        self.assertEqual(OV.parse_ts(naive).utcoffset(), OV.DEFAULT_TZ.utcoffset(None))  # Надзиратель: GMT+4
        local = datetime(2026, 10, 27, 5, 0, 0).astimezone().utcoffset()
        self.assertEqual(T.parse_at(naive).utcoffset(), local)                         # at:, отчёт: местное
        self.assertEqual(RP._ts(naive).utcoffset(), local)

    def test_zone_given_and_garbage(self):
        for text in ("2026-10-27T05:00:00+03:00", "2026-10-27T02:00:00Z"):
            want = datetime.fromisoformat(text.replace("Z", "+00:00"))
            for parse in (T.parse_dt, OV.parse_ts, T.parse_at, RP._ts):
                self.assertEqual(parse(text), want, (parse, text))
        self.assertIsNone(T.parse_at("не время"))
        self.assertIsNone(RP._ts(None))
        with self.assertRaises(ValueError):
            T.parse_dt("не время")
        with self.assertRaises(ValueError):
            OV.parse_ts("не время")

    def test_no_private_copies(self):
        here = Path(__file__).resolve().parent
        for name in ("ci_watch", "errors", "notify", "start", "supervise", "ticket"):
            src = (here / f"{name}.py").read_bytes().decode("utf-8")
            self.assertNotIn("astimezone().isoformat(timespec", src, name)


class ConstantsOnce(unittest.TestCase):
    """Флаги создания процесса — в hide.py, версия X.Y.Z — в project.SEMVER; других определений нет."""

    def test_definitions_once(self):
        here = Path(__file__).resolve().parent
        for token in ("CREATE_NO_WINDOW = ", "CREATE_NEW_PROCESS_GROUP = ", "SEMVER = "):
            owners = [p.name for p in sorted(here.glob("*.py"))
                      if not p.name.startswith("test_") and token in p.read_bytes().decode("utf-8")]
            self.assertEqual(len(owners), 1, (token, owners))

    def test_readers_use_the_one(self):
        import release as REL
        import start as ST
        self.assertFalse(hasattr(ST, "CREATE_NO_WINDOW"))
        self.assertTrue(P.SEMVER.fullmatch("2.0.7"))
        self.assertFalse(hasattr(REL, "SEMVER") or hasattr(RP, "SEMVER"))


class AtomicJsonOnce(unittest.TestCase):
    """Запись состояния — `project.write_json` (indent=2, повтор при PermissionError), чтение — `project.read_json`."""

    def test_write_read_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "a" / "s.json"
            P.write_json(p, {"б": 1, "а": [1]}, sort_keys=True)
            self.assertEqual(p.read_bytes().decode("utf-8"), '{\n  "а": [\n    1\n  ],\n  "б": 1\n}\n')
            self.assertEqual(P.read_json(p), {"а": [1], "б": 1})
            self.assertEqual(list(p.parent.glob("*.tmp")), [])

    def test_read_missing_or_broken(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.json"
            self.assertEqual(P.read_json(p, {}), {})
            p.write_text("{нет", encoding="utf-8")
            self.assertIsNone(P.read_json(p))

    def test_replace_retries_permission_error(self):
        import os
        from unittest import mock
        real = os.replace
        calls = []

        def flaky(a, b):
            calls.append(1)
            if len(calls) < 3:
                raise PermissionError("занят")
            return real(a, b)
        with tempfile.TemporaryDirectory() as d, mock.patch("os.replace", flaky), mock.patch("time.sleep"):
            p = Path(d) / "s.json"
            P.write_json(p, {"x": 1})
            self.assertEqual(P.read_json(p), {"x": 1})
        self.assertEqual(len(calls), 3)

    def test_no_private_writers(self):
        here = Path(__file__).resolve().parent
        for name in ("cfg", "overseer", "tickets", "dispatch", "ci_watch", "merge_rule", "lifewatch"):
            src = (here / f"{name}.py").read_bytes().decode("utf-8")
            self.assertNotIn("os.replace(tmp", src.replace("os.replace(tmp, pid_file)", ""), name)
            self.assertNotIn("indent=1", src, name)
        self.assertIs(T.atomic_write_text, P.atomic_write_text)


class GitOnce(unittest.TestCase):
    """git из диспетчера — только `hide.git` (скрытое окно, предел времени); частных обёрток и голых вызовов нет."""

    def test_no_bare_git_calls(self):
        import re
        here = Path(__file__).resolve().parent
        for f in sorted(here.glob("*.py")):
            if f.name.startswith("test_") or f.name == "hide.py":
                continue
            self.assertIsNone(re.search(r"""\[\s*["']git["']""", f.read_text(encoding="utf-8")), f.name)

    def test_hide_git_on_real_repo(self):
        import hide
        import subprocess
        with tempfile.TemporaryDirectory() as d:
            subprocess.run(["git", "init", "-q", d], check=True)
            r = hide.git(d, "rev-parse", "--is-inside-work-tree")
            self.assertEqual((r.returncode, r.stdout.strip()), (0, b"true"))
            self.assertEqual(hide.git(d, "rev-parse", "--is-inside-work-tree", text=True).stdout.strip(), "true")
            self.assertNotEqual(hide.git(d, "cat-file", "-e", "HEAD:нет").returncode, 0)

    def test_release_and_routes_use_it(self):
        import hide
        import release as REL
        import subprocess
        self.assertIs(REL._git.__kwdefaults__["run"], hide.git)
        with tempfile.TemporaryDirectory() as d:
            subprocess.run(["git", "init", "-q", d], check=True)
            (Path(d) / "f.txt").write_text("x", encoding="utf-8")
            self.assertEqual(RT.tracked_at_head(Path(d), "f.txt")[0], "")
            subprocess.run(["git", "-C", d, "add", "f.txt"], check=True)
            subprocess.run(["git", "-C", d, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "x"], check=True)
            sha, why = RT.tracked_at_head(Path(d), "f.txt")
            self.assertTrue(sha and not why, (sha, why))


class ProcessScanOnce(unittest.TestCase):
    """Таблица процессов читается одним сканером `project.scan_processes` с одним пределом времени (30 с)."""

    def test_sees_own_process_with_parent_and_cmdline(self):
        rows = {pid: (ppid, cmd) for pid, ppid, cmd in P.scan_processes()}
        self.assertIn(os.getpid(), rows)
        self.assertEqual(rows[os.getpid()][0], os.getppid())
        self.assertIn("python", rows[os.getpid()][1].lower())
        self.assertEqual(P._parent_map()[os.getpid()], os.getppid())

    def test_cmdline_false_gives_pairs(self):
        self.assertIn(os.getpid(), {pid for pid, _, _ in P.scan_processes(cmdline=False)})

    def test_one_default_timeout_and_no_private_scanners(self):
        import start as S
        self.assertEqual(P.PROC_SCAN_TIMEOUT_S, 30.0)
        here = Path(__file__).resolve().parent
        for name in ("dispatch", "start", "doctor", "supervise"):
            src = (here / f"{name}.py").read_text(encoding="utf-8")
            self.assertNotIn("Get-CimInstance Win32_Process |", src, name)
            self.assertNotIn('"-eo"', src, name)
            self.assertNotIn('"-axo"', src, name)
        self.assertIn(os.getpid(), {p for p, _ in S._list_procs()})


if __name__ == "__main__":
    unittest.main()
