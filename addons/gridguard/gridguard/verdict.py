"""Pure decision core — no I/O, no clock reads. Fully unit-testable."""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from enum import Enum

from .config import Config


class Phase(str, Enum):
    MORNING = "morning"
    DAY_HOLD = "day_hold"
    EVE_PLAN = "eve_plan"
    FINAL = "final"
    NIGHT = "night"


class EveVerdict(str, Enum):
    EMERGENCY_ON = "emergency_on"
    RESCUE_TIMER = "rescue_timer"       # v4 §3: failsafe timer programmed (verified)
    NIGHT_RELEASE = "night_release"     # v4 §3: ratio held → grid released for the night
    SAFE_CLEAR = "safe_clear"           # nothing armed: failsafe beyond horizon
    GRID_HOLD = "grid_hold"             # grid kept/joined live (no timer involved)


def current_phase(now: datetime, cfg: Config, morning_done: bool, night_over: bool) -> Phase:
    t = now.time()
    if night_over and t < cfg.window_start:
        return Phase.MORNING
    if t >= cfg.window_end or t < cfg.window_start:
        return Phase.NIGHT
    final_from = (datetime.combine(now.date(), cfg.window_end)
                  - timedelta(minutes=cfg.final_offset_min)).time()
    if t >= final_from:
        return Phase.FINAL
    if t >= cfg.eve_start:
        return Phase.EVE_PLAN
    return Phase.DAY_HOLD if morning_done else Phase.MORNING


def deep_floor_hit(min_cell_v: float | None, cfg: Config) -> bool:
    """R1'a: one-directional safety floor. Unknown data → no hit (IN on outage = ah_tief rule)."""
    if min_cell_v is None:
        return False
    return min_cell_v <= cfg.thresh_deep


# L2 wave 1 (owner ruling 2026-10-25, option 1): the STANDARD LiFePO4 SoC↔voltage
# approximation — honest about the flat band (coarse ±10 % mid-curve), precise at
# the ENDS where the floor decisions live. The fitted per-pack table from the
# soc_anchor dataset supersedes this in wave 2 (display quality, not safety).
SOCV_PCT = [(3.10, 0.0), (3.20, 3.0), (3.27, 8.0), (3.31, 25.0),
            (3.33, 50.0), (3.40, 90.0), (3.45, 100.0)]


def soc_pct_from_v(v: float | None) -> float | None:
    """Physical SoC approximation from the corrected cell voltage (clamped)."""
    if v is None:
        return None
    if v <= SOCV_PCT[0][0]:
        return 0.0
    if v >= SOCV_PCT[-1][0]:
        return 100.0
    for (v0, p0), (v1, p1) in zip(SOCV_PCT, SOCV_PCT[1:]):
        if v <= v1:
            return p0 + (v - v0) * (p1 - p0) / (v1 - v0)
    return 100.0


def corrected_min_cell_v(min_cell_v: float | None, pack_current: float | None,
                         cfg: Config) -> float | None:
    """IR-corrected rested-equivalent worst-cell voltage. Ingest sign: current
    > 0 = CHARGING (+chg/−dsg per the wire convention) — remove the charge
    over-voltage, add back the discharge sag. DC-path R is config-derived from
    LIVE data (2026-10-25: 3.234 V at −7 A loaded → 3.269 V rested = 5 mΩ);
    the app's AC-pulse 0.051 Ω must NOT be used — it over-corrects ~7× and
    would fire the floor dangerously late. None if either input is unknown."""
    if min_cell_v is None or pack_current is None:
        return None
    return min_cell_v - pack_current * cfg.r_cell_ohm


def voltage_floor_hit(min_cell_v: float | None, pack_current: float | None,
                      cfg: Config) -> bool:
    """Owner ruling 2026-10-25 (option 1): the VOLTAGE-ONLY operational floor —
    the BMS coulomb counter is display/audit-only; it can never cut a night
    early again (the 06:40 incident: fired at 5.0 Ah while cells rested at
    3.269 V ≈ 10 % true SoC). Unknown data → conservative True (the retired
    retired capacity shape); the deep 2.90 V backstop stays independent."""
    v = corrected_min_cell_v(min_cell_v, pack_current, cfg)
    return v is None or v <= cfg.true_floor_v


# --- owner ruling 2026-10-25 (part 3): weather-conditional RESERVE TIERS -------
def _num(x) -> float | None:
    """Honest numeric or None — bool/str/NaN/inf never become a trigger."""
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    x = float(x)
    return x if math.isfinite(x) else None


