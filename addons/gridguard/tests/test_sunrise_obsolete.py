"""Owner ruling §31 (2026-10-28 ~20:55) + §32 ruling round — the ON-timer is
the WiFi-BLACKOUT INSURANCE ONLY; once actor contact exists after sunrise a
programmed grid-on is obsolete (direct reaction exists).

TWO LAYERS:
  L1 EVE-CLAMP — the crossing is compared against the ABSOLUTE next solar
     edge (the array-onset), not only against `now + night_len` (the length
     drifts). THE FIXTURE = tonight's incident: available 2341.8 ≈ need
     2350.5 → the crossing lands 11:38-class = just under the length clamp →
     on the current code the eve ARMS a post-sunrise fire (RED); the new
     code refuses → the "battery carries until sunrise" none-branch.
  L2 MORNING-CLEARING — a standing post-sunrise ON-timer with actor contact
     gets cleared, REGARDLESS of switch_state, OUTSIDE the grid-off gate (the
     yesterday-finding: the grid-off only runs on switch==on; tomorrow the grid
     is off → the live 11:38 would fire blind). Phase scope §32:
     MORNING/DAY_HOLD only. Clear scope §32 carve-out: only timers whose
     fire time-of-day >= the onset time-of-day (the post-sunrise class);
     pre-dawn timers = the blackout insurance = UNTOUCHED.

ONSET DERIVATION (§32): PRIMARY = the MEASURED last solar-night span END
(_e, the array-onset datetime) stepped +24 h (concept applied throughout with
night_len's spans-first source; seasonal drift ~1-2 min/day = minutes in
either direction vs the 3.5-h miss); FALLBACK = profile.array_onset_h
anchored to the coming local day; BOTH ABSENT = None → the length clamp
stays the sole guard (no guess).
"""
from datetime import datetime, timedelta

import pytest

from gridguard.config import load_config
from gridguard.main import AppState, tick
from test_main_tick import D, INSUFF, PACK_V, SUFF, FakeDeps, buckets

# --- tonight's incident constants (handoff §31, live 2026-10-28) ---------------
AVAIL_INC = 2341.8                     # eve available (counter × V_avg, live)
NEED_INC = 2350.5                      # learned CLEAN-median need (live)
INC = {"TZ": "Europe/Madrid", "WINDOW_START": "09:00", "WINDOW_END": "22:00",
       "EVE_START": "20:00", "SAFETY_FACTOR": "1.0",
       "OPENSEARCH_URL": "http://x", "BMS_INDEX": "i", "EWCLOUD_EMAIL": "a",
       "EWCLOUD_PASS": "b", "SW_MAIN_DEVICEID": "d", "SW_MAIN_DEVICEKEY": "k",
       "SW_MAIN_IP": "1.2.3.4", "NTFY_WEBHOOK": "https://n/t"}
inc_cfg = load_config(INC)
NEED2350 = buckets(NEED_INC, NEED_INC)

T = lambda h, m: datetime(2026, 10, 28, h, m)      # the incident local day
REM_INC = AVAIL_INC / PACK_V                       # the counter fixture (Ah)
FLOOR_INC = 0.015 * 222.0 * PACK_V                 # 85.248 Wh (curve × capacity × V)
# the measured solar night of the incident: END = this morning's onset 07:55,
# length 16 h = the max the eve's sanity gate accepts (the "just under the
# length clamp" headroom of tonight's borderline case)
ONSET_BASE = datetime(2026, 10, 28, 7, 55)
SPANS = {datetime(2026, 10, 28, 7, 55):
         (ONSET_BASE - timedelta(hours=16), ONSET_BASE)}
NEXT_ONSET = ONSET_BASE + timedelta(days=1)         # at the eve: tomorrow 07:55
# the incident crossing: rate = 2350.5/16 = 146.90625 W; head = 2256.552 Wh
# → +15.3606 h from 20:05 → 2026-10-29 11:26:38 (the 11:38-class result)
CROSSING_INC = T(20, 5) + timedelta(
    hours=(AVAIL_INC - FLOOR_INC) / (NEED_INC / 16.0))


def _mk(**kw):
    """FakeDeps + the solar-night spans attribute (solar_night_windows reads
    deps.spans via getattr — construction kwarg not supported); defaults =
    the incident fixture (counter at the incident available, OFF, clean eve)."""
    spans = kw.pop("spans", SPANS)
    kw.setdefault("remaining_ah", REM_INC)
    kw.setdefault("switch", "off")
    kw.setdefault("inventory_timers", [])
    kw.setdefault("buckets", NEED2350)
    deps = FakeDeps(**kw)
    deps.spans = spans
    return deps


