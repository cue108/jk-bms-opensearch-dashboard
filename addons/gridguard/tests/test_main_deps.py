"""F4 (final review): RealDeps write verifies + v4 plan executor (spec v4 §6).

RealDeps is wired against stubbed clients (monkeypatched in gridguard.main /
gridguard.audit) — no network, no disk; the timers dict IS the device truth.
v4: apply_plan executes keep_grid (no write, audited with reason),
failsafe_timer (the ensure-on programming path: fresh read → signature merge
→ write → read-back verify) and clear_grid_timers. release_night /
bring_on_now / none are dispatched in _eve via act() and never reach here.
"""
from datetime import datetime

import pytest
from gridguard.bms import BmsError
from gridguard.config import load_config
from gridguard.main import RealDeps
from gridguard.planner import TimerPlan, build_on_timer
from gridguard.switchlink import ChannelError

GOOD = {"TZ": "Europe/Madrid", "WINDOW_START": "09:00", "WINDOW_END": "22:00",
        "EVE_START": "20:00", "RESCUE_AT": "04:00", "CHARGE_MIN_A": "1.5",
        "OPENSEARCH_URL": "http://x", "BMS_INDEX": "i", "EWCLOUD_EMAIL": "a",
        "EWCLOUD_PASS": "b", "SW_MAIN_DEVICEID": "d", "SW_MAIN_DEVICEKEY": "k",
        "SW_MAIN_IP": "1.2.3.4", "NTFY_WEBHOOK": "https://n/t"}


class StubCloud:
    """In-memory CoolKit. `ignore_writes` simulates an accepted-but-lost write
    (error 0 on the wire, state unchanged) — the read-back must catch it."""
    def __init__(self, *a, **kw):
        self.timers, self.ignore_writes, self.fail_reads = [], False, False
        self.switch, self.set_calls = "off", 0
    def login(self): pass
    def get_device(self, deviceid):
        if self.fail_reads:
            raise ChannelError("cloud down")
        return {"params": {"switch": self.switch,
                           "timers": [dict(t) for t in self.timers]}}
    def get_inventory(self, deviceid):                    # read_state() shape (cloud truth)
        if self.fail_reads:
            raise ChannelError("cloud down")
        return {"switch": self.switch, "timers": [dict(t) for t in self.timers]}
    def set_timers(self, deviceid, timers):
        self.set_calls += 1
        if not self.ignore_writes:
            self.timers = [dict(t) for t in timers]
        return {"error": 0}
    def set_switch(self, deviceid, value):
        self.switch = value                               # read-back verify reads THIS
        return {"error": 0}


class StubLan:
    def __init__(self, *a, **kw): pass
    def set_switch(self, value): raise ChannelError("lan down")


class StubBms:
    def __init__(self, *a, **kw): pass
    def solar_day_boundaries(self, *a, **kw): return {}   # conservative: no data


class StubAudit:
    def __init__(self, path, **kw): self.events = []   # **kw: RealDeps wires cfg (§10b)
    def record(self, event, **fields): self.events.append((event, fields))


@pytest.fixture()
def make_deps(monkeypatch):
    def _make(**env):
        monkeypatch.setattr("gridguard.main.CloudClient", StubCloud)
        monkeypatch.setattr("gridguard.main.LanClient", StubLan)
        monkeypatch.setattr("gridguard.main.BmsSource", StubBms)
        monkeypatch.setattr("gridguard.audit.AuditLog", StubAudit)
        return RealDeps(load_config({**GOOD, **env}))
    return _make


@pytest.fixture()
def deps(make_deps):
    return make_deps()


def _events(deps):
    return [e for e, _ in deps.audit_log.events]


def _failsafe_plan(at):
    tz = load_config(GOOD).tz
    return TimerPlan(actions=[("failsafe_timer",
                               {"at_local": at, "timer": build_on_timer(at, tz),
                                "fire_str": at.strftime("%H:%M")})])


