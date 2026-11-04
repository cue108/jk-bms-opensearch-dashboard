"""RED tests for the reference-load model (Phase A, owner ruling 2026-10-30).

The owner verdict (condensed): the flat CLEAN-median night total must NOT be
the instantaneous law for the grid-RETURN crossing. The reference is the LOAD
OF THE MOST RECENT POSSIBLE NIGHT — the CALENDAR night (weather sunset →
sunrise) clipped to GRID-OFF intervals (no sun + no grid = pure house load),
plain mean in Wh/h, newest qualifying night first, mid-night grid switches =
KNOWN moments that CUT the night into usable clip segments (never a discard
class). No reference at all → the OLD median method stays (fallback, marked).

Fixtures below are the REAL incident data (2026-10-29 eve → the 04:14 fire):

* drift fixture: 26 night_need runs 19:01Z→20:59Z, need 2350.5 Wh constant,
  available 1645.6 → 1384.5 Wh (real drain ≈130–136 Wh/h), old model rate
  ≈210 Wh/h slid the armed time 03:31→03:53→04:14 (+17 min/h drift).
* mid-night switch clip: grid ON from 04:14 (confirmed write moment) → the
  reference clip = the [sunset → 04:14] grid-OFF portion only; the night is
  USABLE (it would have been a MIXED discard in the old bucket classes).
"""
import json
from datetime import date, datetime, timedelta
from unittest import mock

import pytest

import gridguard.web_payload as wp
from gridguard.bms import BmsReading
from gridguard.config import load_config
from gridguard.main import AppState, _night_resolve, tick
from gridguard.planner import plan_evening
from gridguard.verdict import soc_pct_from_v
from gridguard.web_page import PAGE
from test_main_tick import (AH400, AV400, D, FLOOR_WH, FIRE_EVE, GOOD,
                            FakeDeps, NEED300, cfg)

try:                            # RED: the module does not exist yet — guard so
    from gridguard.reference import (audit_inputs, grid_off_windows,   # every
                                     newest_reference,                 # test in
                                     reference_load as build_reference)  # this file
    REF_MODULE_MISSING = ""            # fails individually, not on collection
except ImportError:
    REF_MODULE_MISSING = "gridguard.reference module missing (Phase A)"
    audit_inputs = grid_off_windows = newest_reference = build_reference = None


def _need_reference_module():
    assert not REF_MODULE_MISSING, REF_MODULE_MISSING

# --- planner fixture pack: floor pinned to exactly 120 Wh -------------------
# soc curve (3.15 V) = 1.5 % × 200 Ah × 40 V = 120 Wh — the numbers the owner
# fixtures were quoted against (avail 1645.6 → 06:30 at 133 Wh/h ≈ +11.5 h).
REFCFG = load_config({**GOOD, "SAFETY_FACTOR": "1.0"})
REF_FULL_AH, REF_PACK_V = 200.0, 40.0
FLOOR120 = soc_pct_from_v(REFCFG.true_floor_v) / 100.0 * REF_FULL_AH * REF_PACK_V
NEED_INCIDENT = 2350.5                  # the constant night_need of the incident
LOAD_REF = 133.0                        # Wh/h — the owner's quoted reference


def _at(day, h, m):
    """Incident-night wall clock (test_main_tick's D() is pinned to Oct 18)."""
    return datetime(2026, 10, day, h, m)


def _ref_fire(now, avail):
    plan = plan_evening(now, available_wh=avail, need_wh=NEED_INCIDENT,
                        switch_state="off", night_len_hours=13.0, cfg=REFCFG,
                        forecast_ok=True, pack_v=REF_PACK_V,
                        full_ah=REF_FULL_AH, load_wh_h=LOAD_REF)
    assert plan.actions[0][0] == "failsafe_timer"
    return plan.actions[0][1]["at_local"]


def test_fixture_floor_is_exactly_120wh():
    """Pin the fixture math the crossing assertions below rest on."""
    assert FLOOR120 == pytest.approx(120.0)