# --- L1 RED: the incident must NOT be armed -----------------------------------
def test_eve_refuses_the_1138_class_post_sunrise_crossing():
    """TONIGHT'S INCIDENT as the red fixture: on the current code this tick
    programs the 11:26/11:38-class timer (just under the length clamp) — the
    new code must take the sunrise none-branch: NOTHING armed, the standard
    carries push, honest §27 state. (The branch's reason string itself is
    planner-level-pinned in test_planner_refuses_… — the none dispatch never
    journals the reason, pre-existing behavior.)"""
    st = AppState(); st.morning_done = True
    deps = _mk()
    tick(inc_cfg, st, deps, T(20, 5))
    assert st.evening_verdict == "safe_clear"       # RED: was "rescue_timer"
    assert deps.plan_seen == []                     # RED: nothing programmed now
    assert not deps.acts                            # no write, no clear (§31(c))
    assert any("battery carries" in t for _, t in deps.pushes)
    # the crossing really was post-sunrise under the OLD arithmetic (sanity)
    assert CROSSING_INC.date() == datetime(2026, 10, 29).date()
    assert CROSSING_INC >= NEXT_ONSET
    # … and under the length clamp it WOULD have been armed (the outer guard
    # alone let it through — the reason the absolute edge is required)
    assert (CROSSING_INC - T(20, 5)).total_seconds() / 3600.0 < 16.0
    # no timer state invented: the §27 fields stay empty
    assert st.last_planned_fire is None and st.rescue_timer_set is False


def test_eve_clamp_does_not_clear_anything_itself():
    """§31(c): the eve only DECIDES not to arm — the clamp runs no clearing
    step (the pre-existing carry-flip disarm is separate machinery, pinned by
    its own tests)."""
    st = AppState(); st.morning_done = True
    deps = _mk()
    tick(inc_cfg, st, deps, T(20, 5))
    assert not [e for e, _ in deps.audits if e == "sunrise_timer_clear"]
    assert not [a for a in deps.acts if a[0] == "disable_on_timers"]
    assert deps.inventory_timers == []              # nothing armed, nothing cleared


def test_eve_keeps_a_legitimate_predawn_crossing():
    """The clamp only refuses POST-SUNRISE crossings: the incident pack a bit
    fuller → crossing 05:10 (pre-onset 07:55) → armed as before."""
    st = AppState(); st.morning_done = True
    deps = _mk(remaining_ah=900.0 / PACK_V)   # available ≈ 900 Wh
    tick(inc_cfg, st, deps, T(20, 5))
    plan = deps.plan_seen[-1]
    assert plan[0] == "failsafe_timer"
    at = plan[1][0][1]["at_local"]
    assert at < NEXT_ONSET                          # pre-sunrise = the insurance


def test_clamp_length_guard_stays_the_outer_guard():
    """Both guards stay: a crossing beyond `now + night_len` keeps the OLD
    length-clamp wording even when no absolute edge is known."""
    from gridguard.planner import plan_evening
    st = AppState(); st.morning_done = True
    deps = _mk(remaining_ah=REM_INC, switch="off", inventory_timers=[],
                    buckets=NEED2350, spans={})      # no spans, profile insufficient
    tick(inc_cfg, st, deps, T(20, 5))
    assert deps.plan_seen[-1][0] == "failsafe_timer"  # no onset known → old behavior
    # the same fixture WITH the length exceeded → the old none-branch wording
    plan = plan_evening(T(20, 5), available_wh=10 * NEED_INC, need_wh=NEED_INC,
                        switch_state="off", night_len_hours=16.0, cfg=inc_cfg,
                        pack_v=PACK_V, full_ah=222.0)
    assert plan.actions[0][1]["reason"] == "battery carries the whole night"


# --- L1 planner contract: next_onset param ------------------------------------
def test_planner_refuses_a_post_sunrise_timer_on_the_off_case():
    from gridguard.planner import plan_evening
    plan = plan_evening(T(20, 5), available_wh=AVAIL_INC, need_wh=NEED_INC,
                        switch_state="off", night_len_hours=16.0, cfg=inc_cfg,
                        pack_v=PACK_V, full_ah=222.0, next_onset=NEXT_ONSET)
    assert [k for k, _ in plan.actions] == ["none"]
    assert plan.actions[0][1]["reason"] == "battery carries until sunrise"


