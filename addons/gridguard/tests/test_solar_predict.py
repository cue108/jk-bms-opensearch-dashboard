"""Phases C + D (+ B folded) — RED fixtures (review wave on 6b4be64).

C — the forecast onset link ("Will there be a solar day tomorrow?"): an honest,
statistic-modest prediction of the next solar day and its onset from the TWO
existing data streams (the outlook forecast the reserve tier already fetches,
and the own measurements: measured solar-edge history + the learned
yield_learned factor pairs). Basis classes: sunny_chain_analog (a recent
measured sunny day with a SIMILAR forecast → predict ITS onset hour),
forecast_amount (the learned forecast→measured connection decides WHETHER a
solar day is expected; no clock claim), insufficient (honest None).

B (folded) — the horizon re-scope: the 6–16 h gate keeps ONLY its divisor
protection (the fallback median crossing still divides); the HORIZON may use
a measured span of ANY length; when the measured onset no longer extends
(the sunless chain — MEASURED_ONSET_MAX_AGE_H) the predicted onset supplies
the horizon; neither → None → the existing length clamp alone. The horizon
SOURCE is disclosed in the night_need audit (labelled, never state).

D — the reference visibility: decision.reference carries the reference
object's FULL truth and the page renders one compact detail line where the
crossing shows (the Phase-A last_ref line stays).
"""
import json
from datetime import date, datetime, timedelta
from unittest import mock

import pytest

import gridguard.web_payload as wp
from gridguard.config import load_config
from gridguard.main import (MEASURED_ONSET_MAX_AGE_H, AppState, RealDeps,
                            tick)
from gridguard.planner import plan_evening
from gridguard.web_page import PAGE
from test_main_tick import (D, FLOOR_WH, GOOD, INSUFF, PACK_V, SUFF,
                            FakeDeps, buckets, cfg)

try:                            # RED guards: fail per-test, never on collection
    from gridguard.main import next_solar_onset_src
    from gridguard.solar_predict import solar_day_prediction
    _C_MISSING = ""
except ImportError:
    _C_MISSING = "Phase C module/function missing"
    next_solar_onset_src = solar_day_prediction = None


def _need_c():
    assert not _C_MISSING, _C_MISSING


# ---------------------------------------------------------------------------
# C1 — the sunny-chain analog (owner: "will very probably be the one from
# yesterday, timing-wise, if yesterday's weather was nice too")
def _naive(d, h, m):
    return datetime(2026, 10, d, h, m)


def test_sunny_chain_analog_predicts_the_recent_measured_onset_hour():
    _need_c()
    bounds = {date(2026, 10, 28): (_naive(28, 7, 41), _naive(28, 17, 50)),
              date(2026, 10, 29): (_naive(29, 7, 42), _naive(29, 17, 49))}
    fc = [{"date": "2026-10-29", "radiation_sum_j": 4.3e6},
          {"date": "2026-10-30", "radiation_sum_j": 4.0e6}]
    pred = solar_day_prediction(date(2026, 10, 30), forecast_days=fc,
                                day_bounds=bounds, yield_hist=[],
                                tz=cfg.tz)
    assert pred["predicted"] is True
    assert pred["basis"] == "sunny_chain_analog"
    assert pred["onset_h"] == pytest.approx(7.7)     # yesterday's 07:42 edge
    assert pred["samples_used"] == 1                 # only the 29th has bounds+fc
    assert pred["analog_day"] == "2026-10-29"
    assert pred["forecast_kwh_m2"] == pytest.approx(4.0 / 3.6, abs=0.01)
    assert pred["factor"] is None                    # the amount class unused here


# ---------------------------------------------------------------------------
# C2 — the forecast-amount class + the honest insufficient case
_YIELD_HIST = [
    {"ts": "2026-10-26T21:10:00+00:00",
     "inputs": {"day": "2026-10-26", "measured_kwh": 3.0,
                "forecast_kwh_m2": 2.0, "factor": 1.5}},
    {"ts": "2026-10-25T21:10:00+00:00",
     "inputs": {"day": "2026-10-25", "measured_kwh": 1.8,
                "forecast_kwh_m2": 2.0, "factor": 0.9}},
]


