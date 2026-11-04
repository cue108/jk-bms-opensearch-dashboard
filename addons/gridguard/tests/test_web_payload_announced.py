"""Display truth (owner doctrine 2026-10-26): the page's "announced grid-on"
must be the LAST write-confirmed programming (ensure_on act, result ok,
carrying its programmed time), NEVER the planner's internal plan ideal.

- announced_on_at      = newest time-bearing write-confirmed ensure_on act
                         (dedicated cluster query, newest-first; absence =
                         None — NO fallback to the plan ideal, ever)
- planned_on_at        = the plan ideal (the planner's own crossing, carried
                         in the NIGHT PLAN push text) — separately labelled
                         on the page; both truths coexist.
- decision.acts[].fire = the ladder: each write generation shows its time.
"""
import json
from unittest import mock

import pytest

import gridguard.web_payload as wp


# per-test stub answers, dispatched by request-body content
_STUB: dict = {}

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


class _Resp:
    def __init__(self, obj): self._b = json.dumps(obj).encode()
    def read(self): return self._b
    def __enter__(self): return self
    def __exit__(self, *a): return False
    status, headers = 200, {}


def _router(req, timeout=None):
    url = str(getattr(req, "full_url", req))
    if "open-meteo.com" in url:
        return _Resp(_FORECAST)               # no sunrise keys → sun times (None, None)
    data = getattr(req, "data", None) or b""
    if url.endswith("/_msearch"):
        n = data.decode().count('"index"')
        return _Resp({"responses": [{"hits": {"hits": []}} for _ in range(n)]})
    if b"ensure_on" in data:                  # announced (size:1) vs gen trail (size:400)
        if b'"size": 400' in data:
            return _Resp({"hits": {"hits": _STUB.get("gens") or []}})
        _STUB["ann_query"] = data.decode()
        hits = [_STUB["ann"]] if _STUB.get("ann") else []
        return _Resp({"hits": {"hits": hits}})
    if b'"terms": {"timer_action"' in data:   # §32 announced-cancel query (unique key shape)
        _STUB["cancel_query"] = data.decode()
        c = _STUB.get("cancel")
        return _Resp({"hits": {"hits": [c] if c else []}})
    if b"keep_grid" in data:                  # the act-trail query (decision.acts)
        return _Resp({"hits": {"hits": _STUB.get("trail") or []}})
    if b"NACHTPLAN" in data:                  # push queries (evidence + plan ideal)
        _STUB["push_query"] = data.decode()
        p = _STUB.get("push")
        return _Resp({"hits": {"hits": [p] if p else []}})
    if b'"match_all"' in data:                # events timeline + latest BMS doc
        return _Resp(_TIMELINE)
    return _Resp({"hits": {"hits": []}})


@pytest.fixture()
def payload_env(monkeypatch):
    _STUB.clear()
    _STUB.update(ann=None, trail=[], push=None, ann_query=None, gens=[],
                 cancel=None, cancel_query=None, push_query=None)
    monkeypatch.setattr(wp, "TZ", "UTC")      # deterministic anchor math
    monkeypatch.setattr(wp, "OS", "http://stub:9200")
    monkeypatch.setattr(wp, "INDEX", "bms-test-*")
    if isinstance(getattr(wp, "_SUN_CACHE", None), dict):
        wp._SUN_CACHE.clear()
    if isinstance(getattr(wp, "_FC3", None), dict):
        wp._FC3.update(ts=0.0, data=None)
    with mock.patch("urllib.request.urlopen", side_effect=_router):
        yield


def _ann_hit(ts, fire):
    return {"_source": {"ts": ts, "event": "act",
                        "action": {"type": "failsafe_timer"},
                        "timer_action": "ensure_on", "result": "ok",
                        "inputs": {"fire_str": fire,
                                   "at_local": "2026-10-26T" + fire}}}


def _push(text, ts):
    return {"_source": {"ts": ts, "event": "push", "kind": "NACHTPLAN",
                        "inputs": {"text": text}}}


