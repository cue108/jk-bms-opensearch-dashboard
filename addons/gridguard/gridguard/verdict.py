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
    RESCUE_TIMER = "rescue_timer"       # failsafe timer programmed (verified)
    NIGHT_RELEASE = "night_release"     # ratio held → grid released for the night
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
    """One-directional safety floor. Unknown data → no hit (the IN-on-outage
    ah_tief rule)."""
    if min_cell_v is None:
        return False
    return min_cell_v <= cfg.thresh_deep


# The textbook LiFePO4 voltage→SoC table is REMOVED from the source: our
# sensor is the BMS and it is the only truth — tables are only ever built
# from OUR measurement data (e.g. a per-pack fit from the soc_anchor
# dataset), never from a book. The live BMS SOC is the truth; voltage =
# the out-of-band observation only.


def corrected_min_cell_v(min_cell_v: float | None, pack_current: float | None,
                         cfg: Config) -> float | None:
    """IR-corrected rested-equivalent worst-cell voltage. Ingest sign: current
    > 0 = CHARGING (+chg/−dsg per the wire convention) — remove the charge
    over-voltage, add back the discharge sag. DC-path R is config-derived from
    LIVE data (3.234 V at −7 A loaded → 3.269 V rested = 5 mΩ);
    the app's AC-pulse 0.051 Ω must NOT be used — it over-corrects ~7× and
    would fire the floor dangerously late. None if either input is unknown."""
    if min_cell_v is None or pack_current is None:
        return None
    return min_cell_v - pack_current * cfg.r_cell_ohm


def voltage_floor_hit(min_cell_v: float | None, pack_current: float | None,
                      cfg: Config) -> bool:
    """The VOLTAGE-ONLY operational floor — the BMS coulomb counter is
    display/audit-only; it can never cut a night early again (a coulomb-count
    trigger fired at 5.0 Ah while cells rested at 3.269 V ≈ 10 % true SoC).
    Unknown data → conservative True (the retired capacity-gate shape);
    the deep 2.90 V backstop stays independent."""
    v = corrected_min_cell_v(min_cell_v, pack_current, cfg)
    return v is None or v <= cfg.true_floor_v


# --- weather-conditional RESERVE TIERS ----------------------------------------
def _num(x) -> float | None:
    """Honest numeric or None — bool/str/NaN/inf never become a trigger."""
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    x = float(x)
    return x if math.isfinite(x) else None


