import json
from pathlib import Path

import pytest
from gridguard.config import Config, ConfigError, load_config

# Switch identity: ONE test config instead of hardcoded values (owner ruling
# 2026-11-04) — the file doubles as a worked example for repo users.
IDENT = json.loads((Path(__file__).resolve().parent / "fixtures"
                    / "switchlink-identity.json").read_text())

GOOD = {
    "TZ": "Europe/Madrid", "WINDOW_START": "09:00", "WINDOW_END": "22:00",
    "EVE_START": "20:00", "RESCUE_AT": "04:00", "NIGHT_LOAD_A": "12",
    "THRESH_EMERG": "3.00", "THRESH_OFF": "3.10", "PROJ_FLOOR": "3.05",
    "OPENSEARCH_URL": "http://127.0.0.1:9205", "BMS_INDEX": "bms-2p4s-upstairs-*",
    "EWCLOUD_EMAIL": "a@b.c", "EWCLOUD_PASS": "x", "EWCLOUD_CC": "+34",
    "SW_MAIN_DEVICEID": IDENT["SW_MAIN_DEVICEID"], "SW_MAIN_DEVICEKEY": IDENT["SW_MAIN_DEVICEKEY"],
    "SW_MAIN_IP": IDENT["SW_MAIN_IP"],
    "NTFY_WEBHOOK": "https://ntfy.example/t", "DRY_RUN": "1",
}

def test_missing_required_lists_names():
    with pytest.raises(ConfigError) as e:
        load_config({})
    assert "TZ" in str(e.value) and "SW_MAIN_DEVICEID" in str(e.value)

def test_good_env_parses_types():
    cfg = load_config(GOOD)
    assert (cfg.window_start.hour, cfg.window_start.minute) == (9, 0)
    assert (cfg.window_end.hour, cfg.window_end.minute) == (22, 0)
    assert cfg.dry_run is True
    assert cfg.weather_lat is None and cfg.weather_min_j is None
    assert cfg.bms_cell_fields == ("v1", "v2", "v3", "v4", "v5", "v6", "v7", "v8")

def test_weather_pairing():
    cfg = load_config({**GOOD, "WEATHER_MIN_J": "3000000"})
    assert cfg.weather_min_j == 3000000.0
    with pytest.raises(ConfigError):
        load_config({**GOOD, "WEATHER_LAT": "37.0"})   # lat without lon


def test_weather_min_j_nonpositive_or_nonfinite_is_unset():
    # final-review F4 (§12.1 safety gate): 0/negative/nan/inf are NO thresholds —
    # they end as the conservative unset (None = gate closed), like empty/disabled
    for raw in ("0", "-5", "nan", "inf", "-inf"):
        cfg = load_config({**GOOD, "WEATHER_MIN_J": raw})
        assert cfg.weather_min_j is None, f"WEATHER_MIN_J={raw!r} must be unset"
    # a real positive number is still a threshold
    assert load_config({**GOOD, "WEATHER_MIN_J": "8000000"}).weather_min_j == 8000000.0

def test_window_order_and_bad_time():
    with pytest.raises(ConfigError):
        load_config({**GOOD, "WINDOW_START": "22:00", "WINDOW_END": "09:00"})
    with pytest.raises(ConfigError):
        load_config({**GOOD, "EVE_START": "nope"})

def test_v2_defaults():
    cfg = load_config(GOOD)
    assert cfg.wlan_iface == ""                # §R5' owner: auto-discover, no pinned name
    assert cfg.thresh_deep == 2.90
    assert cfg.ah_off == 10.0
    assert cfg.solar_off_a == 10.0 and cfg.solar_hold_s == 300
    assert cfg.solar_noise_a == 2.0          # §13 v7: SOC_OFF_PCT retired (no static gate)
    assert cfg.bms_remaining_path == "soc.remaining_ah"

def test_v4_safety_factor_default_and_override():
    # v4 §3: SAFETY = the evening ratio's margin (120% of the learned night need)
    assert load_config(GOOD).safety_factor == 1.0            # owner ruling2026-10-22
    # (deep drain / maximum solar use): the crossing uses the RAW learned need
    assert load_config({**GOOD, "SAFETY_FACTOR": "1.5"}).safety_factor == 1.5
    assert load_config({**GOOD, "SAFETY_FACTOR": "1"}).safety_factor == 1.0   # float parse
    with pytest.raises(ConfigError):
        load_config({**GOOD, "SAFETY_FACTOR": "safe"})       # not a number

def test_flow_a_default_and_override():
    # owner boundary threshold 2026-10-22: 3.0 A (measured 2.676A balancing
    # transient must never open a solar day); mirror of frontend GG_FLOW_A
    assert load_config(GOOD).flow_a == 3.0
    assert load_config({**GOOD, "FLOW_A": "2.5"}).flow_a == 2.5

def test_v2_removed_fields_are_absent():
    cfg = load_config(GOOD)
    for gone in ("thresh_emerg", "thresh_off", "proj_floor", "proj_window_h", "charge_min_a"):
        assert not hasattr(cfg, gone)

def test_v3_fixed_constants_removed_hist_days_added():
    # R3'': the v2 fixed keys in GOOD are SUPERSEDED — parsed nowhere, attributes gone
    cfg = load_config(GOOD)
    for gone in ("rescue_at", "night_load_a"):
        assert not hasattr(cfg, gone)
    assert cfg.hist_days == 7                             # HIST_DAYS default
    assert load_config({**GOOD, "HIST_DAYS": "14"}).hist_days == 14

