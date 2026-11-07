import json, os, re, time, urllib.request
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

OS = os.environ.get("OPENSEARCH_URL", "http://127.0.0.1:9200").rstrip("/")
INDEX = os.environ.get("INDEX", "jkbms-*")
TZ = os.environ.get("CHARTS_TZ", "UTC")

# --- GridGuard tab (owner spec2026-10-20) ---------------------------------
GG_EVENTS = os.environ.get("GG_EVENTS_INDEX", "gridguard-events-*")
GG_WEATHER = os.environ.get("GG_WEATHER_INDEX", "gridguard-weather-*")
GG_SOLARDAY_H = int(os.environ.get("GG_SOLARDAY_H", "6"))   # history-horizon anchor only (see below)
GG_FLOW_A = float(os.environ.get("GG_FLOW_A", "3.0"))       # solar-day flow threshold (A) — owner rule (2026-10-22: raised 2.0→3.0): the DAY starts at the first ≥3A charge after sunrise, ends at the last ≥3A before sunset; below is NOT solar day — the measured sister-pack balancing transient (2.676A step at a switch instant) must never open a false day, cost on a real dawn ≈4min
# OWNER DIRECTIVE 2026-10-26 (no frontend value inventions): there is NO
# GG_WEATHER_MIN_J read/mirror here anymore — the shared bad-solar reference
# renders from BACKEND EVENTS only (see ref_kwh in fetch_gridguard:
# reserve_tier.reference_kwh_m2, fallback grid_off.weather_min_j) → absent
# from the events = no number on screen.
GG_LAT = os.environ.get("GG_LAT", "36.76")                  # site coords (same as the addon)
GG_LON = os.environ.get("GG_LON", "-4.19")

# owner REFERENCE-LOAD model (Phase A): locale-free English labels for the
# compact last_ref line ("Thu 29 Oct") — strftime %a/%b would follow the
# process locale and the payload golden is byte-exact
_WD = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_MO = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
       "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# sun times are CONSTANTS within a local day — cache per date (owner find
# 2026-10-24: the 60-s auto-refresh issued ~11k open-meteo calls/day, got
# throttled → transient (None, None) → dates chained as "sunless" and whole
# history rows vanished between refreshes). Failed fetches are NOT cached
# (the next refresh self-heals).
_SUN_CACHE: dict = {}

_FC3 = {"ts": 0.0, "data": None}


def _forecast3():
    """Next3 days' irradiation forecast (open-meteo; same unit contract as the
    addon: MJ/m²/day → kWh/m² for display). Cached60s — the page polls.
    OWNER DIRECTIVE 2026-10-26: NO reference comparison here anymore — the
    shared bad-solar reference lives in the BACKEND EVENTS (ref_kwh, resolved
    in fetch_gridguard) and gate_ok is applied there per payload value."""
    if time.time() - _FC3["ts"] < 60 and _FC3["data"] is not None:
        return _FC3["data"]
    url = (f"https://api.open-meteo.com/v1/forecast?latitude={GG_LAT}&longitude={GG_LON}"
           "&daily=shortwave_radiation_sum&timezone=auto&forecast_days=3")
    with urllib.request.urlopen(url, timeout=10) as resp:
        daily = json.loads(resp.read()).get("daily", {})
    mjs = daily.get("shortwave_radiation_sum") or []
    days = []
    for i, d in enumerate(daily.get("time", [])):
        mj = mjs[i] if i < len(mjs) else None
        days.append({"date": d,
                     "kwh_m2": None if mj is None else round(mj / 3.6, 2)})
    _FC3.update(ts=time.time(), data=days)
    return days


def _gg_search(index: str, body: dict) -> dict:
    req = urllib.request.Request(f"{OS}/{index}/_search",
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=25) as resp:
        return json.loads(resp.read())


