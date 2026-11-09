from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from gridguard.bms import BmsError, BmsReading
from gridguard.config import load_config
from gridguard.learn import NightProfile
from gridguard.main import AppState, RealDeps, tick
from gridguard.switchlink import ChannelError, CloudClient
from gridguard.verdict import soc_pct_from_v

GOOD = {"TZ": "Europe/Madrid", "WINDOW_START": "09:00", "WINDOW_END": "22:00",
        "EVE_START": "20:00", "SAFETY_FACTOR": "1.2",
        "OPENSEARCH_URL": "http://x", "BMS_INDEX": "i", "EWCLOUD_EMAIL": "a",
        "EWCLOUD_PASS": "b", "SW_MAIN_DEVICEID": "d", "SW_MAIN_DEVICEKEY": "k",
        "SW_MAIN_IP": "1.2.3.4", "NTFY_WEBHOOK": "https://n/t"}
cfg = load_config(GOOD)
D = lambda h, m: datetime(2026, 10, 18, h, m)
PACK_V = 25.6                                        # fixture stand-in voltage; production = pack.voltage from the BMS doc (4S ≈13 V, pinned in test_bms_weather)
SUFF = NightProfile(array_onset_h=8.0, array_offset_h=19.0,
                    samples_used=448, sufficient=True)
INSUFF = NightProfile(None, None, 0, False)


def buckets(*wh):
    """Chronological CLEAN night buckets (round-A shape) → need = median(wh)."""
    return [{"night": f"2026-10-{10 + i}", "wh": w, "minutes": 780,
             "on_minutes": 0, "status": "CLEAN"} for i, w in enumerate(wh)]


NEED300 = buckets(300.0, 300.0)                       # need = 300 Wh
NEED800 = buckets(800.0, 800.0)                       # need = 800 Wh
FULL_AH = 222.0                                       # FakeDeps capacity anchor (BMS soc.full_ah)
AH400 = 400.0 / PACK_V                                # remaining_ah fixture (400 Wh at v_avg=PACK_V)
# --- owner physics ruling 2026-10-26 (§19/§20): available = remaining_ah × V_avg
# (the BMS counter = the trusted mid-range instrument; FakeDeps v_avg defaults
# to PACK_V → available = AH400 × PACK_V = 400.0 Wh). The corrected-voltage SOC
# path no longer feeds the eve's available; floor = the voltage curve stays the
# deep band's instrument (planner byte-untouched).
def _corr(min_v, charge=0.0):
    """IR-Korrektur wie verdict.corrected_min_cell_v (Defaults r_cell_ohm)."""
    return min_v - charge * cfg.r_cell_ohm


FLOOR_WH = soc_pct_from_v(cfg.true_floor_v) / 100.0 * FULL_AH * PACK_V   # 1,5 % ≈ 85,25 Wh
V400 = 3.26                                           # minimum-cell fixture (deep-band disclosure)
AV400 = 400.0                                         # available = AH400 × PACK_V (§20 anchor)
# v5 schedule: floor = SoC curve(TRUE_FLOOR_V)×full_ah×pack_v; rate = safety × need ÷ night_len
FIRE_EVE = D(20, 10) + timedelta(hours=(AV400 - FLOOR_WH) / (300.0 * 1.2 / 11.0))
# same math with SUFF edges (onset 8 − offset 19 = 13 h learned night)
FIRE_SUFF = D(20, 10) + timedelta(hours=(AV400 - FLOOR_WH) / (300.0 * 1.2 / 13.0))


class FakeDeps:
    def __init__(self, min_v=3.30, charge=0.0, remaining_ah=100.0, pack_current=0.0,
                 soc_pct=None, switch="on", reachable_flag=True,
                 cloud=True, solar_window=None, inventory_timers=None,
                 fail_read=False, fail_acts=False, profile=None, buckets=None,
                 sun_ok=True, sun_reason="test sun free", radiation_j=12e6,
                 pack_v=PACK_V, reserve_tier_inputs=None, v_avg=PACK_V):
        self.min_v, self.charge, self.remaining_ah = min_v, charge, remaining_ah
        self.pack_v = pack_v
        # owner physics ruling 2026-10-26 (§20): the Wh-conversion voltage is a
        # cheap AVERAGE over the recent minutes — the fake defaults it to
        # pack_v so the counter value IS the available (v_avg × remaining_ah)
        self.v_avg = v_avg
        # restart seed (owner fix 2026-10-25): the newest reserve_tier event's
        # INPUTS — dict = found, None = no docs, Exception instance = raises
        self.reserve_tier_inputs = reserve_tier_inputs
        self.pack_current, self.soc_pct = pack_current, soc_pct
        self.switch = switch
        self.reachable_flag, self.cloud = reachable_flag, cloud
        self.solar_window = solar_window if solar_window is not None else []
        self.inventory_timers = inventory_timers if inventory_timers is not None else \
            [{"type": "repeat", "do": {"switch": "on"}, "enabled": 1, "atLocal": "0 4 * * *"}]
        self.fail_read, self.fail_acts = fail_read, fail_acts
        self.profile = profile if profile is not None else INSUFF   # no edges → 11 h night
        self.buckets = list(buckets) if buckets is not None else []  # v4: [] → need None
        # T3 §9.1 sun gate: the fake forecast verdict (+ the audit mirror r)
        self.sun_ok, self.sun_reason = sun_ok, sun_reason
        self.radiation_j = radiation_j
        self.last_radiation_j, self.forecast_calls = None, 0
        self.acts, self.pushes, self.audits = [], [], []
        self.plan_seen = []                      # T7: (lead-kind, actions) per apply_plan call
        # Phase C: the forecast onset prediction the eve consults — None by
        # default (old behavior: no prediction source); pred_calls counts asks
        self.pred, self.pred_calls = None, 0

    # --- reads ---
    def read_state(self):
        if self.fail_read:
            raise RuntimeError("thing list failed: {'error': 401}")
        return self.switch, list(self.inventory_timers)
    def timer_exists(self):
        return False   # T5 deps API: F2 rollover re-sync (brief transcription dropped it)
    def bms_latest(self):
        return BmsReading("t", self.min_v,
                          self.min_v + 0.01 if self.min_v is not None else None,
                          self.charge,
                          self.soc_pct, self.remaining_ah, self.pack_v,
                          full_ah=222.0)   # L2 wave1: capacity anchor (BMS figure)
    def bms_samples_2h(self):
        t0 = D(19, 0)
        return [(t0, self.remaining_ah), (t0 + timedelta(hours=1), self.remaining_ah)]
    def pack_v_avg(self):                                # owner §20: conversion average
        return self.v_avg
    def bms_solar_window(self): return list(self.solar_window)
    def latest_reserve_tier(self):                       # restart seed (owner fix 2026-10-25)
        if isinstance(self.reserve_tier_inputs, Exception):
            raise self.reserve_tier_inputs
        return self.reserve_tier_inputs
    def night_profile(self): return self.profile            # edges → night_len (v4)
    def solar_day_prediction(self, target_date):            # Phase C: the eve's
        self.pred_calls += 1                               # horizon prediction ask
        return self.pred
    def solar_night_windows(self):                          # T6: measured spans win over edges
        return getattr(self, "spans", None) or {}
    def night_buckets(self): return list(self.buckets)      # v4: CLEAN-median need feed
    def yield_learning(self): return None                   # L1: learning only (no effect)
    def forecast_ok(self):                                  # T3 §9.1 sun gate (Deps contract)
        self.forecast_calls += 1
        self.last_radiation_j = self.radiation_j            # audit mirror of r
        return self.sun_ok, self.sun_reason
    def reachable(self): return self.reachable_flag
    def cloud_alive(self): return self.cloud
    # --- actions ---
    def act(self, action, *, value=None, at=None, require_verify=False, reason=None):
        if self.fail_acts and action != "disable_on_timers":
            return False
        self.acts.append((action, value, at, require_verify, reason))
        if action == "switch_on": self.switch = "on"
        if action == "switch_off": self.switch = "off"
        if action == "disable_on_timers":
            self.inventory_timers = [          # §12.2: BOTH classes disarmed (matches RealDeps)
                {**t, "enabled": 0}
                for t in self.inventory_timers]
            self.audit("act", action={"type": "disable_on_timers"})   # T5 shape: audit like RealDeps
        return True
    def push(self, title, text): self.pushes.append((title, text))
    def audit(self, event, **f): self.audits.append((event, f))
    def apply_plan(self, plan):
        # T7: record what the planner handed us (lead action kind + full actions), always OK
        self.plan_seen.append((plan.actions[0][0] if plan.actions else None,
                               list(plan.actions)))
        return True


def eve_bring_on(cls=FakeDeps, **kw):
    """The eve bring-on path under the voltage rule (owner ruling2026-10-25):
    the cells sit ON the floor (corrected ≤ TRUE_FLOOR_V) with switch grid-off —
    the emergency already fired today and is confirmed via emergency_ack,
    so _eve (not the trigger) owns the conservative ON when the
    night need is unknown. Without the Ack the floor-trigger low
    would rebound earlier and never reach this path."""
    st = AppState()
    st.emergency_ack = D(20, 10).date()
    st.low_capacity_ack = D(20, 10).date()
    kw.setdefault("min_v", 3.14)                  # corrected 3.14 ≤ 3.15 → floor
    kw.setdefault("switch", "off")
    kw.setdefault("inventory_timers", [])
    kw.setdefault("buckets", [])
    return st, cls(**kw)


def test_night_probe_uses_reachability():
    st, deps = AppState(), FakeDeps(reachable_flag=False, switch="on", soc_pct=80.0)
    tick(cfg, st, deps, D(3, 30), probe_due=True)
    assert not st.night_over
    deps.reachable_flag = True
    tick(cfg, st, deps, D(3, 35), probe_due=True)
    assert st.night_over
    tick(cfg, st, deps, D(3, 40), probe_due=True)
    # R2'b path: SOC>=50 while reachable → grid-off (no sun condition; phase-transition proof)
    assert any(a[0] == "switch_off" for a in deps.acts)   # morning grid-off fires (healthy preset)


def test_read_state_failure_throttled_push():
    st, deps = AppState(), FakeDeps(fail_read=True)
    tick(cfg, st, deps, D(9, 20))
    assert any(e == "read_state_failed" for e, _ in deps.audits)          # audit every tick
    assert len(deps.pushes) == 1
    tick(cfg, st, deps, D(9, 30))
    assert len(deps.pushes) == 1                                          # <6h → no re-push
    tick(cfg, st, deps, D(9, 20) + timedelta(hours=7))
    assert len(deps.pushes) == 2


def test_deep_floor_emergency_in_with_verify():
    st, deps = AppState(), FakeDeps(min_v=2.89, switch="off")
    tick(cfg, st, deps, D(14, 0))
    # §12.2 final-review F1: the emergency clears ALL timers BEFORE it switches
    assert deps.acts == [("disable_on_timers", None, None, False, None),
                         ("switch_on", None, None, True, None)]
    assert deps.pushes[0][0] == "EMERGENCY" and st.emergency_ack == D(14, 0).date()
    tick(cfg, st, deps, D(14, 5))                     # same day: no repeat act
    assert len([a for a in deps.acts if a[0] == "switch_on"]) == 1


def test_emergency_on_clears_timers_first_then_switches():
    # final-review F1 (b): §12.2 „on every intervention" — order pinned:
    # disable_on_timers act BEFORE the switch_on act (emergency protection), and
    # the clear result flag lands in the audit input timer_clear.
    st, deps = AppState(), FakeDeps(min_v=2.89, switch="off")
    tick(cfg, st, deps, D(14, 0))
    assert [a[0] for a in deps.acts] == ["disable_on_timers", "switch_on"]
    assert deps.inventory_timers[0]["enabled"] == 0     # the ON-timer class is disarmed
    ev = [f for e, f in deps.audits if e == "emergency_on"]
    assert ev and ev[0]["inputs"]["timer_clear"] is True


def test_emergency_on_runs_even_when_timer_clear_fails():
    # final-review F1 (c): best-effort proof — clear False → switch_on happens
    # ANYWAY (the emergency must never depend on a timer block); the audit
    # carries timer_clear False, no extra push for the clear.
    class NoClear(FakeDeps):
        def act(self, action, **kw):
            if action == "disable_on_timers":
                self.acts.append((action, None, None, False, None)); return False
            return super().act(action, **kw)
    st, deps = AppState(), NoClear(min_v=2.89, switch="off")
    tick(cfg, st, deps, D(14, 0))
    assert [a[0] for a in deps.acts] == ["disable_on_timers", "switch_on"]
    assert deps.switch == "on"                         # the safety ON still happened
    ev = [f for e, f in deps.audits if e == "emergency_on"]
    assert ev and ev[0]["inputs"]["timer_clear"] is False
    assert [p[0] for p in deps.pushes] == ["EMERGENCY"]  # NO push for the clear