# ---------------------------------------------------------------------------
# 1) The drift fixture: 133 Wh/h → crossing ≈06:30, NO drift between the
#    three sample instants (old model: 03:31→03:53→04:14, +17 min/h).
def test_reference_load_crossing_is_0630_and_does_not_drift():
    samples = [                                    # journal 2026-10-29 (UTC→local)
        (_at(29, 19, 1), 1645.6),
        (_at(29, 19, 56), 1527.1),
        (_at(29, 20, 57), 1388.4),
        (_at(29, 20, 59), 1384.5),
    ]
    fires = [_ref_fire(t, avail) for t, avail in samples]
    target = datetime(2026, 10, 30, 6, 30)         # the owner-quoted crossing
    for f in fires:
        assert abs((f - target).total_seconds()) <= 5 * 60, f
    spread_min = (max(fires) - min(fires)).total_seconds() / 60.0
    assert spread_min <= 3.0, f"crossing drifted {spread_min:.1f} min"


# ---------------------------------------------------------------------------
# 2) The mid-night switch fixture: the clip is the grid-OFF portion only.
TZ = REFCFG.tz
SUNSET = datetime(2026, 10, 28, 18, 30)            # weather sunset (naive local)
SUNRISE = datetime(2026, 10, 29, 7, 0)             # weather sunrise next day
FIRE_AT = datetime(2026, 10, 29, 4, 14)            # the confirmed write moment
PACKV_FIX = 52.0
DRAIN_AH_H = LOAD_REF / PACKV_FIX                  # 133 Wh/h at 52.0 V
AH0 = 100.0                                        # counter at the OFF moment

_SWITCH_OFF = {"ts": "2026-10-28T18:30:00+01:00", "event": "act", "result": "ok",
               "action": {"type": "switch_off"}, "inputs": {}}
_ENSURE_ON = {"ts": "2026-10-28T21:03:00+01:00", "event": "act", "result": "ok",
              "action": {"type": "failsafe_timer"}, "timer_action": "ensure_on",
              "inputs": {"fire_str": "04:14"}}


def _series():
    """Per-minute counter docs (production cadence): the pure house drain
    133 Wh/h until the 04:14 grid-ON fire, then FLAT (the UPS never charges
    the pack — grid ON ⇒ battery neither charges nor discharges)."""
    out = []
    t = datetime(2026, 10, 28, 18, 0)
    while t <= datetime(2026, 10, 29, 7, 30):
        if t <= FIRE_AT:
            ah = AH0 - DRAIN_AH_H * ((t - SUNSET).total_seconds() / 3600.0)
        else:
            ah = AH0 - DRAIN_AH_H * ((FIRE_AT - SUNSET).total_seconds() / 3600.0)
        out.append((t.replace(tzinfo=TZ), ah, PACKV_FIX))
        t += timedelta(minutes=1)
    return out


def test_midnight_switch_night_uses_only_the_grid_off_clip_and_stays_usable():
    _need_reference_module()
    offs = grid_off_windows([_SWITCH_OFF, _ENSURE_ON],
                            SUNSET - timedelta(hours=2),
                            SUNRISE + timedelta(hours=1), TZ)
    # newest-first candidate list: the RUNNING night is not a candidate, the
    # switched night wins with its clip → the night is USABLE, not discarded
    nights = [(date(2026, 10, 29), datetime(2026, 10, 29, 18, 30),
               datetime(2026, 10, 30, 7, 0)),           # still running at `now`
              (date(2026, 10, 28), SUNSET, SUNRISE)]
    ref = newest_reference(nights, offs, _series(),
                           now=datetime(2026, 10, 29, 9, 0, tzinfo=TZ),
                           min_clip_min=30.0)
    assert ref is not None                      # NOT a discard class
    assert ref["source"] == "clip"
    assert ref["night"] == "2026-10-28"
    assert len(ref["clips"]) == 1
    clip = ref["clips"][0]
    # the clip is EXACTLY [sunset (OFF write) → 04:14 (confirmed write moment)]
    assert clip["start"] == "2026-10-28T18:30+01:00"
    assert clip["end"] == "2026-10-29T04:14+01:00"
    assert clip["hours"] == pytest.approx((FIRE_AT - SUNSET).total_seconds() / 3600.0)
    # load = plain mean of the clip: 133 Wh/h …
    assert ref["wh_h"] == pytest.approx(LOAD_REF, abs=0.3)
    # … and the post-04:14 grid-ON span is EXCLUDED: the full calendar night
    # is 12.5 h; including the ON span would dilute the mean to ≈102 Wh/h.
    full_h = (SUNRISE - SUNSET).total_seconds() / 3600.0
    assert full_h == pytest.approx(12.5)
    assert clip["hours"] < full_h - 2.0
    assert ref["wh"] / full_h != pytest.approx(ref["wh_h"], abs=5.0)


