"""Evening ratio planner — pure truth table + failsafe math.

The decision is the ONE ratio
`available >= SAFETY_FACTOR × need` (need = median Wh of CLEAN night buckets);
t_fire is demoted from decision core to failsafe timer inside the learned
night ("t_fire as decision core → at most an info line").
"""
from datetime import datetime, timedelta

import pytest

from gridguard.config import load_config
from gridguard.planner import (build_off_timer, build_on_timer,
                               hold_reason, local_cron, plan_evening)
from gridguard.planner import night_floor_wh

GOOD = {"TZ": "Europe/Madrid", "WINDOW_START": "09:00", "WINDOW_END": "22:00",
        "EVE_START": "20:00", "SAFETY_FACTOR": "1.2",
        "OPENSEARCH_URL": "http://x",
        "BMS_INDEX": "i", "EWCLOUD_EMAIL": "a", "EWCLOUD_PASS": "b",
        "SW_MAIN_DEVICEID": "d", "SW_MAIN_DEVICEKEY": "k", "SW_MAIN_IP": "1.2.3.4",
        "NTFY_WEBHOOK": "https://n/t"}
cfg = load_config(GOOD)
NOW = datetime(2026, 10, 18, 20, 10)      # eve window
NIGHT_LEN = 11.0                          # learned-night length passed by _eve

kinds = lambda plan: [k for k, _ in plan.actions]

# floor_wh = SoC curve(TRUE_FLOOR_V) ×
# full_ah × live pack_v — 1,5 % × 200 Ah × 25,6 V = 76,8 Wh (the BMS counter
# is audit/display only).
FLOOR_PCT = 1.5                                        # kept as a plain fixture number (the table left the source)
FLOOR_WH = 76.8                                         # fixture energy base @ _plan defaults


def _plan(available=1000.0, need=800.0, switch="off", night_len=NIGHT_LEN, c=cfg,
          forecast_ok=True, pack_v=25.6, full_ah=200.0,
          load_wh_h=None):          # True = the ratio cases isolate the ONE ratio rule;
    return plan_evening(NOW, available_wh=available, need_wh=need,   # the sun gate gets its own tests
                        switch_state=switch, night_len_hours=night_len, cfg=c,
                        forecast_ok=forecast_ok, pack_v=pack_v, full_ah=full_ah,
                        load_wh_h=load_wh_h)


def test_failsafe_without_pack_voltage_is_not_guessed():
    """Sensor data beats textbook constants — no
    pack voltage → no schedule is guessed; the conservative ON covers the
    night instead (never a phantom crossing, never an idle gamble)."""
    plan = _plan(available=1000.0, need=800.0, switch="off", pack_v=None)
    assert kinds(plan) == ["bring_on_now"]


# --- timer machinery kept from v3 (build_on/off_timer, local_cron) --------------
def test_local_cron():
    assert local_cron(datetime(2026, 10, 19, 4, 0)) == "0 4 * * *"
    assert local_cron(datetime(2026, 10, 19, 22, 45)) == "45 22 * * *"


def test_build_timer_shapes():
    on = build_on_timer(datetime(2026, 10, 19, 4, 0), cfg.tz)
    assert on["type"] == "repeat" and on["do"] == {"switch": "on"} and on["enabled"] == 1
    assert on["atLocal"] == "0 4 * * *" and on["at"].endswith("* * *")
    off = build_off_timer(datetime(2026, 10, 19, 8, 0), cfg.tz)
    assert off["do"] == {"switch": "off"} and off["atLocal"] == "0 8 * * *"


# --- the curve floor left the source -------------------------------------------
def test_floor_follows_pack_voltage():
    """OUTSIDE the UPS mode there is NO floor —
    the crossing does not consume the pack voltage anymore (same available,
    any pack_v → byte-identical raw crossing). INSIDE the UPS band the tier
    percent converts with full_ah × LIVE pack_v (night_floor_wh): higher
    voltage → higher band → less headroom → the stop fires SOONER."""
    raw_hi = _plan(available=200.0, need=300.0, pack_v=26.0)
    raw_lo = _plan(available=200.0, need=300.0, pack_v=13.0)
    assert kinds(raw_hi) == kinds(raw_lo) == ["failsafe_timer"]
    assert raw_hi.actions[0][1]["at_local"] == raw_lo.actions[0][1]["at_local"]
    # the UPS band is the one floor left — and it DOES follow pack_v
    t_hi = _tier_plan(available_wh=400.0, need_wh=300.0, pack_v=26.0,
                      reserve_floor_pct=5)
    t_lo = _tier_plan(available_wh=400.0, need_wh=300.0, pack_v=13.0,
                      reserve_floor_pct=5)
    assert kinds(t_hi) == kinds(t_lo) == ["failsafe_timer"]
    assert t_hi.actions[0][1]["at_local"] < t_lo.actions[0][1]["at_local"]


