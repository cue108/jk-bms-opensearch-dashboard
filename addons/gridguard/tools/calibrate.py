#!/usr/bin/env python3
"""Print per-day morning (09:00-12:00 local) max charge current from the BMS index
plus the v2 capacity columns (min remaining_ah reached over the day, hours at/below
AH_ON), merged with open-meteo daily shortwave sums (past_days=14). Human picks gates."""
import json, os, sys, urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo(os.environ.get("TZ", "Europe/Madrid"))
OS = os.environ["OPENSEARCH_URL"].rstrip("/")
INDEX = os.environ["BMS_INDEX"]
REM_PATH = os.environ.get("BMS_REMAINING_AH_PATH", "soc.remaining_ah") or "soc.remaining_ah"
try:
    AH_ON = float(os.environ.get("AH_ON", "5") or "5")   # v2 default 5 Ah (config.py)
except ValueError:
    sys.exit("calibrate: AH_ON must be a number")


def os_search(body):
    req = urllib.request.Request(f"{OS}/{INDEX}/_search", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def dig(obj, path):
    cur = obj
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def main():
    query = {"range": {"@timestamp": {"gte": "now-14d"}}}
    days = {}
    after, n_docs, capped = None, 0, False
    for _ in range(50):  # hard safety cap: 50 pages x 10000 (+ stop at 100k docs)
        body = {"size": 10000, "sort": [{"@timestamp": "asc"}], "query": query,
                "_source": ["@timestamp", "pack.current", REM_PATH]}
        if after is not None:
            body["search_after"] = [after]
        hits = os_search(body)["hits"]["hits"]
        for hit in hits:
            src = hit["_source"]
            ts = datetime.fromisoformat(src["@timestamp"].replace("Z", "+00:00")).astimezone(TZ)
            d = days.setdefault(ts.date().isoformat(),
                                {"max_a": None, "pos_a": 0.0, "min_rem": None, "n_below": 0})
            rem = dig(src, REM_PATH)                     # v2: full-day capacity stats
            if rem is not None:
                if d["min_rem"] is None or rem < d["min_rem"]:
                    d["min_rem"] = rem
                if rem <= AH_ON:
                    d["n_below"] += 1
            cur = (src.get("pack") or {}).get("current")
            if cur is None or not (9 <= ts.hour < 12):
                continue
            d["max_a"] = cur if d["max_a"] is None else max(d["max_a"], cur)
            if cur > 0:
                d["pos_a"] = max(d["pos_a"], cur)
        n_docs += len(hits)
        if len(hits) < 10000:
            break
        after = hits[-1]["sort"][0]
        if n_docs >= 100_000:
            capped = True
            break
    else:
        capped = True
    if capped:
        print(f"calibrate: safety cap hit after {n_docs} docs — data truncated, "
              "percentiles may be incomplete", file=sys.stderr)
    lat, lon = os.environ.get("WEATHER_LAT"), os.environ.get("WEATHER_LON")
    rad = {}
    if lat and lon:
        url = (f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
               f"&daily=shortwave_radiation_sum&timezone=auto&past_days=14&forecast_days=1")
        with urllib.request.urlopen(url, timeout=20) as r:
            daily = json.loads(r.read().decode())["daily"]
        rad = {d: v for d, v in zip(daily["time"], daily["shortwave_radiation_sum"]) if v is not None}
    print(f"{'date':10s} {'maxChargeA':>10s} {'maxPosA':>8s} {'minRemAh':>9s} "
          f"{'h<AH_ON':>7s} {'radiationMJ/m2':>14s}")
    for d in sorted(days):
        v = days[d]
        r = rad.get(d)
        maxa = " " * 10 if v["max_a"] is None else f"{v['max_a']:10.2f}"
        posa = " " * 8 if v["pos_a"] <= 0 else f"{v['pos_a']:8.2f}"
        minr = " " * 9 if v["min_rem"] is None else f"{v['min_rem']:9.2f}"
        hrs = f"{v['n_below'] / 60.0:7.1f}"              # per-minute docs at/below AH_ON
        print(f"{d:10s} {maxa} {posa} {minr} {hrs} {'' if r is None else f'{r:8.2f}'}")
    vals = [v["pos_a"] for v in days.values() if v["pos_a"] > 0]
    if vals:
        vals.sort()
        print(f"\nclear-morning positive-charge p25/p50/p75: "
              f"{vals[len(vals)//4]:.2f} / {vals[len(vals)//2]:.2f} / {vals[3*len(vals)//4]:.2f} A")
    print("v2 columns inform the calibratable picks AH_ON/AH_OFF/SOLAR_HOLD_S/SOLAR_NOISE_A "
          "(h<AH_ON = per-minute docs ≤ AH_ON ÷ 60); WEATHER_MIN_J in J/m² "
          "(e.g.3_000_000 for a weak day), leave weather empty to disable")


if __name__ == "__main__":
    main()