def test_forecast_amount_class_predicts_existence_without_a_clock_claim():
    _need_c()
    # sunny chain ABSENT (no measured edges) → the learned connection decides
    fc = [{"date": "2026-10-31", "radiation_sum_j": 7.2e6}]      # 2.0 kWh/m²
    pred = solar_day_prediction(date(2026, 10, 31), forecast_days=fc,
                                day_bounds={}, yield_hist=_YIELD_HIST,
                                tz=cfg.tz)
    assert pred["basis"] == "forecast_amount"
    assert pred["predicted"] is True        # 2.0 × median(1.5, 0.9) = 2.4 ≥ floor
    assert pred["onset_h"] is None          # …and NO onset time is invented
    assert pred["samples_used"] == 2
    assert pred["factor"] == pytest.approx(1.2)
    # wash-out forecast → the learned amount says NO solar day
    fc2 = [{"date": "2026-10-31", "radiation_sum_j": 0.1e6}]     # ≈0.028 kWh/m²
    pred2 = solar_day_prediction(date(2026, 10, 31), forecast_days=fc2,
                                 day_bounds={}, yield_hist=_YIELD_HIST,
                                 tz=cfg.tz)
    assert pred2["basis"] == "forecast_amount" and pred2["predicted"] is False


def test_nothing_sufficient_is_an_honest_none():
    _need_c()
    empty = {"predicted": None, "onset_h": None, "basis": "insufficient",
             "samples_used": 0, "forecast_kwh_m2": None, "analog_day": None,
             "factor": None}
    # no forecast at all → nothing to stand on
    assert solar_day_prediction(date(2026, 10, 30), forecast_days=[],
                                day_bounds={}, yield_hist=[], tz=cfg.tz) == empty
    # forecast present but NOTHING learned yet (no yield pairs) → insufficient
    # — the forecast amount itself stays disclosed as evidence
    no_learn = solar_day_prediction(
        date(2026, 10, 30),
        forecast_days=[{"date": "2026-10-30", "radiation_sum_j": 4.0e6}],
        day_bounds={}, yield_hist=[], tz=cfg.tz)
    assert no_learn["basis"] == "insufficient"
    assert no_learn["predicted"] is None and no_learn["onset_h"] is None
    assert no_learn["samples_used"] == 0
    assert no_learn["forecast_kwh_m2"] == pytest.approx(4.0 / 3.6, abs=0.01)


# ---------------------------------------------------------------------------
# C3/B — the horizon source chain (measured fresh | predicted | profile | none)
NOW_EVE = datetime(2026, 10, 29, 20, 5)
FRESH_SPANS = {datetime(2026, 10, 29, 7, 55):
               (datetime(2026, 10, 28, 15, 55), datetime(2026, 10, 29, 7, 55))}
# the sunless chain: the last measured morning is 2+ days back (>36 h old)
STALE_SPANS = {datetime(2026, 10, 26, 7, 50):
               (datetime(2026, 10, 25, 15, 50), datetime(2026, 10, 26, 7, 50))}
PRED = {"predicted": True, "onset_h": 7.7, "basis": "sunny_chain_analog",
        "samples_used": 1, "forecast_kwh_m2": 1.11,
        "analog_day": "2026-10-25", "factor": None}


def test_measured_fresh_stays_the_primary_horizon_source():
    _need_c()
    onset, src = next_solar_onset_src(NOW_EVE, FRESH_SPANS, INSUFF, PRED)
    assert src == "measured"
    assert onset == datetime(2026, 10, 30, 7, 55)   # fresh edge stepped +24 h


def test_sunless_chain_measured_does_not_extend_and_the_prediction_supplies():
    _need_c()
    onset, src = next_solar_onset_src(NOW_EVE, STALE_SPANS, INSUFF, PRED)
    assert src == "predicted"
    assert onset == datetime(2026, 10, 30, 7, 42)   # onset_h anchored to the next day


def test_sunless_chain_without_prediction_is_none():
    _need_c()
    assert next_solar_onset_src(NOW_EVE, STALE_SPANS, INSUFF, None) == (None, "none")