# ---------------------------------------------------------------------------
# 3) Orchestration + audit contract (the event class `reference_load`).
def test_reference_load_asks_weather_events_and_counter_and_audits_clip():
    _need_reference_module()
    seen = {"sun": [], "since": None}

    def sun(d):
        seen["sun"].append(d)
        table = {date(2026, 10, 28): (datetime(2026, 10, 28, 6, 58),
                                      datetime(2026, 10, 28, 18, 31)),
                 date(2026, 10, 29): (datetime(2026, 10, 29, 6, 59),
                                      datetime(2026, 10, 29, 18, 30)),
                 date(2026, 10, 30): (datetime(2026, 10, 30, 7, 0),
                                      datetime(2026, 10, 30, 18, 29))}
        return table.get(d, (None, None))

    def events(since):
        seen["since"] = since
        return [_SWITCH_OFF, _ENSURE_ON]

    ref = build_reference(sun_times_fn=sun, events_fn=events,
                          series_fn=lambda a, b: _series(),
                          now=datetime(2026, 10, 29, 9, 0, tzinfo=TZ),
                          tz=TZ, n_nights=5, min_clip_min=30.0)
    # the three existing sources were consulted (same weather client path,
    # the addon's own journal, the BMS counter series)
    assert date(2026, 10, 28) in seen["sun"] and date(2026, 10, 29) in seen["sun"]
    assert seen["since"] is not None
    assert ref is not None and ref["source"] == "clip"
    inputs = audit_inputs(ref)
    assert inputs["source"] == "clip"
    assert inputs["night"] == "2026-10-28"
    assert inputs["clips"][0]["end"] == "2026-10-29T04:14+01:00"
    assert inputs["wh_h"] == pytest.approx(LOAD_REF, abs=0.3)
    assert inputs["min_clip_min"] == 30.0


def test_no_reference_falls_back_and_the_audit_marks_median_fallback():
    _need_reference_module()
    ref = build_reference(sun_times_fn=lambda d: (None, None),
                          events_fn=lambda since: [],
                          series_fn=lambda a, b: [],
                          now=datetime(2026, 10, 29, 9, 0, tzinfo=TZ), tz=TZ)
    assert ref is None                       # caller keeps the OLD median method
    assert audit_inputs(ref) == {"source": "median_fallback"}


def test_newest_completed_qualifying_night_wins_over_older_and_running():
    """Owner spec ruling: ALWAYS the most recent possible reference night — of the
    COMPLETED calendar nights the NEWEST one with a qualifying clip wins, and
    a still-RUNNING night never displaces a completed one (even when it would
    qualify on its own). Three nights here, each carrying its own measured
    load, so a wrong pick is loud: 28th = 133 Wh/h, 29th = 100 Wh/h, 30th
    (running at `now`) would qualify too if the completed-filter were gone."""
    _need_reference_module()
    s28, s29, s30 = _at(28, 18, 30), _at(29, 18, 30), _at(30, 18, 30)
    r29, r30, r31 = _at(29, 7, 0), _at(30, 7, 0), _at(31, 7, 0)
    now = _at(30, 9, 0)                      # the 30th's morning: 29th done

    def series():
        out, t = [], _at(28, 17, 30)
        while t <= _at(31, 6, 30):           # covers every candidate boundary
            if t < s29:
                ah = 100.0 - (LOAD_REF / 52.0) * ((t - s28).total_seconds()
                                                  / 3600.0)
            else:                            # after sunset29: the house draws 100 Wh/h
                ah = (100.0 - (LOAD_REF / 52.0) * 24.0
                      - (100.0 / 52.0) * ((t - s29).total_seconds() / 3600.0))
            out.append((t.replace(tzinfo=TZ), ah, 52.0))
            t += timedelta(minutes=10)
        return out

    offs = [(_at(28, 16, 30).replace(tzinfo=TZ),
             _at(31, 6, 0).replace(tzinfo=TZ))]      # grid OFF the whole span
    nights = [
        (date(2026, 10, 30), s30, r31),       # RUNNING at `now` (sunrise ahead)
        (date(2026, 10, 29), s29, r30),       # completed · 100 Wh/h
        (date(2026, 10, 28), s28, r29),       # completed · 133 Wh/h
    ]
    # BOTH completed nights qualify on their own …
    only28 = newest_reference(nights[2:], offs, series(), now=now)
    assert only28 is not None and only28["night"] == "2026-10-28"
    assert only28["wh_h"] == pytest.approx(LOAD_REF, abs=0.3)
    only29 = newest_reference(nights[1:2], offs, series(), now=now)
    assert only29 is not None and only29["wh_h"] == pytest.approx(100.0, abs=0.3)
    # … so the NEWEST completed one wins, the running one never displaces it
    ref = newest_reference(nights, offs, series(), now=now)
    assert ref is not None
    assert ref["night"] == "2026-10-29"
    assert ref["wh_h"] == pytest.approx(100.0, abs=0.3)


