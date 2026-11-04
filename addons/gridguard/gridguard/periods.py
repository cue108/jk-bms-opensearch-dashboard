"""Period-boundary rule (owner2026-10-21) — the ONE current curve decides.

The solar day is read from `pack.current` alone: it begins at the first
positive excursion (charging) at/after astronomical sunrise, and it ends at
the last positive excursion before sunset. The grid state has NO say in the
boundary. Site threshold (owner2026-10-22): **3 A** — below that is NOT solar
day (`frontend GG_FLOW_A` and addon `FLOW_A`, default 3.0 each, are the same
value; change both in the same commit). Raised from 2 A because of a measured
switching transient:
the08:01 grid-ON fired a sister-pack BALANCING spike peaking at **2.676 A**
(06:02Z, decay ~6 min) — a >2.0 AND >2.5 A step that would open a FALSE
solar day if it ever lands after sunrise (e.g. at the morning drain switch).
3.0 clears the measured peak; cost on a real dawn ≈ 4 min (09:09 → 09:13
observed2026-10-21).

Evidence (host50 live curve,2026-10-21): charge ~+10…+25A from13:21 to~17:44,
fall to ~-12A (discharge), then a FLAT ZERO across the whole night (22:08-
06:53), rise again from~09:03. The night reads zero, not negative, so there is
no grid state that can flip the dawn into the negative range — the legacy
`not _grid_on(t)` guard protected against a hazard this site does not have.

Two implementations, ONE rule: this module is the executable spec (the L1/L2
learning tasks consume it); `gridguard/web_payload.py` mirrors it inline (the
payload block moved verbatim out of `frontend/server.py`, which carries no copy
anymore — single source lives in this package). Change the rule → change BOTH
in the same commit.
"""
from __future__ import annotations

from datetime import date, datetime


def solar_day_bounds(samples, flow_a: float, sunrise: datetime,
                     sunset: datetime | None = None):
    """First positive excursion at/after `sunrise` → last positive excursion.

    samples: iterable of (datetime, current_in_A or None).
    flow_a:  the charging threshold — current at/above it counts as charging.
    sunrise: the astronomical sunrise (local, tz-aware) of the day in question.
    sunset:  the astronomical sunset (local, tz-aware); default is end of day.

    Returns (start, end) or None — a day that never lifts above the threshold
    has NO solar day (the winter edge case: no fallback window, no invented
    day).

    Owner finality rule2026-10-23: for an ONGOING day (started, calendar
    sunset not yet reached) the returned end is PROVISIONAL — mid-day cloud
    gaps must never close a started day; its length is final only at/after
    the calendar sunset. Consumers must not treat a provisional end as a
    completed day (history rows mark it running; the yield learning waits
    for the sunset)."""
    if sunset is None:
        sunset = sunrise.replace(hour=23, minute=59, second=59, microsecond=999999)

    charging = [t for t, cur in samples
                if cur is not None and sunrise <= t <= sunset and cur >= flow_a]
    if not charging:
        return None
    return charging[0], charging[-1]


def night_windows(
    day_bounds: dict[date, tuple[datetime, datetime]],
) -> dict[date, tuple[datetime, datetime]]:
    """Solar-NIGHT windows (owner ruling2026-10-22: one concept everywhere).

    A night is labeled by the MORNING date `d` and spans
    (previous measured day's END, day d's START) — the definition mirrors the
    frontend history rows. Sunless calendar days produce NO day pair, so they
    have no night entry of their own: the NEXT measured day's night SPANS
    them (continuous solar night). The first measured day has no preceding END
    → skipped (partial history, never guessed).
    """
    out: dict[date, tuple[datetime, datetime]] = {}
    dates = sorted(day_bounds)
    for prev, d in zip(dates, dates[1:]):
        out[d] = (day_bounds[prev][1], day_bounds[d][0])
    return out