def test_planner_on_case_releases_but_never_arms_a_post_sunrise_timer():
    from gridguard.planner import plan_evening
    plan = plan_evening(T(20, 5), available_wh=AVAIL_INC, need_wh=NEED_INC,
                        switch_state="on", night_len_hours=16.0, cfg=inc_cfg,
                        pack_v=PACK_V, full_ah=222.0, next_onset=NEXT_ONSET)
    # the NIGHT RELEASE stays (§13 spending is its own doctrine) — only the
    # obsolete post-sunrise timer is dropped
    assert [k for k, _ in plan.actions] == ["release_night"]
    # and the pre-sunrise case keeps the full combo byte-identical
    keep = plan_evening(T(20, 5), available_wh=AVAIL_INC, need_wh=NEED_INC,
                        switch_state="on", night_len_hours=16.0, cfg=inc_cfg,
                        pack_v=PACK_V, full_ah=222.0,
                        next_onset=datetime(2026, 10, 30, 7, 55))
    assert [k for k, _ in keep.actions] == ["release_night", "failsafe_timer"]


def test_planner_next_onset_default_keeps_the_old_path():
    from gridguard.planner import plan_evening
    plan = plan_evening(T(20, 5), available_wh=AVAIL_INC, need_wh=NEED_INC,
                        switch_state="off", night_len_hours=16.0, cfg=inc_cfg,
                        pack_v=PACK_V, full_ah=222.0)   # next_onset absent
    assert [k for k, _ in plan.actions] == ["failsafe_timer"]   # unchanged default


# --- onset derivation (§32 source order) --------------------------------------
def test_onset_primary_is_the_measured_span_end_stepped_to_the_next_morning():
    from gridguard.main import next_solar_onset
    got = next_solar_onset(T(20, 5), SPANS, INSUFF)
    assert got == NEXT_ONSET                       # _e (this morning) + 24 h
    # after the onset has passed the same evening → the NEXT morning again
    assert next_solar_onset(datetime(2026, 10, 29, 8, 0), SPANS, INSUFF) \
        == datetime(2026, 10, 30, 7, 55)


def test_onset_falls_back_to_the_learned_profile_hour():
    from gridguard.main import next_solar_onset
    assert next_solar_onset(T(20, 5), {}, SUFF) == datetime(2026, 10, 29, 8, 0)
    # early (pre-onset) the coming edge is TODAY's learned hour
    assert next_solar_onset(T(6, 0), {}, SUFF) == datetime(2026, 10, 28, 8, 0)


def test_onset_absent_stays_none_no_guess():
    from gridguard.main import next_solar_onset, solar_onset_on
    assert next_solar_onset(T(20, 5), {}, INSUFF) is None
    assert solar_onset_on(T(10, 0).date(), {}, INSUFF) is None


def test_solar_onset_on_anchors_the_measured_base_to_the_requested_date():
    from gridguard.main import solar_onset_on
    # base 2026-10-28 07:55 → tomorrow's date = +24 h (drift shifts minutes,
    # never the date — the rule this function documents)
    assert solar_onset_on(datetime(2026, 10, 29).date(), SPANS, INSUFF) \
        == datetime(2026, 10, 29, 7, 55)
    assert solar_onset_on(datetime(2026, 10, 28).date(), SPANS, INSUFF) \
        == datetime(2026, 10, 28, 7, 55)


# --- L2 MORNING-CLEARING ------------------------------------------------------
ARM_1138 = [{"type": "repeat", "do": {"switch": "on"}, "enabled": 1,
             "atLocal": "38 11 * * *"}]            # tonight's LIVE armed 11:38
ARM_0530 = [{"type": "repeat", "do": {"switch": "on"}, "enabled": 1,
             "atLocal": "30 5 * * *"}]             # the pre-dawn insurance


def _st_armed():
    st = AppState()
    st.last_planned_fire = datetime(2026, 10, 29, 11, 38)
    st.plan_ideal = datetime(2026, 10, 29, 11, 38)
    st.rescue_timer_set = True
    return st


def test_morning_clears_the_post_sunrise_timer_even_with_the_grid_off():
    """THE §31 RED: tomorrow the grid = OFF (the grid-off-gate would never run) —
    the armed 11:38 must still be cleared by the STANDALONE step."""
    st = _st_armed()
    deps = _mk(switch="off", inventory_timers=list(ARM_1138),
                    spans=SPANS, remaining_ah=REM_INC, buckets=[])
    tick(inc_cfg, st, deps, T(10, 0))
    assert [a[0] for a in deps.acts] == ["disable_on_timers"]   # RED: was []
    clr = [f for e, f in deps.audits if e == "sunrise_timer_clear"]
    assert len(clr) == 1
    assert clr[0]["result"] == "ok"
    assert clr[0]["inputs"]["reason"] == \
        "sunrise: scheduled switch-on obsolete"
    assert clr[0]["inputs"]["armed"] == ["38 11 * * *"]   # device truth atLocal
    assert clr[0]["inputs"]["onset"] == datetime(2026, 10, 28, 7, 55).isoformat()
    assert all(t["enabled"] == 0 for t in deps.inventory_timers)  # read-back truth
    # §27 fields → None (the next planned/announced reads tell the truth)
    assert st.last_planned_fire is None and st.plan_ideal is None
    assert st.rescue_timer_set is False


