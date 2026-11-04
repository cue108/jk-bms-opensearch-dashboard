"""gridguard.solar_predict — Phase C: the forecast onset link
("Will there be a solar day tomorrow?") — horizon for sunless chains.

Owner algorithm (his words, condensed): in a stretch of fine days the system
may expect tomorrow's solar-day start to be about the same time as yesterday's
— when yesterday's weather was also fine; and the link between the SOLAR
WEATHER FORECAST and the OWN SYSTEM MEASUREMENTS is already learnable from
the data at hand, so the system can say whether tomorrow will have a solar
day at all. "The amount of data merely helps produce a better average" —
honest statistics, NO model machinery.

Both inputs are EXISTING streams (injected as plain data, no second transport):
* forecast — the rolling outlook the reserve tier already fetches
  (WeatherClient.daily_outlook → RealDeps.outlook_days): per-day
  `radiation_sum_j` (J/m²/day, None-tolerant) + `date`;
* own measurements — the measured solar-edge history (BmsSource.solar_day_
  boundaries via deps.solar_windows: per day the FIRST charge after sunrise
  = the onset, the LAST before sunset) and the yield_learned pairs
  (measured_kwh vs forecast_kwh_m2 per completed day).

Basis classes (honest, statistic-modest):
* sunny_chain_analog — a RECENT day that really had a measured solar day and
  whose OWN forecast is SIMILAR to the target's → predict the onset ≈ that
  day's own measured onset (plain mean over the qualifying chain, newest
  first). Never without a real measured edge — a prediction is a LABEL.
* forecast_amount — no usable analog: the LEARNED forecast→measured factor
  (median of the yield pairs) times the target forecast decides WHETHER a
  solar day is expected; NO clock time is invented (onset_h stays None).
* insufficient — nothing sufficient → predicted None (the caller keeps its
  guards; never a guess).

Every evaluation is audited by RealDeps.solar_day_prediction as the event
`solar_day_pred` with this FULL basis (display-truth: predictions are
labelled, never state).

NAMED KNOBS (flagged for the owner — plain module constants, honest defaults):
* SIM_KWH_TOL          ± kWh/m²/day: forecast target vs analog day = "similar"
* ANALOG_LOOKBACK_DAYS how far back a recent measured sunny day still counts
* ANALOG_MIN_FC_KWH    kWh/m²/day: below this the analog never claims a day
* SOLAR_DAY_MIN_KWH    kWh/m²/day: the learned amount's floor for "some day"
"""
from __future__ import annotations

import statistics
from datetime import date, datetime, timedelta, tzinfo

SIM_KWH_TOL = 1.0            # "similar forecast" window (± kWh/m²/day)
ANALOG_LOOKBACK_DAYS = 5     # recent-sunny-day search depth
ANALOG_MIN_FC_KWH = 0.5      # a wash-out forecast never gets an analog claim
SOLAR_DAY_MIN_KWH = 0.25     # learned-amount floor (measured kWh/day)


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v


def _onset_hour(day: date, day_bounds: dict, tz: tzinfo | None) -> float | None:
    """The day's MEASURED onset (first charge ≥ flow after sunrise) as a local
    hour-of-day. None = no measured solar day for that date (never guessed)."""
    b = (day_bounds or {}).get(day)
    if not b:
        return None
    s = b[0]
    if s is None:
        return None
    if tz is not None:
        s = s.replace(tzinfo=tz) if s.tzinfo is None else s.astimezone(tz)
    return s.hour + s.minute / 60.0


def solar_day_prediction(target_date: date, *, forecast_days, day_bounds,
                         yield_hist, tz: tzinfo | None = None) -> dict:
    """Predict the solar day for `target_date` — {predicted: True|False|None,
    onset_h: float|None, basis, samples_used, forecast_kwh_m2, analog_day,
    factor} (the evidence fields ride along so the audit carries the FULL
    basis). Pure: all three sources are injected by the caller."""
    out: dict = {"predicted": None, "onset_h": None, "basis": "insufficient",
                 "samples_used": 0, "forecast_kwh_m2": None, "analog_day": None,
                 "factor": None}
    # the target's forecast amount (kWh/m²/day) — without it nothing is known
    fc: dict[date, float] = {}
    for d in forecast_days or []:
        if not isinstance(d, dict):
            continue
        try:
            fd = date.fromisoformat(str(d.get("date")))
        except (TypeError, ValueError):
            continue
        j = d.get("radiation_sum_j")
        if _is_num(j) and j >= 0:
            fc[fd] = j / 3.6e6
    fc_t = fc.get(target_date)
    if fc_t is None:
        return out                          # no forecast → honest nothing
    out["forecast_kwh_m2"] = round(fc_t, 3)
    # (1) SUNNY-CHAIN ANALOG: recent measured sunny day(s) with a similar
    # forecast → predict the onset ≈ their own measured onset (plain mean;
    # the chain is ordered newest-first, so analog_day = the most recent).
    if fc_t >= ANALOG_MIN_FC_KWH:
        analogs: list[tuple[date, float]] = []
        for i in range(1, ANALOG_LOOKBACK_DAYS + 1):
            d = target_date - timedelta(days=i)
            h = _onset_hour(d, day_bounds, tz)
            f = fc.get(d)
            if h is None or f is None:
                continue                    # needs a REAL edge AND its forecast
            if abs(fc_t - f) <= SIM_KWH_TOL:
                analogs.append((d, h))
        if analogs:
            out.update(predicted=True,
                       basis="sunny_chain_analog",
                       onset_h=round(statistics.fmean(h for _, h in analogs), 3),
                       samples_used=len(analogs),
                       analog_day=analogs[0][0].isoformat())
            return out
    # (2) FORECAST-AMOUNT: no analog → the OWN learned forecast→measured
    # connection decides WHETHER a day is expected; no clock claim.
    ratios = []
    for e in yield_hist or []:
        inp = (e or {}).get("inputs") or {}
        m, f = inp.get("measured_kwh"), inp.get("forecast_kwh_m2")
        if _is_num(m) and _is_num(f) and f > 0:
            ratios.append(m / f)
    if ratios:
        factor = statistics.median(ratios)
        out.update(basis="forecast_amount",
                   predicted=(fc_t * factor) >= SOLAR_DAY_MIN_KWH,
                   samples_used=len(ratios),
                   factor=round(factor, 3))
    return out                              # else: insufficient (honest)