# ---------------------------------------------------------------------------
# 4/5) The eve consult wires the reference into the crossing (and the
#      fallback keeps the OLD median crossing unchanged).
class RefDeps(FakeDeps):
    def __init__(self, ref=None, **kw):
        super().__init__(**kw)
        self.ref, self.ref_calls = ref, 0

    def reference_load(self):
        self.ref_calls += 1
        return self.ref


def _eve_fire(deps):
    (lead, actions) = deps.plan_seen[0]
    kind, data = actions[-1]
    assert kind == "failsafe_timer"
    return data["at_local"]


def test_eve_without_reference_keeps_the_old_median_crossing():
    st, deps = AppState(), RefDeps(ref=None, switch="off",
                                   buckets=list(NEED300), remaining_ah=AH400)
    tick(cfg, st, deps, D(20, 10))
    assert deps.ref_calls >= 1                # the consult ASKS for the reference
    # the fallback crossing is byte-identical to the pre-model math
    assert _eve_fire(deps) == FIRE_EVE
    # …and the fallback marker rides the night_need audit (rate actually used)
    night = [f for e, f in deps.audits if e == "night_need"][-1]["inputs"]
    assert night["load_wh_h"] is None
    assert night["crossing_rate_wh_h"] == pytest.approx(300.0 * 1.2 / 11.0)


def test_eve_with_reference_crossing_uses_the_reference_load():
    st = AppState()
    deps = RefDeps(switch="off", buckets=list(NEED300),
                   remaining_ah=AH400,
                   ref={"night": "2026-10-17", "source": "clip", "wh_h": LOAD_REF,
                        "wh": 1200.0, "clips": [], "min_clip_min": 30.0})
    tick(cfg, st, deps, D(20, 10))
    # t_fire = now + (avail − floor) ÷ (load × safety) — NOT need×safety/night_len
    expected = D(20, 10) + timedelta(hours=(AV400 - FLOOR_WH) / (LOAD_REF * 1.2))
    assert _eve_fire(deps) == expected
    night = [f for e, f in deps.audits if e == "night_need"][-1]["inputs"]
    assert night["load_wh_h"] == pytest.approx(LOAD_REF)
    assert night["crossing_rate_wh_h"] == pytest.approx(LOAD_REF * 1.2)


# ---------------------------------------------------------------------------
# 6/7/8) OVERNIGHT RE-ESTIMATION (night_resolve) in the NIGHT phase.
ON_TIMER_0414 = {"type": "repeat", "do": {"switch": "on"}, "enabled": 1,
                 "atLocal": "14 4 * * *"}
REF_CLIP = {"night": "2026-10-29", "source": "clip", "wh_h": LOAD_REF,
            "wh": 1294.5, "min_clip_min": 30.0,
            "clips": [{"start": "2026-10-29T18:30+01:00",
                       "end": "2026-10-30T04:14+01:00",
                       "wh": 1294.5, "hours": 9.7333}]}