def _gg_msearch(index: str, bodies: list[dict]) -> list[dict]:
    """Batched cluster searches — one HTTP round-trip (NDJSON _msearch).
    Responses come back aligned with `bodies` by contract."""
    if not bodies:
        return []
    nd = "".join(f'{{"index":"{index}"}}\n{json.dumps(b)}\n' for b in bodies)
    req = urllib.request.Request(f"{OS}/_msearch", data=nd.encode(),
                                 headers={"Content-Type": "application/x-ndjson"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=25) as resp:
        out = json.loads(resp.read()).get("responses", [])
    for r in out:
        if "error" in r:
            raise RuntimeError(f"cluster agg failed: {r['error']}")
    return out


# Owner QA ruling 2026-10-21: ALL energy aggregation/integration happens IN THE
# CLUSTER — never in local code (no drift between implementations; the addon's
# learning tasks issue the same aggregation). This scripted_metric sums the
# per-sample power (pack.current × pack.voltage, W) over a window; the only
# local step is the unit conversion × GG_SAMPLE_S / 3.6e6 (per-minute ingestion
# cadence → kWh). Samples absent in a gap contribute 0 (no fabricated fill).
# runtime_mappings would be neater but this cluster rejects them on search —
# scripted_metric is the portable form.
GG_SAMPLE_S = int(os.environ.get("GG_SAMPLE_S", "60"))   # per-minute ingestion
ENERGY_AGG = {"e": {"scripted_metric": {
    "init_script": "state.sum = 0.0;",
    "map_script": ("if (doc['pack.current'].size() != 0 "
                   "&& doc['pack.voltage'].size() != 0) { state.sum += "
                   "doc['pack.current'].value * doc['pack.voltage'].value; }"),
    "combine_script": "return state.sum;",
    "reduce_script": ("double s = 0.0; for (x in states) { s += (double) x; } "
                      "return s;")}}}


# --- owner ruling #10 (2026-10-26): WINDOW TO BACK-END TRUTH — boundary tiers
def _write_confirmed_fires(gen_hits, cancels, now, tz):
    """Tier (1): ON state evidence from WRITE-CONFIRMED program generations
    (ensure_on acts carrying inputs.fire_str; hits ASC by ts — the query
    contract). Device semantics = multi-write LAST-WINS: a generation is
    SUPERSEDED when the next write lands before its fire time (set_timers
    replaces the ON class); a cancel (disable/clear act) between write and
    fire disarms it; only an ALREADY-PASSED fire becomes evidence (an unfired
    eve target asserts nothing in the past — its movement shows in the hero
    announced and the act trail instead). Returns fire datetimes (tz-local),
    ascending. Time-bearing history starts the night the writer shipped
    (2026-10-26); older days fall to tier (2)/(3)."""
    prog = []
    for src in gen_hits:
        try:
            ts = datetime.fromisoformat(src.get("ts") or "")
        except ValueError:
            continue
        fs = (src.get("inputs") or {}).get("fire_str")
        if ts.tzinfo is None:
            continue
        if not (isinstance(fs, str) and re.fullmatch(r"\d{1,2}:\d{2}", fs)):
            continue                          # absence rule: never invent
        hh, mm = (int(x) for x in fs.split(":"))
        local = ts.astimezone(tz)
        fires = local.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if fires <= local:
            fires += timedelta(days=1)
        prog.append((ts, fires))
    out = []
    for i, (ts, fires) in enumerate(prog):
        nxt = prog[i + 1][0] if i + 1 < len(prog) else None
        if nxt is not None and fires > nxt:
            continue                          # superseded by the next write
        if fires > now:
            continue                          # not yet fired
        if any(ts < c <= fires for c in cancels):
            continue                          # disarmed before firing
        out.append(fires)
    return out


def _measured_on_fill(evidence, day_bounds, tier1_fires=()):
    """Tier (2): the off→on STATE GAP the journal never observed (the device
    fires across the unreachable window) closes at the CLUSTER-measured
    first-positive intake when that measurement falls STRICTLY inside the
    gap (owner-accepted instantiation of "the amp-curve" tier, ruling #10 —
    deliberately no thresholds: a steepness discriminator cannot separate a
    fire-step from a cloud-edge ramp step either). Measurement outside the
    gap → the observed boundary stands unchanged (fallback); on→off gaps are
    never filled; equality with a gap edge (M == t0 / M == t1) is OUTSIDE
    the strictly-inside contract (no fill).
    TRUE TIER-1 DOMINANCE (fix round, review b8776ee): tier-1 else tier-2 —
    whenever a write-confirmed fire OWNS the gap (lies in [t0, t1]), tier-1
    wins UNCONDITIONALLY: the fill applies only when NO tier-1 fire is
    present for that gap, so the fire instant stands even on a sunny
    blackout morning (an earlier measured step never displaces it).
    Returns (evidence, filled) — filled = the measurement-derived instants,
    so the render can DISCLOSE them at the edge."""
    if not day_bounds or len(evidence) < 2:
        return list(evidence), []
    firsts = sorted(day_bounds[d][0] for d in day_bounds)
    t1_fires = sorted(tier1_fires)
    merged = []                                # collapse same-state duplicates
    for t, s in evidence:
        if merged and merged[-1][1] == s:
            continue
        merged.append((t, s))
    filled = []
    for (t0, s0), (t1, s1) in zip(merged, merged[1:]):
        if s0 != "off" or s1 != "on":
            continue
        if any(t0 < f <= t1 for f in t1_fires):
            continue                          # tier-1 owns this gap → unconditional
        m = next((x for x in firsts if t0 < x < t1), None)
        if m is not None:
            filled.append(m)
    if not filled:
        return merged, []
    out = sorted(merged + [(m, "on") for m in filled], key=lambda e: e[0])
    final = []
    for t, s in out:
        if final and final[-1][1] == s:
            continue                          # the fill opens ONE ON run
        final.append((t, s))
    return final, sorted(filled)


def _plan_marks_into(history_plan, programs):
    """Plan ideals as EXPLICITLY-LABELLED context (ruling #10): each passed
    program anchor lands as extra["plan_marks"] = [{"at", "time"}] on the
    history row CONTAINING it — never state evidence; an anchor outside every
    row is dropped (absence over fabrication)."""
    for fires, hhmm in programs:
        for _kind, a, b, extra in history_plan:
            if a <= fires < b:
                extra.setdefault("plan_marks", []).append(
                    {"at": fires.isoformat(timespec="minutes"), "time": hhmm})
                break


def fetch_gridguard() -> dict:
    """Third tab: solar-day energy ledger + tonight's plan + decision log.

    Wire units: **kWh** (+ kWh/m² for irradiation) — the trade unit of PV
    installations, owner ruling. Physics axioms (owner): the inverter is an
    online UPS with battery-charge function OFF — grid ON ⇒ the battery is
    neither charged (solar is the ONLY charge path) nor discharged (the UPS
    feeds the loads); power-out inside KNOWN grid-ON windows (observed act /
    journal / write-confirmed fire — candidate F tier gate, ruling §14) is
    compensated to zero. A MEASURED (fill-derived) ON boundary is an
    INFERENCE, not a known label: there the raw net keeps the battery-out
    visible and the zeroed cell discloses it (measured_out) — never
    withheld, never re-added. Solar-day (owner rule2026-10-21) = the
    MEASURED window: first positive charge current (≥GG_FLOW_A) at/after
    calendar sunrise → last positive current before sunset — one solar day
    per calendar day, none on sunless days, no grid gating. All times
    rendered in TZ (site local).

    QA ruling (owner2026-10-21): every aggregation/integration — boundary
    detection, window sums, latest values — is a CLUSTER request (OpenSearch
    aggregations via _msearch, see ENERGY_AGG); this function only declares
    windows and converts units. No local integration loops → no drift between
    implementations (the addon's learning tasks use the same aggregations).
    """
    tz = ZoneInfo(TZ)
    now = datetime.now(tz)
    # history horizon: 7 days back, anchored at a fixed pre-dawn hour so the
    # window does not drift through the day (GG_SOLARDAY_H is NOT a solar-day
    # boundary anymore — the measured sunrise/sunset window above is)
    if now.hour < GG_SOLARDAY_H:
        day_start = (now - timedelta(days=1)).replace(
            hour=GG_SOLARDAY_H, minute=0, second=0, microsecond=0)
    else:
        day_start = now.replace(hour=GG_SOLARDAY_H, minute=0, second=0, microsecond=0)
    hist_start = day_start - timedelta(days=7)
    # Owner bug order (2026-11-07): the OLDEST history
    # row always rendered a leading "unlabelled" slice — every state-evidence
    # query opened AT hist_start (the same 06:00 anchor the periods use), so
    # the state that labels the first row's start (the pre-dawn fire ≈05:31 /
    # the previous eve's switch) fell JUST behind the window, state_era
    # landed mid-row and the leading slice had no covering evidence. Evidence
    # now looks back GG_EVIDENCE_LOOKBACK_H behind the horizon; periods,
    # plan marks and goldens stay exactly where they are. "Unlabelled"
    # remains the honest answer only when NO evidence exists in the extended
    # window either (never inferred to OFF — only the window boundary moved).
    EVIDENCE_LOOKBACK_H = int(os.environ.get("GG_EVIDENCE_LOOKBACK_H", "48"))
    ev_start = hist_start - timedelta(hours=EVIDENCE_LOOKBACK_H)

    # A) gridguard events: liveness, decisions, switch act labels (newest first)
    ev_hits = _gg_search(GG_EVENTS, {"size": 150, "sort": [{"ts": "desc"}],
                                     "query": {"match_all": {}},
                                     "_source": ["ts", "event", "phase", "result",
                                                 "inputs", "action", "timer_action"]})
    events = [h["_source"] for h in ev_hits.get("hits", {}).get("hits", [])]
    last_tick = next((e for e in events if e.get("event") == "tick"), None)
    last_start = next((e for e in events if e.get("event") == "start"), None)

    def _ts(e):
        try:
            return datetime.fromisoformat(e["ts"])
        except Exception:
            return None

    # STATE EVIDENCE (owner ruling2026-10-22; hierarchy refined by owner
    # ruling #10, 2026-10-26 — WINDOW TO BACK-END TRUTH): the history Actor
    # column, the compensation windows AND the hero's grid truth all come
    # from ONE merged, labelled timeline —
    #   1. successful switch acts (the system's own commands, verified)
    #   2. the observation journal (state_observed baselines/transitions)
    #   3. manual_override observations (from/to at the detection moment)
    #   4. WRITE-CONFIRMED program generations (ensure_on acts carrying
    #      inputs.fire_str — the device fires them autonomously across the
    #      WiFi blackout (22:00–10:00), which IS the point of the
    #      pre-programming; last-wins supersession + cancel rules live in
    #      _write_confirmed_fires). NO push/plan-ideal ever enters here —
    #      plan ideals render ONLY as labelled plan_marks, an unobserved
    #      off→on gap closes at the CLUSTER-measured first-positive intake
    #      (_measured_on_fill) or NOT AT ALL (absence rule). Dedicated
    #      cluster queries over the WHOLE ledger horizon — owner QA lesson
    #      2026-10-21: never buffer scans.
    ev_src = ["ts", "event", "action", "result", "timer_action", "inputs"]
    act_hits = _gg_search(GG_EVENTS, {
        "size": 400, "sort": [{"ts": "asc"}],
        # owner find 2026-10-24 ("loses data sets / not the current state"):
        # the size-400 ASC window silently cut the NEWEST events once the eve's
        # ensure-on act spam (pre-collapse: one per FINAL tick) filled it —
        # today's switch acts never arrived and the actor column contradicted
        # the live hero. Labels only need STATE-CHANGING acts (switch + the
        # timer clears); failsafe/keep_grid acts carry no state.
        "query": {"bool": {
            "filter": [{"range": {"ts": {"gte": ev_start.isoformat()}}}],
            "should": [{"bool": {"filter": [
                {"term": {"event": "act"}},
                {"terms": {"action.type": ["switch_on", "switch_off",
                                           "disable_on_timers",
                                           "clear_grid_timers"]}}]}},
                       {"term": {"event": "state_observed"}},
                       {"term": {"event": "manual_override"}}],
            "minimum_should_match": 1}},
        "_source": ev_src})
    evidence = []                                  # (ts, "on"/"off")
    cancels = []                                   # timer-armed → disarmed acts
    for h in act_hits.get("hits", {}).get("hits", []):
        e = h["_source"]
        t = _ts(e)
        if t is None:
            continue
        name = e.get("event")
        if name == "act":
            if str(e.get("result", "")).lower() != "ok":
                continue
            atype = (e.get("action") or {}).get("type")
            if atype in ("switch_on", "switch_off"):
                evidence.append((t, "on" if atype == "switch_on" else "off"))
            elif e.get("timer_action") in ("disable", "clear"):
                cancels.append(t)                 # kills a PENDING program
        else:                                     # state_observed / manual_override
            to = (e.get("inputs") or {}).get("to")
            if to in ("on", "off"):
                evidence.append((t, to))
    # tier (1): the write-confirmed program generations over the whole horizon
    gen_hits = []
    try:
        gen_hits = _gg_search(GG_EVENTS, {
            "size": 400, "sort": [{"ts": "asc"}],
            "query": {"bool": {"filter": [
                {"range": {"ts": {"gte": ev_start.isoformat()}}},
                {"term": {"event": "act"}},
                {"match_phrase": {"action.type": "failsafe_timer"}},
                {"match_phrase": {"timer_action": "ensure_on"}},
                {"term": {"result": "ok"}},
                {"exists": {"field": "inputs.fire_str"}}]}},
            "_source": ["ts", "inputs"]}).get("hits", {}).get("hits", [])
    except Exception:                              # never break the page
        gen_hits = []
    tier1_fires = _write_confirmed_fires(
        [h["_source"] for h in gen_hits], cancels, now, tz)
    evidence += [(t, "on") for t in tier1_fires]
    # PLAN IDEALS → LABELLED CONTEXT ONLY (ruling #10): the newest NIGHT PLAN
    # announcement per night (later pushes supersede an un-passed earlier
    # one), anchored to its fire instant — passed anchors become plan_marks
    # on the row containing them (_plan_marks_into), NEVER state evidence
    # (the 08:44 bug). The ANNOUNCEMENT stays a historical fact even when
    # the morning drain disarmed the timer → no cancel filtering here.
    program = None                                 # (ts_push, fires_at|None)
    program_events = []                            # passed anchors (fires_at)
    push_hits = _gg_search(GG_EVENTS, {
        "size": 200, "sort": [{"ts": "asc"}],
        "query": {"bool": {"filter": [
            {"term": {"event": "push"}},
            {"bool": {"should": [{"match_phrase": {"kind": "NIGHT PLAN"}}, {"match_phrase": {"kind": "NACHTPLAN"}}], "minimum_should_match": 1}},
            {"range": {"ts": {"gte": hist_start.isoformat()}}}]}},
        "_source": ["ts", "inputs"]}).get("hits", {}).get("hits", [])

    def _anchor(hhmm, src_ts):
        hh, mm = (int(x) for x in hhmm.split(":"))
        local = src_ts.astimezone(tz)
        fires = local.replace(hour=hh, minute=mm, second=0, microsecond=0)
        return fires if fires > local else fires + timedelta(days=1)

    for h in push_hits:
        p = h["_source"]
        pts = _ts(p)
        if pts is None:
            continue
        txt = ((p.get("inputs") or {}).get("text") or "")
        m = re.findall(r"\b(\d{1,2}:\d{2})\b", txt)
        t_fires = (_anchor(m[-1], pts)
                   if m and "no timer" not in txt else None)
        if program and program[1] is not None and program[1] <= pts:
            program_events.append(program)         # old time passed → it "fired"
        program = (pts, t_fires)                   # newest announcement wins
    if program and program[1] is not None and program[1] <= now:
        program_events.append(program)
    programs = [(f, f.strftime("%H:%M")) for (_p, f) in program_events if f]
    evidence.sort(key=lambda x: x[0])
    # (runs/windows/state_era build AFTER day_bounds below — tier (2) needs
    #  the measured day starts first)

    # B) latest forecast doc → kWh/m² (wire unit ruling: never Joules on screen)
    weather = {"radiation_kwh_m2": None, "ts": None, "source": None}
    try:
        wx = _gg_search(GG_WEATHER, {"size": 1, "sort": [{"ts": "desc"}],
                                     "query": {"match_all": {}}})
        doc = (wx.get("hits", {}).get("hits") or [{}])[0].get("_source", {})
        j = doc.get("radiation_sum_j")
        weather = {"radiation_kwh_m2": None if j is None else round(j / 3.6e6, 2),
                   "ts": doc.get("ts"), "source": doc.get("source"),
                   "kind": doc.get("kind")}
    except Exception:
        pass

    # C) BMS energy — owner energy model: pack.current is the NET of solar-in
    #    minus load-out (grid OFF: "5 in −12 out = −7 shown"; grid ON: loads
    #    ride the UPS → battery sees pure solar). One truth per period: the
    #    SUM of all values — periods, not flicker lists. Per the owner QA
    #    ruling (2026-10-21) NOTHING is integrated locally: boundaries and
    #    window sums are cluster queries (see ENERGY_AGG); this function only
    #    declares windows and converts units.
    # latest BMS doc → live card values (the cluster picks it, no local scan)
    last_hits = _gg_search(INDEX, {
        "size": 1, "sort": [{"@timestamp": "desc"}], "query": {"match_all": {}},
        "_source": ["pack.current", "pack.voltage", "soc.soc",
                    "soc.remaining_ah"]}).get("hits", {}).get("hits", [])
    last_doc = last_hits[0]["_source"] if last_hits else {}
    last_soc = last_doc.get("soc", {}).get("soc")
    last_cur = last_doc.get("pack", {}).get("current")
    last_rem = last_doc.get("soc", {}).get("remaining_ah")
    last_pv = last_doc.get("pack", {}).get("voltage")

    # --- Sun times (open-meteo, same API as the weather forecast) ---
    def _sun_times(d: date) -> tuple[datetime | None, datetime | None]:
        if d in _SUN_CACHE:                        # constants per local day
            return _SUN_CACHE[d]
        url = (f"https://api.open-meteo.com/v1/forecast?latitude={GG_LAT}"
               f"&longitude={GG_LON}&daily=sunrise,sunset&timezone=auto"
               f"&start_date={d}&end_date={d}")
        try:
            with urllib.request.urlopen(url, timeout=8) as r:
                daily = json.loads(r.read().decode()).get("daily") or {}
            rise = (daily.get("sunrise") or [None])[0]
            set_ = (daily.get("sunset") or [None])[0]
            if not rise or not set_:
                return None, None
            # open-meteo returns NAIVE local ISO strings — attach the site tz,
            # the BMS samples are tz-aware and refuse naive comparisons
            out = (datetime.fromisoformat(rise).replace(tzinfo=tz),
                   datetime.fromisoformat(set_).replace(tzinfo=tz))
            _SUN_CACHE[d] = out                    # failures stay uncached
            return out
        except Exception:
            return None, None

    # solar-day boundaries: AT MOST ONE solar day per calendar day, defined by
    # the current curve (pack.current) alone — no grid gating (owner2026-10-21).
    # The boundary is the first positive excursion (≥GG_FLOW_A) at or after
    # astronomical sunrise, and the last positive excursion before sunset.
    # A calendar day that never lifts above the threshold has NO solar day —
    # several CONSECUTIVE days may lack one (too little light); the next
    # measured day's night SPANS them as ONE long solar night (the history row
    # carries the ·N sunless d counter). No invented day, never a second pair.
    # MIRRORS gridguard/periods.py (the addon-side executable spec) — change
    # the rule → change BOTH in the same commit (this container is stdlib-only).
    # The CLUSTER finds the boundary samples (filter ≥GG_FLOW_A inside the
    # sunrise..sunset window, sort, size:1) — no local scan of raw samples.
    cand, d0 = [], hist_start.date()
    while d0 <= now.date():
        cand.append(d0)
        d0 += timedelta(days=1)
    bnd_jobs = []                                     # (date, "asc"|"desc")
    today_sunset = None
    for d in cand:
        sun_r, sun_s = _sun_times(d)
        if d == now.date():
            # owner rule2026-10-23: the ONGOING day is final only at/after
            # calendar sunset (mid-day cloud gaps must not close it)
            today_sunset = sun_s
        if sun_r is None or sun_s is None:
            continue  # no solar day without sun times (conservative)
        flt = [{"range": {"@timestamp": {"gte": sun_r.isoformat(),
                                         "lte": sun_s.isoformat()}}},
               {"range": {"pack.current": {"gte": GG_FLOW_A}}}]
        for direction in ("asc", "desc"):
            bnd_jobs.append((d, direction, {
                "size": 1, "sort": [{"@timestamp": direction}],
                "query": {"bool": {"filter": flt}},
                "_source": ["@timestamp"]}))
    day_bounds = {}                                   # measured days only
    bnd_res = _gg_msearch(INDEX, [b for _d, _dir, b in bnd_jobs])
    for (d, direction, _body), res in zip(bnd_jobs, bnd_res):
        hits = res.get("hits", {}).get("hits", [])
        if not hits:
            continue
        t = datetime.fromisoformat(hits[0]["_source"]["@timestamp"])
        t = t.replace(tzinfo=tz) if t.tzinfo is None else t
        a, b = day_bounds.get(d, (None, None))
        day_bounds[d] = (t, b) if direction == "asc" else (a, t)
    day_bounds = {d: ab for d, ab in day_bounds.items() if None not in ab}
    # hierarchy tier (2) (owner ruling #10): close the off→on state gap the
    # journal never observed at the CLUSTER-measured first-positive intake —
    # the render discloses measurement-derived starts via seg src="measured"
    # (tier-1 dominance: a gap a write-confirmed fire owns is NEVER filled)
    evidence, _filled = _measured_on_fill(evidence, day_bounds, tier1_fires)
    measured_starts = set(_filled)
    # collapse consecutive same-state events into runs → windows = the ON runs
    runs = []
    for t, s in evidence:
        if not runs or runs[-1][0] != s:
            runs.append([s, t])
    windows = [(runs[i][1], runs[i + 1][1])
               for i in range(len(runs) - 1) if runs[i][0] == "on"]
    if runs and runs[-1][0] == "on":
        windows.append((runs[-1][1], now))
    state_era = evidence[0][0] if evidence else None

    def _prev_measured(d):
        earlier = [k for k in day_bounds if k < d]
        return max(earlier) if earlier else None

    dates = sorted(day_bounds.keys())
    history_plan = []                                 # (kind, a, b, extra)
    for d in dates:
        a, b = day_bounds[d]
        prev_d = _prev_measured(d)
        if prev_d is not None:
            history_plan.append(("night", day_bounds[prev_d][1], a,
                                 {"sunless": (d - prev_d).days - 1}))
        _ongoing = (d == now.date() and today_sunset is not None
                    and now <= today_sunset)          # owner rule2026-10-23
        history_plan.append(("day", a, b,
                             {"day": d.isoformat(), "running": _ongoing}))
    # plan ideals as LABELLED context markers on their row (ruling #10)
    _plan_marks_into(history_plan, programs)

    # current cards: the running period (day if inside measured window, else
    # night) plus the last completed counterpart — always both visible; the
    # night's start = the NEAREST earlier measured day-end (sunless days in
    # between just stretch the night — winter mode).
    cur_day = cur_night = None
    if dates:
        last_d = dates[-1]
        la, lb = day_bounds[last_d]
        # owner rule2026-10-23: the ONGOING solar day is never closed by a
        # mid-day cloud gap — it runs until CALENDAR SUNSET (its length is
        # only final there; the provisional last-spike just trails). The old
        # 10-minute-since-last-spike rule flipped any longer cloud into a
        # phantom mid-day SOLAR NIGHT row. The 10-minute rule survives only
        # as the fallback when today's sun times are unknown (API blip).
        in_day = (last_d == now.date() and (
            (today_sunset is not None and now <= today_sunset) or
            (today_sunset is None and (now - lb) <= timedelta(minutes=10))))
        if in_day:
            cur_day = {"from": la, "to": now, "running": True}
        else:
            cur_day = {"from": la, "to": lb, "running": False}
        idx = dates.index(last_d)
        if in_day:
            if idx > 0:
                cur_night = {"from": day_bounds[dates[idx - 1]][1], "to": la,
                             "running": False}
        else:
            cur_night = {"from": lb, "to": now, "running": True}

    # window energies: ONE batched cluster call. Per period up to THREE
    # aggregations. Candidate F tier gate (owner ruling §14, 2026-10-26): the
    # owner compensation rule — known grid-ON ⇒ battery-out is withheld from
    # the net — rests on the label being KNOWN, so it applies only to KNOWN
    # windows (observed act / journal / write-confirmed tier-1 fire). Under a
    # MEASURED (fill-derived) window the "ON" is an inference from the solar
    # ramp edge (forensic: Oct23 07:56Z was no switch — the day-boundary
    # step, flickering back to discharge), so withholding there HID real
    # battery-out: the raw net now stays visible in kwh and the zeroed cell
    # DISCLOSES the slice (measured_out, △ tier marker) instead of pretending.
    # Window tier is decided by its START (the boundary's source — exactly
    # what the actor column discloses as seg src="measured"); one window,
    # one tier, never a per-slice split (no double-counted number).
    known_windows = [w for w in windows if w[0] not in measured_starts]
    meas_windows = [w for w in windows if w[0] in measured_starts]
    win = {}                                          # (a_iso, b_iso) → (a, b)
    for _kind, a, b, _x in history_plan:
        win[(a.isoformat(), b.isoformat())] = (a, b)
    for p in (cur_day, cur_night):
        if p:
            win[(p["from"].isoformat(), p["to"].isoformat())] = (p["from"], p["to"])
    flat, bodies = [], []
    for key, (a, b) in win.items():
        flt = [{"range": {"@timestamp": {"gte": a.isoformat(),
                                         "lt": b.isoformat()}}}]
        flat.append((key, "tot"))
        bodies.append({"size": 0, "query": {"bool": {"filter": flt}},
                       "aggs": ENERGY_AGG})
        for which, wins in (("held", known_windows), ("meas", meas_windows)):
            if not wins:
                continue
            w_flt = flt + [{"range": {"pack.current": {"lt": 0}}},
                           {"bool": {"should": [
                               {"range": {"@timestamp": {
                                   "gte": wa.isoformat(),
                                   "lte": wb.isoformat()}}}
                               for wa, wb in wins],
                               "minimum_should_match": 1}}]
            flat.append((key, which))
            bodies.append({"size": 0, "query": {"bool": {"filter": w_flt}},
                           "aggs": ENERGY_AGG})
    agg = {key: {} for key in win}
    for (key, which), res in zip(flat, _gg_msearch(INDEX, bodies)):
        agg[key][which] = res.get("aggregations", {}).get("e", {}).get("value") or 0.0

    def _sum(a, b):
        r = agg.get((a.isoformat(), b.isoformat()), {})
        f = GG_SAMPLE_S / 3.6e6                       # W·samples → kWh
        held = -r.get("held", 0.0) * f or 0.0   # withheld: KNOWN windows only
        meas = -r.get("meas", 0.0) * f or 0.0   # disclosed: MEASURED windows
        # net = raw + withheld-known; the measured slice stays INSIDE the raw
        # net (kwh) and is only disclosed — never re-added (§14 ii)
        tot = r.get("tot", 0.0) * f + held
        return round(tot, 3), round(held, 3), round(meas, 3)

    def _actor_segments(a, b):
        """Actor column (owner spec2026-10-21/22; source hierarchy ruling #10
        2026-10-26): clip the labelled grid-ON windows into the period [a, b).
        HONESTY RULE: labels exist only since the FIRST state evidence of the
        horizon (act, journal, write-confirmed program fire, measured gap
        fill) — everything before it is UNKNOWN (never inferred to OFF).
        Between labels the state is known (OFF = labelled gap, ON = inside a
        window); a boundary placed by the measured gap-fill carries
        src="measured" so the render discloses it at that edge; plan ideals
        never contribute a window (they render as plan_marks only)."""
        if state_era is None:
            return []                              # no labels at all → "—"
        era = state_era                            # first labelled evidence
        segs, cur = [], a
        if era > a:
            segs.append({"s": "unknown",
                         "from": a.isoformat(timespec="minutes"),
                         "to": era.isoformat(timespec="minutes")})
            cur = era
        for w0, w1 in windows:                     # chronological, disjoint
            if w1 <= cur or w0 >= b:
                continue
            if w0 > cur:
                segs.append({"s": "off",
                             "from": cur.isoformat(timespec="minutes"),
                             "to": min(w0, b).isoformat(timespec="minutes")})
            s0, s1 = max(w0, cur), min(w1, b)
            if s1 > s0:
                seg = {"s": "on", "from": s0.isoformat(timespec="minutes"),
                       "to": s1.isoformat(timespec="minutes")}
                if w0 < a:                         # ON began before the row
                    seg["since"] = w0.isoformat(timespec="minutes")
                if w0 in measured_starts:          # ruling #10: disclose the fill
                    seg["src"] = "measured"
                segs.append(seg)
                cur = s1
        if cur < b:
            segs.append({"s": "off", "from": cur.isoformat(timespec="minutes"),
                         "to": b.isoformat(timespec="minutes")})
        return segs

    def _row_energy(a, b):
        """kwh + withheld (known windows) + the disclosure (measured windows).
        The disclosure key appears ONLY when there is something to disclose —
        the wire shape of honest rows stays exactly what it was (§14 ii)."""
        kwh, held, meas = _sum(a, b)
        out = {"kwh": kwh, "corrected": held}
        if meas:
            out["measured_out"] = meas
        return out

    history = []
    for kind, a, b, extra in history_plan:
        row = {"kind": kind, "from": a.isoformat(timespec="minutes"),
               "to": b.isoformat(timespec="minutes"),
               "actor": _actor_segments(a, b), **_row_energy(a, b)}
        row.update(extra)
        history.append(row)
    # the RUNNING night would otherwise be invisible in the table: a night row
    # only completes when the NEXT solar day starts (its "to" = that day's
    # start). Append it explicitly, marked running (owner QA2026-10-21).
    # (A running DAY is already the partial today-row above — lb tracks the
    # last ≥GG_FLOW_A sample — so only the night needs this.)
    if cur_night and cur_night.get("running"):
        a, b = cur_night["from"], cur_night["to"]
        run_extra = {}                             # plan marks (ruling #10)
        _plan_marks_into([("night", a, b, run_extra)], programs)
        history.append({"kind": "night", "from": a.isoformat(timespec="minutes"),
                        "to": b.isoformat(timespec="minutes"), "running": True,
                        "actor": _actor_segments(a, b), **_row_energy(a, b),
                        **run_extra})

    def _per(p):
        if not p:
            return None
        return {"running": p.get("running", False),
                "today": p["from"].date() == now.date(),
                "from": p["from"].isoformat(timespec="minutes"),
                "to": p["to"].isoformat(timespec="minutes"),
                **_row_energy(p["from"], p["to"])}

    # D) plan: latest-of-kind cluster queries (size:1 each, batched) — never
    #    scans of the recent buffer, which ages them out within hours (owner
    #    QA lesson2026-10-21: eve verdict + night-need went silently blank).
    #    The DECISION LOG below still uses the buffer by design: it IS the
    #    recent story.
    plan = {
        "eve": None, "night_need": None, "night_learned": None, "latest_emergency": None,
        "yield_learned": None, "yield_hist": [],
        # owner REFERENCE-LOAD model (Phase A): the newest reference basis of
        # the crossing (event class reference_load) — newest-of-kind like the
        # plan queries; position matters: zip above maps bodies BY ORDER
        "reference_load": None,
        # owner part4 (2026-10-26): UPS-mode layer — defaults; filled by the
        # dedicated bounded queries BELOW (zip above stops at the body count)
        "reserve_tier_hist": [], "tier_hold": None,
    }
    plan_bodies = [
        # the last SWITCHING verdict: eve_* events (holds/suppressions with a
        # reason), a release (grid_off), the reserve-tier layer's switching
        # decisions (owner part3 2026-10-25: grid_on = the tier stop, tier_hold
        # = release held below the tier — the enumeration must not hide them),
        # the eve's announcement (NIGHT PLAN push) or its timer programming
        # (failsafe_timer act) — QA lesson 2026-10-22: the eve's own path
        # audits NEITHER eve_* NOR grid_off, so the programmed night stayed
        # invisible on the card
        {"size": 1, "sort": [{"ts": "desc"}],
         "query": {"bool": {"should": [
             {"prefix": {"event": "eve_"}},
             {"term": {"event": "grid_off"}},
             {"term": {"event": "grid_on"}},
             {"term": {"event": "tier_hold"}},
             {"bool": {"must": [{"term": {"event": "push"}},
                                {"bool": {"should": [{"match_phrase": {"kind": "NIGHT PLAN"}}, {"match_phrase": {"kind": "NACHTPLAN"}}], "minimum_should_match": 1}}]}},
             {"bool": {"must": [{"term": {"event": "act"}},
                                {"match_phrase": {"action.type": "failsafe_timer"}}]}},
         ], "minimum_should_match": 1}}},
        {"size": 1, "sort": [{"ts": "desc"}],
         "query": {"term": {"event": "night_need"}}},
        {"size": 1, "sort": [{"ts": "desc"}],
         "query": {"term": {"event": "night_learned"}}},
        {"size": 1, "sort": [{"ts": "desc"}],
         # owner2026-10-25: english-only event (low_capacity_on) + legacy docs
         "query": {"bool": {"should": [{"term": {"event": "emergency_on"}},
                                       {"terms": {"event": ["low_capacity_on",
                                                            "ah_tief_on"]}}],
                            "minimum_should_match": 1}}},
        # L1 yield learning (owner2026-10-22): newest learned factor (the
        # weather card shows its median over the last measured days) — same
        # newest-of-kind pattern as above, PLUS a size-10 history body
        {"size": 1, "sort": [{"ts": "desc"}],
         "query": {"term": {"event": "yield_learned"}}},
        {"size": 10, "sort": [{"ts": "desc"}],
         "query": {"term": {"event": "yield_learned"}},
         "_source": ["ts", "inputs"]},
        # owner REFERENCE-LOAD model (Phase A): the newest reference basis of
        # the crossing (night date + clips + Wh/h, or median_fallback) — the
        # payload renders it as the ONE compact last_ref display line
        {"size": 1, "sort": [{"ts": "desc"}],
         "query": {"term": {"event": "reference_load"}},
         "_source": ["ts", "inputs"]},
    ]
    for key, res in zip(plan, _gg_msearch(GG_EVENTS, plan_bodies)):
        hits = res.get("hits", {}).get("hits", [])
        # zip maps responses to plan keys BY ORDER (dict insertion order ==
        # body order) — the history body yields the full hit list
        plan[key] = ([h["_source"] for h in hits] if key == "yield_hist"
                     else (hits[0]["_source"] if hits else None))
    # owner part4 (2026-10-26): UPS-mode layer (display only) — the LATCHED
    # reserve tier for TODAY lives in the NEWEST reserve_tier event (tiers are
    # set at the eve for the COMING day and survive midnight); tier_hold = the
    # audit line of a release held below the tier (signature-guarded upstream,
    # but bounded to size 1 here anyway). Two bounded newest-first queries in
    # ONE batched round-trip; a failed query must never break the page — the
    # same error netting as the act-trail below: empty list / None instead.
    #
    # OWNER DIRECTIVE 2026-10-26 (no frontend value inventions): the SHARED
    # bad-solar reference and the history-note era resolve from BACKEND EVENTS
    # here too (third/fourth bodies of the same batch):
    #   ref_kwh ← reserve_tier.inputs.reference_kwh_m2 (the contract field —
    #             present-key wins, value or null = configured away → no
    #             number on screen), else the eve release audit's
    #             grid_off.inputs.weather_min_j (Joules → kWh/m², unit
    #             conversion only), else None → the UI renders no number.
    #   label_era ← the FIRST labelled evidence event ever (was a hardcoded
    #             "20 Oct, 20:02" literal in the history note).
    ref_kwh = None
    label_era = None
    try:
        ups_hits = _gg_msearch(GG_EVENTS, [
            {"size": 8, "sort": [{"ts": "desc"}],
             "query": {"term": {"event": "reserve_tier"}},
             "_source": ["ts", "inputs"]},
            {"size": 1, "sort": [{"ts": "desc"}],
             "query": {"term": {"event": "tier_hold"}},
             "_source": ["ts", "inputs"]},
            # fallback reference: newest release audit that actually carries
            # the configured value (exists skips nulls → unset config = no hit)
            {"size": 1, "sort": [{"ts": "desc"}],
             "query": {"bool": {"must": [{"term": {"event": "grid_off"}},
                                         {"exists": {"field": "inputs.weather_min_j"}}]}},
             "_source": ["ts", "inputs"]},
            # the first labelled evidence ever (state_observed/manual/switch)
            {"size": 1, "sort": [{"ts": "asc"}],
             "query": {"bool": {
                 "should": [{"bool": {"filter": [
                     {"term": {"event": "act"}},
                     {"terms": {"action.type": ["switch_on", "switch_off",
                                                "disable_on_timers",
                                                "clear_grid_timers"]}}]}},
                            {"term": {"event": "state_observed"}},
                            {"term": {"event": "manual_override"}}],
                 "minimum_should_match": 1}},
             "_source": ["ts"]},
        ])
        plan["reserve_tier_hist"] = [h["_source"] for h in
                                     ups_hits[0].get("hits", {}).get("hits", [])]
        hold_hits = ups_hits[1].get("hits", {}).get("hits", [])
        plan["tier_hold"] = hold_hits[0]["_source"] if hold_hits else None
        # --- shared bad-solar reference, events only (no GG_GATE_J, no literal)
        rt0 = plan["reserve_tier_hist"][0] if plan["reserve_tier_hist"] else None
        rt_in = (rt0 or {}).get("inputs") or {}
        if rt0 is not None and "reference_kwh_m2" in rt_in:
            v = rt_in["reference_kwh_m2"]
            ref_kwh = round(float(v), 2) if isinstance(v, (int, float)) else None
        else:
            go_hits = ups_hits[2].get("hits", {}).get("hits", [])
            gj = (((go_hits[0].get("_source") or {}).get("inputs") or {})
                  .get("weather_min_j") if go_hits else None)
            if isinstance(gj, (int, float)):
                ref_kwh = round(gj / 3.6e6, 2)          # J → kWh/m² display unit
        era_hits = ups_hits[3].get("hits", {}).get("hits", [])
        label_era = era_hits[0]["_source"].get("ts") if era_hits else None
    except Exception:
        plan["reserve_tier_hist"], plan["tier_hold"] = [], None
        ref_kwh, label_era = None, None


    # decision log: the STORY, not the firehose — keep_grid and repeated
    # high-frequency audit kinds appear once (newest); pushes/acts/verdicts all
    # owner part3 (2026-10-25): tier_hold can flip on every SOC crossing of the
    # reserve tier → newest line only; reserve_tier stays un-deduped (signature-
    # guarded upstream: one line per real decision change).
    high_freq = {"night_need", "night_learned", "night_profile", "night_buckets",
                 "state_observed", "yield_learned", "tier_hold",
                 "reference_load"}
    seen, decisions = set(), []
    for e in events:                                  # newest first
        name = e.get("event")
        if name == "tick":
            continue
        if name == "act" and (e.get("action") or {}).get("type") == "keep_grid":
            continue
        if name in high_freq and name in seen:
            continue
        seen.add(name)
        decisions.append(e)
        if len(decisions) >= 9:
            break

    age_s = None
    if last_tick:
        t = _ts(last_tick)
        if t is not None:
            age_s = max(int((now - t).total_seconds()), 0)
    mode = None
    if last_start:
        res = str(last_start.get("result", ""))
        mode = "LIVE" if "dry_run=False" in res else ("DRY" if "dry_run=True" in res else None)

    # state truth (owner ruling2026-10-22; sources per ruling #10 since
    # b8776ee): the SAME merged evidence timeline as the history Actor
    # column — switch acts + observation journal + manual_override
    # detections + WRITE-CONFIRMED program fires (the device fires them
    # across the blackout by design; last-wins/cancel in
    # _write_confirmed_fires) + the measured first-positive gap fill
    # (tier-2, disclosed). Pushes/plan ideals are NOT evidence here — they
    # render as labelled plan_marks only. grid_now = the current run's
    # state; grid_since = when that run STARTED (a confirming baseline
    # collapses into the run and cannot move it; a contradicting one starts
    # a run at its detection moment — honest best). Empty era → label-window
    # fallback.
    if runs:
        grid_now = runs[-1][0] == "on"
        grid_since = runs[-1][1]
    else:
        grid_open = [a for a, b in windows if a <= now <= b]
        grid_now = bool(grid_open)
        grid_since = (max(grid_open) if grid_open
                      else max((b for a, b in windows), default=None))
    nni = (plan.get("night_need") or {}).get("inputs") or {}
    wx_kwh = weather.get("radiation_kwh_m2")
    # owner REFERENCE-LOAD model (Phase A, display truth): ONE compact honest
    # line (≤60 chars) for the basis the crossing is computed from — the
    # newest evaluated reference (clip night + measured Wh/h), else the
    # median fallback with the rate the eve actually used (night_need's own
    # crossing_rate_wh_h audit field). No reference event → None (the page
    # renders nothing the journal cannot evidence — absence rule).
    rl_in = (plan.get("reference_load") or {}).get("inputs") or {}
    last_ref = None
    if str(rl_in.get("source")) == "clip" \
            and isinstance(rl_in.get("night"), str) \
            and isinstance(rl_in.get("wh_h"), (int, float)):
        try:
            _rd = date.fromisoformat(rl_in["night"])
            _label = f"{_WD[_rd.weekday()]} {_rd.day} {_MO[_rd.month - 1]}"
        except ValueError:
            _label = rl_in["night"][:12]
        last_ref = f"last_ref: {_label} clip · {rl_in['wh_h']:.0f} Wh/h"
    elif str(rl_in.get("source")) == "median_fallback":
        _rate = nni.get("crossing_rate_wh_h")
        last_ref = (f"last_ref: median-fallback ({_rate:.0f} Wh/h)"
                    if isinstance(_rate, (int, float)) and not isinstance(_rate, bool)
                    else "last_ref: median-fallback")
    # OWNER DIRECTIVE 2026-10-26: the shared reference is the payload value
    # ref_kwh resolved from the events above — None (no events / configured
    # away) means the UI renders NO reference number and NO gate verdict.
    gate_kwh = ref_kwh
    decision = {
        "as_of": (plan.get("night_need") or {}).get("ts"),
        "need_wh": nni.get("need_wh"), "clean": nni.get("clean"),
        "available_wh": nni.get("available_wh"), "safety": nni.get("safety"),
        "soc_pct": last_soc,
        "grid_now": grid_now,
        "grid_since": grid_since.isoformat(timespec="minutes") if grid_since else None,
        "last_current_a": last_cur,
        # owner's battery-capacity formula, LIVE from the newest BMS sample
        "battery_live": {"kwh": round(last_rem * last_pv / 1000.0, 3)
                         if last_rem is not None and last_pv is not None else None,
                         "soc": last_soc, "remaining_ah": last_rem,
                         "pack_v": last_pv},
        "planned_on_at": None, "planned_fires_at": None,
        # display-truth doctrine (owner 2026-10-26): ANNOUNCED = the LAST
        # write-confirmed programming (the newest result-ok ensure_on act
        # carrying its programmed time, queried below). ABSENCE RULE: no
        # such event in the journal (every historical day until tonight)
        # → None → the page shows NO announced time — it must NEVER fall
        # back to the plan ideal as if that were device-confirmed.
        "announced_on_at": None, "announced_fires_at": None,
        # owner REFERENCE-LOAD model (Phase A): the crossing's basis line
        # (computed above; None = no reference event in the journal yet)
        "last_ref": last_ref,
        # Phase B/C + D: the horizon's LABELLED source (night_need.onset_src)
        # and the reference object's FULL truth (night, every clip window with
        # its hours, Wh, Wh/h, source clip|median_fallback). Absence rules:
        # no event/audit field → None — the page renders nothing it cannot
        # evidence (a prediction is a label, never state).
        "onset_src": nni.get("onset_src"),
        "reference": rl_in or None,
        "gate": {"radiation_kwh_m2": wx_kwh, "gate_kwh_m2": gate_kwh,
                 "ts": weather.get("ts"),
                 "ok": None if (wx_kwh is None or gate_kwh is None)
                      else wx_kwh >= gate_kwh},
        "acts": [],
        "rule": "v7 · DAY: no prediction, no static gate — stored solar drains "
                "unconditionally into the loads above the safety floor (make room "
                "for new harvest) · EVE: the sole prediction — night need (median "
                "of the CLEAN nights' integrated ENERGY, Wh — never amperes × span; "
                "energy is the prediction unit) vs. battery headroom, grid RETURN "
                "programmed at the crossing · the weather value below is a "
                "LEARNING input, not a gate",
    }
    # act trail: TRANSITIONS only — keep_grid re-affirms every few minutes and
    # would evict the historic switch acts out of the recent-events buffer
    try:
        acts_hits = _gg_search(GG_EVENTS, {
            "size": 10, "sort": [{"ts": "desc"}],
            "query": {"bool": {"must": [{"term": {"event": "act"}}],
                               "must_not": [
                                   {"match_phrase": {"action.type": "keep_grid"}}]}},
            "_source": ["ts", "action", "result", "inputs", "timer_action"]})
        trail = [h["_source"] for h in acts_hits.get("hits", {}).get("hits", [])]
    except Exception:
        trail = []
    # the ladder: each write generation carries its programmed time (fire)
    # so superseded programmings stay visible beside the last announced one
    decision["acts"] = [{"ts": e.get("ts"), "type": (e.get("action") or {}).get("type"),
                         "result": e.get("result"),
                         "fire": (e.get("inputs") or {}).get("fire_str")}
                        for e in trail]
    decision["keep_grid_n"] = sum(
        1 for e in events if e.get("event") == "act"
        and (e.get("action") or {}).get("type") == "keep_grid")
    # ANNOUNCED (owner doctrine 2026-10-26): the write-confirmed program —
    # a DEDICATED newest-first cluster query (the recent buffer ages out
    # within hours — QA lesson 2026-10-21): act / failsafe_timer /
    # ensure_on, result ok, time-bearing inputs.fire_str (writer: apply_plan
    # records fire_str + at_local per write). Newest wins = the last write,
    # exactly what controls the actor (multi-write, last-wins). Absence
    # stays None — the query IS the source, no other fallback exists.
    announced_src = None
    try:
        ann_hits = _gg_search(GG_EVENTS, {
            "size": 1, "sort": [{"ts": "desc"}],
            "query": {"bool": {"filter": [
                {"term": {"event": "act"}},
                {"match_phrase": {"action.type": "failsafe_timer"}},
                {"match_phrase": {"timer_action": "ensure_on"}},
                {"term": {"result": "ok"}},
                {"exists": {"field": "inputs.fire_str"}}]}},
            "_source": ["ts", "inputs"]}).get("hits", {}).get("hits", [])
        if ann_hits:
            src0 = ann_hits[0].get("_source") or {}
            fs = (src0.get("inputs") or {}).get("fire_str")
            if isinstance(fs, str) and re.fullmatch(r"\d{1,2}:\d{2}", fs):
                decision["announced_on_at"] = fs
                announced_src = _ts(src0)
        # §31/§32 ANNOUNCED-CANCEL (owner ruling round, sanctioned): a LATER
        # ok disable/clear act means the system itself DISARMED the program →
        # the page must stop claiming it ("fires device-side, no WiFi needed"
        # for a timer we just cleared = the two-truths class). Same semantic
        # the tier-1 history cancels already use (see _write_confirmed_fires):
        # strictly-after the newest write wins; `result ok` is re-checked
        # in-code beside the query filter (a refused cloud write never killed
        # a program). A failed cancel query lands in the except below →
        # announced None (absence rule: no armed claim without evidence).
        if decision["announced_on_at"] and announced_src is not None:
            cancel_hits = _gg_search(GG_EVENTS, {
                "size": 1, "sort": [{"ts": "desc"}],
                "query": {"bool": {"filter": [
                    {"term": {"event": "act"}},
                    {"terms": {"action.type": ["disable_on_timers",
                                               "clear_grid_timers"]}},
                    {"terms": {"timer_action": ["disable", "clear"]}},
                    {"term": {"result": "ok"}}]}},
                "_source": ["ts", "result"]}).get("hits", {}).get("hits", [])
            c0 = (cancel_hits[0].get("_source") or {}) if cancel_hits else {}
            cts = _ts(c0)
            if cts is not None and str(c0.get("result", "")).lower() == "ok" \
                    and cts > announced_src:
                decision["announced_on_at"] = None    # armed claim dies with the disarm
    except Exception:                              # never break the page
        decision["announced_on_at"] = None
        announced_src = None
    if decision["announced_on_at"] and announced_src is not None:
        # same anchor rule as the plan ideal: the bare HH:MM → its NEXT
        # occurrence after the write instant (an evening write fires the
        # NEXT morning); a spent program expires from the hero card
        hh_a, mm_a = (int(x) for x in decision["announced_on_at"].split(":"))
        local_w = announced_src.astimezone(tz)
        ann_fires = local_w.replace(hour=hh_a, minute=mm_a, second=0,
                                    microsecond=0)
        if ann_fires <= local_w:
            ann_fires += timedelta(days=1)
        decision["announced_fires_at"] = ann_fires.isoformat(timespec="minutes")
    # PLAN IDEAL (owner doctrine 2026-10-26): the planner's own crossing —
    # the time in the newest NIGHT PLAN push text (final_plan). An INTENT
    # figure rendered with its own label; the write-confirmed program is
    # announced_on_at above. The old "armed act's own fields" source was
    # the conflated bug (it claimed act inputs as the plan): with the writer
    # now recording times it would re-label the WRITE as the plan ideal —
    # the push (the planner's announcement) is the plan.
    # dedicated query — the recent buffer never even carries "kind" in its
    # _source, so this fallback was silently dead (QA lesson2026-10-21)
    plan_src = None
    push_hits = _gg_search(GG_EVENTS, {
        "size": 1, "sort": [{"ts": "desc"}],
        "query": {"bool": {"filter": [{"term": {"event": "push"}},
                                      {"bool": {"should": [{"match_phrase": {"kind": "NIGHT PLAN"}}, {"match_phrase": {"kind": "NACHTPLAN"}}], "minimum_should_match": 1}}]}},
        "_source": ["ts", "inputs"]}).get("hits", {}).get("hits", [])
    if push_hits:
        txt = ((push_hits[0]["_source"].get("inputs") or {}).get("text") or "")
        m = re.findall(r"\b(\d{1,2}:\d{2})\b", txt)
        if m and "no timer" not in txt:
            decision["planned_on_at"] = m[-1]
            plan_src = _ts(push_hits[0]["_source"])
    if decision["planned_on_at"] and plan_src is not None:
        # anchor the bare HH:MM to its NEXT occurrence after the source event —
        # an evening-armed 08:13 fires TOMORROW morning, and a timer that has
        # already fired must EXPIRE from the plan card (owner QA2026-10-22:
        # the card still advertised the fired 08:13 after the morning drain)
        hh, mm = (int(x) for x in decision["planned_on_at"].split(":"))
        local_src = plan_src.astimezone(tz)
        fires = local_src.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if fires <= local_src:
            fires += timedelta(days=1)
        decision["planned_fires_at"] = fires.isoformat(timespec="minutes")

    # measured_today = TODAY's solar intake only — on a sunless calendar day
    # (or in the morning before the first ≥GG_FLOW_A crossing) the day card
    # shows the LAST measured solar day, so its kWh must NOT be relabelled
    # "today": truthful value then is 0.0 (nothing reached the battery today)
    day_per = _per(cur_day)
    # forecast chips compare the payload forecast against the payload
    # reference (both kWh/m²) — ref absent → gate_ok null → the chip renders
    # "—" (owner directive: never invent the number)
    # payload resilience (owner order 2026-11-02; doctrine fc61806 applied to
    # the page side): a dead forecast dependency must never blank the payload —
    # the page renders "forecast unavailable" for null and keeps every other
    # truth it already holds. Failures stay uncached (the next poll retries).
    try:
        fc3_days = _forecast3()
    except Exception:
        fc3_days = None
    forecast3 = (None if fc3_days is None else
                 [dict(d, gate_ok=(None if (ref_kwh is None or d["kwh_m2"] is None)
                                   else d["kwh_m2"] >= ref_kwh))
                  for d in fc3_days])
    return {
        "status": {"last_tick": last_tick.get("ts") if last_tick else None,
                   "tick_age_s": age_s,
                   "phase": (last_tick or {}).get("phase"),
                   "mode": mode,
                   "started": last_start.get("ts") if last_start else None,
                   "grid_on_windows": len(windows)},
        "counters": {"day": day_per, "night": _per(cur_night)},
        "history": history,
        "plan": plan,
        "weather": weather,
        "decisions": decisions,
        "decision": decision,
        "forecast3": forecast3,
        # owner directive 2026-10-26: first labelled evidence, events only —
        # the history note used to hardcode "20 Oct, 20:02"
        "label_era": label_era,
        "measured_today_kwh": day_per["kwh"] if day_per and day_per["today"] else 0.0,
    }