def test_low_capacity_in_when_floor_reached():
    # Voltage successor of the old counter trigger (owner ruling2026-10-25):
    # corrected minimum cell 3,14 V ≤ TRUE_FLOOR_V → low-capacity IN; the
    # counter (4,5 Ah) sits beside it only as an audit number.
    st, deps = AppState(), FakeDeps(min_v=3.14, remaining_ah=4.5, switch="off")
    tick(cfg, st, deps, D(21, 0))
    assert [a[0] for a in deps.acts] == ["disable_on_timers", "switch_on"]  # clear first (F1)
    assert deps.pushes[0][0] == "LOW BATTERY"
    ev = [f for e, f in deps.audits if e == "low_capacity_on"]
    assert ev and ev[0]["inputs"] == {"min_cell_v": 3.14, "min_cell_corr": 3.14,
                                      "floor_v": cfg.true_floor_v,
                                      "remaining_ah": 4.5, "charge_a": 0.0,
                                      "timer_clear": True}


def test_low_capacity_healthy_voltage_stays_out():
    # Charging situation: the charging plateau carries the measured voltage
    # (3,25 V − 9 A × 5 mΩ = 3,205 V corrected) → no floor → no IN. The old
    # "sun keeps the counter trigger up" condition retired with the counter
    # trigger — the voltage now carries the case alone.
    st, deps = AppState(), FakeDeps(min_v=3.25, charge=9.0, remaining_ah=4.5,
                                    switch="on")
    tick(cfg, st, deps, D(14, 0))
    assert not any(a[0] == "switch_on" for a in deps.acts)


def test_aus_solar_collision_first_then_off():
    st, deps = AppState(), FakeDeps(min_v=3.30, remaining_ah=55.0, pack_current=22.0,
                                    soc_pct=None, switch="on", solar_window=[22.0, 23.5])
    tick(cfg, st, deps, D(11, 0))
    types = [a[0] for a in deps.acts]
    assert types == ["disable_on_timers", "switch_off"]       # T1 order enforced
    assert deps.acts[1][3] is True                            # require_verify
    assert deps.pushes[0][0] == "MAIN OFF" and deps.switch == "off"
    assert deps.inventory_timers[0]["enabled"] == 0           # ON-timer deactivated


def test_aus_blocked_without_reachability():
    st, deps = AppState(), FakeDeps(remaining_ah=55.0, pack_current=22.0, switch="on",
                                    reachable_flag=False, solar_window=[22.0])
    tick(cfg, st, deps, D(11, 0))
    assert deps.acts == []


def test_aus_soc_path():
    st, deps = AppState(), FakeDeps(remaining_ah=60.0, soc_pct=55.0, switch="on")
    tick(cfg, st, deps, D(12, 0))
    assert [a[0] for a in deps.acts] == ["disable_on_timers", "switch_off"]


def test_aus_skipped_when_switch_already_off():
    st, deps = AppState(), FakeDeps(remaining_ah=55.0, pack_current=22.0, switch="off",
                                    solar_window=[22.0])
    tick(cfg, st, deps, D(11, 0))
    assert deps.acts == []


def test_collision_failure_aborts_grid_off():
    class NoClear(FakeDeps):
        def act(self, action, **kw):
            if action == "disable_on_timers":
                self.acts.append((action, None, None, False, None)); return False
            return super().act(action, **kw)
    st, deps = AppState(), NoClear(remaining_ah=55.0, pack_current=22.0, switch="on",
                                   solar_window=[22.0])
    tick(cfg, st, deps, D(11, 0))
    assert not any(a[0] == "switch_off" for a in deps.acts)


# --- T3 §9.1 sun gate on the morning grid-off release (conservative on missing data) --
# §13 v7 SUPERSEDED: test_morning_aus_suppressed_when_forecast_not_ok — the morning
# consults NO forecast at all (see test_v7_drain_is_unconditional_above_the_floor:
# forecast_calls == 0); there is no suppression branch anymore to pin.


def test_morning_aus_audit_carries_drain_ground():
    # v7: the release happens because the battery sits above the safety floor and
    # has stored solar to give — ground="drain", floor in force, NO forecast facts.
    # owner2026-10-25: the floor is the VOLTAGE (floor_v + min_cell_corr), not
    # the counter's Ah figure anymore.
    st, deps = AppState(), FakeDeps(remaining_ah=60.0, soc_pct=55.0, switch="on",
                                    solar_window=[], sun_ok=True)
    tick(cfg, st, deps, D(12, 0))
    assert [a[0] for a in deps.acts] == ["disable_on_timers", "switch_off"]
    off = [f for e, f in deps.audits if e == "grid_off"][0]
    assert off["inputs"] == {"remaining_ah": 60.0, "floor_v": cfg.true_floor_v,
                             "min_cell_corr": 3.30, "solar_a": 0.0, "soc": 55.0,
                             "ground": "drain"}
    assert st.morning_done and deps.pushes[0][0] == "MAIN OFF"
    assert deps.forecast_calls == 0                            # the day predicts nothing


# §13 v7 SUPERSEDED: test_morning_gate_not_consulted_when_aus_condition_false — the
# "no candidate" concept is gone; forecast_calls == 0 is pinned on the drain release
# and the hold case on test_v7_safety_floor_outranks_everything.


def test_eve_release_night_runs_aus_collision_first():
    # v7: the grid-off owns the release at eve-time too — but when ITS clear fails (the
    # channel collision), the eve's release-branch is the live RETRY path and still
    # runs collision-first with a REASONED switch_off. DisableOnce = fail at the grid-off,
    # succeed at the eve.
    class DisableOnce(FakeDeps):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw); self._dis = 0
        def act(self, action, **kw):
            if action == "disable_on_timers":
                self._dis += 1
                if self._dis == 1:
                    self.acts.append((action, None, None, False, None)); return False
            return super().act(action, **kw)
    st = AppState(); st.morning_done = True
    deps = DisableOnce(min_v=V400, remaining_ah=AH400, switch="on", buckets=NEED300)
    tick(cfg, st, deps, D(20, 10))
    assert [(a[0], a[3]) for a in deps.acts] == [("disable_on_timers", False),
                                                 ("disable_on_timers", False),
                                                 ("switch_off", True)]  # grid-off clear failed → eve retry
    assert deps.acts[2][4] is not None                      # the retry's switch_off carries a reason
    assert deps.switch == "off" and deps.inventory_timers[0]["enabled"] == 0
    assert st.evening_verdict == "rescue_timer" and st.morning_done  # combo end state = armed return
    # the combo: the spend is followed by the programmed return at the crossing
    assert deps.plan_seen[-1][1][1][0] == "failsafe_timer"   # the plan's second action
    assert st.rescue_timer_set is True and st.last_planned_fire == FIRE_EVE
    grid_off = [f for e, f in deps.audits if e == "grid_off"]
    assert grid_off and grid_off[0]["result"] == "ok"
    assert grid_off[0]["inputs"] == {"available_wh": AV400, "need_wh": 300.0,
                                     "safety": 1.2,
                                     # the eve still SHIPS the gate facts (audit)
                                     "radiation_sum_j": 12e6, "weather_min_j": None,
                                     "forecast_ok": True}
    night_rel = [t for p, t in deps.pushes if p == "NIGHT PLAN" and "300" in t]
    assert night_rel                                      # release push names the need


def test_eve_release_night_aborts_on_collision_failure():
    # T1 collision-first: disable_on_timers False → NO switch_off, honest NO CHANNEL
    class NoClear(FakeDeps):
        def act(self, action, **kw):
            if action == "disable_on_timers":
                self.acts.append((action, None, None, False, None)); return False
            return super().act(action, **kw)
    st = AppState(); st.morning_done = True
    deps = NoClear(min_v=V400, remaining_ah=AH400, switch="on", inventory_timers=[], buckets=NEED300)
    tick(cfg, st, deps, D(20, 10))
    assert not any(a[0] == "switch_off" for a in deps.acts)
    assert deps.pushes and deps.pushes[0][0] == "NO CHANNEL" \
        and "release_night" in deps.pushes[0][1]             # honest failure signal


def test_eve_keep_grid_hold_push_reason():
    # v4 case 2: switch ON + not ok (need unknown, <2 CLEAN) → keep + grid_hold.
    # v7/L2: the fixture sits AT the VOLTAGE floor (3,19 V − 9 A × 5 mΩ = 3,145 V
    # corrected ≤ TRUE_FLOOR_V) — the only place where an eve-keep survives the
    # day-drain rule (above the floor the grid-off releases first). The counter (4,0 Ah)
    # is audit only now.
    st = AppState(); st.morning_done = True
    deps = FakeDeps(min_v=3.19, charge=9.0, remaining_ah=4.0, switch="on",
                    inventory_timers=[], buckets=[])
    tick(cfg, st, deps, D(20, 10))
    assert deps.plan_seen[0][0] == "keep_grid"
    assert deps.acts == []                                   # keep writes nothing
    assert st.evening_verdict == "grid_hold"
    assert deps.pushes and deps.pushes[0][0] == "GRID ON"
    assert deps.pushes[0][1] == "reason: night need not yet determinable"  # no doubling


def test_eve_keep_grid_at_floor_reserve():
    # v5: ON + no headroom above the floor → keep_grid with the floor reason.
    # §20 anchor: the counter (3,0 Ah → 76,8 Wh) sits BELOW the voltage floor
    # (85,25 Wh) → head = 0. The floor-trigger case (3,14 V corrected) stays.
    st = AppState(); st.morning_done = True
    deps = FakeDeps(min_v=3.14, remaining_ah=3.0, switch="on",
                    inventory_timers=[], buckets=NEED800)
    tick(cfg, st, deps, D(20, 10))
    assert deps.plan_seen[0][0] == "keep_grid"
    assert deps.pushes[0][1] == "reason: battery at floor reserve only"


# --- T3 §9.1 sun gate on the evening release (eve-release v4) -------------------
def test_eve_spends_regardless_of_sun_gate():
    # §13: the FORECAST does not gate the eve spend (the gate = the morning):
    # sun_ok=False still runs the release; the gate facts still ride the audit
    st = AppState(); st.morning_done = True
    deps = FakeDeps(min_v=V400, remaining_ah=AH400, switch="on", inventory_timers=[],
                    buckets=NEED300, sun_ok=False, sun_reason="no weather data")
    tick(cfg, st, deps, D(20, 10))
    assert [(a[0], a[3]) for a in deps.acts] == [("disable_on_timers", False),
                                                 ("switch_off", True)]
    assert deps.switch == "off"                               # the spend happened
    assert st.evening_verdict == "rescue_timer"                # combo end state = armed return
    assert deps.forecast_calls == 1                         # still consulted + audited


def test_eve_release_runs_when_sun_gate_ok():
    # the same spend + the combo's second action: the programmed return
    st = AppState(); st.morning_done = True
    deps = FakeDeps(min_v=V400, remaining_ah=AH400, switch="on", inventory_timers=[],
                    buckets=NEED300, sun_ok=True)
    tick(cfg, st, deps, D(20, 10))
    assert [(a[0], a[3]) for a in deps.acts] == [("disable_on_timers", False),
                                                 ("switch_off", True)]
    assert st.evening_verdict == "rescue_timer" and deps.switch == "off"
    assert deps.plan_seen[-1][1][0][0] == "failsafe_timer"   # single-action plan: the timer


def test_eve_off_known_need_schedules_even_when_sun_gate_holds():
    # v5 (§13): OFF + known need + sun_ok=False → the battery-first schedule
    # (the programmed return at the crossing), NOT the conservative bring_on
    st = AppState()
    deps = FakeDeps(min_v=V400, remaining_ah=AH400, switch="off", inventory_timers=[],
                    buckets=NEED300, sun_ok=False, sun_reason="radiation 5.0 MJ < 8 MJ")
    tick(cfg, st, deps, D(20, 10))
    assert deps.acts == []                                 # no switch act tonight
    assert deps.plan_seen[-1][1][0][0] == "failsafe_timer"  # single-action plan: the timer
    assert st.rescue_timer_set and st.last_planned_fire == FIRE_EVE


def test_eve_bring_on_now_clears_timers_first_then_switches():
    # final-review F1 (d): eve order pinned — disable_on_timers BEFORE switch_on
    st, deps = eve_bring_on(inventory_timers=None)   # with an armed ON-timer
    tick(cfg, st, deps, D(20, 10))
    assert [a[0] for a in deps.acts] == ["disable_on_timers", "switch_on"]
    assert deps.inventory_timers[0]["enabled"] == 0          # both classes disarmed


def test_eve_bring_on_now_runs_even_when_timer_clear_fails():
    # final-review F1: best-effort at the eve ON too — clear False → the
    # rescue-ON still switches (no new abort condition), no extra push
    class NoClear(FakeDeps):
        def act(self, action, **kw):
            if action == "disable_on_timers":
                self.acts.append((action, None, None, False, None)); return False
            return super().act(action, **kw)
    st, deps = eve_bring_on(NoClear)
    tick(cfg, st, deps, D(20, 10))
    assert [a[0] for a in deps.acts] == ["disable_on_timers", "switch_on"]
    assert deps.switch == "on"                              # the ON happened anyway
    assert deps.pushes and deps.pushes[0][0] == "GRID ON"   # no clear-push noise


