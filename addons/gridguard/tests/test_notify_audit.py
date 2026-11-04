import json
import pytest
from gridguard.audit import AuditLog
from gridguard.messages import msg

def test_audit_whitelist_accepts_and_rejects(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    log.record("tick", phase="eve_plan", verdict="safe_clear",
               inputs={"min_cell_v": 3.18}, action={"type": "none"},
               channel="cloud", result="ok", sequence="123")
    rec = json.loads((tmp_path / "audit.jsonl").read_text().splitlines()[-1])
    assert rec["event"] == "tick" and rec["phase"] == "eve_plan" and "ts" in rec
    with pytest.raises(ValueError):
        log.record("x", devicekey="secret-should-never-land")
    with pytest.raises(ValueError):
        log.record("x", password="nope")

# T6: msg key FINAL SET (Interfaces) — morning_off removed, grid_off/low_capacity_on/planner_set added
# v4 §9.7: grid_hold de-duplicated (title carries the state), night_release added
# 2026-10-21: manual_override added (owner — recognize a hand on the switch)
# §27 2026-10-27: night_plan_armed(+_ideal) — the final push leads with the
# write-confirmed armed, the plan ideal rides last when it differs (both dicts)
KEYS = {"emergency_on", "low_capacity_on", "rescue_set", "safe_clear", "grid_off",
        "no_channel", "night_plan", "night_plan_armed", "night_plan_armed_ideal",
        "timer_fallback", "planner_set", "grid_hold",
        "night_release", "manual_override"}

def test_message_key_set_de_and_en():
    from gridguard.messages import MSG
    assert set(MSG["de"]) == KEYS
    assert set(MSG["en"]) == KEYS

def test_push_title_localization():
    # owner ruling 2026-11-04 ("English ONLY" + compatibility): the STORED kind
    # is the stable English key, the ntfy display title stays localized — de
    # keeps the former German kind values byte-identical (history + phone);
    # unknown kinds pass through (display-truth: never invent).
    from gridguard.messages import title
    assert title("NIGHT PLAN", "de") == "NACHTPLAN"
    assert title("EMERGENCY", "de") == "NOTSCHUTZ"
    assert title("MAIN OFF", "de") == "MAIN AUS"
    assert title("NIGHT PLAN", "en") == "NIGHT PLAN"
    assert title("NIGHT PLAN", "xx") == "NIGHT PLAN"      # unknown lang → en
    assert title("UNKNOWN KIND", "de") == "UNKNOWN KIND"

def test_grid_hold_wording_is_not_doubled():
    # §9.7 fix: previously "GRID ON / grid on: Rest…" — the reason stands alone now
    assert msg("de", "grid_hold", reason="reason: night need not yet determinable") \
        == "reason: night need not yet determinable"
    assert msg("en", "grid_hold", reason="available 768 Wh < 1.2× night need 800 Wh") \
        == "available 768 Wh < 1.2× night need 800 Wh"
    assert "Netz an" not in msg("de", "grid_hold", reason="x")
    assert "Grid on" not in msg("en", "grid_hold", reason="x")

def test_night_release_format_de_and_en():
    s = msg("de", "night_release", available=1024.0, need=800.0, factor=1.2)
    assert "Netz aus" in s and "1024" in s and "800" in s and "1.2" in s
    s2 = msg("en", "night_release", available=1024.0, need=800.0, factor=1.2)
    assert "grid off" in s2 and "1024" in s2 and s2 != s

def test_grid_off_format_de_and_en():
    s = msg("de", "grid_off", remaining=55.0, solar=22.4, soc=50)
    assert "MAIN AUS" in s and "55.0" in s and "22.4" in s and "50" in s
    s2 = msg("en", "grid_off", remaining=55.0, solar=22.4, soc=50)
    assert "MAIN OFF" in s2 and "55.0" in s2 and "22.4" in s2 and "50" in s2

def test_messages_de_and_en():
    s = msg("de", "rescue_set", time="04:00")
    assert "04:00" in s and "Nachtplan" in s
    s2 = msg("en", "rescue_set", time="04:00")
    assert "04:00" in s2 and s2 != s

def test_push_never_raises(monkeypatch):
    from gridguard import notify
    def boom(*a, **k):
        raise OSError("down")
    monkeypatch.setattr(notify.urllib.request, "urlopen", boom)
    assert notify.push("https://ntfy.invalid/t", "t", "m") is False

def test_push_survives_request_construction_errors():
    # Brief-bug regression: Request()/encode() must be inside the try —
    # malformed URLs or unencodable payloads must not raise into the loop.
    from gridguard import notify
    assert notify.push("http://[::1", "t", "m") is False
    assert notify.push("https://ntfy.invalid/t", "t", "a\ud800b") is False
