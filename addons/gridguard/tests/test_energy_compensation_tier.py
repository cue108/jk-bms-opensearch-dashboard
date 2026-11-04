"""Candidate F (owner ruling §14, 2026-10-26): ENERGY-COMPENSATION TIER GATE.

The owner compensation rule is "known grid-ON ⇒ battery-out is withheld from
the net". Its own premise is KNOWN — so it applies ONLY to windows whose ON
boundary is known evidence:

  (i)   withholding applies to KNOWN windows = observed acts (switch acts /
        journal state_observed) OR write-confirmed tier-1 fires;
  (ii)  under tier-2 MEASURED (fill-derived) ON windows the raw net stays
        VISIBLE in kwh — the withheld cell DISCLOSES the in-window discharge
        with a tier marker instead of pretending it never happened
        (disclosure is never re-added into kwh = never double-counted);
  (iii) OBSERVED windows produce IDENTICAL numbers to before (the Oct20/21
        held 0.006 shape = regression pin);
  (iv)  the actor column / ruling #10 code is untouched — energy layer only;
  (v)   tonight's tier-1 write-confirmed windows = withhold (pinned).

Forensic context (cluster, host50 — same run): the Oct23 fill instant
07:56Z was NOT an observed switch — the journal holds only ticks there; the
07:56–07:58 +6.6..7.3 A step flickered back to −7.14 A (07:59) before
sustained charging from 08:03, i.e. the solar-day ramp edge (day_bounds
start = the first ≥3 A sample) read as a grid-ON run start. The old code
then WITHHELD a real 1.909 kWh of battery-out (raw counter net −1.891) under
that inferred label — exactly what this wave discloses instead.
"""
import json
from datetime import datetime, timezone
from unittest import mock

import pytest

import gridguard.web_payload as wp
from gridguard.web_page import PAGE

UTC = timezone.utc
FREEZE = datetime(2026, 10, 26, 12, 0, 0, tzinfo=UTC)   # frozen "now"


class FrozenDT(datetime):                                # arithmetic-safe
    @classmethod
    def now(cls, tz=None):
        return FREEZE.astimezone(tz) if tz else FREEZE


# per-test stub answers ------------------------------------------------------
_STUB: dict = {}

# one virtual week: solar days Oct22..Oct26 (site tz = UTC in tests)
_SUN = {f"2026-10-{d}": (f"2026-10-{d}T07:10", f"2026-10-{d}T18:10")
        for d in ("19", "20", "21", "22", "23", "24", "25", "26")}
_BOUNDS = {                                            # measured first/last ≥3A
    "2026-10-22": ("2026-10-22T07:18:00+00:00", "2026-10-22T16:31:00+00:00"),
    "2026-10-23": ("2026-10-23T07:56:00+00:00", "2026-10-23T16:29:00+00:00"),
    "2026-10-24": ("2026-10-24T07:26:00+00:00", "2026-10-24T16:19:00+00:00"),
    "2026-10-25": ("2026-10-25T07:23:00+00:00", "2026-10-25T16:29:00+00:00"),
    "2026-10-26": ("2026-10-26T08:02:00+00:00", "2026-10-26T11:31:00+00:00"),
}
_F = 60.0 / 3.6e6                                      # GG_SAMPLE_S → kWh


def _kwh_to_val(k):
    """kWh → the W·sample scripted_metric sum that yields it."""
    return k / _F


# window start instants (the classification keys of this wave)
_A = "2026-10-22T20:00:00+00:00"    # KNOWN  (switch_on act)
_B = "2026-10-23T07:56:00+00:00"    # MEASURED (the fill — forensic 07:56Z)
_E = "2026-10-24T10:00:00+00:00"    # KNOWN  (switch_on act — mixed row)
_C = "2026-10-26T07:16:00+00:00"    # KNOWN  (tier-1 write-confirmed fire)
_KNOWN = (_A, _E, _C)               # chronological, as the should-list builds
_MEAS = (_B,)

