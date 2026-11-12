"""gridguard.reference — the owner's REFERENCE-LOAD model (Phase A, 2026-10-30).

Owner ruling (the verdict behind this module): the CLEAN-median WHOLE-NIGHT
total must NOT be the instantaneous law of the grid-RETURN crossing. The
reference is the LOAD OF THE MOST RECENT POSSIBLE NIGHT, measured under
laboratory conditions:

* the CALENDAR night = [weather sunset, next weather sunrise] — the SAME
  weather client path `deps.solar_night_windows`/the eve-onset fallback use
  (no second weather path);
* clipped to GRID-OFF intervals — no sun input + no grid = the discharge is
  the pure house load. A mid-night grid switch is a KNOWN moment (the addon's
  own ok-confirmed journal writes) and CUTS the night into usable clip
  segments — never a discard class ("the amount of data merely helps a better
  mean — statistics, that's all": plain mean, no model machinery);
* segment energy = (Ah_start − Ah_end) × the segment's mean pack V — the BMS
  per-minute counter, unit Wh/h is explicitly sanctioned;
* each clip must be >= `min_clip_min` (default 30 min); a night's load =
  ΣWh ÷ Σhours over ITS qualifying clips. A still-running night (sunrise
  ahead) is never a candidate — the reference is always a COMPLETED night;
* OWNER RULES 2026-11-12 (final form, 23:3x — always a RATE Wh/h, consulted
  ONLY for the timer projection): the reference = the ASTRONOMICAL night
  (sunset→sunrise, defined INDEPENDENTLY of the grid state) ∩ grid off, and
  the candidates are RANKED BY THE COVERED GRID-OFF HOURS — "Je mehr
  Stunden wir hier bekommen, desto genauer ist das Lastprofil" (the whole
  off-grid night = the max-hours case, NOT a separate class; the gapless
  notion is dissolved for the measurement, `full` = display disclosure):
  the freshness gate = the `n_nights` window (the owner's seven-day rule,
  day7 inclusive); nothing in the window → the same hours-ranking at ANY
  AGE (the winter rule: "die Off-Grid-Nacht die wir haben direkt nach
  Sonnenuntergang");
  tier3 = HOLD the last journaled clip value until new data arrives (the
    value WANDERS — every new measurement replaces it; grid-on nights learn
    NOTHING: the battery is isolated from the loads, no median of nothing);
* nothing of any tier and no history → return None: the caller keeps the old
  median method and the audit event carries `source: "median_fallback"`.

Every evaluation is audited by the caller (RealDeps.reference_load) as the
event class `reference_load` with the inputs built by `audit_inputs` — the
night date, every clip window, the Wh, the resulting Wh/h and the source.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, tzinfo

_MIN_CLIP_RE = re.compile(r"\d{1,2}:\d{2}")   # fire_str contract (payload mirror)


def _aware(dt: datetime, tz: tzinfo) -> datetime:
    """F5 doctrine: NAIVE timestamps are LOCAL wall-clock time (attach the tz);
    aware stamps convert into the reference tz."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=tz)
    return dt.astimezone(tz)