def test_learned_profile_stays_the_existing_fallback_after_the_prediction():
    _need_c()
    onset, src = next_solar_onset_src(NOW_EVE, STALE_SPANS, SUFF, None)
    assert src == "profile"
    assert onset == datetime(2026, 10, 30, 8, 0)


# ---------------------------------------------------------------------------
# C4 — every prediction evaluation is AUDITED with the full basis
def test_realdeps_evaluates_and_audits_the_prediction_with_full_basis(tmp_path):
    c = load_config({**GOOD, "LOG_DIR": str(tmp_path)})
    deps = RealDeps(c)
    recs = []
    deps.audit = lambda ev, **f: recs.append((ev, f))     # capture, no journal I/O
    today = datetime.now(c.tz).date()
    yday, tgt = today - timedelta(days=1), today + timedelta(days=1)
    deps._set_outlook([{"date": yday.isoformat(), "radiation_sum_j": 4.3e6},
                       {"date": tgt.isoformat(), "radiation_sum_j": 4.0e6}])
    s = datetime.now(c.tz).replace(hour=7, minute=42, second=0,
                                   microsecond=0) - timedelta(days=1)
    deps.solar_windows = lambda: ({yday: (s, s.replace(hour=17, minute=49))}, {})
    deps.bms.yield_history = lambda size=10: []
    pred = deps.solar_day_prediction(tgt)
    assert pred["basis"] == "sunny_chain_analog"
    assert pred["onset_h"] == pytest.approx(7.7)
    ev = [f for e, f in recs if e == "solar_day_pred"]
    assert len(ev) == 1                           # ONE line per real evaluation
    inp = ev[0]["inputs"]
    assert inp["target"] == tgt.isoformat()
    assert inp["predicted"] is True and inp["basis"] == "sunny_chain_analog"
    assert inp["samples_used"] == 1
    assert inp["analog_day"] == yday.isoformat()  # the full basis rides along
    assert inp["forecast_kwh_m2"] == pytest.approx(4.0 / 3.6, abs=0.01)
    # same day + target → the cache serves it, the audit stays deduped
    deps.solar_day_prediction(tgt)
    deps.solar_day_prediction(tgt)
    assert len([f for e, f in recs if e == "solar_day_pred"]) == 1


# ---------------------------------------------------------------------------
# B tick-level — the horizon: 40 h span (the OLD 6–16 gate refused it) …
def _eve_deps(**kw):
    st = AppState()
    st.morning_done = True
    deps = FakeDeps(switch="off", inventory_timers=[], buckets=buckets(100.0, 100.0),
                    **kw)
    return st, deps


def test_eve_uses_any_measured_span_length_as_the_horizon_40h_included():
    # span = 40 h (Oct16 15:55 → Oct18 07:55); crossing +11.5 h: the OLD gate
    # refused the span → night_len fell back to 11 h → "battery carries" (none).
    # The new code serves the horizon with the measured 40 h → armed.
    SPANS40 = {datetime(2026, 10, 18, 7, 55):
               (datetime(2026, 10, 16, 15, 55), datetime(2026, 10, 18, 7, 55))}
    avail = FLOOR_WH + 100.0 * cfg.safety_factor / 11.0 * 11.5
    st, deps = _eve_deps(remaining_ah=avail / PACK_V)
    deps.spans = SPANS40
    deps.pred = None                              # the onset side is NOT under test
    tick(cfg, st, deps, D(20, 10))
    assert deps.plan_seen and deps.plan_seen[0][0] == "failsafe_timer"
    at = deps.plan_seen[0][1][0][1]["at_local"]
    assert at == D(20, 10) + timedelta(hours=11.5)   # the DIVISOR stays gated/old
    night = [f for e, f in deps.audits if e == "night_need"][-1]["inputs"]
    assert night["onset_src"] == "measured"          # fresh edge still primary