# period keys (history rows) that carry registered energy values
_P2_N = ("2026-10-22T16:31:00+00:00", "2026-10-23T07:56:00+00:00")
_P3_D = ("2026-10-23T07:56:00+00:00", "2026-10-23T16:29:00+00:00")
_P4_N = ("2026-10-23T16:29:00+00:00", "2026-10-24T07:26:00+00:00")
_P5_D = ("2026-10-24T07:26:00+00:00", "2026-10-24T16:19:00+00:00")
_P8_N = ("2026-10-25T16:29:00+00:00", "2026-10-26T08:02:00+00:00")


def _week_agg():
    """The pinned energy world of the week above (kWh, wire semantics: tot =
    the signed raw net; held/meas = the DISCHARGE-ONLY cluster sums, which
    are ≤ 0 exactly like ENERGY_AGG returns them — _sum negates them).

    KNOWN window A (night Oct22→23):  raw −2.433, discharge-in-A 0.006
        → withheld → kwh −2.427          (the Oct20/21 observed shape);
    MEASURED window B (fill 07:56Z):   raw stays visible, discharge disclosed
        day Oct23:   raw 1.533, in-B 0.096
        night Oct23→24: raw −1.891, in-B 1.909   ← the §14 finding;
    mixed row (day Oct24: measured B tail + known E):
        raw 1.803, known-E 0.050, measured-B 0.010;
    tier-1 window C (write-confirmed fire 07:16, night Oct25→26):
        raw −2.271, in-C 0.100 → withheld.
    """
    return {
        ("tot", _P2_N, None): -2.433,
        ("held", _P2_N, _KNOWN): -0.006,
        ("tot", _P3_D, None): 1.533,
        ("meas", _P3_D, _MEAS): -0.096,
        ("tot", _P4_N, None): -1.891,
        ("meas", _P4_N, _MEAS): -1.909,
        ("tot", _P5_D, None): 1.803,
        ("held", _P5_D, _KNOWN): -0.050,
        ("meas", _P5_D, _MEAS): -0.010,
        ("tot", _P8_N, None): -2.271,
        ("held", _P8_N, _KNOWN): -0.100,
    }


_EVIDENCE = [
    {"_source": {"ts": _A, "event": "act", "action": {"type": "switch_on"},
                 "result": "ok", "inputs": {}, "timer_action": ""}},
    {"_source": {"ts": "2026-10-23T07:30:00+00:00", "event": "state_observed",
                 "inputs": {"to": "off"}}},
    {"_source": {"ts": "2026-10-24T08:03:00+00:00", "event": "state_observed",
                 "inputs": {"to": "on"}}},
    {"_source": {"ts": "2026-10-24T08:05:00+00:00", "event": "act",
                 "action": {"type": "switch_off"}, "result": "ok",
                 "inputs": {}}},
    {"_source": {"ts": _E, "event": "act", "action": {"type": "switch_on"},
                 "result": "ok", "inputs": {}}},
    {"_source": {"ts": "2026-10-24T12:00:00+00:00", "event": "act",
                 "action": {"type": "switch_off"}, "result": "ok",
                 "inputs": {}}},
    {"_source": {"ts": "2026-10-26T08:11:00+00:00", "event": "state_observed",
                 "inputs": {"to": "on"}}},
]
_GENS = [{"_source": {"ts": "2026-10-25T20:12:00+00:00", "event": "act",
                      "action": {"type": "failsafe_timer"},
                      "timer_action": "ensure_on", "result": "ok",
                      "inputs": {"fire_str": "07:16",
                                 "at_local": "2026-10-26T07:16"}}}]
_TIMELINE = {"hits": {"hits": [
    {"_source": {"ts": "2026-10-26T11:55:00+00:00", "event": "tick",
                 "phase": "day_hold", "result": "", "inputs": {},
                 "action": "", "timer_action": ""}},
    {"_source": {"ts": "2026-10-22T06:20:02+00:00", "event": "start",
                 "result": "dry_run=False", "inputs": {},
                 "action": "", "timer_action": ""}},
]}}
_LATEST = {"hits": {"hits": [{"_source": {
    "@timestamp": "2026-10-26T11:59:00+00:00", "pack": {"current": -3.2,
                                                        "voltage": 13.1},
    "soc": {"soc": 41.0, "remaining_ah": 86.0}}}]}}
