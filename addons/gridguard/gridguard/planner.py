"""v4 evening ratio planner (spec v4 §3/§6) — pure; execution lives in main.

The ONE evening decision (§2/§3): `available >= SAFETY_FACTOR × need`, where
`need` is the median Wh of the CLEAN night buckets (learn.night_need_wh) and
`available` is the remaining pack energy in Wh. Conservative by construction:
None on either side = not-ok (keep/bring the grid on) — the None-under-2-CLEAN
default is the standing answer while the §9.1 ratio ↔ R2' grid-off-gate interlock
stays OPEN (only the SUN part of §9.1 is ruled — see the AND-gate below).

t_fire is demoted from decision core to FAILSAFE TIMER (spec §8): on a green
ratio with the switch still OFF, an ON timer is programmed at the moment the
learned drain strikes the floor — only when that crossing lies INSIDE the
learned night. No forecast anywhere IN the ratio (controller ruling
2026-10-19); §9.1 (owner rule "release only with sun") adds the sun gate as
a SEPARATE AND-condition: `ok = ratio_ok AND forecast_ok` (default False =
conservative — the fetch/ship lives in main.RealDeps.forecast_ok, never here).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo

from .config import Config


@dataclass(frozen=True)
class TimerPlan:
    actions: list  # list[tuple[str, dict]]


def local_cron(dt: datetime) -> str:
    return f"{dt.minute} {dt.hour} * * *"


def utc_cron(dt: datetime, tz: tzinfo) -> str:
    # hour shifted by the CURRENT offset of cfg.tz (same algorithm as _timer helper)
    off = dt.utcoffset() or datetime.now(tz).utcoffset()
    off_h = int(off.total_seconds() // 3600)
    return f"{dt.minute} {(dt.hour - off_h) % 24} * * *"


def _timer(do_switch: str, at_local: datetime, tz: tzinfo) -> dict:
    return {"type": "repeat", "coolkit_timer_type": "repeat", "enabled": 1,
            "do": {"switch": do_switch}, "at": utc_cron(at_local, tz),
            "atLocal": local_cron(at_local)}


def build_on_timer(at_local: datetime, tz: tzinfo) -> dict:
    return _timer("on", at_local, tz)


def build_off_timer(at_local: datetime, tz: tzinfo) -> dict:
    return _timer("off", at_local, tz)


def night_floor_wh(cfg: Config, full_ah: float | None, pack_v: float | None,
                   reserve_floor_pct: int | None = None) -> float | None:
    """The ONE floor law (owner rulings 2026-10-25 L2 wave1 + part 3):
    floor_wh = SoC curve(TRUE_FLOOR_V) × capacity × LIVE pack_v, raised to the
    RESERVE TIER via max() (the tier is a BMS-SOC percent of the SAME capacity
    figure — both compose; the tier can only RAISE the night floor, the deep
    voltage floor stays untouched beneath it). Shared by the eve plan and the
    overnight re-estimation. None whenever a factor is unknown — pack_v is
    checked HERE so the multiplication can never TypeError on None before a
    caller's unknown-branch guard (a doc with full_ah but no pack.voltage
    lands conservative). None/0 reserve = the documented pass-through."""
    from .verdict import soc_pct_from_v
    floor_pct = soc_pct_from_v(cfg.true_floor_v)
    if reserve_floor_pct:
        floor_pct = (max(floor_pct, reserve_floor_pct) if floor_pct is not None
                     else float(reserve_floor_pct))
    if floor_pct is None or full_ah is None or pack_v is None:
        return None
    return floor_pct / 100.0 * full_ah * pack_v


def reference_crossing(now: datetime, available_wh: float | None,
                       load_wh_h: float | None, cfg: Config, *,
                       full_ah: float | None = None,
                       pack_v: float | None = None,
                       reserve_floor_pct: int | None = None) -> datetime | None:
    """Owner REFERENCE-LOAD model (Phase A, 2026-10-30): the SAME crossing law
    as plan_evening restricted to the reference basis — the overnight
    re-estimation recomputes t_fire = now + max(0, avail − floor) ÷
    (load × safety) from the fresh §19 counter anchor. None whenever the
    available/floor/load is unknown — a guessed time is never returned (the
    emergency floors keep owning safety underneath)."""
    floor_wh = night_floor_wh(cfg, full_ah, pack_v, reserve_floor_pct)
    if (available_wh is None or floor_wh is None or load_wh_h is None
            or load_wh_h <= 0):
        return None
    head = max(0.0, available_wh - floor_wh)
    return now + timedelta(hours=head / (load_wh_h * cfg.safety_factor))


def ratio_ok(available_wh: float | None, need_wh: float | None,
             cfg: Config) -> bool:
    """Spec v4 §3: available ≥ SAFETY × night need. None on either side is
    conservatively NOT ok (no guess without evidence)."""
    if available_wh is None or need_wh is None:
        return False
    return available_wh >= cfg.safety_factor * need_wh


def hold_reason(available_wh: float | None, need_wh: float | None,
                cfg: Config, forecast_ok: bool = True,
                sun_reason: str = "") -> str:
    """The single wording source for the grid_hold push (§9.7 honesty fix):
    NO "grid on:"-prefix duplication (the title already carries the state);
    numbers only when BOTH figures are known, else a reason without numbers.
    §9.1 (T3): ratio held but the sun gate said no → the SUN is the reason
    (`sun_reason` = the forecast_ok() explanation; empty = the planner-internal
    call, no dangling colon). Default forecast_ok=True keeps every existing
    call site's wording untouched; None- and ratio-reasons come BEFORE the
    sun line (the ratio stays the binding restriction when IT fails)."""
    if forecast_ok is False and ratio_ok(available_wh, need_wh, cfg):
        return (f"solar not released: {sun_reason}" if sun_reason
                else "solar not released")
    if need_wh is None:
        return "reason: night need not yet determinable"
    if available_wh is None:
        return "reason: state of charge not yet determinable"
    return (f"available {available_wh:.0f} Wh < "
            f"{cfg.safety_factor:.1f}× night need {need_wh:.0f} Wh")


def plan_evening(now: datetime, available_wh: float | None,
                 need_wh: float | None, switch_state: str,
                 night_len_hours: float, cfg: Config,
                 forecast_ok: bool = False,
                 pack_v: float | None = None,
                 full_ah: float | None = None,
                 reserve_floor_pct: int | None = None,
                 next_onset: datetime | None = None,
                 load_wh_h: float | None = None,
                 horizon_hours: float | None = None) -> TimerPlan:
    """v5 evening plan (NORTH STAR spec §13: MAXIMIZE SOLAR USE — owner
    2026-10-20: battery usage first, no idle half-full battery; the weather
    gate lives in the MORNING release: whether/how fast the next day refills).
    The NEED is the anchor (one CLEAN night suffices, min_clean=1):
    0. need / available / pack_v unknown → conservative: OFF → bring_on_now
       (the grid-off-suppression in main still applies), ON → keep_grid.
    1. need known → battery-first schedule: floor = SoC curve(TRUE_FLOOR_V) ×
       capacity × LIVE pack_v (owner2026-10-25: voltage-anchored);
       rate = safety × need ÷ max(1.0, night_len) — the plan assumes the
       night eats safety × the measured need (default safety = 1.0 since the
       owner's deep-drain ruling2026-10-22: the ×1.2 made the return fire at
       ~29 % SOC and wasted harvest room; the insurance now is the accepted
       physical backstop — 0 % BMS ≈ real end 3 V/cell ≈ 12 V pack);
       t_fire = now + max(0, available − floor) ÷ rate.
    2. OFF + crossing inside the learned night → failsafe_timer (the grid
       returns at the crossing; the battery carries the head); crossing
       beyond → none (the battery carries the whole night).
    3. ON + headroom above the floor → release_night THEN failsafe_timer:
       spend the stored solar NOW, program the grid back at the crossing —
       a passing forecast is NOT required at the eve (spending stored solar
       is always the goal; a sunless day simply has the grid carry loads).
    4. ON + no headroom → keep_grid (nothing to give above the reserve).
    Unknowns never get a guessed number (owner doctrine: sensor values).

    `reserve_floor_pct` (owner ruling 2026-10-25, part 3): the weather-
    conditional reserve tier as a BMS-SOC percent — floors the night target
    at the tier band (max with the voltage-curve floor, see below); main.py
    passes it only when the latch is > 0 (None/0 = unchanged behavior).

    `next_onset` (owner ruling §31, 2026-10-28): the ABSOLUTE next solar
    edge (the array-onset datetime, naive local) — the ON-timer exists ONLY
    as the WiFi-blackout insurance, so a crossing at/after sunrise must be
    REFUSED (the 11:38 incident: available ≈ need put the crossing just
    under the length clamp = an armed post-sunrise device fire). The
    existing `now + night_len` length clamp stays the OUTER guard (both
    remain, §32); None = no absolute edge known → the length clamp alone
    (no guess). On the ON-case the NIGHT RELEASE survives (spending stored
    solar is its own doctrine §13) — only the obsolete timer is dropped
    (after sunrise a direct reaction exists).

    `load_wh_h` (owner REFERENCE-LOAD model, Phase A 2026-10-30): the
    measured house load of the newest qualifying calendar night (grid-OFF
    clip mean, Wh/h, reference.reference_load). Present → it REPLACES the
    median basis in the crossing rate (rate = load × safety); absent (None)
    → the legacy rate need × safety ÷ night_len, byte-identical to the old
    behavior (night_len stays the horizon clamp/divisor fallback — Phase B
    redoes it). The safety factor rides the LOAD (conservative-preserving);
    the owner CLOSED safety at 1.0 (2026-10-30, "change nothing about it") —
    NOT an open knob.

    `horizon_hours` (Phase B re-scope, folded into the C/D wave): the LENGTH
    clamp may run on a horizon of ANY measured span length (owner: the gate
    was horizon-blocking; the crossing's fallback divisor is a SEPARATE role —
    night_len keeps its semantics untouched, only the clamp reads this).
    None (default) → the clamp uses night_len_hours exactly as before."""
    # owner rulings 2026-10-25 (L2 wave1 + part 3): the ONE floor law moved
    # into night_floor_wh (SoC curve × capacity × live pack_v, reserve tier
    # via max()) — shared with the overnight re-estimation; unknown factor →
    # None → the conservative branch below (no guessed floor, no TypeError).
    floor_wh = night_floor_wh(cfg, full_ah, pack_v, reserve_floor_pct)
    if (need_wh is None or available_wh is None or pack_v is None
            or floor_wh is None):
        if switch_state == "on":
            return TimerPlan(actions=[("keep_grid",
                                       {"reason": hold_reason(available_wh, need_wh, cfg)})])
        return TimerPlan(actions=[("bring_on_now", {})])
    # owner REFERENCE-LOAD model (Phase A 2026-10-30): the rate law. With a
    # measured calendar-night clip load it IS the load (× safety — the
    # conservative-preserving placement; the owner CLOSED safety at 1.0 on
    # 2026-10-30, "change nothing about it" — NOT a knob); without it the legacy
    # median basis (need × safety ÷ night_len) stays byte-identical —
    # night_len remains the horizon fallback, never the reference's divisor.
    if load_wh_h is not None and load_wh_h > 0:
        rate = load_wh_h * cfg.safety_factor
    else:
        rate = need_wh * cfg.safety_factor / max(1.0, night_len_hours)
    head = max(0.0, available_wh - floor_wh)
    t_fire = now + timedelta(hours=head / rate)
    # Phase B: the length clamp reads the HORIZON (any measured span length
    # via horizon_hours); night_len above stays the fallback divisor role
    horizon = horizon_hours if horizon_hours is not None else night_len_hours
    if switch_state == "off":
        if t_fire >= now + timedelta(hours=horizon):
            return TimerPlan(actions=[("none",
                                       {"reason": "battery carries the whole night"})])
        # owner ruling §31 (§32: both clamps stay): the ABSOLUTE edge — a
        # crossing at/after the next array-onset would arm a post-sunrise
        # fire whose purpose (the blackout) is over before it even starts.
        if next_onset is not None and t_fire >= next_onset:
            return TimerPlan(actions=[("none",
                                       {"reason": "battery carries until sunrise"})])
        return TimerPlan(actions=[("failsafe_timer",
                                   {"at_local": t_fire,
                                    "timer": build_on_timer(t_fire, cfg.tz),
                                    "fire_str": t_fire.strftime("%H:%M")})])
    if head <= 0:
        return TimerPlan(actions=[("keep_grid",
                                   {"reason": "reason: battery at floor reserve only"})])
    # §31 ON-case: the release stays (§13), the post-sunrise TIMER does not —
    # the direct reaction exists once actor contact is back after sunrise.
    if next_onset is not None and t_fire >= next_onset:
        return TimerPlan(actions=[("release_night", {})])
    return TimerPlan(actions=[
        ("release_night", {}),
        ("failsafe_timer", {"at_local": t_fire,
                            "timer": build_on_timer(t_fire, cfg.tz),
                            "fire_str": t_fire.strftime("%H:%M")})])
