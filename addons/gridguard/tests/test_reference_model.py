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
from gridguard.web_page import PAGE
from test_main_tick import (AH400, AV400, D, FLOOR_WH, FIRE_EVE, GOOD,
                            FakeDeps, NEED300, PACK_V, cfg)

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
FLOOR120 = 120.0                    # the fixture quote's number (the table left the source)
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
    # raw head (owner floor law 2026-11-13: no floor outside the UPS mode): the
    # 133 Wh/h reference crossing of the first sample (1645.6 Wh from 19:01 →
    # ≈ 07:23; the old 06:30 quote carried the retired 120 Wh band).
    target = datetime(2026, 10, 30, 7, 23)
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
    # raw head (owner floor law 2026-11-13: no floor outside the UPS):300 Wh ÷
    # the median-basis rate = 9.17 h → the crossing stays inside the learned
    # night and is computed with the OLD fallback basis (never the reference).
    st, deps = AppState(), RefDeps(ref=None, switch="off",
                                   buckets=list(NEED300),
                                   remaining_ah=300.0 / PACK_V)
    tick(cfg, st, deps, D(20, 10))
    assert deps.ref_calls >= 1                # the consult ASKS for the reference
    # the fallback crossing runs on raw available ÷ (need × safety ÷ night_len)
    assert _eve_fire(deps) == D(20, 10) + timedelta(
        hours=300.0 / (300.0 * 1.2 / 11.0))
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
    # t_fire = now + available ÷ (load × safety) — NOT need×safety/night_len
    # (raw head: owner floor law 2026-11-13, no floor outside the UPS mode)
    expected = D(20, 10) + timedelta(hours=AV400 / (LOAD_REF * 1.2))
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
    raw-head crossing 06:30 (718.5 ÷ 159.6 Wh/h = +4.50 h; > 20 min move)
    → the full write path + audit.
    The normal-night anti-drift workhorse is the EVE consult (the 19:01–
    20:59Z drift fixture above); the pre-blackout 22:30 tail case below pins
    THAT window."""
    deps = NightDeps(ref=dict(REF_CLIP), remaining_ah=718.5 / REF_PACK_V)
    st = AppState()
    tick(cfg, st, deps, _at(30, 2, 0))
    expected = _at(30, 2, 0) + timedelta(hours=718.5 / (LOAD_REF * 1.2))
    assert len(deps.applied) == 1
    kind, data = deps.applied[0].actions[0]
    assert kind == "failsafe_timer"
    assert data["at_local"] == expected
    assert data["fire_str"] == "06:30"
    audit = _night_audit(deps)
    assert len(audit) == 1
    assert audit[0]["inputs"] == {"old_fire": "04:14", "new_fire": "06:30",
                                  "available_wh": 718.5, "load_wh_h": LOAD_REF,
                                  "deadband_min": 20}
    assert audit[0].get("result") == "ok"
    # the announced/plan fields follow the write truth (§27 machinery)
    assert st.last_planned_fire == expected
    assert st.plan_ideal == expected


def test_normal_night_tail_2230_re_estimates_and_writes():
    """F1 scoping pin — the NORMAL-night window of night_resolve: the pre-
    blackout NIGHT-phase tail after the eve close (22:30 local; night_over
    already latched by the reachable 22:00 probe — the phase stays NIGHT
    because t >= window_end wins BEFORE the night_over→MORNING branch of
    verdict.py, which only flips t < window_start). Same write contract:
    armed 04:14, fresh available 1277.1 Wh → raw-head crossing 06:30
    (1277.1 ÷ 159.6 Wh/h = +8.00 h; > 20 min)."""
    deps = NightDeps(ref=dict(REF_CLIP), remaining_ah=1277.1 / REF_PACK_V)
    st = AppState()
    st.night_over = True                      # latched by the reachable 22:00 probe
    tick(cfg, st, deps, _at(29, 22, 30))
    expected = _at(29, 22, 30) + timedelta(hours=1277.1 / (LOAD_REF * 1.2))
    # phase truth: this tick IS a night tick …
    assert deps.audits[0] == ("tick", {"phase": "night"})
    # … and the re-estimation ran the full write path
    assert len(deps.applied) == 1
    kind, data = deps.applied[0].actions[0]
    assert kind == "failsafe_timer"
    assert data["at_local"] == expected
    assert data["fire_str"] == "06:30"
    audit = _night_audit(deps)
    assert len(audit) == 1
    assert audit[0]["inputs"] == {"old_fire": "04:14", "new_fire": "06:30",
                                  "available_wh": 1277.1, "load_wh_h": LOAD_REF,
                                  "deadband_min": 20}
    assert audit[0].get("result") == "ok"
    assert st.last_planned_fire == expected
    assert st.plan_ideal == expected


def test_deadband_fresh_crossing_within_20_min_writes_nothing():
    # armed 04:14; fresh crossing 04:30 (raw head: 399 Wh ÷ 159.6 = +2.5 h)
    # → 16 min move ≤ 20 → silent keep
    deps = NightDeps(ref=dict(REF_CLIP), remaining_ah=399.0 / REF_PACK_V)
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


# ---------------------------------------------------------------------------
# Owner rules 2026-11-12 (the three-tier reference hierarchy): the value is a
# RATE (Wh/h) for the timer projection ONLY. Tier1 = a WHOLE off-grid solar
# night REPLACES the default (marginal accuracy edge); tier2 = the off-grid
# window we have directly after sunset (ANY age — "if we have gone seven
# days without a value"); tier3 = HOLD the last journaled value until new data
# arrives (never a static value: every new measurement replaces it = the value
# WANDERS with the load profile). Grid-on nights learn NOTHING (the battery is
# isolated from the loads).
def _rate_series(rate_wh_h, start_day=18, end_day=31, v=52.0, switch_at=None,
                 switch_rate=None):
    """A discharge series at `rate_wh_h` (Wh/h) — optional rate switch at a
    wall-clock instant (the existing `_series` fixture keeps its own name)."""
    out, t = [], _at(start_day, 15, 0)
    ah = 4000.0
    while t <= _at(end_day, 8, 0):
        out.append((t.replace(tzinfo=TZ), ah, v))
        rate = (switch_rate if (switch_at is not None and t >= switch_at
                                and switch_rate is not None) else rate_wh_h)
        ah -= (rate / v) * (10.0 / 60.0)
        t += timedelta(minutes=10)
    return out


def test_tier1_whole_off_grid_solar_night_replaces_the_default():
    """The HOURS-RANKED pick (owner ruling 2026-11-12, 23:3x): the night with
    the MOST grid-off hours inside the astronomical night wins — here the
    12.5 h whole night beats the NEWER2 h post-sunset partial (the whole
    night is just the max-hours case, not a separate class)."""
    _need_reference_module()
    s28, r29 = _at(28, 18, 30), _at(29, 7, 0)
    s29, r30 = _at(29, 18, 30), _at(30, 7, 0)
    now = _at(30, 9, 0)
    offs = [
        (_at(28, 17, 0).replace(tzinfo=TZ), _at(29, 7, 30).replace(tzinfo=TZ)),   # FULL night28
        (_at(29, 18, 30).replace(tzinfo=TZ), _at(29, 20, 30).replace(tzinfo=TZ)),  # 2h partial (NEWER)
    ]
    nights = [(date(2026, 10, 29), s29, r30), (date(2026, 10, 28), s28, r29)]
    ref = newest_reference(nights, offs, _rate_series(133.0,
                                                      switch_at=_at(29, 18, 30),
                                                      switch_rate=100.0),
                           now=now)
    assert ref is not None and ref["night"] == "2026-10-28"
    assert ref["full"] is True
    assert ref["wh_h"] == pytest.approx(133.0, abs=0.5)


def test_tier2_post_sunset_window_carries_the_rate_at_any_age():
    """The window's hours-ranking with a single candidate: the post-sunset
    window we HAVE carries the rate — ANY age (winter), never a guess,
    never None."""
    _need_reference_module()
    s19, r20 = _at(19, 18, 30), _at(20, 7, 0)
    now = _at(29, 9, 0)                          # the clip is TEN days old
    offs = [(_at(19, 18, 30).replace(tzinfo=TZ),
             _at(19, 20, 30).replace(tzinfo=TZ))]
    ref = newest_reference([(date(2026, 10, 19), s19, r20)], offs,
                           _rate_series(141.0, start_day=19, end_day=20),
                           now=now)
    assert ref is not None and ref["night"] == "2026-10-19"
    assert ref["full"] is False
    assert ref["wh_h"] == pytest.approx(141.0, abs=0.5)


def test_tier3_holds_the_last_journaled_value_when_no_off_grid_data():
    """Tier3: grid-ON nights (no off-window touches the solar night — the
    battery is ISOLATED, owner rule) → nothing measurable anywhere → HOLD the
    last journaled clip value until new data arrives (source 'held', the
    provenance preserved)."""
    _need_reference_module()

    def sun(d):
        return (datetime(d.year, d.month, d.day, 7, 0),
                datetime(d.year, d.month, d.day, 18, 30))

    history = [{"ts": "2026-09-20T06:10:00+00:00", "event": "reference_load",
                "inputs": {"source": "clip", "night": "2026-09-20",
                           "wh": 1300.0, "wh_h": 130.0, "clips": [],
                           "min_clip_min": 30}}]
    ref = build_reference(sun_times_fn=sun, events_fn=lambda since: [],
                          series_fn=lambda a, b: [],
                          now=datetime(2026, 10, 29, 9, 0), tz=TZ,
                          n_nights=5, history_fn=lambda: history)
    assert ref is not None and ref["source"] == "held"
    assert ref["wh_h"] == 130.0 and ref["night"] == "2026-09-20"


def test_grid_on_nights_yield_no_rate_even_when_older_data_exists():
    """Owner rule: grid-ON = the battery is isolated from the loads → NO
    measurable value from those nights (they never win, never fall back to
    a median of nothing) — the held/None tiers decide instead."""
    _need_reference_module()
    offs = [(_at(18, 17, 0).replace(tzinfo=TZ), _at(19, 7, 30).replace(tzinfo=TZ))]
    # the candidate night is grid-ON (its window does NOT touch the off-span)
    s25, r26 = _at(25, 18, 30), _at(26, 7, 0)
    ref = newest_reference([(date(2026, 10, 25), s25, r26)], offs,
                           _rate_series(133.0, start_day=18, end_day=26),
                           now=_at(29, 9, 0))
    assert ref is None


def test_payload_last_ref_line_names_the_full_solar_night(payload_env):
    _STUB["ref_event"] = {"ts": "2026-10-30T06:05:00+00:00",
                          "event": "reference_load",
                          "inputs": {"source": "clip", "full": True,
                                     "night": "2026-10-29",
                                     "wh_h": 133.0, "wh": 1294.5,
                                     "clips": [], "min_clip_min": 30}}
    line = wp.fetch_gridguard()["decision"]["last_ref"]
    assert line == "last_ref: Thu 29 Oct full night · 133 Wh/h"
    assert len(line) <= 60


def test_payload_last_ref_line_names_the_held_reference(payload_env):
    _STUB["ref_event"] = {"ts": "2026-10-30T06:05:00+00:00",
                          "event": "reference_load",
                          "inputs": {"source": "held", "night": "2026-09-20",
                                     "wh_h": 130.0, "wh": 1294.5,
                                     "clips": [], "min_clip_min": 30}}
    line = wp.fetch_gridguard()["decision"]["last_ref"]
    assert line == "last_ref: held from Sun 20 Sep · 130 Wh/h"
    assert len(line) <= 60


def test_tier1_full_night_at_the_seven_day_boundary_beats_newer_partial():
    """THE BOUNDARY (owner rule: "if we have gone seven days without a
    value…" → day 7 INCLUSIVE): the night exactly `n_nights` days
    before `now` must still be EVALUATED — its hours (12.5 h vs 2.0 h) decide
    under the 23:3x hours-ranking. The date walk has to include
    `today - n_nights` (the running night eats one slot otherwise — live find
    2026-11-12: the day-7 night fell just outside block0 and a day-2 partial
    won)."""
    _need_reference_module()
    # NOTE: pre-Oct25 nights are CEST (+02:00), after the switch CET (+01:00).
    events = [
        {"ts": "2026-10-23T17:00:00+02:00", "event": "act", "result": "ok",
         "action": {"type": "switch_off"}, "inputs": {}},        # off BEFORE sunset
        {"ts": "2026-10-24T07:30:00+02:00", "event": "act", "result": "ok",
         "action": {"type": "switch_on"}, "inputs": {}},         # on AFTER sunrise
        {"ts": "2026-10-28T18:30:00+01:00", "event": "act", "result": "ok",
         "action": {"type": "switch_off"}, "inputs": {}},        # partial, 2 days old
        {"ts": "2026-10-28T20:30:00+01:00", "event": "act", "result": "ok",
         "action": {"type": "switch_on"}, "inputs": {}},
    ]
    series = _rate_series(150.0, start_day=22, end_day=30,
                          switch_at=_at(28, 18, 30), switch_rate=111.0)

    def sun(d):
        return (datetime(d.year, d.month, d.day, 7, 0),
                datetime(d.year, d.month, d.day, 18, 30))

    ref = build_reference(sun_times_fn=sun, events_fn=lambda since: events,
                          series_fn=lambda a, b: series,
                          now=datetime(2026, 10, 30, 9, 0), tz=TZ, n_nights=7)
    assert ref is not None
    assert ref["night"] == "2026-10-23"        # exactly day 7 → tier1, inclusive
    assert ref["full"] is True
    assert ref["wh_h"] == pytest.approx(150.0, abs=1.0)


def test_most_grid_off_hours_win_even_when_older_and_not_gapless():
    """Owner ruling 2026-11-12 (23:3x) — DISSOLVES the gapless-full gate for
    the load-profile measurement: "the load-profile reference follows the
    astronomical night … what matters are the times of the grid-off state
    during the astronomical night. The more hours we get here, the more
    accurate the load profile." Selection = the MOST grid-off HOURS inside
    the astronomical night (the whole night = just the max-hours case), the
    freshness stays the7-day window. The live case: the day-7 night carries
    13.4 h off-grid with a5-min dawn release (full=False) and MUST beat the
    day-2 evening fragment of2.6 h."""
    _need_reference_module()
    events = [
        {"ts": "2026-10-23T09:00:00+02:00", "event": "act", "result": "ok",
         "action": {"type": "switch_off"}, "inputs": {}},   # off all day
        {"ts": "2026-10-24T06:55:00+02:00", "event": "act", "result": "ok",
         "action": {"type": "switch_on"}, "inputs": {}},    # dawn release →5-min gap
        {"ts": "2026-10-28T18:30:00+01:00", "event": "act", "result": "ok",
         "action": {"type": "switch_off"}, "inputs": {}},   # newer fragment
        {"ts": "2026-10-28T20:30:00+01:00", "event": "act", "result": "ok",
         "action": {"type": "switch_on"}, "inputs": {}},
    ]
    series = _rate_series(150.0, start_day=22, end_day=30,
                          switch_at=_at(28, 18, 30), switch_rate=111.0)

    def sun(d):
        return (datetime(d.year, d.month, d.day, 7, 0),
                datetime(d.year, d.month, d.day, 18, 30))

    ref = build_reference(sun_times_fn=sun, events_fn=lambda since: events,
                          series_fn=lambda a, b: series,
                          now=datetime(2026, 10, 30, 9, 0), tz=TZ, n_nights=7)
    assert ref is not None
    assert ref["night"] == "2026-10-23"        # OLDER but12.4 h vs2.0 h → wins
    assert ref["full"] is False                # the dawn release keeps it non-gapless
    assert ref["wh_h"] == pytest.approx(150.0, abs=1.0)