def grid_off_windows(events, horizon_start, horizon_end, tz: tzinfo) -> list:
    """Positively-KNOWN grid-OFF intervals from the addon's own journal.

    `events` = the ok-confirmed act entries the payload's announced/cancel
    queries already read (dicts with ts/event/action/timer_action/result/
    inputs). Reconstruction:

    * act `switch_off` ok → OFF from its ts (positive off evidence);
    * act `switch_on`  ok → ON  from its ts (closes the open OFF window);
    * a write-confirmed program (act `failsafe_timer` ok carrying inputs.fire_str
      — apply_plan's ensure_on writes) → ON at its anchored fire instant
      (next occurrence of the bare HH:MM after the write — the payload's
      _write_confirmed_fires rule), REPEATING daily while the generation is
      armed: until the NEXT ensure_on write (the device's ON class is replaced)
      or the first later ok disable/clear act (the cancel). Known moments only —
      a state that no ok write established stays UNKNOWN and yields no clip.

    Returned windows are clipped to [horizon_start, horizon_end], ascending."""
    h0, h1 = _aware(horizon_start, tz), _aware(horizon_end, tz)
    entries: list[tuple[datetime, str]] = []
    programs: list[tuple[datetime, int, int]] = []
    cancels: list[datetime] = []
    for ev in events or []:
        if not isinstance(ev, dict) or ev.get("result") != "ok":
            continue
        try:
            ts = _aware(datetime.fromisoformat(str(ev.get("ts"))), tz)
        except (TypeError, ValueError):
            continue
        act = (ev.get("action") or {}).get("type")
        if act in ("disable_on_timers", "clear_grid_timers"):
            cancels.append(ts)
        if act == "switch_off":
            entries.append((ts, "off"))
        elif act == "switch_on":
            entries.append((ts, "on"))
        elif act == "failsafe_timer":
            fs = (ev.get("inputs") or {}).get("fire_str")
            if isinstance(fs, str) and _MIN_CLIP_RE.fullmatch(fs):
                hh, mm = (int(x) for x in fs.split(":"))
                if 0 <= hh <= 23 and 0 <= mm <= 59:
                    programs.append((ts, hh, mm))
    programs.sort(key=lambda p: p[0])
    for i, (w, hh, mm) in enumerate(programs):
        armed_until = programs[i + 1][0] if i + 1 < len(programs) else None
        # the EARLIEST cancel after the write kills the generation — min() so
        # the result is independent of the query's sort direction (desc safe)
        cancel = min((c for c in cancels if c > w), default=None)
        if cancel is not None:
            armed_until = cancel if armed_until is None else min(armed_until, cancel)
        fire = w.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if fire <= w:
            fire += timedelta(days=1)
        while fire <= h1:
            if armed_until is not None and fire > armed_until:
                break
            entries.append((fire, "on"))
            fire += timedelta(days=1)
    entries.sort(key=lambda p: p[0])
    # walk the positive-evidence timeline → OFF runs (an ON without a prior
    # known OFF simply leaves the state unknown — no clip, no guess)
    windows, off_since = [], None
    for ts, state in entries:
        if state == "off":
            if off_since is None:
                off_since = ts
        elif off_since is not None:
            if ts > off_since:
                windows.append((off_since, ts))
            off_since = None
    if off_since is not None:
        windows.append((off_since, h1))
    out = []
    for a, b in windows:
        a2, b2 = max(a, h0), min(b, h1)
        if b2 > a2:
            out.append((a2, b2))
    return out


def _night_clips(sunset: datetime, sunrise: datetime, off_windows, samples,
                 min_clip_min: float) -> list[dict]:
    """One calendar night → its qualifying grid-OFF clip segments.

    Per clip: boundary counter samples (last <= start, first >= end) give the
    Ah delta; the mean pack V over the clip's own samples converts it to Wh
    (owner formula: segment Wh = ΔAh × V_mean). A clip is DISCARDED only when
    it is too short, has no boundary/inside voltage evidence, or shows no
    discharge at all (ΔAh <= 0 — the pure-house-load premise is broken)."""
    clips = []
    for a, b in off_windows:
        s, e = max(a, sunset), min(b, sunrise)
        if e <= s:
            continue
        hours = (e - s).total_seconds() / 3600.0
        if hours * 60.0 < min_clip_min:
            continue
        before = after = None       # boundary counter samples (ts, Ah):
        inside_v = []                # last <= clip start / first >= clip end
        for ts, ah, v in samples:
            if s <= ts <= e and v is not None:
                inside_v.append(v)   # the clip's own voltage evidence (V_mean)
            if ah is None:
                continue
            if ts <= s:
                before = (ts, ah)
            elif after is None and ts >= e:
                after = (ts, ah)
        if before is None or after is None or after[0] <= before[0]:
            continue                 # boundary unknown → the clip proves nothing
        delta_ah = before[1] - after[1]
        if delta_ah <= 0 or not inside_v:
            continue
        v_mean = sum(inside_v) / len(inside_v)
        clips.append({"start": s.isoformat(timespec="minutes"),
                      "end": e.isoformat(timespec="minutes"),
                      "wh": round(delta_ah * v_mean, 1),
                      "hours": round(hours, 6)})
    return clips