def test_apply_plan_failsafe_timer_verified(deps):
    at = datetime(2026, 10, 19, 1, 10)
    # write sticks → signature present after read-back → True + ensure_on audit
    assert deps.apply_plan(_failsafe_plan(at)) is True
    assert [t["atLocal"] for t in deps.cloud.timers] == ["10 1 * * *"]
    ev = deps.audit_log.events[-1]
    assert ev[0] == "act" and ev[1]["timer_action"] == "ensure_on" and ev[1]["result"] == "ok"
    # accepted-but-lost write → post-read lacks the signature → False + verify_failed
    deps.cloud.timers, deps.cloud.ignore_writes = [], True
    assert deps.apply_plan(_failsafe_plan(at)) is False
    assert deps.cloud.timers == []                         # device truth: nothing landed
    assert "verify_failed" in [f.get("result") for _, f in deps.audit_log.events]


def test_apply_plan_failsafe_timer_dryrun_suppresses(make_deps):
    deps = make_deps(DRY_RUN="1")
    at = datetime(2026, 10, 19, 1, 10)
    assert deps.apply_plan(_failsafe_plan(at)) is True
    assert deps.cloud.timers == [] and deps.cloud.set_calls == 0   # nothing written
    assert "dry_run" in _events(deps)


def test_apply_plan_rejects_dispatch_only_kinds(deps):
    # _eve-dispatched kinds never reach the executor — pin the boundary
    for kind in ("bring_on_now", "release_night", "none", "ensure_grid_on_now"):
        with pytest.raises(ValueError):
            deps.apply_plan(TimerPlan(actions=[(kind, {})]))


def test_act_reason_lands_in_audit(deps):
    # §9.7: the act line carries the reason (only when one was passed)
    assert deps.act("switch_on", require_verify=True,
                    reason="reason: night need not yet determinable") is True
    ev = [f for _, f in deps.audit_log.events if _ == "act"][-1]
    assert ev["inputs"] == {"reason": "reason: night need not yet determinable"}
    deps.act("switch_off", require_verify=True)              # no reason → no inputs field
    ev2 = [f for e, f in deps.audit_log.events if e == "act"][-1]
    assert "inputs" not in ev2


def test_apply_plan_clear_grid_timers_deactivates(deps):
    deps.cloud.timers = [{"type": "repeat", "do": {"switch": "on"}, "enabled": 1,
                          "atLocal": "10 1 * * *"}]
    plan = TimerPlan(actions=[("clear_grid_timers", {})])
    assert deps.apply_plan(plan) is True
    assert deps.cloud.timers[0]["enabled"] == 0            # deactivated, never deleted


def test_apply_plan_crossing_move_replaces_stale_on_timer(deps):
    # owner live find 2026-10-22 (08:01 → 08:13 drift): the eve re-computed the
    # crossing and re-armed; the merge keyed on (switch, atLocal) KEPT the
    # stale 08:01 timer and appended 08:13 — the device held BOTH and would
    # fire the STALE one. Dedup = the STATE target (do.switch), regardless of
    # atLocal; foreign timers (other state / other kind) survive untouched.
    # UPDATE (owner physics ruling 2026-10-26): a crossing move of ≤ 20 min is
    # now a DEADBAND skip (no write) — the eviction proof needs a REAL move:
    # 08:01 → 08:41 (40 min > WRITE_DEADBAND_MIN) still evicts the stale one.
    tz = load_config(GOOD).tz
    deps.cloud.timers = [
        build_on_timer(datetime(2026, 10, 22, 8, 1), tz),      # the stale crossing
        {"type": "repeat", "do": {"switch": "off"}, "enabled": 1,
         "atLocal": "0 4 * * *"},                              # foreign state — keep
        {"type": "repeat", "do": {"thing": "lamp"}, "enabled": 1,
         "atLocal": "0 5 * * *"},                              # foreign kind — keep
    ]
    assert deps.apply_plan(_failsafe_plan(datetime(2026, 10, 22, 8, 41))) is True
    ats = [t["atLocal"] for t in deps.cloud.timers]
    assert "1 8 * * *" not in ats                              # the STALE on-timer is gone
    assert ats.count("41 8 * * *") == 1                        # exactly the fresh one
    assert "0 4 * * *" in ats and "0 5 * * *" in ats           # foreign timers survive


