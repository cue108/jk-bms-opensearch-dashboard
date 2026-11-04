"""open-meteo forecast — release-GATE only (§12.1): may PREVENT a release (grid stays on), never force an action; None/any failure → conservative hold.

v4 §12.1 self-optimization basis: `ship_forecast` additionally POSTs every
morning forecast (best-effort, never raising into the loop) as one doc into
`{os_url}/{WEATHER_INDEX_PREFIX}-{YYYY.MM.DD}/_doc/{md5}`. md5-idempotent per
local day + value — a second ship of the same day and radiation value rewrites
the SAME doc; `radiation_sum_j: null` is shipped too, so failed forecast days
land in the history as well. Kill-switch `WEATHER_INDEX=0` (cfg.weather_index
is False) → None without any POST. Same building style as the Decision-Index
write in audit.py §10b: stdlib-HTTP, INDEX_TIMEOUT_S, errors swallowed into
ONE throttled stderr line ≥1h apart.
"""
from __future__ import annotations

import hashlib
import json
import math
import sys
import time
import urllib.request
from datetime import date, datetime
from zoneinfo import ZoneInfo

from .audit import INDEX_TIMEOUT_S
from .config import Config

SHIP_THROTTLE_S = 3600.0       # ≥1h between dual-write error lines (audit.py pattern)
_last_index_err: float | None = None   # module-level: ONE throttled stderr line per hour

MJ_PER_DAY_TO_J = 1_000_000.0            # open-meteo daily shortwave sum = MJ/m²/day
MAX_PLAUSIBLE_MJ_PER_DAY = 50.0          # desert max ≈40MJ — above that: unit-change guard


def _mj_per_day_to_j(raw) -> float | None:
    """Daily shortwave sum MJ/m²/day → Joules (the morning_radiation_j unit
    contract); non-numeric / negative / beyond the plausibility roof → honest
    None (owner2026-10-25 part 3: a tier-1 day with unknown radiation
    contributes nothing — never an invented harvest)."""
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v) or v < 0.0 or v > MAX_PLAUSIBLE_MJ_PER_DAY:
        return None
    return v * MJ_PER_DAY_TO_J