def test_push_queries_ask_for_both_kind_eras(payload_env):
    """Owner ruling 2026-11-04 (translate + compatibility): the stored kind is
    the stable English key (NIGHT PLAN) while history carries the former
    German values (NACHTPLAN) — every push query must ask for BOTH, so the
    plan/ideal context never loses the older events."""
    _STUB["push"] = _push("Nachtplan final: timer 08:44",
                          "2026-10-25T20:57:22+00:00")
    wp.fetch_gridguard()
    q = _STUB.get("push_query") or ""
    assert '"kind": "NIGHT PLAN"' in q, q
    assert '"kind": "NACHTPLAN"' in q, q


def test_announced_comes_from_last_write_confirmed_programming(payload_env):
    """LAST night's shape: the final write (20:59:26Z → 09:01) is the
    announced truth; the query must ask newest-first for a time-bearing,
    result-ok ensure_on act (not scans, not the recent buffer)."""
    _STUB["ann"] = _ann_hit("2026-10-25T20:59:26+00:00", "09:01")
    _STUB["push"] = _push("Nachtplan final: timer 08:44",
                          "2026-10-25T20:57:22+00:00")
    d = wp.fetch_gridguard()["decision"]
    assert d["announced_on_at"] == "09:01"
    assert d["announced_fires_at"] == "2026-10-26T09:01+00:00"
    q = _STUB.get("ann_query") or ""
    assert '"ts": "desc"' in q and '"event": "act"' in q
    assert "ensure_on" in q and '"result": "ok"' in q
    assert "inputs.fire_str" in q


def test_absence_rule_no_time_bearing_write_never_renders_plan_ideal_as_announced(
        payload_env):
    """THE bug regression (owner find 2026-10-26): journal holds no
    programmed time (reality until tonight) → announced is None, and the
    plan ideal 08:44 must NOT leak into the announced slot."""
    _STUB["ann"] = None
    _STUB["push"] = _push("Nachtplan final: timer 08:44",
                          "2026-10-25T20:57:22+00:00")
    d = wp.fetch_gridguard()["decision"]
    assert d["announced_on_at"] is None
    assert d["announced_fires_at"] is None
    assert d["planned_on_at"] == "08:44"      # plan ideal survives, its own field


def test_plan_ideal_and_announced_are_independent_and_acts_carry_their_time(
        payload_env):
    """Both truths coexist: announced 09:01 (write) beside plan ideal 08:44
    (planner push) — and the trail acts carry fire_str so superseded ladder
    generations stay visible in the payload."""
    _STUB["ann"] = _ann_hit("2026-10-25T20:59:26+00:00", "09:01")
    _STUB["push"] = _push("Nachtplan final: timer 08:44",
                          "2026-10-25T20:57:22+00:00")
    _STUB["trail"] = [
        {"_source": {"ts": "2026-10-26T08:11:37+00:00", "event": "act",
                     "action": {"type": "disable_on_timers"},
                     "timer_action": "disable", "result": "ok"}},
        {"_source": {"ts": "2026-10-25T20:59:26+00:00", "event": "act",
                     "action": {"type": "failsafe_timer"},
                     "timer_action": "ensure_on", "result": "ok",
                     "inputs": {"fire_str": "09:01",
                                "at_local": "2026-10-26T09:01"}}},
    ]
    d = wp.fetch_gridguard()["decision"]
    assert d["announced_on_at"] == "09:01"
    assert d["planned_on_at"] == "08:44"      # from the PUSH, not from act inputs
    gen = [a for a in d["acts"] if a.get("type") == "failsafe_timer"]
    assert gen and gen[0].get("fire") == "09:01"


def test_planned_ignores_act_inputs_even_without_announced_query(payload_env):
    """The old conflated source: planned_on_at used to read act inputs —
    with the writer now recording times, that would have re-labelled the
    write time as 'plan ideal'. It must stay push-only."""
    _STUB["ann"] = None
    _STUB["push"] = None
    _STUB["trail"] = [_ann_hit("2026-10-25T20:59:26+00:00", "07:33")]
    d = wp.fetch_gridguard()["decision"]
    assert d["planned_on_at"] is None
    assert d["announced_on_at"] is None       # absence: the query (not the trail) is the source


