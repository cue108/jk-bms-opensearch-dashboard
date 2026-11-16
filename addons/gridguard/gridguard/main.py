"""GridGuard loop — the only clock in the system."""
from __future__ import annotations

import sys
import time as _time
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta

from .bms import BmsError, BmsSource
from .config import Config, ConfigError, load_config
from .learn import NightProfile, learn_profile, night_need_wh
from .messages import msg, title
from .notify import push as ntfy_push
from .periods import night_windows
from .planner import (TimerPlan, build_on_timer, hold_reason, local_cron,
                      night_floor_wh, plan_evening, reference_crossing)
from .reach import reachable as _reachable
from .reference import (audit_inputs as reference_audit_inputs,
                        reference_load as build_reference_load)
from .solar_predict import solar_day_prediction as _solar_day_prediction
from .switchlink import ChannelError, CloudClient, LanClient, deactivate_grid_timers
from .verdict import (EveVerdict, Phase, active_tier_on, corrected_min_cell_v,
                      current_phase, deep_floor_hit, reserve_tier,
                      voltage_floor_hit)
from .weather import WeatherClient, load_outlook, ship_forecast, ship_outlook

DEFAULT_NIGHT_LEN_H = 11.0   # fallback when the learned night edges are unknown
GRID_OFF_SUPPRESS_REASON = "grid-off rule active/contested — grid stays off, restrained as before"
# Per-class yield learning: the pack shunt measures solar DEPENDING ON THE
# GRID STATE (grid-ON day = gross solar into the pack, grid-OFF day = net
# after covering the load) → the learning keeps ONE factor per class, keyed
# by the day's grid-ON share (state_observed + switch acts from the events
# index). Class: pct < GRID_OFF_CLASS_PCT → "grid_off", pct >=
# GRID_ON_CLASS_PCT → "grid_on", between → "mixed", unknown share →
# "unknown". The solar-day boundary rule stays curve-only — this is learning
# state-awareness only, display-only, never a gate.
GRID_OFF_CLASS_PCT = 10.0
GRID_ON_CLASS_PCT = 50.0
# --- the write DEADBAND -------------------------------------------------------
# Reprogram the actor only when the new crossing differs from the ARMED one by
# more than WRITE_DEADBAND_MIN minutes. apply_plan's dedup gains threshold
# semantics: |new − armed| <= X → silent skip (audited once as a noop
# "within deadband"); beyond X → the write evicts the stale timer. Rationale:
# device stability + trail noise — unbounded rewrites churn the device and
# flood the audit trail.
WRITE_DEADBAND_MIN = 20
# The MEASURED onset is a live horizon source only while FRESH — its last
# completed span end must be at most this many hours old; beyond it the
# sunless chain has stepped past (it slips one day per sunless day) and the
# solar-day prediction supplies the horizon instead. 36 h at the eve consult
# ≈ "the last measured morning is not older than yesterday" (one sunless day
# tips over; the normal same-day edge sits at ~12 h).
MEASURED_ONSET_MAX_AGE_H = 36.0
# the Wh-conversion voltage average window: a cheap cluster-side mean over
# the recent minutes (the conversion only — the instantaneous pack voltage is
# a minor but real error term, so it is averaged)
PACK_V_AVG_MIN = 30
# override recognition: the loop cadence is ~10 min; a longer observation gap
# = actor-night / outage / restart → the first read after it RE-BASELINES
# silently (our own programmed night-timer fires across such gaps — flagging
# it would false-positive every morning)
OVERRIDE_MAX_GAP = timedelta(minutes=45)


def is_rescue_timer(t: dict) -> bool:
    """A grid-ON timer = repeat timer that switches ON and is still enabled.
    There is NO fixed atLocal anymore (t_fire is computed per eve and
    programmed through apply_plan), so identity is direction + state —
    deactivated (enabled:0) inventory never counts as armed. Shared by
    RealDeps.timer_exists and the eve's carries-flip disarm (device truth
    from the fresh read — covers a restart that lost the in-memory flag)."""
    return (t.get("type") == "repeat"
            and (t.get("do") or {}).get("switch") == "on"
            and t.get("enabled", 1) != 0)


def _cron_minutes(sig: str | None) -> int | None:
    """Minutes-of-day of a local 'M H * * *' signature; None when unparseable."""
    try:
        parts = str(sig).split()
        return (int(parts[1]) % 24) * 60 + (int(parts[0]) % 60)
    except (IndexError, ValueError, AttributeError):
        return None


def crossing_move_min(sig_a: str | None, sig_b: str | None) -> int | None:
    """Circular minute distance between two crossing signatures (23:55 → 00:10
    = 15 min); None when either side is unparseable (→ no deadband grace)."""
    ma, mb = _cron_minutes(sig_a), _cron_minutes(sig_b)
    if ma is None or mb is None:
        return None
    d = abs(ma - mb) % 1440
    return min(d, 1440 - d)


def _measured_onset_base(spans: dict) -> datetime | None:
    """Primary onset source: the MEASURED last completed solar night's
    END (_e = the array-onset datetime the eve already fetches — the system's
    own sunrise), as naive local wall time (the run() contract)."""
    if not spans:
        return None
    _s, _e = spans[max(spans)]
    if _e is None:
        return None
    return _e.replace(tzinfo=None) if _e.tzinfo is not None else _e