def test_apply_plan_keep_grid_no_write_audits_keep_with_reason(deps):
    deps.cloud.timers = [{"type": "repeat", "do": {"switch": "on"}, "enabled": 1,
                          "atLocal": "10 1 * * *"}]
    before = [dict(t) for t in deps.cloud.timers]
    plan = TimerPlan(actions=[("keep_grid", {
        "reason": "available 768 Wh < 1.2× night need 800 Wh"})])
    assert deps.apply_plan(plan) is True                   # no write by design
    assert deps.cloud.timers == before and deps.cloud.set_calls == 0
    assert deps.cloud.switch == "off"                      # planner never auto-switches OFF
    ev = deps.audit_log.events[-1]
    assert ev[0] == "act" and ev[1]["timer_action"] == "keep" and ev[1]["result"] == "ok"
    assert ev[1]["inputs"] == {"reason": "available 768 Wh < 1.2× night need 800 Wh"}


def test_timer_exists_reads_device_truth(deps):
    on_timer = {"type": "repeat", "do": {"switch": "on"}, "enabled": 1,
                "atLocal": "10 1 * * *"}
    assert deps.timer_exists() is False
    deps.cloud.timers = [on_timer]
    assert deps.timer_exists() is True
    deps.cloud.timers = [{**on_timer, "enabled": 0}]        # deactivated inventory never counts
    assert deps.timer_exists() is False
    deps.cloud.timers = [{"type": "repeat", "do": {"switch": "off"}, "enabled": 1}]
    assert deps.timer_exists() is False
    deps.cloud.fail_reads = True
    assert deps.timer_exists() is False                     # ChannelError → conservative False


# ---- solar windows (owner ruling 2026-10-22: one concept everywhere) --------
from datetime import date as _date, datetime as _datetime, timedelta as _timedelta


class _StubBmsWindows:
    def __init__(self, bounds=None):
        self.calls = 0
        self.dates = None                    # F5-3: captures the requested window
        self._bounds = bounds                # optional two-day measurement fixture

    def solar_day_boundaries(self, dates, tz_name, flow_a, sun_fn):
        self.calls += 1
        self.dates = list(dates)
        if self._bounds is not None:
            return self._bounds
        d = _date(2026, 10, 21)
        return {d: (_datetime(2026, 10, 21, 9, 13),
                    _datetime(2026, 10, 21, 18, 35))}


class _StubWeather:
    def sun_times(self, target_date):
        return (_datetime(2026, 10, 21, 8, 30), _datetime(2026, 10, 21, 19, 30))


def test_solar_night_windows_composes_and_caches(make_deps):
    deps = make_deps()
    deps.bms = _StubBmsWindows()          # injected fakes, real composition
    deps.weather = _StubWeather()
    spans1 = deps.solar_night_windows()
    spans2 = deps.solar_night_windows()   # cached: boundaries queried ONCE
    assert deps.bms.calls == 1
    # single measured day → no completed night yet (needs a preceding day)
    assert spans1 == spans2 == {}


def test_solar_night_windows_fail_silent(make_deps):
    deps = make_deps()

    class _Boom:
        def __init__(self):
            self.calls = 0

        def solar_day_boundaries(self, *a, **k):
            self.calls += 1
            raise RuntimeError("cluster down")
    deps.bms = _Boom()
    deps.weather = _StubWeather()
    assert deps.solar_night_windows() == {}     # conservative: no windows
    # F5-2: a cluster failure is NOT cached for the local day — like
    # night_profile/night_buckets, the NEXT call retries the cluster.
    assert deps.solar_night_windows() == {}
    assert deps.bms.calls == 2                   # retried, not pinned empty


def test_solar_windows_dates_list_and_two_day_span(make_deps):
    """F5-3: pins the requested dates list (rolling hist_days window ending
    at today) AND a REAL two measured days → night(day2) = (day1 END,
    day2 START). Days are relative to today so the test stays deterministic
    against the wall clock."""
    deps = make_deps()
    today = datetime.now(deps.cfg.tz).date()
    day1, day2 = today - _timedelta(days=2), today - _timedelta(days=1)

    def _stamp(d, h, m):
        return _datetime(d.year, d.month, d.day, h, m)
    deps.bms = _StubBmsWindows({
        day1: (_stamp(day1, 9, 13), _stamp(day1, 18, 35)),
        day2: (_stamp(day2, 9, 20), _stamp(day2, 18, 40)),
    })
    deps.weather = _StubWeather()
    spans = deps.solar_night_windows()
    # the window: hist_days back … today, one entry per calendar day
    assert deps.bms.dates[0] == today - _timedelta(days=deps.cfg.hist_days)
    assert deps.bms.dates[-1] == today
    assert len(deps.bms.dates) == deps.cfg.hist_days + 1
    # two measured days → the composed span for day2 (day1 END … day2 START)
    assert spans == {day2: (_stamp(day1, 18, 35), _stamp(day2, 9, 20))}


