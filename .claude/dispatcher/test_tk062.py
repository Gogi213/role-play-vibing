"""TK-062 (В-24): простой CEO. Устав CEO — наблюдение держать всегда и «покрыто решением — делай, не спрашивай»;
`/ceo` взводит `Monitor`; сторож `watch-unreleased` («очередь пуста, в main влито и не выпущено») за ключом `watch_unreleased`."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_watch import WatchSandbox, CFG, SIG, T, W  # noqa: E402 — песочница сторожа
import charter  # noqa: E402
import project as P  # noqa: E402
import rpv_config  # noqa: E402
import unreleased as UNR  # noqa: E402

PLUGIN = Path(__file__).resolve().parents[2]
CEO_TEMPLATE = PLUGIN / "templates" / "roles" / "ceo.md"
CEO_COMMAND = PLUGIN / "commands" / "ceo.md"
SIGNALS_DOC = PLUGIN / "docs" / "signals.md"

TAGS = [{"name": "v2.0.9"}, {"name": "v2.0.14"}, {"name": "v2.0.10"}, {"name": "latest"}, {"name": "v2.0.15-rc1"}]


def rule(text: str, head: str) -> str:
    """Строка устава, начинающаяся с `- **head`, вместе с продолжением (отступ в 2 пробела)."""
    lines = text.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith(f"- **{head}"))
    end = start + 1
    while end < len(lines) and lines[end].startswith("  "):
        end += 1
    return "\n".join(lines[start:end])


class CeoCharterTests(unittest.TestCase):
    def setUp(self):
        self.text = CEO_TEMPLATE.read_text(encoding="utf-8")

    def test_watch_is_kept_always(self):
        r = rule(self.text, "Наблюдение держишь, пока есть работа")
        for must in ("30 мин", "постоянного режима нет", "Monitor expired", "после рестарта", "перевзводи сразу",
                     "`timeout_ms: 1800000`", "умолчание инструмента — 5 мин", "без фильтра `grep` по `TK-`",
                     "строки сторожа идут с «*»", "молча", "сразу прочитай `tickets.py inbox`", "`tail -n 0` теряет строки",
                     "Исключение одно — «Monitor expired» при пустой очереди", "`watch_unreleased` выключен — не перевзводи",
                     "«Напишу, когда …» владельцу не обещай, пока наблюдение не включено"):
            self.assertIn(must, r)

    def test_covered_by_decision_acts_without_asking(self):
        r = rule(self.text, "Покрыто решением")
        for must in ("сверь реестр", "действующее решение В-N", "цитатой владельца прямо называет",
                     "сразу заводи тикет со ссылкой `--ref В-N`", "вопрос не задавай"):
            self.assertIn(must, r)

    def test_covered_rule_quotes_only_owner_line(self):
        r = rule(self.text, "Покрыто решением")
        for must in ("Цитата — только строка «>» самого владельца", "«Суть» и «Вопрос CEO» цитатой не считаются",
                     "цепочку «Заменяет» проверь: замена бывает частичной",
                     "«да» / «ок делай» покрывают только названное в «Вопрос CEO» этой записи и один раз",
                     "«Толкование CEO» или «при сомнении переспросить» — спрашивай"):
            self.assertIn(must, r)
        self.assertNotIn("выпуск после приёмки Судьи", r)

    def test_covered_rule_does_not_widen_authority(self):
        r = rule(self.text, "Покрыто решением")
        for must in ("«похожее» решение не в счёт", "деньги, необратимое, направление и бизнес-правило — владельцу",
                     "`allow <ID> publish --ref В-N`", "строке про самодеятельность",
                     "по строке «Перенос, переименование, удаление»",
                     "по строкам «Согласие на публикацию» и «Удаление веток на удалённом»"):
            self.assertIn(must, r)
        for head in ("Перенос, переименование, удаление", "Согласие на публикацию", "Удаление веток на удалённом"):
            self.assertTrue(any(ln.startswith(f"- **{head}") for ln in self.text.splitlines()), head)

    def test_no_contradiction_with_neighbouring_rules(self):
        # строки, с которыми новое правило согласовано, на месте и называют те же понятия
        self.assertIn("Новые задачи — только по слову владельца", self.text)
        self.assertIn("слово → `secretary` в реестр (В-N) → `tickets.py allow <ID> publish --ref В-N`", self.text)
        self.assertIn("**Кто решает:**", self.text)
        self.assertIn("**Работа команды — без самодеятельности:**", self.text)
        self.assertIn("«Новые задачи»", rule(self.text, "Покрыто решением"))

    def test_alarm_rule_still_there(self):
        self.assertIn("`Monitor` на `.claude/dispatcher/ceo-wake.log`", rule(self.text, "Будильник"))

    def test_sync_carries_both_rules_into_project_charter(self):
        with tempfile.TemporaryDirectory() as tmp:
            roles = Path(tmp) / ".claude" / "roles"
            roles.mkdir(parents=True)
            (roles / "ceo.md").write_text("# CEO\nстарый устав\n", encoding="utf-8")
            _, _, err = charter.sync(Path(tmp), ["ceo.md"], now=W.datetime.now().astimezone())
            self.assertIsNone(err)
            synced = (roles / "ceo.md").read_text(encoding="utf-8")
            self.assertIn("**Наблюдение держишь, пока есть работа:**", synced)
            self.assertIn("**Покрыто решением — делай, не спрашивай:**", synced)


class CeoCommandTests(unittest.TestCase):
    def test_command_arms_the_monitor(self):
        text = CEO_COMMAND.read_text(encoding="utf-8")
        self.assertIn("allowed-tools: Bash, Monitor", text)
        self.assertIn("`Monitor` на `<корень проекта>/.claude/dispatcher/ceo-wake.log`", text)
        self.assertIn("Monitor expired", text)
        self.assertIn("после рестарта сессии перевзводи сразу и молча", text)
        self.assertIn("с `timeout_ms: 1800000`", text)
        self.assertIn("без фильтра `grep` по `TK-`", text)
        self.assertIn("сразу прочитай `tickets.py inbox`", text)
        self.assertIn("не перевзводи только при «Monitor expired» с пустой очередью", text)
        self.assertIn("не обещай, пока наблюдение не включено", text)

    def test_monitor_step_precedes_the_reply(self):
        text = CEO_COMMAND.read_text(encoding="utf-8")
        self.assertLess(text.index("Взведи наблюдение"), text.index("«Сессия помечена как CEO»"))


class UnreleasedSandbox(WatchSandbox):
    def setUp(self):
        super().setUp()
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        (Path(self.root.name) / ".claude").mkdir()
        old = P._ROOT
        P.set_root(self.root.name)
        self.addCleanup(P.set_root, old)
        self.calls = []
        self.ahead = 2
        self.gh_down = False
        self.branch = "main"
        self.files = [{"filename": ".claude/dispatcher/watch.py"}]

    def rpv(self, **kv):
        (Path(self.root.name) / ".claude" / "rpv.json").write_text(json.dumps(kv), encoding="utf-8")

    def on(self):
        self.rpv(watch_unreleased=True, ci_repo="owner/repo")

    def gh(self, path):
        self.calls.append(path)
        if self.gh_down:
            raise RuntimeError("gh: нет сети")
        if "/tags" in path:
            return TAGS
        if "/compare/" in path:
            return {"ahead_by": self.ahead, "files": self.files}
        return {"default_branch": self.branch}

    def check(self, minutes_after_first=0, ws=None):
        ws = {} if ws is None else ws
        return W.check_unreleased(ws, self.now + timedelta(minutes=minutes_after_first), self.gh), ws


class UnreleasedSwitchTests(UnreleasedSandbox):
    def test_off_by_default_and_gh_not_called(self):
        self.rpv(ci_repo="owner/repo")
        out, ws = self.check(60)
        self.assertEqual(out, [])
        self.assertEqual(self.calls, [])
        self.assertNotIn(UNR.IDLE_SINCE_KEY, ws)

    def test_no_file_no_finding(self):
        self.assertEqual(self.check(60)[0], [])
        self.assertEqual(self.calls, [])

    def test_no_ci_repo_silent(self):
        self.rpv(watch_unreleased=True)
        self.assertEqual(self.check(60)[0], [])
        self.assertEqual(self.calls, [])

    def test_key_is_a_valid_boolean_setting(self):
        check, _ = rpv_config.KEYS["watch_unreleased"]
        self.assertTrue(check(True) and check(False))
        self.assertFalse(check("yes") or check(1) or check(None))


class UnreleasedQueueTests(UnreleasedSandbox):
    def test_empty_queue_must_last_ten_minutes(self):
        self.on()
        ws = {}
        self.assertEqual(self.check(0, ws)[0], [])  # первое наблюдение: отметка, не находка
        self.assertEqual(self.calls, [])
        self.assertEqual(self.check(UNR.UNRELEASED_IDLE_MIN - 1, ws)[0], [])
        self.assertEqual(self.calls, [])
        out, _ = self.check(UNR.UNRELEASED_IDLE_MIN, ws)
        self.assertEqual([(f.kind, f.key) for f in out], [("unreleased", "main")])

    def test_open_ticket_resets_the_idle_timer(self):
        self.on()
        ws = {}
        self.check(0, ws)
        p = T.create_ticket(self.tickets_dir, owner="engineer", title="В работе", status="todo")
        T.write_header_updates(p, {"status": "in_progress"})
        self.assertEqual(self.check(30, ws)[0], [])
        self.assertNotIn(UNR.IDLE_SINCE_KEY, ws)
        T.write_header_updates(p, {"status": "done"})
        self.assertEqual(self.check(31, ws)[0], [])  # очередь опустела только сейчас — отсчёт заново
        self.assertEqual(self.calls, [])
        self.assertEqual(len(self.check(31 + UNR.UNRELEASED_IDLE_MIN, ws)[0]), 1)

    def test_every_open_status_counts_as_work(self):
        self.on()
        for status in T.OPEN_STATUSES:
            with self.subTest(status=status):
                p = T.create_ticket(self.tickets_dir, owner="engineer", title=f"Тикет {status}", status="todo")
                T.write_header_updates(p, {"status": status})
                ws = {UNR.IDLE_SINCE_KEY: T.now_iso(self.now - timedelta(hours=3))}
                self.assertEqual(self.check(0, ws)[0], [])
                self.assertNotIn(UNR.IDLE_SINCE_KEY, ws)
                T.write_header_updates(p, {"status": "done"})

    def test_closed_tickets_do_not_hold_the_queue(self):
        self.on()
        for status in ("done", "stopped"):
            p = T.create_ticket(self.tickets_dir, owner="engineer", title=status, status="todo")
            T.write_header_updates(p, {"status": status})
        ws = {UNR.IDLE_SINCE_KEY: T.now_iso(self.now - timedelta(minutes=11))}
        self.assertEqual(len(self.check(0, ws)[0]), 1)

    def test_unreadable_ticket_is_not_an_empty_queue(self):
        self.on()
        (self.tickets_dir / "BROKEN.md").write_text("нет шапки\n", encoding="utf-8")
        ws = {UNR.IDLE_SINCE_KEY: T.now_iso(self.now - timedelta(hours=1))}
        self.assertEqual(self.check(0, ws)[0], [])
        self.assertEqual(self.calls, [])


class UnreleasedGithubTests(UnreleasedSandbox):
    def setUp(self):
        super().setUp()
        self.on()
        self.ws = {UNR.IDLE_SINCE_KEY: T.now_iso(self.now - timedelta(hours=1))}

    def test_compares_latest_version_tag_with_origin_main(self):
        out, _ = self.check(0, self.ws)
        self.assertEqual(self.calls, ["repos/owner/repo/tags?per_page=100", "repos/owner/repo",
                                      "repos/owner/repo/compare/v2.0.14...main?per_page=1"])
        self.assertIn("2 коммит", out[0].message)
        self.assertIn("v2.0.14", out[0].message)
        self.assertIn("owner/repo", out[0].message)
        self.assertIn("тикетов в работе нет 60 мин", out[0].message)

    def test_nothing_after_the_tag_is_silent(self):
        self.ahead = 0
        self.assertEqual(self.check(0, self.ws)[0], [])

    def test_no_release_tags_is_silent(self):
        with mock.patch.object(self, "gh", lambda path: [{"name": "latest"}]):
            self.assertEqual(W.check_unreleased(self.ws, self.now, self.gh), [])

    def test_github_failure_is_silent_not_a_crash(self):
        self.gh_down = True
        out, _ = self.check(0, self.ws)
        self.assertEqual([(f.kind, f.unknown) for f in out], [("unreleased", True)])  # «не знаю», текста нет
        self.assertEqual(W.notify_findings(out, {}, self.now), [])

    def test_latest_tag_is_by_number_not_by_order(self):
        self.assertEqual(UNR.last_release_tag(TAGS), "v2.0.14")
        self.assertEqual(UNR.last_release_tag([{"name": "v1.9.0"}, {"name": "v1.10.0"}]), "v1.10.0")
        self.assertEqual(UNR.last_release_tag([]), "")
        self.assertEqual(UNR.last_release_tag(None), "")


class UnreleasedSignalTests(UnreleasedSandbox):
    def finding(self):
        return [W.Finding("unreleased", "main", "влито, не выпущено: тест")]

    def test_repeats_once_a_day_and_wakes_ceo(self):
        self.assertIn("unreleased", W.WATCH_LONG_REPEAT_KINDS)
        ws = {}
        self.assertEqual(len(W.notify_findings(self.finding(), ws, self.now)), 1)
        self.assertEqual(W.notify_findings(self.finding(), ws, self.now + timedelta(hours=6)), [])
        self.assertEqual(len(W.notify_findings(self.finding(), ws, self.now + timedelta(hours=24, minutes=1))), 1)
        self.assertIn("[watch-unreleased]", CFG.CEO_INBOX.read_text(encoding="utf-8"))
        self.assertIn("watch-unreleased", CFG.CEO_WAKE_LOG.read_text(encoding="utf-8"))

    def test_not_mechanics_and_not_a_summary(self):
        self.assertNotIn("unreleased", SIG.MECH_KINDS)
        self.assertNotIn("watch-unreleased", SIG.MECH_KINDS)
        self.assertEqual(SIG.classify_signal("watch-unreleased"), "wake")

    def test_finding_gone_after_release_forgets_the_dedup(self):
        ws = {}
        W.notify_findings(self.finding(), ws, self.now)
        W.notify_findings([], ws, self.now + timedelta(hours=1))
        self.assertEqual(len(W.notify_findings(self.finding(), ws, self.now + timedelta(hours=2))), 1)

    def test_run_once_posts_it_only_with_probes(self):
        with mock.patch.object(W, "check_unreleased", return_value=self.finding()), \
                mock.patch.object(W, "check_strays", return_value=[]):
            self.assertEqual(W.run_once(self.now, probes=False), [])
            posted = W.run_once(self.now, probes=True)
        self.assertEqual([f.kind for f in posted], ["unreleased"])

    def test_run_once_survives_a_failing_check(self):
        with mock.patch.object(W, "check_unreleased", side_effect=RuntimeError("boom")), \
                mock.patch.object(W, "check_strays", return_value=[]):
            self.assertEqual(W.run_once(self.now, probes=True), [])


class UnreleasedDocsTests(unittest.TestCase):
    def test_signals_doc_lists_the_kind(self):
        text = SIGNALS_DOC.read_text(encoding="utf-8")
        row = next(ln for ln in text.splitlines() if ln.startswith("| `watch-unreleased`"))
        for must in ("10 мин", "`watch_unreleased`", "по умолчанию выключен", "раз в сутки", "будит"):
            self.assertIn(must, row)

    def test_signals_doc_says_monitor_expires(self):
        text = SIGNALS_DOC.read_text(encoding="utf-8")
        self.assertIn("не дольше 30 мин", text)
        self.assertIn("перевзводит молча и сразу", text)
        self.assertIn("`timeout_ms: 1800000`", text)
        self.assertIn("Не перевзводит он только при «Monitor expired» с пустой очередью", text)


if __name__ == "__main__":
    unittest.main()