# --- the NEED anchors every evening decision -----------------------------------
def test_unknown_need_or_available_stays_conservative():
    # unknown need / available / pack_v → never release, never schedule:
    assert kinds(_plan(available=None, need=800.0, switch="on")) == ["keep_grid"]
    assert kinds(_plan(available=1000.0, need=None, switch="on")) == ["keep_grid"]
    assert kinds(_plan(available=None, need=None, switch="on")) == ["keep_grid"]
    assert kinds(_plan(available=1000.0, need=None, switch="off")) == ["bring_on_now"]
    assert kinds(_plan(available=None, need=800.0, switch="off")) == ["bring_on_now"]


def test_safety_factor_rides_the_rate_not_a_gate():
    """The factor buffers the PLAN's rate (the night eats factor × measured):
    a higher factor moves the programmed crossing EARLIER."""
    lo = _plan(available=900.0, need=800.0, switch="off")            # factor1.2
    c15 = load_config({**GOOD, "SAFETY_FACTOR": "1.5"})
    hi = _plan(available=900.0, need=800.0, switch="off", c=c15)
    assert kinds(lo) == kinds(hi) == ["failsafe_timer"]
    assert hi.actions[0][1]["at_local"] < lo.actions[0][1]["at_local"]


# --- case ON: headroom → spend the stored solar + schedule the return --------------
def test_case_on_headroom_releases_and_schedules():
    """NORTH STAR: no idle half-full battery — spend now, grid back at the
    crossing; the forecast does NOT gate the eve (the gate lives in the morning)."""
    plan = _plan(available=1000.0, need=800.0, switch="on", forecast_ok=False)
    assert kinds(plan) == ["release_night", "failsafe_timer"]
    assert plan.actions[0][1] == {}                             # release: no payload
    dt_h = (1000.0 ) / (800.0 * 1.2 / 11.0)   # raw head ÷ rate (no floor outside UPS)
    assert plan.actions[1][1]["at_local"] == NOW + timedelta(hours=dt_h)


def test_case_on_headless_keeps_grid_with_floor_reason():
    plan = _plan(available=0.0, need=800.0, switch="on")      # head ≤ 0 (raw law)
    assert kinds(plan) == ["keep_grid"]
    assert plan.actions[0][1]["reason"] == "reason: battery at floor reserve only"


def test_case_on_need_unknown_reason_zahlenfrei():
    plan = _plan(available=1000.0, need=None, switch="on")
    reason = plan.actions[0][1]["reason"]
    assert reason == "reason: night need not yet determinable"
    assert not any(ch.isdigit() for ch in reason)


def test_case_on_available_unknown_reason_zahlenfrei():
    plan = _plan(available=None, need=800.0, switch="on")
    assert plan.actions[0][1]["reason"] == "reason: state of charge not yet determinable"
    assert _plan(available=None, need=None, switch="on").actions[0][1]["reason"] \
        == "reason: night need not yet determinable"


# --- case OFF: the crossing schedule -----------------------------------------------
def test_case_off_schedule_math():
    """Raw head (no floor outside the UPS mode);
    rate = safety × need ÷ night_len; the programmed ON fires at the crossing."""
    plan = _plan(available=100.0, need=100.0, switch="off")
    assert kinds(plan) == ["failsafe_timer"]
    on = plan.actions[0][1]
    dt_h = (100.0 ) / (100.0 * 1.2 / 11.0)   # raw head < rate×11 (within the night)
    assert on["at_local"] == NOW + timedelta(hours=dt_h)
    assert (on["at_local"] - NOW).total_seconds() == pytest.approx(dt_h * 3600)
    assert on["fire_str"] == on["at_local"].strftime("%H:%M")
    assert on["timer"]["atLocal"] == local_cron(on["at_local"])
    assert on["timer"]["do"] == {"switch": "on"} and on["timer"]["enabled"] == 1


