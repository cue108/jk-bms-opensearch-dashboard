"""Environment → typed Config. Nothing else touches env vars."""
from __future__ import annotations

import math
import os
from dataclasses import dataclass
from datetime import time as dtime
from pathlib import Path
from typing import Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class ConfigError(RuntimeError):
    pass


REQUIRED = ("TZ", "OPENSEARCH_URL", "BMS_INDEX", "EWCLOUD_EMAIL", "EWCLOUD_PASS",
            "SW_MAIN_DEVICEID", "SW_MAIN_DEVICEKEY", "SW_MAIN_IP", "NTFY_WEBHOOK")


@dataclass(frozen=True)
class Config:
    tz: ZoneInfo
    window_start: dtime
    window_end: dtime
    eve_start: dtime
    final_offset_min: int
    os_url: str
    bms_index: str
    bms_cell_fields: tuple[str, ...]
    bms_current_path: str
    wlan_iface: str
    thresh_deep: float
    true_floor_v: float             # the VOLTAGE-anchored operational floor (corrected
                                    # rested-equivalent min cell, V); BMS counter
                                    # is display-only from here on
    r_cell_ohm: float               # DC-path cell R for the IR correction — derived
                                    # from live data (35 mV @ 7 A), NOT
                                    # the app's AC-pulse figure (over-corrects ~7×)
    ah_off: float
    solar_off_a: float
    solar_hold_s: int
    solar_noise_a: float
    safety_factor: float             # evening ratio margin (available >= SAFETY × need)
    flow_a: float                 # solar-day boundary threshold (A) — mirror of
                                  # GG_FLOW_A in gridguard/web_payload.py (single source); default 3.0
    forecast_refresh_min: float   # forecast REVISION path — how
                                  # often the midday-changed open-meteo value is
                                  # re-fetched/shipped (value-id = revision chain)
    hist_days: int
    bms_remaining_path: str
    ew_email: str
    ew_pass: str
    ew_cc: str
    sw_deviceid: str
    sw_devicekey: str
    sw_ip: str
    ntfy_webhook: str
    msg_lang: str
    poll_fast_s: int
    poll_morning_s: int
    probe_night_s: int
    log_dir: Path
    dry_run: bool
    weather_lat: float | None
    weather_lon: float | None
    weather_min_j: float | None
    events_index: str                # audit event dual-write index prefix
    events_index_enabled: bool       # kill-switch: EVENTS_INDEX=0 → jsonl-only
    weather_index: bool              # forecast dual-write: WEATHER_INDEX=0 → no weather docs
    weather_index_prefix: str        # forecast index prefix (…-{YYYY.MM.DD})

    # --- weather-conditional RESERVE TIERS ---
    # POLICY constants (config doctrine: thresholds are ENV-tunable policy —
    # they may be retuned; they are NOT observations). Instrument: the
    # tier comparisons run on BMS SOC (verdict.reserve_tier carve-out) — the
    # deep/emergency floors keep corrected cell voltage, untouched below.
    tier1_days: int                  # outlook days with radiation_sum_j BELOW
                                     # the weather card's own reference
                                     # (WEATHER_MIN_J; the harvest-vs-need
                                     # variant is dead) —
                                     # COUNTED ANYWHERE in the window: any bad
                                     # day holds the mode (the UPS exists to
                                     # bridge a possible grid outage ON a bad
                                     # weather day) → the 30 % moderate tier (default 1)
    tier2_wmo_code: int              # severe 50 % tier: any outlook day with
                                     # weather_code >= this (95 = WMO thunderstorm)
    tier2_gust_kmh: float            # severe tier: day gust maximum STRICTLY above
    tier2_intensity_mmh: float       # severe tier: day's MAX HOURLY rain rate
                                     # at/above (>=), mm/h — extreme-weather
                                     # intensity, not accumulated volume; default 25 =
                                     # DWD's upper warning class. Replaces the
                                     # daily-sum knob TIER2_PRECIP_MM (retired)
    tier_lookahead_days: int         # how many outlook.days entries count (default 3)
    tier_release_hyst_pct: int       # anti-flap: release needs
                                    # soc >= tier + hyst so the >= release and the
                                    # <= stop zones never overlap at an exact
                                    # integer-SOC plateau (write churn); POLICY,
                                    # env-tunable, 0 = literal exact reach

    # --- REFERENCE-LOAD model (THREE-TIER rules): a RATE (Wh/h) used only
    # for the timer projection.
    # reference_nights = the tier1 PREFERENCE WINDOW (a whole off-grid solar
    # night wins within these days — the seven-day rule) AND the deep
    # walk's block size; reference_min_clip_min = the minimum qualifying
    # grid-OFF clip length; reference_max_lookback_days = the age-unlimited
    # tier2 search bound (winter: older post-sunset values still count — the
    # value wanders with every new measurement, is never static, and tier3
    # holds the last one until new data arrives).
    reference_nights: int = 7
    reference_min_clip_min: int = 30
    reference_max_lookback_days: int = 365
    # a plan whose
    # projected grid-off span is SHORTER than this many minutes keeps the
    # GRID ON state (no release); the "grid is ON because the battery is too
    # empty → no plan may switch to OFF" rule shares the same anchor —
    # too-empty = a short projected span. Minutes.
    release_min_span_min: int = 30

    @property
    def tz_name(self) -> str:
        """IANA name of `tz` — ship_forecast builds its ZoneInfo from it."""
        return str(self.tz)