def test_morning_clears_regardless_of_switch_on_too():
    st = _st_armed()
    deps = _mk(switch="on", inventory_timers=list(ARM_1138),
                    spans=SPANS, remaining_ah=REM_INC, buckets=[],
                    min_v=3.14, soc_pct=40.0)   # at the floor: grid-off must not own it
    tick(inc_cfg, st, deps, T(10, 0))
    assert any(e == "sunrise_timer_clear" for e, _ in deps.audits)


def test_pre_dawn_insurance_is_never_cleared():
    """§32 carve-out: the 05:30 timer (fire < onset) = the blackout insurance
    — untouched in the morning, even with contact (a future blackout is
    unknowable; the night's probe-cadence lag is what the insurance covers)."""
    st = _st_armed()
    deps = _mk(switch="off", inventory_timers=list(ARM_0530),
                    spans=SPANS, remaining_ah=REM_INC, buckets=[])
    tick(inc_cfg, st, deps, T(10, 0))
    assert not [a for a in deps.acts if a[0] == "disable_on_timers"]
    assert not [e for e, _ in deps.audits if e == "sunrise_timer_clear"]
    assert deps.inventory_timers[0]["enabled"] == 1        # still armed
    assert st.last_planned_fire is not None                # §27 untouched


def test_pre_dawn_insurance_survives_even_before_the_onset():
    """§31 blackout preservation (a): the morning arriving BEFORE the onset —
    the insurance lives until the onset (and beyond, per the §32 carve-out)."""
    st = _st_armed()
    deps = _mk(switch="off", inventory_timers=list(ARM_0530),
                    spans=SPANS, remaining_ah=REM_INC, buckets=[])
    st.night_over = True                       # probe fired early → MORNING phase
    tick(inc_cfg, st, deps, datetime(2026, 10, 28, 6, 30))
    assert not [a for a in deps.acts if a[0] == "disable_on_timers"]
    assert deps.inventory_timers[0]["enabled"] == 1


def test_blackout_without_contact_never_clears():
    """§31 blackout preservation (b): reach DOWN at/after the onset → no
    obsolescence decision (no contact = no decision; the timer must fire
    blind if needed)."""
    st = _st_armed()
    deps = _mk(switch="off", inventory_timers=list(ARM_1138),
                    spans=SPANS, remaining_ah=REM_INC, buckets=[],
                    reachable_flag=False)
    tick(inc_cfg, st, deps, T(10, 0))
    assert not [a for a in deps.acts if a[0] == "disable_on_timers"]
    assert not [e for e, _ in deps.audits if e == "sunrise_timer_clear"]
    assert deps.inventory_timers[0]["enabled"] == 1
    assert st.last_planned_fire is not None


def test_the_eve_phase_never_runs_the_clearing_step():
    """§32 phase scope: inside EVE_PLAN/FINAL the step would fight the eve's
    own fresh arming every poll — only MORNING/DAY_HOLD may clear."""
    st = _st_armed()
    deps = _mk(switch="off", inventory_timers=list(ARM_1138),
                    spans=SPANS, remaining_ah=REM_INC, buckets=[])
    tick(inc_cfg, st, deps, T(20, 10))
    assert not [e for e, _ in deps.audits if e == "sunrise_timer_clear"]
    assert deps.inventory_timers[0]["enabled"] == 1        # untouched by the step


def test_no_onset_known_means_no_clearing_decision():
    """No guess without an edge: spans gone + insufficient profile → the step
    stays silent (the length-clamp/eve machinery keeps its own guards)."""
    st = _st_armed()
    deps = _mk(switch="off", inventory_timers=list(ARM_1138),
                    spans={}, remaining_ah=REM_INC, buckets=[])
    tick(inc_cfg, st, deps, T(10, 0))
    assert not [e for e, _ in deps.audits if e == "sunrise_timer_clear"]
    assert deps.inventory_timers[0]["enabled"] == 1
