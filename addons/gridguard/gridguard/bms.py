"""Read the BMS time series from OpenSearch (field contract: backend/os_push.py)."""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta  # deviation2: timezone import dropped with the utc-attach (unused)
from typing import Callable
from zoneinfo import ZoneInfo


class BmsError(RuntimeError):
    pass


@dataclass(frozen=True)
class BmsReading:
    ts: str | None
    min_cell_v: float | None
    max_cell_v: float | None
    charge_a: float | None
    soc: float | None
    remaining_ah: float | None
    pack_v: float | None = None        # LIVE pack voltage (owner doctrine2026-10-20)
    full_ah: float | None = None       # L2 wave1 (owner2026-10-25): capacity anchor for
                                        # the voltage-anchored energy model (interim BMS
                                        # figure — the learned band capacity supersedes)


@dataclass(frozen=True)
class BmsSample:
    ts: datetime
    min_cell_v: float | None
    pack_current: float | None
    remaining_ah: float | None
    soc_pct: float | None


def dig(obj: dict, path: str):
    cur = obj
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _minmax(cells: dict, fields: tuple[str, ...]) -> tuple[float | None, float | None]:
    vals = [cells[f] for f in fields if isinstance(cells.get(f), (int, float))]
    return (min(vals), max(vals)) if vals else (None, None)


def _wh(cur: float | None, volt: float) -> float:
    """One per-minute doc → watt-hours: U × I × dt with dt = 1/60 h,
    DISCHARGE SIDE ONLY — spec v4 §3 "energy management, not
    amp management". Grid-on (|I| ≤ solar_noise_a), charge (I ≥ 0) and
    missing current all contribute exactly 0 Wh."""
    if cur is None or cur >= 0:
        return 0.0
    return -cur * volt / 60.0


# Owner QA ruling 2026-10-21 (Aggregations-Doctrin): ALL energy integration
# happens IN THE CLUSTER — this scripted_metric is the verbatim mirror of
# gridguard/web_payload.py ENERGY_AGG/GG_SAMPLE_S (single source: the payload
# block moved verbatim there out of frontend/server.py); the only local step
# is the unit conversion × SAMPLE_S / 3.6e6 (per-minute ingestion cadence →
# kWh). Samples absent in a gap contribute 0 (no fabricated fill).
SAMPLE_S = 60.0            # one doc = one minute (mirror of frontend GG_SAMPLE_S)
ENERGY_AGG = {"e": {"scripted_metric": {
    "init_script": "state.sum = 0.0;",
    "map_script": ("if (doc['pack.current'].size() != 0 "
                   "&& doc['pack.voltage'].size() != 0) { state.sum += "
                   "doc['pack.current'].value * doc['pack.voltage'].value; }"),
    "combine_script": "return state.sum;",
    "reduce_script": ("double s = 0.0; for (x in states) { s += (double) x; } "
                      "return s;")}}}


