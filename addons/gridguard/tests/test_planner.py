"""v4 evening ratio planner (spec v4 §3/§6) — pure truth table + failsafe math.

v4 replaces the v3 empirical planner: the decision is the ONE ratio
`available >= SAFETY_FACTOR × need` (need = median Wh of CLEAN night buckets);
t_fire is demoted from decision core to failsafe timer inside the learned
night (spec §8: "t_fire as decision core → at most an info line").
"""
from datetime import datetime, timedelta

import pytest

from gridguard.config import load_config
from gridguard.planner import (build_off_timer, build_on_timer,
                               hold_reason, local_cron, plan_evening)
from gridguard.verdict import soc_pct_from_v

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

# L2 wave1 (owner ruling 2026-10-25): floor_wh = SoC curve(TRUE_FLOOR_V) ×
# full_ah × live pack_v — 1,5 % × 200 Ah × 25,6 V = 76,8 Wh (replaces the old
# ah_on × pack_v = 128 Wh; the BMS counter is audit/display only now).
FLOOR_PCT = soc_pct_from_v(cfg.true_floor_v)              # 1,5 %
FLOOR_WH = FLOOR_PCT / 100.0 * 200.0 * 25.6               # 76,8 Wh @ _plan defaults


def _plan(available=1000.0, need=800.0, switch="off", night_len=NIGHT_LEN, c=cfg,
          forecast_ok=True, pack_v=25.6, full_ah=200.0):  # True = the ratio cases isolate the ONE ratio rule;
    return plan_evening(NOW, available_wh=available, need_wh=need,   # the sun gate gets its own tests
                        switch_state=switch, night_len_hours=night_len, cfg=c,
                        forecast_ok=forecast_ok, pack_v=pack_v, full_ah=full_ah)


def test_failsafe_without_pack_voltage_is_not_guessed():
    """Owner doctrine (2026-10-20): sensor data beats textbook constants — no
    pack voltage → no schedule is guessed; the conservative ON covers the
    night instead (never a phantom crossing, never an idle gamble)."""
    plan = _plan(available=1000.0, need=800.0, switch="off", pack_v=None)
    assert kinds(plan) == ["bring_on_now"]


def test_floor_follows_pack_voltage():
    """floor = SoC curve(TRUE_FLOOR_V) × full_ah × the LIVE pack voltage: higher
    voltage → higher floor → less usable headroom → the failsafe fires SOONER
    (owner's remaining×voltage math, now anchored on the voltage curve × the
    BMS capacity figure instead of ah_on). Numbers chosen inside the learned
    night: avail ∈ [1.2×need; need+floor)."""
    high_v = _plan(available=362.0, need=300.0, switch="off", pack_v=26.0)
    low_v = _plan(available=362.0, need=300.0, switch="off", pack_v=13.0)
    assert high_v.actions[0][0] == low_v.actions[0][0] == "failsafe_timer"
    assert high_v.actions[0][1]["at_local"] < low_v.actions[0][1]["at_local"]


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


# --- L2 wave1 (owner ruling 2026-10-25): the floor is the VOLTAGE curve ------
def test_floor_wh_is_voltage_curve_percent_of_capacity():
    """floor_wh = SoC curve(TRUE_FLOOR_V)=1,5 % × full_ah × pack_v (76,8 Wh @
    200 Ah / 25,6 V) — the crossing math runs exactly on top of it; WITHOUT
    capacity everything falls into the conservative branch (no guessed floor)."""
    assert FLOOR_PCT == pytest.approx(1.5) and FLOOR_WH == pytest.approx(76.8)
    plan = _plan(available=190.0, need=100.0, switch="off")
    dt_h = (190.0 - FLOOR_WH) / (100.0 * 1.2 / 11.0)       # head ÷ rate
    assert plan.actions[0][1]["at_local"] == NOW + timedelta(hours=dt_h)
    # full_ah=None → unknown branch: OFF → conservative bring_on_now,
    # ON → keep_grid — never a phantom crossing from a guessed floor
    assert kinds(_plan(available=190.0, need=100.0, switch="off",
                       full_ah=None)) == ["bring_on_now"]
    assert kinds(_plan(available=190.0, need=100.0, switch="on",
                       full_ah=None)) == ["keep_grid"]


# --- v5 (NORTH STAR §13): the NEED anchors every evening decision ------------------
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
    lo = _plan(available=1000.0, need=800.0, switch="off")            # factor1.2
    c15 = load_config({**GOOD, "SAFETY_FACTOR": "1.5"})
    hi = _plan(available=1000.0, need=800.0, switch="off", c=c15)
    assert kinds(lo) == kinds(hi) == ["failsafe_timer"]
    assert hi.actions[0][1]["at_local"] < lo.actions[0][1]["at_local"]


# --- case ON: headroom → spend the stored solar + schedule the return --------------
def test_case_on_headroom_releases_and_schedules():
    """NORTH STAR: no idle half-full battery — spend now, grid back at the
    crossing; the forecast does NOT gate the eve (the gate lives in the morning)."""
    plan = _plan(available=1000.0, need=800.0, switch="on", forecast_ok=False)
    assert kinds(plan) == ["release_night", "failsafe_timer"]
    assert plan.actions[0][1] == {}                             # release: no payload
    dt_h = (1000.0 - FLOOR_WH) / (800.0 * 1.2 / 11.0)   # floor = 1,5 % × full_ah × pack_v = 76,8
    assert plan.actions[1][1]["at_local"] == NOW + timedelta(hours=dt_h)