def test_planner_horizon_hours_overrides_only_the_length_clamp():
    avail = FLOOR_WH + 100.0 * cfg.safety_factor / 11.0 * 11.5
    args = dict(available_wh=avail, need_wh=100.0, switch_state="off",
                night_len_hours=11.0, cfg=cfg, forecast_ok=True,
                pack_v=PACK_V, full_ah=222.0)
    old = plan_evening(D(20, 10), **args)
    assert [k for k, _ in old.actions] == ["none"]     # 11.5 ≥ 11 (old horizon)
    h40 = plan_evening(D(20, 10), horizon_hours=40.0, **args)
    assert h40.actions[0][0] == "failsafe_timer"
    # …and the crossing time is untouched: night_len still feeds the DIVISOR
    assert h40.actions[0][1]["at_local"] == D(20, 10) + timedelta(
        hours=(avail - FLOOR_WH) / (100.0 * cfg.safety_factor / 11.0))


# ---------------------------------------------------------------------------
# B tick-level — the sunless chain: prediction supplies / clamp alone
STALE16 = {datetime(2026, 10, 16, 7, 55):
           (datetime(2026, 10, 15, 15, 55), datetime(2026, 10, 16, 7, 55))}


def _chain_tick(pred):
    # crossing +11.6 h (rate = 100×1.2 ÷ the gated 16-h measured span) — the
    # boundary band between the PREDICTED onset (07:42 = +11.533 h) and the
    # old stepped-measured onset (07:55 = +11.75 h): prediction present →
    # refused at 07:46; the old code armed it (stepped edge said 07:55).
    avail = FLOOR_WH + 100.0 * cfg.safety_factor / 16.0 * 11.6
    st, deps = _eve_deps(remaining_ah=avail / PACK_V)
    deps.spans = STALE16                          # length16 h (gate-legal), END 60 h old
    deps.pred = pred
    tick(cfg, st, deps, D(20, 10))
    return st, deps


def test_chain_eve_refuses_at_the_predicted_onset_labelled_in_the_audit():
    st, deps = _chain_tick(dict(PRED))
    assert deps.pred_calls == 1                   # the eve CONSULTS the prediction
    assert st.evening_verdict == "safe_clear"     # crossing 08:10 ≥ predicted 07:42
    assert deps.plan_seen == []                   # … refused — nothing armed
    night = [f for e, f in deps.audits if e == "night_need"][-1]["inputs"]
    assert night["onset_src"] == "predicted"      # horizon source DISCLOSED


def test_chain_eve_without_prediction_falls_to_the_length_clamp_alone():
    st, deps = _chain_tick(None)
    assert deps.pred_calls == 1                   # asked, honestly nothing to use
    assert st.evening_verdict == "rescue_timer"   # onset None → the clamp decides
    assert deps.plan_seen[0][0] == "failsafe_timer"
    night = [f for e, f in deps.audits if e == "night_need"][-1]["inputs"]
    assert night["onset_src"] == "none"


def test_the_prediction_is_never_consulted_outside_the_eve():
    # NIGHT-phase re-estimation: no prediction ask (the horizon belongs to the
    # eve clamp; _night_resolve keeps its post-sunrise carve-out)
    from test_reference_model import NightDeps, REF_CLIP, _at
    deps = NightDeps(ref=dict(REF_CLIP), remaining_ah=718.5 / 40.0)
    # NightDeps has NO solar_day_prediction → the eve-only contract holds
    tick(cfg, AppState(), deps, _at(30, 2, 0))
    assert any(e == "night_resolve" for e, _ in deps.audits)


# ---------------------------------------------------------------------------
# D — the payload carries the reference object's FULL truth + the page pins
_FORECAST = {"daily": {"time": ["2026-10-25", "2026-10-26", "2026-10-27"],
                       "shortwave_radiation_sum": [4.2, 3.1, 2.4]},
             "daily_units": {"shortwave_radiation_sum": "MJ/m²"}}
