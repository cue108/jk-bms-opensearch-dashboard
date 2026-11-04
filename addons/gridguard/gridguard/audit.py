# gridguard/audit.py
"""JSONL journal — whitelist-enforced so secrets structurally cannot land (R7).

v4 §10b Decision-Index dual-write: the SAME record is additionally POSTed
(best-effort, never raising into the loop) as a doc into
`{os_url}/{EVENTS_INDEX_PREFIX}-{YYYY.MM.DD}/_doc`. The jsonl write comes
FIRST and is unconditional — local truth always lands even when the cluster
is down; the index is the query truth, the jsonl stays the robustness truth.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable

ALLOWED = {"phase", "verdict", "inputs", "action", "channel", "result", "sequence", "value",
           "inventory", "timer_action", "kind"}

INDEX_TIMEOUT_S = 3.0         # §10b: same stdlib-HTTP way as bms._search, short timeout
_INDEX_THROTTLE_S = 3600.0    # ≥1h between dual-write error lines (main.py hotfix pattern)
_last_index_err: float | None = None   # module-level: ONE throttled stderr line per hour


def _post(url: str, body: bytes, timeout: float) -> None:
    """One doc into the Decision-Index — stdlib only, no auth (local cluster open)."""
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        r.read()


def _local_date(ts: str, tz) -> date:
    """Index day = LOCAL date of the record's ts in `tz` (spec §10b).

    Record ts is a UTC ISO string → converted with `astimezone(tz)`;
    a NAIVE ts is taken as local wall-clock time as-is (documented contract)."""
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is not None:
        dt = dt.astimezone(tz)
    return dt.date()


def _report_index_err(e: Exception) -> None:
    """Throttled stderr line (≥1h apart) — the loop must never see this error."""
    global _last_index_err
    now = time.monotonic()
    if _last_index_err is None or now - _last_index_err >= _INDEX_THROTTLE_S:
        _last_index_err = now
        print(f"[audit-index] dual-write failed: {e}", file=sys.stderr, flush=True)


class AuditLog:
    def __init__(self, path: Path, *, os_url: str | None = None, tz=None,
                 dry_run: bool = False, events_index: str = "gridguard-events",
                 events_index_enabled: bool = True):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        # §10b dual-write target: os_url=None (path-only construction) = jsonl-only
        self.os_url = os_url
        self.tz = timezone.utc if tz is None else tz
        self.dry_run = dry_run
        self.events_index = events_index
        self.events_index_enabled = events_index_enabled
        # injection points, same style as bms.BmsSource._search (tests replace these)
        self._post: Callable[[str, bytes, float], None] = _post
        self._now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)

    def record(self, event: str, **fields) -> None:
        unknown = set(fields) - ALLOWED
        if unknown:
            raise ValueError(f"audit field not allowed: {sorted(unknown)}")
        rec = {"ts": self._now().isoformat(timespec="seconds"), "event": event}
        rec.update(fields)
        with self.path.open("a", encoding="utf-8") as f:   # jsonl FIRST, unconditional
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self._index_write(rec)               # §10b advisory — may fail, may never raise
        print(json.dumps(rec, ensure_ascii=False), flush=True)   # tee-style console echo —
        # owner2026-10-21: index/jsonl stay THE one source; this only SHOWS the same
        # record on stdout so `docker logs` reveals the loop's decisions (never a secret,
        # rec is R7-whitelist-clean before this point).

    def _index_write(self, rec: dict) -> None:
        """Dual-write one record to `{prefix}-{YYYY.MM.DD}/_doc` (spec §10b).
        Kill-switch off or no target → no-op; ANY failure → throttled stderr only."""
        if not self.events_index_enabled or not self.os_url:
            return
        try:
            day = _local_date(rec["ts"], self.tz).strftime("%Y.%m.%d")
            doc = {**rec, "dry_run": bool(self.dry_run)}  # additive: jsonl stays R7-identical
            self._post(f"{self.os_url}/{self.events_index}-{day}/_doc",
                       json.dumps(doc, ensure_ascii=False).encode("utf-8"),
                       INDEX_TIMEOUT_S)
        except Exception as e:                 # noqa: BLE001 — never disturb the loop
            _report_index_err(e)
