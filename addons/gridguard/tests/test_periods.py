"""solar_day_bounds — the ONE current curve decides the solar day boundary.
(owner2026-10-21: no grid gating, no invented window; a day that never lifts
above the threshold has NO solar day.)
"""
from datetime import datetime, timedelta

from gridguard.periods import solar_day_bounds

def _t(h, m):
    return datetime(2026, 10, 21, h, m)

# Reference curve (host50 live, 2026-10-21):
# charge ~+10…+25A from13:21 to~17:44, fall to ~-12A (discharge), then a FLAT
# ZERO across the whole night (22:08–06:53), rise again from~09:03.
CURVE = [
    (_t(9, 3), 0.0), (_t(9, 13), 3.2), (_t(9, 23), 8.1),   # dawn rise
    (_t(13, 21), 15.0), (_t(15, 32), 14.0),                # peak
    (_t(17, 44), 10.0), (_t(18, 0), -5.0),                 # fall
    (_t(19, 57), -12.0), (_t(22, 8), 0.0),                 # night zero
    (_t(23, 59), 0.0),
]
SUNRISE = _t(8, 0)
SUNSET = _t(20, 0)


def test_solar_day_starts_at_first_positive_after_sunrise():
    bounds = solar_day_bounds(CURVE, 2.0, SUNRISE, SUNSET)
    assert bounds is not None
    start, _ = bounds
    assert start == _t(9, 13)  # first sample ≥2A at/after sunrise


def test_solar_day_ends_at_last_positive_before_sunset():
    bounds = solar_day_bounds(CURVE, 2.0, SUNRISE, SUNSET)
    _, end = bounds
    assert end == _t(17, 44)  # last sample ≥2A before sunset


def test_no_positive_in_window_returns_none():
    # all negative or zero in the window → no solar day
    curve = [(_t(10, 0), -5.0), (_t(12, 0), 0.0), (_t(15, 0), -12.0)]
    assert solar_day_bounds(curve, 2.0, SUNRISE, SUNSET) is None


def test_empty_samples_returns_none():
    assert solar_day_bounds([], 2.0, SUNRISE, SUNSET) is None


def test_positive_outside_sunrise_sunset_ignored():
    # positive before sunrise → not counted (no solar day)
    curve = [(_t(6, 0), 5.0), _t(7, 0)]  # malformed: second item not a tuple
    curve = [(_t(6, 0), 5.0)]  # only pre-sunrise positive
    assert solar_day_bounds(curve, 2.0, SUNRISE, SUNSET) is None


def test_sunset_none_defaults_to_end_of_day():
    # sunset=None → default is end of solar day (midnight-ish)
    curve = [(_t(19, 57), -12.0), (_t(23, 0), 3.0), (_t(23, 59), 5.0)]
    bounds = solar_day_bounds(curve, 2.0, SUNRISE, None)
    assert bounds is not None
    _, end = bounds
    assert end == _t(23, 59)  # default sunset covers the whole day


# --- solar-night windows (owner ruling2026-10-22: concept applied throughout) --
from datetime import datetime as _dt
from gridguard.periods import night_windows


def _db(d, sh, sm, eh, em):
    return (_dt(2026, 10, d, sh, sm), _dt(2026, 10, d, eh, em))


def test_night_windows_chain_measured_days():
    db = {1: _db(1, 9, 0, 18, 30), 2: _db(2, 9, 5, 18, 25),
          3: _db(3, 9, 10, 18, 20)}
    nw = night_windows(db)
    # night labeled by morning date = (prev END, this START)
    assert nw[2] == (_dt(2026, 10, 1, 18, 30), _dt(2026, 10, 2, 9, 5))
    assert nw[3] == (_dt(2026, 10, 2, 18, 25), _dt(2026, 10, 3, 9, 10))
    assert 1 not in nw          # first measured day: no preceding END


def test_night_windows_span_sunless_days_continuous():
    # calendar day 2 has NO solar day (too little light) → the night after
    # day 1's end runs THROUGH day 2 and closes at day 3's start
    db = {1: _db(1, 9, 0, 18, 30), 3: _db(3, 9, 10, 18, 20)}
    nw = night_windows(db)
    assert list(nw) == [3]
    assert nw[3] == (_dt(2026, 10, 1, 18, 30), _dt(2026, 10, 3, 9, 10))


def test_night_windows_empty_and_single_day():
    assert night_windows({}) == {}
    assert night_windows({5: _db(5, 9, 0, 18, 0)}) == {}