_TIMELINE = {"hits": {"hits": [
    {"_source": {"ts": "2026-10-25T11:40:00+00:00", "event": "tick",
                 "phase": "day_hold", "result": "", "inputs": {},
                 "action": "", "timer_action": ""}},
    {"_source": {"ts": "2026-10-25T09:10:00+00:00", "event": "start",
                 "result": "dry_run=False", "inputs": {},
                 "action": "", "timer_action": ""}},
]}, "total": {"value": 2}}
_EMPTY = {"hits": {"hits": [], "total": {"value": 0}}}
_STUB: dict = {}
REF_INPUTS = {"source": "clip", "night": "2026-10-29", "wh_h": 133.0,
              "wh": 1294.5, "min_clip_min": 30,
              "clips": [{"start": "2026-10-29T18:30+01:00",
                         "end": "2026-10-30T04:14+01:00",
                         "wh": 1294.5, "hours": 9.7333}]}


class _Resp:
    def __init__(self, obj): self._b = json.dumps(obj).encode()
    def read(self): return self._b
    def __enter__(self): return self
    def __exit__(self, *a): return False
    status, headers = 200, {}


def _router(req, timeout=None):
    url = str(getattr(req, "full_url", req))
    if "open-meteo.com" in url:
        return _Resp(_FORECAST)
    data = getattr(req, "data", None) or b""
    if url.endswith("/_msearch"):
        lines = data.decode().strip().split("\n")
        responses = []
        for i in range(0, len(lines) - 1, 2):
            body = lines[i + 1]
            if '"reference_load"' in body:
                src = _STUB.get("ref_event")
            elif '"night_need"' in body:
                src = _STUB.get("nn_event")
            else:
                src = None
            responses.append({"hits": {"hits": [{"_source": src}] if src else [],
                                       "total": {"value": 1 if src else 0}}})
        return _Resp({"responses": responses})
    if b'"match_all"' in data:
        return _Resp(_TIMELINE)
    return _Resp(_EMPTY)


@mock.patch("urllib.request.urlopen", side_effect=_router)
def test_payload_reference_object_carries_the_full_basis(_m):
    _STUB.clear()
    _STUB["ref_event"] = {"ts": "2026-10-30T06:05:00+00:00",
                          "event": "reference_load", "inputs": dict(REF_INPUTS)}
    _STUB["nn_event"] = {"ts": "2026-10-29T20:57:00+00:00", "event": "night_need",
                         "inputs": {"need_wh": 2350.5, "onset_src": "predicted"}}
    for cache in ("_FC3", "_SUN_CACHE"):
        c = getattr(wp, cache, None)
        if isinstance(c, dict):
            c.clear()
    fc3 = getattr(wp, "_FC3", None)
    if isinstance(fc3, dict):
        fc3.update(ts=0.0, data=None)
    with mock.patch.object(wp, "OS", "http://stub:9200"), \
         mock.patch.object(wp, "INDEX", "bms-test-*"):
        d = wp.fetch_gridguard()["decision"]
    # D: the FULL reference object (night, clips with hours, Wh, Wh/h, source)
    assert d["reference"] == REF_INPUTS
    # the Phase-A compact line stays beside it
    assert d["last_ref"] == "last_ref: Thu 29 Oct clip · 133 Wh/h"
    # B/C disclosure rides the payload too (label, never state)
    assert d["onset_src"] == "predicted"


def test_payload_reference_is_null_without_the_event():
    _STUB.clear()
    for cache in ("_FC3", "_SUN_CACHE"):
        c = getattr(wp, cache, None)
        if isinstance(c, dict):
            c.clear()
    fc3 = getattr(wp, "_FC3", None)
    if isinstance(fc3, dict):
        fc3.update(ts=0.0, data=None)
    with mock.patch("urllib.request.urlopen", side_effect=_router), \
         mock.patch.object(wp, "OS", "http://stub:9200"), \
         mock.patch.object(wp, "INDEX", "bms-test-*"):
        d = wp.fetch_gridguard()["decision"]
    assert d.get("reference") is None          # absence rule: no invented basis
    assert d.get("onset_src") is None
    assert d.get("last_ref") is None


def test_page_renders_the_reference_detail_where_the_crossing_shows():
    assert "function refDetail(" in PAGE
    assert ('if(D.reference) $("ap-sub").textContent+=" · "'
            '+refDetail(D.reference);') in PAGE