class NightDeps:
    """Minimal NIGHT-phase deps: read/act surface of the re-estimation step."""

    def __init__(self, ref=None, reachable_flag=True, switch="off",
                 timers=None, remaining_ah=17.9625, pack_v=REF_PACK_V,
                 v_avg=REF_PACK_V, full_ah=REF_FULL_AH):
        self.ref, self.reachable_flag = ref, reachable_flag
        self.switch = switch
        self.timers = list(timers) if timers is not None else [ON_TIMER_0414]
        self.remaining_ah, self.pack_v, self.v_avg, self.full_ah = \
            remaining_ah, pack_v, v_avg, full_ah
        self.audits, self.pushes = [], []
        self.applied, self.reads, self.bms_calls = [], 0, 0
        self.reachable_calls = 0

    def audit(self, event, **f): self.audits.append((event, f))
    def push(self, title, text): self.pushes.append((title, text))

    def reachable(self):
        self.reachable_calls += 1
        return self.reachable_flag
    def timer_exists(self): return False

    def read_state(self):
        self.reads += 1
        return self.switch, list(self.timers)

    def bms_latest(self):
        self.bms_calls += 1
        return BmsReading("t", None, None, 0.0, None, self.remaining_ah,
                          self.pack_v, full_ah=self.full_ah)

    def pack_v_avg(self): return self.v_avg
    def reference_load(self): return self.ref
    def apply_plan(self, plan):
        self.applied.append(plan)
        return True


def _night_audit(deps):
    return [f for e, f in deps.audits if e == "night_resolve"]


def test_blackout_recovery_0200_writes_the_new_crossing_and_audits_old_new():
    """THE BLACKOUT-RECOVERY EXCEPTION — not the normal-night path (review F1).

    02:00 local sits INSIDE the owner's WiFi blackout (reachable ≈False from
    ~23:00–08:00), and on a normal night night_over latched at ~22:00 → every
    post-midnight tick is Phase.MORNING (verdict.py) — 00:00–09:00
    re-estimation is structurally unreachable on a NORMAL night, correctly so
    (blackout preservation). This instant models the EXCEPTION: the link died
    before the22:00 probe (night_over never latched → phase stays NIGHT) and
    CONTACT RESUMES at 02:00. Armed 04:14, fresh available 718.5 Wh →
    crossing 05:45 (> 20 min move) → the full write path + audit.
    The normal-night anti-drift workhorse is the EVE consult (the 19:01–
    20:59Z drift fixture above); the pre-blackout 22:30 tail case below pins
    THAT window."""
    deps = NightDeps(ref=dict(REF_CLIP), remaining_ah=718.5 / REF_PACK_V)
    st = AppState()
    tick(cfg, st, deps, _at(30, 2, 0))
    assert len(deps.applied) == 1
    kind, data = deps.applied[0].actions[0]
    assert kind == "failsafe_timer"
    assert data["at_local"] == datetime(2026, 10, 30, 5, 45)
    assert data["fire_str"] == "05:45"
    audit = _night_audit(deps)
    assert len(audit) == 1
    assert audit[0]["inputs"] == {"old_fire": "04:14", "new_fire": "05:45",
                                  "available_wh": 718.5, "load_wh_h": LOAD_REF,
                                  "deadband_min": 20}
    assert audit[0].get("result") == "ok"
    # the announced/plan fields follow the write truth (§27 machinery)
    assert st.last_planned_fire == datetime(2026, 10, 30, 5, 45)
    assert st.plan_ideal == datetime(2026, 10, 30, 5, 45)


