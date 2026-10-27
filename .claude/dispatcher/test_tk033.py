"""TK-033 (раунд ревью 2): запись уставов повторяется при `PermissionError` (общий цикл с `project.atomic_write_text`);
журналы `charter-log.json` и `rpv-config-log.json` читаются одной функцией `project.read_log`."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import charter  # noqa: E402
import project as P  # noqa: E402
import rpv_config as cfg_mod  # noqa: E402


class AtomicBytes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "proj"
        self.roles = self.root / ".claude" / "roles"
        self.roles.mkdir(parents=True)
        self.tpl = Path(self.tmp.name) / "plugin" / "templates" / "roles"
        self.tpl.mkdir(parents=True)
        (self.tpl.parent.parent / ".claude-plugin").mkdir()
        (self.tpl.parent.parent / ".claude-plugin" / "plugin.json").write_text('{"version": "9.9.9"}', encoding="utf-8")
        (self.tpl / "judge.md").write_bytes("# Судья\r\nновая\r\n".encode("utf-8"))
        (self.roles / "judge.md").write_text("старый", encoding="utf-8")

    def locked_once(self, times=1):
        real, left = os.replace, [times]

        def replace(src, dst):
            if left[0] > 0:                                   # Windows: файл держит читающая роль
                left[0] -= 1
                raise PermissionError(13, "занят")
            return real(src, dst)
        return mock.patch("os.replace", side_effect=replace), mock.patch("time.sleep")

    def test_charter_write_retries_while_a_role_holds_the_file(self):
        rep, sleep = self.locked_once(2)
        with rep, sleep:
            _, _, err = charter.sync(self.root, ["judge.md"], templates=self.tpl)
        self.assertIsNone(err)
        self.assertEqual((self.roles / "judge.md").read_bytes(), "# Судья\r\nновая\r\n".encode("utf-8"))
        self.assertEqual([p.name for p in self.roles.glob("*.tmp")], [])

    def test_a_file_held_for_good_stays_as_it_was_and_leaves_no_tmp(self):
        rep, sleep = self.locked_once(99)
        with rep, sleep, self.assertRaises(PermissionError):
            P.atomic_write_bytes(self.roles / "judge.md", b"x", retries=3)
        self.assertEqual((self.roles / "judge.md").read_text(encoding="utf-8"), "старый")
        self.assertEqual([p.name for p in self.roles.glob("*.tmp")], [])


class OneLogReader(unittest.TestCase):
    def test_both_journals_go_through_project_read_json(self):
        with mock.patch.object(P, "read_json", return_value=[{"a": 1}, 3, "x"]) as rj:
            self.assertEqual(charter.read_log(Path("p")), [{"a": 1}])
            self.assertEqual(cfg_mod.read_log(Path("p")), [{"a": 1}])
        self.assertEqual(rj.call_count, 2)

    def test_not_a_list_or_no_file_is_empty(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(P.read_log(Path(d) / "нет.json"), [])
            f = Path(d) / "obj.json"
            f.write_text('{"a": 1}', encoding="utf-8")
            self.assertEqual(P.read_log(f), [])


if __name__ == "__main__":
    unittest.main()