def test_eve_failsafe_timer_applied_and_rescue_push():
    # v5: switch OFF + known need → the battery-first schedule INSIDE the night
    st = AppState(); st.morning_done = True
    deps = FakeDeps(min_v=V400, remaining_ah=AH400, switch="off", inventory_timers=[],
                    buckets=NEED300)                          # need 300, head > 0
    tick(cfg, st, deps, D(20, 10))
    assert deps.plan_seen and deps.plan_seen[0][0] == "failsafe_timer"
    on = deps.plan_seen[0][1][0][1]
    assert on["at_local"] == FIRE_EVE and on["fire_str"] == FIRE_EVE.strftime("%H:%M")
    assert st.rescue_timer_set and st.evening_verdict == "rescue_timer"
    assert st.last_planned_fire == FIRE_EVE                  # FINAL text reads THIS
    assert any(FIRE_EVE.strftime("%H:%M") in t for _, t in deps.pushes)   # rescue_set


def test_eve_failsafe_uses_learned_night_edges():
    # profile edges 8.0/19.0 → night_len 13 h → slower rate → later fire
    st = AppState(); st.morning_done = True
    deps = FakeDeps(min_v=V400, remaining_ah=AH400, switch="off", inventory_timers=[],
                    buckets=NEED300, profile=SUFF)
    tick(cfg, st, deps, D(20, 10))
    assert deps.plan_seen[0][1][0][1]["at_local"] == FIRE_SUFF
    # degenerate edges (onset == offset) fall back to 11 h instead of a 0 h night
    zero = NightProfile(8.0, 8.0, 448, False)
    deps2 = FakeDeps(min_v=V400, remaining_ah=AH400, switch="off", inventory_timers=[],
                     buckets=NEED300, profile=zero)
    tick(cfg, st, deps2, D(20, 10))
    assert deps2.plan_seen[0][1][0][1]["at_local"] == FIRE_EVE


def test_eve_failsafe_beyond_horizon_skips_timer():
    # v5 identity: available ≥ safety×need + floor → the battery covers the whole
    # (buffered) night → „none": nothing armed, safe_clear
    st = AppState(); st.morning_done = True
    deps = FakeDeps(remaining_ah=43.0, switch="off", inventory_timers=[], buckets=NEED800)
    tick(cfg, st, deps, D(20, 10))                           # avail 1100.8 ≥ 1088
    assert deps.acts == [] and not deps.plan_seen            # nothing dispatched, nothing applied
    assert st.last_planned_fire is None and not st.rescue_timer_set
    assert st.evening_verdict == "safe_clear"
    assert any("battery carries" in t for _, t in deps.pushes)


def test_eve_failsafe_applied_then_released_final_honest():
    # timer armed at 20:10; grid ON again at FINAL → v5 spends AGAIN and re-arms
    # the return; FINAL honestly states the ACTUAL programmed fire
    st = AppState(); st.morning_done = True
    deps = FakeDeps(min_v=V400, remaining_ah=AH400, switch="off", inventory_timers=[], buckets=NEED300)
    tick(cfg, st, deps, D(20, 10))
    assert st.last_planned_fire == FIRE_EVE
    deps.switch = "on"                                       # grid came back before FINAL
    tick(cfg, st, deps, D(21, 56))                           # spend + re-armed return
    assert st.evening_verdict == "rescue_timer"
    re_fire = D(21, 56) + timedelta(
        hours=(AV400 - FLOOR_WH) / (300.0 * 1.2 / 11.0))
    assert st.last_planned_fire == re_fire
    # §27 RETIME: the final no longer fires at the FIRST final tick (21:56 — the
    # incident's own timing this test used to pin) but at the eve's CLOSING
    # (first tick ≥ window_end); content moves to the §27 wording (armed leads).
    tick(cfg, st, deps, D(22, 1))
    finals = [t for p, t in deps.pushes if p == "NIGHT PLAN" and "final" in t]
    assert finals and f"switch time {re_fire.strftime('%H:%M')} (write-confirmed)" in finals[-1]
    assert "no timer" not in finals[-1]


def test_eve_bring_on_now_switches_with_verify():
    # v4 case 4: switch OFF + not ok → bring_on_now now, read-back verified
    # (voltage successor: floor in the evening + ack of today's emergency — the
    # conservative path owned the ON, need unknown → conservative)
    st, deps = eve_bring_on()
    tick(cfg, st, deps, D(20, 10))
    assert deps.acts == [("disable_on_timers", None, None, False, None),
                         ("switch_on", None, None, True,
                          "reason: night need not yet determinable")]
    assert st.evening_verdict == "grid_hold" and deps.switch == "on"
    assert deps.pushes and deps.pushes[0][0] == "GRID ON"
    assert deps.pushes[0][1] == "reason: night need not yet determinable"


def test_eve_bring_on_now_failure_signals_no_channel():
    st, deps = eve_bring_on(fail_acts=True)
    tick(cfg, st, deps, D(20, 10))
    assert deps.pushes and deps.pushes[0][0] == "NO CHANNEL" \
        and "bring_on_now" in deps.pushes[0][1]


def test_eve_night_buckets_failure_falls_conservative():
    # bucket fetch explodes (OpenSearch down) → audit + need=None → conservative
    class BoomBucketsDeps(FakeDeps):
        def night_buckets(self):
            raise RuntimeError("opensearch down")
    st, deps = eve_bring_on(BoomBucketsDeps)
    tick(cfg, st, deps, D(20, 10))
    assert any(e == "night_buckets_unavailable" for e, _ in deps.audits)
    assert deps.acts == [("disable_on_timers", None, None, False, None),
                         ("switch_on", None, None, True,
                          "reason: night need not yet determinable")]   # conservative bring-on
    assert st.evening_verdict == "grid_hold" and deps.pushes[0][0] == "GRID ON"


# --- v4 fix-round: anti-oscillation guard on case 4 (no fight with R2' grid-off) -----
SUPPRESS = "grid-off rule active/contested — grid stays off, restrained as before"


def test_eve_bring_on_suppressed_when_drain_rule_would_revert():
    # §13 v7: the battery sits ABOVE the floor → the drain rule would switch OFF
    # again right after any bring_on → flip-flop. Guard: NO switch_on, audit, ZERO push.
    st = AppState()                                          # fresh day: morning_done False
    deps = FakeDeps(min_v=3.30, remaining_ah=AH400, soc_pct=65.0, switch="off",
                    inventory_timers=[], buckets=[])         # need unknown → case 4
    tick(cfg, st, deps, D(20, 10))
    assert deps.acts == []                                   # suppressed: no switch_on
    sup = [f for e, f in deps.audits if e == "eve_bring_on_suppressed"]
    assert sup and sup[0]["inputs"] == {"reason": SUPPRESS, "soc": 65.0,
                                        "remaining_ah": AH400, "floor_v": cfg.true_floor_v,
                                        "morning_done": False}
    assert deps.pushes == []                                 # no push → no alternation
    assert st.evening_verdict is None                        # no verdict transition at all


def test_eve_bring_on_now_still_fires_when_soc_below_aus_gate():
    # guard-off case: SOC 40 < 50 → the grid-off gate is NOT active → legit path
    # intact (the SOC gate itself is v7 retired — carried along here as proof
    # that a low SOC never blocks the legitimate floor path)
    st, deps = eve_bring_on(soc_pct=40.0)
    tick(cfg, st, deps, D(20, 10))
    assert deps.acts == [("disable_on_timers", None, None, False, None),
                         ("switch_on", None, None, True,
                          "reason: night need not yet determinable")]
    assert not [f for e, f in deps.audits if e == "eve_bring_on_suppressed"]
    assert deps.pushes and deps.pushes[0][0] == "GRID ON"


def test_eve_bring_on_suppressed_after_aus_release_same_day():
    # morning_done=True: the grid-off already released today (any successful grid-off sets
    # it) → the eve must not undo that same day
    st = AppState(); st.morning_done = True
    deps = FakeDeps(remaining_ah=AH400, soc_pct=40.0, switch="off",
                    inventory_timers=[], buckets=[])
    tick(cfg, st, deps, D(20, 10))
    assert deps.acts == [] and deps.pushes == []
    sup = [f for e, f in deps.audits if e == "eve_bring_on_suppressed"]
    assert sup and sup[0]["inputs"]["morning_done"] is True


def test_eve_suppressed_bring_on_still_sends_final_summary():
    # suppression skips the verdict transition but NOT the FINAL block: the
    # once-per-day summary push still fires exactly once — §27 RETIME: at the
    # eve's CLOSING (old timing pinned it at the first final tick 21:56, the
    # incident), and explicitly NOT during the eve itself anymore.
    st = AppState(); st.morning_done = True
    deps = FakeDeps(remaining_ah=AH400, soc_pct=65.0, switch="off",
                    inventory_timers=[], buckets=[])
    tick(cfg, st, deps, D(21, 56))
    assert deps.acts == []
    assert [t for p, t in deps.pushes if p == "NIGHT PLAN" and "final" in t] == []
    tick(cfg, st, deps, D(22, 1))
    finals = [t for p, t in deps.pushes if p == "NIGHT PLAN" and "final" in t]
    assert finals and "no timer" in finals[-1]
    n = len(deps.pushes)
    tick(cfg, st, deps, D(22, 2))
    assert len(deps.pushes) == n                             # still exactly once


# --- fix-round: same-tick grid-off must be what _eve sees (review FIX1/FIX2) ------
def test_same_tick_aus_off_eve_sees_fresh_state_not_stale_on():
    # ONE eve tick: the grid-off drain-rule releases (battery above floor) BEFORE _eve runs. _eve must
    # evaluate against the VERIFIED post-grid-off state ("off"), not the as-read "on":
    # (a) exactly one grid-off sequence, (b) no eve act after it, (c) exactly ONE push
    # that tick = the MAIN OFF grid_off, (d) case-4 suppression audit present.
    st = AppState()                                          # fresh day
    deps = FakeDeps(min_v=V400, remaining_ah=AH400, soc_pct=65.0, switch="on",
                    buckets=[])                              # need unknown → would be case 4/2
    tick(cfg, st, deps, D(20, 10))
    # (a) T1 grid-off sequence ran exactly once — no second disable/switch_off pair
    assert [(a[0], a[3]) for a in deps.acts] == [("disable_on_timers", False),
                                                 ("switch_off", True)]
    # (b) no eve bring_on_now / release_night act after the grid-off
    assert not any(a[0] == "switch_on" for a in deps.acts)
    # (c) exactly one push, and it is the grid-off grid_off — no GRID-ON/keep contradiction
    assert len(deps.pushes) == 1 and deps.pushes[0][0] == "MAIN OFF"
    # (d) suppression audit (soc gate AND morning_done both true after the grid-off)
    sup = [f for e, f in deps.audits if e == "eve_bring_on_suppressed"]
    assert sup and sup[0]["inputs"] == {"reason": SUPPRESS, "soc": 65.0,
                                        "remaining_ah": AH400, "floor_v": cfg.true_floor_v,
                                        "morning_done": True}
    assert st.evening_verdict is None                        # no eve verdict transition


def test_same_tick_aus_off_no_duplicate_release_when_ratio_ok():
    # ratio OK variant: with the stale switch_state the eve would re-run the whole
    # grid-off sequence (case 1 release_night) in the SAME tick — it must not happen;
    # after the fresh state the eve programs the failsafe timer instead (case 3).
    st = AppState()                                          # fresh day
    deps = FakeDeps(min_v=V400, remaining_ah=AH400, soc_pct=65.0, switch="on",
                    buckets=NEED300)                         # AV400 ≥ 1.2×300 → ratio ok
    tick(cfg, st, deps, D(20, 10))
    assert [(a[0], a[3]) for a in deps.acts] == [("disable_on_timers", False),
                                                 ("switch_off", True)]   # ONE sequence only
    assert deps.pushes[0][0] == "MAIN OFF"                   # the grid-off push leads
    assert all(p != "GRID ON" for p, _ in deps.pushes)       # no stale keep-hold text
    assert deps.plan_seen and deps.plan_seen[0][0] == "failsafe_timer"   # case 3, not case 1


def test_eve_night_need_audit_every_tick_full_fidelity():
    # v4: night_need lands EVERY eve tick (idempotency of pushes stays transition-guarded).
    # owner physics ruling 2026-10-26 (§19/§20): available = remaining_ah ×
    # V_avg (anchor + conversion average disclosed), available_bms_wh = the
    # SAME counter against the INSTANT pack voltage (avg-vs-instant line).
    st = AppState(); st.morning_done = True
    deps = FakeDeps(min_v=V400, remaining_ah=AH400, switch="off", inventory_timers=[], buckets=NEED300)
    tick(cfg, st, deps, D(20, 10))
    tick(cfg, st, deps, D(20, 20))
    audits = [f for e, f in deps.audits if e == "night_need"]
    assert len(audits) == 2
    assert audits[0] == {"inputs": {"need_wh": 300.0, "clean": 2,
                                    "available_wh": AV400,
                                    "available_bms_wh": AH400 * PACK_V,
                                    "remaining_ah": AH400,
                                    "v_avg": PACK_V,
                                    "min_cell_v": V400, "min_cell_corr": V400,
                                    "soc_pct_v": soc_pct_from_v(V400),
                                    # Phase A (reference-load model) disclosure:
                                    # the crossing basis — this fake has NO
                                    # reference → None + the legacy rate actually
                                    # used (need × safety ÷ night_len)
                                    "load_wh_h": None,
                                    "crossing_rate_wh_h": 300.0 * 1.2 / 11.0,
                                    # Phase B/C: the horizon source — no spans,
                                    # no prediction → the length clamp alone
                                    "onset_src": "none",
                                    "safety": 1.2}}
    n = len(deps.pushes)
    assert n == 1                                             # rescue_set pushed once