class WeatherClient:
    def __init__(self, lat: float, lon: float, timeout: float = 8.0):
        self.lat, self.lon, self.timeout = lat, lon, timeout
        self._get = _get_json

    def morning_radiation_j(self) -> float | None:
        """Daily shortwave sum (J/m²) for today — forecast input to the release gate
        (§12.1): shipped for calibration, None on any failure.

        Unit reconciliation (owner-live ruling2026-10-20): open-meteo serves
        `shortwave_radiation_sum` in **MJ/m² per day** (WMO convention; the
        response carries no `units` field — physical double-check10./19.10
        Málaga: raw10.0/11.87 =2.8/3.3 kWh plausible, kWh/Wh readings absurd).
        Converted ×1e6 to JOULES so the gate compares `WEATHER_MIN_J` apples
        to apples (8e6 =8MJ ≈2.2kWh: rainy days hold, real sun releases).
        Plausibility roof (>50MJ/day = beyond Earth-surface maxima, <0) =
        silent-unit-change tripwire → None → gate holds CONSERVATIVELY (grid
        stays on; never the battery left in the rain)."""
        try:
            url = (f"https://api.open-meteo.com/v1/forecast?latitude={self.lat}"
                   f"&longitude={self.lon}"
                   f"&daily=shortwave_radiation_sum,sunrise,sunset"
                   f"&timezone=auto&forecast_days=1")
            daily = self._get(url).get("daily") or {}
            sums = daily.get("shortwave_radiation_sum") or []
            if not sums or sums[0] is None:
                return None
            raw = float(sums[0])
            if not math.isfinite(raw) or raw < 0.0 or raw > MAX_PLAUSIBLE_MJ_PER_DAY:
                return None
            return raw * MJ_PER_DAY_TO_J
        except Exception:
            return None

    def sun_times(self, target_date: date | None = None) -> tuple[datetime | None, datetime | None]:
        """Calendar sunrise & sunset for `target_date` (default: today),
        from the SAME open-meteo API as the radiation sum — no extra
        dependency (astral/ephem not needed), no invented window
        (owner2026-10-21). Conservative: any failure or a missing field
        → (None, None); the solar day is then simply not boundary-anchored
        (a sunless day is OK)."""
        try:
            url = (f"https://api.open-meteo.com/v1/forecast?latitude={self.lat}"
                   f"&longitude={self.lon}"
                   f"&daily=sunrise,sunset&timezone=auto"
                   f"&start_date={target_date}&end_date={target_date}"
                   if target_date else
                   f"https://api.open-meteo.com/v1/forecast?latitude={self.lat}"
                   f"&longitude={self.lon}"
                   f"&daily=sunrise,sunset&timezone=auto&forecast_days=1")
            daily = self._get(url).get("daily") or {}
            rise = (daily.get("sunrise") or [None])[0]
            set_ = (daily.get("sunset") or [None])[0]
            if not rise or not set_:
                return None, None
            return datetime.fromisoformat(rise), datetime.fromisoformat(set_)
        except Exception:
            return None, None

    def daily_outlook(self, days: int = 3) -> list[dict] | None:
        """Owner outage-indicator INTAKE2026-10-23 (DATA): the next `days`
        local days with the grid-outage indicators open-meteo honestly offers
        — WMO weather_code (95-99 = thunder, categorical),
        precipitation_probability_max (% — the one TRUE probability in the
        set), wind_gusts_10m_max (km/h), cape_max (J/kg — convective energy,
        the thunderstorm ingredient) and precipitation_sum (mm). The free tier
        serves NO thunder/gust *probability*: we store the real values, never
        invented percentages.

        Owner ruling 2026-10-25 (part 3): the SAME request also asks for
        `shortwave_radiation_sum` (no second HTTP) and carries it per day as
        `radiation_sum_j` (MJ/m²/day → J, the morning_radiation_j unit
        contract + plausibility tripwire → honest None) — the reserve-tier
        tier-1 trigger (verdict.reserve_tier) is now this doc's consumer:
        expected harvest vs the learned CLEAN night need. Per-day
        None-tolerant; any failure → None (conservative).

        Owner ruling 2026-11-03 (METRIC SWITCH): the SAME request also asks
        `hourly=precipitation` and carries each local day's PEAK hourly rate
        as `intensity_max_mmh` (mm/h — hourly timestamp's date prefix groups
        the naive-local hours; a day without hourly values → None, honest
        unknown) — the severe tier's rain leg reads this, never the daily
        sum ("extreme weather, not the threat of a puddle")."""
        try:
            url = (f"https://api.open-meteo.com/v1/forecast?latitude={self.lat}"
                   f"&longitude={self.lon}"
                   f"&daily=weather_code,precipitation_probability_max,"
                   f"wind_gusts_10m_max,cape_max,precipitation_sum,"
                   f"shortwave_radiation_sum"
                   f"&hourly=precipitation"          # owner 2026-11-03: mm/h rates
                   f"&timezone=auto&forecast_days={days}")
            resp = self._get(url)
            daily = resp.get("daily") or {}
            times = daily.get("time") or []
            if not times:
                return None
            cols = {"weather_code": daily.get("weather_code") or [],
                    "precip_prob_max_pct": daily.get("precipitation_probability_max") or [],
                    "gust_max_kmh": daily.get("wind_gusts_10m_max") or [],
                    "cape_max_j": daily.get("cape_max") or [],
                    "precip_sum_mm": daily.get("precipitation_sum") or []}
            rad = daily.get("shortwave_radiation_sum") or []
            out = [{"date": d, **{k: (col[i] if i < len(col) else None)
                                  for k, col in cols.items()}}
                   for i, d in enumerate(times)]
            for i, day in enumerate(out):       # owner part3: per-day radiation, honest unknown
                day["radiation_sum_j"] = _mj_per_day_to_j(rad[i] if i < len(rad) else None)
            # owner ruling 2026-11-03 (metric switch): per LOCAL day the MAX
            # hourly rain rate (mm/h) — the severe tier's rain leg reads this,
            # never the daily sum. Grouping = the hourly timestamp's date
            # prefix (timezone=auto → naive local hours, aligned with
            # daily.time). A day without hourly values → None (honest unknown).
            hourly = resp.get("hourly") or {}
            peak: dict[str, float] = {}
            for t, v in zip(hourly.get("time") or [], hourly.get("precipitation") or []):
                if v is None:
                    continue
                dkey = str(t)[:10]
                if v > peak.get(dkey, float("-inf")):
                    peak[dkey] = v
            for day in out:
                v = peak.get(day["date"])
                day["intensity_max_mmh"] = None if v is None else round(float(v), 2)
            return out
        except Exception:
            return None


