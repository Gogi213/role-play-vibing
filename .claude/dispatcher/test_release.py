"""Выпуск и откат (TK-076 п.5): версии согласованы, bump/update/rollback — по последовательности команд claude."""
import json
import os
import re
import subprocess
import time
import tempfile
import unittest
from unittest import mock
from pathlib import Path

import release


class FakeClaude:
    def __init__(self, versions, fail_on=None, tags=("1.7.1",), pinned=None):
        self.versions, self.calls, self.fail_on, self.tags, self.pinned = list(versions), [], fail_on, tags, pinned

    def __call__(self, cmd, **kw):
        self.calls.append(cmd[1:])
        if cmd[1:3] == ["plugin", "list"]:
            v = self.versions[0] if len(self.versions) == 1 else self.versions.pop(0)
            return subprocess.CompletedProcess(cmd, 0, f"  ❯ {release.NAME}@{release.NAME}\n    Version: {v}\n", "")
        if cmd[0] == "git":
            out = "".join(f"abc\trefs/tags/v{t}\n" for t in self.tags if cmd[-1] == f"refs/tags/v{t}")
            return subprocess.CompletedProcess(cmd, 0, out, "")
        if cmd[1:4] == ["plugin", "marketplace", "list"]:
            ref = f"@{self.pinned}" if self.pinned else ""
            return subprocess.CompletedProcess(cmd, 0, f"  ❯ {release.NAME}\n    Source: GitHub ({release.REPO}{ref})\n", "")
        return subprocess.CompletedProcess(cmd, 1 if cmd[1:4] == self.fail_on else 0, "", "boom")


def make_root(d: Path, version="1.0.0", changelog="## 1.0.0\n"):
    (d / ".claude-plugin").mkdir()
    (d / ".claude-plugin" / "plugin.json").write_text(json.dumps({"version": version}), encoding="utf-8")
    (d / ".claude-plugin" / "marketplace.json").write_text(json.dumps({"plugins": [{"version": version}]}), encoding="utf-8")
    (d / "CHANGELOG.md").write_text("# Changelog\n\n" + changelog, encoding="utf-8")