def _onset_on_day(day: date, onset_h: float) -> datetime:
    """Learned profile hour anchored to a local calendar day (naive)."""
    mins = int(round(onset_h * 60)) % 1440
    return datetime(day.year, day.month, day.day, mins // 60, mins % 60)


def next_solar_onset_src(now: datetime, spans: dict, profile,
                         predicted: dict | None = None
                         ) -> tuple[datetime | None, str]:
    """Next array-onset source chain — the eve clamp's boundary WITH its label
    (display truth: the source rides the night_need audit; a prediction is a
    LABEL, never state):
    (1) MEASURED — the last completed solar-night span end stepped +24 h while
        <= now, BUT only while fresh: age <=
        MEASURED_ONSET_MAX_AGE_H, else the sunless chain has stepped past it
        and the measured edge no longer extends;
    (2) PREDICTED — solar_day_prediction (predicted True with an
        onset_h): onset_h anchored to its next occurrence after `now` —
        the sunless chain's horizon;
    (3) PROFILE — the learned profile.array_onset_h (the existing
        fallback);
    (4) None → no guess; the length clamp stays the last guard.
    Returns (onset, source) with source ∈ measured | predicted | profile |
    none."""
    base = _measured_onset_base(spans)
    if base is not None and (now - base).total_seconds() / 3600.0 \
            <= MEASURED_ONSET_MAX_AGE_H:
        while base <= now:
            base += timedelta(days=1)
        return base, "measured"
    if isinstance(predicted, dict) and predicted.get("predicted") is True:
        h = predicted.get("onset_h")
        if isinstance(h, (int, float)) and not isinstance(h, bool):
            cand = _onset_on_day(now.date(), float(h))
            if cand <= now:
                cand = _onset_on_day(now.date() + timedelta(days=1), float(h))
            return cand, "predicted"
    if profile is not None and profile.array_onset_h is not None:
        cand = _onset_on_day(now.date(), profile.array_onset_h)
        if cand <= now:
            cand = _onset_on_day(now.date() + timedelta(days=1),
                                 profile.array_onset_h)
        return cand, "profile"
    return None, "none"


def next_solar_onset(now: datetime, spans: dict, profile,
                     predicted: dict | None = None) -> datetime | None:
    """The ABSOLUTE next array-onset AFTER `now` (naive local) — the
    eve-clamp's boundary; see next_solar_onset_src for the labelled chain.
    PRIMARY = the measured span end stepped +24 h while fresh (≤
    MEASURED_ONSET_MAX_AGE_H), else the predicted onset, else the learned
    profile hour anchored to the coming local day, else None — no guess; the
    planner's length-clamp stays the sole guard."""
    return next_solar_onset_src(now, spans, profile, predicted)[0]


def solar_onset_on(day: date, spans: dict, profile) -> datetime | None:
    """The array-onset BELONGING to `day` (naive local). The measured base
    steps ±24 h until the DATE matches — the seasonal drift shifts the CLOCK
    by minutes, never the date (the anchoring rule); the learned profile hour
    lands on the date directly. None = no edge known → the clearing step
    stays silent (no guess)."""
    base = _measured_onset_base(spans)
    if base is not None:
        while base.date() < day:
            base += timedelta(days=1)
        while base.date() > day:
            base -= timedelta(days=1)
        return base
    if profile is not None and profile.array_onset_h is not None:
        return _onset_on_day(day, profile.array_onset_h)
    return None


@dataclass
class AppState:
    morning_done: bool = False
    night_over: bool = False
    rescue_timer_set: bool = False
    final_pushed: bool = False
    eve_ran: bool = False           # TODAY's eve consult actually ran — the
                                    # gate for the closing final push (a restart
                                    # that never reached the eve must not push
                                    # fresh-state claims); reset at day rollover
    evening_verdict: str | None = None
    emergency_ack: date | None = None
    low_capacity_ack: date | None = None   # the two floors ack
                                           # SEPARATELY — a capacity event must never
                                           # suppress the emergency (no interference)
    floor_blocked_audit: date | None = None  # one blocked-audit/day
                                             # that is NOT an ack — the floor still
                                             # fires the moment the link returns
    deep_pushed: date | None = None          # one danger push per day while blind
    current_date: date | None = None       # key of the day-scoped latches
    rescue_fallback: bool | None = None    # actual outcome of the grid-ON fallback
    last_read_fail_push: datetime | None = None   # throttle human spam, audit stays full
    reach_blocked: bool = False          # the reach-outage transition latch
    last_planned_fire: datetime | None = None     # WRITE-CONFIRMED armed (device
                                                  # truth — stamped by the executor;
                                                  # t_fire actually applied, today)
    plan_ideal: datetime | None = None     # the planner's LATEST crossing (intent,
                                           # re-projected every eve tick — separate from
                                           # the armed; the final push labels it when it
                                           # differs from the write-confirmed time)
    last_observed_switch: str | None = None   # override recognition: last READ actor state
    last_observed_at: datetime | None = None  # … and when (a gap → silent re-baseline)
    # --- weather-conditional RESERVE TIERS -------------------------------------
    reserve_tier_pct: int = 0          # the tier (% of BMS SOC) — set at the eve for
                                       # the coming day, NOT cleared at day rollover,
                                       # overwritten at the next eve (and re-evaluated
                                       # on every forecast/outlook revision)
    reserve_tier_sig: tuple | None = None   # audit guard: the last AUDITED decision
                                            # (tier, triggers, window dates) — the tier
                                            # is recomputed on every consult/revision,
                                            # the reserve_tier line fires per CHANGE
    tier_hold_pct: int = 0             # guarded tier_hold audit: the tier under which
                                       # the release hold is engaged (0 = no hold) —
                                       # one line per engage/tier change, never per tick


def tick(cfg: Config, st: AppState, deps, now: datetime, *, probe_due: bool = False) -> None:
    """One decision step. `deps` is duck-typed (RealDeps or a test fake)."""
    # --- day rollover BEFORE the phase runs (phase reads morning_done!) ---
    if st.current_date is None:
        st.current_date = now.date()       # first tick establishes the day (fresh latches)
    elif st.current_date != now.date():
        st.current_date = now.date()
        st.morning_done = False
        st.final_pushed = False
        st.eve_ran = False          # the closing push needs TODAY's eve
        st.evening_verdict = None
        st.rescue_fallback = None
        st.last_planned_fire = None       # "set today" scope for the FINAL text
        st.plan_ideal = None              # same day scope as the armed
        st.rescue_timer_set = deps.timer_exists()   # re-sync flag with device truth
    phase = current_phase(now, cfg, st.morning_done, st.night_over)
    deps.audit("tick", phase=phase.value)

    if phase is Phase.NIGHT:
        # the OVERNIGHT RE-ESTIMATION runs FIRST in the NIGHT branch, so the
        # closing push below (and every later announced/plan read) states the
        # WRITE truth after it.
        _night_resolve(cfg, st, deps, now)
        # --- the FINAL push fires at the eve's CLOSING — the FIRST tick at/after
        # window_end, i.e. the phase transition out of FINAL (verdict.current_phase:
        # t >= window_end → NIGHT, and _eve only ever runs in EVE_PLAN/FINAL), so
        # no further timer write for tonight is possible and the latches are
        # settled. Chosen over "the last FINAL tick": the closing is decided by
        # the phase function alone (single source), while a "last FINAL tick"
        # would need cadence arithmetic that cannot know the next tick's instant.
        # eve_ran = the eve actually rolled today (a restart that never reached
        # the eve must not push fresh-state claims);
        # final_pushed = ONE push per eve/day (re-armed by the day rollover).
        if st.eve_ran and not st.final_pushed and now.time() >= cfg.window_end:
            st.final_pushed = True
            armed_s = (st.last_planned_fire.strftime("%H:%M")
                       if st.last_planned_fire is not None else None)
            ideal_s = (st.plan_ideal.strftime("%H:%M")
                       if st.plan_ideal is not None else None)
            if armed_s is None:
                # no write-confirmed timer this eve → honest "no timer" (the
                # payload's planned_on_at stays null on this text —
                # web_payload.py excludes "no timer").
                deps.push("NIGHT PLAN", msg(cfg.msg_lang, "night_plan",
                                           plan="no timer"))
            elif ideal_s is not None and ideal_s != armed_s:
                # BOTH truths — armed as the headline, the planner's ideal
                # LABELLED when it differs. Order is load-bearing: the payload
                # reads the LAST HH:MM of the newest NIGHT PLAN text as
                # planned_on_at (web_payload.py) → the ideal rides LAST.
                deps.push("NIGHT PLAN", msg(cfg.msg_lang, "night_plan_armed_ideal",
                                           armed=armed_s, ideal=ideal_s))
            else:
                deps.push("NIGHT PLAN", msg(cfg.msg_lang, "night_plan_armed",
                                           armed=armed_s))
            # additive inputs — both truth sources stay readable in the
            # journal (the event's verdict/action shape is unchanged).
            deps.audit("final_plan", verdict=st.evening_verdict or "unknown",
                       action={"type": "timer" if armed_s else "none"},
                       inputs={"armed": armed_s, "plan_ideal": ideal_s})
        if probe_due and deps.reachable():
            st.night_over = True
            deps.audit("night_over")
            return
        return

    # --- read-FIRST gate ---
    try:
        switch_state, timers = deps.read_state()
    except (ChannelError, RuntimeError) as e:   # nets raw RuntimeError too (401 shape)
        deps.audit("read_state_failed", result=str(e))   # audit = full fidelity, every tick
        # push = human signal, throttled to 1 per 6h (spammy 401 storms)
        if st.last_read_fail_push is None or \
                (now - st.last_read_fail_push) >= timedelta(hours=6):
            deps.push("NO CHANNEL", msg(cfg.msg_lang, "no_channel", action="read"))
            st.last_read_fail_push = now
        return

    # --- state journal + manual-override recognition ---
    # DATA QUALITY: inside the known WLAN-off window (22:00–10:00) nobody CAN
    # switch — no path to the actor — and a cloud read then returns the device's
    # LAST KNOWN state, never an actor response. Recording such reads produced
    # wrong data and a wrong conclusion (a false HAND-ON-THE-SWITCH). Therefore
    # state observations — baseline, transitions AND the override gap clock —
    # run ONLY on a reachable uplink; stale reads leave no trace. The gap then
    # always anchors at the last ACTOR-GROUNDED observation, so the first live
    # read after a blackout silently re-baselines (device timers fire precisely
    # when unreachable — that IS the pre-programming working), while a change
    # inside a live session still counts as a hand (manual_override + push).
    reach_ok = deps.reachable()   # ONE probe feeds journal + floors + observability
    if reach_ok:
        if st.last_observed_switch is None:
            # first LIVE read ever (process start) — baseline so the index
            # carries the current state (no "from": no predecessor known)
            deps.audit("state_observed", inputs={"to": switch_state})
        elif switch_state != st.last_observed_switch:
            deps.audit("state_observed", inputs={"from": st.last_observed_switch,
                                                 "to": switch_state})
            if (st.last_observed_at is not None
                    and (now - st.last_observed_at) <= OVERRIDE_MAX_GAP):
                deps.audit("manual_override", inputs={"from": st.last_observed_switch,
                                                      "to": switch_state})
                deps.push("MANUAL", msg(cfg.msg_lang, "manual_override",
                                         to=switch_state, at=now.strftime("%H:%M")))
        st.last_observed_switch = switch_state
        st.last_observed_at = now

    # --- universal emergency IN-guard (all awake phases) ---
    try:
        reading = deps.bms_latest()
    except Exception as e:   # ANY failure class (incl. data-shape types like
        # TypeError) nets to the same conservative fallback + a VISIBLE audit;
        # the decision survives its instrumentation.
        deps.audit("bms_unavailable", result=str(e))
        reading = None
    deep = reading is not None and deep_floor_hit(reading.min_cell_v, cfg)
    # the capacity trigger is VOLTAGE-ONLY — the corrected worst-cell voltage
    # decides; the BMS counter is display-only (it cut the night early while
    # cells still rested near 10 %).
    low = reading is not None and voltage_floor_hit(reading.min_cell_v,
                                                    reading.charge_a, cfg)
    # the two floors ack SEPARATELY — a capacity event must never suppress
    # the emergency of the same day.
    deep_unacked = deep and st.emergency_ack != now.date()
    low_unacked = low and st.low_capacity_ack != now.date()
    if (deep_unacked or low_unacked) and switch_state != "on":
        if not reach_ok:
            # NO fixed outage config — a usage-variable parameter (the WiFi
            # schedule) must never live in env; the LIVE link is the only
            # truth. While no path exists: no switching, audit ONCE per day via
            # a latch that is deliberately NOT an ack — so the floor FIRES THE
            # MOMENT THE LINK RETURNS, instead of waiting for programmed plans
            # or tomorrow.
            if st.floor_blocked_audit != now.date():
                st.floor_blocked_audit = now.date()
                _inputs = {"min_cell_v": reading.min_cell_v,
                           "min_cell_corr": corrected_min_cell_v(
                               reading.min_cell_v, reading.charge_a, cfg),
                           "floor_v": cfg.true_floor_v,
                           "remaining_ah": reading.remaining_ah,   # display-only
                           "charge_a": reading.charge_a,
                           "path": "unreachable"}
                if deep:
                    if st.deep_pushed != now.date():   # one danger push per day
                        st.deep_pushed = now.date()
                        deps.push("EMERGENCY", msg(cfg.msg_lang, "emergency_on",
                                                   min_v=reading.min_cell_v))
                    deps.audit("emergency_on", action={"type": "switch_on"},
                               result="blocked_unreachable", inputs=_inputs)
                else:
                    deps.audit("low_capacity_on", result="blocked_unreachable",
                               inputs=_inputs)
            return
        if deep_unacked:
            st.emergency_ack = now.date()
        if low_unacked:
            st.low_capacity_ack = now.date()
        # best-effort timer clear BEFORE the ON — deliberate difference to the
        # two switch_off releases (collision-first ABORT there): a release is
        # release-critical and must not fight an armed timer; the emergency
        # must NEVER be blocked by a failing clear — switch_on runs either way,
        # only the clear flag rides the audit.
        timer_clear = deps.act("disable_on_timers")
        ok = deps.act("switch_on", require_verify=True)   # read-back verify
        if ok:
            st.last_observed_switch = "on"   # override recognition: our own act = the new baseline
        if deep:                       # dual push titles: deep floor vs capacity floor
            deps.push("EMERGENCY", msg(cfg.msg_lang, "emergency_on",
                                       min_v=reading.min_cell_v))
            event = "emergency_on"
        else:
            _v_c = corrected_min_cell_v(reading.min_cell_v, reading.charge_a, cfg)
            deps.push("LOW BATTERY", msg(cfg.msg_lang, "low_capacity_on",
                                       v=_v_c if _v_c is not None else "–"))
            event = "low_capacity_on"
        deps.audit(event, action={"type": "switch_on"}, result="ok" if ok else "failed",
                   inputs={"min_cell_v": reading.min_cell_v,
                           "min_cell_corr": corrected_min_cell_v(
                               reading.min_cell_v, reading.charge_a, cfg),
                           "floor_v": cfg.true_floor_v,
                           "remaining_ah": reading.remaining_ah,   # display-only
                           "charge_a": reading.charge_a,
                           "timer_clear": timer_clear})   # best-effort flag
        return

    # a reach-gate silently blocking the awake rules can hide a WHOLE morning —
    # announce the transition, guarded so a stuck outage audits exactly once.
    reach_ok = deps.reachable()
    # announce EVERY transition — an uplink outage while the grid stays OFF must
    # be visible too (a switch=="on"-gated latch announced nothing). The guard
    # still makes it once per outage, not per tick.
    if reading is not None and not reach_ok:
        if not st.reach_blocked:
            st.reach_blocked = True
            deps.audit("reach_blocked", inputs={"switch": switch_state})
    elif st.reach_blocked and reach_ok:
        st.reach_blocked = False
        deps.audit("reach_restored")

    # --- MORNING-CLEARING of an OBSOLETE post-sunrise grid-ON program. The
    # ON-timer is the WiFi-BLACKOUT INSURANCE ONLY; once actor contact exists
    # after sunrise a programmed grid-on is obsolete (a direct reaction
    # exists). STANDALONE step — REGARDLESS of switch_state, OUTSIDE the
    # grid-off gate (the grid-off release only runs on switch==on; tomorrow the
    # grid = OFF → the armed post-sunrise timer would fire blind without this).
    # PHASE SCOPE: only MORNING/DAY_HOLD — inside EVE_PLAN/FINAL the step would
    # fight the eve's own fresh arming every poll (the eve self-heals stale
    # timers via write-evict + the carry-flip disarm instead). CLEAR SCOPE:
    # ONLY when EVERY armed ON-timer's fire time-of-day is >= the onset
    # time-of-day (fire and its onset share the occurrence date, so the date
    # cancels — the honest rule across midnight: a 11:38 counts as post-sunrise
    # on any day, a pre-dawn 05:30 never does; unparseable device truth = not
    # comparable = never cleared); the PRE-DAWN timers = the blackout insurance
    # = untouched (contact returning late must not strip tonight's
    # blind-firing insurance — a future blackout is unknowable and the night's
    # probe-cadence lag is exactly what the insurance covers). Placement:
    # AFTER the emergency block (safety first), BEFORE the tier/grid-off
    # machinery — overlaps self-resolve (whoever clears first; the other then
    # sees no armed timer). No onset edge known → NO decision (no guess — the
    # consult side nets to silence).
    if phase in (Phase.MORNING, Phase.DAY_HOLD) and reach_ok:
        try:
            _sp = deps.solar_night_windows()
            _pf = deps.night_profile()
        except Exception:                       # noqa: BLE001 — consult side
            _sp, _pf = {}, None                 # = no edge → no decision
        _onset = solar_onset_on(now.date(), _sp, _pf)
        _armed = [t for t in timers if is_rescue_timer(t)]
        if _onset is not None and _armed:
            _onset_min = _onset.hour * 60 + _onset.minute
            _class = [_cron_minutes(t.get("atLocal")) for t in _armed]
            if all(m is not None and m >= _onset_min for m in _class):
                _ok = deps.act("disable_on_timers")   # read-back verified in _collision_clear
                deps.audit("sunrise_timer_clear",
                           result="ok" if _ok else "failed",
                           inputs={"reason": "sunrise: scheduled "
                                             "switch-on obsolete",
                                   "armed": [t.get("atLocal") for t in _armed],
                                   "onset": _onset.isoformat()})
                if _ok:
                    # armed/ideal fields → None: the next planned/announced
                    # reads tell the truth (page + push claim only what is armed)
                    st.last_planned_fire = None
                    st.plan_ideal = None
                    st.rescue_timer_set = False

    # --- DRAIN STOP AT THE RESERVE TIER ---------------------------------------
    # While the battery DISCHARGES (grid OFF), reaching the tier ends the drain —
    # grid back ON at the raised level. That is the INTENDED shallow cycle around
    # the tier (outage probability rises with bad weather → everything below the
    # tier stays UPS reserve), NOT an emergency: it runs the normal non-emergency
    # switch-on flow (collision clear + verified switch_on, journaled as act +
    # grid_on) and NEVER the emergency block above — no emergency_on, no emergency
    # push. Layering is additive: the deep/emergency floors stay the safety spine
    # untouched beneath this stop. Condition mirror of the release gate: draining
    # requires soc > tier → stop at soc <= tier; reading/soc UNKNOWN while a tier
    # is active → NO new act (existing behavior unchanged — do not invent unknown
    # paths; the emergency floors are the safety net). Placed BEFORE the grid-off
    # release block and evaluated on the AS-READ state, so one tick can never
    # stop and release at once.
    tier_stopped = False
    # the stop must guard the DISCHARGE DIRECTION — a state-only condition
    # fired while the pack was CHARGING from solar (Ah climbed while the
    # reason text claimed 'tier reached'). Now: only a genuine discharge
    # (charge_a below −solar_noise, ingest sign positive = charging)
    # below/at the tier stops the drain; a charging or flat pack is left alone
    # — the release gate keeps its hold and the deep floors stay the safety net.
    # NO reach gate here — RealDeps.act is CLOUD-first (set_switch over eth0)
    # with LAN fallback and self-honest failure audits (NO CHANNEL /
    # verify_failed); a wlan operstate gate only blocked viable cloud acts (a
    # down dongle with SOC <= tier produced zero grid_on events all evening). A
    # dead path still fails VISIBLY through act()'s own audits; the drain-stop
    # intent is unconditional on contact.
    # the drain-stop enforces the tier OF TODAY (the forecast day itself) — a
    # latched window decision for another day (the day after the bad weather)
    # must never stop today's discharge.
    tier_now = _active_tier_pct(cfg, st, deps, now.date())
    if (reading is not None and switch_state == "off"
            and tier_now > 0 and reading.soc is not None
            and reading.soc <= tier_now
            and reading.charge_a is not None
            and reading.charge_a < -cfg.solar_noise_a):
        ok = False
        if deps.act("disable_on_timers"):      # collision-first (grid-off order)
            ok = deps.act("switch_on", require_verify=True,
                          reason=f"below reserve tier (BMS SOC {reading.soc:g}% "
                                 f"<= {tier_now}%) — stopping the "
                                 f"discharge, not an emergency")
            if ok:
                switch_state = "on"            # mirror: _eve sees the fresh state
                st.last_observed_switch = "on"  # our own act = the new observation baseline
                tier_stopped = True            # … and the grid-off release stays out THIS tick
        deps.audit("grid_on", action={"type": "switch_on"},
                   result="ok" if ok else "failed",
                   inputs={"soc": reading.soc, "tier_pct": tier_now,
                           "ground": "tier"})

    # --- grid-off-gate (collision-first; the ONLY grid-off path, all awake phases) ---
    if (not tier_stopped) and reading is not None and reach_ok and switch_state == "on":
        window = deps.bms_solar_window()
        # DAY DOCTRINE: DAY has NO prediction and NO static gate — stored
        # solar is simply drained into the load (room for new harvest).
        # the ONLY FLOOR hold = the VOLTAGE floor (corrected min cell >
        # TRUE_FLOOR_V — a physics invariant), unknown data (never guess), and
        # the deep 2.90 V backstop. The BMS counter does not stop the drain
        # (it hid ~15-20 Ah below the counter's 5 Ah "floor").
        # BESIDE that floor the RESERVE-TIER release gate adds a soft BMS-SOC
        # hold (`and _tier_release_ok`, below) — while a tier is active the
        # release needs soc at/above it; the floors underneath are unchanged
        # and remain the safety spine.
        # the DRAIN release needs the same reality anchor as the eve — the
        # projected grid-off span (head ÷ reference rate) must reach
        # release_min_span_min (30) min. Near the floor the LiFePO4 REBOUND
        # misleads the plain voltage check (ON = no load = the cells rebound
        # above the floor) and this gate oscillated with the eve's crossing
        # timers in ~1-minute ON cycles. Unknown rate/floor → the doctrine
        # holds (the drain above the floor/tier stays unconditional — no
        # reference to anchor against); the ANCHOR applies when the reference
        # exists.
        _span_ok = True
        try:
            _rate = (deps.reference_load() or {}).get("wh_h")
            _floor = night_floor_wh(cfg, reading.full_ah, reading.pack_v, None)
            _avail = (reading.remaining_ah or 0.0) * (reading.pack_v or 0.0)
            if isinstance(_rate, (int, float)) and not isinstance(_rate, bool) \
                    and _rate > 0 and _floor is not None \
                    and reading.remaining_ah is not None:
                _span_ok = ((_avail - _floor) / _rate) * 60.0 \
                    >= cfg.release_min_span_min
        except Exception:
            _span_ok = True
        if not voltage_floor_hit(reading.min_cell_v, reading.charge_a, cfg) \
                and _tier_release_ok(cfg, st, deps, reading, tier_now) and _span_ok:
            # collision-first order: abort the grid-off sequence on False
            if deps.act("disable_on_timers"):
                ok = deps.act("switch_off", require_verify=True)   # read-back verified
                solar = window[-1] if window and window[-1] is not None \
                    else (reading.charge_a or 0.0)
                deps.audit("grid_off", action={"type": "switch_off"},
                           result="ok" if ok else "failed",
                           inputs={"remaining_ah": reading.remaining_ah,
                                   # the floor is VOLTAGE (not the Ah counter)
                                   "floor_v": cfg.true_floor_v,
                                   "min_cell_corr": corrected_min_cell_v(
                                       reading.min_cell_v, reading.charge_a, cfg),
                                   "solar_a": solar, "soc": reading.soc,
                                   # the drain-doctrine clause that fired
                                   "ground": "drain"})
                if ok:
                    st.morning_done = True
                    # the grid-off write was verified — _eve must evaluate
                    # the SAME tick against this fresh "off" (not the as-read
                    # "on"); failed switch_off keeps the state as read.
                    switch_state = "off"
                    st.last_observed_switch = "off"   # override recognition: our own act = baseline
                    deps.push("MAIN OFF", msg(cfg.msg_lang, "grid_off",
                                              remaining=reading.remaining_ah
                                              if reading.remaining_ah is not None else 0.0,
                                              solar=solar,
                                              soc=reading.soc if reading.soc is not None else "–"))
        # floor / unknown / tier-hold: nothing (idempotent re-eval next tick)

    if phase in (Phase.EVE_PLAN, Phase.FINAL):
        _eve(cfg, st, deps, now, reading, switch_state, timers)
    # DAY_HOLD/MORNING: emergency + grid-off above are their whole duty (+ loop-cadence health)


def _night_resolve(cfg: Config, st: AppState, deps, now: datetime) -> None:
    """Overnight re-estimation of the armed grid-ON timer.

    TRUE OPERATING WINDOW: this step runs in the NIGHT phase only
    (never EVE/FINAL — those keep their own logic), and on a NORMAL night
    that phase reaches it in exactly two places:
    * the PRE-BLACKOUT TAIL ≈22:00–23:00 local — after the eve's closing
      write and before the WiFi blackout (reachable() = False ≈
      23:00–08:00, reach.py = wlan operstate); once night_over latches at
      the reachable 22:00 probe, every post-midnight tick is Phase.MORNING
      (verdict.py), so 00:00–09:00 is structurally NOT this step's window on
      a normal night — correctly so (blackout preservation below);
    * BLACKOUT RECOVERY — whenever a night RESUMES contact while the tick is
      in NIGHT phase (e.g. night_over never latched because the link died
      before the 22:00 probe, then contact returns pre-dawn).
    The ANTI-DRIFT WORKHORSE on a normal night is the EVE's own consult
    (~5-min cadence against the measured reference load); _night_resolve
    covers the pre-blackout tail + the recovery moments around it.

    While the link is UP, a measured reference load exists, the switch still
    DRAINS (grid OFF — a crossing only speaks while discharging) and EXACTLY
    ONE armed grid-ON timer exists (the same armed-state the eve reads):
    recompute t_fire = now + (avail − floor) ÷ (load × safety) from the fresh
    counter anchor (remaining_ah × V_avg-class, exactly how the eve builds
    it). A move BEYOND WRITE_DEADBAND_MIN rewrites the device timer through
    the same apply_plan/ensure_on path the eve uses (the write-confirmed
    armed stamp → last_planned_fire/plan_ideal follow the write
    truthfully); within the deadband → silent keep (apply_plan's own
    within-deadband note never fires here because the step itself gates
    BEFORE the write — the probe cadence must not dust the trail).

    BLACKOUT PRESERVATION: reach down → NOTHING happens — no read, no audit,
    no write: the armed timer IS the blackout insurance and is never touched.
    This step is the only thing running in the NIGHT branch before it returns;
    the universal emergency floor-watch belongs to the AWAKE phases —
    tick()'s IN-guard sits below the NIGHT branch, which returns before
    reaching it. No reference → no evaluation at all (the old model's armed
    time stands). A post-sunrise crossing is deliberately NOT refused here:
    the morning clearing owns obsolete post-sunrise programs."""
    if not deps.reachable():
        return                                   # blackout: hands off the timer
    try:
        ref = deps.reference_load()
    except Exception:                            # noqa: BLE001 — consult side
        return                                   # no method / consult crash → no step
    if not isinstance(ref, dict) or not ref.get("wh_h"):
        return                                   # no reference → no evaluation
    try:
        switch_state, timers = deps.read_state()
    except (ChannelError, RuntimeError):         # same gate shape as tick's read
        return
    if switch_state != "off":
        return                     # the crossing only speaks while DISCHARGING
    armed = [t for t in timers if is_rescue_timer(t)]
    if len(armed) != 1:
        return                     # no / ambiguous device truth → no decision
    old_sig = armed[0].get("atLocal")
    old_m = _cron_minutes(old_sig)
    if old_m is None:
        return                     # unparseable device truth → never comparable
    try:
        reading = deps.bms_latest()
        v_used = deps.pack_v_avg()
    except Exception:              # noqa: BLE001 — unknown data → never a guess
        return
    _v_anchor = v_used if v_used is not None else (
        reading.pack_v if reading is not None else None)
    avail = (reading.remaining_ah * _v_anchor
             if (reading is not None and reading.remaining_ah is not None
                 and _v_anchor is not None) else None)
    # this step RE-PROGRAMS the armed timer — a crossing after midnight
    # carries the FIRING day's tier, never the latched window max of the
    # programming day.
    # the floor comes from the CLUSTER day-view (_active_tier_day, never the
    # window latch). BOTH candidate days unknown (cluster down / no doc) → NO
    # write at all: the armed timer — the last cluster-verified program —
    # stands untouched (blackout preservation); a raw-head rewrite here could
    # silently disarm a band the eve programmed.
    floor_today = _active_tier_day(cfg, deps, now.date())
    floor_next = _active_tier_day(cfg, deps, (now + timedelta(days=1)).date())
    if floor_today is None or floor_next is None:
        return
    t_new = reference_crossing(
        now, avail, ref["wh_h"], cfg,
        full_ah=reading.full_ah if reading is not None else None,
        pack_v=reading.pack_v if reading is not None else None,
        reserve_floor_pct=floor_today,
        reserve_floor_pct_next=floor_next)
    if t_new is None:
        return                                   # unknown floor/avail → no guess
    new_sig = local_cron(t_new)
    move = crossing_move_min(old_sig, new_sig)
    if move is None or move <= WRITE_DEADBAND_MIN:
        return                                   # within the deadband → keep
    data = {"at_local": t_new, "timer": build_on_timer(t_new, cfg.tz),
            "fire_str": t_new.strftime("%H:%M")}
    ok = deps.apply_plan(TimerPlan(actions=[("failsafe_timer", data)]))
    if ok:
        st.plan_ideal = t_new                     # intent after the rewrite
        stamped = data.get("armed_local")         # … and the executor's device
        st.last_planned_fire = stamped if stamped is not None else t_new   # truth
    deps.audit("night_resolve", result="ok" if ok else "failed",
               inputs={"old_fire": f"{old_m // 60:02d}:{old_m % 60:02d}",
                       "new_fire": data["fire_str"],
                       "available_wh": round(avail, 1),
                       "load_wh_h": ref["wh_h"],
                       "deadband_min": WRITE_DEADBAND_MIN})


def _eve(cfg: Config, st: AppState, deps, now: datetime, reading,
         switch_state: str, timers: list) -> None:
    st.eve_ran = True      # today's eve consult rolled — gate for the
                           # closing final push (set here, at the consult's
                           # entry: any roll counts, even fully suppressed ones)
    samples = []
    try:
        samples = deps.bms_samples_2h()
    except Exception as e:   # same rule as tick's reading:
        deps.audit("eve_input_unavailable", result=str(e))   # everything stays conservative
    # learned night profile — it feeds only the NIGHT EDGES for the failsafe
    # horizon
    try:
        profile = deps.night_profile()
    except Exception as e:   # conservative + visible
        deps.audit("night_profile_unavailable", result=str(e))
        profile = None
    # the CLEAN-median night need. Fetch failure nets to buckets=[] →
    # need=None → conservative hold/bring-on (no guess without evidence).
    try:
        buckets = deps.night_buckets()
    except Exception as e:   # ANY failure class → buckets=[]
        deps.audit("night_buckets_unavailable", result=str(e))
        buckets = []
    need = night_need_wh(buckets)
    try:
        deps.yield_learning()          # forecast vs measured (learning only)
    except Exception:                  # noqa: BLE001 — a PURE side-channel
        # (never gates) may NEVER take the eve decision down with it — the
        # decision path must survive its instrumentation.
        pass
    clean = sum(1 for b in buckets if b.get("status") == "CLEAN")
    # --- mid-range capacity is AMP-HOUR based, NEVER the voltage snapshot. The
    # BMS counter IS the trusted mid-range instrument (its own internal
    # amp-integration; restart-proof: every run re-reads it); the LiFePO4
    # plateau makes the cell voltage utterly blind mid-range — a small load sag
    # swings the corrected-voltage SOC snapshot for HOURS of crossing chaos.
    # available = remaining_ah × V_avg — V_avg is the cheap Wh-conversion
    # average only; a failed average falls back to the LIVE pack_v (a sensor
    # value — sensor data beats textbook constants), both unknown → None → the
    # conservative ratio branch (never a guessed number). The corrected-voltage
    # SOC path is REMOVED from the eve's available; it keeps ONLY its designed
    # home: the deep/knee band below (floor_wh in the planner + the
    # emergency/low-capacity machinery in tick) — KNEE VOLTAGE IS THE
    # BELOW-ZERO GUARD for the deliberate below-counter discharge strategy (the
    # pack IS driven below the BMS-reported Ah to force its Ah learning; the
    # knee/deep band catches a counter that lies HIGH — the stable anchor bias
    # is accepted).
    try:
        _v_avg = deps.pack_v_avg()
    except Exception as e:   # consult side crash
        deps.audit("eve_input_unavailable", result=str(e))   # → live-pack_v fallback
        _v_avg = None
    _v_used = _v_avg if _v_avg is not None else (
        reading.pack_v if reading is not None else None)
    available_wh = (reading.remaining_ah * _v_used
                    if (reading is not None and reading.remaining_ah is not None
                        and _v_used is not None) else None)
    # honest disclosure line: the SAME counter against the INSTANT pack voltage
    # (the avg-vs-instant conversion delta — second-order, but never hidden)
    available_bms_wh = (reading.remaining_ah * reading.pack_v
                        if (reading is not None and reading.remaining_ah is not None
                            and reading.pack_v is not None) else None)
    # voltage stays audited as the DEEP-BAND disclosure (min_cell/corrected/
    # SoC-curve) — it feeds NO mid-range decision anymore
    _v_corr = (corrected_min_cell_v(reading.min_cell_v, reading.charge_a, cfg)
               if reading is not None else None)
    # the BMS is the ONLY sensor and the ONLY truth — no textbook LiFePO4
    # voltage→SoC table; the live BMS SOC is the deep-band disclosure value.
    _soc_p = reading.soc if reading is not None else None
    # the crossing horizon = the MEASURED solar-night length (last completed
    # solar night), else the learned hour edges, else the documented default.
    night_len = DEFAULT_NIGHT_LEN_H
    _measured = False
    horizon_len = None         # the LENGTH clamp may use a measured span of
                               # ANY length (the 6–16 h gate keeps only its
                               # DIVISOR role below; the fallback median
                               # crossing still divides by night_len)
    try:
        spans = deps.solar_night_windows()
    except Exception:        # any failure: spans={} → edges/default
        spans = {}
    if spans:
        _s, _e = spans[max(spans)]          # latest morning-labeled span
        hours = (_e - _s).total_seconds() / 3600.0
        if hours > 0:
            horizon_len = hours             # ANY measured length = the horizon
        if 6.0 <= hours <= 16.0:               # gate stays for the DIVISOR role:
            night_len, _measured = hours, True   # a sunless-chain 40 h never divides
    # learned night length from the profile edges (onset − offset, midnight-wrap);
    # degenerate/unknown edges → documented 11.0 h default. Fallback only: the
    # measured span above wins whenever it yielded a sane value.
    if not _measured and profile is not None and profile.array_onset_h is not None \
            and profile.array_offset_h is not None:
        edges = (profile.array_onset_h - profile.array_offset_h) % 24
        night_len = edges if edges > 0 else DEFAULT_NIGHT_LEN_H
    # --- the REFERENCE-LOAD model: the measured house load of the newest
    # qualifying calendar night (grid-OFF clip mean, Wh/h). ANY failure nets
    # to None → the median method keeps the crossing (the RealDeps side audits
    # every real evaluation as the event class reference_load — source clip |
    # median_fallback — so the fallback marker exists WITHOUT this call
    # needing to write it; old test fakes without the method net silently).
    try:
        _ref = deps.reference_load()
    except Exception:                  # noqa: BLE001 — missing method / consult crash
        _ref = None
    load_wh_h = _ref.get("wh_h") if isinstance(_ref, dict) else None
    if load_wh_h is not None and load_wh_h <= 0:
        load_wh_h = None               # a non-positive load proves nothing
    # --- the horizon SOURCE — measured (fresh only) | predicted | profile |
    # none. The prediction consult reads the outlook forecast_ok refreshed at
    # the PREVIOUS consult (the reserve tier's own weather transport — no
    # second path, no extra HTTP here) and is consulted every eve tick
    # (RealDeps evaluates once per day+target). Target = TOMORROW: the eve
    # only runs in the evening window, so the next morning is tomorrow.
    # RealDeps audits each evaluation as solar_day_pred (labelled, never
    # state); old fakes without the method net to None → labelled 'none'
    # below.
    try:
        _pred = deps.solar_day_prediction(now.date() + timedelta(days=1))
    except Exception:                  # noqa: BLE001 — missing method / consult crash
        _pred = None
    next_onset, onset_src = next_solar_onset_src(now, spans, profile, _pred)
    # honest disclosure of the rate actually used (display truth): the
    # reference × safety, else the legacy need ÷ night_len × safety
    _basis = (load_wh_h if load_wh_h is not None
              else (need / max(1.0, night_len) if need is not None else None))
    deps.audit("night_need", inputs={"need_wh": need, "clean": clean,
                                     # available = counter × V_avg (both
                                     # disclosed)
                                     "available_wh": available_wh,
                                     "available_bms_wh": available_bms_wh,
                                     "remaining_ah": reading.remaining_ah
                                     if reading is not None else None,
                                     "v_avg": _v_used,
                                     # voltage = deep-band disclosure only
                                     "min_cell_v": reading.min_cell_v
                                     if reading is not None else None,
                                     "min_cell_corr": _v_corr,
                                     "soc_pct_v": _soc_p,
                                     # the crossing basis (None = the median
                                     # fallback path kept its own rate)
                                     "load_wh_h": load_wh_h,
                                     "crossing_rate_wh_h": (
                                         _basis * cfg.safety_factor
                                         if _basis is not None else None),
                                     # the horizon's SOURCE (the onset itself
                                     # never renders as state — solar_day_pred
                                     # carries the prediction)
                                     "onset_src": onset_src,
                                     "safety": cfg.safety_factor})
    # the sun gate: the eve release needs a good forecast too (conservative
    # on any missing data) — one fetch+ship per consult, verdict into the
    # ratio.
    try:
        sun_ok, sun_reason = deps.forecast_ok()
    except Exception as e:   # a forecast crash closes the gate conservatively
        # (missing data = hold), never the whole consult.
        sun_ok, sun_reason = False, f"forecast crash: {type(e).__name__}: {e}"
        deps.audit("forecast_unavailable", result=str(e))
    # THE eve consult = the sole prediction point — compute the
    # weather-conditional reserve tier from the rolling outlook (freshly
    # fetched by forecast_ok above) and latch it for the coming day (NOT
    # cleared at rollover; overwritten next eve / revision).
    _update_reserve_tier(cfg, st, deps)
    # the ONE evening ratio decision (pure) → dispatched actions. PUSHES stay
    # transition-guarded and tell the APPLICATION truth.
    # the ABSOLUTE next solar edge — computed above WITH its source label
    # (measured | predicted | profile | none, disclosed in
    # night_need.onset_src); None → the length clamp alone (no guess).
    # the plan's floor = the tier OF THE DAY ITS TIMER FIRES ON (today's tier
    # for a pre-midnight crossing, the next day's for one after —
    # planner.action_day_floor_pct probes), while the release gates below
    # speak for TODAY (the eve's own day).
    tier_today = _active_tier_pct(cfg, st, deps, now.date())
    tier_tomorrow = _active_tier_pct(cfg, st, deps,
                                     (now + timedelta(days=1)).date())
    # the PROGRAMMED floor comes from the CLUSTER day-view (never the latch;
    # unknown → None = plan_evening's raw-head behavior) — the release gates
    # below keep tier_today (the latched enforcement view, unchanged).
    plan_floor_today = _active_tier_day(cfg, deps, now.date())
    plan_floor_tomorrow = _active_tier_day(
        cfg, deps, (now + timedelta(days=1)).date())
    plan = plan_evening(now, available_wh, need, switch_state, night_len, cfg,
                        forecast_ok=sun_ok,
                        pack_v=reading.pack_v if reading is not None else None,
                        full_ah=reading.full_ah if reading is not None else None,
                        # honor the reserve band in the night target
                        # (CLUSTER day-view; None = raw-head behavior)
                        reserve_floor_pct=plan_floor_today,
                        reserve_floor_pct_next=plan_floor_tomorrow,
                        next_onset=next_onset,
                        # the reference load replaces the median basis in the
                        # crossing (None = old rate, audited)
                        load_wh_h=load_wh_h,
                        # the length clamp may run on ANY measured span
                        # length (None → night_len exactly as before)
                        horizon_hours=horizon_len)
    # the plan may carry MULTIPLE actions (spend + schedule) — the loop
    # dispatches each; every act narrates itself (transition-guarded push).
    for kind, data in (list(plan.actions) or [("none", {"reason": "empty plan"})]):
        if kind == "release_night":
            # the night spend is a discharge release too — gated by the SAME
            # reserve-tier rule: below the tier → no act, no verdict, no push
            # (the grid stays on; the floors below are untouched). The guarded
            # tier_hold line usually already fired in the grid-off block this
            # very tick → still exactly ONE line.
            if not _tier_release_ok(cfg, st, deps, reading, tier_today):
                verdict, ok = None, True        # hold: the transition guard stays quiet
                continue
            verdict = EveVerdict.NIGHT_RELEASE
            # collision-first order: abort the grid-off sequence on False
            ok = False
            if deps.act("disable_on_timers"):
                ok = deps.act("switch_off", require_verify=True,
                              reason="Battery-first: headroom above the floor reserve is used tonight")
                deps.audit("grid_off", action={"type": "switch_off"},
                           result="ok" if ok else "failed",
                           inputs={"available_wh": available_wh, "need_wh": need,
                                   "safety": cfg.safety_factor,
                                   # the release = spend the stored solar
                                   "radiation_sum_j": deps.last_radiation_j,
                                   "weather_min_j": cfg.weather_min_j,
                                   "forecast_ok": sun_ok})
                if ok:
                    st.morning_done = True
                    st.rescue_timer_set = False  # ON timers were just disabled
                    st.last_planned_fire = None  # the timer action (if any) re-sets it
                    st.plan_ideal = None         # same "re-set below" scope
                    st.last_observed_switch = "off"   # override recognition baseline
        elif kind == "bring_on_now":
            # Anti-oscillation guard: never fight the grid-off release.
            # Suppressed when the grid-off partner rule would immediately
            # revert this ON — the DRAIN rule fires this very tick (battery
            # above the voltage floor (corrected > TRUE_FLOOR_V), or the
            # grid-off released today (morning_done since rollover).
            # Suppressed = audit only: no act, no verdict change, NO push
            # (zero alternation); the night_need audit above still carries
            # full fidelity.
            grid_off_fight = (reading is not None and not voltage_floor_hit(
                reading.min_cell_v, reading.charge_a, cfg)) or st.morning_done
            # the fight must be REAL — the drain rule can only revert this ON
            # THIS tick when its release path is open: actor contact AND the
            # tier release gate (soc >= tier + hyst). Below a held tier — or
            # without contact — nothing would revert; vetoing here turned the
            # tier's own drain-stop intent into NO ACTION.
            if grid_off_fight and not (
                    deps.reachable()
                    and _tier_release_ok(cfg, st, deps, reading, tier_today)):
                grid_off_fight = False
            if grid_off_fight:
                deps.audit("eve_bring_on_suppressed", inputs={
                    "reason": GRID_OFF_SUPPRESS_REASON,
                    "soc": reading.soc if reading is not None else None,
                    # the rule in force (the drain rule would revert this ON)
                    "remaining_ah": reading.remaining_ah if reading is not None else None,
                    "floor_v": cfg.true_floor_v,
                    "morning_done": st.morning_done})
                verdict, ok = None, True          # None → no transition → no push;
            else:                                 # the FINAL block below still runs
                verdict = EveVerdict.GRID_HOLD
                # best-effort timer clear BEFORE the ON — same deliberate
                # difference to the collision-first switch_off paths: this
                # rescue-ON ("stays on") must never be blocked by a failing
                # clear; switch_on runs either way. No extra audit here:
                # RealDeps.act already journals the clear result
                # (act/act_failed, result ok/failed).
                deps.act("disable_on_timers")
                ok = deps.act("switch_on", require_verify=True,
                              reason=hold_reason(available_wh, need, cfg,
                                                 sun_ok, sun_reason))
                if ok:
                    st.last_observed_switch = "on"   # override recognition baseline
                elif not st.rescue_timer_set and not any(
                        is_rescue_timer(t) for t in timers):
                    # an eve ON decision that did not deliver must NEVER
                    # leave the night bare — arm the DEVICE-side timer
                    # (blackout insurance: fires from the device clock
                    # without WiFi) + the intent/armed truth fields; the
                    # failed direct attempt still signals itself (act()'s
                    # NO CHANNEL / verify audit + the push below).
                    _at = now + timedelta(minutes=2)
                    _data = {"at_local": _at,
                             "timer": build_on_timer(_at, cfg.tz),
                             "fire_str": _at.strftime("%H:%M")}
                    _ins = deps.apply_plan(
                        TimerPlan(actions=[("failsafe_timer", _data)]))
                    deps.audit("eve_bring_on_timer_fallback",
                               result="ok" if _ins else "failed",
                               inputs={"fire_str": _data["fire_str"],
                                       "reason": "direct switch_on did not deliver"})
                    if _ins:
                        st.rescue_timer_set = True
                        st.plan_ideal = _data["at_local"]        # intent
                        st.last_planned_fire = (_data.get("armed_local")
                                                or _data["at_local"])  # device stamp wins
        elif kind == "failsafe_timer":
            verdict = EveVerdict.RESCUE_TIMER
            # an armed program on the device IS the night's prediction from
            # the load-profile snapshot — keep it, never rewrite it every tick
            # (a re-arm following the rebound-contaminated counter drifts and
            # can land in the past). The standing program is the device truth
            # and carries the stamps.
            standing = [t for t in timers
                        if (t.get("do") or {}).get("switch") == "on"
                        and t.get("enabled", 1) != 0]
            if standing:
                st.rescue_timer_set = True
                # stamps ride the PROJECTION (the card's ideal; the device
                # truth lives in the audit's standing field) — no cron parsing
                st.plan_ideal = data["at_local"]
                st.last_planned_fire = data["at_local"]
                ok = True
                deps.audit("eve_snapshot_kept",
                           inputs={"standing": standing[0].get("atLocal"),
                                   "projected": data["at_local"].isoformat(
                                       timespec="minutes")})
            else:
                ok = deps.apply_plan(plan)           # the existing ensure-on programming path
                st.rescue_timer_set = ok             # device truth from the executor's read-back
                # TWO truths ride along — the PLANNER's crossing (intent,
                # re-projected every tick = the ideal) and the
                # WRITE-CONFIRMED armed (device truth, stamped by the executor
                # as data["armed_local"]: verified write / identical program /
                # within-deadband keep — the deadband path returns True
                # WITHOUT writing, so `ok` alone would label the ideal as
                # armed under the write-deadband semantics).
                st.plan_ideal = data["at_local"]
                armed = data.get("armed_local")
                if armed is not None:
                    st.last_planned_fire = armed      # device-grounded armed only
                elif ok and "armed_local" not in data:
                    # executors without the stamp contract (test fakes,
                    # DRY_RUN's suppressed write): ok meant "applied" there →
                    # keep the handed-off-crossing semantics.
                    st.last_planned_fire = data["at_local"]
                # not ok / stamped-unknown → the last CONFIRMED armed survives —
                # never overwrite device truth with an intention.
        elif kind == "keep_grid":
            verdict = EveVerdict.GRID_HOLD
            ok = deps.apply_plan(plan)           # no write by design; audited with reason
        else:                                     # "none": the battery carries the night
            verdict, ok = EveVerdict.SAFE_CLEAR, True
            if st.rescue_timer_set or any(is_rescue_timer(t) for t in timers):
                # a verdict FLIP rescue_timer → carries must DISARM the
                # previously armed return — else the stale timer still fires
                # at the old crossing and cuts the night short. Device truth
                # from the fresh read covers a restart that lost the
                # in-memory flag. Best-effort: the clear journals itself; on
                # failure the flag stays True and the next eve tick retries.
                if deps.act("disable_on_timers"):
                    st.rescue_timer_set = False
                    st.last_planned_fire = None
                    st.plan_ideal = None     # carries = no timer wanted

        if verdict is not None and st.evening_verdict != verdict.value:   # transition guard drives PUSHES only
            st.evening_verdict = verdict.value
            if verdict is EveVerdict.SAFE_CLEAR:
                st.rescue_fallback = None
            if not ok:                               # honest failure signal, action named
                deps.push("NO CHANNEL", msg(cfg.msg_lang, "no_channel", action=kind))
            elif verdict is EveVerdict.NIGHT_RELEASE:   # the spend act (numbers ride along)
                deps.push("NIGHT PLAN", msg(cfg.msg_lang, "night_release",
                                           available=available_wh, need=need,
                                           factor=cfg.safety_factor))
            elif verdict is EveVerdict.SAFE_CLEAR:
                deps.push("NIGHT PLAN", msg(cfg.msg_lang, "safe_clear",
                                           min_v=(reading.min_cell_v if reading else None) or 0.0,
                                           slope=_slope(samples)))
            elif verdict is EveVerdict.RESCUE_TIMER:  # rescue_set text kept, time = fire
                deps.push("NIGHT PLAN", msg(cfg.msg_lang, "rescue_set",
                                           time=data["fire_str"]))
            else:                                    # GRID_HOLD: reason from payload/hold_reason
                deps.push("GRID ON", msg(cfg.msg_lang, "grid_hold",
                                         reason=data.get("reason")
                                         or hold_reason(available_wh, need, cfg,
                                                        sun_ok, sun_reason)))

    # the FINAL push does not fire here: it fires at the eve's CLOSING in
    # tick()'s phase-NIGHT branch — after the last possible write. The
    # armed/ideal composition lives in the night_plan[_armed[_ideal]]
    # templates.


def _slope(samples) -> float:
    if len(samples) < 2:
        return 0.0
    (t0, v0), (t1, v1) = samples[0], samples[-1]
    h = (t1 - t0).total_seconds() / 3600.0
    return (v1 - v0) / h if h > 0 else 0.0


# --- weather-conditional RESERVE TIERS ------------------------------------------
def restore_reserve_tier(st: AppState, deps) -> None:
    """Restart-window safety: AppState is in-memory, so a restart resets
    st.reserve_tier_pct to 0 while the durable decision still stands — the
    FIRST tick after a restart would otherwise evaluate the release gate with
    tier=0 (open) and grant a day-drain release the standing tier forbids.
    The decision IS durable in the events index (event reserve_tier); this
    seed reloads it BEFORE the first decision tick.

    Consult side like every other: ANY Exception → keep 0, no crash (the
    restart must never fail on the seed). Only a non-negative INT saturates
    the latch. No audit for a seed — no NEW decision is made, only the
    durable one is reloaded. ACCEPTED: st.reserve_tier_sig stays None, so
    the next legitimate recomputation CAN write ONE duplicate reserve_tier
    event with the same decision — one honest line per restart, never
    spam."""
    try:
        inputs = deps.latest_reserve_tier()
    except Exception:                        # noqa: BLE001 — keep 0, no crash
        return
    tier = inputs.get("tier_pct") if isinstance(inputs, dict) else None
    if isinstance(tier, int) and not isinstance(tier, bool) and tier >= 0:
        st.reserve_tier_pct = tier           # reload the durable decision (latch)


def restore_phase_flags(st: AppState, deps, now: datetime, cfg) -> None:
    """Restart seed for the day-scoped phase flags: a mid-day restart left
    morning_done=False although today's morning OFF had already happened →
    phase stuck at MORNING → no eve roll → the frontend never opened tonight's
    night row. Evidence rules derived FROM THE SETTERS: morning_done := a
    successful grid_off TODAY (both setters audit exactly that after ok).
    night_over := a night_over event in the last 24 h (NIGHT probe audit;
    night_over is NEVER cleared — the rollover resets morning_done only — so
    the 24 h scope covers every window the flag can matter in).
    Crash-netted like every consult side: a query failure leaves the
    defaults (False) and the next restart retries — a failed seed must never
    kill the boot."""
    try:
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        evidence = deps.phase_evidence(day_start.isoformat())
        st.morning_done = any(
            e.get("event") == "grid_off" and e.get("result") == "ok"
            for e in evidence)
        cutoff = (now - timedelta(hours=24)).isoformat()
        st.night_over = any(
            e.get("event") == "night_over" and str(e.get("ts", "")) >= cutoff
            for e in evidence)
    except Exception:                                   # noqa: BLE001 — boot safety
        return


def _update_reserve_tier(cfg: Config, st: AppState, deps) -> None:
    """Compute the weather-conditional RESERVE TIER from the rolling outlook
    and overwrite the latch (eve consult + every forecast/outlook revision).

    Call sites: `_eve` (the sole prediction point — the eve re-reads the
    outlook its own forecast_ok just fetched) and
    `RealDeps.forecast_refresh(st)` (the midday revision path — wherever the
    forecast/outlook is refreshed). Tier-1 reads the outlook's own radiation
    vs the weather card's solar reference (WEATHER_MIN_J) — trigger and card
    share ONE number. Pure consult side: any crash → conservative no-op (the
    latch keeps the last decision; the emergency floors carry safety). Always
    recomputes; the `reserve_tier` audit fires only when the DECISION (tier +
    triggers + window dates) actually changes — one WHY line per change,
    never per tick.
    """
    try:
        outlook, outlook_ts = deps.outlook_days()
    except Exception:   # noqa: BLE001 — no outlook source → no recompute (conservative)
        return
    try:
        need = night_need_wh(deps.night_buckets())
    except Exception:   # noqa: BLE001 — unknown need
        need = None
    try:
        yl = deps.yield_learning()          # cached per day (RealDeps) — side-channel
        factor = yl.get("factor") if isinstance(yl, dict) else None
    except Exception:   # noqa: BLE001 — unknown factor → tier-1 simply cannot fire
        factor = None
    tier, triggers = reserve_tier(outlook, cfg)   # tier-1 = solar-reference comparison
    sig = (tier, tuple(triggers),
           tuple(d.get("date") for d in outlook if isinstance(d, dict))
           if isinstance(outlook, list) else ())
    st.reserve_tier_pct = tier              # overwrite the latch (eve / revision path)
    if sig == st.reserve_tier_sig:
        return                              # unchanged decision → no repeated line
    st.reserve_tier_sig = sig
    # no value inventions in the front end: these are the BACKEND-configured
    # numbers the monitor renders — the display never keeps its own copy of
    # any of them (reference, hysteresis, thresholds).
    deps.audit("reserve_tier", inputs={"tier_pct": tier,
                                       "triggers": list(triggers),
                                       "lookahead_days": cfg.tier_lookahead_days,
                                       "outlook_ts": outlook_ts,
                                       "reference_kwh_m2": (
                                           round(cfg.weather_min_j / 3.6e6, 3)
                                           if cfg.weather_min_j is not None else None),
                                       "release_hyst_pct": cfg.tier_release_hyst_pct})


def _active_tier_pct(cfg: Config, st: AppState, deps, on_date) -> int:
    """Every ENFORCEMENT read of the reserve tier (drain-stop, release gates,
    plan floors) evaluates the tier OF THE DAY THE ACTOR ACTS ON — the
    bad-weather forecast day itself, never the day after (a programmed timer
    must consider the day the actor acts, not the day the programming
    happens). The rolling outlook is filtered per date
    (verdict.active_tier_on); outlook unknown or not covering `on_date` →
    honest fallback to the latched window decision (missing data must never
    silently disarm a hold). The latch itself (reserve_tier_pct / its audit)
    stays the window record — this is the per-day enforcement view."""
    try:
        outlook, _ts = deps.outlook_days()
    except Exception:                                   # noqa: BLE001 — no outlook source
        return st.reserve_tier_pct                      # → the conservative latch
    res = active_tier_on(outlook, cfg, on_date)
    return st.reserve_tier_pct if res is None else res[0]


def _active_tier_day(cfg: Config, deps, on_date) -> int | None:
    """The per-day tier FOR TIMER PROGRAMMING — straight from the cluster
    outlook copy (deps.outlook_days reads OpenSearch), NEVER the window
    latch: unknown (cluster down / date uncovered) stays honest None so the
    crossing computes no floor and the armed timer is left untouched. An
    invented floor may never reprogram a crossing (a latch fallback once
    armed a wrong-band crossing instead of the eve's programmed one)."""
    try:
        outlook, _ts = deps.outlook_days()
    except Exception:                                   # noqa: BLE001 — no source
        return None
    res = active_tier_on(outlook, cfg, on_date)
    return None if res is None else res[0]


def _tier_release_ok(cfg: Config, st: AppState, deps, reading, tier: int) -> bool:
    """The RESERVE-TIER RELEASE GATE — while a tier is active, a release
    that would let the battery DISCHARGE may only run at/above tier +
    tier_release_hyst_pct (BMS SOC instrument — display-only stays binding
    for the DEEP/emergency decisions, BMS SOC is the correct instrument for
    the mid-band tier gates). Below the tier → the grid STAYS ON: a soft
    hold, NO act; the deep floors, the emergency act and voltage_floor_hit
    beneath are untouched (additive layer) and the GRID-ON emergency switch
    is never gated by the tier.

    `tier` is the caller's per-day evaluation (_active_tier_pct for the day
    this gate acts on) — the gate never reads the window latch itself.

    UNKNOWN reading/soc while a tier is active → the gate cannot show
    positive evidence → the release stays blocked (no discharge act without
    data); the mirrored drain-stop rule keeps the EXISTING drain on unknown
    SOC (no new act there either). Guarded audit: ONE tier_hold line per
    engage or tier change, never one per tick; the release act narrates the
    hold's end."""
    soc = reading.soc if reading is not None else None
    if tier <= 0:
        st.tier_hold_pct = 0                 # no tier → nothing left to hold
        return True
    # hysteresis (controller, disclosed): release zone starts at tier + hyst so
    # it can NEVER coincide with the <= tier stop zone at an exact plateau
    if soc is not None and soc >= tier + cfg.tier_release_hyst_pct:
        st.tier_hold_pct = 0                 # released at/above tier+hyst (silent)
        return True
    if st.tier_hold_pct != tier:             # engage OR tier change → ONE line
        st.tier_hold_pct = tier
        deps.audit("tier_hold", inputs={"soc": soc, "tier_pct": tier,
                                     "release_at_pct": tier + cfg.tier_release_hyst_pct})
    return False


class RealDeps:
    """Production wiring. DRY_RUN=True: every act() journals+pushes, zero writes."""

    def __init__(self, cfg: Config, timer_api_supported: bool = True):
        self.cfg = cfg
        self.cloud = CloudClient(cfg.ew_email, cfg.ew_pass, cfg.ew_cc,
                                 timer_api_supported=timer_api_supported)
        self.lan = LanClient(cfg.sw_ip, cfg.sw_deviceid, cfg.sw_devicekey)
        self.bms = BmsSource(cfg.os_url, cfg.bms_index, cfg.bms_cell_fields,
                             cfg.bms_current_path)
        # forecast docs live in the weather index — mirror the cfg prefix
        # (default composes to the frontend's GG_WEATHER "gridguard-weather-*")
        self.bms.weather_index = f"{cfg.weather_index_prefix}-*"
        # per-class yield: the events index (the addon's own labels) for the
        # grid-ON share — same cfg-mirror doctrine
        self.bms.events_index = f"{cfg.events_index}-*"
        self.weather = WeatherClient(cfg.weather_lat, cfg.weather_lon) \
            if cfg.weather_lat is not None else None
        from .audit import AuditLog
        from datetime import timezone
        self.audit_log = AuditLog(cfg.log_dir / "gridguard.jsonl",
                                  os_url=cfg.os_url, tz=cfg.tz, dry_run=cfg.dry_run,
                                  events_index=cfg.events_index,
                                  events_index_enabled=cfg.events_index_enabled)
        self._tz = timezone.utc
        self._profile_cache: tuple[date, NightProfile] | None = None   # learn once/day
        self._buckets_cache: tuple[date, list[dict]] | None = None     # buckets once/day
        self._solar_cache: tuple[date, dict, dict] | None = None   # day_bounds + night spans, once/day
        self._yield_cache: tuple[date, dict | None] | None = None  # learn once/day (also caches honest None)
        self._weather_refresh_t: float | None = None  # forecast revision throttle
        self._last_ok_act: tuple | None = None  # identical ok-confirmation only once
        # one "within deadband" audit per (kind, armed signature) — the trail
        # notes the skip once, never per tick
        self._deadband_sig: tuple | None = None
        self.last_radiation_j: float | None = None   # audit mirror of the last fetch
        # ══ CACHE DOCTRINE ════════════════════════════════════════════════════
        # A cache may exist ONLY where it makes sense:
        #  (i) throttling an EXTERNAL, slow, rate-limited API on the FETCH side
        #      (_weather_refresh_t — the open-meteo call),
        #  (ii) write/audit dedup (_last_ok_act, _deadband_sig, the *sig guards)
        #      — they guard ACTIONS and compare against FRESH device reads,
        #  (iii) daily-LEARNED derived state whose inputs cannot change within
        #      the local day (_profile/_buckets/_solar/_yield/_ref — each
        #      date-keyed with its "only changes at sunrise" reason stated).
        # READS of a truth source (OpenSearch, BMS index, the device) are
        # ALWAYS DIRECT — the cluster answers in <3 ms, so no read-side cache
        # may ever stand between a decision and its truth source. And NEVER a
        # silent invented fallback value: unknown → honest None → conservative
        # no-op / hands-off. (An in-memory weather hand-off once served a
        # restart-empty value and reprogrammed a crossing from a stale latch —
        # hence the cluster copy is the only read source; outlook_days() reads
        # it.)
        # THE FULL MODEL: the CLUSTER is the truth layer and ALL consumers
        # read against it. WRITE side — run() calls forecast_refresh(st) after
        # EVERY tick in ALL phases (self-throttled FORECAST_REFRESH_MIN = 30
        # min default; the host's internet is independent of the actor-WiFi
        # blackout) → ship_forecast + ship_outlook; the outlook doc id
        # md5(local-day|outlook) is STABLE per day, so each cycle OVERWRITES
        # the forecast day's data with the fresh fetch (rolling latest). READ
        # side — every consumer reads load_outlook (the newest doc by ts); no
        # consumer may keep its own copy of weather truth.
        # the reference only changes when a calendar night completes → ONE
        # evaluation (and ONE reference_load audit) per local day, like
        # night_buckets/profile
        self._ref_cache: tuple[date, dict | None] | None = None
        # the solar-day prediction only changes when the outlook, the
        # measured edges or the yield facts do → ONE evaluation (and ONE
        # solar_day_pred audit) per (local day, target)
        self._solar_pred_cache: tuple | None = None

    # --- deps API ---
    def bms_latest(self): return self.bms.latest()
    def bms_samples_2h(self):
        # samples only feed the safe_clear slope
        return self.bms.samples_minutes(120)

    def night_profile(self) -> NightProfile:
        """Learned night-load/array-run profile — recomputed at most once per
        local day over the rolling HIST_DAYS window; it feeds the NIGHT EDGES
        for the failsafe horizon. Fetch errors propagate → _eve nets them
        (conservative hold)."""
        today = datetime.now(self.cfg.tz).date()
        if self._profile_cache is not None and self._profile_cache[0] == today:
            return self._profile_cache[1]
        samples = self.bms.samples_days(self.cfg.hist_days)
        profile = learn_profile(samples, self.cfg.tz, self.cfg.solar_noise_a,
                                self.cfg.hist_days)
        self._profile_cache = (today, profile)
        self.audit("night_learned", inputs={
            "array_onset_h": profile.array_onset_h,
            "array_offset_h": profile.array_offset_h,
            "samples_used": profile.samples_used,
            "sufficient": profile.sufficient,
            "hist_days": self.cfg.hist_days})
        return profile

    def solar_windows(self) -> tuple[dict, dict]:
        """(day_bounds, night_spans) — the ONE solar-day rule queried in the
        cluster (periods.py holds the executable rule), cached per local day
        like the profile/buckets. Sunless dates simply get no pair; failures →
        empty maps, NOT cached (next call retries the cluster — conservative:
        consumers fall back to documented defaults, never guess a boundary)."""
        today = datetime.now(self.cfg.tz).date()
        if self._solar_cache is not None and self._solar_cache[0] == today:
            return self._solar_cache[1], self._solar_cache[2]

        def _sun(d):
            try:
                return self.weather.sun_times(d) if self.weather is not None else None
            except Exception:
                return None
        # today's calendar sunset rides the boundary round (no extra HTTP) —
        # a solar day's length is final only AFTER it. CONTRACT: sun_times
        # returns NAIVE local datetimes (fromisoformat of offset-less
        # open-meteo strings) while bounds/now are tz-aware — normalize here
        # (naive = local wall time) before anything compares it.
        _t_sun = _sun(today)
        today_sunset = None
        if _t_sun and _t_sun[1] is not None:
            _s_raw = _t_sun[1]
            today_sunset = (_s_raw.replace(tzinfo=self.cfg.tz)
                            if _s_raw.tzinfo is None else _s_raw)
        dates = [today - timedelta(days=i)
                 for i in range(self.cfg.hist_days, -1, -1)]
        try:
            bd = self.bms.solar_day_boundaries(dates, str(self.cfg.tz),
                                               self.cfg.flow_a, _sun)
            sp = night_windows(bd)
        except (BmsError, RuntimeError):
            # failures are NOT cached (night_profile/night_buckets doctrine)
            # — return empty WITHOUT writing _solar_cache, so the next call
            # retries the cluster instead of pinning the clock-fallback for
            # the whole local day after one blip.
            return {}, {}
        self._solar_cache = (today, bd, sp, today_sunset)
        return bd, sp

    def solar_night_windows(self) -> dict:
        """deps API for _eve: morning-labeled solar-night windows."""
        return self.solar_windows()[1]

    def solar_today_sunset(self) -> datetime | None:
        """Calendar sunset of the local today as captured by the boundary
        round (a solar day's length is final only at/after it)."""
        self.solar_windows()               # ensures the daily cache
        return self._solar_cache[3] if self._solar_cache else None

    def forecast_refresh(self, st: AppState | None = None) -> None:
        """The forecast REVISION PATH must land in the cluster THROUGH the
        day — a midday-changed open-meteo value is a new data point for the
        forecast↔harvest mapping and must not wait for the ~21:00 eve ship.
        Throttled to cfg.forecast_refresh_min; ship_forecast's value-based
        md5 id makes an unchanged value an idempotent rewrite and a changed
        value a NEW doc — the day's revision chain accumulates by
        construction. Also refreshes the rolling storm outlook. Best-effort:
        never raises, never blocks, no gate touched.

        When the caller passes the AppState (run() does), the freshly shipped
        outlook also RE-EVALUATES the reserve tier — the midday forecast
        revision path overwrites the latch and audits reserve_tier again on
        every actual decision change (the event name stays stable, the result
        may differ)."""
        now_t = _time.monotonic()
        if (self._weather_refresh_t is not None
                and now_t - self._weather_refresh_t
                < self.cfg.forecast_refresh_min * 60.0):
            return
        self._weather_refresh_t = now_t        # a failed fetch consumes the slot
        try:
            r = self.weather.morning_radiation_j() if self.weather is not None else None
            ship_forecast(self.cfg, r)
        except Exception:                      # noqa: BLE001 — intake never disturbs
            pass
        try:
            days = self.weather.daily_outlook() if self.weather is not None else None
            ship_outlook(self.cfg, days)        # cluster copy = the read source
        except Exception:                      # noqa: BLE001
            pass
        if st is not None:
            try:
                _update_reserve_tier(self.cfg, st, self)   # revision re-evaluation
            except Exception:                  # noqa: BLE001 — the revision path never dies
                pass

    def outlook_days(self) -> tuple[list[dict] | None, str | None]:
        """deps API for the reserve-tier computation: reads the NEWEST
        shipped outlook copy straight from the OpenSearch cluster
        (ship_outlook = the write side, this = the read side) — NEVER the
        in-memory fetch cache, so no result ever depends on process memory,
        restart timing or deploy order (the timer calculation must ALWAYS
        come from the OpenSearch cluster). (None, None) = honest unknown
        (kill-switch / no doc / cluster down)."""
        return load_outlook(self.cfg)

    def day_grid_on_pct(self, start: datetime, end: datetime) -> float | None:
        """The grid-ON share of [start, end] — the bounded events-index query
        lives in bms (same home as forecast_doc_for/night_buckets); honest
        None on any failure → class "unknown", never a fabricated 0."""
        return self.bms.day_grid_on_pct(start, end)

    def latest_reserve_tier(self) -> dict | None:
        """Restart-window safety: the newest reserve_tier decision's inputs —
        the bounded events-index query lives in bms (same home as
        day_grid_on_pct); honest None on any failure (run()'s seed then keeps
        the 0 latch)."""
        return self.bms.latest_reserve_tier()

    def pack_v_avg(self) -> float | None:
        """The eve's Wh-conversion voltage is a cheap AVERAGE over the recent
        minutes (the instantaneous pack voltage is a minor but real error
        term). One cluster-side avg aggregation; honest None on any failure →
        _eve falls back to the LIVE pack_v (a sensor value), never a guess."""
        return self.bms.pack_v_avg(PACK_V_AVG_MIN)

    def reference_load(self) -> dict | None:
        """The REFERENCE-LOAD model: the newest qualifying CALENDAR-night
        grid-OFF clip load (Wh/h + provenance) — weather sunset/sunrise (the
        SAME WeatherClient path the solar-night windows use), the addon's own
        ok-confirmed journal moments and the BMS counter series, evaluated by
        gridguard.reference.

        Evaluated at most once per local day (a completed night only ever
        appears at sunrise — like night_buckets), and AUDITED on EVERY actual
        evaluation as the event class `reference_load` with the full inputs
        (night, clips, Wh, Wh/h, source) — `median_fallback` whenever nothing
        qualifies (the caller then keeps the old median method). Honest None
        on ANY consult failure; the cache holds the honest result so the
        fallback stays stable within the day too."""
        today = datetime.now(self.cfg.tz).date()
        if self._ref_cache is not None and self._ref_cache[0] == today:
            return self._ref_cache[1]
        ref = None
        try:
            ref = build_reference_load(
                sun_times_fn=(lambda d: self.weather.sun_times(d)
                              if self.weather is not None else (None, None)),
                events_fn=lambda since: self.bms.reference_events(since),
                series_fn=lambda a, b: self.bms.counter_series(a, b),
                now=datetime.now(self.cfg.tz), tz=self.cfg.tz,
                n_nights=self.cfg.reference_nights,
                min_clip_min=float(self.cfg.reference_min_clip_min),
                # three-tier rules: the deep tier2 walk + tier3 HOLD memory
                # (the newest journaled clip reference)
                history_fn=lambda: self.bms.reference_history(),
                max_lookback_days=self.cfg.reference_max_lookback_days)
        except Exception:                  # noqa: BLE001 — consult side
            ref = None                     # ANY failure = honest median fallback
        self.audit("reference_load", inputs=reference_audit_inputs(ref))
        self._ref_cache = (today, ref)
        return ref

    def solar_day_prediction(self, target_date: date) -> dict:
        """Predict the solar day and its onset for `target_date` from the
        three EXISTING streams — the rolling outlook (the reserve tier's own
        forecast transport, forecast_ok's cache), the measured solar-edge
        history (deps.solar_windows) and the yield_learned pairs
        (bms.yield_history, the payload's own query). Evaluated at most once
        per (local day, target) and AUDITED on EVERY actual evaluation as the
        event `solar_day_pred` with the FULL basis (display truth: a
        prediction is a LABEL, never state — the eve only discloses its
        horizon source, night_need.onset_src). Honest insufficient-shape dict
        on ANY consult failure → the caller keeps its guards (no guess)."""
        key = (datetime.now(self.cfg.tz).date(), target_date)
        if self._solar_pred_cache is not None and self._solar_pred_cache[0] == key:
            return self._solar_pred_cache[1]
        pred = None
        try:
            outlook, _ts = self.outlook_days()
            bounds, _sp = self.solar_windows()
            pred = _solar_day_prediction(
                target_date, forecast_days=outlook or [],
                day_bounds=bounds or {}, yield_hist=self.bms.yield_history(10),
                tz=self.cfg.tz)
        except Exception:              # noqa: BLE001 — consult side
            pred = None                # ANY failure = honest 'no prediction'
        if not isinstance(pred, dict):
            pred = {"predicted": None, "onset_h": None, "basis": "insufficient",
                    "samples_used": 0, "forecast_kwh_m2": None,
                    "analog_day": None, "factor": None}
        self.audit("solar_day_pred",
                   inputs={"target": target_date.isoformat(), **pred})
        self._solar_pred_cache = (key, pred)
        return pred

    def phase_evidence(self, since_iso: str) -> list[dict]:
        return self.bms.phase_evidence(since_iso)

    def yield_learning(self) -> dict | None:
        """Compare the SHIPPED solar forecast with the MEASURED yield of the
        same completed solar day → factor (kWh into the battery per kWh/m²
        forecast) — an ADDITIONAL dimension for the coming day. Pure learning:
        never gates anything. Once per local day; honest None (no audit) when
        either side is missing. Unlike the solar-window failure-cache rule,
        the None IS cached here — audit-dedup: one attempt per local day, no
        re-learn after an honest miss."""
        today = datetime.now(self.cfg.tz).date()
        now = datetime.now(self.cfg.tz)
        if self._yield_cache is not None and self._yield_cache[0] == today:
            return self._yield_cache[1]
        out = None
        bounds: dict = {}
        sunset_today = None
        try:
            bounds, _sp = self.solar_windows()
            sunset_today = self.solar_today_sunset()
            # a solar day is final only AT/AFTER its calendar sunset — an
            # ONGOING day's provisional last-spike (b <= now while the sun is
            # still up) must not count as a completed day (mid-day clouds
            # would otherwise truncate — and poison — the learned day). Past
            # dates are trivially final.
            def _final(d, end):
                if end > now:
                    return False
                if d < today:
                    return True
                return sunset_today is not None and now > sunset_today
            done = [d for d, (_a, b) in bounds.items() if _final(d, b)]
            if done:
                day = max(done)               # last COMPLETED measured solar day
                a, b = bounds[day]
                measured = self.bms.day_charge_kwh(a, b)
                doc = self.bms.forecast_doc_for(day, str(self.cfg.tz))
                if measured is not None and doc:
                    fj = doc.get("radiation_sum_j")
                    if isinstance(fj, (int, float)) and fj > 0:
                        fc = fj / 3.6e6
                        out = {"day": day.isoformat(),
                               "measured_kwh": round(measured, 3),
                               "forecast_kwh_m2": round(fc, 3),
                               "factor": round(measured / fc, 4),
                               "day_start": a.isoformat(timespec="minutes"),
                               "day_end": b.isoformat(timespec="minutes"),
                               "forecast_ts": doc.get("ts")}
                        # per-class yield: the SAME day-cache block asks for
                        # the day's grid-ON share — exactly ONE share query
                        # per learned day. Netted by design: a failing share
                        # query may NEVER take the learning down
                        # (None/exception → class "unknown", the factor line
                        # stays alive and audited).
                        try:
                            pct = self.day_grid_on_pct(a, b)
                        except Exception:        # noqa: BLE001 — netted by design
                            pct = None
                        out["grid_on_pct"] = pct        # verbatim (honest None)
                        out["class"] = ("unknown" if pct is None
                                        else "grid_off" if pct < GRID_OFF_CLASS_PCT
                                        else "grid_on" if pct >= GRID_ON_CLASS_PCT
                                        else "mixed")
                        self.audit("yield_learned", inputs=out)
        except (BmsError, RuntimeError):
            out = None
        # Cache the outcome unless today's day is still RUNNING: that case
        # must retry after the sunset — retries hit only the solar-window
        # cache, no new HTTP. Every other outcome is one attempt per local day
        # (audit-dedup).
        if not (out is None and today in bounds
                and (sunset_today is None or now <= sunset_today)):
            self._yield_cache = (today, out)
        return out

    def night_buckets(self) -> list[dict]:
        """Learned night buckets (one Wh figure per night, quality
        classified) — recomputed at most once per local day over the same
        rolling HIST_DAYS window (like night_profile). Fetch errors propagate
        (not cached) → _eve nets them to need=None (conservative). The night
        window = the REAL solar-night SPANS from solar_windows() — the bms
        clock default (19–08) applies only while no boundary data exists."""
        today = datetime.now(self.cfg.tz).date()
        if self._buckets_cache is not None and self._buckets_cache[0] == today:
            return self._buckets_cache[1]
        _bd, spans = self.solar_windows()
        buckets = self.bms.night_buckets(self.cfg.hist_days, str(self.cfg.tz),
                                         self.cfg.solar_noise_a, window=None,
                                         spans=spans or None)
        self._buckets_cache = (today, buckets)
        return buckets

    def bms_solar_window(self) -> list[float | None]:
        """grid-off input: pack.current samples covering >= SOLAR_HOLD_S."""
        return [s.pack_current for s in self.bms.samples_window(self.cfg.solar_hold_s)]

    # release only with sun: the ONE sun-gate verdict per consult — BOTH
    # releases (morning grid-off and eve-release) consult it. The ratio
    # itself stays forecast-free; the gate adds the AND-condition at the two
    # release points.
    def forecast_ok(self) -> tuple[bool, str]:
        """The sun gate: fetch the morning forecast ONCE per consult and
        return (ok, reason) — conservative by rule: missing data of ANY kind
        = False = both releases stay blocked (the grid stays on).

        Order: fetch (WeatherClient.morning_radiation_j —
        exception-swallowing, None on any failure) → ALWAYS ship the value
        into the forecast history (also None = error day; ship errors never
        propagate into the loop) → (False, "no weather data") on no client /
        r None → (False, "MIN_J not set") on cfg.weather_min_j None →
        comparison. `last_radiation_j` mirrors the fetched r for the audit
        inputs."""
        r = self.weather.morning_radiation_j() if self.weather is not None else None
        self.last_radiation_j = r
        try:
            ship_forecast(self.cfg, r)          # dual-write — also for None
        except Exception:                       # noqa: BLE001 — ship must never block the gate
            pass
        # storm-outage indicators (thunder CAPE + WMO code, precip
        # probability, gusts + radiation) — ships beside the forecast INTO THE
        # OPENSEARCH CLUSTER — the reserve-tier and timer reads go STRAIGHT to
        # that copy (no in-memory hand-off; the gate itself stays out of it —
        # the tier is a separate latch, audited as reserve_tier).
        try:
            days = self.weather.daily_outlook() if self.weather is not None else None
            ship_outlook(self.cfg, days)        # cluster copy = the read source
        except Exception:                       # noqa: BLE001 — intake never blocks the gate
            pass
        if r is None:
            return False, "no weather data"
        if self.cfg.weather_min_j is None:
            return False, "MIN_J not set"
        min_j = self.cfg.weather_min_j
        if r < min_j:
            return False, f"radiation {r/1e6:.1f} MJ < {min_j/1e6:.0f} MJ"
        return True, f"radiation {r/1e6:.1f} MJ ≥ {min_j/1e6:.0f} MJ"

    def read_switch(self) -> str:
        dev = self.cloud.get_device(self.cfg.sw_deviceid)
        val = (dev.get("params") or {}).get("switch")
        if val not in ("on", "off"):
            raise ChannelError(f"unexpected switch state: {val!r}")
        return val

    def timer_exists(self) -> bool:
        """Device truth behind the rescue_timer_set flag. Conservative False on
        ChannelError — a stale timer is then RE-SET on the next rescue eve
        (idempotent rebuild of the same signature), a green eve simply skips a
        clear; the rebuild/clear signature logic keeps device truth converging."""
        try:
            dev = self.cloud.get_device(self.cfg.sw_deviceid)
            timers = (dev.get("params") or {}).get("timers", [])
            return any(self._is_rescue(t) for t in timers)
        except ChannelError:
            return False

    def act(self, action: str, *, value: str | None = None,
            at: dtime | None = None, require_verify: bool = False,
            reason: str | None = None) -> bool:
        if action in ("switch_on", "switch_off"):
            value = "on" if action == "switch_on" else "off"
            if self.cfg.dry_run:
                self.audit("dry_run", action={"type": action}, result="suppressed", value=value)
                return True
            try:
                self.cloud.set_switch(self.cfg.sw_deviceid, value)
                channel = "cloud"
            except ChannelError:
                try:
                    self.lan.set_switch(value)             # fallback (reads stay cloud-only)
                    channel = "lan"
                except ChannelError as e:
                    self.push("NO CHANNEL", msg(self.cfg.msg_lang, "no_channel", action=action))
                    self.audit("act_failed", action={"type": action}, result=str(e))
                    return False
            if require_verify:
                try:
                    if self.read_switch() != value:
                        self.audit("verify_failed", action={"type": action},
                                   channel=channel, result="mismatch")
                        return False
                except ChannelError as e:
                    self.audit("verify_failed", action={"type": action}, result=str(e))
                    return False
            fields = {"action": {"type": action}, "channel": channel,
                      "result": "ok", "value": value}
            if reason is not None:      # the act line carries the reason
                fields["inputs"] = {"reason": reason}
            self.audit("act", **fields)
            return True

        if action == "disable_on_timers":
            return self._collision_clear()   # THE call shape — DRY_RUN/verify/audit live there

        raise ValueError(f"unknown action {action!r}")

    def read_state(self):
        inv = self.cloud.get_inventory(self.cfg.sw_deviceid)
        return inv["switch"], inv["timers"]

    def apply_plan(self, plan: TimerPlan) -> bool:
        """Timer-plan executor: keep_grid writes nothing (True + audited with
        the reason); failsafe_timer rides the ensure-on programming path —
        fresh read → state merge (keep foreign timers, replace ANY grid timer
        of the target STATE — a re-computed crossing must evict the stale one)
        → write → read-back verify (exactly one armed timer, the fresh
        signature). Dispatched kinds (release_night / bring_on_now / none)
        never reach here — _eve owns them. DRY_RUN suppresses every write
        (+True); audits carry timer_action = ensure_on | clear | keep | noop
        (an already-armed identical signature skips the redundant write).

        STAMP CONTRACT: on every handled failsafe_timer the executor stamps
        `data["armed_local"] = <datetime>` = what the DEVICE holds AFTER this
        handoff — the verified write's crossing, the identical program's
        crossing, or (within-deadband skip, no write) the armed signature it
        kept. Stamps only on known device truth; failure paths and DRY_RUN
        leave the key absent (the caller keeps its last confirmed value).
        _eve reads it into st.last_planned_fire so the final push can state
        the WRITE-CONFIRMED armed separately from the plan ideal."""
        ok_all = True
        for kind, data in plan.actions:
            if kind == "clear_grid_timers":
                # signal=False: _eve owns the plan-failure push — no double signal
                ok = self._collision_clear(signal=False)
                if not self.cfg.dry_run:
                    self.audit("act", action={"type": "clear_grid_timers"},
                               timer_action="clear", channel="cloud",
                               result="ok" if ok else "failed")
                ok_all = ok_all and ok
                continue
            if kind == "keep_grid":
                # switch already ON — zero writes BY DESIGN (the grid-off
                # gates release with sun). Audit = mirror + reason.
                self.audit("act", action={"type": "keep_grid"}, timer_action="keep",
                           result="ok", inputs={"reason": data.get("reason", "")})
                continue
            if kind != "failsafe_timer":
                raise ValueError(f"unknown plan action {kind!r}")
            want, t_action = "on", "ensure_on"
            sig = local_cron(data["at_local"])
            # display-truth doctrine: every ladder WRITE records its OWN
            # programmed time in the act audit — the page's announced grid-on
            # = the last result-ok ensure_on act (fire_str = the device-cron
            # minute; at_local = the same instant as naive LOCAL wall-clock,
            # matching atLocal — naive by run() contract, main.py run():
            # datetime.now(cfg.tz).replace(tzinfo=None))
            prog_inputs = {"fire_str": data.get("fire_str")
                           or data["at_local"].strftime("%H:%M"),
                           "at_local": data["at_local"].isoformat(
                               timespec="minutes")}
            if self.cfg.dry_run:
                self.audit("dry_run", action={"type": kind}, timer_action=t_action,
                           result="suppressed")
                continue
            try:
                _, timers = self.read_state()     # fresh inventory, every action
                # dedup = the STATE target (do.switch), regardless of atLocal —
                # keyed on (switch, atLocal), a RE-COMPUTED crossing kept the
                # stale timer and appended the new one: the device armed BOTH
                # and fires the old one. A moved crossing must EVICT the stale
                # timer.
                keep = [t for t in timers
                        if (t.get("do") or {}).get("switch") != want]
                armed_now = [t for t in timers
                             if (t.get("do") or {}).get("switch") == want
                             and t.get("enabled", 1) != 0]
                if (len(armed_now) == 1 and armed_now[0].get("atLocal") == sig
                        and len(keep) + 1 == len(timers)):
                    # the actor already holds EXACTLY this program — the fresh
                    # read IS the verification (nothing changed in between) →
                    # skip the redundant rewrite (repeated identical writes
                    # invite the device's refusal; the crossing only ever
                    # moves ONCE per change). noop mirrors _collision_clear's
                    # no-change branch.
                    data["armed_local"] = data["at_local"]   # device holds EXACTLY this
                    if (kind, sig) != self._last_ok_act:   # ONE confirmation per state
                        # log readability: the trail shows decisions/changes
                        # (new signature, every movement), not the 60-s
                        # heartbeat of the FINAL phase. Error and verify paths
                        # stay visible.
                        self._last_ok_act = (kind, sig)
                        self.audit("act", action={"type": kind}, timer_action="noop",
                                   channel="cloud", result="ok")
                    continue
                # --- the write DEADBAND — reprogram only on >
                # WRITE_DEADBAND_MIN crossing movement from the ARMED one. The
                # dedup's threshold semantics: |new − armed| <= X → the write
                # itself is skipped (device stability + trail noise —
                # unbounded rewrites churn the device and flood the trail),
                # audited ONCE per (kind, armed signature) as a noop "within
                # deadband". Beyond X (or an unparseable armed signature — no
                # grace for incomparable device truth) the write runs and
                # evicts the stale timer (scoped to real moves). Same
                # semantics in the FINAL window: the armed signature stays the
                # anchor everywhere (the crossing converges under the counter
                # instrument; nothing is rewritten while it drifts within X).
                armed_sig = armed_now[0].get("atLocal") if len(armed_now) == 1 \
                    else None
                move = (crossing_move_min(armed_sig, sig)
                        if len(armed_now) == 1 and len(keep) + 1 == len(timers)
                        else None)
                if move is not None and move <= WRITE_DEADBAND_MIN:
                    # no write happened — the device KEEPS its armed
                    # signature; stamp THAT (device truth, cron → wall clock
                    # on the wanted crossing's date — display anchor only) so
                    # the final push never labels the drifting ideal as
                    # armed. NOTE: this DATE anchor can be a day off across
                    # midnight — only %H:%M is ever consumed from the stamp.
                    _m = _cron_minutes(armed_sig)
                    if _m is not None:
                        data["armed_local"] = data["at_local"].replace(
                            hour=_m // 60, minute=_m % 60,
                            second=0, microsecond=0)
                    if (kind, armed_sig) != self._deadband_sig:
                        self._deadband_sig = (kind, armed_sig)
                        self.audit("act", action={"type": kind}, timer_action="noop",
                                   channel="cloud", result="ok",
                                   inputs={"within_deadband_min": move,
                                           "armed": armed_sig, "wanted": sig})
                    continue
                self.cloud.set_timers(self.cfg.sw_deviceid, keep + [data["timer"]])
                _, after = self.read_state()      # verify read-back presence of the signature
                armed = [t for t in after
                         if (t.get("do") or {}).get("switch") == want
                         and t.get("enabled", 1) != 0]
                present = len(armed) == 1 and armed[0].get("atLocal") == sig
                if not present:
                    # the ATTEMPTED generation stays time-readable in the
                    # journal; result != ok keeps it OUT of the announced slot
                    self.audit("act", action={"type": kind}, timer_action=t_action,
                               channel="cloud", result="verify_failed",
                               inputs=prog_inputs)
                    ok_all = False
                    continue
                self._deadband_sig = None    # a real write re-arms the deadband note
                data["armed_local"] = data["at_local"]   # verified write = device holds it
                if (kind, sig) != self._last_ok_act:   # as in the noop branch above
                    self._last_ok_act = (kind, sig)
                    self.audit("act", action={"type": kind}, timer_action=t_action,
                               channel="cloud", result="ok",
                               inputs=prog_inputs)
            except ChannelError as e:
                # no push here — _eve pushes the honest failure signal once,
                # on transition
                self.audit("act_failed", action={"type": kind}, timer_action=t_action,
                           result=str(e))
                ok_all = False
        return ok_all

    def _collision_clear(self, *, signal: bool = True) -> bool:
        if self.cfg.dry_run:
            self.audit("dry_run", action={"type": "disable_on_timers"}, result="suppressed")
            return True
        try:
            _, timers = self.read_state()
            new = deactivate_grid_timers(timers)
            if new == timers:
                self.audit("act", action={"type": "disable_on_timers"}, timer_action="noop",
                           channel="cloud", result="ok")
                return True
            self.cloud.set_timers(self.cfg.sw_deviceid, new)
            _, verify = self.read_state()
            # "cleared" = NO timer armed anymore — BOTH classes (on AND off);
            # robust: empty list → nothing armed → cleared ✓; a missing
            # "enabled" field or a non-dict entry counts as armed
            # (conservative, honest verify).
            still_armed = [t for t in verify
                           if not isinstance(t, dict) or t.get("enabled", 1) != 0]
            ok = not still_armed
            self.audit("act", action={"type": "disable_on_timers"}, timer_action="disable",
                       channel="cloud", result="ok" if ok else "verify_failed")
            return ok
        except ChannelError as e:
            if signal:      # direct calls (grid-off) keep their signal; apply_plan path defers to _eve
                self.push("NO CHANNEL", msg(self.cfg.msg_lang, "no_channel", action="disable_on_timers"))
            self.audit("act_failed", action={"type": "disable_on_timers"}, result=str(e))
            return False

    def _is_rescue(self, t: dict) -> bool:
        """A grid-ON timer = repeat timer that switches ON and is still
        enabled. There is NO fixed atLocal anymore (t_fire is computed per
        eve and programmed through apply_plan), so identity is direction +
        state — deactivated (enabled:0) inventory never counts as armed."""
        return is_rescue_timer(t)

    def push(self, kind: str, text: str) -> None:
        """Honest pushes: EVERY push is journaled into the jsonl (kind =
        the stable English event key, text capped at 60 chars — never the
        webhook URI) even when the ntfy call fails (try/finally); ntfy itself
        stays advisory-only. The ntfy DISPLAY title stays localized
        (messages.title) — kind = the query key, title = the language layer."""
        try:
            ntfy_push(self.cfg.ntfy_webhook, title(kind, self.cfg.msg_lang), text)
        finally:
            self.audit("push", kind=kind, inputs={"text": text[:60]})

    def audit(self, event: str, **fields) -> None:
        try:
            self.audit_log.record(event, **fields)
        except Exception as e:                         # never let audit kill run()
            print(f"[audit] {e}", file=sys.stderr, flush=True)

    def cloud_alive(self) -> bool:
        try:
            self.cloud.login()
            self.cloud.get_device(self.cfg.sw_deviceid)
            return True
        except ChannelError:
            return False

    def reachable(self) -> bool:
        return _reachable(self.cfg)


def run(cfg: Config) -> None:
    st = AppState()
    deps = RealDeps(cfg)
    deps.audit("start", result=f"dry_run={cfg.dry_run}")
    # Restart-window safety: reload the durable reserve_tier decision (events
    # index) into the fresh in-memory latch BEFORE the first decision tick —
    # a deploy restart must never open the release gate at tier=0 while the
    # standing tier still applies. Crash-netted like every consult side; no
    # audit (a seed is not a new decision) — see restore_reserve_tier.
    restore_reserve_tier(st, deps)
    # same seam for the day-scoped phase flags — a mid-day restart must not
    # leave the phase stuck at MORNING (no eve roll → no night row in the
    # frontend). See restore_phase_flags.
    restore_phase_flags(st, deps, datetime.now(cfg.tz), cfg)
    # Web surface (page+payload+health) on a crash-netted daemon thread: a
    # bind/start failure prints one honest line and the worker loop goes on.
    from .webservice import start as _web_start
    _web_start(cfg)
    while True:
        now = datetime.now(cfg.tz).replace(tzinfo=None)
        phase = current_phase(now, cfg, st.morning_done, st.night_over)
        probe_due = phase is Phase.NIGHT
        try:
            tick(cfg, st, deps, now, probe_due=probe_due)
        except Exception as e:                        # noqa: BLE001 — the ruler must not die
            deps.audit("tick_error", result=f"{type(e).__name__}: {e}")
        deps.forecast_refresh(st)   # forecast revision path (self-throttled)
        # + re-evaluates the reserve tier from the fresh outlook (midday
        # revision overwrites the latch, audits reserve_tier)
        sleep_s = {Phase.NIGHT: cfg.probe_night_s, Phase.MORNING: cfg.poll_morning_s,
                   Phase.DAY_HOLD: cfg.poll_fast_s, Phase.EVE_PLAN: cfg.poll_fast_s,
                   Phase.FINAL: min(60, cfg.poll_fast_s)}[phase]
        _time.sleep(sleep_s)


if __name__ == "__main__":
    try:
        run(load_config())
    except ConfigError as e:
        sys.exit(f"config error: {e}")