def test_page_labels_a_predicted_horizon_never_as_state():
    """DISPLAY-TRUTH (C): the horizon source may only ever render as a LABEL —
    never into the armed/announced slots (the prediction is never state)."""
    lines = [ln for ln in PAGE.splitlines() if "D.onset_src" in ln]
    assert lines, "the payload disclosure must reach the page"
    assert all("(label, not state)" in ln for ln in lines)
    assert not any("ap-time" in ln or "ap-state" in ln for ln in lines)


# ---------------------------------------------------------------------------
# F-1 (BLOCKING): SOURCE ORDER — the onset label APPENDS to ap-eve, so it must
# come AFTER the unconditional ap-eve assignment; before it, the first paint
# reassigns the element and the label never appears (dead JS).
ONSET_RENDER = ('if(D.onset_src) $("ap-eve").textContent+=" · onset src: "'
                '+D.onset_src+" (label, not state)";')
AP_EVE_ASSIGN = '$("ap-eve").textContent=(A'


def test_onset_label_renders_after_the_ap_eve_assignment():
    assert ONSET_RENDER in PAGE                       # the exact string stays
    assert AP_EVE_ASSIGN in PAGE
    # SOURCE-ORDER pin: the append must sit AFTER the reassignment
    assert PAGE.index(ONSET_RENDER) > PAGE.index(AP_EVE_ASSIGN)


# ---------------------------------------------------------------------------
# F-4 (pins): the chain MEAN + the exact 36-h freshness boundary
def test_sunny_chain_multiple_similar_days_use_the_chain_mean_newest_first():
    """The owner's "the amount of data merely helps a better average":
    two recent similar sunny days (07:41 + 07:42) → the plain
    MEAN of their measured onsets, analog_day = the NEWEST of the chain."""
    _need_c()
    bounds = {date(2026, 10, 28): (_naive(28, 7, 41), _naive(28, 17, 50)),
              date(2026, 10, 29): (_naive(29, 7, 42), _naive(29, 17, 49))}
    fc = [{"date": "2026-10-28", "radiation_sum_j": 4.0e6},
          {"date": "2026-10-29", "radiation_sum_j": 4.3e6},
          {"date": "2026-10-30", "radiation_sum_j": 4.0e6}]
    pred = solar_day_prediction(date(2026, 10, 30), forecast_days=fc,
                                day_bounds=bounds, yield_hist=[], tz=cfg.tz)
    assert pred["predicted"] is True
    assert pred["basis"] == "sunny_chain_analog"
    # mean(7:41, 7:42) = 7 + 41.5/60 h = 7.6917 → 7.692
    assert pred["onset_h"] == pytest.approx(7.692, abs=1e-3)
    assert pred["samples_used"] == 2
    assert pred["analog_day"] == "2026-10-29"        # the NEWEST chain day


def test_measured_onset_at_exactly_the_max_age_is_still_measured():
    """F-4(b): the freshness boundary is INCLUSIVE — age exactly
    MEASURED_ONSET_MAX_AGE_H stays a live measured source."""
    assert MEASURED_ONSET_MAX_AGE_H == 36.0          # the named knob's default
    base = datetime(2026, 10, 28, 7, 55)
    spans = {base: (base - timedelta(hours=16), base)}
    now = base + timedelta(hours=MEASURED_ONSET_MAX_AGE_H)   # age EXACTLY 36 h
    onset, src = next_solar_onset_src(now, spans, INSUFF, None)
    assert src == "measured"
    assert onset == datetime(2026, 10, 30, 7, 55)    # stepped past `now`


def test_measured_onset_past_the_max_age_falls_through_to_none():
    """F-4(b): 36 h + 6 min → the measured edge no longer extends; with no
    prediction and no profile the chain ends in 'none' (clamp alone)."""
    base = datetime(2026, 10, 28, 7, 55)
    spans = {base: (base - timedelta(hours=16), base)}
    now = base + timedelta(hours=MEASURED_ONSET_MAX_AGE_H, minutes=6)
    assert next_solar_onset_src(now, spans, INSUFF, None) == (None, "none")