# ---------------------------- L1 yield learning ----------------------------
def test_yield_learning_computes_factor_and_audits(make_deps):
    deps = make_deps()
    from datetime import datetime as _dt3, date as _d3

    tz = deps.cfg.tz                                  # aware bounds (prod contract)
    class _LBms:
        def solar_day_boundaries(self, dates, tz_name, flow_a, sun_fn):
            d = _d3(2026, 10, 20)                    # last completed measured day
            return {d: (_dt3(2026, 10, 20, 7, 15, tzinfo=tz),
                        _dt3(2026, 10, 20, 18, 33, tzinfo=tz))}
        def day_charge_kwh(self, a, b):
            return 1.35
        def forecast_doc_for(self, day, tz_name):
            return {"ts": "2026-10-20T09:10:00+00:00",
                    "radiation_sum_j": 16_452_000.0, "kind": "forecast"}
    deps.bms = _LBms()
    out = deps.yield_learning()
    assert out is not None
    assert out["day"] == "2026-10-20"
    assert out["measured_kwh"] == 1.35
    assert out["forecast_kwh_m2"] == pytest.approx(4.57)
    assert out["factor"] == pytest.approx(1.35 / 4.57, rel=1e-3)
    ev = [f for e, f in deps.audit_log.events if e == "yield_learned"]
    assert ev and ev[0]["inputs"]["factor"] == out["factor"]
    # cached: second call does not re-learn or re-audit
    deps.yield_learning()
    assert len([e for e, _ in deps.audit_log.events
                if e == "yield_learned"]) == 1


def test_yield_learning_today_counts_only_after_calendar_sunset(make_deps):
    """Owner rule2026-10-23: the length of an ONGOING solar day is only
    final AT/AFTER calendar sunset — today's provisional last-spike (b <= now
    while the sun is still up) must NOT count as a completed day (mid-day
    clouds would otherwise poison the learning with a truncated day)."""
    from datetime import datetime as _dt9, timedelta as _td9

    deps = make_deps()
    tz = deps.cfg.tz
    today = _dt9.now(tz).date()
    a = _dt9(today.year, today.month, today.day, 6, 50, tzinfo=tz)
    b = _dt9.now(tz) - _td9(minutes=30)               # provisional last spike

    class _LBms9:
        def solar_day_boundaries(self, dates, tz_name, flow_a, sun_fn):
            return {today: (a, b)}
        def day_charge_kwh(self, start, end):
            return 1.35                                # truncated "day"
        def forecast_doc_for(self, day, tz_name):
            return {"ts": today.isoformat(),
                    "radiation_sum_j": 16_452_000.0, "kind": "forecast"}

    class _Wx9:
        def sun_times(self, d):
            # sunset still ahead (offset sign flips the case)
            delta = self._delta
            return (a, _dt9.now(tz) + _td9(minutes=delta))

    deps.bms = _LBms9()
    deps.weather = _Wx9()
    deps.weather._delta = 480                         # sunset in 8h
    assert deps.yield_learning() is None              # ongoing day: NO learning
    assert not [e for e, _ in deps.audit_log.events if e == "yield_learned"]

    # the same day learns once the calendar sunset has passed
    deps2 = make_deps()
    deps2.bms = _LBms9()
    deps2.weather = _Wx9()
    deps2.weather._delta = -5                         # sunset 5 min ago
    out = deps2.yield_learning()
    assert out is not None and out["day"] == today.isoformat()


