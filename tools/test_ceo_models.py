"""Тесты tools/ceo_models.py (TK-068) на фикстурах: отбор интерактивного CEO, смена модели, вес, поправка владельца."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import ceo_models as cm  # noqa: E402

HOOK = "РОЛЬ СЕССИИ: ты — «CEO» команды"


def _u(ts, text, **kw):
    return {"type": "user", "timestamp": f"2026-10-30T10:{ts}:00Z", "message": {"content": text}, **kw}


def _a(ts, mid, model, text="ответ", tools=0, usage=None):
    content = [{"type": "text", "text": text}] + [{"type": "tool_use"}] * tools
    return {"type": "assistant", "timestamp": f"2026-10-30T10:{ts}:00Z",
            "message": {"id": mid, "model": model, "content": content,
                        "usage": usage or {"input_tokens": 10, "cache_creation_input_tokens": 100,
                                           "cache_read_input_tokens": 1000, "output_tokens": 20}}}


def _j(o):
    return json.dumps(o, ensure_ascii=False, separators=(",", ":"))


def _write(path, rows, entry="cli"):
    lines = [_j({"entrypoint": entry, "type": "system"}), _j(_u("00", HOOK, isMeta=True))]
    lines += [_j(r) for r in rows]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def test_weight_formula():
    assert cm.weight({"input_tokens": 10, "cache_creation_input_tokens": 100,
                      "cache_read_input_tokens": 1000, "output_tokens": 20}) == 10 + 125 + 100 + 100


def test_family():
    assert cm.family("claude-opus-4-7") == "opus"
    assert cm.family("claude-sonnet-5-5") == "sonnet"
    assert cm.family(None) == "прочее"


def test_ceo_session_vs_dispatcher_and_other_roles(tmp_path):
    ceo = _write(tmp_path / "ceo.jsonl", [])
    disp = _write(tmp_path / "disp.jsonl", [], entry="sdk-cli")
    other = tmp_path / "eng.jsonl"
    other.write_text(json.dumps({"entrypoint": "cli"}) + "\n" + json.dumps(_u("00", "РОЛЬ СЕССИИ: ты — «Инженер»")), encoding="utf-8")
    assert cm.is_ceo_session(ceo) is True
    assert cm.is_ceo_session(disp) is False
    assert cm.is_ceo_session(other) is False


def test_turns_split_by_model_and_sidechain_ignored(tmp_path):
    rows = [_u("01", "сделай"), _a("02", "m1", "claude-opus-4-7", tools=2),
            dict(_a("02", "sub", "claude-haiku-5-5"), isSidechain=True),
            _u("05", "почему так?"), _a("06", "m2", "claude-sonnet-5-5"),
            _u("09", "спасибо")]
    p = _write(tmp_path / "s.jsonl", rows)
    turns = cm.read_turns(p)
    assert [t["model"] for t in turns] == ["opus", "sonnet"]
    assert turns[0]["tools"] == 2 and turns[0]["n_msgs"] == 1
    assert turns[0]["weight"] == 335
    assert cm.corrected(turns[0]) is True      # «почему» в следующем сообщении
    assert cm.corrected(turns[1]) is False


def test_mixed_model_within_turn_and_segments(tmp_path):
    rows = [_u("01", "делай"), _a("02", "m1", "claude-opus-4-7"), _a("03", "m2", "claude-sonnet-5-5"),
            _a("04", "m3", "claude-sonnet-5-5")]
    p = _write(tmp_path / "s.jsonl", rows)
    assert cm.read_turns(p)[0]["model"] == "смешанный"
    segs = cm.segments(p)
    assert [(s["family"], s["n"]) for s in segs] == [("opus", 1), ("sonnet", 2)]


def test_tool_result_is_not_owner_message(tmp_path):
    tr = {"type": "user", "timestamp": "2026-10-30T10:03:00Z",
          "message": {"content": [{"type": "tool_result", "content": "ok"}]}}
    rows = [_u("01", "делай"), _a("02", "m1", "claude-opus-4-7"), tr, _a("04", "m2", "claude-opus-4-7")]
    turns = cm.read_turns(_write(tmp_path / "s.jsonl", rows))
    assert len(turns) == 1 and turns[0]["n_msgs"] == 2


def test_critic_and_wilson():
    assert cm.critic_asked({"next_user": "включай критика"}) is True
    assert cm.critic_asked({"next_user": None}) is False
    lo, hi = cm.wilson(5, 10)
    assert lo < 0.5 < hi
    assert cm.wilson(0, 0) == (0.0, 0.0)


def test_main_end_to_end(tmp_path, capsys):
    for proj, model in (("rpv", "claude-opus-4-7"), ("alpha", "claude-sonnet-5-5")):
        d = tmp_path / proj
        d.mkdir()
        _write(d / "s.jsonl", [_u("01", "а"), _a("02", "m1", model), _u("03", "ты что"), _a("04", "m2", model)])
    cm.main(["--rpv", str(tmp_path / "rpv"), "--alpha", str(tmp_path / "alpha"), "--segments"])
    out = capsys.readouterr().out
    assert "Интерактивных сессий CEO: 2" in out and "| rpv | opus |" in out and "| alpha | sonnet |" in out