_FORECAST = {"daily": {"time": ["2026-10-26", "2026-10-27", "2026-10-28"],
                       "shortwave_radiation_sum": [4.2, 3.1, 2.4]},
             "daily_units": {"shortwave_radiation_sum": "MJ/m²"}}


class _Resp:
    def __init__(self, obj): self._b = json.dumps(obj).encode()
    def read(self): return self._b
    def __enter__(self): return self
    def __exit__(self, *a): return False
    status, headers = 200, {}


def _agg_answer(body):
    """Evaluate one energy body exactly like the cluster would: only the
    FILTER STRUCTURE decides which registered value comes back."""
    flt = body["query"]["bool"]["filter"]
    per = flt[0]["range"]["@timestamp"]
    neg = any("range" in f and "pack.current" in f["range"]
              and f["range"]["pack.current"].get("lt") == 0 for f in flt)
    should = next((f["bool"]["should"] for f in flt if "bool" in f), None)
    wins = (tuple(r["range"]["@timestamp"]["gte"] for r in should)
            if should else None)
    which = "tot" if not neg else ("held" if wins == _KNOWN else "meas")
    return _STUB["agg"].get((which, (per["gte"], per["lt"]), wins), 0.0)


def _msearch_responses(nd):
    """NDJSON = alternating {"index":…} header + body lines; each pair
    answers on its own (the index lives in the header, not the URL)."""
    lines = nd.decode().strip().split("\n")
    out = []
    for i in range(0, len(lines), 2):
        index = json.loads(lines[i])["index"]
        body = json.loads(lines[i + 1])
        if index.startswith("bms"):
            if "aggs" in body:                       # energy agg
                out.append({"aggregations": {
                    "e": {"value": _kwh_to_val(_agg_answer(body))}}})
            else:                                    # boundary job
                per = body["query"]["bool"]["filter"][0]["range"]["@timestamp"]
                hit = _BOUNDS.get(per["gte"][:10])
                ts = None
                if hit:
                    ts = hit[0] if body["sort"][0]["@timestamp"] == "asc" else hit[1]
                out.append({"hits": {"hits": (
                    [{"_source": {"@timestamp": ts}}] if ts else [])}})
        else:                                        # events msearch → empty
            out.append({"hits": {"hits": []}})
    return out


def _router(req, timeout=None):
    url = str(getattr(req, "full_url", req))
    data = getattr(req, "data", None) or b""
    if "open-meteo.com" in url:
        if "start_date=" in url:                     # per-day sun times
            d = url.split("start_date=")[1][:10]
            rise, set_ = _SUN.get(d, (None, None))
            obj = {"daily": {"time": [d], "sunrise": [rise], "sunset": [set_]}}
        else:                                        # 3-day forecast chip
            obj = _FORECAST
        return _Resp(obj)
    if url.endswith("/_msearch"):
        return _Resp({"responses": _msearch_responses(data)})
    if "gridguard-events" in url:
        if b"ensure_on" in data:                     # gens (400) vs announced (1)
            if b'"size": 400' in data:
                return _Resp({"hits": {"hits": _STUB.get("gens") or []}})
            return _Resp({"hits": {"hits": (_STUB.get("gens") or [])[:1]}})
        if b"switch_on" in data or b"state_observed" in data:
            return _Resp({"hits": {"hits": _STUB.get("evidence") or []}})
        if b"NACHTPLAN" in data or b"keep_grid" in data:
            return _Resp({"hits": {"hits": []}})
        if b'"match_all"' in data:
            return _Resp(_TIMELINE)
        return _Resp({"hits": {"hits": []}})
    if b'"match_all"' in data:                       # latest BMS doc
        return _Resp(_LATEST)
    return _Resp({"hits": {"hits": []}})