def test_eve_available_comes_from_the_counter_times_voltage_average():
    # Owner PHYSICS ruling 2026-10-26 (§19/§20): available = remaining_ah ×
    # V_avg — the BMS counter IS the trusted mid-range instrument (its internal
    # amp-integration); the corrected-voltage SOC path is REMOVED from the
    # eve's available (tonight's 46 mV compressor sag swung it for hours = the
    # oscillation incident, handoff §18). Voltage keeps ONLY the deep band +
    # the min_cell_corr/soc_pct_v disclosure lines. The counter drives the
    # crossing exactly.
    st = AppState(); st.morning_done = True
    deps = FakeDeps(min_v=3.26, charge=0.5, remaining_ah=12.0, switch="on",
                    inventory_timers=[], buckets=NEED300, v_avg=13.05)
    tick(cfg, st, deps, D(20, 10))
    night = [f for e, f in deps.audits if e == "night_need"][0]["inputs"]
    assert night["available_wh"] == 12.0 * 13.05             # remaining_ah × V_avg
    assert night["remaining_ah"] == 12.0 and night["v_avg"] == 13.05
    assert night["available_bms_wh"] == 12.0 * PACK_V        # same counter, INSTANT V
    assert night["min_cell_v"] == 3.26                       # deep-band disclosure kept
    assert night["min_cell_corr"] == 3.26 - 0.5 * cfg.r_cell_ohm
    # the crossing time comes from the counter instrument, not the voltage curve
    floor = soc_pct_from_v(cfg.true_floor_v) / 100.0 * FULL_AH * PACK_V
    expected = D(20, 10) + timedelta(
        hours=(12.0 * 13.05 - floor) / (300.0 * 1.2 / 11.0))
    assert deps.plan_seen[-1][1][0][0] == "failsafe_timer"
    assert deps.plan_seen[-1][1][0][1]["at_local"] == expected


def test_final_plan_states_actual_failsafe_timer():
    st = AppState(); st.morning_done = True
    deps = FakeDeps(min_v=V400, remaining_ah=AH400, switch="off", inventory_timers=[], buckets=NEED300)
    tick(cfg, st, deps, D(20, 10))                     # programs the failsafe fire
    assert st.last_planned_fire == FIRE_EVE
    second_fire = D(21, 56) + timedelta(               # FINAL re-projects idempotently
        hours=(AV400 - FLOOR_WH) / (300.0 * 1.2 / 11.0))
    tick(cfg, st, deps, D(21, 56))
    assert st.last_planned_fire == second_fire
    # §27 RETIME: the old assertion pinned the final at the FIRST final tick
    # (21:56) with the pre-§27 "timer <time>" text — it was the incident's own
    # timing. The push now fires at the eve's CLOSING with the write-confirmed
    # armed as the headline (same value here: the write world).
    tick(cfg, st, deps, D(22, 1))
    finals = [t for p, t in deps.pushes if p == "NIGHT PLAN" and "final" in t]
    assert finals and \
        f"switch time {second_fire.strftime('%H:%M')} (write-confirmed)" in finals[-1]


def test_final_plan_says_no_timer_without_programming():
    st = AppState(); st.morning_done = True
    deps = FakeDeps(remaining_ah=100.0, switch="on", inventory_timers=[])   # need unknown → keep
    tick(cfg, st, deps, D(20, 10))
    tick(cfg, st, deps, D(21, 56))
    # §27 RETIME: "no timer" honesty now arrives at the eve's CLOSING, not at
    # the first final tick (old timing = the incident's stale push).
    tick(cfg, st, deps, D(22, 1))
    finals = [t for p, t in deps.pushes if p == "NIGHT PLAN" and "final" in t]
    assert finals and "no timer" in finals[-1]


def test_final_push_once():
    st = AppState(); st.morning_done = True
    deps = FakeDeps(remaining_ah=5.0, switch="on", inventory_timers=[])
    tick(cfg, st, deps, D(21, 56))                      # eve ran (keep) — no push yet
    assert not any(p[0] == "NIGHT PLAN" for p in deps.pushes)
    tick(cfg, st, deps, D(22, 1))                       # §27 closing: the ONE final
    assert any(p[0] == "NIGHT PLAN" for p in deps.pushes)
    n = len(deps.pushes)
    tick(cfg, st, deps, D(22, 2))                       # repeated closing ticks
    assert len(deps.pushes) == n


def test_day2_aus_runs_again_after_rollover():
    st, deps = AppState(current_date=D(17, 0), morning_done=True), FakeDeps(
        remaining_ah=55.0, pack_current=22.0, switch="on", solar_window=[22.0])
    tick(cfg, st, deps, datetime(2026, 10, 18, 11, 0))
    assert [a[0] for a in deps.acts] == ["disable_on_timers", "switch_off"]
    # next day again after re-connect
    deps.switch = "on"
    tick(cfg, st, deps, datetime(2026, 10, 19, 11, 0))
    assert [a[0] for a in deps.acts][-1] == "switch_off" and st.current_date == datetime(2026, 10, 19).date()


# --- Task5: T1 timer-collision pieces (Task6 wires the grid-off order into tick) ----
def test_aus_path_disables_on_timers_first():
    # Task6 wires the grid-off dispatch; Task5 tests the pieces:
    st, deps = AppState(morning_done=True), FakeDeps(
        min_v=3.30, switch="on", remaining_ah=55.0, pack_current=22.0, soc_pct=80.0)
    ok = deps.act("disable_on_timers")
    assert ok and deps.audits[-1][0] == "act" and deps.audits[-1][1]["action"]["type"] == "disable_on_timers"


# --- Task5: RealDeps read-first inventory + T1 _collision_clear (isolated) -----
class _StubCloud(CloudClient):
    """In-memory CoolKit — the timers ARE the device truth (no network).
    Subclasses the real client so production get_inventory() runs on it."""
    def __init__(self, *a, **kw):
        self.timers, self.fail_reads, self.ignore_writes = [], False, False
        self.set_calls = 0
    def login(self): pass
    def get_device(self, deviceid):
        if self.fail_reads:
            raise ChannelError("cloud down")
        return {"params": {"switch": "off", "timers": [dict(t) for t in self.timers]}}
    def set_timers(self, deviceid, timers):
        self.set_calls += 1
        if not self.ignore_writes:
            self.timers = [dict(t) for t in timers]
        return {"error": 0}
    def set_switch(self, deviceid, value): return {"error": 0}


class _StubLan:
    def __init__(self, *a, **kw): pass


class _StubBms:
    def __init__(self, *a, **kw):
        self.days_calls, self.fail_days, self.days_samples = 0, False, []
        self.night_buckets_calls, self.fail_buckets = 0, False
        self.night_buckets_args = None
    def samples_days(self, days):                          # v3 learner feed (bms.py)
        self.days_calls += 1
        if self.fail_days:
            raise BmsError("opensearch unreachable: down")
        return list(self.days_samples)
    def solar_day_boundaries(self, dates, tz_name, flow_a, sun_fn):  # T5 wiring
        return {}                                    # no history → conservative {}
    def night_buckets(self, days, tz_name, solar_noise_a, window=None,
                      spans=None):                                # v4 need feed
        self.night_buckets_calls += 1
        self.night_buckets_args = (days, tz_name, solar_noise_a, window, spans)
        if self.fail_buckets:
            raise BmsError("opensearch unreachable: down")
        return []


class _StubAudit:
    def __init__(self, path, **kw): self.events = []   # **kw: RealDeps wires cfg (§10b)
    def record(self, event, **fields): self.events.append((event, fields))


@pytest.fixture()
def make_deps(monkeypatch):
    def _make(**env):
        monkeypatch.setattr("gridguard.main.CloudClient", _StubCloud)
        monkeypatch.setattr("gridguard.main.LanClient", _StubLan)
        monkeypatch.setattr("gridguard.main.BmsSource", _StubBms)
        monkeypatch.setattr("gridguard.audit.AuditLog", _StubAudit)
        pushed = []
        monkeypatch.setattr("gridguard.main.ntfy_push",
                            lambda webhook, title, text: pushed.append((title, text)))
        deps = RealDeps(load_config({**GOOD, **env}))
        deps.pushes = pushed                      # captured ntfy pushes (never a real call)
        return deps
    return _make


def test_realdeps_read_state_returns_cloud_switch_and_timers(make_deps):
    deps = make_deps()
    on_timer = {"type": "repeat", "do": {"switch": "on"}, "enabled": 1, "atLocal": "0 4 * * *"}
    deps.cloud.timers = [on_timer]
    assert deps.read_state() == ("off", [on_timer])           # cloud device truth only


def test_realdeps_collision_clear_disables_and_verifies_both_classes(make_deps):
    deps = make_deps()
    deps.cloud.timers = [
        {"type": "repeat", "do": {"switch": "on"}, "enabled": 1, "atLocal": "0 4 * * *"},
        {"type": "repeat", "do": {"switch": "off"}, "enabled": 1, "atLocal": "0 6 * * 1"}]
    assert deps._collision_clear() is True
    assert [t["enabled"] for t in deps.cloud.timers] == [0, 0]  # §12.2: BOTH classes disarmed
    assert deps.audit_log.events[-1][0] == "act"
    fields = deps.audit_log.events[-1][1]
    assert fields["action"] == {"type": "disable_on_timers"}
    assert fields["timer_action"] == "disable" and fields["channel"] == "cloud"
    assert fields["result"] == "ok"


def test_realdeps_collision_clear_noop_when_nothing_to_flip(make_deps):
    deps = make_deps()
    deps.cloud.timers = [{"type": "repeat", "do": {"switch": "off"}, "enabled": 0}]
    assert deps._collision_clear() is True
    assert deps.cloud.set_calls == 0                          # no write for a no-op
    ev = deps.audit_log.events[-1]
    assert ev[0] == "act" and ev[1]["timer_action"] == "noop"


def test_realdeps_collision_clear_dryrun_suppresses_write(make_deps):
    deps = make_deps(DRY_RUN="1")
    deps.cloud.timers = [{"type": "repeat", "do": {"switch": "on"}, "enabled": 1}]
    assert deps._collision_clear() is True
    assert deps.cloud.set_calls == 0 and deps.cloud.timers[0]["enabled"] == 1   # nothing written
    ev = deps.audit_log.events[-1]
    assert ev[0] == "dry_run" and ev[1]["result"] == "suppressed"
    assert ev[1]["action"] == {"type": "disable_on_timers"}


def test_realdeps_collision_clear_channel_error_pushes_no_channel(make_deps):
    deps = make_deps()
    deps.cloud.fail_reads = True                              # first read already raises
    assert deps._collision_clear() is False
    assert [p[0] for p in deps.pushes] == ["NO CHANNEL"]
    assert "disable_on_timers" in deps.pushes[0][1]           # the human push names the action
    ev = deps.audit_log.events[-1]
    assert ev[0] == "act_failed" and ev[1]["action"] == {"type": "disable_on_timers"}
    assert ev[1]["result"] == "cloud down"


def test_realdeps_collision_clear_verify_fails_on_lost_write(make_deps):
    deps = make_deps()
    deps.cloud.timers = [{"type": "repeat", "do": {"switch": "on"}, "enabled": 1}]
    deps.cloud.ignore_writes = True                           # write accepted, device unchanged
    assert deps._collision_clear() is False
    assert deps.cloud.timers[0]["enabled"] == 1              # device truth: still enabled
    ev = deps.audit_log.events[-1]
    assert ev[0] == "act" and ev[1]["result"] == "verify_failed"


def test_realdeps_collision_clear_verify_fails_on_armed_off_timer_lost_write(make_deps):
    # §12.2 critical case: an armed OFF timer must NOT survive a lost write as "ok"
    deps = make_deps()
    deps.cloud.timers = [{"type": "repeat", "do": {"switch": "off"}, "enabled": 1}]
    deps.cloud.ignore_writes = True                           # write accepted, device unchanged
    assert deps._collision_clear() is False                   # verify counts the armed OFF timer
    assert deps.cloud.timers[0]["enabled"] == 1              # device truth: still armed
    ev = deps.audit_log.events[-1]
    assert ev[0] == "act" and ev[1]["result"] == "verify_failed"


def test_realdeps_collision_clear_verify_ok_when_off_timer_disarmed(make_deps):
    # same OFF-only scenario, write sticks → read-back disarmed → cleared ✓
    deps = make_deps()
    deps.cloud.timers = [{"type": "repeat", "do": {"switch": "off"}, "enabled": 1}]
    assert deps._collision_clear() is True
    assert deps.cloud.timers[0]["enabled"] == 0
    ev = deps.audit_log.events[-1]
    assert ev[0] == "act" and ev[1]["result"] == "ok"