def test_case_off_below_floor_waits_for_the_observation():
    # available 0 → head ≤ 0 = OUT of the middle band (raw law: only the BMS
    # zeros leave the band) — the plan WAITS for
    # the cell-voltage observation (`voltage_floor_hit` → the emergency owns
    # the edge), never a rescue and never a dead past-timer
    plan = _plan(available=0.0, need=80.0, switch="off")
    assert kinds(plan) == ["none"]


def test_case_off_beyond_horizon_battery_carries_all():
    # v5 identity: beyond the horizon ⟺ available ≥ safety×need + floor —
    # the battery covers the BUFFERED whole night plus the reserve → no timer
    plan = _plan(available=1100.0, need=800.0, switch="off")
    assert kinds(plan) == ["none"]
    assert plan.actions[0][1] == {"reason": "battery carries the whole night"}


def test_case_off_tiny_night_len_uses_min_rate():
    # night_len below 1 h → rate floors at need/1.0 (max(1.0, night_len));
    # raw head = 60 Wh → a half-hour span keeps the timer path alive
    # (below the minimum span the plan rescues
    # directly instead of programming)
    plan = _plan(available=60.0, need=100.0, switch="off", night_len=1.0)
    assert plan.actions[0][1]["at_local"] == NOW + timedelta(hours=60.0 / 120.0)


# --- hold_reason: the single wording source for grid_hold pushes --------------------
def test_hold_reason_wording_source():
    assert hold_reason(1000.0, 900.0, cfg) == "available 1000 Wh < 1.2× night need 900 Wh"
    assert hold_reason(None, 800.0, cfg) == "reason: state of charge not yet determinable"
    assert hold_reason(1000.0, None, cfg) == "reason: night need not yet determinable"
    assert hold_reason(None, None, cfg) == "reason: night need not yet determinable"


# --- the forecast never gates the eve ------------------------------------------
def test_eve_forecast_never_gates_the_spend_or_schedule():
    # OFF + known need + bad forecast → the schedule runs (the gate = morning)
    assert kinds(_plan(available=100.0, need=100.0, switch="off",
                       forecast_ok=False)) == ["failsafe_timer"]
    # the default forecast_ok (False) changes nothing on the ON-side either
    no_arg = plan_evening(NOW, available_wh=1000.0, need_wh=800.0, switch_state="on",
                          night_len_hours=NIGHT_LEN, cfg=cfg, pack_v=25.6,
                          full_ah=200.0)
    assert kinds(no_arg) == ["release_night", "failsafe_timer"]


def test_hold_reason_sun_line_when_ratio_held():
    # ratio held + sun blocked → the SUN is the reason (it carries the detail)
    assert hold_reason(1000.0, 800.0, cfg, forecast_ok=False,
                       sun_reason="no weather data") \
        == "solar not released: no weather data"
    # planner-internal call without a reason (plan_evening passes none): no dangling colon
    assert hold_reason(1000.0, 800.0, cfg, False) == "solar not released"
    # ratio NOT ok + sun blocked → the ratio stays the binding reason (unchanged)
    assert hold_reason(1000.0, 900.0, cfg, False, "no weather data") \
        == "available 1000 Wh < 1.2× night need 900 Wh"
    # the None reasons stay untouched, sun or no sun
    assert hold_reason(1000.0, None, cfg, False) == "reason: night need not yet determinable"
    assert hold_reason(None, 800.0, cfg, False, "no weather data") \
        == "reason: state of charge not yet determinable"


# --- reserve tiers in the night plan -------------------------------------------
def _tier_plan(**kw):
    args = dict(available_wh=190.0, need_wh=100.0, switch_state="off",
                night_len_hours=NIGHT_LEN, cfg=cfg, forecast_ok=True,
                pack_v=25.6, full_ah=200.0)
    args.update(kw)
    return plan_evening(NOW, **args)


