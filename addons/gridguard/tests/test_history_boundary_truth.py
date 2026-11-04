"""Owner ruling #10 (2026-10-26): WINDOW TO BACK-END TRUTH — every
timer/target/boundary surface follows the hierarchy:

(1) newest WRITE-CONFIRMED program generation (each ensure_on act with a
    time; superseded by the next write — last-wins, movement is truth),
(2) else observed measurement: the journal (state_observed / switch acts)
    plus the CLUSTER-measured first-positive intake filling an off→on
    STATE GAP (the amp step the journal never saw — the device fires
    across the unreachable window),
(3) else NO asserted boundary (absence, never a fabrication).

NIGHT PLAN pushes / plan ideals appear ONLY as explicitly-labelled context
markers (plan_marks), never as state evidence.
"""
from datetime import date, datetime, timedelta, timezone

import gridguard.web_payload as wp

UTC = timezone.utc


def _dt(*a):
    return datetime(*a, tzinfo=UTC)


def _gen(ts, fire=None):
    src = {"ts": ts, "event": "act", "action": {"type": "failsafe_timer"},
           "timer_action": "ensure_on", "result": "ok"}
    if fire:
        src["inputs"] = {"fire_str": fire, "at_local": "2026-10-26T" + fire}
    return src


# --- tier (1): write-confirmed generations -----------------------------------

def test_superseded_generations_drop_last_write_fires():
    """Last night's exact shape: 09:26 → 08:44 → 09:01 writes; each next
    write REPLACES the ON slot before it fired → only the last fires."""
    gens = [_gen("2026-10-25T19:01:38+00:00", "09:26"),
            _gen("2026-10-25T20:57:22+00:00", "08:44"),
            _gen("2026-10-25T20:59:26+00:00", "09:01")]
    now = _dt(2026, 10, 26, 12, 0)
    fires = wp._write_confirmed_fires(gens, [], now, UTC)
    assert fires == [_dt(2026, 10, 26, 9, 1)]


def test_generation_that_already_fired_survives_a_later_write():
    """Morning re-program at 08:00 cannot unsay the 07:10 fire — the
    generation stands iff its fire precedes the next write."""
    gens = [_gen("2026-10-26T06:00:00+00:00", "07:10"),
            _gen("2026-10-26T08:00:00+00:00", "12:30")]
    now = _dt(2026, 10, 26, 9, 0)
    assert wp._write_confirmed_fires(gens, [], now, UTC) == [_dt(2026, 10, 26, 7, 10)]


def test_unfired_generation_asserts_nothing_during_the_eve():
    """While the eve moves the target, the HISTORY boundary must not be
    asserted before the program actually fires (absence rule)."""
    gens = [_gen("2026-10-25T20:59:26+00:00", "09:01")]
    now = _dt(2026, 10, 25, 21, 0)
    assert wp._write_confirmed_fires(gens, [], now, UTC) == []


def test_cancel_between_write_and_fire_disarms_the_generation():
    """The morning drain disables timers before firing → that fire never
    happened (same cancel semantics the push evidence used to carry)."""
    gens = [_gen("2026-10-25T20:59:26+00:00", "09:01")]
    cancel = _dt(2026, 10, 26, 8, 11, 37)
    now = _dt(2026, 10, 26, 12, 0)
    assert wp._write_confirmed_fires(gens, [cancel], now, UTC) == []
    # a cancel BEFORE the write does not disarm it
    early = _dt(2026, 10, 25, 20, 0)
    assert wp._write_confirmed_fires(gens, [early], now, UTC) == [_dt(2026, 10, 26, 9, 1)]


def test_generation_without_a_time_is_skipped_never_invented():
    assert wp._write_confirmed_fires([_gen("2026-10-25T20:59:26+00:00")],
                                     [], _dt(2026, 10, 26, 12, 0), UTC) == []
    assert wp._write_confirmed_fires([], [], _dt(2026, 10, 26, 12, 0), UTC) == []


# --- tier (2): the measured off→on state-gap fill ----------------------------

def _gap():
    return [(_dt(2026, 10, 25, 9, 37), "off"),   # last observed off (yesterday's drain)
            (_dt(2026, 10, 26, 8, 11, 36), "on")]  # re-baseline after the blackout


def test_off_on_gap_filled_at_the_measured_first_positive():
    bounds = {date(2026, 10, 26): (_dt(2026, 10, 26, 8, 2), _dt(2026, 10, 26, 16, 30))}
    out, measured = wp._measured_on_fill(_gap(), bounds)
    assert measured == [_dt(2026, 10, 26, 8, 2)]
    # the fill IS the boundary; the later baseline merges into the same run
    assert out == [(_dt(2026, 10, 25, 9, 37), "off"),
                   (_dt(2026, 10, 26, 8, 2), "on")]


def test_no_measurement_inside_the_gap_keeps_the_observed_boundary():
    """Fallback: first positive OUTSIDE the gap (or none) → the observed
    state change stands exactly where the journal saw it."""
    bounds = {date(2026, 10, 26): (_dt(2026, 10, 26, 12, 0), _dt(2026, 10, 26, 16, 30))}
    out, measured = wp._measured_on_fill(_gap(), bounds)
    assert measured == []
    assert out == _gap()


