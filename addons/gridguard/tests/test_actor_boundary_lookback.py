"""Owner bug order (2026-11-07): the FIRST (oldest) period-history row
always rendered a leading ``unlabelled`` slice in the ACTOR column.

Root cause (structural, not data): every state-evidence query opened at
``hist_start`` (the fixed 06:00 anchor, 7 days back) — the same instant the
period window opens. The state that LABELS the oldest row's start (the
pre-dawn program fire ≈05:31 / the previous eve's switch) sits just BEHIND
that anchor, so ``state_era`` always landed mid-row and the leading slice
had no covering evidence → "unlabelled", on the first row, every render.

The fix extends the EVIDENCE lookback behind the horizon (periods, goldens
and every other consumer stay untouched). "Unlabelled" remains the honest
answer only when no evidence exists in the extended window either — the
never-inferred-OFF rule is preserved, only its (buggy) window boundary
moves.

The fake ES below honors the query's own ``ts >= gte`` range filter, so the
test fails iff the evidence window still opens at/after the horizon.
"""
from datetime import datetime, timezone
import json
from unittest import mock

import gridguard.web_payload as wp

UTC = timezone.utc
NOW = datetime(2026, 11, 7, 12, 0, 0, tzinfo=UTC)   # hour ≥ GG_SOLARDAY_H(6)
HIST_START = "2026-10-31T06:00:00+00:00"             # NOW−7d at the 06:00 anchor
FIRE_TS = "2026-10-30T19:30:00+00:00"                # eve write, pre-horizon
FIRE_AT = "2026-10-31T05:31"                         # fires BEFORE hist_start
OFF_TS = "2026-10-31T09:11:00+00:00"                 # first in-horizon switch


class FrozenDT(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz) if tz else NOW


def _env():
    import os
    os.environ["OPENSEARCH_URL"] = "http://stub:9200"
    os.environ["INDEX"] = "bms-test-*"
    os.environ["CHARTS_TZ"] = "UTC"
    os.environ.setdefault("GG_LAT", "36.76")
    os.environ.setdefault("GG_LON", "-4.19")


class _Resp:
    def __init__(self, obj):
        self._b = json.dumps(obj).encode()
    def read(self):
        return self._b
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
    status, headers = 200, {}


def _gte_of(body):
    """Smallest ``ts >= gte`` bound found anywhere in the query (the fake
    ES applies it — the whole point of this test)."""
    best = None
    def walk(x):
        nonlocal best
        if isinstance(x, dict):
            for k, v in x.items():
                if k == "range" and isinstance(v, dict) and "ts" in v:
                    g = (v["ts"] or {}).get("gte")
                    if g and (best is None or g < best):
                        best = g
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(body)
    return best


def _keep(ev, body):
    gte = _gte_of(body)
    return gte is None or ev["ts"] >= gte


_EVENTS = [
    # tier-1 write-confirmed generation: written on the eve, fires pre-dawn
    {"ts": FIRE_TS, "event": "act", "action": {"type": "failsafe_timer"},
     "timer_action": "ensure_on", "result": "ok",
     "inputs": {"fire_str": "05:31", "at_local": FIRE_AT}},
    # the first in-horizon state change
    {"ts": OFF_TS, "event": "act", "action": {"type": "switch_off"},
     "timer_action": "", "result": "ok", "inputs": {}},
]


def _search_response(url, body):
    if "gridguard-events" in url:
        s = json.dumps(body)
        if '"match_all"' in s:                       # liveness timeline
            return {"hits": {"hits": []}}
        if "ensure_on" in s:                         # tier-1 generations
            hits = [{"_source": e} for e in _EVENTS
                    if e["timer_action"] == "ensure_on" and _keep(e, body)]
            return {"hits": {"hits": hits}}
        if "action.type" in s:                       # switch/journal acts
            hits = [{"_source": e} for e in _EVENTS
                    if e["action"]["type"] in ("switch_on", "switch_off")
                    and _keep(e, body)]
            return {"hits": {"hits": hits}}
        return {"hits": {"hits": []}}                 # plan/push/tier queries
    if "gridguard-weather" in url:
        return {"hits": {"hits": []}}
    # BMS index: latest doc
    return {"hits": {"hits": []}}