def reserve_tier(days, cfg: Config) -> tuple[int, list[str]]:
    """Weather-conditional RESERVE TIER (owner ruling 2026-10-25, part 3):
    tier = the max reserve target in PERCENT OF BMS SOC — 50 (severe) >
    30 (moderate) > 0 (normal) — computed from the rolling `outlook.days`
    window (weather.py writer; evaluated at the eve consult and re-evaluated
    wherever the forecast/outlook is refreshed).

    TRIGGERS
    - 50 % severe: any day in the TIER_LOOKAHEAD_DAYS window with
      weather_code >= TIER2_WMO_CODE OR gust_max_kmh > TIER2_GUST_KMH OR
      rain INTENSITY (max hourly rate, mm/h) >= TIER2_INTENSITY_MMH.
      Owner ruling 2026-11-03 (METRIC SWITCH): the severe rain leg reads the
      day's PEAK HOURLY rate — "extreme weather, not the threat of a
      puddle" (the DWD warns in mm/h:15–25 = markant,25–40 = the next class;
      default25 anchors there). The daily-sum leg TIER2_PRECIP_MM is retired.
      The thresholds are POLICY values (config doctrine: env-tunable — the
      owner may retune them).
    - 30 % moderate: >= TIER1_DAYS CONSECUTIVE outlook days whose weather-API
      radiation (radiation_sum_j) is BELOW the SAME solar reference the
      weather card displays (WEATHER_MIN_J, joules — owner correction
      2026-10-25: "we do NOT have a three day bad weather outlook" killed the
      harvest-vs-need variant, which was never owner-confirmed; trigger and
      card can now never contradict). Unknown radiation or a disabled/unknown
      reference contributes NOTHING — honest unknown, no claim.
    - max(tier1, tier2): the severe tier outranks the moderate one.

    INSTRUMENT CARVE-OUT (owner, binding): the 2026-10-25 "display-only"
    wording stays BINDING for the DEEP/emergency decisions — those keep the
    corrected cell voltage (deep floor, emergency act, voltage_floor_hit
    semantics, 2.90 V backstop — all untouched beneath this layer). Tier
    comparisons use BMS SOC (reading.soc, the coulomb counter): the LiFePO4
    mid-band has no voltage expression (~12 mV between 30 % and 50 %), so for
    a SOFT hold the counter's local precision beats the voltage curve's
    global bias. Pure; no I/O; returns (tier_pct, human-readable triggers)."""
    window = [d for d in list(days or [])[:max(0, cfg.tier_lookahead_days)]
              if isinstance(d, dict)]
    if not window:                          # empty/short/unknown outlook → tier 0
        return 0, []
    tier2_triggers: list[str] = []
    seen: set[str] = set()                  # one reason per indicator kind (audit stays readable)
    for d in window:
        code, gust, inten = (_num(d.get("weather_code")),
                             _num(d.get("gust_max_kmh")),
                             _num(d.get("intensity_max_mmh")))
        if code is not None and code >= cfg.tier2_wmo_code and "code" not in seen:
            seen.add("code")
            tier2_triggers.append(f"weather_code {code:g} >= {cfg.tier2_wmo_code:g}")
        if gust is not None and gust > cfg.tier2_gust_kmh and "gust" not in seen:
            seen.add("gust")
            tier2_triggers.append(f"gust_max_kmh {gust:g} > {cfg.tier2_gust_kmh:g}")
        if inten is not None and inten >= cfg.tier2_intensity_mmh and "rain" not in seen:
            seen.add("rain")
            tier2_triggers.append(
                f"rain_intensity {inten:g} mm/h >= {cfg.tier2_intensity_mmh:g}")
    # moderate tier: consecutive days below the WEATHER CARD's own solar
    # reference (owner correction 2026-10-25 — the API's radiation vs the same
    # reference the monitor shows, so the sentence can never claim bad outlook
    # while the card says "≥ ref"); reference unknown/disabled → no claim.
    run = best = 0
    low_kwh: list[float] = []
    for d in window:
        rad = _num(d.get("radiation_sum_j"))
        ref = cfg.weather_min_j
        if ref is None or rad is None or rad < 0.0:
            run = 0
            continue
        if rad < ref:
            run += 1
            best = max(best, run)
            low_kwh.append(rad / 3.6e6)            # J/m² → kWh/m² for humans
        else:
            run = 0
    tier1_triggers = ([f"{best} of {len(window)} outlook days below the solar "
                       f"reference ({'/'.join(f'{v:.1f}' for v in low_kwh[:4])}"
                       f" vs {cfg.weather_min_j / 3.6e6:.2f} kWh/m2)"]
                      if best >= cfg.tier1_days else [])
    return max(50 if tier2_triggers else 0, 30 if tier1_triggers else 0), \
        tier2_triggers + tier1_triggers



def off_solar_ok(remaining_ah: float | None, recent_currents: list[float | None],
                 cfg: Config) -> bool:
    """R2'a: caller supplies the pack.current window covering >= SOLAR_HOLD_S.

    §13 v7: no production release path gates on it anymore (the day drains
    unconditionally above the floor) — kept as a LEARNING tool (L1/L2 input)."""
    if remaining_ah is None or remaining_ah < cfg.ah_off:
        return False
    if not recent_currents:
        return False
    return all(c is not None and c >= cfg.solar_off_a for c in recent_currents)
# v3 R3'': project_remaining (fixed night-load trajectory) is SUPERSEDED —
# the prediction speaks ENERGY since v4 (plan_evening: need Wh ÷ night length);
# the ampere night load + its t_fire helper were removed (owner ruling
# 2026-10-22: "energy is the unit which counts for prediction, nothing else").
# §13 v7 RETIRED: off_soc_ok (the static SOC gate, SOC_OFF_PCT) — the day has no
# gate; the reserve comes back later as a LEARNED dark-span value (L1-L3).
