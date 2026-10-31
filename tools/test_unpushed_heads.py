import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import unpushed_heads as u


def _proj(tmp_path, ticket, watch):
    (tmp_path / "tickets").mkdir()
    (tmp_path / "dispatcher").mkdir()
    (tmp_path / "tickets" / "TK-900.md").write_text(ticket, "utf-8")
    (tmp_path / "dispatcher" / "ci_watch.run.log").write_text(watch, "utf-8")
    return tmp_path


T = """## Лог
### 2026-10-31T10:00:00+04:00 engineer
[итог: pr] PR #5@aaaaaaa1 — первая
### 2026-10-31T10:03:00+04:00 ci
CI зелёный на aaaaaaa1 (PR #5)
### 2026-10-31T10:10:00+04:00 engineer
[итог: pr] PR #5@bbbbbbb2 — вторая
### 2026-10-31T10:25:00+04:00 ceo
Залип: коммит bbbbbbb2 лежит в копии на detached HEAD и не запушен; в ветке по-прежнему aaaaaaa1
"""


def test_marker_flags_only_named_head(tmp_path):
    p = _proj(tmp_path, T, "[ci_watch] PR #5 aaaaaaa1: success\n")
    rows = u.analyse(p)
    assert [(r["sha"], r["stuck"]) for r in rows] == [("aaaaaaa", False), ("bbbbbbb", True)]
    assert rows[1]["minutes"] == 15.0
    s = u.summary(rows)
    assert (s["submissions"], s["stuck"], s["share"]) == (2, 1, 0.5)


def test_slow_ci_is_separate_and_weak_needs_window(tmp_path):
    t = T.split("### 2026-10-31T10:10")[0].replace("10:03:00", "10:20:00")
    p = _proj(tmp_path, t, "[ci_watch] PR #9 ccccccc: success\n")
    rows = u.analyse(p)
    assert rows[0]["slow"] and not rows[0]["stuck"] and not rows[0]["weak"]
    assert u.summary(rows)["slow_ci"] == 1