def test_yield_learning_naive_sunset_from_weather_client_contract(make_deps):
    """PRODUCTION CONTRACT regression (tick_error 2026-10-23 20:07+): the
    weather client returns NAIVE local datetimes (weather.py sun_times =
    fromisoformat of offset-less open-meteo strings) while bounds/now are
    tz-aware — the finality compare must normalize, never TypeError."""
    from datetime import datetime as _dt8, timedelta as _td8

    deps = make_deps()
    tz = deps.cfg.tz
    today = _dt8.now(tz).date()
    a = _dt8(today.year, today.month, today.day, 6, 50, tzinfo=tz)
    b = _dt8.now(tz) - _td8(minutes=30)

    class _LBms8:
        def solar_day_boundaries(self, dates, tz_name, flow_a, sun_fn):
            return {today: (a, b)}
        def day_charge_kwh(self, start, end):
            return 1.0
        def forecast_doc_for(self, day, tz_name):
            return {"ts": today.isoformat(),
                    "radiation_sum_j": 3_600_000.0, "kind": "forecast"}

    class _WxNaive:                      # wie der Produktions-Client: NAIV
        def sun_times(self, d):
            return (_dt8.now(tz).replace(tzinfo=None),
                    (_dt8.now(tz) + _td8(hours=8)).replace(tzinfo=None))

    deps.bms = _LBms8()
    deps.weather = _WxNaive()
    assert deps.yield_learning() is None          # running day: no TypeError, no learning


def test_real_weather_client_parse_feeds_solar_windows_aware(make_deps):
    """TDD gap 2 (20:07 series): no test drove the REAL client parse
    (offset-less open-meteo strings → NAIVE datetimes) into the RealDeps path —
    the stubs had drifted off the production contract. Pin: the captured
    sunset must come out of solar_windows() tz-aware."""
    from datetime import datetime as _dt7
    from gridguard.weather import WeatherClient

    deps = make_deps()
    w = WeatherClient(36.7, -4.4)
    # production shape (live-checked 23.10.): offset-LESS local strings
    w._get = lambda url: {"daily": {"time": ["2026-10-23"],
                                    "sunrise": ["2026-10-23T08:32"],
                                    "sunset": ["2026-10-23T19:31"]}}
    deps.weather = w
    deps.solar_night_windows()                       # fires the sun parse
    sun = deps.solar_today_sunset()
    assert sun is not None and sun.tzinfo is not None  # AWARE — never naive
    assert _dt7.now(deps.cfg.tz) - sun               # comparable without TypeError


def test_apply_plan_ensure_on_write_refusal_is_audited_not_raised(deps):
    """TDD gap 23.10. (act_failed 21:40): the ensure-on WRITE branch was never
    tested — the REAL cloud refusal (ChannelError with the live-4002
    payload) must be absorbed: act_failed verbatim, False, NOTHING
    propagated into the eve (20:07 lesson) — and the next attempt heals."""
    real_4002 = ("cloud status write failed: "
                 "{'error': 4002, 'msg': '控制失败', 'data': {}}")
    at = datetime(2026, 10, 19, 1, 10)
    healthy = deps.cloud.set_timers

    def _refuse(deviceid, timers):
        deps.cloud.set_calls += 1
        raise ChannelError(real_4002)
    deps.cloud.set_timers = _refuse
    assert deps.apply_plan(_failsafe_plan(at)) is False      # never True without device truth
    failed = [f for e, f in deps.audit_log.events if e == "act_failed"]
    assert failed and failed[-1]["action"] == {"type": "failsafe_timer"}
    assert failed[-1]["timer_action"] == "ensure_on"
    assert failed[-1]["result"] == real_4002                 # wire-fixtures doctrine: live payload
    # healing: healthy channel on the next tick → True + act ok
    deps.cloud.set_timers = healthy
    assert deps.apply_plan(_failsafe_plan(at)) is True
    assert [t["atLocal"] for t in deps.cloud.timers] == ["10 1 * * *"]


def test_apply_plan_skips_write_when_actor_already_holds_signature(deps):
    """Owner2026-10-23 (4002 refusal): no redundant rewrite — ~15
    timer writes/evening loaded the cloud. If the actor carries exactly this
    program, the fresh read IS the verification → no write. A wild
    crossing still writes (the stale one must be evicted)."""
    at = datetime(2026, 10, 19, 1, 10)
    assert deps.apply_plan(_failsafe_plan(at)) is True        # first run: write needed
    assert deps.cloud.set_calls == 1
    assert deps.apply_plan(_failsafe_plan(at)) is True        # second run: identical
    assert deps.cloud.set_calls == 1                          # NO second write
    acts = [f for e, f in deps.audit_log.events if e == "act"]
    assert len(acts) == 1                                     # no second confirmation:
    # the write-act already confirmed this state (log readability 2026-10-23)
    # wild crossing → the write happens (dedup evicts the old one)
    later = datetime(2026, 10, 19, 1, 40)
    assert deps.apply_plan(_failsafe_plan(later)) is True
    assert deps.cloud.set_calls == 2
    assert [t["atLocal"] for t in deps.cloud.timers] == ["40 1 * * *"]


