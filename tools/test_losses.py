import losses

RUNS = """2026-10-30T10:00:00+04:00 TK-1 researcher reason=todo attempt=0 resolved_cost=1.0000 status=ok
2026-10-30T11:00:00+04:00 TK-1 researcher reason=wait_for-met attempt=0 resolved_cost=0.5000 status=ok
2026-10-30T12:00:00+04:00 TK-1 researcher reason=wait_for-met attempt=0 cost_usd=0.2 status=ok
2026-10-30T13:00:00+04:00 TK-1 judge reason=next attempt=0 resolved_cost=0.3000 status=timeout
"""
TK = """---
id: TK-1
---

## Лог

### 2026-10-30T09:59:00+04:00 researcher
[итог: wait] ждём: host:calc:/a.done — считает

### 2026-10-30T10:59:00+04:00 researcher
[итог: wait] ждём: host:calc:/a.done — считает

### 2026-10-30T11:30:00+04:00 judge
[итог: return] проверка: docs/x.md — нет синтетики

### 2026-10-30T11:50:00+04:00 ceo
text
"""
GUARD = ("2026-10-30T10:00:00+04:00\tBash\trm\tdeny\tУдаление запрещено\n"
         "2026-10-30T10:01:00+04:00\tBash\trm\tallow\t\n")
INBOX = "- 2026-10-30T11:00:00+04:00 TK-1 [done] x\n"


def make(tmp_path):
    d = tmp_path / ".claude"
    (d / "dispatcher").mkdir(parents=True)
    (d / "tickets" / "archive").mkdir(parents=True)
    (d / "dispatcher" / "runs.log").write_text(RUNS, encoding="utf-8")
    (d / "tickets" / "TK-1.md").write_text(TK, encoding="utf-8")
    (d / "dispatcher" / "guard.log").write_text(GUARD, encoding="utf-8")
    (d / "dispatcher" / ("ceo-" + "inbox.md")).write_text(INBOX, encoding="utf-8")
    return d


def test_wasted_wakes_and_costs(tmp_path):
    d = make(tmp_path)
    runs = losses.parse_runs(d / "dispatcher" / "runs.log")
    assert [r["cost"] for r in runs] == [1.0, 0.5, 0.2, 0.3]
    w = losses.wasted_wakes(runs, losses.parse_logs(d))
    kinds = {(r["ts"][11:13], k) for r, k in w}
    assert ("11", "повторное ожидание той же цели") in kinds
    assert ("12", "нет записи роли") in kinds
    assert ("13", "запуск не удался: timeout") in kinds
    assert len(w) == 3


def test_returns_delay_refusals_report(tmp_path):
    d = make(tmp_path)
    logs = losses.parse_logs(d)
    rt = losses.returns(logs, "", "")
    assert [(t, why) for t, _, why, _ in rt] == [("TK-1", "нет/не вошло по договору")]
    assert losses.ceo_delay(d, logs, "", "")[0][0] == 50
    c, _ = losses.refusals(d, "", "")
    assert sum(c.values()) == 1
    assert "расход $2.00" in losses.report(tmp_path)


def test_refusal_runs_attribution(tmp_path):
    d = make(tmp_path)
    runs = losses.parse_runs(d / "dispatcher" / "runs.log")
    hit = losses.refusal_runs(d, runs, "", "")
    assert dict(hit) == {0: 1}
    assert "запусков с отказом охранника: 1 из 4, $1.00" in losses.report(tmp_path)
