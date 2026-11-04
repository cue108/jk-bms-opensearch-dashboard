"""GridGuard learners — spec v4 §3/§4: ENERGY, not amperes; buckets never span
regimes; only CLEAN feeds the trend. `night_need_wh` = median Wh of CLEAN night
buckets → the evening decision's need figure. The v3 empirical learner below
(array sun-run edges + sufficiency) feeds the night WINDOW for that integration.
Owner ruling 2026-10-22: energy is THE prediction unit — the ampere-based
night load (and the dead `t_fire` amperes helper) were removed as misleading."""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import tzinfo
from typing import Iterable


@dataclass(frozen=True)
class NightProfile:
    array_onset_h: float | None     # local hour-of-day, learned morning ingress
    array_offset_h: float | None    # local hour-of-day, learned evening
    samples_used: int               # qualifying night discharge samples
    sufficient: bool                # array_onset_h not None AND samples_used >=60
    # owner ruling 2026-10-22: ENERGY is the prediction unit, amperes never are
    # — the former night_load_a (median A) was removed as misleading; the
    # learned figure is the integrated night ENERGY (Wh) in `night_need_wh`.


def _edge_hour(order: Iterable[int], night_hours: set[int],
               pos_median: dict[int, float | None],
               neg_median: dict[int, float | None],
               solar_noise_a: float) -> float | None:
    """Spec R3'' steps 6/7, two-tier: prefer a POS-qualified hour (sun ingress surge),
    else fall back to the first/last not-in-night hour that has any discharge data.
    `order` is ascending for the onset, descending for the offset."""
    for tier in (1, 2):
        for h in order:
            if h in night_hours:
                continue
            if tier == 1:
                if pos_median[h] is not None and pos_median[h] >= 2 * solar_noise_a:
                    return float(h)
            else:
                if neg_median[h] is not None:
                    return float(h)
    return None


def learn_profile(samples, tz: tzinfo, solar_noise_a: float,
                  hist_days: int) -> NightProfile:
    """LOCKED algorithm (spec R3'' — the ampere median was dropped by the owner
    energy-unit ruling2026-10-22; edges + sufficiency unchanged). `hist_days`
    documents the rolling window the caller fetched; no exceptions on
    empty/extreme input — everything degrades to None/`sufficient=False`."""
    # 1) bucket every sample's current by LOCAL hour-of-day; None currents are DROPPED
    buckets: dict[int, list[float]] = {h: [] for h in range(24)}
    for s in samples:
        c = s.pack_current
        if c is None:
            continue
        ts = s.ts
        if ts.tzinfo is not None:
            ts = ts.astimezone(tz)      # aware → local wall time; naive used as-is
        buckets[ts.hour].append(c)

    # 2/3) NULLLINIE-AUSSCHLUSS (owner rule): |c| <= solar_noise_a NEVER enters a
    # statistic — grid-on idle hours contribute nothing to night load or sun-run.
    neg_median: dict[int, float | None] = {}
    pos_median: dict[int, float | None] = {}
    for h in range(24):
        negs = [c for c in buckets[h] if c < -solar_noise_a]
        poss = [c for c in buckets[h] if c > solar_noise_a]
        neg_median[h] = statistics.median(negs) if negs else None
        pos_median[h] = statistics.median(poss) if poss else None

    # 4) night hours = hours discharging at >= 60% of the PEAK learned discharge.
    # (Rule pinned on |current| magnitudes — same convention as step 5's store; the
    # signed reading would select the WEAKEST hours and can never match the fixture.)
    neg_vals = [v for v in neg_median.values() if v is not None]
    night_max = max((-v for v in neg_vals), default=None)      # None-aware peak (A)
    night_hours: set[int] = set()
    if night_max is not None:
        night_hours = {h for h in range(24)
                       if neg_median[h] is not None
                       and -neg_median[h] >= 0.6 * night_max}

    # 5) qualifying night discharge samples (count only — owner ruling
    #    2026-10-22: the learned PREDICTION figure is integrated ENERGY, never
    #    an ampere value; the median-A night load was removed as misleading)
    night_negs = [c for h in night_hours for c in buckets[h] if c < -solar_noise_a]
    samples_used = len(night_negs)

    # 6/7) learned array sun-run: morning ingress, evening offset (two-tier)
    onset = _edge_hour(range(4, 15), night_hours, pos_median, neg_median, solar_noise_a)
    offset = _edge_hour(range(23, 13, -1), night_hours, pos_median, neg_median,
                        solar_noise_a)

    # 8) sufficiency gate (feeds the planner's number-safe branch)
    sufficient = onset is not None and samples_used >= 60
    return NightProfile(onset, offset, samples_used, sufficient)



def night_need_wh(buckets: list[dict], max_nights: int = 14,
                  min_clean: int = 1) -> float | None:
    """Spec v4 §3/§4: the night need = median Wh over the CLEAN buckets among
    the LAST `max_nights` entries of the chronologically ordered `buckets`
    (`BmsSource.night_buckets` output). Grid-on minutes already contribute
    exactly 0 Wh, but a half-run night DISTORTS the need (too little measured),
    so quality classes matter: MIXED/GRID_ON/SHORT/OPEN are excluded from the
    median ("only CLEAN feeds the trend"). None while the window holds no CLEAN
    bucket — owner goal ruling (2026-10-20, "maximize solar use"): ONE full
    measured CLEAN night is enough evidence (night fluctuations are minimal;
    the safety factor + the cell-volt floor carry the buffer). `min_clean`
    stays a knob to raise the moment the measured data shows real variance."""
    recent = buckets[-max_nights:] if max_nights > 0 else []
    clean = [b["wh"] for b in recent if b.get("status") == "CLEAN"]
    if len(clean) < min_clean:
        return None
    return float(statistics.median(clean))