def _covers_night(sunset: datetime, sunrise: datetime, off_windows, tz) -> bool:
    """TIER1 classifier (owner rules 2026-11-12): does the KNOWN grid-OFF
    timeline cover the WHOLE solar night [sunset, sunrise] without a gap?
    Any gap = grid-ON or UNKNOWN state inside the night → not a whole
    off-grid night (grid-on learns nothing: the battery is isolated from the
    loads; unknown earns no claim)."""
    segs = sorted((max(_aware(a, tz), sunset), min(_aware(b, tz), sunrise))
                  for a, b in off_windows)
    segs = [(a, b) for a, b in segs if b > a]
    if not segs:
        return False
    cur = sunset
    for a, b in segs:
        if a > cur:
            return False                      # gap = ON/unknown inside the night
        cur = max(cur, b)
        if cur >= sunrise:
            return True
    return cur >= sunrise


def newest_reference(nights, off_windows, samples, *, now=None,
                     min_clip_min: float = 30.0) -> dict | None:
    """THE HOURS-RANKED pick (owner ruling 2026-11-12, 23:3x). `nights` =
    newest-first `(date, sunset, sunrise)` — one block at a time. The
    load-profile reference = the ASTRONOMICAL night (defined independently of
    the grid state) ∩ grid off, and the selection is ranked by the COVERED
    GRID-OFF HOURS: "Je mehr Stunden wir hier bekommen, desto genauer ist
    das Lastprofil" — the whole off-grid night is just the max-hours case,
    NOT a separate class (the gapless notion is dissolved for the
    measurement; `full` stays a display disclosure only). Ties keep the
    NEWEST (nights come newest-first, the comparison is strictly greater).
    Running nights never qualify; None = nothing here (the caller walks the
    freshness window, then deeper, then holds the last value)."""
    if not nights or not off_windows:
        return None
    tz = off_windows[0][0].tzinfo
    norm = sorted((_aware(ts, tz), ah, v) for ts, ah, v in (samples or []))
    now_a = _aware(now, tz) if now is not None else None
    best = None
    best_hours = -1.0
    for night_date, sunset, sunrise in nights:
        s0, r0 = _aware(sunset, tz), _aware(sunrise, tz)
        if now_a is not None and r0 > now_a:
            continue                          # still running → not POSSIBLE yet
        clips = _night_clips(s0, r0, off_windows, norm, min_clip_min)
        if not clips:
            continue                          # nothing measurable this night
        wh = round(sum(c["wh"] for c in clips), 1)
        hours = sum(c["hours"] for c in clips)
        if hours <= 0:
            continue
        nd = (night_date.date() if isinstance(night_date, datetime)
              else night_date)
        entry = {"night": nd.isoformat(), "source": "clip", "clips": clips,
                 "wh": wh, "wh_h": round(wh / hours, 1),
                 "min_clip_min": min_clip_min}
        entry["full"] = _covers_night(s0, r0, off_windows, tz)  # display only
        if hours > best_hours:                # more grid-off hours = more accurate
            best, best_hours = entry, hours
    return best


