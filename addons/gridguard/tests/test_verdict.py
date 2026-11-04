from dataclasses import replace
from datetime import datetime
import pytest
from gridguard.config import load_config
from gridguard.verdict import (Phase, corrected_min_cell_v, current_phase,
                               deep_floor_hit, off_solar_ok, reserve_tier,
                               voltage_floor_hit)

GOOD = {"TZ": "Europe/Madrid", "WINDOW_START": "09:00", "WINDOW_END": "22:00",
        "EVE_START": "20:00", "RESCUE_AT": "04:00", "OPENSEARCH_URL": "http://x",
        "BMS_INDEX": "i", "EWCLOUD_EMAIL": "a", "EWCLOUD_PASS": "b",
        "SW_MAIN_DEVICEID": "d", "SW_MAIN_DEVICEKEY": "k", "SW_MAIN_IP": "1.2.3.4",
        "NTFY_WEBHOOK": "https://n/t"}
cfg = load_config(GOOD)
D = lambda h, m: datetime(2026, 10, 18, h, m)

def test_phase_matrix():
    assert current_phase(D(3, 0), cfg, False, False) is Phase.NIGHT
    assert current_phase(D(8, 59), cfg, False, False) is Phase.NIGHT
    assert current_phase(D(9, 0), cfg, False, False) is Phase.MORNING
    assert current_phase(D(14, 0), cfg, False, False) is Phase.MORNING   # deviation1: brief bug, see task-4-report (R3: no time limit)
    assert current_phase(D(14, 0), cfg, True, False) is Phase.DAY_HOLD
    assert current_phase(D(9, 30), cfg, True, False) is Phase.DAY_HOLD
    assert current_phase(D(20, 0), cfg, True, False) is Phase.EVE_PLAN
    assert current_phase(D(21, 54), cfg, True, False) is Phase.EVE_PLAN
    assert current_phase(D(21, 55), cfg, True, False) is Phase.FINAL
    assert current_phase(D(22, 0), cfg, True, False) is Phase.NIGHT
    assert current_phase(D(3, 0), cfg, False, True) is Phase.MORNING   # early WLAN return

def test_deep_floor():
    assert deep_floor_hit(2.89, cfg) is True
    assert deep_floor_hit(2.90, cfg) is True          # inclusive
    assert deep_floor_hit(2.91, cfg) is False
    assert deep_floor_hit(None, cfg) is False


def test_off_solar_matrix():
    assert off_solar_ok(10.0, [10.0, 12.5], cfg) is True      # inclusive remaining + hold met
    assert off_solar_ok(9.9, [10.0, 12.0], cfg) is False      # remaining below
    assert off_solar_ok(12.0, [9.9, 12.0], cfg) is False      # one dip below solar_off_a
    assert off_solar_ok(12.0, [], cfg) is False               # empty window
    assert off_solar_ok(None, [15.0], cfg) is False           # unknown → no grid-off (R8')
    assert off_solar_ok(12.0, [15.0, None], cfg) is False     # unknown sample blocks grid-off

# §13 v7 RETIRED: test_off_soc_matrix — off_soc_ok (the static SOC gate) is gone;
# the day drains above the floor, the reserve returns later as a LEARNED value (L1-L3).

# v3 R3'': test_project_remaining (fixed night-load trajectory) — SUPERSEDED by
# learn.t_fire; pins live in tests/test_learn.py::test_t_fire_math.


# --- L2 wave 1 (owner ruling 2026-10-25, option 1: voltage-only floor) -------
def test_corrected_min_cell_v_sign_convention():
    """Ingest sign: current > 0 = CHARGING (+chg/−dsg). The correction removes
    the charge over-voltage and adds back the discharge sag — verified against
    the live 2026-10-25 data: loaded 3.234 V at −7 A → corrected 3.269 V."""
    cfg = load_config(GOOD)
    # discharge: sag is ADDED BACK → corrected is HIGHER than measured
    assert corrected_min_cell_v(3.234, -7.0, cfg) == pytest.approx(
        3.234 + 7.0 * cfg.r_cell_ohm)
    # charge: over-voltage is REMOVED → corrected is LOWER
    assert corrected_min_cell_v(3.35, 16.0, cfg) == pytest.approx(
        3.35 - 16.0 * cfg.r_cell_ohm)
    # unknown input → None (never guess a floor)
    assert corrected_min_cell_v(None, -7.0, cfg) is None
    assert corrected_min_cell_v(3.2, None, cfg) is None