class ReleaseTests(unittest.TestCase):
    def test_repo_versions_consistent(self):
        self.assertIsNone(release.check())

    def test_bump_requires_changelog_and_updates_both_files(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            make_root(root)
            with self.assertRaises(SystemExit):
                release.bump("1.1.0", root)
            (root / "CHANGELOG.md").write_text("# C\n\n## 1.1.0\n- x\n\n## 1.0.0\n", encoding="utf-8")
            release.bump("1.1.0", root)
            self.assertEqual(release.read_versions(root), {"plugin": "1.1.0", "marketplace": "1.1.0", "changelog": "1.1.0"})
            with self.assertRaises(SystemExit):
                release.bump("v1.2", root)

    def test_update_records_previous_version(self):
        with tempfile.TemporaryDirectory() as t:
            st = Path(t) / "s.json"
            fake = FakeClaude(["1.7.1", "1.8.0"])
            self.assertEqual(release.update(fake, st), 0)
            self.assertEqual(json.loads(st.read_text())["previous"], "1.7.1")
            self.assertIn(["plugin", "update", f"{release.NAME}@{release.NAME}"], fake.calls)

    def test_rollback_uses_recorded_previous_and_tag_ref(self):
        with tempfile.TemporaryDirectory() as t:
            st = Path(t) / "s.json"
            st.write_text(json.dumps({"previous": "1.7.1"}))
            fake = FakeClaude(["1.8.0", "1.7.1"])
            self.assertEqual(release.rollback(None, fake, st), 0)
            self.assertIn(["plugin", "marketplace", "add", f"{release.REPO}#v1.7.1"], fake.calls)
            self.assertEqual(json.loads(st.read_text())["previous"], "1.8.0")  # откат обратим

    def test_rollback_without_target_refuses_and_failure_reported(self):
        with tempfile.TemporaryDirectory() as t:
            st = Path(t) / "none.json"
            self.assertEqual(release.rollback(None, FakeClaude(["1"]), st), 2)

    def test_rollback_missing_tag_touches_nothing(self):
        with tempfile.TemporaryDirectory() as t:
            fake = FakeClaude(["1.8.0"], tags=())
            self.assertEqual(release.rollback("1.7.1", fake, Path(t) / "s.json"), 3)
            self.assertFalse([c for c in fake.calls if c[:3] == ["plugin", "marketplace", "remove"]])

    def test_rollback_failure_restores_previous_marketplace(self):
        with tempfile.TemporaryDirectory() as t:
            fake = FakeClaude(["1.8.0"], fail_on=["plugin", "install", f"{release.NAME}@{release.NAME}"])
            self.assertEqual(release.rollback("1.7.1", fake, Path(t) / "s.json"), 1)
            adds = [c for c in fake.calls if c[:3] == ["plugin", "marketplace", "add"]]
            self.assertEqual(adds[0], ["plugin", "marketplace", "add", f"{release.REPO}#v1.7.1"])
            self.assertEqual(adds[1:], [["plugin", "marketplace", "add", release.REPO]] * 3)   # возврат прежнего — с повторами

    def test_update_failure_restores_pinned_marketplace(self):  # add после remove упал — прежний маркетплейс возвращается
        fake = FakeClaude(["1.8.0"], fail_on=["plugin", "marketplace", "add"], pinned="v1.8.0")
        self.assertEqual(release.update(fake, Path(tempfile.gettempdir()) / "x.json"), 1)
        adds = [c for c in fake.calls if c[:3] == ["plugin", "marketplace", "add"]]
        self.assertEqual(adds, [["plugin", "marketplace", "add", release.REPO]] + [["plugin", "marketplace", "add", f"{release.REPO}#v1.8.0"]] * 3)

    def test_install_uses_project_scope_when_installed_there(self):
        with tempfile.TemporaryDirectory() as t:
            reg = Path(t) / "r.json"
            reg.write_text(json.dumps({"plugins": {f"{release.NAME}@{release.NAME}": [{"scope": "project"}]}}))
            self.assertEqual(release._scope_args(reg), ["--scope", "project"])
            reg.write_text(json.dumps({"plugins": {f"{release.NAME}@{release.NAME}": [{"scope": "user"}]}}))
            self.assertEqual(release._scope_args(reg), [])

    def test_update_unpins_marketplace_from_tag(self):
        with tempfile.TemporaryDirectory() as t:
            fake = FakeClaude(["1.7.1", "1.8.0"], pinned="v1.7.1")
            self.assertEqual(release.update(fake, Path(t) / "s.json"), 0)
            self.assertIn(["plugin", "marketplace", "add", release.REPO], fake.calls)
            self.assertNotIn(["plugin", "marketplace", "add", f"{release.REPO}#v1.7.1"], fake.calls)

    def test_update_on_main_does_not_reinstall(self):
        with tempfile.TemporaryDirectory() as t:
            fake = FakeClaude(["1.7.1", "1.8.0"])
            release.update(fake, Path(t) / "s.json")
            self.assertFalse([c for c in fake.calls if c[:3] == ["plugin", "marketplace", "remove"]])


class AutoReleaseTests(unittest.TestCase):
    """TK-094: автовыпуск — update, службы заново, проверка; провал → откат + сигнал владельцу."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.proj = Path(self.tmp.name)
        self.st = self.proj / "s.json"
        self.reg = self.proj / "installed.json"
        self.reg.write_text(json.dumps({"plugins": {f"{release.NAME}@{release.NAME}": [
            {"scope": "project", "projectPath": str(self.proj), "installPath": str(self.proj / "plug")}]}}), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def journal(self):
        f = self.proj / ".claude" / "dispatcher" / "release-journal.jsonl"
        return [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines()] if f.exists() else []

    def notices(self):
        qs = sorted((self.proj / ".claude" / "pulse" / "questions").glob("q-release-*.json"))
        return [json.loads(p.read_text(encoding="utf-8")) for p in qs]

    def test_notice_visible_on_dispatcher_and_answerable(self):  # сигнал выпуска виден на Диспетчерской, снимается ответом
        self.auto(["1.8.3", "1.8.4", "1.8.4"], alive=False, tags=("1.8.3",))
        import view2, ask
        qs = self.notices()
        v = view2.make({}, {}, qs, time.time())
        self.assertIn("rolled-back", v["questions"][0]["text"])
        qdir = self.proj / ".claude" / "pulse" / "questions"
        with mock.patch.object(ask.P, "questions_dir", return_value=qdir):
            ok, msg, _ = ask.answer_question(qs[0]["id"], "a")
        self.assertTrue(ok)
        self.assertFalse(view2.make({}, {}, self.notices(), time.time())["questions"])

    def auto(self, versions, alive, **kw):
        restarts = []
        fake = FakeClaude(versions, **kw)
        rc = release.autorelease(self.proj, fake, self.st, self.reg,
                                 restart=lambda p, d, run: restarts.append(d) or True, verify=lambda p, d, run: alive)
        return rc, fake, restarts

    def test_ok_restarts_services_and_journals(self):
        rc, fake, restarts = self.auto(["1.8.3", "1.8.4"], alive=True)
        self.assertEqual((rc, len(restarts), [j["result"] for j in self.journal()]), (0, 1, ["ok"]))
        self.assertEqual(self.notices(), [])

    def test_same_version_is_not_silent(self):  # влит PR без bump: службы не трогаем, но журнал и «ждёт вас»
        rc, fake, restarts = self.auto(["1.8.3"], alive=True)
        self.assertEqual((rc, restarts, [j["result"] for j in self.journal()]), (0, [], ["no-bump"]))
        self.assertIn("no-bump", self.notices()[0]["text"])

    def test_failed_check_rolls_back_and_tells_owner(self):
        rc, fake, restarts = self.auto(["1.8.3", "1.8.4", "1.8.4"], alive=False, tags=("1.8.3",))
        self.assertEqual(rc, 2)  # проверка не прошла и после отката (verify всегда False)
        self.assertIn(["plugin", "marketplace", "add", f"{release.REPO}#v1.8.3"], fake.calls)
        self.assertEqual(len(restarts), 2)  # службы подняты заново и после отката
        self.assertEqual(self.journal()[-1]["result"], "rolled-back")
        self.assertIn("rolled-back", self.notices()[0]["text"])

    def test_rollback_failure_is_reported(self):
        rc, fake, restarts = self.auto(["1.8.3", "1.8.4"], alive=False, tags=())  # тега прошлой версии нет
        self.assertEqual((rc, self.journal()[-1]["result"]), (2, "rollback-failed"))

    def test_clean_env_drops_session_keeps_service_settings(self):
        env = release.clean_env({"RPV_ROLE": "engineer", "RPV_TICKET": "TK-1", "ALPHA_ROLE": "x", "ALPHA_TICKET": "y",
                                 "CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli", "HOST_SESSION": "1", "PATH": "p",
                                 "ALPHA_DISPATCH_MAX_PARALLEL": "8", "ALPHA_DISPATCH_ROLE_PARALLEL": "engineer:6",
                                 "RPV_DISPATCH_X": "1"})
        self.assertEqual(env, {"PATH": "p", "ALPHA_DISPATCH_MAX_PARALLEL": "8", "ALPHA_DISPATCH_ROLE_PARALLEL": "engineer:6",
                               "RPV_DISPATCH_X": "1"})

    def test_clean_env_matches_supervise_session_env(self):  # supervise отказывает ровно по SESSION_ENV — чистим их
        import supervise
        self.assertTrue(set(supervise.SESSION_ENV) <= set(release.SESSION_DROP))

    def test_parallel_auto_serialized_by_lock(self):  # второй выпуск при живом первом — пометка, держатель доделает
        runs = []
        lock, pending = release._auto_paths(self.proj)
        lock.parent.mkdir(parents=True, exist_ok=True)

        def first(p):
            runs.append("a")
            if len(runs) == 1:
                self.assertEqual(release.run_auto_locked(p, lambda q: runs.append("nested") or 0), 0)  # занят → в очередь
                self.assertTrue(pending.exists())
            return 0

        self.assertEqual(release.run_auto_locked(self.proj, first), 0)
        self.assertEqual(runs, ["a", "a"])  # держатель прошёл ещё раз; вложенный сам update не запускал
        self.assertFalse(lock.exists() or pending.exists())

    def test_stale_lock_is_taken_over(self):
        lock, _ = release._auto_paths(self.proj)
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.touch()
        old = lock.stat().st_mtime - release.LOCK_STALE_S - 1
        os.utime(lock, (old, old))
        self.assertTrue(release._lock_acquire(lock))

    def test_installed_dir_prefers_project_scope(self):
        self.assertEqual(release.installed_dir(self.proj, self.reg), self.proj / "plug")
        self.assertIsNone(release.installed_dir(self.proj, self.proj / "none.json"))

    def test_spawn_auto_detached_and_switchable(self):
        calls = []
        self.assertTrue(release.spawn_auto(self.proj, popen=lambda *a, **k: calls.append((a, k))))
        self.assertIn("auto", calls[0][0][0])
        old = os.environ.get("RPV_AUTORELEASE")
        os.environ["RPV_AUTORELEASE"] = "0"
        try:
            self.assertFalse(release.spawn_auto(self.proj, popen=lambda *a, **k: calls.append(1)))
        finally:
            os.environ.pop("RPV_AUTORELEASE") if old is None else os.environ.update(RPV_AUTORELEASE=old)
        self.assertEqual(len(calls), 1)


class VerifyAliveTests(unittest.TestCase):
    ROWS = [{"check": "диспетчер", "status": "OK", "detail": "x"},
            {"check": "простой сегодня", "status": "FAIL", "detail": "84 мин из 10"}]

    def fake_doctor(self, rows):
        return lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, json.dumps(rows), "")

    def test_idle_fail_alone_does_not_fail_release(self):
        """Б1: doctor любой версии (и старый, без --alive) может вернуть FAIL по простою — выпуск от этого не откатывается."""
        self.assertTrue(release.verify_alive(Path("."), Path("."), run=self.fake_doctor(self.ROWS), wait_s=0))

    def test_service_fail_fails_release(self):
        rows = self.ROWS + [{"check": "сторож", "status": "FAIL", "detail": "не запущен"}]
        self.assertFalse(release.verify_alive(Path("."), Path("."), run=self.fake_doctor(rows), wait_s=0))
        self.assertFalse(release.verify_alive(Path("."), Path("."), run=lambda c, **k: subprocess.CompletedProcess(c, 1, "boom", ""), wait_s=0))


class TimeoutAndAtomicRollbackTests(unittest.TestCase):
    def test_run_timeout_returns_124_not_hang(self):
        """Б3: git/claude без предела держали клон часами."""
        def hang(cmd, **kw):
            self.assertIn("timeout", kw)
            raise subprocess.TimeoutExpired(cmd, kw["timeout"])
        r = release._run(["git", "clone", "x"], hang)
        self.assertEqual(r.returncode, 124)

    def test_rollback_touches_nothing_when_target_not_fetchable(self):
        """Б2: сбой GitHub при добыче цели — маркетплейс и плагин не тронуты (не remove)."""
        class NoClone(FakeClaude):
            def __call__(self, cmd, **kw):
                if cmd[:2] == ["git", "clone"]:
                    self.calls.append(cmd[1:])
                    return subprocess.CompletedProcess(cmd, 128, "", "fetch failed")
                return super().__call__(cmd, **kw)
        with tempfile.TemporaryDirectory() as t:
            fake = NoClone(["1.8.4"], tags=("1.8.3",))
            self.assertEqual(release.rollback("1.8.3", fake, Path(t) / "s.json"), 1)
            self.assertFalse([c for c in fake.calls if c[:3] == ["plugin", "marketplace", "remove"]])
            self.assertFalse([c for c in fake.calls if c[:3] == ["plugin", "marketplace", "add"]])


class ChangelogTagTests(unittest.TestCase):
    ROOT = Path(__file__).resolve().parents[2]

    def headings(self):
        return re.findall(r"^## (\d+\.\d+\.\d+)", (self.ROOT / "CHANGELOG.md").read_text(encoding="utf-8"), re.M)

    def test_manifest_version_is_top_changelog_entry(self):
        ver = json.loads((self.ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["version"]
        self.assertEqual(self.headings()[0], ver)

    def test_every_tag_has_changelog_entry(self):
        r = subprocess.run(["git", "tag", "--list", "v*"], cwd=self.ROOT, capture_output=True, text=True)
        tags = [t[1:] for t in r.stdout.split() if re.fullmatch(r"v\d+\.\d+\.\d+", t)]
        if r.returncode or not tags:
            self.skipTest("тегов нет (клон без тегов)")
        # 1.8.4 выпущена без тега — исторический пропуск; тег без записи — ошибка
        self.assertEqual([t for t in tags if t not in self.headings()], [])


if __name__ == "__main__":
    unittest.main()