def test_normal_night_tail_2230_re_estimates_and_writes():
    """F1 scoping pin — the NORMAL-night window of night_resolve: the pre-
    blackout NIGHT-phase tail after the eve close (22:30 local; night_over
    already latched by the reachable 22:00 probe — the phase stays NIGHT
    because t >= window_end wins BEFORE the night_over→MORNING branch of
    verdict.py, which only flips t < window_start). Same write contract:
    armed 04:14, fresh available 1277.1 Wh → crossing 05:45 (> 20 min)."""
    deps = NightDeps(ref=dict(REF_CLIP), remaining_ah=1277.1 / REF_PACK_V)
    st = AppState()
    st.night_over = True                      # latched by the reachable 22:00 probe
    tick(cfg, st, deps, _at(29, 22, 30))
    # phase truth: this tick IS a night tick …
    assert deps.audits[0] == ("tick", {"phase": "night"})
    # … and the re-estimation ran the full write path
    assert len(deps.applied) == 1
    kind, data = deps.applied[0].actions[0]
    assert kind == "failsafe_timer"
    assert data["at_local"] == datetime(2026, 10, 30, 5, 45)
    assert data["fire_str"] == "05:45"
    audit = _night_audit(deps)
    assert len(audit) == 1
    assert audit[0]["inputs"] == {"old_fire": "04:14", "new_fire": "05:45",
                                  "available_wh": 1277.1, "load_wh_h": LOAD_REF,
                                  "deadband_min": 20}
    assert audit[0].get("result") == "ok"
    assert st.last_planned_fire == datetime(2026, 10, 30, 5, 45)
    assert st.plan_ideal == datetime(2026, 10, 30, 5, 45)


def test_deadband_fresh_crossing_within_20_min_writes_nothing():
    # armed 04:14; fresh crossing 04:29 → 15 min move ≤ 20 → silent keep
    deps = NightDeps(ref=dict(REF_CLIP), remaining_ah=516.4 / REF_PACK_V)
    tick(cfg, AppState(), deps, _at(30, 2, 0))
    assert deps.applied == []                  # the step LOOKED (read device
    assert deps.reads >= 1                     # truth) … but wrote NOTHING
    assert _night_audit(deps) == []


def test_blackout_reach_down_preserves_the_armed_timer_untouched():
    deps = NightDeps(ref=dict(REF_CLIP), reachable_flag=False,
                     remaining_ah=718.5 / REF_PACK_V)
    st = AppState()
    tick(cfg, st, deps, _at(30, 2, 0))
    assert deps.applied == []                  # no write path call
    assert _night_audit(deps) == []            # no night_resolve audit
    assert deps.reachable_calls >= 1           # the step CONSULTS the reach truth …
    assert deps.reads == 0 and deps.bms_calls == 0   # … and then does NOTHING
    assert deps.timers == [ON_TIMER_0414]      # the insurance survives
    assert st.last_planned_fire is None


def test_night_without_reference_never_touches_the_timer():
    deps = NightDeps(ref=None, remaining_ah=718.5 / REF_PACK_V)
    tick(cfg, AppState(), deps, _at(30, 2, 0))
    assert deps.reachable_calls >= 1           # the step engaged and evaluated …
    assert deps.applied == [] and _night_audit(deps) == []   # … no ref → no touch


def test_eve_and_final_phases_never_run_the_night_resolve_step():
    # EVE_PLAN at 20:10 with the reference present: the eve plans, but the
    # night_resolve step belongs to the NIGHT phase ONLY.
    st = AppState()
    deps = RefDeps(switch="off", buckets=list(NEED300), ref=dict(REF_CLIP))
    tick(cfg, st, deps, _at(29, 21, 30))
    assert deps.ref_calls >= 1                 # the eve consults the reference …
    assert _night_audit(deps) == []            # … but night_resolve is NIGHT-only


def test_night_resolve_docstring_states_the_true_operating_window():
    """Review F1/F2 (documentation scoping, no model change): the docstring
    must state where the step REALLY runs — the pre-blackout NIGHT tail plus
    the blackout-recovery contact windows, with the EVE consult named as the
    anti-drift workhorse of normal nights — and must NOT claim the emergency
    floor-watch sits above this step (it belongs to the awake phases below
    the NIGHT branch, which returns before reaching it)."""
    low = (_night_resolve.__doc__ or "").lower()
    assert "pre-blackout tail" in low              # F1: the ~22:00–23:00 window
    assert "recovery" in low                       # F1: the recovery path
    assert "workhorse" in low and "eve" in low     # F1: the eve consult is named
    assert "runs above this step unchanged" not in low   # F2: the false claim


# ---------------------------------------------------------------------------
# 9) The DISPLAY line in the payload + the page render pin.
_FORECAST = {"daily": {"time": ["2026-10-25", "2026-10-26", "2026-10-27"],
                       "shortwave_radiation_sum": [4.2, 3.1, 2.4]},
             "daily_units": {"shortwave_radiation_sum": "MJ/m²"}}