def test_apply_plan_collapses_identical_ok_acts_but_never_failures(deps):
    """Owner2026-10-23 (log readability): identical OK confirmations of one
    signature are audited only for the FIRST — changes and errors are never
    swallowed (the trail shows decisions, not the heartbeat)."""
    at = datetime(2026, 10, 19, 1, 10)
    assert deps.apply_plan(_failsafe_plan(at)) is True        # write needed → act ok (1)
    assert deps.apply_plan(_failsafe_plan(at)) is True        # identical → NO second act
    acts = [f for e, f in deps.audit_log.events if e == "act"]
    assert len(acts) == 1
    later = datetime(2026, 10, 19, 1, 40)
    assert deps.apply_plan(_failsafe_plan(later)) is True     # crossing moved → act ok (2)
    acts = [f for e, f in deps.audit_log.events if e == "act"]
    assert len(acts) == 2 and acts[-1]["timer_action"] == "ensure_on"
    # ERROR after suppression: the write is forced (different signature)
    # and refused → act_failed audited (never swallowed)
    def _refuse(deviceid, timers):
        raise ChannelError("cloud down")
    deps.cloud.set_timers = _refuse
    assert deps.apply_plan(_failsafe_plan(at)) is False
    assert [e for e, f in deps.audit_log.events if e == "act_failed"]


# -------- L1 per-class yield (owner ruling 2026-10-25, option B) -----------
# The pack shunt measures solar REGARDLESS of grid status (grid-ON = gross
# into the battery, grid-OFF = net after load coverage) → the learning factor
# line is kept per class. Only the LEARNING becomes state-dependent; the day
# threshold stays curve-based (Owner 2026-10-21). Thresholds: <10 % grid_off,
# >=50 % grid_on, in between mixed, no value → unknown.
def _learn_deps(make_deps, pct):
    """yield_learning fixture like test_yield_learning_computes_factor_and_audits,
    plus day_grid_on_pct (returns `pct`, or an Exception instance = raises)."""
    deps = make_deps()
    from datetime import datetime as _dt5, date as _d5
    tz = deps.cfg.tz

    class _LBms5:
        def solar_day_boundaries(self, dates, tz_name, flow_a, sun_fn):
            d = _d5(2026, 10, 20)
            return {d: (_dt5(2026, 10, 20, 7, 15, tzinfo=tz),
                        _dt5(2026, 10, 20, 18, 33, tzinfo=tz))}
        def day_charge_kwh(self, a, b):
            return 1.35
        def forecast_doc_for(self, day, tz_name):
            return {"ts": "2026-10-20T09:10:00+00:00",
                    "radiation_sum_j": 16_452_000.0, "kind": "forecast"}
        def day_grid_on_pct(self, a, b):
            if isinstance(pct, Exception):
                raise pct
            return pct
    deps.bms = _LBms5()
    return deps


def test_yield_learning_carries_grid_on_pct_and_class_grid_on(make_deps):
    deps = _learn_deps(make_deps, 96.4)
    out = deps.yield_learning()
    assert out is not None and out["factor"] > 0          # the factor line lives
    assert out["grid_on_pct"] == 96.4
    assert out["class"] == "grid_on"
    ev = [f for e, f in deps.audit_log.events if e == "yield_learned"]
    assert ev and ev[0]["inputs"]["class"] == "grid_on" \
        and ev[0]["inputs"]["grid_on_pct"] == 96.4


def test_yield_learning_share_queried_once_for_the_learned_day(make_deps):
    """ONE query per learned day — inside the same day-cache block as
    measured/forecast/factor (second call: cached, no new query)."""
    deps = _learn_deps(make_deps, 55.0)
    calls: list = []
    real = deps.bms.day_grid_on_pct

    def _wrap(a, b):
        calls.append((a, b))
        return real(a, b)
    deps.bms.day_grid_on_pct = _wrap
    out = deps.yield_learning()
    deps.yield_learning()                                # cached
    assert len(calls) == 1
    a, b = calls[0]                                      # exactly [day_start, day_end]
    assert a.isoformat(timespec="minutes") == out["day_start"]
    assert b.isoformat(timespec="minutes") == out["day_end"]


