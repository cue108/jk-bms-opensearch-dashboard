"""§12.1 self-optimization basis — `weather.ship_forecast` dual-writes EVERY
forecast (also radiation=None error days) as one doc into
`{os_url}/{WEATHER_INDEX_PREFIX}-{YYYY.MM.DD}/_doc/{md5}`:
md5-idempotent per local-day+value (id = md5(f"{day}|forecast|{r}")),
kill-switch `WEATHER_INDEX=0` → None without any POST, stdlib-POST in the
audit.py:80 building style (short INDEX_TIMEOUT_S, errors swallowed into ONE
throttled stderr line ≥1h apart) — tests inject `_post(url, payload)`, no net.
"""
import hashlib
import json
from datetime import datetime
from urllib.error import URLError
from zoneinfo import ZoneInfo

import pytest

import gridguard.weather as weather_mod
from gridguard.config import load_config
from gridguard.weather import WeatherClient, ship_forecast, ship_outlook

MADRID = ZoneInfo("Europe/Madrid")
OS_URL = "http://127.0.0.1:9205"

GOOD = {"TZ": "Europe/Madrid", "WINDOW_START": "09:00", "WINDOW_END": "22:00",
        "EVE_START": "20:00", "OPENSEARCH_URL": OS_URL, "BMS_INDEX": "i",
        "EWCLOUD_EMAIL": "a", "EWCLOUD_PASS": "b", "SW_MAIN_DEVICEID": "d",
        "SW_MAIN_DEVICEKEY": "k", "SW_MAIN_IP": "1.2.3.4",
        "NTFY_WEBHOOK": "https://n/t", "DRY_RUN": "1",
        "WEATHER_LAT": "36.7", "WEATHER_LON": "-4.4"}


@pytest.fixture(autouse=True)
def fresh_throttle(monkeypatch):
    """module-level stderr throttle starts clean per test (≥1h rule)."""
    monkeypatch.setattr(weather_mod, "_last_index_err", None, raising=False)


def _cfg(**env):
    return load_config({**GOOD, **env})


def _sink(posts):
    def fake(url, payload):
        posts.append((url, payload))
    return fake


def _expected_id(day: str, radiation_j) -> str:
    return hashlib.md5(f"{day}|forecast|{radiation_j}".encode()).hexdigest()


def test_ship_forecast_posts_dated_index_with_full_payload():
    cfg = _cfg()
    posts: list = []
    doc = ship_forecast(cfg, 5.2e6, _post=_sink(posts))
    assert doc is not None and len(posts) == 1
    url, payload = posts[0]
    assert payload == doc                                     # shipped doc == return value
    # ts = ISO in cfg.tz (Europe/Madrid); index day = LOCAL day of that ts
    payload_ts = datetime.fromisoformat(payload["ts"])
    assert payload_ts.utcoffset() == datetime.now(MADRID).utcoffset()
    day = payload_ts.date().strftime("%Y.%m.%d")
    assert url == f"{OS_URL}/gridguard-weather-{day}/_doc/{_expected_id(day, 5.2e6)}"
    assert payload["radiation_sum_j"] == 5.2e6
    assert payload["source"] == "open-meteo" and payload["kind"] == "forecast"
    assert payload["lat"] == 36.7 and payload["lon"] == -4.4


def test_md5_id_idempotent_per_day_and_value():
    cfg = _cfg()
    posts: list = []
    ship_forecast(cfg, 5.2e6, _post=_sink(posts))
    ship_forecast(cfg, 5.2e6, _post=_sink(posts))             # same day + value
    ship_forecast(cfg, 7.4e6, _post=_sink(posts))             # value changes → new id
    ids = [url.rsplit("/", 1)[-1] for url, _ in posts]
    assert ids[0] == ids[1]                                   # idempotent: same _id
    assert len(set(ids)) == 2                                 # different value, new doc id
    day = datetime.fromisoformat(posts[0][1]["ts"]).date().strftime("%Y.%m.%d")
    assert ids[0] == _expected_id(day, 5.2e6)


def test_kill_switch_off_returns_none_without_any_post():
    cfg = _cfg(WEATHER_INDEX="0")
    posts: list = []
    assert ship_forecast(cfg, 5.2e6, _post=_sink(posts)) is None
    assert posts == []                                        # NO Post at all