def test_realdeps_collision_clear_no_timer_is_cleared(make_deps):
    # robust: no timer at all = cleared ✓ (nothing armed, nothing written)
    deps = make_deps()
    deps.cloud.timers = []
    assert deps._collision_clear() is True
    assert deps.cloud.set_calls == 0
    ev = deps.audit_log.events[-1]
    assert ev[0] == "act" and ev[1]["timer_action"] == "noop"


def test_audit_whitelist_allows_inventory_and_timer_action(tmp_path):
    from gridguard.audit import AuditLog                       # real whitelist, not the stub
    log = AuditLog(tmp_path / "gridguard.jsonl")
    log.record("act", action={"type": "disable_on_timers"}, timer_action="disable",
               channel="cloud", result="ok")
    log.record("inventory", inventory=[{"type": "repeat", "do": {"switch": "on"}, "enabled": 0}])
    log.record("push", kind="GRID ON", inputs={"text": "reason"})   # v4 §9.7 push audit
    lines = (tmp_path / "gridguard.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3 and "timer_action" in lines[0] and "inventory" in lines[1]
    assert '"kind": "GRID ON"' in lines[2]


# --- Task6: RealDeps.bms_solar_window (contract Interfaces) --------------------
def test_realdeps_bms_solar_window_maps_pack_current(make_deps):
    deps = make_deps()
    deps.bms.samples_window = lambda minutes: [
        SimpleNamespace(pack_current=12.0), SimpleNamespace(pack_current=None)]
    assert deps.bms_solar_window() == [12.0, None]


# --- Task7: planner wired into _eve — apply every tick, push on change only -----
def test_eve_plan_wired_and_idempotent():
    st = AppState(); st.morning_done = True
    deps = FakeDeps(min_v=V400, remaining_ah=AH400, switch="off", inventory_timers=[], buckets=NEED300)
    tick(cfg, st, deps, D(20, 15))
    assert deps.plan_seen and deps.plan_seen[0][0] == "failsafe_timer"
    n = len(deps.pushes)
    tick(cfg, st, deps, D(20, 25))
    assert len(deps.plan_seen) == 2 and len(deps.pushes) == n   # apply each tick, push once


# --- Task7-fix I1: pushes tell the APPLICATION truth (rescue only if applied) ----
class FailPlanDeps(FakeDeps):
    """FakeDeps whose apply_plan always FAILS (device write lost / no channel)."""
    def apply_plan(self, plan):
        self.plan_seen.append((plan.actions[0][0] if plan.actions else None,
                               list(plan.actions)))
        return False


def test_eve_no_rescue_push_when_failsafe_apply_fails():
    st = AppState(); st.morning_done = True
    deps = FailPlanDeps(min_v=V400, remaining_ah=AH400, switch="off", inventory_timers=[], buckets=NEED300)
    tick(cfg, st, deps, D(20, 15))
    assert deps.plan_seen and deps.plan_seen[0][0] == "failsafe_timer"   # verdict still rescue
    assert st.evening_verdict == "rescue_timer" and not st.rescue_timer_set
    assert st.last_planned_fire is None                        # never claim an unapplied timer
    assert not any(p[1].startswith("Night plan: rescue timer") for p in deps.pushes)  # NO rescue_set
    assert deps.pushes and deps.pushes[0][0] == "NO CHANNEL" \
        and "failsafe_timer" in deps.pushes[0][1]                     # honest failure signal


# --- v3 R3'': RealDeps.night_profile — daily cache + journaled learned params ---
def test_realdeps_night_profile_learns_once_per_day(make_deps):
    deps = make_deps()
    p1, p2 = deps.night_profile(), deps.night_profile()
    assert deps.bms.days_calls == 1                          # cached per local day
    assert p1 is p2 and p1.sufficient is False               # empty history → number-safe
    assert "night_learned" in [e for e, _ in deps.audit_log.events]   # jsonl visibility


def test_realdeps_night_profile_failure_is_not_cached(make_deps):
    deps = make_deps()
    deps.bms.fail_days = True
    with pytest.raises(BmsError):
        deps.night_profile()                                 # _eve nets it → rule1
    deps.bms.fail_days = False
    deps.night_profile()
    assert deps.bms.days_calls == 2                          # failed fetch retries


# --- v4: RealDeps.night_buckets — cached per day over the solar-night windows --
def test_realdeps_night_buckets_cached_per_day(make_deps):
    deps = make_deps()
    b1, b2 = deps.night_buckets(), deps.night_buckets()
    assert deps.bms.night_buckets_calls == 1                 # cached per local day
    assert b1 == b2 == []                                    # empty history → need None
    assert deps.bms.night_buckets_args == (7, "Europe/Madrid", 2.0, None, None)  # window=None + no spans → bms 19→8 clock fallback


def test_realdeps_night_buckets_failure_is_not_cached(make_deps):
    deps = make_deps()
    deps.bms.fail_buckets = True
    with pytest.raises(BmsError):
        deps.night_buckets()                                 # _eve nets it → conservative
    deps.bms.fail_buckets = False
    deps.night_buckets()
    assert deps.bms.night_buckets_calls == 2                 # failed fetch retries


# --- v4 §9.7: pushes are audited into the jsonl (fake audit sink, no network) ---
def test_realdeps_push_audits_event(make_deps):
    deps = make_deps()
    deps.push("GRID ON", "reason: night need not yet determinable")
    ev = [f for e, f in deps.audit_log.events if e == "push"]
    assert ev == [{"kind": "GRID ON",
                   "inputs": {"text": "reason: night need not yet determinable"}}]
    deps.push("T", "x" * 100)                                 # text capped at 60 chars
    push_fields = [f for e, f in deps.audit_log.events if e == "push"]
    assert push_fields[-1]["inputs"]["text"] == "x" * 60
    assert "https://n/t" not in str(push_fields)              # never the URI


def test_realdeps_push_audit_lands_even_when_ntfy_explodes(make_deps, monkeypatch):
    deps = make_deps()
    def boom(*a):                                             # adversarial sink
        raise OSError("ntfy down")
    monkeypatch.setattr("gridguard.main.ntfy_push", boom)
    with pytest.raises(OSError):
        deps.push("GRID ON", "reason text")
    assert [f for e, f in deps.audit_log.events if e == "push"][0]["kind"] == "GRID ON"


# --- T3 §9.1 RealDeps.forecast_ok — ONE fetch, ALWAYS shipped, conservative ------
SUN_ENV = {"WEATHER_LAT": "36.7", "WEATHER_LON": "-4.4", "WEATHER_MIN_J": "8000000"}


def _ship_spy(monkeypatch):
    """Spy on ship_forecast (T2 §12.1) — records the shipped radiation values."""
    calls: list = []
    monkeypatch.setattr("gridguard.main.ship_forecast",
                        lambda c, r, **kw: calls.append(r))
    return calls


def test_realdeps_forecast_ok_without_client_is_conservative_and_ships(make_deps,
                                                                       monkeypatch):
    deps = make_deps()                                        # no WEATHER_LAT → no client
    assert deps.weather is None
    shipped = _ship_spy(monkeypatch)
    assert deps.forecast_ok() == (False, "no weather data")
    assert shipped == [None]                                  # ALWAYS ship, even None
    assert deps.last_radiation_j is None                      # audit mirror


def test_realdeps_forecast_ok_fetch_none_ships_null_and_blocks(make_deps, monkeypatch):
    deps = make_deps(**SUN_ENV)
    deps.weather = SimpleNamespace(morning_radiation_j=lambda: None)   # fetch error
    shipped = _ship_spy(monkeypatch)
    assert deps.forecast_ok() == (False, "no weather data")
    assert shipped == [None]                                  # error days into the history


def test_realdeps_forecast_ok_min_j_unset_is_conservative(make_deps, monkeypatch):
    deps = make_deps(WEATHER_LAT="36.7", WEATHER_LON="-4.4")   # host50: MIN_J empty = None
    assert deps.cfg.weather_min_j is None
    deps.weather = SimpleNamespace(morning_radiation_j=lambda: 12e6)
    shipped = _ship_spy(monkeypatch)
    assert deps.forecast_ok() == (False, "MIN_J not set")
    assert shipped == [12e6]                                  # data present → ship anyway


def test_realdeps_forecast_ok_threshold_matrix_and_reasons(make_deps, monkeypatch):
    deps = make_deps(**SUN_ENV)                               # MIN_J = 8 MJ
    shipped = _ship_spy(monkeypatch)
    deps.weather = SimpleNamespace(morning_radiation_j=lambda: 5e6)
    assert deps.forecast_ok() == (False, "radiation 5.0 MJ < 8 MJ")
    deps.weather = SimpleNamespace(morning_radiation_j=lambda: 12e6)
    assert deps.forecast_ok() == (True, "radiation 12.0 MJ ≥ 8 MJ")
    assert shipped == [5e6, 12e6]                             # EVERY call ships
    assert deps.last_radiation_j == 12e6                      # mirror = last fetch


def test_realdeps_forecast_ok_ship_failure_never_propagates(make_deps, monkeypatch):
    # §12.1: ship errors must never propagate through the gate (ship_forecast
    # swallows internally; the invariant is enforced and pinned locally here)
    deps = make_deps(**SUN_ENV)
    deps.weather = SimpleNamespace(morning_radiation_j=lambda: 12e6)

    def boom(cfg, r, **kw):
        raise OSError("cluster down")

    monkeypatch.setattr("gridguard.main.ship_forecast", boom)
    assert deps.forecast_ok() == (True, "radiation 12.0 MJ ≥ 8 MJ")   # Gate-Truth untouched


# --- §13 R4: forecast-informed morning release — the rule in force is AUDITED ----
# §13 v7 SUPERSEDED (the R4 gate era) — the release audit now carries ground="drain"
# + floor_ah (see test_morning_aus_audit_carries_drain_ground); soc_gate no longer exists.



# §13 v7 SUPERSEDED — no weather-suppression branch exists; the day consults no
# forecast at all (forecast_calls == 0 pinned in test_v7_drain_is_unconditional...).

def test_r4_pass_low_soc_releases_day_room():
    # PASS + SOC55, no solar evidence yet → grid-off: tomorrow gets the day-room
    st = AppState()
    st.night_over = True             # the night is over (probe ran)
    deps = FakeDeps(remaining_ah=55.0, soc_pct=55.0, switch="on",
                    solar_window=[], sun_ok=True)
    tick(cfg, st, deps, D(6, 30))
    assert [a[0] for a in deps.acts] == ["disable_on_timers", "switch_off"]
    assert st.morning_done and deps.switch == "off"


# v6 supersession (owner2026-10-21, purpose-argument): "FAIL beats SOC95" and
# "FAIL beats proven sun" are OVERTURNED — a near-full battery closing the sink is
# the only irreversible loss. The truth now lives in
# test_v6_sink_imperative_releases_at_high_soc_even_on_fail and
# test_v6_proven_sun_beats_failed_gate_even_below_50.


# §13 v7 SUPERSEDED — "no candidate" is gone: any battery above the safety floor
# drains into the load (test_v7_drain_is_unconditional_above_the_floor).

def test_v7_drain_is_unconditional_above_the_floor():
    # no SOC, no solar evidence, sun_ok=False — NONE of it matters: switch ON +
    # battery above the safety floor → RELEASE, and the morning consults NO forecast
    st = AppState(); st.night_over = True
    deps = FakeDeps(remaining_ah=60.0, soc_pct=None, switch="on",
                    solar_window=[], sun_ok=False, sun_reason="irrelevant")
    tick(cfg, st, deps, D(6, 30))
    assert [a[0] for a in deps.acts] == ["disable_on_timers", "switch_off"]
    assert deps.forecast_calls == 0                            # no prediction at day
    off = [f for e, f in deps.audits if e == "grid_off"][0]
    assert off["inputs"].get("ground") == "drain"


def test_day_drain_continues_past_counter_floor_and_stops_at_voltage_floor():
    # L2 wave1 (owner ruling 2026-10-25, the 06:40 incident): the BMS counter
    # must NEVER stop the day again — with healthy corrected cell voltage
    # the drain keeps running even at a 2,0 Ah counter reading; and ONLY the
    # voltage floor (corrected ≤ TRUE_FLOOR_V) stops it.
    st = AppState(); st.night_over = True
    deps = FakeDeps(min_v=3.26, remaining_ah=2.0, switch="on",
                    solar_window=[], sun_ok=False)
    tick(cfg, st, deps, D(6, 30))
    assert [a[0] for a in deps.acts] == ["disable_on_timers", "switch_off"]  # drain runs
    off = [f for e, f in deps.audits if e == "grid_off"][0]
    assert off["inputs"]["remaining_ah"] == 2.0           # counter: audit number only
    assert off["inputs"]["min_cell_corr"] == 3.26 and off["inputs"]["floor_v"] == cfg.true_floor_v
    # same case ON the floor (3,14 V corrected ≤ 3,15): the drain stops
    st2 = AppState(); st2.night_over = True
    deps2 = FakeDeps(min_v=3.14, remaining_ah=2.0, switch="on",
                     solar_window=[], sun_ok=False)
    tick(cfg, st2, deps2, D(6, 30))
    assert deps2.acts == [] and deps2.switch == "on"


def test_v7_safety_floor_outranks_everything():
    # L2 (owner2026-10-25): the floor is the corrected CELL VOLTAGE — even at
    # SOC95 and a counter reading under the old ah_on: 3,14 V corrected ≤
    # TRUE_FLOOR_V → nothing to drain → HOLD (a physics invariant, not a tuning
    # value). The counter (4,0 Ah) plays no role anymore.
    st = AppState(); st.night_over = True
    deps = FakeDeps(min_v=3.14, remaining_ah=4.0, soc_pct=95.0, switch="on",
                    solar_window=[], sun_ok=False, sun_reason="irrelevant")
    tick(cfg, st, deps, D(6, 30))
    assert deps.acts == [] and deps.switch == "on"
    assert deps.forecast_calls == 0


def test_v7_unknown_battery_holds_without_guessing():
    # unknown min-cell voltage → corrected None → conservative floor, never
    # guessed (doctrine of unknowns), no act, no fetch
    st = AppState(); st.night_over = True
    deps = FakeDeps(min_v=None, soc_pct=80.0, switch="on",
                    solar_window=[], sun_ok=True)
    tick(cfg, st, deps, D(6, 30))
    assert deps.acts == [] and deps.forecast_calls == 0


# --- v7 observability (lesson2026-10-21): a blocked reach-gate must announce itself
def test_reach_block_is_audited_once_not_silently():
    # reachable=False blocked a WHOLE morning with zero events — the awake block
    # must announce itself, transition-guarded (once per outage, not per tick)
    st = AppState()
    deps = FakeDeps(remaining_ah=88.0, switch="on", reachable_flag=False)
    tick(cfg, st, deps, D(10, 0))
    tick(cfg, st, deps, D(10, 10))
    rb = [f for e, f in deps.audits if e == "reach_blocked"]
    assert rb and rb[0]["inputs"] == {"switch": "on"}           # ONCE, not per tick
    assert not any(a[0] == "switch_off" for a in deps.acts)     # the gate held
    assert not [e for e, _ in deps.audits if e == "reach_restored"]


def test_reach_restored_announces_and_the_drain_runs():
    st = AppState()
    deps = FakeDeps(remaining_ah=88.0, switch="on", reachable_flag=False)
    tick(cfg, st, deps, D(10, 0))
    deps.reachable_flag = True                                  # the uplink returns
    tick(cfg, st, deps, D(10, 10))
    assert any(e == "reach_restored" for e, _ in deps.audits)
    assert [a[0] for a in deps.acts] == ["disable_on_timers", "switch_off"]  # drain ran


# --- manual-override recognition (owner2026-10-21) -----------------------------
# The actor is WiFi-only; during WiFi-on hours a state change NO act of ours
# caused = a hand on the switch. Recognized by comparing the just-read state
# with the last observation; our own acts sync the baseline; after an
# unreachable GAP (actor-night, outage) the first read re-baselines SILENTLY —
# our own programmed night-timer fires across such gaps, flagging it would be
# a false positive every single morning.

def test_manual_override_recognized_on_flip():
    st, deps = AppState(), FakeDeps(switch="off", soc_pct=80.0)
    tick(cfg, st, deps, D(10, 0))                    # baseline: off
    deps.switch = "on"                               # the hand on the switch
    tick(cfg, st, deps, D(10, 10))
    ov = [f for e, f in deps.audits if e == "manual_override"]
    assert ov and ov[0]["inputs"] == {"from": "off", "to": "on"}
    assert any(t == "MANUAL" for t, _x in deps.pushes)


def test_no_override_when_state_unchanged():
    st, deps = AppState(), FakeDeps(switch="off", soc_pct=80.0)
    tick(cfg, st, deps, D(10, 0))
    tick(cfg, st, deps, D(10, 10))
    assert not any(e == "manual_override" for e, _ in deps.audits)
    assert not any(t == "MANUAL" for t, _x in deps.pushes)


def test_first_tick_baselines_silently():
    # fresh process (or first read ever): establish the baseline, never flag
    st, deps = AppState(), FakeDeps(switch="on", soc_pct=80.0)
    tick(cfg, st, deps, D(10, 0))                    # drain fires (own act) — no override
    assert not any(e == "manual_override" for e, _ in deps.audits)


def test_own_drain_act_is_not_flagged():
    st, deps = AppState(), FakeDeps(switch="on", soc_pct=80.0)
    tick(cfg, st, deps, D(10, 0))                    # drain: OUR switch_off
    assert any(a == "switch_off" for a, *_r in deps.acts)
    tick(cfg, st, deps, D(10, 10))                   # reads off — our doing
    assert not any(e == "manual_override" for e, _ in deps.audits)


def test_eve_own_bring_on_is_not_flagged():
    st, deps = eve_bring_on()
    tick(cfg, st, deps, D(20, 10))                   # eve bring_on_now: OUR switch_on
    assert any(a == "switch_on" for a, *_r in deps.acts)
    tick(cfg, st, deps, D(20, 20))                   # reads on — our doing
    assert not any(e == "manual_override" for e, _ in deps.audits)


def test_override_gap_rebaselines_silently():
    # across an unreachable gap (actor-night) the state changed — that is our
    # own programmed night-timer firing on the device, NOT a hand: re-baseline
    st, deps = AppState(), FakeDeps(switch="off", soc_pct=80.0)
    tick(cfg, st, deps, D(10, 0))                    # baseline off at 10:00
    deps.switch = "on"                               # changed across the gap
    tick(cfg, st, deps, D(20, 10))                   # 10 h later → silent
    assert not any(e == "manual_override" for e, _ in deps.audits)
    assert not any(t == "MANUAL" for t, _x in deps.pushes)


# --- state_observed journal (owner find 2026-10-22) --------------------------
# Device timers fire unlabelled while unreachable — the act-label windows then
# lie (hero stayed OFF through 08:01→10:03 and "since" pointed at yesterday).
# The journal: baseline on first read (no "from"), transition on every observed
# change — but only in-session changes are accused as manual_override.

def test_state_observed_baseline_then_transition_then_quiet():
    st, deps = AppState(), FakeDeps(switch="off", soc_pct=80.0)
    tick(cfg, st, deps, D(10, 0))
    so = [f for e, f in deps.audits if e == "state_observed"]
    assert so == [({"inputs": {"to": "off"}} and {"inputs": {"to": "off"}})] and \
        so[0]["inputs"] == {"to": "off"}                    # baseline, no from
    deps.switch = "on"                                      # the hand / a device timer
    tick(cfg, st, deps, D(10, 10))
    so = [f for e, f in deps.audits if e == "state_observed"]
    assert so[-1]["inputs"] == {"from": "off", "to": "on"}   # transition journaled
    tick(cfg, st, deps, D(10, 20))                           # unchanged → nothing new
    assert len([1 for e, _ in deps.audits if e == "state_observed"]) == 2


def test_state_observed_journals_gap_change_without_manual_accusation():
    # device timer fired across an unreachable gap (the eve's pre-programming):
    # the change IS journaled — but NOT called a hand on the switch
    st, deps = AppState(), FakeDeps(switch="off", soc_pct=80.0)
    tick(cfg, st, deps, D(10, 0))                            # baseline off
    deps.switch = "on"                                       # device timer fired
    tick(cfg, st, deps, D(20, 10))                           # 10 h gap → journal only
    assert any(f["inputs"] == {"from": "off", "to": "on"}
               for e, f in deps.audits if e == "state_observed")
    assert not any(e == "manual_override" for e, _ in deps.audits)
    assert not any(t == "MANUAL" for t, _x in deps.pushes)


# --- data quality assurance (owner voice 2026-10-22, binding) -----------------
# "9:53 can nothing switch, because the WLAN is off" — a read inside the known
# WLAN-off window returns the device's LAST KNOWN state, never an actor
# response. Such reads must not become data: no baseline, no transition, no
# gap-clock advance — otherwise wrong conclusions (the false
# HAND-ON-THE-SWITCH at 10:03) are built on data that is not correct at its
# basis. State observations only from a REACHABLE uplink.

def test_unreachable_reads_write_no_state_data():
    st, deps = AppState(), FakeDeps(switch="off", soc_pct=80.0,
                                    reachable_flag=False)
    tick(cfg, st, deps, D(10, 0))                    # stale read, no actor possible
    tick(cfg, st, deps, D(10, 10))
    assert not any(e == "state_observed" for e, _ in deps.audits)
    assert not any(e == "manual_override" for e, _ in deps.audits)
    # first LIVE read is the baseline — and the device-timer change it reveals
    # is journaled, never accused (the gap runs from the last LIVE anchor)
    deps.reachable_flag, deps.switch = True, "on"
    tick(cfg, st, deps, D(20, 0))
    so = [f for e, f in deps.audits if e == "state_observed"]
    assert so == [{"inputs": {"to": "on"}}]
    assert not any(e == "manual_override" for e, _ in deps.audits)
    assert not any(t == "MANUAL" for t, _x in deps.pushes)


def test_unreachable_stale_reads_do_not_advance_the_gap_clock():
    # live baseline at 10:00, then a stale blackout read at 10:10 must NOT
    # become the anchor: the flip detected live at 10:50 is 50 min after the
    # last ACTOR-GROUNDED observation → over the gap → silent re-baseline.
    # Without the QA the stale 10:10 read would anchor the gap at 40 min and
    # accuse a hand — the exact wrong conclusion of 2026-10-22.
    st, deps = AppState(), FakeDeps(switch="off", soc_pct=80.0)
    tick(cfg, st, deps, D(10, 0))                    # live baseline off, clock t=10:00
    deps.reachable_flag = False                      # blackout: stale reads only
    tick(cfg, st, deps, D(10, 10))
    tick(cfg, st, deps, D(10, 20))
    deps.reachable_flag, deps.switch = True, "on"    # live again, state changed
    tick(cfg, st, deps, D(10, 50))                   # 50 min after the LIVE anchor
    assert not any(e == "manual_override" for e, _ in deps.audits)
    assert not any(t == "MANUAL" for t, _x in deps.pushes)


# --- deep-drain ruling (owner2026-10-22: safety → 1.0, maximum solar use) -----
# The ×1.2 used to ride in the crossing RATE: the return fired with ~29 % SOC
# still in the pack (this morning's find). With the raw learned need the verdict
# may FLIP rescue_timer → carries once the pack refilled — and the previously
# armed return must then be DISARMED, else the stale timer still fires at the
# old crossing and cuts the night short (exactly what the ruling removes).

def test_eve_carry_flip_disarms_the_armed_return():
    st = AppState()
    deps = FakeDeps(min_v=V400, remaining_ah=AH400, switch="off", inventory_timers=[],
                    buckets=NEED300)
    tick(cfg, st, deps, D(20, 10))
    assert st.rescue_timer_set and st.evening_verdict == "rescue_timer"
    deps.remaining_ah = 100.0                            # the pack refilled (§20: the COUNTER)
    tick(cfg, st, deps, D(20, 20))
    assert st.evening_verdict == "safe_clear"            # carries the whole night
    assert "disable_on_timers" in [a[0] for a in deps.acts]   # stale timer disarmed
    assert st.rescue_timer_set is False
    assert st.last_planned_fire is None


def test_eve_carry_flip_disarms_from_device_truth_after_restart():
    # a restart wipes the in-memory rescue_timer_set flag while the device
    # still holds the armed return (deploy between eve and morning): the
    # carries verdict must disarm from the FRESH inventory, not the flag
    st = AppState()
    armed = [{"type": "repeat", "do": {"switch": "on"}, "enabled": 1,
              "atLocal": "52 7 * * *"}]
    deps = FakeDeps(remaining_ah=1000.0 / PACK_V, switch="off",
                    inventory_timers=armed, buckets=NEED300)
    assert st.rescue_timer_set is False               # flag lost in the restart
    tick(cfg, st, deps, D(21, 10))
    assert st.evening_verdict == "safe_clear"
    assert "disable_on_timers" in [a[0] for a in deps.acts]


# --- crossing horizon = MEASURED solar night (owner: concept applied throughout) --
def test_eve_night_len_prefers_measured_solar_span():
    st = AppState()
    deps = FakeDeps(min_v=V400, remaining_ah=AH400, switch="off", inventory_timers=[],
                    buckets=NEED300)
    # measured solar night of 8 h (fresher than the 11 h default/edges)
    deps.spans = {D(22, 10): (datetime(2026, 10, 21, 20, 0),
                              datetime(2026, 10, 22, 4, 0))}
    tick(cfg, st, deps, D(20, 10))
    assert deps.plan_seen[-1][1][0][0] == "failsafe_timer"
    at = deps.plan_seen[-1][1][0][1]["at_local"]
    expected = D(20, 10) + timedelta(
        hours=(AV400 - FLOOR_WH) / (300.0 * 1.2 / 8.0))
    assert at == expected


# --- Owner2026-10-23: forecast_ok ships the storm outlook as a side branch ----
def test_realdeps_forecast_ok_ships_storm_outlook_alongside(make_deps, monkeypatch):
    deps = make_deps(**SUN_ENV)
    outlook: list = []
    monkeypatch.setattr("gridguard.main.ship_outlook",
                        lambda c, d, **kw: outlook.append(d))
    deps.weather = SimpleNamespace(
        morning_radiation_j=lambda: 12e6,
        daily_outlook=lambda: [{"date": "2026-10-24", "weather_code": 95}])
    assert deps.forecast_ok()[0] is True
    assert outlook == [[{"date": "2026-10-24", "weather_code": 95}]]
    # without the daily_outlook attribute (old stubs) the side branch never blocks
    deps.weather = SimpleNamespace(morning_radiation_j=lambda: 12e6)
    deps.forecast_ok()
    assert len(outlook) == 1


# --- Owner2026-10-23: forecast-revision outake (throttled, intake only) ------
def test_realdeps_forecast_refresh_throttles_and_ships_revisions(make_deps, monkeypatch):
    import gridguard.main as main_mod
    deps = make_deps(**SUN_ENV)
    ticks = {"t": 5000.0}
    monkeypatch.setattr(main_mod._time, "monotonic", lambda: ticks["t"])
    shipped = _ship_spy(monkeypatch)
    outlook: list = []
    monkeypatch.setattr("gridguard.main.ship_outlook",
                        lambda c, d, **kw: outlook.append(d))
    deps.weather = SimpleNamespace(morning_radiation_j=lambda: 12e6,
                                   daily_outlook=lambda: [{"date": "2026-10-24"}])
    deps.forecast_refresh()
    deps.forecast_refresh()                    # inside the window: no ship
    assert shipped == [12e6] and len(outlook) == 1
    ticks["t"] += deps.cfg.forecast_refresh_min * 60.0 + 1
    deps.weather = SimpleNamespace(morning_radiation_j=lambda: 9e6,   # midday revision
                                   daily_outlook=lambda: [{"date": "2026-10-24"}])
    deps.forecast_refresh()
    assert shipped == [12e6, 9e6]              # the revision lands in the cluster
    assert len(outlook) == 2


def test_realdeps_forecast_refresh_never_raises(make_deps, monkeypatch):
    import gridguard.main as main_mod
    deps = make_deps(**SUN_ENV)
    monkeypatch.setattr(main_mod._time, "monotonic", lambda: 1000.0)

    def _boom():
        raise OSError("api down")
    deps.weather = SimpleNamespace(morning_radiation_j=_boom)
    deps.forecast_refresh()                    # swallows everything — the loop keeps running


def test_eve_survives_yield_learning_crash_and_still_decides():
    """Defense2026-10-23 (tick_error series20:07+): yield_learning is a
    pure side branch (v7: never gates) — ITS crash must never kill the eve
    decision (need audit + plan)."""
    st = AppState()
    deps = FakeDeps(min_v=V400, remaining_ah=AH400, switch="off", inventory_timers=[],
                    profile=SUFF, buckets=NEED300)
    deps.yield_learning = lambda: (_ for _ in ()).throw(
        TypeError("can't compare offset-naive and offset-aware datetimes"))
    tick(cfg, st, deps, D(20, 10))
    assert any(e == "night_need" for e, _ in deps.audits)
    assert deps.plan_seen                            # the plan came despite the crash


# --- Owner2026-10-23 (TDD gap): decision-path resilience matrix ----------------
# NO single consult-side Deps call may kill the tick-/eve decision —
# regardless of exception class (BmsError, RuntimeError AND the
# data-shape classes like TypeError that escaped in the 20:07 series).
# Fallback = conservative + visible audit (never a dead loop).
@pytest.mark.parametrize("crash", ["bms_latest", "bms_samples_2h", "night_profile",
                                   "night_buckets", "solar_night_windows",
                                   "forecast_ok", "pack_v_avg"])
def test_eve_decides_for_any_consult_side_crash(crash):
    st = AppState()
    deps = FakeDeps(min_v=V400, remaining_ah=AH400, switch="off", inventory_timers=[],
                    profile=SUFF, buckets=NEED300)
    boom = lambda *a, **k: (_ for _ in ()).throw(
        TypeError("can't compare offset-naive and offset-aware datetimes"))
    setattr(deps, crash, boom)
    tick(cfg, st, deps, D(20, 10))                  # must NEVER bail out
    assert any(e == "night_need" for e, _ in deps.audits)
    # Whoever deletes the decision basis (need via night_buckets, the
    # forecast gate via forecast_ok, or available via bms_latest) rightly gets
    # the conservative plan "none" (no apply_plan) — the cycle itself
    # (night_need audit) runs through in EVERY case.
    if crash not in ("night_buckets", "forecast_ok", "bms_latest"):
        assert deps.plan_seen


def test_eve_repeated_apply_failure_pushes_no_channel_once_only():
    """Push discipline 21:40 reality: consecutive apply failures with an
    UNCHANGED verdict => exactly ONE NO CHANNEL push (the transition guard),
    the individual errors stay visible as act_failed."""
    st = AppState(); st.morning_done = True
    deps = FailPlanDeps(min_v=V400, remaining_ah=AH400, switch="off",
                        inventory_timers=[], buckets=NEED300)
    tick(cfg, st, deps, D(20, 15))
    assert [p[0] for p in deps.pushes] == ["NO CHANNEL"]
    tick(cfg, st, deps, D(20, 20))                       # second failure, same verdict
    assert [p[0] for p in deps.pushes] == ["NO CHANNEL"]  # no spam
    assert len(deps.plan_seen) == 2                      # every tick retries


# --- owner2026-10-25: dynamic floors + no interference ------------------------
def test_capacity_ack_never_suppresses_the_deep_emergency_same_day():
    """Thresholds don't disturb each other: a low_capacity_ack no longer
    blocks the emergency on the same day (Ack = only its own action)."""
    st, deps = AppState(), FakeDeps(min_v=3.14, switch="off")
    st.night_over = True
    tick(cfg, st, deps, D(5, 40))                    # low floor, path open → low ack
    deps.switch = "off"                              # the day drain switches off again
    deps.min_v = 2.89                                # genuinely deep afterwards
    tick(cfg, st, deps, D(10, 30))                   # path open: the emergency may fire
    assert [a[0] for a in deps.acts] == ["disable_on_timers", "switch_on",
                                         "disable_on_timers", "switch_on"]
    assert deps.pushes and deps.pushes[-1][0] == "EMERGENCY"

# --- owner2026-10-25 (DYNAMIC ruling): no fixed outage config — the live link
# is the only truth, and suppressed floors ACT THE INSTANT IT RETURNS ----------
def test_floor_unreachable_never_dials_but_acts_the_instant_the_link_returns():
    """No fixed outage parameter (config doctrine: Env = no usage-
    dependent values). Without a path: no dialing, ONE audit/day (the latch
    is NOT an Ack) — hence the floor fires the MOMENT the link returns
    (09:10 proof: restore → act in 1 s), not tomorrow or via a program."""
    st, deps = AppState(), FakeDeps(min_v=3.14, switch="off", reachable_flag=False)
    st.night_over = True
    tick(cfg, st, deps, D(5, 40))
    tick(cfg, st, deps, D(5, 45))                    # second blind tick: no repeat
    assert deps.acts == []                           # never choose a dead channel
    assert deps.pushes == []                         # low without a path = audit only
    ev = [f for e, f in deps.audits if e == "low_capacity_on"]
    assert len(ev) == 1 and ev[0]["result"] == "blocked_unreachable"
    assert ev[0]["inputs"]["path"] == "unreachable"
    assert st.low_capacity_ack is None               # NO Ack → the action waits
    deps.reachable_flag = True                       # link returns
    tick(cfg, st, deps, D(5, 46))
    assert [a[0] for a in deps.acts] == ["disable_on_timers", "switch_on"]
    assert st.low_capacity_ack == D(5, 46).date()    # only now is it acknowledged


def test_deep_emergency_pushes_once_while_blind_but_never_dials():
    st, deps = AppState(), FakeDeps(min_v=2.89, switch="off", reachable_flag=False)
    st.night_over = True
    tick(cfg, st, deps, D(4, 10))
    tick(cfg, st, deps, D(4, 20))
    assert deps.acts == []                           # the emergency path chooses none either
    assert len([p for p in deps.pushes if p[0] == "EMERGENCY"]) == 1  # 1 danger push/day
    ev = [f for e, f in deps.audits if e == "emergency_on"]
    assert len(ev) == 1 and ev[0]["result"] == "blocked_unreachable"
    assert st.emergency_ack is None                  # act waits until the link
    deps.reachable_flag = True
    tick(cfg, st, deps, D(4, 25))
    assert [a[0] for a in deps.acts] == ["disable_on_timers", "switch_on"]


# --- owner ruling 2026-10-25 (part 3): weather-conditional RESERVE TIERS -------
# Instrument carve-out (owner, binding): the tier gates compare BMS SOC
# (reading.soc — the coulomb counter) because the LiFePO4 mid-band has no
# voltage expression (~12 mV between 30 % and 50 %): display-only stays
# BINDING for the deep/emergency decisions; BMS SOC is the correct instrument
# for the mid-band tier gates — local precision beats global bias for a soft
# hold. The deep floors / emergency act / voltage_floor_hit stay untouched.


def test_eve_sets_reserve_tier_and_audits_the_why():
    st = AppState()
    deps = FakeDeps(min_v=V400, remaining_ah=AH400, switch="on",
                    inventory_timers=[], buckets=NEED300)
    deps.outlook_days = lambda: ([{"date": "2026-10-19", "weather_code": 95,
                                   "gust_max_kmh": 40.0, "precip_sum_mm": 0.0,
                                   "radiation_sum_j": 14e6}],
                                 "2026-10-18T20:10:00")
    tick(cfg, st, deps, D(20, 10))                   # the eve consult computes the tier
    assert st.reserve_tier_pct == 50
    ev = [f for e, f in deps.audits if e == "reserve_tier"]
    assert len(ev) == 1
    inp = ev[0]["inputs"]
    assert inp["tier_pct"] == 50
    assert inp["lookahead_days"] == cfg.tier_lookahead_days == 3
    assert inp["outlook_ts"] == "2026-10-18T20:10:00"
    assert any(t.startswith("weather_code") for t in inp["triggers"])
    # the eve consult runs every eve tick — an UNCHANGED decision audits once
    # (signature-guarded: one line per real change, never one per tick)
    tick(cfg, st, deps, D(20, 15))
    assert len([f for e, f in deps.audits if e == "reserve_tier"]) == 1


def test_midday_forecast_revision_overwrites_the_reserve_tier_latch(make_deps,
                                                                    monkeypatch):
    import gridguard.main as main_mod
    deps = make_deps(**{"WEATHER_INDEX": "0"})       # ships off → no HTTP in tests
    ticks = {"t": 5000.0}
    monkeypatch.setattr(main_mod._time, "monotonic", lambda: ticks["t"])
    deps.weather = SimpleNamespace(
        morning_radiation_j=lambda: 12e6,
        daily_outlook=lambda: [{"date": "2026-10-19", "weather_code": 95}])
    st = AppState()
    deps.forecast_refresh(st)                        # revision path carries the latch
    assert st.reserve_tier_pct == 50
    ev = [f for e, f in deps.audit_log.events if e == "reserve_tier"]
    assert len(ev) == 1 and ev[0]["inputs"]["tier_pct"] == 50
    assert ev[0]["inputs"]["lookahead_days"] == cfg.tier_lookahead_days
    # midday revision: the storm passed → the SAME computation overwrites the
    # latch and audits AGAIN (event name stable, the result may differ)
    ticks["t"] += deps.cfg.forecast_refresh_min * 60.0 + 1
    deps.weather = SimpleNamespace(
        morning_radiation_j=lambda: 12e6,
        daily_outlook=lambda: [{"date": "2026-10-19", "weather_code": 3,
                                "gust_max_kmh": 40.0, "precip_sum_mm": 0.0,
                                "radiation_sum_j": 14e6}])
    deps.forecast_refresh(st)
    assert st.reserve_tier_pct == 0
    ev = [f for e, f in deps.audit_log.events if e == "reserve_tier"]
    assert len(ev) == 2                              # audit again, same event name


def test_release_gate_blocks_below_the_tier_with_one_guarded_tier_hold_line():
    st = AppState()
    st.reserve_tier_pct = 30
    deps = FakeDeps(remaining_ah=60.0, soc_pct=20.0, switch="on", solar_window=[])
    tick(cfg, st, deps, D(11, 0))
    assert not any(a[0] == "switch_off" for a in deps.acts)     # leave the grid on
    th = [f for e, f in deps.audits if e == "tier_hold"]
    assert len(th) == 1 and th[0]["inputs"] == {"soc": 20.0, "tier_pct": 30,
                                                "release_at_pct": 35}
    tick(cfg, st, deps, D(11, 10))                  # same state: NO second line (no spam)
    assert len([f for e, f in deps.audits if e == "tier_hold"]) == 1
    deps.soc_pct = 30.0                             # exactly AT the tier → still held
    tick(cfg, st, deps, D(11, 20))                  # (release = tier + hyst: the zones
    assert not any(a[0] == "switch_off" for a in deps.acts)   #  never overlap → no flap)
    deps.soc_pct = 35.0                             # tier(30) + hyst(5) → release allowed
    tick(cfg, st, deps, D(11, 30))
    assert [a[0] for a in deps.acts] == ["disable_on_timers", "switch_off"]
    assert st.tier_hold_pct == 0                    # hold released (the act narrates)
    assert len([f for e, f in deps.audits if e == "tier_hold"]) == 1


def test_eve_release_night_holds_below_the_tier_with_a_single_tier_hold_line():
    """Instrument split in action: the eve's available (counter × V_avg, §20)
    clears the ratio and the reserve band → the plan PROPOSES the spend, while
    the BMS SOC (28 < tier+hyst) gates the release — the day gate + eve gate
    share ONE guarded tier_hold line in the same tick (no double audit)."""
    st = AppState(); st.morning_done = True; st.reserve_tier_pct = 30
    deps = FakeDeps(min_v=3.33, remaining_ah=120.0, soc_pct=28.0, switch="on",
                    inventory_timers=[], buckets=NEED300)
    tick(cfg, st, deps, D(20, 10))
    assert not any(a[0] == "switch_off" for a in deps.acts)  # no night spend below tier
    # the PLAN proposed the spend (head above the reserve floor) — the GATE held it
    assert deps.plan_seen and deps.plan_seen[-1][1][0][0] == "release_night"
    th = [f for e, f in deps.audits if e == "tier_hold"]
    assert len(th) == 1 and th[0]["inputs"] == {"soc": 28.0, "tier_pct": 30,
                                                "release_at_pct": 35}
    assert st.reserve_tier_pct == 30                # latch untouched (no outlook deps fake)


def test_drain_stops_at_the_tier_through_the_non_emergency_switch_on():
    st = AppState(); st.reserve_tier_pct = 30; st.night_over = True
    deps = FakeDeps(min_v=3.30, soc_pct=31.0, charge=-9.0, switch="off")  # wirklich entladend
    tick(cfg, st, deps, D(14, 0))                   # above the tier → drain untouched
    assert deps.acts == []
    deps.soc_pct = 30.0                             # SOC reached the tier while draining
    tick(cfg, st, deps, D(14, 10))
    assert [a[0] for a in deps.acts] == ["disable_on_timers", "switch_on"]
    assert deps.switch == "on"
    # NON-emergency flow: no emergency event, no EMERGENCY push — intent, not alarm
    assert not any(e in ("emergency_on", "low_capacity_on") for e, _ in deps.audits)
    assert [t for t, _ in deps.pushes] == []
    on = [f for e, f in deps.audits if e == "grid_on"]
    assert len(on) == 1
    assert on[0]["inputs"] == {"soc": 30.0, "tier_pct": 30, "ground": "tier"}
    assert on[0]["action"] == {"type": "switch_on"} and on[0]["result"] == "ok"
    assert st.last_observed_switch == "on"          # own act = the new observation baseline


def test_reserve_tier_layer_never_blocks_the_deep_emergency():
    # the tier is an ADDITIVE layer ABOVE the safety spine: the deep 2.90 V
    # backstop still acts (with a tier latched and BMS SOC below it)
    st = AppState(); st.reserve_tier_pct = 50; st.night_over = True
    deps = FakeDeps(min_v=2.89, soc_pct=40.0, switch="off")
    tick(cfg, st, deps, D(4, 0))
    assert [a[0] for a in deps.acts] == ["disable_on_timers", "switch_on"]
    assert any(e == "emergency_on" for e, _ in deps.audits)
    assert deps.pushes and deps.pushes[0][0] == "EMERGENCY"
    assert st.reserve_tier_pct == 50                # the layer stays latched, untouched


def test_day_rollover_keeps_the_reserve_tier_latch():
    # the eve sets the latch for the coming day (the feature's own path) …
    st = AppState(); st.morning_done = True
    deps = FakeDeps(min_v=V400, remaining_ah=AH400, switch="on",
                    inventory_timers=[], buckets=NEED300)
    deps.outlook_days = lambda: ([{"date": "2026-10-18", "weather_code": 95}],
                                 "2026-10-18T20:10:00")
    tick(cfg, st, deps, D(20, 10))
    assert st.reserve_tier_pct == 50
    # … and the day rollover does NOT clear it (only the next eve overwrites)
    tick(cfg, st, deps, datetime(2026, 10, 19, 11, 0))
    assert st.reserve_tier_pct == 50                # kept across midnight
    assert st.morning_done is False                 # the day-scoped latches DID reset
    assert st.current_date == datetime(2026, 10, 19).date()   # … and the rollover ran


def test_next_eve_overwrites_the_reserve_tier_latch():
    st = AppState(); st.reserve_tier_pct = 30; st.morning_done = True
    deps = FakeDeps(min_v=V400, remaining_ah=AH400, switch="on",
                    inventory_timers=[], buckets=NEED300)
    deps.outlook_days = lambda: ([{"date": "2026-10-19", "weather_code": 96,
                                   "gust_max_kmh": 40.0, "precip_sum_mm": 0.0}],
                                 "2026-10-19T20:10:00")
    tick(cfg, st, deps, D(20, 10))
    assert st.reserve_tier_pct == 50                # overwritten (30 → 50) at the next eve
    ev = [f for e, f in deps.audits if e == "reserve_tier"]
    assert len(ev) == 1 and ev[0]["inputs"]["tier_pct"] == 50


def test_tier_stop_ignores_a_charging_pack_and_only_stops_a_real_discharge():
    """REGRESSION (owner live-bug 2026-10-25 10:32): the pack charged at
    +13 A from solar (Ah climbing, SOC 11 <= tier 30) and the state-only stop
    still dialed switch_on with 'tier reached' — the design says the DISCHARGE
    stops at the tier, so a charging/flat pack must never trigger it."""
    st = AppState(); st.reserve_tier_pct = 30
    deps = FakeDeps(min_v=3.3, charge=13.0, soc_pct=11.0, switch="off",
                    solar_window=[])
    tick(cfg, st, deps, D(14, 0))                 # sun pours in, grid off
    assert deps.acts == []                        # NO act (sun-interrupted!)
    assert not any(e == "grid_on" for e, _ in deps.audits)
    deps.charge = -9.0                            # genuine discharge below the band
    tick(cfg, st, deps, D(14, 2))
    assert [a[0] for a in deps.acts] == ["disable_on_timers", "switch_on"]
    g = [f for e, f in deps.audits if e == "grid_on"]
    assert g and g[0]["inputs"]["ground"] == "tier" and g[0]["inputs"]["soc"] == 11.0


# --- restart-window safety (owner incident 2026-10-25 09:37:39 UTC) -------------
# A deploy restarted the addon: AppState is in-memory, st.reserve_tier_pct
# back to 0 — the FIRST tick after that evaluated the release gate with tier=0
# (open) and issued a day-drain release (09:37:39 act disable_on_timers +
# switch_off, grid_off inputs {soc: 12, ground: "drain"}) while the
# owner-ordered reserve tier was 30 %. Six seconds later the forecast refresh
# re-latched the tier (09:37:45 reserve_tier {tier_pct: 30}). The
# decision IS durable in the events index — only the latch forgets it on
# every restart. Fix: the seed reloads the newest reserve_tier decision
# into the latch BEFORE the first decision tick.


def test_restart_seed_reload_restores_the_tier_latch_before_any_tick():
    """The seed loads {tier_pct: 30} from the newest reserve_tier event into
    the latch — WITHOUT any tick having run, and WITHOUT an audit (a seed is
    no new decision, nothing is newly decided)."""
    from gridguard.main import restore_reserve_tier
    st = AppState()                                     # fresh restart latch: 0
    deps = FakeDeps(reserve_tier_inputs={"tier_pct": 30,
                                         "triggers": ["gust_max_kmh>75.0"],
                                         "lookahead_days": 3,
                                         "outlook_ts": "2026-10-17T21:00:00",
                                         "reference_kwh_m2": 4.444,
                                         "release_hyst_pct": 2})
    restore_reserve_tier(st, deps)
    assert st.reserve_tier_pct == 30                    # BEFORE any tick
    assert [e for e, _ in deps.audits] == []            # no audit for a seed
    assert st.reserve_tier_sig is None                  # accepted: sig stays unset


def test_restart_seed_reload_keeps_zero_when_no_newest_event_exists():
    """No reserve_tier event in the index (honest None) → the latch stays 0
    (open) — no guessed hold, no crash."""
    from gridguard.main import restore_reserve_tier
    st = AppState()
    restore_reserve_tier(st, FakeDeps(reserve_tier_inputs=None))
    assert st.reserve_tier_pct == 0


def test_restart_seed_reload_is_crash_netted():
    """The seed query is a consult side like any other: ANY Exception →
    latch stays 0, nothing escapes (the restart must never fail on the seed)."""
    from gridguard.main import restore_reserve_tier
    st = AppState()
    restore_reserve_tier(st, FakeDeps(
        reserve_tier_inputs=BmsError("events index unreachable")))
    assert st.reserve_tier_pct == 0                     # keep 0, no crash


def test_restart_seed_reload_ignores_negative_or_garbage_tier_values():
    """Only a non-negative INT in the inputs dict saturates the latch —
    negative, float- or string-shaped values from broken docs are ignored."""
    from gridguard.main import restore_reserve_tier
    for bad in (-1, "30", 30.5):
        st = AppState()
        restore_reserve_tier(st, FakeDeps(reserve_tier_inputs={"tier_pct": bad}))
        assert st.reserve_tier_pct == 0                 # garbage in, latch stays


def test_restart_seeded_tier_holds_the_release_on_the_first_tick_after_restart():
    """INTEGRATION PIN of the incident: seeded from the {tier_pct: 30} event,
    then the tick that WITHOUT the latch would fire the drain release (soc 20 <
    release_at 35 = tier + hyst 5, switch ON) — the seeded hold applies on
    tick 1 after restart: no switch_off, ONE tier_hold line."""
    from gridguard.main import restore_reserve_tier
    st = AppState()
    deps = FakeDeps(remaining_ah=60.0, soc_pct=20.0, switch="on", solar_window=[],
                    reserve_tier_inputs={"tier_pct": 30})
    restore_reserve_tier(st, deps)                      # run() does this BEFORE the loop
    assert st.reserve_tier_pct == 30
    tick(cfg, st, deps, D(11, 0))                       # the first tick after restart
    assert not any(a[0] == "switch_off" for a in deps.acts)   # grid stays on
    th = [f for e, f in deps.audits if e == "tier_hold"]
    assert len(th) == 1 and th[0]["inputs"] == {"soc": 20.0, "tier_pct": 30,
                                                "release_at_pct": 35}


def test_restart_before_seed_opened_the_release_window():
    """THE OLD HOLE, documented (incident 2026-10-25 09:37:39 UTC): the same
    tick fixture WITHOUT the seed — fresh AppState, latch 0 — lets the drain
    release through (soc 20 far below the owner-ordered tier 30): exactly
    the window the seed closes."""
    st = AppState()                                     # restart without seed: tier=0
    deps = FakeDeps(remaining_ah=60.0, soc_pct=20.0, switch="on", solar_window=[])
    tick(cfg, st, deps, D(11, 0))
    assert [a[0] for a in deps.acts] == ["disable_on_timers", "switch_off"]
    off = [f for e, f in deps.audits if e == "grid_off"]
    assert off and off[0]["inputs"]["ground"] == "drain" \
        and off[0]["inputs"]["soc"] == 20.0             # the incident: the drain ran


def test_run_seeds_the_reserve_tier_before_the_first_decision_tick(monkeypatch):
    """run()-seam pin: the seed runs with the real Deps wiring path
    BEFORE the loop's first tick() — the first decision tick sees the
    reloaded latch (30), not 0. (run() has no existing test harness; the
    loop is broken here via an uncaught KeyboardInterrupt in the tick-fence
    ring — the seed runs before it.)"""
    import gridguard.main as main_mod
    seen = {"seed": 0, "tick_pct": None}

    class _Deps:
        def __init__(self, cfg, **kw): pass
        def audit(self, event, **f): pass
        def latest_reserve_tier(self):
            seen["seed"] += 1
            return {"tier_pct": 30, "triggers": ["gust_max_kmh>75.0"]}
        def forecast_refresh(self, st=None): pass

    def _tick(cfg, st, deps, now, probe_due=False):
        seen["tick_pct"] = st.reserve_tier_pct          # latch at tick time
        raise KeyboardInterrupt                        # BaseException: interrupts run()

    monkeypatch.setattr(main_mod, "RealDeps", _Deps)
    monkeypatch.setattr(main_mod, "tick", _tick)
    with pytest.raises(KeyboardInterrupt):
        main_mod.run(cfg)
    assert seen == {"seed": 1, "tick_pct": 30}          # seeded BEFORE any tick
