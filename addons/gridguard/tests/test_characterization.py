"""Byte-equality oracle for the verbatim move (investigation §8 risk #1).
Golden files are captured ONCE (DSH_CAPTURE=1) against the CURRENT code and
committed; every later run asserts byte-equality. Pre-move the impl is
`server` (repo frontend/); post-move it prefers gridguard.web_payload /
gridguard.web_page — the SAME file, so the flip itself is the proof."""
import importlib, json, os, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest

REPO = Path(__file__).resolve().parents[3]          # …/jk-bms-opensearch-dashboard
FIX = Path(__file__).resolve().parent / "fixtures"
CAPTURE = os.environ.get("DSH_CAPTURE") == "1"

# Frozen wall clock for the payload (datetime.now must be deterministic).
FREEZE = datetime(2026, 10, 25, 12, 0, 0, tzinfo=timezone.utc)


class FrozenDT(datetime):                          # subclass = arithmetic-safe
    @classmethod
    def now(cls, tz=None):
        return FREEZE.astimezone(tz) if tz else FREEZE


def _env():                                         # deterministic inputs
    os.environ["OPENSEARCH_URL"] = "http://stub:9200"
    os.environ["INDEX"] = "bms-test-*"
    os.environ["CHARTS_TZ"] = "UTC"
    os.environ.setdefault("GG_LAT", "36.76")
    os.environ.setdefault("GG_LON", "-4.19")


def _payload_impl():
    try:                                            # post-move module first
        m = importlib.import_module("gridguard.web_payload")
        if hasattr(m, "fetch_gridguard"):
            return m
    except ImportError:
        pass
    if str(REPO / "frontend") not in sys.path:
        sys.path.insert(0, str(REPO / "frontend"))
    return importlib.import_module("server")        # pre-move


def _page_impl_and_attr():
    try:
        m = importlib.import_module("gridguard.web_page")
        if hasattr(m, "PAGE"):
            return m, "PAGE"
    except ImportError:
        pass
    if str(REPO / "frontend") not in sys.path:
        sys.path.insert(0, str(REPO / "frontend"))
    m = importlib.import_module("server")
    return m, "GRIDGUARD_PAGE"


_EMPTY_SEARCH = {"hits": {"hits": [], "total": {"value": 0}}}
_TIMELINE = {"hits": {"hits": [                                  # feeds last_tick/last_start
    {"_source": {"ts": "2026-10-25T11:40:00+00:00", "event": "tick",
                 "phase": "day_hold", "result": "", "inputs": {},
                 "action": "", "timer_action": ""}},
    {"_source": {"ts": "2026-10-25T09:10:00+00:00", "event": "start",
                 "phase": "morning", "result": "dry_run=False",
                 "inputs": {}, "action": "", "timer_action": ""}},
]}}
_LATEST = {"hits": {"hits": [{"_source": {
    "@timestamp": "2026-10-25T11:59:00+00:00", "voltage": 52.1,
    "current": -3.2, "soc": 41.0, "min_cell_v": 3.31, "charge_a": 4.1,
    "remaining_ah": 86.0}}]}}
_ENERGY_AGG = {"hits": {"hits": []},
               "aggregations": {"e": {"value": None}}}
_FORECAST = {"daily": {"time": ["2026-10-25", "2026-10-26", "2026-10-27"],
                       "shortwave_radiation_sum": [4.2, 3.1, 2.4]},
             "daily_units": {"shortwave_radiation_sum": "MJ/m²"}}


class _Resp:                                        # minimal urlopen response
    def __init__(self, obj): self._b = json.dumps(obj).encode()
    def read(self): return self._b
    def __enter__(self): return self
    def __exit__(self, *a): return False
    status, headers = 200, {}


def _router(req, timeout=None):
    url = getattr(req, "full_url", str(req))
    if "open-meteo.com" in url:
        return _Resp(_FORECAST)
    data = getattr(req, "data", None) or b""
    if url.endswith("/_msearch"):
        n = data.decode().count('"index"')
        # Per-site shapes discovered by running (Step 1.4): start with the
        # energy aggregate; if a consumer demands more keys, shape THAT
        # response and record it here — never weaken the byte-compare.
        return _Resp({"responses": [dict(_ENERGY_AGG)] * n})
    if data and b'"match_all"' in data:             # events timeline (@229)
        return _Resp(_TIMELINE)
    if data and b'NACHTPLAN' in data:               # push queries (@303/@840)
        return _Resp(_EMPTY_SEARCH)
    if data and b'"sort"' in data and b'"size": 1' in data.replace(b" ", b""):
        return _Resp(_LATEST)                       # latest doc (@370)
    return _Resp(_EMPTY_SEARCH)


def _run_payload(m):
    _env()
    for cache in ("_FC3", "_SUN_CACHE"):
        c = getattr(m, cache, None)
        if isinstance(c, dict):
            c.clear()
    # Brief-fidelity deviation (Task 1 discovery): _FC3 is NOT a pure cache —
    # server._forecast3() reads _FC3["ts"] unconditionally BEFORE writing
    # (frontend/server.py:136), so clear() alone raises KeyError: 'ts'.
    # Re-seed the sentinel mapping (ts=0.0 → time-based cache always misses);
    # determinism and byte-equality are unchanged.
    fc3 = getattr(m, "_FC3", None)
    if isinstance(fc3, dict):
        fc3.update(ts=0.0, data=None)
    with mock.patch.object(m, "datetime", FrozenDT), \
         mock.patch("urllib.request.urlopen", side_effect=_router):
        return json.dumps(m.fetch_gridguard(), sort_keys=False,
                          ensure_ascii=False).encode()


def test_payload_byte_equal_to_golden():
    m = _payload_impl()
    out = _run_payload(m)
    golden = FIX / "payload_golden.json"
    if CAPTURE:
        golden.write_bytes(out)
        pytest.skip("golden captured")
    assert out == golden.read_bytes()


def test_page_bytes():
    m, attr = _page_impl_and_attr()
    page = getattr(m, attr)
    before = FIX / "page_before.txt"
    if CAPTURE:
        before.write_text(page)
        (FIX / "page_after_move.txt").write_text(
            page.replace('fetch("/api/gridguard")', 'fetch("api/gridguard")'))
        pytest.skip("page goldens captured")
    if attr == "GRIDGUARD_PAGE":                    # pre-move: verbatim
        assert page == before.read_text()
    else:                                           # post-move: exactly one rewrite
        assert page == (FIX / "page_after_move.txt").read_text()