def _msearch_response(data):
    lines = [l for l in data.decode().splitlines() if l.strip()]
    bodies = [json.loads(l) for i, l in enumerate(lines) if i % 2 == 1]
    out = []
    for b in bodies:
        s = json.dumps(b)
        if b.get("size") == 0:                        # energy aggregates
            out.append({"hits": {"hits": []},
                        "aggregations": {"e": {"value": None}}})
        elif "pack.current" in s:                     # solar-day boundary job
            gte = None
            for f in (b.get("query", {}).get("bool", {}).get("filter") or []):
                r = f.get("range", {}).get("@timestamp", {})
                if r.get("gte"):
                    gte = r["gte"]
            direction = (b.get("sort") or [{}])[0].get("@timestamp", "asc")
            ts = None
            if gte and gte.startswith("2026-10-31"):
                ts = ("2026-10-31T08:34:00+00:00" if direction == "asc"
                      else "2026-10-31T16:36:00+00:00")
            out.append({"hits": {"hits": (
                [{"_source": {"@timestamp": ts}}] if ts else [])}})
        else:                                         # plan/tier/era queries
            out.append({"hits": {"hits": []}})
    return {"responses": out}


def _router(req, timeout=None):
    url = getattr(req, "full_url", str(req))
    if "open-meteo.com" in url:
        day = url.split("start_date=")[-1].split("&")[0]
        return _Resp({"daily": {"time": [day],
                                "sunrise": [f"{day}T07:00:00"],
                                "sunset": [f"{day}T17:30:00"]},
                      "daily_units": {}})
    data = getattr(req, "data", None) or b""
    if url.endswith("/_msearch"):
        return _Resp(_msearch_response(data))
    if data:
        return _Resp(_search_response(url, json.loads(data.decode())))
    return _Resp({"hits": {"hits": []}})


def _run():
    _env()
    for cache in ("_FC3", "_SUN_CACHE"):
        c = getattr(wp, cache, None)
        if isinstance(c, dict):
            c.clear()
    fc3 = getattr(wp, "_FC3", None)
    if isinstance(fc3, dict):
        fc3.update(ts=0.0, data=None)
    with mock.patch.object(wp, "datetime", FrozenDT), \
         mock.patch("urllib.request.urlopen", side_effect=_router):
        return wp.fetch_gridguard()


def test_oldest_history_row_starts_labelled_not_unlabelled():
    """The first row's leading slice must be covered by the REAL pre-horizon
    evidence (the05:31 fire → ON since05:31), not rendered "unlabelled"."""
    payload = _run()
    rows = [r for r in payload["history"] if r["from"].startswith("2026-10-31T08:34")]
    assert rows, [r["from"] for r in payload["history"]]
    actor = rows[0]["actor"]
    assert actor, "row must carry an actor timeline"
    assert all(seg["s"] != "unknown" for seg in actor), \
        f"leading slice still unlabelled: {actor}"
    first = actor[0]
    assert first["s"] == "on"
    assert first["from"].startswith("2026-10-31T08:34")
    assert first["to"].startswith("2026-10-31T09:11")
    assert first.get("since", "").startswith("2026-10-31T05:31")
    assert len(actor) == 2 and actor[1]["s"] == "off"


def test_evidence_window_opens_behind_the_horizon_anchor():
    """Query-level pin: the state-evidence searches must ask for events from
    BEFORE hist_start (the fake ES enforces the range — the behavioral test
    above then proves the data flows through)."""
    seen = []
    real_search = wp._gg_search

    def spy(index, body):
        if "gridguard-events" in str(index):
            seen.append(body)
        return real_search(index, body)

    _env()
    with mock.patch.object(wp, "datetime", FrozenDT), \
         mock.patch.object(wp, "_gg_search", side_effect=spy), \
         mock.patch("urllib.request.urlopen", side_effect=_router):
        wp.fetch_gridguard()
    # The two state-evidence searches (switch/journal acts + tier-1
    # generations) are the size-400 queries; plan/verdict lookups share the
    # "action.type" string but carry no ts-range and are out of scope here.
    ev_bodies = [b for b in seen if b.get("size") == 400]
    assert ev_bodies, "state-evidence queries not issued"
    for b in ev_bodies:
        gte = _gte_of(b)
        assert gte is not None and gte < HIST_START, \
            f"evidence window still opens at/after the horizon: {gte}"