def test_no_day_bounds_at_all_changes_nothing():
    out, measured = wp._measured_on_fill(_gap(), {})
    assert (out, measured) == (_gap(), [])
    out, measured = wp._measured_on_fill([], {})
    assert (out, measured) == ([], [])


def test_on_to_off_gaps_are_never_filled():
    ev = [(_dt(2026, 10, 26, 6, 0), "on"), (_dt(2026, 10, 26, 9, 0), "off")]
    bounds = {date(2026, 10, 26): (_dt(2026, 10, 26, 8, 2), _dt(2026, 10, 26, 16, 30))}
    out, measured = wp._measured_on_fill(ev, bounds)
    assert (out, measured) == (ev, [])


def test_tier_one_observed_fire_preempts_the_fill():
    """Preempt: tier-1 already put the on-boundary AT the fire (08:01) —
    the measured first positive (08:02) is then NOT inside the gap and
    must never move the truth backwards or duplicate it."""
    ev = [(_dt(2026, 10, 25, 9, 37), "off"), (_dt(2026, 10, 26, 8, 1), "on")]
    bounds = {date(2026, 10, 26): (_dt(2026, 10, 26, 8, 2), _dt(2026, 10, 26, 16, 30))}
    out, measured = wp._measured_on_fill(ev, bounds)
    assert (out, measured) == (ev, [])


# --- plan ideals: labelled context ONLY (never state) ------------------------

def test_plan_marks_attach_to_the_containing_row_and_drop_elsewhere():
    plan = [("night", _dt(2026, 10, 25, 16, 29), _dt(2026, 10, 26, 8, 2),
             {"sunless": 0}),
            ("day", _dt(2026, 10, 26, 8, 2), _dt(2026, 10, 26, 16, 30),
             {"day": "2026-10-26", "running": False})]
    wp._plan_marks_into(plan, [(_dt(2026, 10, 26, 7, 44), "08:44"),
                               (_dt(2026, 10, 27, 9, 0), "09:00")])
    assert plan[0][3]["plan_marks"] == [{"at": "2026-10-26T07:44+00:00",
                                         "time": "08:44"}]
    assert "plan_marks" not in plan[1][3]   # outside every row → dropped
    assert "plan_marks" not in plan[1][3] and plan[0][3]["sunless"] == 0


def test_plan_marks_absence_leaves_rows_untouched():
    plan = [("day", _dt(2026, 10, 26, 8, 2), _dt(2026, 10, 26, 16, 30), {})]
    wp._plan_marks_into(plan, [])
    assert plan[0][3] == {}


# --- fix round (review b8776ee): TRUE tier-1 dominance ------------------------

def test_tier_one_fire_wins_over_an_earlier_measured_step():
    """Probe case (review): measured first-positive M=08:02 sits STRICTLY
    inside the off→on gap AND the on-transition is the write-confirmed fire
    F=09:01 (blind journal — the device fired across the unreachable window).
    Tier-1 else tier-2 means tier-1 wins UNCONDITIONALLY: the fill must not
    displace the fire instant (sunny-blackout-morning guarantee for c5)."""
    ev = [(_dt(2026, 10, 25, 9, 37), "off"),    # last observed off
          (_dt(2026, 10, 26, 9, 1), "on")]      # the tier-1 fire (blind journal)
    bounds = {date(2026, 10, 26): (_dt(2026, 10, 26, 8, 2),
                                   _dt(2026, 10, 26, 16, 30))}
    out, measured = wp._measured_on_fill(ev, bounds, [_dt(2026, 10, 26, 9, 1)])
    assert measured == []                       # no fill — tier-1 owns the gap
    assert out == ev                            # boundary = 09:01 (tier-1), not 08:02
    # without tier-1 the same gap DOES fill (the tier-2 fallback stays intact)
    out2, measured2 = wp._measured_on_fill(ev, bounds)
    assert measured2 == [_dt(2026, 10, 26, 8, 2)]
    assert out2[1] == (_dt(2026, 10, 26, 8, 2), "on")


def test_measured_step_equal_to_the_gap_edges_never_fills():
    """Equality edges (strictly-inside contract, pinned per review): M == t0
    (the off instant) and M == t1 (the on instant) lie OUTSIDE the gap."""
    bounds = {date(2026, 10, 26): (_dt(2026, 10, 26, 8, 2),
                                   _dt(2026, 10, 26, 16, 30))}
    # M == t0: the day starts exactly at the off evidence instant
    ev_t0 = [(_dt(2026, 10, 26, 8, 2), "off"), (_dt(2026, 10, 26, 9, 0), "on")]
    out0, m0 = wp._measured_on_fill(ev_t0, bounds)
    assert (out0, m0) == (ev_t0, [])
    # M == t1: the observed on sits exactly at the first positive sample
    ev_t1 = [(_dt(2026, 10, 25, 9, 37), "off"), (_dt(2026, 10, 26, 8, 2), "on")]
    out1, m1 = wp._measured_on_fill(ev_t1, bounds)
    assert (out1, m1) == (ev_t1, [])