@pytest.fixture()
def week_env(monkeypatch):
    _STUB.clear()
    _STUB.update(evidence=list(_EVIDENCE), gens=list(_GENS), agg=_week_agg())
    monkeypatch.setattr(wp, "TZ", "UTC")
    monkeypatch.setattr(wp, "OS", "http://stub:9200")
    monkeypatch.setattr(wp, "INDEX", "bms-test-*")
    monkeypatch.setattr(wp, "datetime", FrozenDT)
    if isinstance(getattr(wp, "_SUN_CACHE", None), dict):
        wp._SUN_CACHE.clear()
    if isinstance(getattr(wp, "_FC3", None), dict):
        wp._FC3.update(ts=0.0, data=None)
    with mock.patch("urllib.request.urlopen", side_effect=_router):
        yield


def _row(g, kind, frm, to):
    for h in g["history"]:
        if h["kind"] == kind and h["from"] == frm and h["to"] == to:
            return h
    raise AssertionError(f"no row {kind} {frm}→{to} in "
                         f"{[(x['kind'], x['from'], x['to']) for x in g['history']]}")


# --- (iii) OBSERVED windows: IDENTICAL numbers to before ---------------------

def test_observed_window_withholds_identically_to_before(week_env):
    """The Oct20/21 shape (raw −2.433, held 0.006 → kwh −2.427): a KNOWN
    window keeps the exact old arithmetic — kwh = raw + withheld."""
    g = wp.fetch_gridguard()
    r = _row(g, "night", "2026-10-22T16:31+00:00", "2026-10-23T07:56+00:00")
    assert r["corrected"] == 0.006                     # withheld, unchanged
    assert r["kwh"] == -2.427                          # raw(−2.433)+held
    assert "measured_out" not in r                     # nothing to disclose


# --- (ii) MEASURED windows: raw net visible + disclosure ---------------------

def test_fill_window_never_withholds_and_discloses_instead(week_env):
    """THE §14 finding (fill-derived window B): kwh keeps the RAW net
    (1.533 / −1.891 — NOT the re-added 1.629 / +0.018), corrected goes to 0,
    and the in-window discharge is DISCLOSED instead."""
    g = wp.fetch_gridguard()
    day = _row(g, "day", "2026-10-23T07:56+00:00", "2026-10-23T16:29+00:00")
    assert day["kwh"] == 1.533                         # raw net, visible
    assert day["corrected"] == 0.0                     # nothing withheld
    assert day["measured_out"] == 0.096                # …disclosed instead
    night = _row(g, "night", "2026-10-23T16:29+00:00", "2026-10-24T07:26+00:00")
    assert night["kwh"] == -1.891                      # the battery fed the house
    assert night["corrected"] == 0.0
    assert night["measured_out"] == 1.909
    # coherence: the disclosure is NEVER re-added into kwh (no double count)
    assert night["kwh"] != round(-1.891 + 1.909, 3)


# --- mixed rows: known + measured in ONE period ------------------------------

def test_mixed_row_withholds_known_only_and_discloses_measured(week_env):
    """Day Oct24 contains the measured B tail AND the known window E:
    withheld = known slice only; the measured slice discloses; kwh = raw +
    withheld(known) — never + the disclosed slice."""
    g = wp.fetch_gridguard()
    r = _row(g, "day", "2026-10-24T07:26+00:00", "2026-10-24T16:19+00:00")
    assert r["corrected"] == 0.050                     # known-E discharge
    assert r["measured_out"] == 0.010                  # measured-B discharge
    assert r["kwh"] == 1.853                           # raw 1.803 + 0.050
    assert r["kwh"] != round(1.803 + 0.050 + 0.010, 3)  # never double-counted


# --- (v) tier-1 write-confirmed fire = withhold (first truthful eve) ---------

