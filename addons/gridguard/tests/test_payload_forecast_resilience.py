"""Payload survives a DEAD forecast dependency — owner order 2026-11-02.

Wire evidence (host50, 2026-11-02T15:10Z): open-meteo answered
`503 {"error":true,"reason":"The service is overloaded"}` on every attempt;
`_forecast3()` unwound `fetch_gridguard()` at web_payload.py:1059, the
webservice answered 502, and the page blanked ALL fields (SOC, history,
plan — data the payload already held) behind one unguarded external call.

Doctrine applied to the payload side (the decision path already has it,
fc61806: consult-side deps must never kill): forecast3 = None + the other
contract keys intact; the page's own JS (`g.forecast3||[]`) renders
"forecast unavailable" for null — display-truth without a frontend change.
"""
import importlib
import io
from datetime import datetime, timezone
from unittest import mock
from urllib.error import HTTPError

from test_characterization import (
    _ENERGY_AGG, _EMPTY_SEARCH, _FORECAST, _LATEST, _TIMELINE,  # noqa: F401
    _env, _Resp, _router, FrozenDT,
)

# REAL captured wire body (open-meteo, overloaded) — wire-fixture doctrine.
_OVERLOADED_BODY = b'{"error":true,"reason":"The service is overloaded"}'


def _router_dead_forecast(req, timeout=None):
    """Cluster + sun-times answer normally; EVERY open-meteo forecast call
    fails exactly like the live outage (HTTP 503, real body)."""
    url = getattr(req, "full_url", str(req))
    if "open-meteo.com" in url:
        raise HTTPError(url, 503, "Service Unavailable", None,
                        io.BytesIO(_OVERLOADED_BODY))
    return _router(req, timeout)


def _payload_with_dead_forecast():
    _env()
    m = importlib.import_module("gridguard.web_payload")
    for cache in ("_FC3", "_SUN_CACHE"):
        c = getattr(m, cache, None)
        if isinstance(c, dict):
            c.clear()
    fc3 = getattr(m, "_FC3", None)
    if isinstance(fc3, dict):
        fc3.update(ts=0.0, data=None)          # cache always misses (harness rule)
    with mock.patch.object(m, "datetime", FrozenDT), \
         mock.patch("urllib.request.urlopen", side_effect=_router_dead_forecast):
        return m.fetch_gridguard()             # must NOT raise


def test_payload_survives_dead_forecast_dependency():
    payload = _payload_with_dead_forecast()
    # the whole point: no exception escaped, forecast honestly absent
    assert payload["forecast3"] is None
    # every other contract key the page consumes must be present (the data
    # the payload already held when the forecast died)
    for key in ("status", "counters", "history", "plan", "weather",
                "decisions", "decision", "label_era", "measured_today_kwh"):
        assert key in payload, f"missing contract key: {key}"


def test_forecast_list_shape_unchanged_when_service_works():
    """Twin pin: with a LIVE service the payload keeps the golden shape
    (list of {date, kwh_m2, gate_ok}) — the net must not fire on success."""
    _env()
    m = importlib.import_module("gridguard.web_payload")
    for cache in ("_FC3", "_SUN_CACHE"):
        c = getattr(m, cache, None)
        if isinstance(c, dict):
            c.clear()
    fc3 = getattr(m, "_FC3", None)
    if isinstance(fc3, dict):
        fc3.update(ts=0.0, data=None)
    with mock.patch.object(m, "datetime", FrozenDT), \
         mock.patch("urllib.request.urlopen", side_effect=_router):
        payload = m.fetch_gridguard()
    assert isinstance(payload["forecast3"], list) and payload["forecast3"]
    assert set(payload["forecast3"][0]) >= {"date", "kwh_m2", "gate_ok"}
