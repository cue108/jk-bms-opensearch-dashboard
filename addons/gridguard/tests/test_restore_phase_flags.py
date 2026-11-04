"""Restart seed for the day-scoped phase flags (owner incident 2026-10-25:
phase stuck at 'morning' after a mid-day restart → no eve roll → no night row
in the frontend). Same hole class as restore_reserve_tier — same seam."""
from datetime import datetime, time as _time, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from gridguard.main import AppState, restore_phase_flags
from gridguard.verdict import Phase, current_phase

TZ = ZoneInfo("Europe/Madrid")          # scenario tz; any fixed tz works
# Scenario: mid-day restart, between window start and eve — the ONLY
# discriminator between DAY_HOLD and MORNING is morning_done.
CFG = SimpleNamespace(window_start=_time(6, 0), window_end=_time(23, 0),
                      eve_start=_time(18, 0), final_offset_min=60)
NOW = datetime(2026, 10, 25, 12, 30, tzinfo=TZ)


class FakeDeps:
    """Canned durable evidence — the restore must be pure over this input."""

    def __init__(self, events): self._events = events
    def phase_evidence(self, since_iso: str):
        return [e for e in self._events if e["ts"] >= since_iso]


def test_midday_restart_restores_morning_done_from_durable_grid_off():
    deps = FakeDeps([{"ts": datetime(2026, 10, 25, 9, 10, tzinfo=TZ).isoformat(),
                      "event": "grid_off", "result": "ok"}])
    st = AppState()
    restore_phase_flags(st, deps, NOW, CFG)
    assert st.morning_done is True
    assert st.night_over is False
    assert current_phase(NOW, CFG, st.morning_done, st.night_over) is Phase.DAY_HOLD


def test_no_evidence_keeps_flags_false_and_phase_morning():
    st = AppState()
    restore_phase_flags(st, FakeDeps([]), NOW, CFG)
    assert st.morning_done is False
    assert current_phase(NOW, CFG, st.morning_done, st.night_over) is Phase.MORNING


def test_failed_grid_off_does_not_restore_morning_done():
    deps = FakeDeps([{"ts": datetime(2026, 10, 25, 9, 10, tzinfo=TZ).isoformat(),
                      "event": "grid_off", "result": "failed"}])
    st = AppState()
    restore_phase_flags(st, deps, NOW, CFG)
    assert st.morning_done is False


def test_recent_night_over_event_restores_night_over():
    deps = FakeDeps([{"ts": (NOW - timedelta(hours=6)).isoformat(),
                      "event": "night_over", "result": ""}])
    st = AppState()
    restore_phase_flags(st, deps, NOW, CFG)
    assert st.night_over is True


def test_run_boots_the_restore_before_the_loop():
    """Wiring pin: run() must call restore_phase_flags before while True."""
    src = (Path(__file__).resolve().parents[1] / "gridguard" / "main.py").read_text()
    run_body = src.split("def run(", 1)[1]
    assert "restore_phase_flags(" in run_body.split("while True:", 1)[0]
