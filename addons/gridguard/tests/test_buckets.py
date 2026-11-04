"""GridGuard v4 round A — spec v4 §3/§4: night buckets (cluster-granular Wh),
quality classification truth table, CLEAN-only night need (round B decides)."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from gridguard.bms import BmsSource, _wh

TZ_NAME = "Europe/Madrid"
NOISE = 2.0                      # SOLAR_NOISE_A — the grid-on band
DAY0 = datetime(2026, 10, 1)     # all fixture nights sit WELL in the past


# ---------------------------------------------------------------- helpers ---
def _docs(stamps: list[datetime], cur=None, volt=13.1) -> list[dict]:
    """Fake ES hits: naive local stamps (treated as wall time in tz).
    Voltage defaults to the REAL 4S pack (~13.1 V) — owner doctrine: fixtures
    carry sensor values, never a textbook constant. volt=None = doc WITHOUT
    pack.voltage (the exclusion case)."""
    hits = []
    for ts in stamps:
        pack: dict = {}
        if cur is not None:
            pack["current"] = cur
        if volt is not None:
            pack["voltage"] = volt
        hits.append({"_source": {"@timestamp": ts.isoformat(), "pack": pack}})
    return hits


def _stamps(day: datetime, n: int, hour: int = 22) -> list[datetime]:
    """n consecutive local minutes starting `hour`:00 on `day`."""
    base = day.replace(hour=hour)
    return [base + timedelta(minutes=i) for i in range(n)]


def _source(hits: list[dict]) -> BmsSource:
    src = BmsSource("http://x", "i", ("v1",), "pack.current")
    src._search = lambda body, a, b, c: {"hits": {"hits": hits}}
    return src


# ------------------------------------------- item 1: _wh (the one formula) ---
def test_wh_helper_integrates_discharge_side_only():
    """U × I × dt, dt = 1/60 h: only I < 0 contributes (spec §3)."""
    assert _wh(-12.0, 13.1) == pytest.approx(12.0 * 13.1 / 60.0)   # 2.62 Wh/min
    assert _wh(0.0, 13.1) == 0.0            # grid-on minute → exactly 0
    assert _wh(5.0, 13.1) == 0.0            # charge (sun) → 0
    assert _wh(None, 13.1) == 0.0           # missing current → 0, never raises


# ------------------------------------- night labels, window, tz, key set ----
def test_default_window_label_boundaries_and_tz():
    """Default window [19:00, 08:00) local, label = MORNING date, half-open
    bounds (19:00 in, 08:00 out), aware docs converted to the local tz."""
    hits = _docs([
        datetime(2026, 10, 1, 16, 59, tzinfo=ZoneInfo("UTC")),   # 18:59 out
        datetime(2026, 10, 1, 17, 0, tzinfo=ZoneInfo("UTC")),    # 19:00 IN
        datetime(2026, 10, 1, 17, 30, tzinfo=ZoneInfo("UTC")),   # 19:30 in
        datetime(2026, 10, 2, 1, 0, tzinfo=ZoneInfo("UTC")),     # 03:00 in
        datetime(2026, 10, 2, 5, 59, tzinfo=ZoneInfo("UTC")),    # 07:59 in
        datetime(2026, 10, 2, 6, 0, tzinfo=ZoneInfo("UTC")),     # 08:00 OUT
        datetime(2026, 10, 2, 17, 30, tzinfo=ZoneInfo("UTC")),   # next night
    ], cur=-10.0)
    out = _source(hits).night_buckets(7, TZ_NAME, NOISE)
    assert [b["night"] for b in out] == ["2026-10-02", "2026-10-03"]
    assert [b["minutes"] for b in out] == [4, 1]        # boundary docs excluded
    assert out[0]["wh"] == pytest.approx(4 * 10 * 13.1 / 60.0)
    assert set(out[0]) == {"night", "wh", "minutes", "on_minutes", "status"}
    # both windows long past (2026-10) → SHORT, never OPEN
    assert [b["status"] for b in out] == ["SHORT", "SHORT"]


# ----------------------------------- classification truth table (spec §4) ---
def test_classification_truth_table_pinned_per_status():
    """One call, six nights — every §4 class pinned, precedence pinned:
    OPEN > SHORT > GRID_ON > MIXED > CLEAN. Window (22:00, 23:00) →
    expected = 60 min, SHORT threshold 0.6 × 60 = 36 docs."""
    w = (22.0, 23.0)
    hits: list[dict] = []
    hits += _docs(_stamps(datetime(2026, 10, 5), 60), cur=-10.0)        # CLEAN
    hits += _docs(_stamps(datetime(2026, 10, 6), 10), cur=0.0)           # 22:00..09
    hits += _docs(_stamps(datetime(2026, 10, 6), 60)[10:], cur=-10.0)    # 22:10..59
    hits += _docs(_stamps(datetime(2026, 10, 7), 40), cur=0.0)           # 22:00..39
    hits += _docs(_stamps(datetime(2026, 10, 7), 60)[40:], cur=-10.0)    # 22:40..59
    hits += _docs(_stamps(datetime(2026, 10, 8), 30), cur=-10.0)         # SHORT
    hits += _docs(_stamps(datetime(2026, 10, 9), 30), cur=0.0)           # SHORT>GRID_ON
    hits += _docs(_stamps(datetime(2099, 1, 4), 10), cur=-10.0)          # OPEN
    hits.reverse()                                   # input order irrelevant
    out = _source(hits).night_buckets(7, TZ_NAME, NOISE, window=w)
    assert [b["night"] for b in out] == [
        "2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08",
        "2026-10-09", "2099-01-04"]                   # chronological
    assert [b["status"] for b in out] == [
        "CLEAN",     # 60 docs full, 0 on-minutes
        "MIXED",     # 10/60 = 0.167 on → regime change in the window
        "GRID_ON",   # 40/60 = 0.667 on ≥ 0.6
        "SHORT",     # 30 < 36 docs — even though 0 on-minutes
        "SHORT",     # 30 < 36 docs WINS over on-ratio 1.0 → SHORT > GRID_ON
        "OPEN"]      # window end 2099-01-04 23:00 in the future → OPEN > SHORT
    assert [b["minutes"] for b in out] == [60, 60, 60, 30, 30, 10]
    assert [b["on_minutes"] for b in out] == [0, 10, 40, 0, 30, 0]
    assert out[0]["wh"] == pytest.approx(60 * 10 * 13.1 / 60.0)         # 131 Wh
    assert set(out[0]) == {"night", "wh", "minutes", "on_minutes", "status"}


# -------------------------------------- §3 integration math (per-minute) ----
def test_integration_discharge_only_157_2_wh():
    """Minute series: 60 × −12 A at the fixture voltage 13.1 → ≈157.2 Wh;
    grid-on minutes at 0 A count into on_minutes but add 0 Wh; a +5 A charge
    minute adds 0 Wh (discharge side only, spec §3). Window (21, 23)."""
    hits: list[dict] = []
    hits += _docs(_stamps(datetime(2026, 10, 12), 60, hour=22), cur=-12.0)
    hits += _docs(_stamps(datetime(2026, 10, 12), 15, hour=21), cur=0.0)
    hits += _docs(_stamps(datetime(2026, 10, 12), 20, hour=21)[15:], cur=5.0)
    out = _source(hits).night_buckets(7, TZ_NAME, NOISE, window=(21.0, 23.0))
    assert len(out) == 1
    b = out[0]
    assert b["night"] == "2026-10-12"
    assert b["wh"] == pytest.approx(157.2)          # 60 × 12 A × 13.1 V / 60
    assert b["minutes"] == 80 and b["on_minutes"] == 15
    assert b["status"] == "MIXED"                    # 15/80 = 0.1875 on


def test_voltage_missing_doc_is_excluded_never_invented():
    """Owner doctrine (2026-10-20): sensor values beat textbook constants —
    a doc WITHOUT pack.voltage is NOT a measured minute (no Wh, no minute,
    no on-minute; never a phantom25.6 V energy). A doc without CURRENT but
    with voltage counts a minute, adds no Wh, is not grid-on. And a night
    whose docs all lack voltage produces NO bucket at all (no invention)."""
    hits = _docs([datetime(2026, 10, 13, 22, 0)], cur=-12.0, volt=26.0) \
        + _docs([datetime(2026, 10, 13, 22, 1)], cur=-12.0, volt=None) \
        + _docs([datetime(2026, 10, 13, 22, 2)])        # pack without current (voltage present)
    out = _source(hits).night_buckets(7, TZ_NAME, NOISE, window=(22.0, 23.0))
    b = out[0]
    assert b["wh"] == pytest.approx(12 * 26.0 / 60)    # ONLY the voltage-carrying doc
    assert b["minutes"] == 2 and b["on_minutes"] == 0
    # systemic case: every doc voltage-less → nothing measured → no bucket
    blind = _docs(_stamps(datetime(2026, 10, 14), 30), cur=-12.0, volt=None)
    assert _source(blind).night_buckets(7, TZ_NAME, NOISE) == []


# ------------------------------------- paging: search_after + 15000 cap -----
def _night_hits(start: int, n: int) -> list[dict]:
    return [{"_source": {"@timestamp": (DAY0 + timedelta(minutes=start + i)).isoformat(),
                         "pack": {"current": -10.0, "voltage": 13.1}},
             "sort": [start + i]} for i in range(n)]


def _in_window_minutes(n: int) -> int:
    """Independent re-derivation: how many of the first n consecutive local
    minutes from DAY0 fall in the default [19:00, 08:00) window."""
    cnt = 0
    for i in range(n):
        t = DAY0 + timedelta(minutes=i)
        m = t.hour * 60 + t.minute
        if m >= 19 * 60 or m < 8 * 60:
            cnt += 1
    return cnt


def test_night_buckets_paginates_search_after_and_caps(capsys):
    src = BmsSource("http://x", "i", ("v1",), "pack.current")
    calls, bodies = 0, []

    def fake(body, a, b, c):
        nonlocal calls
        calls += 1
        bodies.append(body)
        if calls == 1:
            assert "search_after" not in body
            return {"hits": {"hits": _night_hits(0, 10000)}}
        if calls == 2:
            assert body["search_after"] == [9999]          # cursor = last sort
            return {"hits": {"hits": _night_hits(10000, 10000)}}
        return {"hits": {"hits": _night_hits(20000, 500)}}  # never reached

    src._search = fake
    out = src.night_buckets(7, TZ_NAME, NOISE)
    assert calls == 2                                       # capped before page 3
    # every fetched in-window minute landed in exactly one bucket (no drops)
    assert sum(b["minutes"] for b in out) == _in_window_minutes(20000)
    nights = [b["night"] for b in out]
    assert nights == sorted(nights) and len(nights) > 10
    assert bodies[0]["_source"] == ["@timestamp", "pack"]
    assert bodies[0]["query"] == {"range": {"@timestamp": {"gte": "now-10080m"}}}
    assert bodies[0]["size"] == 10000 and bodies[0]["sort"] == [{"@timestamp": "asc"}]
    assert "capped" in capsys.readouterr().err             # ONE note to stderr


def test_night_buckets_short_page_ends_without_cap(capsys):
    src = BmsSource("http://x", "i", ("v1",), "pack.current")
    calls = 0

    def fake(body, a, b, c):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"hits": {"hits": _night_hits(0, 10000)}}
        return {"hits": {"hits": _night_hits(10000, 700)}}   # natural end

    src._search = fake
    out = src.night_buckets(3, TZ_NAME, NOISE)
    assert calls == 2
    assert sum(b["minutes"] for b in out) == _in_window_minutes(10700)
    assert capsys.readouterr().err == ""                    # nothing dropped


# ---------------- solar-day boundaries (owner rule2026-10-22: one per day) ---
from datetime import date as _date
from gridguard.bms import BmsSource as _BS


def _boundary_source(first_iso: str, last_iso: str) -> _BS:
    """Fake whose boundary queries answer by sort direction."""
    src = _BS("http://x", "i", ("v1",), "pack.current")
    def _search(body, a, b, c):
        sort = list(body.get("sort", [{}])[0].values())
        direction = sort[0] if sort else "asc"
        ts = first_iso if direction == "asc" else last_iso
        return {"hits": {"hits": [{"_source": {"@timestamp": ts}}]}}
    src._search = _search
    return src


def test_solar_day_boundaries_one_pair_per_calendar_day():
    src = _boundary_source("2026-10-01T07:15:00+02:00",
                           "2026-10-01T18:33:00+02:00")
    sun = lambda d: (datetime(2026, 10, 1, 8, 30), datetime(2026, 10, 1, 19, 30))
    bd = src.solar_day_boundaries([_date(2026, 10, 1)], "Europe/Madrid", 3.0, sun)
    assert list(bd) == [_date(2026, 10, 1)]
    start, end = bd[_date(2026, 10, 1)]
    assert (start.hour, start.minute) == (7, 15)
    assert (end.hour, end.minute) == (18, 33)


def test_solar_day_boundaries_requires_sun_and_both_hits():
    calls = {"n": 0}
    src = _boundary_source("2026-10-02T07:15:00+02:00",
                           "2026-10-02T18:33:00+02:00")
    no_sun = lambda d: None                    # unknown sun times → no day
    empty_src = _BS("http://x", "i", ("v1",), "pack.current")
    empty_src._search = lambda body, a, b, c: {"hits": {"hits": []}}
    assert src.solar_day_boundaries([_date(2026, 10, 2)],
                                    "Europe/Madrid", 3.0, no_sun) == {}
    assert empty_src.solar_day_boundaries([_date(2026, 10, 2)],
                                          "Europe/Madrid", 3.0,
                                          lambda d: (None, None)) == {}
    # no hits (sunless day: nothing ≥ FLOW_A) → NO pair, never a partial day
    assert empty_src.solar_day_boundaries(
        [_date(2026, 10, 2)], "Europe/Madrid", 3.0,
        lambda d: (datetime(2026, 10, 2, 8, 30), datetime(2026, 10, 2, 19, 30))
    ) == {}


def test_solar_day_boundaries_naive_sun_stamps_carry_boundary_tz_offset():
    """F5-1: open-meteo emits NAIVE local wall-time sun stamps. The range
    filter must carry the boundary tz offset — zone-less ISO is read as UTC
    by the cluster, which would shift the whole sun window by the local
    offset (~2 h). Body assertions against a capturing fake (last body)."""
    src = _BS("http://x", "i", ("v1",), "pack.current")
    bodies: list[dict] = []

    def _search(body, a, b, c):
        bodies.append(body)
        direction = list(body["sort"][0].values())[0]
        ts = ("2026-10-01T07:15:00+02:00" if direction == "asc"
              else "2026-10-01T18:33:00+02:00")
        return {"hits": {"hits": [{"_source": {"@timestamp": ts}}]}}

    src._search = _search
    sun = lambda d: (datetime(2026, 10, 1, 8, 30),      # NAIVE (open-meteo)
                     datetime(2026, 10, 1, 19, 30))
    bd = src.solar_day_boundaries([_date(2026, 10, 1)], TZ_NAME, 3.0, sun)
    assert list(bd) == [_date(2026, 10, 1)]              # the pair still lands

    body = bodies[-1]                                    # LAST captured body
    assert body["size"] == 1                             # boundary-query doctrine
    flt = body["query"]["bool"]["filter"]
    assert {"range": {"@timestamp": {
        "gte": "2026-10-01T08:30:00+02:00",              # offset present: naive
        "lte": "2026-10-01T19:30:00+02:00"}}} in flt     # = LOCAL wall time
    assert {"range": {"pack.current": {"gte": 3.0}}} in flt


# ------------- spans mode: integration over the solar-night windows ---------
from datetime import datetime as _dt2, timedelta as _td


def _spans_source(hits):
    src = _BS("http://x", "i", ("v1",), "pack.current")
    src._search = lambda body, a, b, c: {"hits": {"hits": hits}}
    return src


def test_night_buckets_spans_assign_by_window_not_clock():
    # night labeled 2026-10-03 spans (Oct 2 18:33, Oct 3 09:13)
    spans = {_date(2026, 10, 3): (datetime(2026, 10, 2, 18, 33),
                                  datetime(2026, 10, 3, 9, 13))}
    hits = _docs([
        datetime(2026, 10, 2, 19, 0),     # IN the solar night
        datetime(2026, 10, 2, 23, 0),     # IN
        datetime(2026, 10, 3, 9, 0),      # IN (before 09:13 start)
        datetime(2026, 10, 3, 9, 13),     # OUT — half-open end = day start
        datetime(2026, 10, 3, 12, 0),     # OUT — daytime
    ], cur=-10.0)
    out = _spans_source(hits).night_buckets(7, TZ_NAME, NOISE, spans=spans)
    assert len(out) == 1
    b = out[0]
    assert b["night"] == "2026-10-03"
    assert b["minutes"] == 3               # only the three in-window minutes
    assert b["status"] in ("SHORT", "CLEAN", "GRID_ON", "MIXED", "OPEN")


def test_night_buckets_spans_expected_minutes_from_span():
    # a SHORT night is judged against ITS OWN span length, not a clock window
    spans = {_date(2026, 10, 3): (datetime(2026, 10, 2, 18, 0),
                                  datetime(2026, 10, 3, 9, 0))}          # 900 min
    hits = _docs([datetime(2026, 10, 2, 22, i) for i in range(60)],
                 cur=-10.0)                     # only 60 of 900 minutes
    out = _spans_source(hits).night_buckets(7, TZ_NAME, NOISE, spans=spans)
    assert out[0]["minutes"] == 60
    assert out[0]["status"] == "SHORT"          # 60 < 0.6 * 900


# ---------------- L1 yield: measured charge energy + forecast doc -----------
def test_day_charge_kwh_cluster_agg_positive_only():
    src = _BS("http://x", "i", ("v1",), "pack.current")
    captured = {}

    def _search(body, a, b, c):
        captured.update(body)
        # canned: Σ U×I over the window → applied factor below (R1 ruling)
        return {"aggregations": {"e": {"value": 64800.0}}}
    src._search = _search
    out = src.day_charge_kwh(datetime(2026, 10, 21, 9, 13),
                             datetime(2026, 10, 21, 18, 35))
    assert out == pytest.approx(1.08)          # 64800 × 60 / 3.6e6
    flt = captured["query"]["bool"]["filter"]
    assert {"range": {"pack.current": {"gte": 0}}} in flt   # charge-only
    assert "aggs" in captured and "e" in captured["aggs"]


def test_forecast_doc_for_day_uses_ship_day():
    src = _BS("http://x", "i", ("v1",), "pack.current")
    got = {}

    def _search(body, a, b, c):
        got.update(body)
        return {"hits": {"hits": [{"_source": {
            "ts": "2026-10-21T09:10:00+00:00",
            "radiation_sum_j": 16_452_000.0, "kind": "forecast"}}]}}
    src._search = _search
    doc = src.forecast_doc_for(_date(2026, 10, 21), "Europe/Madrid")
    assert doc["radiation_sum_j"] == 16_452_000.0
    # earliest ex-ante ship of that local day, radiation not null
    flt = got["query"]["bool"]["filter"]
    assert {"term": {"kind": "forecast"}} in flt
    assert {"range": {"radiation_sum_j": {"gte": 0}}} in flt
    ts_gte = next(f["range"]["ts"]["gte"] for f in flt
                  if "range" in f and "ts" in f["range"])
    assert ts_gte.startswith("2026-10-21T00")   # R2: LOCAL day start iso