def test_reserve_floor_pct_raises_the_night_floor():
    """reserve_floor_pct (a BMS-SOC tier percent) is the ONLY floor outside
    physics: the planner works in Wh, the tier
    percent converts with the SAME full_ah × pack_v math (tier % is a BMS-SOC
    percent of the BMS capacity figure). Base (no tier): the RAW crossing;
    with the 30 % band the head clamps to zero = OUT of the band."""
    tier_wh = 30.0 / 100.0 * 200.0 * 25.6
    assert tier_wh == pytest.approx(1536.0)
    # OFF: the base crossing sits inside the learned night (raw head); with
    # the UPS tier (30 % SOC) the head (100 − 1536) clamps to zero = OUT of
    # the band → the plan waits for the cell-voltage observation
    base = _plan(available=100.0, need=100.0, switch="off")
    dt_h = (100.0 ) / (100.0 * 1.2 / 11.0)
    assert base.actions[0][1]["at_local"] == NOW + timedelta(hours=dt_h)
    tier = _tier_plan(reserve_floor_pct=30)
    assert kinds(tier) == ["none"]
    # ON: the base spends the stored solar; under the tier band NOTHING sits
    # above the reserve → keep_grid (nothing is released below the tier)
    assert kinds(_plan(available=100.0, need=100.0, switch="on"))[0] == "release_night"
    assert kinds(_tier_plan(switch_state="on", reserve_floor_pct=30)) == ["keep_grid"]


def test_reserve_floor_none_or_zero_keeps_the_raw_crossing():
    """None/0 = the documented pass-through (main only sends it when >0): no
    band at all — every variant runs on the SAME raw crossing."""
    base = _plan(available=100.0, need=100.0, switch="off")
    for kw in ({}, {"reserve_floor_pct": None}, {"reserve_floor_pct": 0}):
        plan = _tier_plan(available_wh=100.0, **kw)
        assert plan.actions[0][1]["at_local"] == base.actions[0][1]["at_local"]


# --- programmed crossing from the reference rate when clean need is unknown ----
def test_crossing_programmed_from_the_reference_rate_when_clean_need_unknown():
    """NO CLEAN night → need=None, but the reference RATE
    (load_wh_h = the post-sunset grid-off clip mean Wh/h) IS a need
    basis — the evening gate must program its PROJECTED crossing into the
    actor (the existing t_fire = now + head/rate mechanism), never the
    conservative bring_on_now that the grid-off rule then suppresses to
    NOTHING (the failure mode: suppressed every eve tick, NO timer programmed,
    battery bleeding toward the floor with nothing armed)."""
    plan = _plan(available=190.0, need=None, switch="off", load_wh_h=100.0)
    assert kinds(plan) == ["failsafe_timer"]
    on = plan.actions[0][1]
    dt_h = (190.0 ) / (100.0 * cfg.safety_factor)   # rate = load × safety
    assert on["at_local"] == NOW + timedelta(hours=dt_h)
    assert on["fire_str"] == on["at_local"].strftime("%H:%M")


def test_conservative_branch_when_neither_need_nor_rate_is_known():
    """The unknown-UNKNOWN case keeps the conservative ON (no invented
    number): need AND load_wh_h both absent → bring_on_now exactly as
    before — the boundary this fix must not blur."""
    assert kinds(_plan(available=190.0, need=None, switch="off")) == ["bring_on_now"]


# --- minimum projected span (the 30-minute rule) --------------------------------
def test_projection_under_thirty_minutes_keeps_the_grid_on():
    """(1) a forecast/plan SHORTER than 30 minutes
    must KEEP the grid ON state — releasing for a span the battery barely
    covers only manufactures an ON → OFF → ON cycle (the live failure: ON
    at 21:00 via the crossing, the eve released at 21:01 with the next
    crossing programmed at 21:10); (2) when the grid is ALREADY ON because
    the battery is too empty, the plans must not switch to GRID OFF
    anymore — too-empty = a short projected span, so both rules land on
    the same reality anchor: the projected grid-off span must reach
    cfg.release_min_span_min (30) minutes, measured against the LIVE
    headroom and the reference rate. The hour-long release stays."""
    # need 1000 @ safety 1.2 / 11 h = 109.1 Wh/h → a 20 Wh raw head = ~11 min → KEEP
    short = _plan(available=20.0, need=1000.0, switch="on")
    assert kinds(short) == ["keep_grid"]
    # a real hour of span → release_night + the programmed crossing unchanged
    span = _plan(available=109.1, need=1000.0, switch="on")
    assert kinds(span) == ["release_night", "failsafe_timer"]