def test_case_on_headless_keeps_grid_with_floor_reason():
    plan = _plan(available=50.0, need=800.0, switch="on")      # 50 < floor 76,8
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
    """floor = SoC curve(TRUE_FLOOR_V) × full_ah × LIVE pack_v (76,8 Wh);
    rate = safety × need ÷ night_len; the programmed ON fires at the crossing."""
    plan = _plan(available=190.0, need=100.0, switch="off")
    assert kinds(plan) == ["failsafe_timer"]
    on = plan.actions[0][1]
    dt_h = (190.0 - FLOOR_WH) / (100.0 * 1.2 / 11.0)   # head 113,2 < rate×11 (within the night)
    assert on["at_local"] == NOW + timedelta(hours=dt_h)
    assert (on["at_local"] - NOW).total_seconds() == pytest.approx(dt_h * 3600)
    assert on["fire_str"] == on["at_local"].strftime("%H:%M")
    assert on["timer"]["atLocal"] == local_cron(on["at_local"])
    assert on["timer"]["do"] == {"switch": "on"} and on["timer"]["enabled"] == 1


def test_case_off_below_floor_fires_now():
    # available 50 < floor 76,8 → t_fire = now (still inside the horizon)
    plan = _plan(available=50.0, need=80.0, switch="off")
    assert plan.actions[0][1]["at_local"] == NOW


def test_case_off_beyond_horizon_battery_carries_all():
    # v5 identity: beyond the horizon ⟺ available ≥ safety×need + floor —
    # the battery covers the BUFFERED whole night plus the reserve → no timer
    plan = _plan(available=1100.0, need=800.0, switch="off")
    assert kinds(plan) == ["none"]
    assert plan.actions[0][1] == {"reason": "battery carries the whole night"}


def test_case_off_tiny_night_len_uses_min_rate():
    # night_len below 1 h → rate floors at need/1.0 (max(1.0, night_len));
    # head = 12 Wh above the 76,8-watt floor
    plan = _plan(available=88.8, need=100.0, switch="off", night_len=0.5)
    assert plan.actions[0][1]["at_local"] == NOW + timedelta(hours=12.0 / 120.0)


# --- hold_reason: the single wording source for grid_hold pushes --------------------
def test_hold_reason_wording_source():
    assert hold_reason(1000.0, 900.0, cfg) == "available 1000 Wh < 1.2× night need 900 Wh"
    assert hold_reason(None, 800.0, cfg) == "reason: state of charge not yet determinable"
    assert hold_reason(1000.0, None, cfg) == "reason: night need not yet determinable"
    assert hold_reason(None, None, cfg) == "reason: night need not yet determinable"


# --- §13 supersedes §9.1's EVE half: the forecast never gates the eve ---------------
def test_eve_forecast_never_gates_the_spend_or_schedule():
    # OFF + known need + bad forecast → the schedule runs (the gate = morning)
    assert kinds(_plan(available=190.0, need=100.0, switch="off",
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


# --- owner ruling 2026-10-25 (part 3): reserve tiers in the night plan ----------
def _tier_plan(**kw):
    args = dict(available_wh=190.0, need_wh=100.0, switch_state="off",
                night_len_hours=NIGHT_LEN, cfg=cfg, forecast_ok=True,
                pack_v=25.6, full_ah=200.0)
    args.update(kw)
    return plan_evening(NOW, **args)


def test_reserve_floor_pct_raises_the_night_floor():
    """reserve_floor_pct (a BMS-SOC tier percent) joins the voltage floor via
    max(): the planner works in Wh, so the tier percent converts with the SAME
    full_ah × pack_v math as floor_wh (tier % is a BMS-SOC percent of the BMS
    capacity figure → both compose naturally). The night crossing then honors
    the reserve band: floor 1,5 % = 76,8 Wh → 30 % = 1536 Wh."""
    tier_wh = 30.0 / 100.0 * 200.0 * 25.6
    assert tier_wh == pytest.approx(1536.0)
    # OFF: the base crossing sits inside the learned night; with the tier floor
    # the head (190 − 1536) clamps to zero → the failsafe fires NOW (the tier)
    base = _plan(available=190.0, need=100.0, switch="off")
    dt_h = (190.0 - FLOOR_WH) / (100.0 * 1.2 / 11.0)
    assert base.actions[0][1]["at_local"] == NOW + timedelta(hours=dt_h)
    tier = _tier_plan(reserve_floor_pct=30)
    assert kinds(tier) == ["failsafe_timer"]
    assert tier.actions[0][1]["at_local"] == NOW
    # ON: the base spends the stored solar; under the tier band NOTHING sits
    # above the reserve → keep_grid (nothing is released below the tier)
    assert kinds(_plan(available=190.0, need=100.0, switch="on"))[0] == "release_night"
    assert kinds(_tier_plan(switch_state="on", reserve_floor_pct=30)) == ["keep_grid"]


def test_reserve_floor_none_or_zero_keeps_the_voltage_floor():
    """None/0 = the documented pass-through (main only sends it when >0): the
    OLD voltage-curve floor stays byte-identical — same crossing time."""
    base = _plan(available=190.0, need=100.0, switch="off")
    for kw in ({}, {"reserve_floor_pct": None}, {"reserve_floor_pct": 0}):
        plan = _tier_plan(**kw)
        assert plan.actions[0][1]["at_local"] == base.actions[0][1]["at_local"]
