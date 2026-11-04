"""v3 empirical learner — spec R3'' algorithm pins (night load, sun-run, t_fire)."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from gridguard.bms import BmsSample
from gridguard.learn import NightProfile, learn_profile, night_need_wh

TZ = ZoneInfo("Europe/Madrid")
NOISE = 2.0          # SOLAR_NOISE_A default — the NULLLINIE band
HIST_DAYS = 7
BASE = datetime(2026, 10, 4)          # two full weeks of local-naive stamps


def _s(ts: datetime, current: float) -> BmsSample:
    return BmsSample(ts=ts, min_cell_v=3.2, pack_current=current,
                     remaining_ah=90.0, soc_pct=70.0)


def build_samples(days: int = 14, zeros: bool = True) -> list[BmsSample]:
    """Two weeks, LOCAL-naive stamps:
    nights 22:00–06:00 → −12±1 A (hour buckets 22..5),
    days 08:00–19:59 → +5..+15 A (hour buckets 8..19),
    `zeros=True` adds a fat grid-on band (0.0 / ±0.3 A) inside 23:00–01:00 that the
    NULLLINIE-AUSSCHLUSS (owner rule) must keep out of every statistic."""
    out: list[BmsSample] = []
    for d in range(days):
        day = BASE + timedelta(days=d)
        for h in (22, 23, 0, 1, 2, 3, 4, 5):
            for m in (0, 15, 30, 45):
                out.append(_s(day.replace(hour=h, minute=m),
                              -12.0 + ((d + m // 15) % 3) - 1))
        if zeros:
            for h in (23, 0, 1):
                for m in (5, 10, 20, 25, 35, 40, 50, 55):
                    out.append(_s(day.replace(hour=h, minute=m),
                                  [0.0, 0.3, -0.3][(d + m) % 3]))
        for h in range(8, 20):                 # exactly hours 8..19 carry sun
            for m in (0, 30):
                out.append(_s(day.replace(hour=h, minute=m),
                              5.0 + (h % 7) * 1.5 + (d % 2)))
    return out


def test_profile_edges_and_sufficient():
    # owner energy-unit ruling2026-10-22: no ampere figure in the profile —
    # the prediction unit is the integrated night ENERGY (night_need_wh)
    p = learn_profile(build_samples(), TZ, NOISE, HIST_DAYS)
    # unambiguous fixture: first pos-qualified morning hour = 8, last evening = 19
    assert p.array_onset_h == 8.0
    assert p.array_offset_h == 19.0
    assert p.samples_used >= 60
    assert p.sufficient is True
    assert not hasattr(p, "night_load_a")


def test_nullline_ausschluss_zeros_change_nothing():
    """Owner rule: |current| <= solar_noise_a samples never enter any statistic."""
    a = learn_profile(build_samples(zeros=True), TZ, NOISE, HIST_DAYS)
    b = learn_profile(build_samples(zeros=False), TZ, NOISE, HIST_DAYS)
    assert len(build_samples(zeros=True)) > len(build_samples(zeros=False))  # band present
    assert a.samples_used == b.samples_used       # zero band contributes nothing


def test_insufficient_with_too_few_samples():
    few = [_s(BASE.replace(hour=23, minute=5) + timedelta(days=d), -12.0)
           for d in range(10)]
    p = learn_profile(few, TZ, NOISE, HIST_DAYS)
    assert p.samples_used == 10
    assert p.sufficient is False                 # < 60 samples, whatever the data


def test_aware_timestamps_learn_the_same_local_profile():
    naive = build_samples(zeros=False)
    aware = [replace(s, ts=s.ts.replace(tzinfo=TZ).astimezone(timezone.utc))
             for s in naive]
    assert learn_profile(aware, TZ, NOISE, HIST_DAYS) == \
        learn_profile(naive, TZ, NOISE, HIST_DAYS)


def test_empty_and_flat_input_never_raise():
    empty = learn_profile([], TZ, NOISE, HIST_DAYS)
    assert empty == NightProfile(None, None, 0, False)
    flat = learn_profile([_s(BASE + timedelta(hours=h), 0.0) for h in range(24)],
                         TZ, NOISE, HIST_DAYS)
    assert flat.samples_used == 0
    assert flat.array_onset_h is None and flat.array_offset_h is None
    assert flat.sufficient is False


# --- v4 round A: night_need_wh (spec v4 §3/§4 — CLEAN-only median trend feed) ---
def _nb(night: str, wh: float, status: str = "CLEAN") -> dict:
    return {"night": night, "wh": wh, "minutes": 60, "on_minutes": 0,
            "status": status}


def test_night_need_median_of_clean_only():
    """MIXED/GRID_ON/SHORT/OPEN never enter the median — a fixture where every
    wrongly-included status would move the result away from 300."""
    buckets = [_nb("2026-10-01", 100.0),
               _nb("2026-10-02", 10000.0, "MIXED"),
               _nb("2026-10-03", 300.0),
               _nb("2026-10-04", 500.0),
               _nb("2026-10-05", 88888.0, "SHORT"),
               _nb("2026-10-06", 77777.0, "OPEN"),
               _nb("2026-10-07", 99999.0, "GRID_ON")]
    assert night_need_wh(buckets) == pytest.approx(300.0)   # median(100,300,500)


def test_night_need_requires_at_least_one_clean():
    """Owner goal ruling (2026-10-20, "maximize solar use"): ONE full measured
    CLEAN night is enough evidence — night-to-night fluctuations are minimal
    and the safety layer (factor + cell floor) carries the buffer."""
    assert night_need_wh([]) is None
    assert night_need_wh([_nb("2026-10-01", 100.0, "MIXED"),
                          _nb("2026-10-02", 200.0, "GRID_ON"),
                          _nb("2026-10-03", 300.0, "SHORT")]) is None
    assert night_need_wh([_nb("2026-10-01", 100.0)]) == pytest.approx(100.0)


def test_night_need_slices_the_last_max_nights_chronologically():
    """Oldest CLEAN bucket (9999, index 0) sits OUTSIDE the last 14 — it must
    be ignored: median of the last 14 (7×100, 7×300) is 200, not 300."""
    buckets = [_nb("2026-09-30", 9999.0)]
    buckets += [_nb(f"2026-10-{d:02d}", 100.0) for d in range(1, 8)]
    buckets += [_nb(f"2026-10-{d:02d}", 300.0) for d in range(8, 15)]
    assert len(buckets) == 15
    assert night_need_wh(buckets) == pytest.approx(200.0)
    # same data, ONLY max_nights widened: the old CLEAN 9999 enters → 300
    assert night_need_wh(buckets, max_nights=15) == pytest.approx(300.0)
    assert night_need_wh(buckets, min_clean=15) is None   # only 14 CLEAN in window