def test_voltage_floor_hit_is_the_only_capacity_trigger():
    """Owner ruling: the BMS counter is display-only; the corrected worst-cell
    voltage owns the floor. Deep backstop (2.90 V) stays independent."""
    cfg = load_config(GOOD)
    # healthy rested-equivalent band → NO hit (counter may say 2 % — irrelevant)
    assert voltage_floor_hit(3.269, 0.0, cfg) is False
    # real empty: corrected ≤ TRUE_FLOOR_V → hit
    assert voltage_floor_hit(3.14, 0.0, cfg) is True
    # sag illusion: measured 3.17 V but −10 A discharge → corrected 3.22 → no hit
    assert voltage_floor_hit(3.17, -10.0, cfg) is False
    # unknown data → conservative True (mirrors the retired ah_tief shape)
    assert voltage_floor_hit(None, 0.0, cfg) is True


# --- owner ruling 2026-10-25 (part 3): weather-conditional RESERVE TIERS -------
# Pure trigger logic over the rolling outlook.days window (weather.py l3
# writer). Instrument carve-out (owner, binding): tier comparisons use BMS SOC
# — the mid-band tier gates compute in BMS SOC percent, the DEEP/Emergency
# floors stay corrected cell voltage (unchanged).
NEED_WH = 2000.0            # learned CLEAN night need (wh) — eve's own median
L1_FACTOR = 0.5             # L1 yield factor: kWh into the battery per kWh/m²
EQ_RAD_J = NEED_WH * 3600.0 / L1_FACTOR   # expected == need exactly (14.4e6 J)


def _day(**kw):
    """One outlook day in the daily_outlook shape (weather.py writer)."""
    base = {"date": "2026-10-19", "weather_code": 3, "gust_max_kmh": 40.0,
            "precip_sum_mm": 0.0, "radiation_sum_j": 14e6}
    return {**base, **kw}


def test_reserve_tier2_weather_code_boundary():
    # TIER2_WMO_CODE (default 95 = WMO thunderstorm) — policy threshold, INCLUSIVE
    assert reserve_tier([_day(weather_code=94)], cfg) == (0, [])
    tier, trig = reserve_tier([_day(weather_code=95)], cfg)
    assert tier == 50 and any(t.startswith("weather_code") for t in trig)
    assert reserve_tier([_day(weather_code=99)], cfg)[0] == 50


def test_reserve_tier2_gust_boundary():
    # TIER2_GUST_KMH (default 75.0) — strictly GREATER than the knob fires
    assert reserve_tier([_day(gust_max_kmh=75.0)], cfg) == (0, [])
    tier, trig = reserve_tier([_day(gust_max_kmh=75.1)], cfg)
    assert tier == 50 and any(t.startswith("gust") for t in trig)


def test_reserve_tier2_rain_intensity_boundary():
    # Owner ruling 2026-11-03 (METRIC SWITCH): the severe rain leg reads the
    # day's MAX HOURLY intensity (mm/h), NOT the daily sum —
    # TIER2_INTENSITY_MMH (default 25.0, DWD-anchored extreme class) fires
    # AT the knob (>=).
    assert reserve_tier([_day(intensity_max_mmh=24.9)], cfg) == (0, [])
    tier, trig = reserve_tier([_day(intensity_max_mmh=25.0)], cfg)
    assert tier == 50 and any(t.startswith("rain_intensity") for t in trig)


