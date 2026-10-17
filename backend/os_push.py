"""OpenSearch indexing for the JK BMS state — library + standalone CLI.

push_state() is normally called by collector.py (one document per minute,
aligned to the minute). The module also runs standalone for cron-style
setups: it reads the collector's /api/state endpoint.

    INDEX_PREFIX-YYYY.MM.DD    daily index, date computed in INDEX_TZ

Stale snapshots are indexed too (flagged "stale": true) so gaps stay visible
in OpenSearch instead of silently disappearing.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

STATE_URL = os.environ.get("STATE_URL", "http://127.0.0.1:8099/api/state")
OS_URL = os.environ.get("OPENSEARCH_URL", "http://127.0.0.1:9200").rstrip("/")
INDEX_PREFIX = os.environ.get("INDEX_PREFIX", "jkbms")
INDEX_TZ = os.environ.get("INDEX_TZ", "UTC")
STALE_AFTER_S = 120

def http_json(url: str, payload: dict | None = None, method: str | None = None) -> dict:
    if method is None:
        method = "POST" if payload is not None else "GET"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


def ensure_index(index: str, os_url: str) -> None:
    try:
        http_json(f"{os_url}/{index}")
    except urllib.error.HTTPError as err:
        if err.code != 404:
            raise
        http_json(f"{os_url}/{index}", {
            "settings": {"number_of_shards": 1, "number_of_replicas": 0},
            "mappings": {"properties": {"@timestamp": {"type": "date"}}},
        }, method="PUT")


def push_state(state: dict, *, os_url: str = OS_URL,
               index_prefix: str = INDEX_PREFIX, tz_name: str = INDEX_TZ) -> int:
    """Index one state snapshot; returns 0 on success (raises on failure)."""
    tz = ZoneInfo(tz_name)
    now = datetime.now(timezone.utc)

    live = state.get("live", {})
    age = live.get("last_frame_age_s")
    stale = age is None or age > STALE_AFTER_S

    doc = {
        "@timestamp": now.isoformat(timespec="seconds"),
        "stale": stale,
        "streaming": bool(live.get("streaming")),
        "status": live.get("status"),
        "frames_total": live.get("frames_total"),
        "frames_cell_info": live.get("frames_cell_info"),
    }
    for section in ("device", "cells", "pack", "soc", "temps", "status"):
        val = state.get(section)
        if isinstance(val, dict):
            doc[section] = val

    # --- index-shape transforms (time-series friendly) -------------------
    # Static values stay out of the time series: balance-wire resistances
    # never change -> live view only, not indexed.
    cells = doc.get("cells") or {}
    cells.pop("resistances", None)
    volts = cells.pop("voltages", None) or []
    # one field per cell -> enables per-cell line graphs & bad-cell queries
    for i, v in enumerate(volts, start=1):
        if v and v > 0.5:
            cells[f"v{i}"] = v

    # --- derived fields (BMS-knowledge conjunction) ----------------------
    live_vals = [v for v in volts if v and v > 0.5]
    if len(live_vals) >= 2:
        avg = sum(live_vals) / len(live_vals)
        devs = [(abs(v - avg), i + 1) for i, v in enumerate(volts) if v and v > 0.5]
        worst_mv, worst_cell = max(devs)
        cells["delta_mv"] = round((max(live_vals) - min(live_vals)) * 1000, 1)
        cells["worst_cell"] = worst_cell
        cells["worst_deviation_mv"] = round(worst_mv * 1000, 1)

    pack = doc.get("pack") or {}
    soc = doc.get("soc") or {}
    full = soc.get("full_ah") or 0
    cur = pack.get("current")
    if cur is not None and full > 0:
        pack["charge_rate_c"] = round(cur / full, 3)  # C-rate: +charge / -discharge
    if cur is not None and cur < -0.2 and soc.get("remaining_ah") is not None:
        soc["est_runtime_h"] = round(soc["remaining_ah"] / -cur, 2)

    index = f"{index_prefix}-{now.astimezone(tz).strftime('%Y.%m.%d')}"
    ensure_index(index, os_url)
    http_json(f"{os_url}/{index}/_doc", doc)
    return 0


def main() -> int:
    """CLI: fetch the collector state over HTTP and index it (cron mode)."""
    now = datetime.now(timezone.utc)
    try:
        state = http_json(STATE_URL)
    except Exception as err:
        print(f"[{now.isoformat(timespec='seconds')}] collector unreachable: {err}",
              file=sys.stderr)
        return 1
    try:
        push_state(state)
    except Exception as err:
        print(f"[{now.isoformat(timespec='seconds')}] opensearch error: {err}",
              file=sys.stderr)
        return 2
    print(f"[{now.isoformat(timespec='seconds')}] indexed soc="
          f"{state.get('soc', {}).get('soc')} streaming={state.get('live', {}).get('streaming')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