_TIMELINE = {"hits": {"hits": [
    {"_source": {"ts": "2026-10-25T11:40:00+00:00", "event": "tick",
                 "phase": "day_hold", "result": "", "inputs": {},
                 "action": "", "timer_action": ""}},
    {"_source": {"ts": "2026-10-25T09:10:00+00:00", "event": "start",
                 "result": "dry_run=False", "inputs": {},
                 "action": "", "timer_action": ""}},
]}, "total": {"value": 2}}
_EMPTY = {"hits": {"hits": [], "total": {"value": 0}}}
_STUB: dict = {}


class _Resp:
    def __init__(self, obj): self._b = json.dumps(obj).encode()
    def read(self): return self._b
    def __enter__(self): return self
    def __exit__(self, *a): return False
    status, headers = 200, {}


def _router(req, timeout=None):
    url = str(getattr(req, "full_url", req))
    if "open-meteo.com" in url:
        return _Resp(_FORECAST)
    data = getattr(req, "data", None) or b""
    if url.endswith("/_msearch"):
        lines = data.decode().strip().split("\n")
        responses = []
        for i in range(0, len(lines) - 1, 2):
            body = lines[i + 1]
            if '"reference_load"' in body:
                src = _STUB.get("ref_event")
            elif '"night_need"' in body:
                src = _STUB.get("nn_event")
            else:
                src = None
            responses.append({"hits": {"hits": [{"_source": src}] if src else [],
                                       "total": {"value": 1 if src else 0}}})
        return _Resp({"responses": responses})
    if b'"match_all"' in data:
        return _Resp(_TIMELINE)
    return _Resp(_EMPTY)


@pytest.fixture()
def payload_env(monkeypatch):
    _STUB.clear()
    monkeypatch.setattr(wp, "TZ", "UTC")
    monkeypatch.setattr(wp, "OS", "http://stub:9200")
    monkeypatch.setattr(wp, "INDEX", "bms-test-*")
    if isinstance(getattr(wp, "_SUN_CACHE", None), dict):
        wp._SUN_CACHE.clear()
    if isinstance(getattr(wp, "_FC3", None), dict):
        wp._FC3.update(ts=0.0, data=None)
    with mock.patch("urllib.request.urlopen", side_effect=_router):
        yield


def test_payload_last_ref_line_from_the_clip_reference(payload_env):
    _STUB["ref_event"] = {"ts": "2026-10-30T06:05:00+00:00",
                          "event": "reference_load",
                          "inputs": {"source": "clip", "night": "2026-10-29",
                                     "wh_h": 133.0, "wh": 1294.5,
                                     "clips": [], "min_clip_min": 30}}
    out = wp.fetch_gridguard()
    assert "reference_load" in out["plan"]      # the query rides the plan batch
    line = out["decision"]["last_ref"]
    assert line == "last_ref: Thu 29 Oct clip · 133 Wh/h"
    assert len(line) <= 60


def test_payload_last_ref_median_fallback_line_with_the_model_rate(payload_env):
    _STUB["ref_event"] = {"ts": "2026-10-30T06:05:00+00:00",
                          "event": "reference_load",
                          "inputs": {"source": "median_fallback"}}
    _STUB["nn_event"] = {"ts": "2026-10-29T20:57:00+00:00",
                         "event": "night_need",
                         "inputs": {"need_wh": 2350.5, "crossing_rate_wh_h": 210.4}}
    line = wp.fetch_gridguard()["decision"]["last_ref"]
    assert line == "last_ref: median-fallback (210 Wh/h)"
    assert len(line) <= 60


def test_payload_last_ref_absent_without_any_reference_event(payload_env):
    assert wp.fetch_gridguard()["decision"].get("last_ref") is None


def test_page_renders_the_reference_basis_next_to_the_crossing_time():
    # DISPLAY-TRUTH: the basis must be visible WHERE the crossing time shows
    # (the Actor-plan card's sub line carries the announced/plan ideal times).
    assert 'if(D.last_ref) $("ap-sub").textContent+=" · "+D.last_ref;' in PAGE