class BmsSource:
    weather_index = "gridguard-weather-*"   # forecast-doc index (mirror of the
    # frontend GG_WEATHER literal; RealDeps may override from the cfg prefix)
    events_index = "gridguard-events-*"     # L1 per-class yield (owner2026-10-25):
    # §10b Decision-Index (mirror of cfg.events_index; RealDeps overrides from cfg)
    grid_switch_acts = ("switch_on", "switch_off")   # the labelled act docs that flip
    # the reconstructed grid state (failed/dry-run acts carry OTHER event names)

    def __init__(self, os_url: str, index: str, cell_fields: tuple[str, ...],
                 current_path: str, timeout: float = 8.0,
                 remaining_path: str = "soc.remaining_ah"):
        self.os_url, self.index, self.cell_fields = os_url, index, cell_fields
        self.current_path, self.timeout = current_path, timeout
        self.remaining_path = remaining_path
        self._search: Callable[[dict, str, str, float], dict] = _search

    def latest(self) -> BmsReading:
        resp = self._search({"size": 1, "sort": [{"@timestamp": "desc"}],
                             "query": {"match_all": {}},
                             "_source": ["@timestamp", "cells", "pack", "soc"]},
                            self.os_url, self.index, self.timeout)
        hits = resp.get("hits", {}).get("hits", [])
        if not hits:
            raise BmsError("no documents in index")
        src = hits[0]["_source"]
        lo, hi = _minmax(src.get("cells") or {}, self.cell_fields)
        return BmsReading(ts=src.get("@timestamp"), min_cell_v=lo, max_cell_v=hi,
                          charge_a=dig(src, self.current_path),
                          soc=dig(src, "soc.soc"),
                          remaining_ah=dig(src, self.remaining_path),
                          pack_v=dig(src, "pack.voltage"),
                          full_ah=dig(src, "soc.full_ah"))

    def samples_minutes(self, minutes: int) -> list[tuple[datetime, float]]:
        resp = self._search({"size": 300, "sort": [{"@timestamp": "asc"}],
                             "query": {"range": {"@timestamp": {"gte": f"now-{minutes}m"}}},
                             "_source": ["@timestamp", "cells"]},
                            self.os_url, self.index, self.timeout)
        out: list[tuple[datetime, float]] = []
        for hit in resp.get("hits", {}).get("hits", []):
            src = hit["_source"]
            lo, _ = _minmax(src.get("cells") or {}, self.cell_fields)
            ts_raw = src.get("@timestamp")
            if lo is None or not ts_raw:
                continue
            ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            # deviation2: brief attached tz=utc to naive timestamps; its own test
            # (and the interface "local-naive", plan line 931) expects naive kept
            # as-is — see task-7-report.
            out.append((ts, lo))
        out.sort(key=lambda p: p[0])   # deviation1: parse-layer ascending sort — see task-7-report
        return out

    def samples_window(self, minutes: int) -> list[BmsSample]:
        resp = self._search({"size": 600, "sort": [{"@timestamp": "asc"}],
                             "query": {"range": {"@timestamp": {"gte": f"now-{minutes}m"}}},
                             "_source": ["@timestamp", "cells", "pack", "soc"]},
                            self.os_url, self.index, self.timeout)
        out: list[BmsSample] = []
        for hit in resp.get("hits", {}).get("hits", []):
            src = hit["_source"]
            ts_raw = src.get("@timestamp")
            if not ts_raw:
                continue
            lo, _ = _minmax(src.get("cells") or {}, self.cell_fields)
            ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            out.append(BmsSample(
                ts=ts, min_cell_v=lo,
                pack_current=dig(src, self.current_path),
                remaining_ah=dig(src, self.remaining_path),
                soc_pct=dig(src, "soc.soc")))
        out.sort(key=lambda p: p.ts)
        return out

    def samples_days(self, days: int) -> list[BmsSample]:
        """v3 learner feed (spec R3''): rolling `days` window, `search_after`
        pagination (size 10000, sort @timestamp asc). Stops at a short page
        (natural end) or at the 15000-sample cap; a capped run prints ONE note
        to stderr. Pages arrive ascending, so concatenation preserves order."""
        page_size, cap = 10000, 15000
        out: list[BmsSample] = []
        after = None
        capped = False
        while True:
            body: dict = {"size": page_size, "sort": [{"@timestamp": "asc"}],
                          "query": {"range": {"@timestamp": {"gte": f"now-{days * 1440}m"}}},
                          "_source": ["@timestamp", "cells", "pack", "soc"]}
            if after is not None:
                body["search_after"] = after
            resp = self._search(body, self.os_url, self.index, self.timeout)
            hits = resp.get("hits", {}).get("hits", [])
            for hit in hits:
                src = hit["_source"]
                ts_raw = src.get("@timestamp")
                if not ts_raw:
                    continue
                lo, _ = _minmax(src.get("cells") or {}, self.cell_fields)
                ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                out.append(BmsSample(
                    ts=ts, min_cell_v=lo,
                    pack_current=dig(src, self.current_path),
                    remaining_ah=dig(src, self.remaining_path),
                    soc_pct=dig(src, "soc.soc")))
            if len(hits) < page_size:
                break                              # short page = natural end (no note)
            if len(out) >= cap:
                capped = True                      # full page AND >= cap → more may exist
                break
            after = hits[-1].get("sort")           # cursor = last hit's sort value
        if capped:
            print(f"[bms] samples_days: capped at {cap} samples "
                  f"(hist_days={days}) — older history dropped",
                  file=sys.stderr, flush=True)
        return out

    def night_buckets(self, days: int, tz_name: str, solar_noise_a: float,
                      window: tuple[float, float] | None = None, *,
                      spans: dict | None = None) -> list[dict]:
        """GridGuard v4 night buckets (spec v4 §3/§4): ONE Wh figure per night.

        Night = the LOCAL window [window_start_h, window_end_h) wrapping
        midnight (default 19.0 → 8.0, i.e. array_offset_h → array_onset_h —
        the `window` param exists now so callers can pass the learned edges
        later). Each bucket is labelled by its MORNING date (the date its
        window ends) and buckets never span regimes: one window, one bucket,
        classified as a whole.

        SPANS MODE (owner ruling2026-10-22 — "concept end-to-end"): when
        `spans` (the output of `periods.night_windows`) is given, a minute
        belongs to the solar-night window that CONTAINS it —
        span = (previous solar day's END, this solar day's START), half-open
        [start, end), labeled by the morning date — exactly the windows the
        history displays show; docs outside every span (daytime) are skipped.
        SHORT is then judged against the ACTUAL span length, not a clock
        window. The clock window above remains the fallback whenever no
        boundary data exists.

        Arithmetic location — documented deviation, controller-pre-approved:
        instead of one Painless aggregation, the per-minute docs are paged
        exactly like `samples_days` (search_after, size 10000, 15000-doc cap,
        `_source` reduced to @timestamp+pack) and integrated PER DOC in Python
        while paging (`_wh(cur, volt)` = the full U × I × dt, dt = 1/60 h),
        so every fetched minute counts at full granularity and the arithmetic
        stays tiny and pure. A single Painless one-shot agg over the window
        filter is the future optimization and would replace only this loop.

        Classification (§4, precedence order): OPEN (bucket's window end is
        still in the future — partial night) > SHORT (minutes < 0.6 × expected
        window minutes — "half the night ran" distorts the need downward) >
        GRID_ON (≥ 0.6 of minutes grid-on: |I| ≤ solar_noise_a) > MIXED
        (≥ 0.05 — regime change inside the window) > CLEAN. Only CLEAN feeds
        the need trend (`gridguard.learn.night_need_wh`).

        Returns chronological dicts with exactly the keys
        {night, wh, minutes, on_minutes, status}; empty list on no data,
        degrades quietly on missing pack/current/voltage fields."""
        tz = ZoneInfo(tz_name)
        start_h, end_h = window if window is not None else (19.0, 8.0)
        start_m = round(start_h * 60) % 1440
        end_m = round(end_h * 60) % 1440
        now = datetime.now(tz)
        # naive span boundaries = local wall time (same doctrine as
        # solar_day_boundaries); live night_windows spans arrive aware.
        span_list = sorted(
            (s if s.tzinfo is not None else s.replace(tzinfo=tz),
             e if e.tzinfo is not None else e.replace(tzinfo=tz), label)
            for label, (s, e) in (spans or {}).items())
        use_spans = bool(span_list)
        acc: dict = {}
        page_size, cap = 10000, 15000
        seen, after, capped = 0, None, False
        while True:
            body: dict = {"size": page_size, "sort": [{"@timestamp": "asc"}],
                          "query": {"range": {"@timestamp": {"gte": f"now-{days * 1440}m"}}},
                          "_source": ["@timestamp", "pack"]}
            if after is not None:
                body["search_after"] = after
            resp = self._search(body, self.os_url, self.index, self.timeout)
            hits = resp.get("hits", {}).get("hits", [])
            for hit in hits:                       # per-doc integration while paging
                src = hit.get("_source") or {}
                ts_raw = src.get("@timestamp")
                if not ts_raw:
                    continue
                ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                local = ts.astimezone(tz) if ts.tzinfo is not None \
                    else ts.replace(tzinfo=tz)     # naive stamps = local wall time
                label = None
                end_dt = None
                expected = 0
                if use_spans:
                    for s, e, lab in span_list:
                        if s <= local < e:          # half-open [start, end)
                            label, end_dt = lab, e
                            expected = (e - s).total_seconds() / 60.0
                            break
                    if label is None:
                        continue           # daytime docs / outside all nights
                else:
                    minute = local.hour * 60 + local.minute + local.second / 60.0
                    if start_m < end_m:
                        inside = start_m <= minute < end_m
                    else:
                        inside = minute >= start_m or minute < end_m
                    if not inside:
                        continue                       # day docs: outside the night
                    label = local.date() + (timedelta(days=1) if minute >= end_m
                                            else timedelta())
                    end_dt = datetime(label.year, label.month, label.day,
                                      end_m // 60, end_m % 60, tzinfo=tz)
                    expected = (end_h - start_h) % 24 * 60
                volt = dig(src, "pack.voltage")
                if not isinstance(volt, (int, float)):
                    continue   # owner doctrine 2026-10-20: no sensor value →
                               # NOT a measured minute (never invent voltage)
                cur = dig(src, self.current_path)
                if not isinstance(cur, (int, float)):
                    cur = None
                b = acc.get(label)
                if b is None:
                    b = acc[label] = {"wh": 0.0, "minutes": 0, "on_minutes": 0,
                                      "first_ts": local, "last_ts": local,
                                      "end_dt": end_dt, "expected": expected}
                b["wh"] += _wh(cur, volt)
                b["minutes"] += 1
                if cur is not None and abs(cur) <= solar_noise_a:
                    b["on_minutes"] += 1
                if local < b["first_ts"]:
                    b["first_ts"] = local
                if local > b["last_ts"]:
                    b["last_ts"] = local
            seen += len(hits)
            if len(hits) < page_size:
                break                              # short page = natural end
            if seen >= cap:
                capped = True
                break
            after = hits[-1].get("sort")           # cursor = last hit's sort value
        if capped:
            print(f"[bms] night_buckets: capped at {cap} docs "
                  f"(days={days}) — older history dropped",
                  file=sys.stderr, flush=True)
        # §4 classification in precedence order → chronological output
        out: list[dict] = []
        for night in sorted(acc):
            b = acc[night]
            minutes = b["minutes"]
            if b["end_dt"] > now:
                status = "OPEN"                    # window/span end in the future
            elif minutes < 0.6 * b["expected"]:
                status = "SHORT"
            elif b["on_minutes"] / minutes >= 0.6:
                status = "GRID_ON"
            elif b["on_minutes"] / minutes >= 0.05:
                status = "MIXED"
            else:
                status = "CLEAN"
            out.append({"night": night.isoformat(), "wh": b["wh"],
                        "minutes": minutes, "on_minutes": b["on_minutes"],
                        "status": status})
        return out

    def solar_day_boundaries(self, dates, tz_name: str, flow_a: float,
                             sun_fn) -> dict:
        """Cluster-side solar-day rule (owner2026-10-22 — MIRRORS
        periods.solar_day_bounds + the frontend day_bounds): per calendar
        date, the FIRST and LAST sample with pack.current >= flow_a between
        calendar sunrise and sunset (sun_fn(date) -> (rise, set) or None).

        AT MOST ONE pair per calendar day — a midday dip below the
        threshold cannot split the day (first..last span it) and no calendar
        day ever yields two; but a day can equally have NO pair when its
        window holds no sample >= flow_a (too little light). Several
        CONSECUTIVE calendar days may therefore lack a solar day — the next
        measured day's night SPANS all of them as ONE long solar night
        (periods.night_windows; pinned by
        test_night_windows_span_sunless_days_continuous). Missing sun
        times → no day (conservative, same as the frontend).
        Boundaries are size-1 filter+sort queries in the cluster
        (Aggregations-Doctrin) — no local scan of raw samples."""
        tz = ZoneInfo(tz_name)
        bounds: dict = {}
        for d in dates:
            try:
                sun = sun_fn(d)
            except Exception:
                sun = None
            if not sun or sun[0] is None or sun[1] is None:
                continue
            rise, set_ = sun
            # F5-1: open-meteo sun stamps are NAIVE local wall times — attach
            # the boundary tz, else the cluster reads zone-less ISO as UTC
            # and the whole sun window shifts by the local offset (~2 h).
            # Aware stamps pass through unchanged.
            if rise.tzinfo is None:
                rise = rise.replace(tzinfo=tz)
            if set_.tzinfo is None:
                set_ = set_.replace(tzinfo=tz)
            flt = [{"range": {"@timestamp": {"gte": rise.isoformat(),
                                             "lte": set_.isoformat()}}},
                   {"range": {self.current_path: {"gte": flow_a}}}]
            pair: dict = {}
            for direction in ("asc", "desc"):
                body = {"size": 1, "sort": [{"@timestamp": direction}],
                        "query": {"bool": {"filter": flt}},
                        "_source": ["@timestamp"]}
                hits = self._search(body, self.os_url,
                                    self.index, self.timeout
                                    ).get("hits", {}).get("hits", [])
                if hits:
                    raw = hits[0].get("_source", {}).get("@timestamp")
                    if raw:
                        t = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                        t = t.replace(tzinfo=tz) if t.tzinfo is None else t
                        pair[direction] = t
            if "asc" in pair and "desc" in pair and pair["asc"] <= pair["desc"]:
                bounds[d] = (pair["asc"], pair["desc"])
        return bounds

    def day_charge_kwh(self, start: datetime, end: datetime) -> float | None:
        """L1 measured yield (owner2026-10-22): ENERGY that reached the
        BATTERY during the solar day — cluster aggregation (Aggregations-
        Doctrin): Σ U×I over the window with a charge-only filter
        (pack.current >= 0), × SAMPLE_S → Wh → kWh. None on failure."""
        body = {"size": 0,
                "query": {"bool": {"filter": [
                    {"range": {"@timestamp": {"gte": start.isoformat(),
                                              "lt": end.isoformat()}}},
                    {"range": {self.current_path: {"gte": 0}}}]}},
                "aggs": ENERGY_AGG}
        try:
            resp = self._search(body, self.os_url, self.index, self.timeout)
        except Exception:
            return None
        raw = (resp.get("aggregations", {}).get("e", {}) or {}).get("value")
        if not isinstance(raw, (int, float)):
            return None
        return raw * SAMPLE_S / 3.6e6

    def forecast_doc_for(self, day, tz_name: str) -> dict | None:
        """The ex-ante forecast SHIPPED for `day`: the earliest non-null
        forecast doc of that LOCAL ship-day (morning_radiation_j uses
        forecast_days=1 → sums[0] = the ship day's own sum; a doc with
        local-ts on day D describes day D — confirmed live 2026-10-22:
        index gridguard-weather-YYYY.MM.DD, kind="forecast", ts local-offset,
        radiation_sum_j in J, null on error days). Queries the weather index
        on the SAME cluster (self.os_url); never raises (audit-dedup in
        RealDeps caches the honest None)."""
        tz = ZoneInfo(tz_name)
        lo = datetime(day.year, day.month, day.day, tzinfo=tz)
        hi = lo + timedelta(days=1)
        body = {"size": 1, "sort": [{"ts": "asc"}],
                "query": {"bool": {"filter": [
                    {"term": {"kind": "forecast"}},
                    {"range": {"ts": {"gte": lo.isoformat(),
                                      "lt": hi.isoformat()}}},
                    {"range": {"radiation_sum_j": {"gte": 0}}}]}}}
        try:
            hits = self._search(body, self.os_url, self.weather_index,
                                self.timeout).get("hits", {}).get("hits", [])
        except Exception:
            return None
        return hits[0].get("_source") if hits else None

    def day_grid_on_pct(self, start: datetime, end: datetime) -> float | None:
        """L1 per-class yield (owner ruling 2026-10-25): the grid-ON share of
        the window [start, end], reconstructed from the addon's OWN events-index
        labels — ONE bounded filter+sort query (size 50, Aggregations-Doctrin:
        no scan): act switch_on/switch_off + the state_observed journal.

        Reconstruction: a doc AT/BEFORE `start` seeds the OPENING state at
        start; every act / observed transition flips the state at its ts;
        `inputs.from` (when present) marks a state_observed as a TRANSITION —
        `inputs.to` is the resulting state either way. A stretch with no label
        yet is UNKNOWN and counts as NOT grid-ON (computed from what exists,
        never guessed). Result = ON minutes clipped to [start, end] / total
        minutes × 100. Query failure, no docs, no usable label or a
        degenerate window → None (honest unknown — never a fabricated 0)."""
        body = {"size": 50, "sort": [{"ts": "asc"}],
                "query": {"bool": {"filter": [
                    {"range": {"ts": {"gte": start.isoformat(),
                                      "lte": end.isoformat()}}},
                    {"bool": {"should": [
                        {"bool": {"filter": [
                            {"term": {"event": "act"}},
                            {"terms": {"action.type":
                                       list(self.grid_switch_acts)}}]}},
                        {"term": {"event": "state_observed"}}],
                        "minimum_should_match": 1}}]}},
                "_source": ["ts", "event", "action", "inputs"]}
        try:
            hits = self._search(body, self.os_url, self.events_index,
                                self.timeout).get("hits", {}).get("hits", [])
        except Exception:
            return None
        if not hits:
            return None
        total = (end - start).total_seconds() / 60.0
        if total <= 0:
            return None
        flips: list[tuple[datetime, str]] = []
        for hit in hits:
            src = hit.get("_source") or {}
            raw = src.get("ts")
            if not raw:
                continue
            ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=start.tzinfo)   # naive = local wall time (F5)
            new = None
            if src.get("event") == "state_observed":
                inp = src.get("inputs") or {}
                if isinstance(inp.get("to"), str):
                    new = inp["to"]
            elif (src.get("action") or {}).get("type") in self.grid_switch_acts:
                new = "on" if src["action"]["type"] == "switch_on" else "off"
            if new in ("on", "off"):
                flips.append((ts, new))
        if not flips:
            return None                    # docs present, nothing usable → honest None
        flips.sort(key=lambda p: p[0])
        on_min, state, cur = 0.0, None, start
        for ts, new in flips:
            if ts <= cur:                  # at/before the window head → seeds it
                state = new
                continue
            if state == "on":
                on_min += (min(ts, end) - cur).total_seconds() / 60.0
            if ts >= end:                  # beyond the window → nothing after it
                break
            state, cur = new, ts
        if state == "on":
            on_min += (end - cur).total_seconds() / 60.0
        return on_min / total * 100.0

    def pack_v_avg(self, minutes: int) -> float | None:
        """Owner physics ruling 2026-10-26 (§20): the Wh-conversion voltage for
        the eve's available is a cheap AVERAGE over the recent `minutes` — ONE
        cluster-side avg aggregation (Aggregations-Doctrin: no local scan, same
        home as day_charge_kwh); honest None on any failure or when no doc in
        the window carries a pack voltage (callers fall back to the LIVE pack_v)."""
        body = {"size": 0,
                "query": {"range": {"@timestamp": {"gte": f"now-{int(minutes)}m"}}},
                "aggs": {"v": {"avg": {"field": "pack.voltage"}}}}
        try:
            resp = self._search(body, self.os_url, self.index, self.timeout)
        except Exception:
            return None
        raw = (resp.get("aggregations", {}).get("v", {}) or {}).get("value")
        return float(raw) if isinstance(raw, (int, float)) else None

    def reference_events(self, since: datetime) -> list[dict]:
        """Owner REFERENCE-LOAD model (Phase A): the addon's OWN ok-confirmed
        switch moments since `since` — the same journal events the payload's
        announced/cancel queries read (grid switch acts, ensure_on program
        generations with their fire_str, the disable/clear cancels). One
        bounded filter+sort query (Aggregations-Doctrine: no scan).

        SORT DESC + size 200 is a SAFETY choice: a capped result must never
        lose the RECENT moments (a dropped fire-moment would extend an OFF
        window OVER a grid-on span and dilute the reference load — the
        over-drain direction). Losing the OLDEST seed instead drops that
        night's clip → honest median fallback (the conservative direction);
        grid_off_windows is order-independent either way. Raises BmsError on
        cluster failure — the caller nets it to that same fallback."""
        body = {"size": 200, "sort": [{"ts": "desc"}],
                "query": {"bool": {"filter": [
                    {"term": {"event": "act"}},
                    {"terms": {"action.type": list(self.grid_switch_acts)
                               + ["failsafe_timer", "disable_on_timers",
                                  "clear_grid_timers"]}},
                    {"term": {"result": "ok"}},
                    {"range": {"ts": {"gte": since.isoformat()}}}]}},
                "_source": ["ts", "event", "action", "timer_action", "result",
                            "inputs"]}
        resp = self._search(body, self.os_url, self.events_index, self.timeout)
        return [h["_source"] for h in resp.get("hits", {}).get("hits", [])]

    def yield_history(self, size: int = 10) -> list[dict]:
        """Phase C input (own measurements): the newest `size` yield_learned
        events ({ts, inputs: day, measured_kwh, forecast_kwh_m2, factor, …}) —
        the learned forecast→measured pairs the solar-day prediction takes its
        plain median factor from. Same bounded newest-of-kind query the
        payload's yield_hist already uses (Aggregations-Doctrine: no scan);
        raises BmsError on cluster failure — the caller nets it to the honest
        'insufficient' basis."""
        body = {"size": size, "sort": [{"ts": "desc"}],
                "query": {"term": {"event": "yield_learned"}},
                "_source": ["ts", "inputs"]}
        resp = self._search(body, self.os_url, self.events_index, self.timeout)
        return [h["_source"] for h in resp.get("hits", {}).get("hits", [])]

    def counter_series(self, start: datetime, end: datetime) -> list:
        """Owner REFERENCE-LOAD model (Phase A): per-minute BMS counter docs
        `(ts aware, remaining_ah, pack voltage)` over [start, end] — the clip
        integration feed (ΔAh × V_mean per segment). Paged like samples_days
        (search_after, size 10000, 15000-doc cap ≫ the default 5-night span);
        docs missing the counter or the voltage keep their slot as None — the
        reference layer treats unknown evidence as 'no clip', never as zero."""
        page_size, cap = 10000, 15000
        out: list = []
        after = None
        while True:
            body: dict = {"size": page_size, "sort": [{"@timestamp": "asc"}],
                          "query": {"range": {"@timestamp": {
                              "gte": start.isoformat(),
                              "lte": end.isoformat()}}},
                          "_source": ["@timestamp", self.remaining_path,
                                      "pack.voltage"]}
            if after is not None:
                body["search_after"] = after
            resp = self._search(body, self.os_url, self.index, self.timeout)
            hits = resp.get("hits", {}).get("hits", [])
            for hit in hits:
                src = hit.get("_source") or {}
                ts_raw = src.get("@timestamp")
                if not ts_raw:
                    continue
                ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                out.append((ts, dig(src, self.remaining_path),
                            dig(src, "pack.voltage")))
            if len(hits) < page_size:
                break                          # short page = natural end
            if len(out) >= cap:
                break                          # documented cap (samples_days rule)
            after = hits[-1].get("sort")
        return out

    def latest_reserve_tier(self) -> dict | None:
        """Restart-window safety (owner incident 2026-10-25 09:37:39 UTC): the
        NEWEST reserve_tier decision's `inputs` dict from the addon's OWN
        events index (§10b) — ONE bounded query (size 1, sort ts desc,
        aggregation doctrine: no scan), same home as day_grid_on_pct. The
        decision IS durable in the index; only the in-memory latch forgets it
        on every restart — the run() seed reloads it before the first tick.
        Return = inputs dict of the newest doc; honest None on query error,
        no docs or no inputs — never a fabricated 0."""
        body = {"size": 1, "sort": [{"ts": "desc"}],
                "query": {"bool": {"filter": [
                    {"term": {"event": "reserve_tier"}}]}},
                "_source": ["ts", "inputs"]}
        try:
            hits = self._search(body, self.os_url, self.events_index,
                                self.timeout).get("hits", {}).get("hits", [])
        except Exception:
            return None
        if not hits:
            return None
        inputs = (hits[0].get("_source") or {}).get("inputs")
        return inputs if isinstance(inputs, dict) else None

    def phase_evidence(self, since_iso: str) -> list[dict]:
        """Durable evidence for the day-scoped phase flags (restart seed).
        Events: grid_off (morning/eve OFF writes morning_done), night_over."""
        body = {"size": 50, "sort": [{"ts": "desc"}],
                "query": {"bool": {"filter": [
                    {"terms": {"event": ["grid_off", "night_over"]}},
                    {"range": {"ts": {"gte": since_iso}}}]}},
                "_source": ["ts", "event", "result"]}
        res = self._search(body, self.os_url, self.events_index, self.timeout)
        return [h["_source"] for h in res.get("hits", {}).get("hits", [])]


def _search(body: dict, os_url: str, index: str, timeout: float) -> dict:
    req = urllib.request.Request(f"{os_url}/{index}/_search",
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise BmsError(f"opensearch unreachable: {e}") from e
    except json.JSONDecodeError as e:
        raise BmsError(f"opensearch bad response: {e}") from e