def reference_load(*, sun_times_fn, events_fn, series_fn, now, tz: tzinfo,
                   n_nights: int = 7, min_clip_min: float = 30.0,
                   history_fn=None,
                   max_lookback_days: int = 365) -> dict | None:
    """THE THREE-TIER reference (owner rules 2026-11-12) — always a RATE
    (Wh/h), consulted ONLY for the timer projection.

    Blocks of `n_nights` calendar nights, newest first: block0 = the
    FRESHNESS window (the owner's seven-day rule, day7 inclusive — the +1
    edge date; its hours-ranked candidate wins there); deeper blocks = the
    SAME hours-ranking at ANY AGE (bounded by `max_lookback_days`), visited
    only when the window is empty. The switch acts are
    fetched FIRST per block — a span without a single `switch_off` seed cannot
    produce any window, so its sun/counter consults are skipped (the deep walk
    stays cheap); sun times are memoized per evaluation (astronomy is constant
    per date). NOTHING found in the whole walk → tier3: `history_fn()` = the
    newest journaled clip reference, returned as `source: "held"` until new
    data arrives (the value wanders — every fresh measurement replaces it).
    None only without history_fn/any history — the caller keeps the old
    median method (the `median_fallback` audit marker)."""
    now_a = _aware(now, tz)
    step = max(1, int(n_nights))
    # +1 = the window EDGE date: the night whose SUNSET sits exactly
    # `n_nights` days back must still be evaluated — day 7 is INCLUSIVE in
    # the owner's rule ("wenn wir sieben Tage keinen Wert mehr bekommen
    # haben") and the currently-RUNNING night eats one slot otherwise
    # (live find 2026-11-12: the day-7 full night fell just outside the
    # block and a day-2 partial won).
    total = max(int(n_nights), int(max_lookback_days)) + 1
    _sun_memo: dict = {}

    def _sun(d):
        if d not in _sun_memo:
            _sun_memo[d] = sun_times_fn(d)
        return _sun_memo[d]

    block_start, block_no = 0, 0
    while block_start < total:
        is_first = (block_no == 0)
        count = (step + 1) if is_first else step
        block_no += 1
        dates = [now_a.date() - timedelta(days=i)
                 for i in range(block_start,
                                min(block_start + count, total))]
        block_start += count
        if not dates:
            break
        # acts first (one bounded query): no switch_off in the span → no
        # window can exist there — skip the sun/counter consults entirely
        probe_start = (datetime(min(dates).year, min(dates).month, min(dates).day)
                       - timedelta(hours=40))
        try:
            events = events_fn(_aware(probe_start, tz))
        except Exception:                     # noqa: BLE001 — consult-side rule
            events = []
        if not any((e.get("action") or {}).get("type") == "switch_off"
                   for e in events if isinstance(e, dict)):
            continue                          # no OFF seed → nothing measurable here
        nights = []
        for d in dates:
            try:
                s_rise, s_set = _sun(d)
                n_rise, _ = _sun(d + timedelta(days=1))
            except Exception:                 # noqa: BLE001
                continue
            if s_set is None or n_rise is None:
                continue                      # weather unknown → no window
            sunset, sunrise = _aware(s_set, tz), _aware(n_rise, tz)
            if sunrise <= sunset:
                continue
            nights.append((d, sunset, sunrise))
        if not nights:
            continue                          # a sunless block must not hide older ones
        horizon_start = min(n[1] for n in nights) - timedelta(hours=36)
        try:
            series = series_fn(min(n[1] for n in nights), now_a)
        except Exception:                     # noqa: BLE001
            series = []
        offs = grid_off_windows(events, horizon_start, now_a, tz)
        ref = newest_reference(nights, offs, series, now=now_a,
                               min_clip_min=min_clip_min)
        if ref is not None:
            return ref
    # --- tier3: HOLD the last journaled clip value until new data arrives ---
    if history_fn is None:
        return None
    try:
        hist = history_fn() or []
    except Exception:                         # noqa: BLE001 — consult-side rule
        return None
    for ev in hist:
        if not isinstance(ev, dict):
            continue
        inputs = ev.get("inputs") or {}
        if str(inputs.get("source")) == "clip" \
                and isinstance(inputs.get("night"), str) \
                and isinstance(inputs.get("wh_h"), (int, float)) \
                and inputs.get("wh_h", 0) > 0:
            held = dict(inputs)
            held["source"] = "held"
            held["held_ts"] = ev.get("ts")
            return held
    return None


def audit_inputs(ref: dict | None) -> dict:
    """The `reference_load` event inputs (audit contract, scope 1): the night
    date, every clip window with its Wh and hours, the resulting Wh/h and the
    source — `median_fallback` whenever no reference exists (the caller then
    keeps the old median method)."""
    if not isinstance(ref, dict):
        return {"source": "median_fallback"}
    out = {"source": ref.get("source") or "none", "night": ref.get("night"),
           "clips": ref.get("clips") or [], "wh": ref.get("wh"),
           "wh_h": ref.get("wh_h"), "min_clip_min": ref.get("min_clip_min")}
    if ref.get("full"):                       # tier1 disclosure (partial = absent)
        out["full"] = True
    if ref.get("held_ts"):                    # tier3 provenance (the held event)
        out["held_ts"] = ref["held_ts"]
    return out