def test_none_radiation_still_ships_null_history():
    # history of error days too: radiation_j=None IS sent along
    cfg = _cfg()
    posts: list = []
    assert ship_forecast(cfg, None, _post=_sink(posts)) is not None
    url, payload = posts[0]
    assert payload["radiation_sum_j"] is None                 # dict value
    assert '"radiation_sum_j": null' in json.dumps(payload)   # wire form = JSON null
    day = datetime.fromisoformat(payload["ts"]).date().strftime("%Y.%m.%d")
    assert url.endswith(f"/{_expected_id(day, None)}")        # id over "…|forecast|None"


def test_post_failure_swallows_and_throttles_one_stderr_line_per_hour(capsys):
    cfg = _cfg()
    calls: list = []

    def boom(url, payload):
        calls.append(url)
        raise URLError("cluster down")

    assert ship_forecast(cfg, 3e6, _post=boom) is None         # never raises into the loop
    assert ship_forecast(cfg, 3e6, _post=boom) is None
    err = capsys.readouterr().err
    assert err.count("[weather-index]") == 1                   # ONE throttled line ≥1h apart
    assert "cluster down" in err
    assert len(calls) == 2                                     # best-effort: still attempted


def test_default_post_is_stdlib_request_with_short_timeout(monkeypatch):
    """Default (no injection) = real stdlib POST — audit.py style, INDEX_TIMEOUT_S."""
    cfg = _cfg()
    seen: dict = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"{}"

    def fake_urlopen(req, timeout=None):
        seen["req"], seen["timeout"] = req, timeout
        return _Resp()

    monkeypatch.setattr(weather_mod.urllib.request, "urlopen", fake_urlopen)
    assert ship_forecast(cfg, 12e6) is not None                # NO _post injection
    req, timeout = seen["req"], seen["timeout"]
    assert timeout == 3.0                                      # audit.INDEX_TIMEOUT_S reused
    assert req.get_method() == "POST"
    assert req.get_header("Content-type") == "application/json"
    assert req.full_url.startswith(f"{OS_URL}/gridguard-weather-")
    day = datetime.fromisoformat(json.loads(req.data)["ts"]).date().strftime("%Y.%m.%d")
    assert req.full_url == f"{OS_URL}/gridguard-weather-{day}/_doc/{_expected_id(day, 12e6)}"
    sent = json.loads(req.data)
    assert sent["radiation_sum_j"] == 12e6 and sent["kind"] == "forecast"
    assert sent["source"] == "open-meteo"


# --- morning_radiation_j: units contract (Owner-LIVE2026-10-20) ---------------
# open-meteo delivers shortwave_radiation_sum in MJ/m²/day (WMO convention;
# physics double-check Málaga 10./19.10: raw 10.0/11.87 = 2.8/3.3 kWh plausible,
# kWh/Wh readings absurd). The gate comparison (WEATHER_MIN_J) = JOULE.

def test_morning_radiation_converts_raw_mj_to_joules():
    w = WeatherClient(36.76, -4.19)
    w._get = lambda url: {"daily": {"shortwave_radiation_sum": [11.87]}}
    assert w.morning_radiation_j() == pytest.approx(11.87e6)


def test_morning_radiation_gate_scale_sane():
    """Calibration double-anchor: rainy day (1.9 MJ) <8MJ = blocked,
    proper sunny day (15 MJ) >8MJ = released — in REAL joules."""
    w = WeatherClient(36.76, -4.19)
    w._get = lambda url: {"daily": {"shortwave_radiation_sum": [1.9]}}
    rain_j = w.morning_radiation_j()
    w._get = lambda url: {"daily": {"shortwave_radiation_sum": [15.0]}}
    sun_j = w.morning_radiation_j()
    assert rain_j is not None and rain_j < 8_000_000 < sun_j