def test_push_is_never_state_evidence_but_keeps_the_plan_ideal_field(payload_env):
    """Ruling #10 (the 08:44 bug, history edition): a NIGHT PLAN push must
    NOT open a grid-ON window any more — it only feeds the labelled plan
    ideal field. Was: grid_on_windows==1 from the push anchor alone."""
    _STUB["push"] = _push("Nachtplan final: timer 08:44",
                          "2026-10-25T20:57:22+00:00")
    g = wp.fetch_gridguard()
    assert g["status"]["grid_on_windows"] == 0      # NOT state evidence
    assert g["decision"]["grid_now"] is False
    assert g["decision"]["planned_on_at"] == "08:44"  # labelled plan ideal survives


def test_write_confirmed_generation_becomes_the_on_window(payload_env):
    """Tier (1): a passed, time-bearing ensure_on generation opens the ON
    window at its fire time — the history boundary's future source. (The
    UNFIRED case is pinned in test_history_boundary_truth tier-1 helpers;
    no wall-clock dependence here: the fire anchor derives from its own
    write ts and is in the past for every run from 2026-10-26 09:01 UTC.)"""
    _STUB["gens"] = [_ann_hit("2026-10-25T20:59:26+00:00", "09:01")]
    g = wp.fetch_gridguard()
    assert g["status"]["grid_on_windows"] == 1      # one passed generation


# ---- §31/§32 ANNOUNCED-CANCEL (owner ruling round, sanctioned option 1) -------
# The page must NEVER advertise a timer the system itself disarmed: after the
# newest ensure_on, a LATER ok disable/clear act → announced null. Same
# semantic the tier-1 history path already uses (web_payload cancels); the
# cancel must not leak into the unrelated plan-ideal slot (push-sourced).
def _cancel_hit(ts, result="ok"):
    return {"_source": {"ts": ts, "event": "act",
                        "action": {"type": "disable_on_timers"},
                        "timer_action": "disable", "result": result}}


def test_announced_dies_with_a_later_ok_disarm(payload_env):
    """THE §31 discrepancy regression: the live 11:38 gets cleared next
    morning → the hero must stop claiming "fires device-side, no WiFi
    needed" the moment the disarm lands."""
    _STUB["ann"] = _ann_hit("2026-10-25T20:59:26+00:00", "11:38")
    _STUB["cancel"] = _cancel_hit("2026-10-26T08:05:12+00:00")   # LATER
    _STUB["push"] = _push("Nachtplan final: timer 08:44",
                          "2026-10-25T20:57:22+00:00")
    d = wp.fetch_gridguard()["decision"]
    assert d["announced_on_at"] is None            # RED: was "11:38"
    assert d["announced_fires_at"] is None
    # the cancel must NOT leak into unrelated slots
    assert d["planned_on_at"] == "08:44"           # plan ideal = push-sourced
    # the cancel query itself = same semantic as tier-1: newest-first, ok only
    q = _STUB.get("cancel_query") or ""
    assert '"ts": "desc"' in q and '"result": "ok"' in q
    assert "disable_on_timers" in q and "timer_action" in q


def test_a_newer_write_after_a_disarm_wins_announced_again(payload_env):
    """ensure_on → later disable → ensure_on AGAIN: the newest write is the
    armed truth again (the cancel only kills generations it POST-dates)."""
    _STUB["ann"] = _ann_hit("2026-10-25T21:30:00+00:00", "05:26")   # newest write
    _STUB["cancel"] = _cancel_hit("2026-10-25T21:00:00+00:00")      # older
    d = wp.fetch_gridguard()["decision"]
    assert d["announced_on_at"] == "05:26"
    assert d["announced_fires_at"] is not None


def test_a_failed_disarm_is_not_a_cancel(payload_env):
    """result != ok never killed a program (the tier-1 cancels filter ok
    only — mirrored here so a refused cloud write cannot blank the page)."""
    _STUB["ann"] = _ann_hit("2026-10-25T20:59:26+00:00", "11:38")
    _STUB["cancel"] = _cancel_hit("2026-10-26T08:05:12+00:00",
                                  result="failed")
    d = wp.fetch_gridguard()["decision"]
    assert d["announced_on_at"] == "11:38"
