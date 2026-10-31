"""TK-072 (разбор критиков, В-31): сторож `watch-unreleased` — ветка репозитория, только docs/tools, сбой GitHub не снимает
метку повтора, «false» строкой не включает; устав наблюдения CEO — исключение «очередь пуста» и фильтр `summary`;
docs/release.md — тег на коммит слияния."""
from __future__ import annotations

import sys
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_tk062 as T62  # noqa: E402 — песочница «влито, не выпущено»
from test_tk062 import CEO_COMMAND, CEO_TEMPLATE, PLUGIN, SIGNALS_DOC, T, UNR, W, rule  # noqa: E402

DOCS_ONLY = [{"filename": "docs/signals.md"}, {"filename": "tools/bench/e2e.py"}]


class Base(T62.UnreleasedSandbox):
    def setUp(self):
        super().setUp()
        self.on()
        self.ws = {UNR.IDLE_SINCE_KEY: T.now_iso(self.now - timedelta(hours=1))}


class DefaultBranchTests(Base):
    def test_branch_comes_from_the_repository(self):
        self.branch = "trunk"
        out, _ = self.check(0, self.ws)
        self.assertEqual(self.calls[-1], "repos/owner/repo/compare/v2.0.14...trunk?per_page=100")
        self.assertIn("в trunk", out[0].message)


class ReleaseFilesTests(Base):
    def test_only_docs_and_tools_is_silent(self):
        self.files = DOCS_ONLY
        self.assertEqual(self.check(0, self.ws)[0], [])

    def test_one_code_file_is_a_signal(self):
        self.files = DOCS_ONLY + [{"filename": "hooks/hooks.json"}]
        self.assertEqual(len(self.check(0, self.ws)[0]), 1)

    def test_no_files_is_silent(self):
        self.files = []
        self.assertEqual(self.check(0, self.ws)[0], [])

    def test_a_full_page_of_docs_is_not_proof(self):
        self.files = [{"filename": f"docs/{i}.md"} for i in range(UNR.FILES_PER_PAGE)]
        self.assertEqual(len(self.check(0, self.ws)[0]), 1)

    def test_path_prefix_not_substring(self):
        self.files = [{"filename": "src/docs/a.md"}, {"filename": "mytools/x.py"}]
        self.assertEqual(len(self.check(0, self.ws)[0]), 1)


class GithubFailureKeepsDedupTests(Base):
    def test_failure_is_unknown_not_none(self):
        self.gh_down = True
        self.assertIsNone(UNR.unreleased_message(self.ws, self.now, self.gh))

    def test_failure_between_two_cycles_does_not_repost(self):
        first = self.check(0, self.ws)[0]
        ws = {"notified": {}, **self.ws}
        self.assertEqual(len(W.notify_findings(first, ws, self.now)), 1)
        self.gh_down = True
        down = self.check(2, self.ws)[0]
        self.assertEqual(W.notify_findings(down, ws, self.now + timedelta(minutes=2)), [])
        self.assertIn("unreleased:main", ws["notified"])  # метка суточного повтора на месте
        self.gh_down = False
        again = self.check(4, self.ws)[0]
        self.assertEqual(W.notify_findings(again, ws, self.now + timedelta(minutes=4)), [])  # то же окно — без повтора
        self.assertEqual(len(W.notify_findings(again, ws, self.now + timedelta(hours=24, minutes=5))), 1)

    def test_answer_without_news_still_forgets_the_mark(self):
        ws = {"notified": {}, **self.ws}
        W.notify_findings(self.check(0, self.ws)[0], ws, self.now)
        self.ahead = 0  # выпустили: GitHub ответил «ничего нового»
        self.assertEqual(W.notify_findings(self.check(2, self.ws)[0], ws, self.now + timedelta(minutes=2)), [])
        self.assertNotIn("unreleased:main", ws["notified"])

    def test_unknown_is_not_counted_in_the_heartbeat(self):
        self.gh_down = True
        out, _ = self.check(0, self.ws)
        self.assertTrue(all(f.unknown for f in out))
        self.assertEqual(sum(not f.unknown for f in out), 0)


class SwitchTests(Base):
    def test_string_false_does_not_turn_it_on(self):
        self.rpv(watch_unreleased="false", ci_repo="owner/repo")
        self.assertEqual(self.check(0, self.ws)[0], [])
        self.assertEqual(self.calls, [])

    def test_true_still_turns_it_on(self):
        self.assertEqual(len(self.check(0, self.ws)[0]), 1)


class CharterTests(unittest.TestCase):
    def test_charter_watch_rule_has_one_exception(self):
        r = rule(CEO_TEMPLATE.read_text(encoding="utf-8"), "Наблюдение держишь, пока есть работа")
        for must in ("Исключение одно — «Monitor expired» при пустой очереди", "`watch_unreleased` выключен — не перевзводи",
                     "`watch_unreleased` включён — перевзводи", "после рестарта или `/clear` — перевзводи всегда",
                     "Фильтровать можно только строки вида `summary`", "без фильтра `grep` по `TK-`"):
            self.assertIn(must, r)
        self.assertNotIn("Очередь пуста и владельцу ничего не обещано — не перевзводи", r)

    def test_command_and_doc_follow_the_charter(self):
        text = CEO_COMMAND.read_text(encoding="utf-8")
        for must in ("только при «Monitor expired» с пустой очередью", "`watch_unreleased` выключен",
                     "после рестарта и `/clear` перевзводи всегда", "только строки `summary`"):
            self.assertIn(must, text)
        doc = SIGNALS_DOC.read_text(encoding="utf-8")
        for must in ("после рестарта сессии и `/clear` перевзводит всегда", "при включённом `watch_unreleased`", "только строки вида `summary`"):
            self.assertIn(must, doc)

    def test_no_persistent_flag_claimed(self):
        for path in (CEO_TEMPLATE, CEO_COMMAND, SIGNALS_DOC, PLUGIN / "CHANGELOG.md", PLUGIN / "docs" / "configuration.md"):
            self.assertNotIn("persistent", path.read_text(encoding="utf-8"), path.name)


class DocsTests(unittest.TestCase):
    def test_release_doc_says_tag_on_merge_commit(self):
        self.assertIn("Тег ставят на коммит слияния", (PLUGIN / "docs" / "release.md").read_text(encoding="utf-8"))

    def test_changelog_enable_command_has_why_and_ref(self):
        lines = [ln for ln in (PLUGIN / "CHANGELOG.md").read_text(encoding="utf-8").splitlines()
                 if "config set watch_unreleased true" in ln]
        self.assertTrue(lines)
        for line in lines:
            self.assertIn('config set watch_unreleased true --why "..." --ref В-N', line)

    def test_docs_describe_branch_and_paths(self):
        row = next(ln for ln in (PLUGIN / "docs" / "configuration.md").read_text(encoding="utf-8").splitlines()
                   if ln.startswith("| `watch_unreleased`"))
        for must in ("`default_branch`", "не только `docs/` и `tools/`", "метку суточного повтора не снимает", "ровно `true`"):
            self.assertIn(must, row)


if __name__ == "__main__":
    unittest.main()