@pytest.mark.parametrize("pct,class_name", [
    (9.99, "grid_off"),          # lower boundary: pct < 10.0
    (10.0, "mixed"),             # lower boundary incl. = mixed
    (49.99, "mixed"),
    (50.0, "grid_on"),           # upper boundary incl. = grid_on
])
def test_yield_learning_class_threshold_boundaries(make_deps, pct, class_name):
    out = _learn_deps(make_deps, pct).yield_learning()
    assert out is not None and out["class"] == class_name
    assert out["grid_on_pct"] == pct


def test_yield_learning_share_query_failure_keeps_the_factor_alive(make_deps):
    """The share query must NEVER take the learning with it: exception →
    grid_on_pct None, class "unknown", factor line + audit stay."""
    deps = _learn_deps(make_deps, BmsError("events index unreachable"))
    out = deps.yield_learning()
    assert out is not None and out["factor"] > 0
    assert out["grid_on_pct"] is None and out["class"] == "unknown"
    ev = [f for e, f in deps.audit_log.events if e == "yield_learned"]
    assert ev and ev[0]["inputs"]["class"] == "unknown"


def test_yield_learning_share_none_is_unknown(make_deps):
    """Honest None (no data) → class "unknown", never a fabricated
    grid_off or grid_on day."""
    out = _learn_deps(make_deps, None).yield_learning()
    assert out is not None and out["grid_on_pct"] is None
    assert out["class"] == "unknown"


def test_realdeps_day_grid_on_pct_wires_events_index_and_delegates(make_deps):
    """RealDeps mirrors the cfg events-index prefix onto the Bms query client
    (like weather_index) and delegates the share computation there."""
    deps = make_deps()
    assert deps.bms.events_index == "gridguard-events-*"
    got: list = []
    deps.bms.day_grid_on_pct = lambda a, b: (got.append((a, b)), 42.0)[1]
    a = datetime(2026, 10, 20, 7, 15)
    b = datetime(2026, 10, 20, 18, 33)
    assert deps.day_grid_on_pct(a, b) == 42.0 and got == [(a, b)]
    deps2 = make_deps(EVENTS_INDEX_PREFIX="myevents")
    assert deps2.bms.events_index == "myevents-*"


# -------- restart-window safety (owner incident 2026-10-25 09:37:39 UTC) -------
# The seed (run() before the loop) asks for the newest reserve_tier decision
# over the SAME Bms query client as day_grid_on_pct — RealDeps
# delegates there unchanged (honest None flows through).
def test_realdeps_latest_reserve_tier_delegates_to_the_bms_query(make_deps):
    deps = make_deps()
    got: list = []
    deps.bms.latest_reserve_tier = lambda: (got.append(1), {"tier_pct": 30})[1]
    assert deps.latest_reserve_tier() == {"tier_pct": 30} and got == [1]
    deps.bms.latest_reserve_tier = lambda: None         # honest None flows through
    assert deps.latest_reserve_tier() is None


# -------- display truth (owner doctrine 2026-10-26): every programming write
# records ITS OWN time — the page's "announced" reads the last write-
# confirmed ensure_on act (Brief: announced = write-confirmed programming;
# plan ideal stays a separate, separately-labelled field) ----------------
def test_apply_plan_ensure_on_audit_carries_programmed_time(deps):
    """The ensure_on act audit (result ok) carries inputs {fire_str, at_local}
    — the write-confirmed programming time of every ladder generation that the
    page renders as 'announced'. Before, the act events carried NO time
    (live find 2026-10-26: 0 of 1707 events with fire_str/at_local)."""
    at = datetime(2026, 10, 19, 1, 10)
    assert deps.apply_plan(_failsafe_plan(at)) is True
    ev = deps.audit_log.events[-1]
    assert ev[0] == "act" and ev[1]["timer_action"] == "ensure_on"
    assert ev[1]["result"] == "ok"
    assert ev[1]["inputs"] == {"fire_str": "01:10",
                               "at_local": "2026-10-19T01:10"}


