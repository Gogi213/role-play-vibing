"""TK-097: заголовок записи «роль (уточнение)» («ceo (служебная)») читают tools/losses.py и tools/unpushed_heads.py как запись роли."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import losses
import unpushed_heads as u

LOG = """### 2026-10-30T11:30:00+04:00 judge
[итог: return] проверка: docs/x.md — нет синтетики

### 2026-10-30T11:40:00+04:00 ceo (служебная)
Срок владельцу: 18:00

### 2026-10-30T11:50:00+04:00 engineer (запуск 2)
[итог: pr] PR #5@aaaaaaa1 — вторая
### 2026-10-30T11:55:00+04:00 ceo (служебная)
Залип: коммит aaaaaaa1 лежит на detached HEAD и не запушен
"""


def test_losses_reads_labelled_header_as_its_role(tmp_path):
    d = tmp_path / ".claude"
    (d / "tickets").mkdir(parents=True)
    (d / "tickets" / "TK-1.md").write_text("---\nid: TK-1\n---\n\n## Лог\n\n" + LOG, encoding="utf-8")
    log = losses.parse_logs(d)["TK-1"]
    assert [e[1] for e in log] == ["judge", "ceo", "engineer", "ceo"]
    assert log[0][2].endswith("нет синтетики") and log[1][2] == "Срок владельцу: 18:00"


def test_unpushed_heads_reads_labelled_header_as_its_role(tmp_path):
    assert [e[1] for e in u.entries(LOG)] == ["judge", "ceo", "engineer", "ceo"]
    (tmp_path / "tickets").mkdir()
    (tmp_path / "dispatcher").mkdir()
    (tmp_path / "tickets" / "TK-900.md").write_text("## Лог\n" + LOG, "utf-8")
    (tmp_path / "dispatcher" / "ci_watch.run.log").write_text("", "utf-8")
    rows = u.analyse(tmp_path)
    assert [(r["sha"], r["stuck"]) for r in rows] == [("aaaaaaa", True)]  # marker от ceo с пометкой засчитан