def test_reserve_tier2_daily_sum_no_longer_fires_the_rain_leg():
    # The puddle is dead (owner: "extreme weather, not the threat of a
    # puddle"): a big DAILY sum spread over the day (low hourly rate) must
    # NOT raise the severe tier —26.6 mm on2026-11-01 was such a day.
    assert reserve_tier([_day(precip_sum_mm=60.0, intensity_max_mmh=4.0)],
                        cfg) == (0, [])
    # unknown intensity → honest unknown (the daily sum claims nothing)
    assert reserve_tier([_day(precip_sum_mm=60.0)], cfg) == (0, [])


def test_reserve_tier2_only_counts_days_inside_the_lookahead_window():
    # TIER_LOOKAHEAD_DAYS=3: a severe day on position 4 lies OUTSIDE the window
    assert cfg.tier_lookahead_days == 3
    days = [_day(date=f"2026-10-{19 + i}") for i in range(3)]
    days.append(_day(date="2026-10-22", weather_code=95))
    assert reserve_tier(days, cfg)[0] == 0


def test_reserve_tier1_fires_on_two_consecutive_low_solar_days():
    # owner correction 2026-10-25: "low solar" = the weather API's OWN radiation
    # below the SAME reference the weather card shows (WEATHER_MIN_J) — 10e6 J
    # (2.78 kWh/m²) < 15e6 J reference = a low day; two IN A ROW → tier 30
    rc = replace(cfg, weather_min_j=15e6)
    days = [_day(radiation_sum_j=10e6),
            _day(date="2026-10-20", radiation_sum_j=10e6)]
    tier, trig = reserve_tier(days, rc)
    assert tier == 30 and any(t.startswith("2 of 2 outlook days below the solar reference")
                              for t in trig)
    # boundary: radiation EXACTLY at the reference is not "below" it (strict <)
    assert reserve_tier([_day(radiation_sum_j=15e6),
                         _day(date="2026-10-20", radiation_sum_j=15e6)], rc)[0] == 0


def test_reserve_tier1_needs_at_least_two_consecutive_low_solar_days():
    rc = replace(cfg, weather_min_j=15e6)
    low, good = dict(radiation_sum_j=10e6), dict(radiation_sum_j=20e6)
    # ONE low-solar day only → no tier
    assert reserve_tier([_day(**low),
                         _day(date="2026-10-20", **good),
                         _day(date="2026-10-21", **good)], rc)[0] == 0
    # two low days SPLIT by a good day are not CONSECUTIVE → no tier
    assert reserve_tier([_day(**low),
                         _day(date="2026-10-20", **good),
                         _day(date="2026-10-21", **low)], rc)[0] == 0


@pytest.mark.parametrize("reference,rads", [
    (15e6, [None, None]),               # unknown outlook radiation per day
    (None, [10e6, 10e6]),               # reference disabled/unknown → no claim
])
def test_reserve_tier1_cannot_fire_with_unknown_inputs(reference, rads):
    # honest unknown → no claim ever (the card and the trigger stay silent)
    rc = replace(cfg, weather_min_j=reference)
    days = [_day(radiation_sum_j=rads[0]),
            _day(date="2026-10-20", radiation_sum_j=rads[1])]
    assert reserve_tier(days, rc) == (0, [])


def test_reserve_tier2_outranks_tier1_max_wins():
    days = [_day(weather_code=95, radiation_sum_j=10e6),
            _day(date="2026-10-20", weather_code=96, radiation_sum_j=10e6)]
    tier, trig = reserve_tier(days, replace(cfg, weather_min_j=15e6))
    assert tier == 50                      # tier 50 > 30 — the max wins
    assert any(t.startswith("weather_code") for t in trig)
    assert any("below the solar reference" in t for t in trig)   # both reasons


def test_reserve_tier_empty_or_short_outlook_is_zero():
    assert reserve_tier([], cfg) == (0, [])
    assert reserve_tier(None, cfg) == (0, [])
    # short window: a single low-solar day cannot reach TIER1_DAYS=2
    assert reserve_tier([_day(radiation_sum_j=10e6)],
                           replace(cfg, weather_min_j=15e6))[0] == 0
    # junk/None entries are tolerated, never a guessed trigger
    assert reserve_tier([None, "garbage"], cfg)[0] == 0

