import blind


def tr():
    return blind.Tracker(alert_s=300, repeat_s=1800)


def test_no_alert_before_threshold():
    t = tr()
    t.record("ssh:calc", False, 0)
    assert t.evaluate(299, {"ssh:calc"}) == []


def test_alert_per_target_with_name():
    t = tr()
    t.record("ssh:calc", False, 0)
    t.record("ssh:vps", True, 0)  # vps жив — тревога только по calc (условие 1 Судьи)
    t.record("bus", False, 100)
    assert t.evaluate(310, {"ssh:calc", "ssh:vps", "bus"}) == [("alert", "ssh:calc", 310)]
    assert t.evaluate(410, {"ssh:calc", "ssh:vps", "bus"}) == [("alert", "bus", 310)]


def test_repeat_every_30_min_and_recovered_once():
    t = tr()
    t.record("ssh:calc", False, 0)
    assert t.evaluate(300, {"ssh:calc"}) == [("alert", "ssh:calc", 300)]
    assert t.evaluate(1000, {"ssh:calc"}) == []
    assert t.evaluate(2100, {"ssh:calc"}) == [("repeat", "ssh:calc", 2100)]
    t.record("ssh:calc", True, 2200)
    assert t.evaluate(2210, {"ssh:calc"}) == [("recovered", "ssh:calc", 2200)]
    assert t.evaluate(2220, {"ssh:calc"}) == []


def test_short_outage_no_noise():
    t = tr()
    t.record("ssh:calc", False, 0)
    t.record("ssh:calc", True, 100)
    assert t.evaluate(110, {"ssh:calc"}) == []


def test_inactive_target_forgotten():
    t = tr()
    t.record("ssh:vps", False, 0)
    assert t.evaluate(1000, {"ssh:calc"}) == []
    assert not t.any_down()


def test_flap_keeps_original_since():
    t = tr()
    t.record("ssh:calc", False, 0)
    t.record("ssh:calc", False, 200)
    assert t.evaluate(300, {"ssh:calc"}) == [("alert", "ssh:calc", 300)]


def test_alert_file_removed_when_target_leaves_active(tmp_path, monkeypatch):
    import time
    import dispatch as D
    monkeypatch.setattr(D, "DISPATCHER_DIR", tmp_path)
    monkeypatch.setattr(D, "CEO_INBOX", tmp_path / "ceo-inbox.md")
    monkeypatch.setattr(D, "CEO_WAKE_LOG", tmp_path / "ceo-wake.log")
    monkeypatch.setattr(D, "TICKETS_DIR", tmp_path / "tickets")
    monkeypatch.setattr(D, "_blind_sound", lambda: None)
    monkeypatch.setattr(D, "BLIND", blind.Tracker(alert_s=300, repeat_s=1800))
    monkeypatch.setattr(D, "_WAIT_WATCH", {("calc", "host", "x"): 1})
    D.BLIND.record("ssh:calc", False, time.time() - 400)
    D._blind_tick()
    f = tmp_path / "ALERT-BLIND.txt"
    assert f.exists() and "ssh:calc" in f.read_text(encoding="utf-8")
    assert "[blind]" in (tmp_path / "mechanics.log").read_text(encoding="utf-8")  # CP5: механика — в лог, CEO не будит
    assert not (tmp_path / "ceo-wake.log").exists()
    monkeypatch.setattr(D, "_WAIT_WATCH", {})  # ожидание снято -> acts пуст, файл обязан уйти
    D._blind_tick()
    assert not f.exists()