def test_morning_radiation_tripwire_absurd_or_negative_is_none():
    """Plausibility roof (>50 MJ/day = beyond terrestrial maxima; <0 = defect)
    → None → gate blocks conservatively (grid stays on instead of the battery
    being sent flat)."""
    w = WeatherClient(36.76, -4.19)
    w._get = lambda url: {"daily": {"shortwave_radiation_sum": [480.0]}}
    assert w.morning_radiation_j() is None
    w._get = lambda url: {"daily": {"shortwave_radiation_sum": [-1.0]}}
    assert w.morning_radiation_j() is None


def test_morning_radiation_failure_and_empty_still_none():
    w = WeatherClient(36.76, -4.19)

    def boom(url):
        raise URLError("offline")

    w._get = boom
    assert w.morning_radiation_j() is None
    w._get = lambda url: {"daily": {"shortwave_radiation_sum": []}}
    assert w.morning_radiation_j() is None
    w._get = lambda url: {"daily": {}}
    assert w.morning_radiation_j() is None


# --- sunrise/sunset from the SAME open-meteo call (owner2026-10-21) ------------
# No astral/ephem dependency: the solar-day boundary needs the CALENDAR
# sunrise/sunset for the site, and open-meteo already knows the location
# (same lat/lon as the radiation sum). One API, one truth, stdlib parse.

def test_morning_radiation_url_carries_sunrise_sunset():
    """The daily= request is extended with sunrise,sunset — same coordinates."""
    w = WeatherClient(36.76, -4.19)
    seen: dict = {}

    def fake_get(url):
        seen["url"] = url
        return {"daily": {"shortwave_radiation_sum": [10.0]}}

    w._get = fake_get
    w.morning_radiation_j()
    assert "sunrise" in seen["url"] and "sunset" in seen["url"]


def test_sun_times_parses_site_local_iso_strings():
    w = WeatherClient(36.76, -4.19)
    w._get = lambda url: {"daily": {
        "shortwave_radiation_sum": [10.0],
        "sunrise": ["2026-10-21T08:14"],
        "sunset": ["2026-10-21T19:32"]}}
    rise, set_ = w.sun_times()
    assert (rise.hour, rise.minute) == (8, 14)
    assert (set_.hour, set_.minute) == (19, 32)


def test_sun_times_none_when_api_omits_or_fails():
    """Conservative: no sun times → (None, None) → the solar day cannot be
    boundary-anchored, never an invented window."""
    w = WeatherClient(36.76, -4.19)
    w._get = lambda url: {"daily": {"shortwave_radiation_sum": [10.0]}}
    assert w.sun_times() == (None, None)

    def boom(url):
        raise URLError("offline")

    w._get = boom
    assert w.sun_times() == (None, None)


# --- daily_outlook / ship_outlook — storm-indicator outake (Owner2026-10-23,
# DATA ONLY: no policy, no gate, no consumer) ----------------------------------
def _outlook_payload():
    """Live shape of the daily response (checked 23.10.: all four indicators
    incl. cape_max are really delivered by the free API)."""
    return {"daily": {
        "time": ["2026-10-23", "2026-10-24", "2026-10-25"],
        "weather_code": [3, 95, 80],
        "precipitation_probability_max": [0, 65, 40],
        "wind_gusts_10m_max": [41.4, 78.0, 31.0],
        "cape_max": [10.0, 850.0, None],
        "precipitation_sum": [0.0, 12.5, 0.6]},
        # owner ruling 2026-11-03 (metric switch): the free tier's HOURLY
        # precipitation (mm per hour) — the source of each day's max rate
        "hourly": {
            "time": ["2026-10-23T00:00", "2026-10-23T13:00",
                     "2026-10-24T06:00", "2026-10-24T07:00",
                     "2026-10-24T08:00", "2026-10-25T10:00"],
            "precipitation": [0.0, 0.0, 3.5, 28.0, 4.0, 1.2]}}


def test_daily_outlook_parses_indicator_arrays():
    w = WeatherClient(36.7, -4.4)
    w._get = lambda url: _outlook_payload()
    out = w.daily_outlook()
    assert [d["date"] for d in out] == ["2026-10-23", "2026-10-24", "2026-10-25"]
    assert out[1]["weather_code"] == 95            # WMO95 = thunderstorm (categorical)
    assert out[1]["precip_prob_max_pct"] == 65     # the ONE real probability
    assert out[1]["gust_max_kmh"] == 78.0
    assert out[1]["cape_max_j"] == 850.0           # convective energy = thunderstorm indicator
    assert out[1]["precip_sum_mm"] == 12.5
    assert out[2]["cape_max_j"] is None            # None tolerance, never invented


