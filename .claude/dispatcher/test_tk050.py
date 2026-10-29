"""TK-050 (В-14): ярлыки доказательства в договоре; приёмка Судьи сама перезапускает команды ярлыков тест и запуск."""
from __future__ import annotations

import contextlib
import io
import unittest
from unittest import mock

import test_result as TR  # первым: задаёт песочницу CLAUDE_PROJECT_DIR
import errors as ER
import tickets as TK
import tickets_proof as TP
import ticket as T

CHK = "tools/chk.py"   # код 4, пока рядом лежит red.flag, иначе 0


class ProofTests(unittest.TestCase):
    setUp, tearDown, res, tkt, refused, _git = (TR.ResultTests.setUp, TR.ResultTests.tearDown, TR.ResultTests.res,
                                                TR.ResultTests.tkt, TR.ResultTests.refused, TR.ResultTests._git)

    def _repo(self):
        self._git("init", "-q")
        (self.base / "tools").mkdir()
        (self.base / CHK).write_text("import sys\nfrom pathlib import Path\nprint('вывод проверки')\n"
                                     "sys.exit(4 if Path('red.flag').exists() else 0)\n", encoding="utf-8")
        self._git("add", CHK)
        self._git("commit", "-qm", "chk")

    def _contract(self, text, author="engineer"):
        with contextlib.redirect_stdout(io.StringIO()):
            TK.cmd_comment(type("A", (), dict(id=self.tid, author=author, text=text, next=None))())

    def _ready(self, contract):
        """git-репо с tools/chk.py, договор исполнителя, его done и файл приёмки."""
        self._repo()
        (self.base / "out.md").write_text("x", encoding="utf-8")
        (self.base / "review.md").write_text("x", encoding="utf-8")
        self._contract(contract)
        self.res("engineer", "done", path="out.md")

    def accept(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = self.res("judge", "accept", path="review.md")
        return rc, err.getvalue()

    def test_accepts_when_command_exits_zero(self):
        self._ready(f"Договор. Примут, если: 1. [тест: python {CHK}] проверка зелёная; 2. [чтение кода] вызов один")
        rc, _ = self.accept()
        self.assertEqual(rc, 0)
        self.assertIn("проверки договора: 1 кмд, код 0", self.tkt().log[-1].text)

    def test_refuses_when_command_exits_nonzero_and_writes_nothing(self):
        self._ready(f"Договор: [запуск: python {CHK}] выдаёт 0")
        (self.base / "red.flag").write_text("x", encoding="utf-8")
        before = self.path.read_text(encoding="utf-8")
        rc, err = self.accept()
        self.assertEqual(rc, 1)
        self.assertIn(f"команда договора `python {CHK}` упала", err)
        self.assertIn("код 4", err)
        self.assertIn("вывод проверки", err)
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)

    def test_newer_failure_cancels_earlier_success(self):
        self._ready(f"Договор: [тест: python {CHK}]")
        tkt = self.tkt()
        self.assertEqual(TP.proof_ack(self.tid, tkt, self.base)[0], "")
        (self.base / "red.flag").write_text("x", encoding="utf-8")
        self.assertIn("упала", TP.proof_ack(self.tid, tkt, self.base)[0])
        (self.base / "red.flag").unlink()
        self.assertEqual(TP.proof_ack(self.tid, tkt, self.base)[0], "")

    def test_label_without_command_refused(self):
        for label in ("[тест]", "[запуск]", "[тест: ]", "[запуск:]"):
            with self.subTest(label=label):
                self.setUp()
                try:
                    self._ready(f"Договор: 1. {label} всё работает")
                    rc, err = self.accept()
                    self.assertEqual(rc, 1)
                    self.assertIn("без команды проверки", err)
                finally:
                    self.tearDown()

    def test_reading_label_needs_no_command_and_old_contract_accepted_as_before(self):
        for contract in ("Договор: 1. [чтение кода] правка в одном месте", "Договор: 1. функция зовётся один раз; 2. тесты зелёные"):
            with self.subTest(contract=contract):
                self.setUp()
                try:
                    self._ready(contract)
                    rc, _ = self.accept()
                    self.assertEqual(rc, 0)
                    self.assertNotIn("проверки договора", self.tkt().log[-1].text)
                finally:
                    self.tearDown()

    def test_command_outside_allowed_forms_refused(self):
        self._ready("Договор: [запуск: python -c \"print(1)\"] печатает 1")
        rc, err = self.accept()
        self.assertEqual(rc, 1)
        self.assertIn("не допущена", err)

    def test_pr_accept_runs_each_command_once_and_refuses_on_failure(self):
        self._ready(f"Договор: [тест: python {CHK}]")
        T.write_header_updates(self.path, {"status": "in_review"}, now=TR.NOW)
        (self.base / "red.flag").write_text("x", encoding="utf-8")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.refused("judge", "accept", pr=7, sha=TR.SHA, decisions="-", numbers="-")
        self.assertIn("упала", err.getvalue())
        (self.base / "red.flag").unlink()
        real = ER.run_check
        with mock.patch.object(ER, "run_check", side_effect=real) as spy, contextlib.redirect_stdout(io.StringIO()):
            rc = self.res("judge", "accept", pr=7, sha=TR.SHA, decisions="-", numbers="-")
        self.assertEqual((rc, spy.call_count), (0, 1))
        self.assertIn("проверки договора: 1 кмд, код 0", self.tkt().log[-2].text)   # запись «ПРИНЯТО»

    def test_labels_come_from_latest_labelled_owner_entry_only(self):
        self._repo()
        self._contract(f"Договор: [тест: python {CHK}] [запуск: python {CHK}]")
        self._contract("[итог: pr] PR #7 — в нём цитата [тест: python tools/нет.py]")
        self._contract("замечание Судьи про [тест: python tools/судья.py]", author="judge")
        labels = TP.contract_labels(self.tkt())
        self.assertEqual(labels, [("тест", f"python {CHK}"), ("запуск", f"python {CHK}")])
        self._contract("Договор заново: [чтение кода] только чтение")
        self.assertEqual(TP.contract_labels(self.tkt()), [("чтение", "")])

    def test_backtick_command_may_contain_bracket(self):
        self._contract('Договор: [тест: `python -m pytest a.py -k "x[1]"`] зелёный')
        self.assertEqual(TP.contract_labels(self.tkt()), [("тест", 'python -m pytest a.py -k "x[1]"')])


if __name__ == "__main__":
    unittest.main()
