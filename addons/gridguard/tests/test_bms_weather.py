import json
from datetime import datetime, timedelta
from gridguard.bms import BmsReading, BmsSource, dig
from gridguard.weather import WeatherClient

SRC = {"@timestamp": "2026-10-18T20:00:00+00:00",
       "cells": {"v1": 3.21, "v2": 3.19, "v3": 3.30, "delta_mv": 110},
       "pack": {"current": -1.75, "voltage": 13.1}, "soc": {"soc": 64.0}}

def test_dig_and_reading_parse():
    assert dig({"pack": {"current": -1.75}}, "pack.current") == -1.75
    assert dig({}, "pack.current") is None
    def fake_search(body, os_url, index, timeout):
        return {"hits": {"hits": [{"_source": SRC}]}}
    src = BmsSource("http://x", "i", ("v1", "v2", "v3"), "pack.current")
    src._search = fake_search
    r = src.latest()
    assert r.min_cell_v == 3.19 and r.max_cell_v == 3.30 and r.charge_a == -1.75 and r.soc == 64.0
    assert r.pack_v == 13.1                                # LIVE pack voltage (owner doctrine)

def test_samples_sorted_ascending_and_min_per_doc():
    docs = [
        {"@timestamp": "2026-10-18T19:00:00", "cells": {"v1": 3.3, "v2": 3.25}},
        {"@timestamp": "2026-10-18T18:00:00", "cells": {"v1": 3.35, "v2": 3.30}},
    ]
    def fake_search(body, os_url, index, timeout):
        return {"hits": {"hits": [{"_source": d} for d in docs]}}
    src = BmsSource("http://x", "i", ("v1", "v2"), "pack.current")
    src._search = fake_search
    s = src.samples_minutes(120)
    assert s == [(datetime(2026, 10, 18, 18, 0), 3.30),
                 (datetime(2026, 10, 18, 19, 0), 3.25)]

def test_weather_advisory_never_raises():
    def boom(*a, **k):
        raise OSError("net down")
    w = WeatherClient(37.0, -3.0)
    w._get = boom
    assert w.morning_radiation_j() is None

def test_weather_parses_sum():
    w = WeatherClient(37.0, -3.0)
    # open-meteo raw value = MJ/m²/day ([5.2] = 5.2 MJ) → weather.py converts ×1e6 to J
    w._get = lambda url: {"daily": {"time": ["2026-10-18"], "shortwave_radiation_sum": [5.2]}}
    assert w.morning_radiation_j() == 5.2e6

def test_reading_remaining_ah():
    src2 = BmsSource("http://x", "i", ("v1", "v2"), "pack.current")
    def fake(body, os_url, index, timeout):
        return {"hits": {"hits": [{"_source": {
            "@timestamp": "2026-10-18T10:00:00+00:00",
            "cells": {"v1": 3.2, "v2": 3.1}, "pack": {"current": 8.5},
            "soc": {"soc": 64.0, "remaining_ah": 5.3, "full_ah": 222.0}}}]}}
    src2._search = fake
    r = src2.latest()
    assert r.remaining_ah == 5.3 and r.soc == 64.0 and r.charge_a == 8.5

def test_samples_window_fields_and_order():
    src2 = BmsSource("http://x", "i", ("v1", "v2"), "pack.current")
    docs = [
        {"@timestamp": "2026-10-18T11:00:00+00:00",
         "cells": {"v1": 3.2}, "pack": {"current": -11.8}, "soc": {"remaining_ah": 9.9, "soc": 3.1}},
        {"@timestamp": "2026-10-18T10:00:00+00:00",
         "cells": {"v1": 3.25}, "pack": {"current": 0.1}, "soc": {"remaining_ah": 12.0, "soc": 3.8}},
    ]
    src2._search = lambda body, a, b, c: {"hits": {"hits": [{"_source": d} for d in docs]}}
    out = src2.samples_window(120)
    assert [s.min_cell_v for s in out] == [3.25, 3.2]          # ascending
    assert [s.pack_current for s in out] == [0.1, -11.8]
    assert [s.remaining_ah for s in out] == [12.0, 9.9]
    assert out[0].soc_pct == 3.8

def test_samples_window_missing_fields_are_none():
    src2 = BmsSource("http://x", "i", ("v1",), "pack.current")
    src2._search = lambda body, a, b, c: {"hits": {"hits": [
        {"_source": {"@timestamp": "2026-10-18T10:00:00Z"}}]}}
    out = src2.samples_window(60)
    assert out[0].pack_current is None and out[0].remaining_ah is None and out[0].soc_pct is None
    assert out[0].min_cell_v is None


# --- v3: samples_days (learner feed — search_after pagination + 15000 cap) -----
DAY0 = datetime(2026, 10, 1)


def _day_hits(start: int, n: int) -> list[dict]:
    return [{"_source": {"@timestamp": (DAY0 + timedelta(minutes=start + i)).isoformat(),
                         "cells": {"v1": 3.2}, "pack": {"current": -11.0},
                         "soc": {"remaining_ah": 40.0, "soc": 50.0}},
             "sort": [start + i]} for i in range(n)]


def test_samples_days_paginates_search_after_and_caps(capsys):
    src = BmsSource("http://x", "i", ("v1",), "pack.current")
    calls, bodies = 0, []

    def fake(body, a, b, c):
        nonlocal calls
        calls += 1
        bodies.append(body)
        if calls == 1:
            assert "search_after" not in body
            return {"hits": {"hits": _day_hits(0, 10000)}}
        if calls == 2:
            assert body["search_after"] == [9999]          # cursor = last hit's sort value
            return {"hits": {"hits": _day_hits(10000, 10000)}}
        return {"hits": {"hits": _day_hits(20000, 500)}}    # short page: never reached

    src._search = fake
    out = src.samples_days(7)
    assert calls == 2                                       # capped BEFORE the short page
    assert len(out) == 20000                                # two full pages, concat order
    assert out[0].ts == DAY0 and out[9999].ts == DAY0 + timedelta(minutes=9999)
    assert out[10000].ts == DAY0 + timedelta(minutes=10000)  # page2 continues seamlessly
    assert out[-1].ts == DAY0 + timedelta(minutes=19999)
    assert all(out[i].ts <= out[i + 1].ts for i in range(0, 19999, 997))
    assert bodies[0]["query"] == {"range": {"@timestamp": {"gte": "now-10080m"}}}
    assert bodies[0]["size"] == 10000 and bodies[0]["sort"] == [{"@timestamp": "asc"}]
    assert "capped" in capsys.readouterr().err              # cap-note once, to stderr


def test_samples_days_short_page_ends_without_cap(capsys):
    src = BmsSource("http://x", "i", ("v1",), "pack.current")
    calls = 0

    def fake(body, a, b, c):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"hits": {"hits": _day_hits(0, 10000)}}
        return {"hits": {"hits": _day_hits(10000, 700)}}    # natural end, short page

    src._search = fake
    out = src.samples_days(3)
    assert calls == 2 and len(out) == 10700
    assert out[10000].ts == DAY0 + timedelta(minutes=10000)
    assert capsys.readouterr().err == ""                    # nothing dropped → no note