def test_very_short_crossing_is_a_valid_clamped_timer_and_out_of_band_waits():
    """The tier/UPS band
    and the trivial `head ÷ rate` formula stay UNTOUCHED (raw head outside the
    UPS). The middle band's short crossing = a
    VALID honest timer (pre-programmed for the offline window —
    low_capacity_on is reach-gated), clamped to the NEXT cron minute so it
    can never land in the past (the dead 21:24:07-for-21:24 class). Only OUT
    of the band (head ≤ 0) the plan waits "none" for the cell-voltage
    observation — no premature rescue."""
    # middle band, tiny raw head → the crossing = the imminent seconds → clamp +1 min
    clamped = _plan(available=1.0, need=1000.0, switch="off")
    assert kinds(clamped) == ["failsafe_timer"]
    assert clamped.actions[0][1]["at_local"] == NOW + timedelta(minutes=1)
    # out of the band (head ≤ 0) → wait for the observation: no timer, no rescue
    oob = _plan(available=0.0, need=1000.0, switch="off")
    assert kinds(oob) == ["none"]
    # the middle band's honest short crossing stays a real, unclamped timer
    short = _plan(available=20.0, need=1000.0, switch="off")
    assert kinds(short) == ["failsafe_timer"]


# --- DAY-OF-ACTION: the UPS belongs to the forecast day -------------------------
# Fixture: NOW = 20:10 on 10-18, need 800 @ safety 1.2 / 11 h = 87.27 Wh/h;
# floor 50 % = 0.5 × 200 Ah × 25.6 V = 2560 Wh. available 3000 → under the
# 50 % floor the crossing is 440 Wh ÷ 87.27 ≈ 5.04 h = 01:12 on 10-19
# (after midnight); without a floor it is 34.4 h → beyond the 11 h horizon.
def test_a_timer_firing_after_midnight_ignores_todays_storm_floor():
    """The flagged bug, pinned: the eve runs ON the storm day, but its
    crossing lands on the NEXT (clean) day — a day-after UPS timer must NOT
    be programmed ("consider the day the actor acts and not the day the
    programming happens"). Contrast: without next-day information the OLD
    law arms exactly that timer."""
    old = plan_evening(NOW, available_wh=3000.0, need_wh=800.0,
                       switch_state="off", night_len_hours=NIGHT_LEN, cfg=cfg,
                       forecast_ok=True, pack_v=25.6, full_ah=200.0,
                       reserve_floor_pct=50)
    assert kinds(old) == ["failsafe_timer"]          # the day-after UPS timer
    new = plan_evening(NOW, available_wh=3000.0, need_wh=800.0,
                       switch_state="off", night_len_hours=NIGHT_LEN, cfg=cfg,
                       forecast_ok=True, pack_v=25.6, full_ah=200.0,
                       reserve_floor_pct=50, reserve_floor_pct_next=0)
    assert kinds(new) == ["none"]
    assert new.actions[0][1]["reason"] == "battery carries the whole night"


def test_tomorrows_storm_floor_stops_the_battery_on_tomorrow():
    """The mirror case: today is clean, tomorrow carries the storm. The
    floorless crossing lands after midnight → the plan re-bases on TOMORROW's
    50 % tier and programs the stop ON the storm day — the reserve arrives
    WITH the bad weather, never a day late."""
    plan = plan_evening(NOW, available_wh=3000.0, need_wh=800.0,
                        switch_state="off", night_len_hours=NIGHT_LEN, cfg=cfg,
                        forecast_ok=True, pack_v=25.6, full_ah=200.0,
                        reserve_floor_pct=0, reserve_floor_pct_next=50)
    assert kinds(plan) == ["failsafe_timer"]
    at = plan.actions[0][1]["at_local"]
    assert at == NOW + timedelta(hours=440.0 / (800.0 * 1.2 / 11.0))
    assert at.date() == datetime(2026, 10, 19).date()