def _get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=8) as r:   # default timeout fixed here
        return json.loads(r.read().decode())


def _post_doc(url: str, payload: dict) -> None:
    """One forecast doc into `{prefix}-{day}/_doc/{md5}` — stdlib only, no auth."""
    req = urllib.request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                 method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=INDEX_TIMEOUT_S) as r:   # audit.py §10b timeout
        r.read()


def _report_index_err(e: Exception) -> None:
    """Throttled stderr line (≥1h apart) — the loop must never see this error."""
    global _last_index_err
    now = time.monotonic()
    if _last_index_err is None or now - _last_index_err >= SHIP_THROTTLE_S:
        _last_index_err = now
        print(f"[weather-index] dual-write failed: {e}", file=sys.stderr, flush=True)


def ship_forecast(cfg: Config, radiation_j: float | None, *, _post=None) -> dict | None:
    """§12.1: ship ONE forecast doc to `{prefix}-{local-day}/_doc/{md5}`.

    md5-id = md5(f"{local-day}|forecast|{radiation_j}") → same day + value
    rewrites the same doc (idempotent). `radiation_j=None` ships
    `"radiation_sum_j": null` (error days stay in the history). Kill-switch
    off → None without any POST; ANY failure → None + throttled stderr only.
    `_post(url, payload)` is the test injection point (audit.py:80 style).
    """
    if cfg.weather_index is False:
        return None
    try:
        tz = ZoneInfo(cfg.tz_name)
        now = datetime.now(tz)
        day = now.strftime("%Y.%m.%d")          # index day = LOCAL date (audit.py:78)
        doc = {"ts": now.isoformat(timespec="seconds"),
               "radiation_sum_j": radiation_j,
               "source": "open-meteo",
               "lat": cfg.weather_lat,
               "lon": cfg.weather_lon,
               "kind": "forecast"}
        doc_id = hashlib.md5(f"{day}|forecast|{radiation_j}".encode("utf-8")).hexdigest()
        (_post or _post_doc)(f"{cfg.os_url}/{cfg.weather_index_prefix}-{day}/_doc/{doc_id}", doc)
        return doc
    except Exception as e:                 # noqa: BLE001 — never disturb the loop
        _report_index_err(e)
        return None


def ship_outlook(cfg: Config, days: list[dict] | None, *, _post=None) -> dict | None:
    """Owner storm-outage INTAKE2026-10-23 (data only): ONE rolling outlook
    doc to `{prefix}-{local-day}/_doc/{md5}` — id = md5(f"{day}|outlook")
    is STABLE per local day, so a later ship of the day REWRITES the same
    doc (rolling-latest, unlike the value-id'd forecast). `days=None` ships
    `"days": null` (fetch-failure days stay visible in the history).
    Kill-switch `WEATHER_INDEX=0` → None without any POST; ANY failure →
    None + the throttled stderr line only."""
    if cfg.weather_index is False:
        return None
    try:
        tz = ZoneInfo(cfg.tz_name)
        now = datetime.now(tz)
        day = now.strftime("%Y.%m.%d")          # index day = LOCAL date
        doc = {"ts": now.isoformat(timespec="seconds"),
               "days": days,
               "source": "open-meteo",
               "lat": cfg.weather_lat,
               "lon": cfg.weather_lon,
               "kind": "outlook"}
        doc_id = hashlib.md5(f"{day}|outlook".encode("utf-8")).hexdigest()
        (_post or _post_doc)(f"{cfg.os_url}/{cfg.weather_index_prefix}-{day}/_doc/{doc_id}", doc)
        return doc
    except Exception as e:                 # noqa: BLE001 — never disturb the loop
        _report_index_err(e)
        return None