def test_apply_plan_verify_failed_audit_also_carries_programmed_time(deps):
    """verify_failed (result != ok) is NOT a write confirmation — the
    announced filter excludes it — but the ATTEMPTED generation stays
    readable in the journal with its time (every ladder generation auditable)."""
    at = datetime(2026, 10, 19, 1, 10)
    assert deps.apply_plan(_failsafe_plan(at)) is True
    deps.cloud.timers, deps.cloud.ignore_writes = [], True
    assert deps.apply_plan(_failsafe_plan(at)) is False
    vf = [f for e, f in deps.audit_log.events
          if e == "act" and f.get("result") == "verify_failed"]
    assert vf and vf[-1]["inputs"] == {"fire_str": "01:10",
                                       "at_local": "2026-10-19T01:10"}


# ---- owner PHYSICS ruling 2026-10-26 (§19): the write DEADBAND ---------------
# Reprogram the actor only when the new crossing differs from the ARMED one by
# more than WRITE_DEADBAND_MIN = 20 min (owner-approved 2026-10-26). The dedup
# gains threshold semantics: |new − armed| <= X → silent skip, audited once as
# a noop "within deadband"; beyond X → the write evicts the stale timer (the
# 2026-10-22 eviction guarantee, now scoped to real moves). The armed timer is
# the anchor in EVERY phase — inside the FINAL window the same semantics hold
# (the crossing converges; nothing is rewritten while it drifts within X).
from gridguard.main import WRITE_DEADBAND_MIN


def _armed(deps, hh, mm):
    deps.cloud.timers = [{"type": "repeat", "do": {"switch": "on"}, "enabled": 1,
                          "atLocal": f"{mm} {hh} * * *"}]


def test_deadband_constant_is_owner_approved_20():
    assert WRITE_DEADBAND_MIN == 20


def test_deadband_skips_the_write_within_20_minutes(deps):
    _armed(deps, 3, 10)                                   # the armed crossing
    assert deps.apply_plan(_failsafe_plan(datetime(2026, 10, 19, 3, 29))) is True
    assert deps.cloud.set_calls == 0                       # NO write — 19 min ≤ 20
    assert [t["atLocal"] for t in deps.cloud.timers] == ["10 3 * * *"]  # device truth kept
    ev = [f for e, f in deps.audit_log.events if e == "act"]
    assert ev[-1]["timer_action"] == "noop" and ev[-1]["result"] == "ok"
    assert ev[-1]["inputs"] == {"within_deadband_min": 19,
                                "armed": "10 3 * * *", "wanted": "29 3 * * *"}
    # the skip is quiet after the first line per armed state (no tick spam)
    assert deps.apply_plan(_failsafe_plan(datetime(2026, 10, 19, 3, 25))) is True
    assert len([f for e, f in deps.audit_log.events if e == "act"]) == 1


def test_deadband_boundary_20_skips_21_writes(deps):
    _armed(deps, 3, 10)
    assert deps.apply_plan(_failsafe_plan(datetime(2026, 10, 19, 3, 30))) is True
    assert deps.cloud.set_calls == 0                       # exactly 20 → still inside
    assert deps.apply_plan(_failsafe_plan(datetime(2026, 10, 19, 3, 31))) is True
    assert deps.cloud.set_calls == 1                       # 21 min → the write lands
    assert [t["atLocal"] for t in deps.cloud.timers] == ["31 3 * * *"]


def test_deadband_is_circular_across_midnight(deps):
    _armed(deps, 23, 55)                                   # armed before midnight
    assert deps.apply_plan(_failsafe_plan(datetime(2026, 10, 19, 0, 10))) is True
    assert deps.cloud.set_calls == 0                       # 15 min circular → skip
    assert deps.apply_plan(_failsafe_plan(datetime(2026, 10, 19, 0, 40))) is True
    assert deps.cloud.set_calls == 1                       # 45 min circular → write


def test_deadband_never_hides_an_unparseable_armed_signature(deps):
    # device truth that cannot be compared → NO deadband grace: the write
    # converges device truth (evicts/normalizes), never silently skips.
    deps.cloud.timers = [{"type": "repeat", "do": {"switch": "on"}, "enabled": 1,
                          "atLocal": "garbage"}]
    assert deps.apply_plan(_failsafe_plan(datetime(2026, 10, 19, 3, 29))) is True
    assert deps.cloud.set_calls == 1
    assert [t["atLocal"] for t in deps.cloud.timers] == ["29 3 * * *"]
