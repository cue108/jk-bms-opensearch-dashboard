"""Restart-window safety (owner incident 2026-10-25 09:37:39 UTC): the
reserve-tier decision IS durable in the events index (event reserve_tier,
inputs {tier_pct, triggers, lookahead_days, outlook_ts, reference_kwh_m2,
release_hyst_pct}) — only the in-memory latch forgets it on every restart
(a deploy reset st.reserve_tier_pct to 0 and the first tick opened the
release gate → day-drain switch_off while the owner-ordered tier was 30 %).

BmsSource asks for the NEWEST reserve_tier event: ONE bounded filter+sort
query (size 1, sort ts desc — no scan) on the events index, as
day_grid_on_pct establishes it. Return = the inputs dict of the newest doc;
honest None on query error, no docs or no inputs — never a
fabricated 0."""
from gridguard.bms import BmsSource

INPUTS = {"tier_pct": 30, "triggers": ["weather_code>=95"],
          "lookahead_days": 3, "outlook_ts": "2026-10-24T21:00:00",
          "reference_kwh_m2": 4.444, "release_hyst_pct": 2}


def _src(hits: list[dict]) -> BmsSource:
    src = BmsSource("http://x", "i", ("v1",), "pack.current")
    src._search = lambda body, a, b, c: {"hits": {"hits": hits}}
    return src


def _doc(inputs=INPUTS, ts="2026-10-24T21:00:00+02:00") -> dict:
    return {"_source": {"ts": ts, "event": "reserve_tier", "inputs": inputs}}


def test_latest_reserve_tier_returns_the_inputs_of_the_newest_event():
    """The newest reserve_tier doc delivers ITS inputs dict (tier_pct, why,
    outlook_ts …) — exactly what the seed reloads into the latch."""
    assert _src([_doc()]).latest_reserve_tier() == INPUTS


def test_latest_reserve_tier_query_is_bounded_size1_sort_desc_on_events_index():
    """Doctrine: ONE bounded query — size 1, sort ts desc, term event
    "reserve_tier", _source only ["ts", "inputs"] — against the events index."""
    got: dict = {}

    def _search(body, os_url, index, timeout):
        got.update(body)
        got["index"] = index
        return {"hits": {"hits": []}}
    src = BmsSource("http://x", "i", ("v1",), "pack.current")
    src._search = _search
    assert src.latest_reserve_tier() is None            # no docs → None
    assert got["index"] == "gridguard-events-*"         # cfg prefix mirror
    assert got["size"] == 1 and got["sort"] == [{"ts": "desc"}]
    assert got["query"]["bool"]["filter"] == [{"term": {"event": "reserve_tier"}}]
    assert got["_source"] == ["ts", "inputs"]


def test_latest_reserve_tier_no_docs_returns_none():
    assert _src([]).latest_reserve_tier() is None


def test_latest_reserve_tier_query_failure_returns_none():
    def boom(body, a, b, c):
        raise OSError("cluster down")
    src = BmsSource("http://x", "i", ("v1",), "pack.current")
    src._search = boom
    assert src.latest_reserve_tier() is None            # honest None, never 0


def test_latest_reserve_tier_doc_without_inputs_returns_none():
    """Doc without inputs dict → None (no guessed latch fodder)."""
    assert _src([_doc(inputs=None)]).latest_reserve_tier() is None
    assert _src([{"_source": {"ts": "2026-10-24T21:00:00+02:00",
                              "event": "reserve_tier"}}]).latest_reserve_tier() is None