def test_superseded_env_keys_ignored():
    cfg = load_config({**GOOD, "THRESH_EMERG": "3.00", "THRESH_OFF": "3.10",
                       "CHARGE_MIN_A": "1.5", "PROJ_FLOOR": "3.05",
                       "RESCUE_AT": "04:00", "NIGHT_LOAD_A": "12"})
    assert not hasattr(cfg, "thresh_emerg")     # ignored, not parsed, not fatal
    assert not hasattr(cfg, "rescue_at")        # v3: unknown key, no field, no error
    assert not hasattr(cfg, "night_load_a")

def test_v2_overrides():
    cfg = load_config({**GOOD, "AH_ON": "7.5", "SOLAR_HOLD_S": "600",
                       "WLAN_IFACE": "wlan9", "BMS_REMAINING_AH_PATH": "pack.rest"})
    assert cfg.solar_hold_s == 600
    assert cfg.wlan_iface == "wlan9" and cfg.bms_remaining_path == "pack.rest"

def test_v4c_decision_index_defaults_and_killswitch():
    # spec §10b: dual-write target — prefix default, kill-switch on by default,
    # EVENTS_INDEX=0 → jsonl-only (no index traffic at all)
    cfg = load_config(GOOD)
    assert cfg.events_index == "gridguard-events"
    assert cfg.events_index_enabled is True
    off = load_config({**GOOD, "EVENTS_INDEX_PREFIX": "gridguard-x", "EVENTS_INDEX": "0"})
    assert off.events_index == "gridguard-x"
    assert off.events_index_enabled is False

def test_v4d_weather_index_defaults_and_killswitch():
    # §12.1 forecast dual-write: prefix default, ON by default (Default "1"),
    # WEATHER_INDEX=0 = kill-switch (same parse style as EVENTS_INDEX)
    cfg = load_config(GOOD)
    assert cfg.weather_index_prefix == "gridguard-weather"
    assert cfg.weather_index is True
    off = load_config({**GOOD, "WEATHER_INDEX_PREFIX": "weather-x", "WEATHER_INDEX": "0"})
    assert off.weather_index_prefix == "weather-x"
    assert off.weather_index is False
    assert load_config({**GOOD, "WEATHER_INDEX": "1"}).weather_index is True
    assert cfg.weather_min_j is None                      # UNCHANGED (Task3-Gate-Input)


def test_forecast_refresh_min_default_and_override():
    # owner2026-10-23: cadence of the forecast REVISION path (midday-changed
    # open-meteo values must land in the cluster for the L1 mapping)
    assert load_config(GOOD).forecast_refresh_min == 30.0
    assert load_config({**GOOD, "FORECAST_REFRESH_MIN": "10"}
                       ).forecast_refresh_min == 10.0


def test_voltage_floor_knobs_default_and_override():
    # owner ruling 2026-10-25: voltage-anchored floor (TRUE_FLOOR_V) + the
    # DC-path cell resistance used for the correction (R_CELL_OHM)
    cfg = load_config(GOOD)
    assert cfg.true_floor_v == 3.15 and cfg.r_cell_ohm == 0.005
    c2 = load_config({**GOOD, "TRUE_FLOOR_V": "3.18", "R_CELL_OHM": "0.004"})
    assert c2.true_floor_v == 3.18 and c2.r_cell_ohm == 0.004


def test_reserve_tier_knobs_default_and_override():
    # owner ruling 2026-10-25 (part 3): the reserve-tier thresholds are POLICY
    # constants (config doctrine: env-tunable, the owner may retune) — NOT
    # observations. Defaults: 1 below-ref day anywhere in the window → 30 %
    # (owner ruling 2026-11-09: any bad weather day holds the UPS mode —
    # COUNT, not consecutive streak); severe at
    # WMO 95 / 75 km/h gusts / 25 mm/h rain INTENSITY; 3-day look-ahead window.
    # Owner ruling 2026-11-03 (metric switch): the rain leg compares the day's
    # max HOURLY rate (mm/h, DWD-anchored extreme class) — the daily-sum knob
    # (TIER2_PRECIP_MM) is retired.
    cfg = load_config(GOOD)
    assert (cfg.tier1_days, cfg.tier2_wmo_code, cfg.tier2_gust_kmh,
            cfg.tier2_intensity_mmh, cfg.tier_lookahead_days) == (1, 95, 75.0, 25.0, 3)
    # hysteresis default: release at tier + 5 (owner ruling 2026-11-09:
    # the 30 tier releases at 35, was 30/32 with hyst 2)
    assert cfg.tier_release_hyst_pct == 5
    c2 = load_config({**GOOD, "TIER1_DAYS": "3", "TIER2_WMO_CODE": "90",
                      "TIER2_GUST_KMH": "60.5", "TIER2_INTENSITY_MMH": "12.5",
                      "TIER_LOOKAHEAD_DAYS": "5"})
    assert (c2.tier1_days, c2.tier2_wmo_code, c2.tier2_gust_kmh,
            c2.tier2_intensity_mmh, c2.tier_lookahead_days) == (3, 90, 60.5, 12.5, 5)