def test_daily_outlook_carries_max_hourly_rain_intensity():
    # Owner ruling 2026-11-03 (metric switch): per LOCAL day the max hourly
    # rate (mm/h) — the reserve tier's severe rain leg reads THIS, never the
    # daily sum. Grouping = the hourly timestamp's date prefix (timezone=auto
    # delivers naive local hours, aligned with daily.time).
    w = WeatherClient(36.7, -4.4)
    w._get = lambda url: _outlook_payload()
    out = w.daily_outlook()
    assert [d["intensity_max_mmh"] for d in out] == [0.0, 28.0, 1.2]


def test_daily_outlook_requests_all_indicators_and_is_none_on_failure():
    w = WeatherClient(36.7, -4.4)
    seen: dict = {}

    def _capture(url):
        seen["url"] = url
        return _outlook_payload()
    w._get = _capture
    w.daily_outlook()
    for p in ("weather_code", "precipitation_probability_max",
              "wind_gusts_10m_max", "cape_max", "precipitation_sum",
              "hourly=precipitation"):
        assert p in seen["url"]

    def _boom(url):
        raise OSError("open-meteo down")
    w._get = _boom
    assert w.daily_outlook() is None               # conservative, never invent


def test_ship_outlook_rolling_day_id_null_days_and_kill_switch():
    cfg = _cfg()
    posts: list = []
    days = [{"date": "2026-10-24", "weather_code": 95}]
    doc = ship_outlook(cfg, days, _post=_sink(posts))
    assert doc is not None and len(posts) == 1
    url, payload = posts[0]
    assert payload == doc and payload["kind"] == "outlook"
    assert payload["days"] == days
    day = datetime.fromisoformat(payload["ts"]).date().strftime("%Y.%m.%d")
    stable = hashlib.md5(f"{day}|outlook".encode()).hexdigest()
    assert url == f"{OS_URL}/gridguard-weather-{day}/_doc/{stable}"
    # rolling-latest: a second ship on the same day writes the SAME doc-id
    ship_outlook(cfg, days, _post=_sink(posts))
    assert posts[1][0] == url
    # fetch outage: days=None still ships (error days visible)
    ship_outlook(cfg, None, _post=_sink(posts))
    assert posts[2][1]["days"] is None
    # Kill-switch
    off = _cfg(WEATHER_INDEX="0")
    assert ship_outlook(off, days, _post=_sink(posts)) is None
    assert len(posts) == 3


def test_daily_outlook_carries_radiation_sum_j_per_day():
    """owner ruling 2026-10-25 (part 3) — the outlook writer (l3 intake) now
    ALSO asks the SAME open-meteo request for shortwave_radiation_sum, so the
    tier-1 trigger gets a per-day expected-harvest input without a second
    request. MJ/m²/Tag → Joule like the morning_radiation_j contract; a
    missing value stays honest None (contributes nothing to the tier)."""
    payload = {"daily": {
        "time": ["2026-10-23", "2026-10-24"],
        "weather_code": [3, 95],
        "precipitation_probability_max": [0, 65],
        "wind_gusts_10m_max": [41.4, 78.0],
        "cape_max": [10.0, 850.0],
        "precipitation_sum": [0.0, 12.5],
        "shortwave_radiation_sum": [11.5, None]}}
    seen: dict = {}

    def _capture(url):
        seen["url"] = url
        return payload
    w = WeatherClient(36.7, -4.4)
    w._get = _capture
    out = w.daily_outlook()
    assert "shortwave_radiation_sum" in seen["url"]   # SAME request, no extra HTTP
    assert out[0]["radiation_sum_j"] == pytest.approx(11.5e6)
    assert out[1]["radiation_sum_j"] is None          # honest unknown day
    # the existing indicator fields are untouched by the added column
    assert out[0]["weather_code"] == 3 and out[1]["gust_max_kmh"] == 78.0
    assert out[1]["cape_max_j"] == 850.0