def test_tier_one_fire_window_withholds(week_env):
    """Window C opens at the write-confirmed fire instant (ensure_on
    fire_str 07:16) — KNOWN → the owner rule applies unchanged."""
    g = wp.fetch_gridguard()
    r = _row(g, "night", "2026-10-25T16:29+00:00", "2026-10-26T08:02+00:00")
    assert r["corrected"] == 0.100
    assert r["kwh"] == -2.171                          # raw −2.271 + 0.100
    assert "measured_out" not in r


# --- empty windows: legacy behaviour ----------------------------------------

def test_empty_windows_legacy_raw_net_and_no_withholding(week_env):
    """No evidence at all → no windows → no held/meas aggs: kwh = raw,
    corrected 0, no disclosure key anywhere (the old wire shape)."""
    _STUB["evidence"] = []
    _STUB["gens"] = []
    _STUB["agg"] = {}
    g = wp.fetch_gridguard()
    assert g["status"]["grid_on_windows"] == 0
    for h in g["history"]:
        assert h["corrected"] == 0.0
        assert "measured_out" not in h
        assert h["kwh"] == 0.0


# --- window inventory unchanged (iv: energy layer only) ----------------------

def test_grid_on_windows_count_still_covers_both_tiers(week_env):
    """The status counter counts ALL labelled windows (4) — the tier gate
    changes the ENERGY treatment only, never the window inventory."""
    g = wp.fetch_gridguard()
    assert g["status"]["grid_on_windows"] == 4


# --- page markup: the disclosure is a tier-marked, honest cell ---------------

def test_page_history_cell_discloses_measured_on_discharge():
    """Owner UI order (2026-10-26, compact cell + one-line follow-up): the
    disclosure keeps the VALUE plus ONE text glyph — U+26A0 WARNING SIGN
    with U+FE0E text variation selector (⚠︎, monochrome, never the emoji
    form) wrapped in a native title/aria-label tooltip carrying the FULL
    explanation. The whole cell composes on ONE line (no <br>, nowrap on
    the disclosure td only) so disclosure rows keep the height of every
    other row; the '-' sign stays RESERVED for truly WITHHELD energy."""
    assert "measured_out" in PAGE                      # the disclosure field
    # the value stays; the verbose in-cell phrase is gone
    assert 'h.measured_out.toFixed(2)+" kWh ' in PAGE
    assert "△ measured ON — kept in net" not in PAGE
    # ONE line: no <br> anywhere; the disclosure joins the withheld state
    # inline with the page's existing "·" separator idiom; the nowrap style
    # rides ONLY the disclosure td (never another cell)
    assert "<br>" not in PAGE
    assert 'h.measured_out?" · "+h.measured_out.toFixed(2)' in PAGE
    assert "\" style='white-space:nowrap'\"" in PAGE
    # ONE glyph: U+26A0 + U+FE0E text variation selector. web_page.py keeps
    # the JS/Python \u escapes in SOURCE (greppable — invisible raw bytes
    # are a known grep hazard); Python resolves them at parse time, so PAGE
    # carries the resolved pair and the test compares at char level.
    assert "\u26A0\uFE0E" in PAGE
    assert "\u26A0\uFE0F" not in PAGE       # never the emoji presentation
    # native tooltip + accessible name carry the complete explanation
    assert "title='measured ON — kept in net:" in PAGE
    assert "aria-label='measured ON — kept in net:" in PAGE
    assert "measurement-derived" in PAGE               # the full wording lives
    # the sign rule: '-' is RESERVED for truly withheld (corrected)
    assert '"-"+h.corrected.toFixed(2)+" kWh"' in PAGE


def test_page_zeroed_note_and_counters_stay_one_coherent_story():
    """The energy-card zeroed line and the history note must describe the
    new tier semantics coherently (withheld = known windows only; the
    measured slice is disclosed, kept in the net). The footer doctrine note
    and the zeroed card line are OUTSIDE the compact-cell order — verbatim."""
    assert "withheld (known grid-ON only)" in PAGE
    assert "disclosed, kept in the net" in PAGE
    assert "withheld inside LABELLED grid-ON" not in PAGE  # old blanket claim
