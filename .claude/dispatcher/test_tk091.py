"""TK-091: предсдаточные отказы `result pr|done` — конфликт с main и непрочитанная поздняя запись CEO/Судьи."""
from __future__ import annotations

import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_result as TR  # noqa: E402,I001  первым: он задаёт песочницу проекта до импорта остальных модулей
from test_result import NOW, SHA  # noqa: E402
import precheck as PC  # noqa: E402
import project as P  # noqa: E402
import project_paths as CFG  # noqa: E402
import ticket as T  # noqa: E402

ORDER = "Заказ изменён: сначала проверь второй путь записи в watch.py, потом сдавай"


def git(cwd, *argv):
    subprocess.run(["git", "-C", str(cwd), *argv], check=True, capture_output=True, text=True, encoding="utf-8")


def commit(cwd, name, text):
    (Path(cwd) / name).write_text(text, encoding="utf-8")
    git(cwd, "add", name)
    git(cwd, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", f"{name} {text}")


def head(cwd) -> str:
    return subprocess.run(["git", "-C", str(cwd), "rev-parse", "HEAD"], check=True, capture_output=True,
                          text=True).stdout.strip()


class MergeConflictTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rpv-tk091-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = Path(self.tmp)
        git(self.repo, "init", "-q", "-b", "main")
        commit(self.repo, "a.txt", "base\n")
        git(self.repo, "checkout", "-q", "-b", "eng/x")

    def test_clean_branch_passes(self):
        commit(self.repo, "b.txt", "mine\n")
        git(self.repo, "checkout", "-q", "main")
        commit(self.repo, "c.txt", "other\n")
        self.assertEqual(PC.merge_conflict(self.repo, self._branch_head(), fetch=False), "")

    def test_conflict_names_file_and_hint(self):
        commit(self.repo, "a.txt", "mine\n")
        sha = head(self.repo)
        git(self.repo, "checkout", "-q", "main")
        commit(self.repo, "a.txt", "other\n")
        err = PC.merge_conflict(self.repo, sha, fetch=False)
        self.assertIn("не сливается с main", err)
        self.assertIn("a.txt", err)
        self.assertIn("git merge main", err)

    def test_unknown_head_or_no_repo_is_not_refusal(self):
        self.assertEqual(PC.merge_conflict(self.repo, "f" * 40, fetch=False), "")
        self.assertEqual(PC.merge_conflict(Path(tempfile.gettempdir()) / "rpv-no-repo-tk091", SHA, fetch=False), "")

    def _branch_head(self):
        return subprocess.run(["git", "-C", str(self.repo), "rev-parse", "eng/x"], check=True, capture_output=True,
                              text=True).stdout.strip()


class QuoteTests(unittest.TestCase):
    def test_quote_needs_verbatim_run(self):
        self.assertTrue(PC.quoted(ORDER, "Прочитал: «сначала проверь второй путь записи в watch.py» — сделано"))
        self.assertFalse(PC.quoted(ORDER, "прочитал, учёл"))
        self.assertFalse(PC.quoted(ORDER, "проверь второй путь"))  # короче 20 знаков подряд из записи
        self.assertTrue(PC.quoted("Не торопись", "ответ: не торопись"))  # запись короче порога — целиком


class LateOrderTests(unittest.TestCase):
    res = TR.ResultTests.res
    tkt = TR.ResultTests.tkt

    def setUp(self):  # noqa: F811
        TR.ResultTests.setUp(self)
        (self.base / "out.md").write_text("x", encoding="utf-8")
        self.run_state(True)

    def tearDown(self):  # noqa: F811
        TR.ResultTests.tearDown(self)

    def run_state(self, running: bool, role: str = "engineer"):
        """Зеркало запуска диспетчера: роль на тикете запущена в NOW (`active_runs` в state.json)."""
        runs = {self.tid: {"role": role, "started": NOW.isoformat(timespec="seconds")}} if running else {}
        P.write_json(CFG.STATE_FILE, {"active_runs": runs})

    def refused(self, *a, **kw) -> str:
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            rc = self.res(*a, **kw)
        self.assertEqual(rc, 1, err.getvalue())
        return err.getvalue()

    def say(self, who, text, plus=60):
        T.append_log(self.path, who, text, now=NOW + timedelta(seconds=plus))

    def test_ceo_entry_after_start_blocks_done_and_pr(self):
        self.say("ceo", ORDER)
        msg = self.refused("engineer", "done", path="out.md")
        self.assertIn("после старта сессии", msg)
        self.assertIn("сначала проверь второй путь", msg)
        self.assertIn("comment", msg)
        self.assertIn("после старта сессии", self.refused("engineer", "pr", pr=7, sha=SHA))
        self.assertNotIn("[итог:", self.tkt().log[-1].text)

    def test_service_entries_of_code_are_not_orders(self):
        """Срок владельцу и согласие на публикацию пишет код от имени ceo (~9 из 177 сдач) — не заказ, сдача проходит."""
        os.environ.pop("RPV_ROLE", None)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(TR.TK.cmd_due(type("A", (), dict(id=self.tid, by="2099-01-02T18:00+04:00", said="к шести"))()), 0)
        T.append_log(self.path, T.service_author("ceo"), "согласие владельца на публикацию по этому тикету (В-1): выпуск",
                     now=NOW + timedelta(seconds=70))
        due = self.tkt().log[-2]
        self.assertTrue(T.is_service(due.author) and T.author_is(due.author, "ceo"))
        self.assertGreater(due.ts, NOW)  # запись позже старта — именно её раньше считали заказом
        self.assertEqual(self.res("engineer", "done", path="out.md"), 0)

    def test_plain_ceo_entry_stays_an_order_next_to_service_one(self):
        self.say("ceo (служебная)", "Срок владельцу: 2099-01-02T18:00+04:00", plus=30)
        self.say("ceo", ORDER)
        self.assertIn("сначала проверь второй путь", self.refused("engineer", "done", path="out.md"))

    def test_judge_entry_blocks_too(self):
        self.say("judge", "Возврат: не проверен второй писатель сигнала в watch.py")
        self.assertIn("judge", self.refused("engineer", "done", path="out.md"))

    def test_quote_in_why_lets_through(self):
        self.say("ceo", ORDER)
        self.assertEqual(self.res("engineer", "done", path="out.md",
                                  why="учёл «сначала проверь второй путь записи в watch.py»"), 0)

    def test_own_later_entry_with_quote_lets_through(self):
        self.say("ceo", ORDER)
        self.say("engineer", "Заказ прочитан: сначала проверь второй путь записи в watch.py — делаю", plus=90)
        self.assertEqual(self.res("engineer", "done", path="out.md"), 0)

    def test_own_entry_without_quote_does_not(self):
        self.say("ceo", ORDER)
        self.say("engineer", "прочитал, учёл", plus=90)
        self.refused("engineer", "done", path="out.md")

    def test_entry_before_start_and_other_authors_pass(self):
        self.say("ceo", ORDER, plus=-30)
        self.say("dispatcher", "Запуск роли engineer", plus=5)
        self.say("merge", "PR влит", plus=6)
        self.assertEqual(self.res("engineer", "done", path="out.md"), 0)

    def test_no_start_in_env_or_other_results_pass(self):
        self.say("ceo", ORDER)
        self.assertEqual(self.res("engineer", "blocked", why="жду ответа"), 0)  # blocked/ask-owner — выход всегда открыт
        self.run_state(False)  # сдача не из запуска диспетчера: старта нет — проверять нечем
        self.assertEqual(self.res("engineer", "done", path="out.md"), 0)


class TemplateTests(unittest.TestCase):
    ROOT = Path(__file__).resolve().parents[2]

    def text(self, name):
        return (self.ROOT / "templates" / "roles" / name).read_text(encoding="utf-8")

    def test_contract_template_has_two_mandatory_items(self):
        readme = self.text("README.md")
        self.assertIn("«Где ещё это живёт»", readme)
        self.assertIn("Таблица граничных входов", readme)
        self.assertIn("не по имени", readme)

    def test_engineer_charter_states_test_rule_once_and_stays_short(self):
        eng = self.text("engineer.md")
        self.assertEqual(eng.count("Прогон тестов (В-21)"), 1)
        self.assertEqual(sum("полный" in ln and "прогон" in ln for ln in eng.splitlines()), 1)  # было в трёх строках
        self.assertLessEqual(len(eng), 4300)  # устав целиком во вставке хука (LIMIT 8000 вместе с блокнотом); main уже 4401 до слияния
        self.assertIn("result pr|done", eng)


if __name__ == "__main__":
    unittest.main()
