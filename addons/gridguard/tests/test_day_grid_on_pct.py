"""L1 per-class yield (owner ruling 2026-10-25, option B): the grid-ON share
of a measured solar day, reconstructed from the addon's OWN events-index
labels (act switch_on/switch_off + the state_observed journal).

Only the LEARNING becomes state-dependent — the solar-day threshold stays
curve-based (Owner-Ruling 2026-10-21, unchanged). A failing Share-Query is an
honest None (never a fabricated 0)."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from gridguard.bms import BmsSource

TZ = ZoneInfo("Europe/Madrid")
START = datetime(2026, 10, 20, 9, 0, tzinfo=TZ)      # coupled time window
END = datetime(2026, 10, 20, 15, 0, tzinfo=TZ)       # 360 minutes


def _src(hits: list[dict]) -> BmsSource:
    src = BmsSource("http://x", "i", ("v1",), "pack.current")
    src._search = lambda body, a, b, c: {"hits": {"hits": hits}}
    return src


def _ev(ts: datetime, doc: dict) -> dict:
    return {"_source": {"ts": ts.isoformat(), **doc}}


def _act(ts, typ):
    return _ev(ts, {"event": "act", "action": {"type": typ}})


def _obs(ts, **inputs):
    return _ev(ts, {"event": "state_observed", "inputs": inputs})


def test_day_grid_on_pct_seeds_opening_from_doc_before_start_and_clips():
    """A label BEFORE the window (process baseline 08:30) sets the opening
    state; a late act (16:00, after `end`) must not count — only the ON share
    clipped to [start, end] counts."""
    hits = [_obs(datetime(2026, 10, 20, 8, 30, tzinfo=TZ), to="on"),   # seed ON
            _act(datetime(2026, 10, 20, 12, 0, tzinfo=TZ), "switch_off"),
            _act(datetime(2026, 10, 20, 16, 0, tzinfo=TZ), "switch_on")]  # after end
    assert _src(hits).day_grid_on_pct(START, END) == pytest.approx(50.0)
    # 09:00→12:00 ON = 180 of 360 minutes


def test_day_grid_on_pct_transition_marker_and_unknown_opening():
    """inputs.from marks state_observed as TRANSITION (state before the doc);
    a first doc AFTER start leaves the opening segment unknown — it does NOT
    count as ON (compute honestly what exists, never guess)."""
    hits = [_obs(datetime(2026, 10, 20, 9, 20, tzinfo=TZ), **{"from": "off", "to": "on"}),
            _act(datetime(2026, 10, 20, 9, 50, tzinfo=TZ), "switch_off")]
    # 09:00–09:20 unknown, 09:20–09:50 ON = 30 of 60 minutes
    assert _src(hits).day_grid_on_pct(START, START + timedelta(hours=1)) \
        == pytest.approx(50.0)


def test_day_grid_on_pct_full_window_labels():
    """Full truth in both directions: ON throughout = 100, a marked OFF
    window = a REAL 0 (not an error)."""
    on = [_obs(datetime(2026, 10, 20, 8, 0, tzinfo=TZ), to="on")]
    off = [_obs(datetime(2026, 10, 20, 9, 0, tzinfo=TZ), to="off")]
    s = _src(on)
    assert s.day_grid_on_pct(START, END) == pytest.approx(100.0)
    s2 = _src(off)
    assert s2.day_grid_on_pct(START, END) == pytest.approx(0.0)


def test_day_grid_on_pct_query_is_bounded_filter_sort_on_events_index():
    """Doctrine: ONE bounded filter+sort query (size ~50, no scan) against the
    events index — range = the window, doc filter = act switch_on/switch_off
    OR state_observed, sort ts asc."""
    got: dict = {}

    def _search(body, os_url, index, timeout):
        got.update(body)
        got["index"] = index
        return {"hits": {"hits": []}}
    src = BmsSource("http://x", "i", ("v1",), "pack.current")
    src._search = _search
    assert src.day_grid_on_pct(START, END) is None          # no docs → None
    assert got["index"] == "gridguard-events-*"             # cfg prefix mirror
    assert got["size"] == 50 and got["sort"] == [{"ts": "asc"}]
    flt = got["query"]["bool"]["filter"]
    assert {"range": {"ts": {"gte": START.isoformat(),
                             "lte": END.isoformat()}}} in flt
    should = flt[1]["bool"]["should"]
    assert {"bool": {"filter": [{"term": {"event": "act"}},
                                {"terms": {"action.type": ["switch_on",
                                                           "switch_off"]}}]}} in should
    assert {"term": {"event": "state_observed"}} in should


def test_day_grid_on_pct_query_failure_returns_none():
    def boom(body, a, b, c):
        raise OSError("cluster down")
    src = BmsSource("http://x", "i", ("v1",), "pack.current")
    src._search = boom
    assert src.day_grid_on_pct(START, END) is None          # honest None, never 0


def test_day_grid_on_pct_unparseable_docs_return_none():
    """Docs in the index, but NO parseable label → None (no fabricated
    grid_off day just because the parsers understood nothing)."""
    hits = [_ev(datetime(2026, 10, 20, 10, 0, tzinfo=TZ), {"event": "tick"})]
    assert _src(hits).day_grid_on_pct(START, END) is None


def test_day_grid_on_pct_degenerate_window_returns_none():
    hits = [_obs(datetime(2026, 10, 20, 9, 0, tzinfo=TZ), to="on")]
    assert _src(hits).day_grid_on_pct(START, START) is None  # total <= 0