def reserve_tier(days, cfg: Config) -> tuple[int, list[str]]:
    """Weather-conditional RESERVE TIER:
    tier = the max reserve target in PERCENT OF BMS SOC — 50 (severe) >
    30 (moderate) > 0 (normal) — computed from the rolling `outlook.days`
    window (weather.py writer; evaluated at the eve consult and re-evaluated
    wherever the forecast/outlook is refreshed).

    TRIGGERS
    - 50 % severe: any day in the TIER_LOOKAHEAD_DAYS window with
      weather_code >= TIER2_WMO_CODE OR gust_max_kmh > TIER2_GUST_KMH OR
      rain INTENSITY (max hourly rate, mm/h) >= TIER2_INTENSITY_MMH.
      The severe rain leg reads the day's PEAK HOURLY rate (extreme-weather
      intensity, not accumulated volume; the DWD warns in mm/h: 15–25 =
      markant, 25–40 = the next class; default 25 anchors there). The
      daily-sum leg TIER2_PRECIP_MM is retired. The thresholds are POLICY
      values (config doctrine: env-tunable).
    - 30 % moderate: >= TIER1_DAYS outlook days in the window whose
      weather-API radiation (radiation_sum_j) is BELOW the SAME solar
      reference the weather card displays (WEATHER_MIN_J, joules — trigger
      and card can never contradict; a three-day bad-weather outlook does
      not exist, so there is no harvest-vs-need variant). Unknown radiation
      or a disabled/unknown reference contributes NOTHING — honest unknown,
      no claim.
    - max(tier1, tier2): the severe tier outranks the moderate one.

    INSTRUMENT CARVE-OUT: the "display-only" wording stays BINDING for the
    DEEP/emergency decisions — those keep the corrected cell voltage (deep
    floor, emergency act, voltage_floor_hit semantics, 2.90 V backstop — all
    untouched beneath this layer). Tier comparisons use BMS SOC
    (reading.soc, the coulomb counter): the LiFePO4 mid-band has no voltage
    expression (~12 mV between 30 % and 50 %), so for a SOFT hold the
    counter's local precision beats the voltage curve's global bias. Pure;
    no I/O; returns (tier_pct, human-readable triggers)."""
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
    # moderate tier: days below the WEATHER CARD's own solar reference
    # (the API's radiation vs the same reference the monitor shows, so the
    # sentence can never claim bad outlook while the card says "≥ ref");
    # reference unknown/disabled → no claim.
    # The tier COUNTS the below-ref days ANYWHERE in the window — any bad
    # weather day holds the UPS mode (it exists to bridge a possible grid
    # outage ON a bad weather day); a fully clear window is the only release.
    low_kwh: list[float] = []
    for d in window:
        rad = _num(d.get("radiation_sum_j"))
        ref = cfg.weather_min_j
        if ref is None or rad is None or rad < 0.0:
            continue
        if rad < ref:
            low_kwh.append(rad / 3.6e6)            # J/m² → kWh/m² for humans
    count = len(low_kwh)
    tier1_triggers = ([f"{count} of {len(window)} outlook days below the solar "
                       f"reference ({'/'.join(f'{v:.1f}' for v in low_kwh[:4])}"
                       f" vs {cfg.weather_min_j / 3.6e6:.2f} kWh/m2)"]
                      if count >= cfg.tier1_days else [])
    return max(50 if tier2_triggers else 0, 30 if tier1_triggers else 0), \
        tier2_triggers + tier1_triggers


def active_tier_on(days, cfg: Config, on_date) -> tuple[int, list[str]] | None:
    """The reserve tier is active ONLY on the day the bad-weather forecast
    is FOR — never the day after (the UPS mode exists for that one day, not
    for the one following it). An actor acting on `on_date` evaluates the
    triggers OF THAT DATE only: filter the rolling outlook to the date and
    run the SAME reserve_tier law on it (tier-2 legs per day, tier-1
    below-reference leg per day) — the 3-day window max stays the
    latch/audit record (set at the eve for the coming day), this helper is
    the ENFORCEMENT view the discharge-stop, the release gates and the plan
    floors read.

    None = honest unknown (the outlook does not cover `on_date`, or is not a
    list at all) — the caller keeps the conservative latch fallback; missing
    data must never silently disarm a hold. `on_date` accepts a date or its
    ISO string."""
    if not isinstance(days, list):
        return None
    key = on_date.isoformat() if hasattr(on_date, "isoformat") else str(on_date)
    hit = [d for d in days if isinstance(d, dict) and d.get("date") == key]
    if not hit:
        return None
    return reserve_tier(hit, cfg)


def off_solar_ok(remaining_ah: float | None, recent_currents: list[float | None],
                 cfg: Config) -> bool:
    """Caller supplies the pack.current window covering >= SOLAR_HOLD_S.

    No production release path gates on it anymore (the day drains
    unconditionally above the floor) — kept as a LEARNING tool (L1/L2 input)."""
    if remaining_ah is None or remaining_ah < cfg.ah_off:
        return False
    if not recent_currents:
        return False
    return all(c is not None and c >= cfg.solar_off_a for c in recent_currents)
# project_remaining (fixed night-load trajectory) is SUPERSEDED — the
# prediction speaks ENERGY (plan_evening: need Wh ÷ night length); the
# ampere night load + its t_fire helper were removed (energy is the unit
# that counts for prediction).
# off_soc_ok (the static SOC gate, SOC_OFF_PCT) RETIRED — the day has no
# gate; the reserve comes back later as a LEARNED dark-span value (L1-L3).