def _ptime(name: str, raw: str) -> dtime:
    try:
        return dtime.fromisoformat(raw)
    except ValueError as e:
        raise ConfigError(f"{name}: invalid time {raw!r}") from e


def load_config(env: Mapping[str, str] | None = None) -> Config:
    e = dict(os.environ if env is None else env)
    missing = [k for k in REQUIRED if not e.get(k)]
    if missing:
        raise ConfigError(f"missing required env vars: {', '.join(missing)}")
    try:
        tz = ZoneInfo(e["TZ"])
    except ZoneInfoNotFoundError as ex:
        raise ConfigError(f"TZ unknown: {e['TZ']}") from ex

    window_start = _ptime("WINDOW_START", e.get("WINDOW_START", "09:00"))
    window_end = _ptime("WINDOW_END", e.get("WINDOW_END", "22:00"))
    if not (window_start < window_end):
        raise ConfigError("WINDOW_START must be earlier than WINDOW_END (no midnight wrap)")
    eve_start = _ptime("EVE_START", e.get("EVE_START", "20:00"))
    # the ACTOR's WiFi-outage hours: inside
    # this window NO path to the actor exists — the floor functions must know.
    # NO blackout knobs — a usage-variable
    # parameter (the WiFi schedule) is NEVER env; the live link is the truth.

    def fnum(name: str, default: str) -> float:
        try:
            return float(e.get(name, default))
        except ValueError as ex:
            raise ConfigError(f"{name}: not a number") from ex

    lat_raw, lon_raw = e.get("WEATHER_LAT", "").strip(), e.get("WEATHER_LON", "").strip()
    if bool(lat_raw) != bool(lon_raw):
        raise ConfigError("WEATHER_LAT and WEATHER_LON must be set together")
    lat = float(lat_raw) if lat_raw else None
    lon = float(lon_raw) if lon_raw else None

    wj_raw = e.get("WEATHER_MIN_J", "").strip()
    wj = None if wj_raw in ("", "disabled") else fnum("WEATHER_MIN_J", wj_raw)
    # forecast safety gate: only a real positive finite number is
    # a threshold — 0/negative/nan/inf end as the conservative unset (None =
    # gate closed), never as an open gate or a "≥ nan MJ" reason.
    if wj is not None and not (math.isfinite(wj) and wj > 0):
        wj = None

    return Config(
        tz=tz, window_start=window_start, window_end=window_end, eve_start=eve_start,
        final_offset_min=int(e.get("FINAL_OFFSET_MIN", "5")),
        os_url=e["OPENSEARCH_URL"].rstrip("/"), bms_index=e["BMS_INDEX"],
        bms_cell_fields=tuple(x for x in e.get("BMS_CELL_FIELDS", "v1,v2,v3,v4,v5,v6,v7,v8").split(",") if x),
        bms_current_path=e.get("BMS_CURRENT_PATH", "pack.current"),
        wlan_iface=e.get("WLAN_IFACE", ""),   # "" = auto-discover the (single) dongle — reach.discover_wlan
        thresh_deep=fnum("THRESH_DEEP", "2.90"),
        true_floor_v=fnum("TRUE_FLOOR_V", "3.15"),   # voltage-anchored floor
        r_cell_ohm=fnum("R_CELL_OHM", "0.005"),      # DC-path, live-derived
        ah_off=fnum("AH_OFF", "10"),
        solar_off_a=fnum("SOLAR_OFF_A", "10"),
        solar_hold_s=int(e.get("SOLAR_HOLD_S", "300")),
        solar_noise_a=fnum("SOLAR_NOISE_A", "2.0"),
        safety_factor=fnum("SAFETY_FACTOR", "1.0"),   # the ×1.2 used to ride in
        # the crossing RATE → the return fired at ~29 %
        # SOC (wasted harvest room). Default now the RAW learned need — the
        # battery carries whenever the median fits; the timer fires only when
        # the night proves worse. Insurance = the accepted physical backstop
        # (0 % BMS ≈ real end 3 V/cell ≈ 12 V pack). Env can still override.
        flow_a=fnum("FLOW_A", "3.0"),   # mirror of GG_FLOW_A in gridguard/web_payload.py —
        # change BOTH together (the solar-day rule, periods.py docstring)
        forecast_refresh_min=fnum("FORECAST_REFRESH_MIN", "30"),  # forecast revision cadence (min)
        hist_days=int(e.get("HIST_DAYS", "7")),      # rolling learning window
        bms_remaining_path=e.get("BMS_REMAINING_AH_PATH", "soc.remaining_ah"),
        ew_email=e["EWCLOUD_EMAIL"], ew_pass=e["EWCLOUD_PASS"], ew_cc=e.get("EWCLOUD_CC", "+34"),
        sw_deviceid=e["SW_MAIN_DEVICEID"], sw_devicekey=e["SW_MAIN_DEVICEKEY"], sw_ip=e["SW_MAIN_IP"],
        ntfy_webhook=e["NTFY_WEBHOOK"], msg_lang=e.get("MSG_LANG", "en"),
        poll_fast_s=int(e.get("POLL_FAST_S", "300")), poll_morning_s=int(e.get("POLL_MORNING_S", "600")),
        probe_night_s=int(e.get("PROBE_NIGHT_S", "1800")),
        log_dir=Path(e.get("LOG_DIR", "/data")),
        dry_run=e.get("DRY_RUN", "0") == "1",
        weather_lat=lat, weather_lon=lon, weather_min_j=wj,
        events_index=e.get("EVENTS_INDEX_PREFIX", "gridguard-events"),
        events_index_enabled=e.get("EVENTS_INDEX", "1") != "0",
        weather_index=e.get("WEATHER_INDEX", "1") != "0",
        weather_index_prefix=e.get("WEATHER_INDEX_PREFIX", "gridguard-weather"),
        # reserve tiers: POLICY thresholds, env-tunable
        tier1_days=int(e.get("TIER1_DAYS", "1")),
        tier2_wmo_code=int(e.get("TIER2_WMO_CODE", "95")),
        tier2_gust_kmh=fnum("TIER2_GUST_KMH", "75"),
        tier2_intensity_mmh=fnum("TIER2_INTENSITY_MMH", "25"),   # peak hourly rate (mm/h)
        tier_lookahead_days=int(e.get("TIER_LOOKAHEAD_DAYS", "3")),
        # release needs tier + hyst (default 5)
        tier_release_hyst_pct=int(e.get("TIER_RELEASE_HYST_PCT", "5")),
        # reference-load model: tier1
        # preference window (= block size) + clip floor + the deep tier2 cap
        reference_nights=int(e.get("REF_NIGHTS", "7")),
        reference_min_clip_min=int(e.get("REF_MIN_CLIP_MIN", "30")),
        reference_max_lookback_days=int(e.get("REF_MAX_LOOKBACK_DAYS", "365")),
        release_min_span_min=int(e.get("RELEASE_MIN_SPAN_MIN", "30")),
    )
