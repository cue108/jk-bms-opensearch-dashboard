"""spec §10b Decision-Index dual-write — every jsonl audit record is ALSO
best-effort POSTed to {os_url}/{EVENTS_INDEX_PREFIX}-{YYYY.MM.DD}/_doc with the
same (R7-clean) payload + additive `dry_run`.

Pins: dated index (local date of the record's ts in cfg.tz), kill-switch
(EVENTS_INDEX=0 → jsonl-only), jsonl FIRST + unconditional (a failing POST
never eats the line, never raises into the loop), one throttled stderr line
per hour, and the RealDeps flow (start + push → same single writer)."""
import json
from datetime import date, datetime, timezone
from urllib.error import URLError
from zoneinfo import ZoneInfo

import pytest

import gridguard.audit as audit_mod
from gridguard.audit import AuditLog
from gridguard.config import load_config
from gridguard.main import RealDeps

MADRID = ZoneInfo("Europe/Madrid")
OS_URL = "http://127.0.0.1:9205"
FIXED = datetime(2026, 10, 19, 12, 0, 0, tzinfo=timezone.utc)   # 14:00 CEST → same local day

GOOD = {"TZ": "Europe/Madrid", "WINDOW_START": "09:00", "WINDOW_END": "22:00",
        "EVE_START": "20:00", "OPENSEARCH_URL": OS_URL, "BMS_INDEX": "i",
        "EWCLOUD_EMAIL": "a", "EWCLOUD_PASS": "b", "SW_MAIN_DEVICEID": "d",
        "SW_MAIN_DEVICEKEY": "k", "SW_MAIN_IP": "1.2.3.4",
        "NTFY_WEBHOOK": "https://n/t", "DRY_RUN": "1"}


@pytest.fixture(autouse=True)
def fresh_throttle(monkeypatch):
    """module-level stderr throttle starts clean per test (≥1h rule)."""
    monkeypatch.setattr(audit_mod, "_last_index_err", None, raising=False)


def _mklog(tmp_path, **kw):
    """AuditLog + fake HTTP sink (bms.BmsSource._search injection style)."""
    kw.setdefault("os_url", OS_URL)
    kw.setdefault("tz", MADRID)
    log = AuditLog(tmp_path / "gridguard.jsonl", **kw)
    posts: list = []
    log._post = lambda url, body, timeout: posts.append((url, body, timeout))
    log._now = lambda: FIXED
    return log, posts


def _lines(tmp_path):
    return (tmp_path / "gridguard.jsonl").read_text().splitlines()


def test_dual_write_posts_dated_index_with_record_and_dry_run(tmp_path):
    log, posts = _mklog(tmp_path, dry_run=True)
    log.record("tick", phase="eve_plan", verdict="safe_clear")
    assert len(posts) == 1
    url, body, timeout = posts[0]
    # 2026-10-19T12:00Z = 14:00 Madrid → local date stays the 19th
    assert url == f"{OS_URL}/gridguard-events-2026.10.19/_doc"
    assert timeout == 3.0                                    # short stdlib timeout
    doc = json.loads(body)
    assert doc["ts"] == "2026-10-19T12:00:00+00:00"
    assert doc["event"] == "tick" and doc["phase"] == "eve_plan"
    assert doc["verdict"] == "safe_clear" and doc["dry_run"] is True
    # jsonl payload unchanged — dry_run is an index-only additive field (R7)
    assert json.loads(_lines(tmp_path)[-1]) == {
        "ts": "2026-10-19T12:00:00+00:00", "event": "tick",
        "phase": "eve_plan", "verdict": "safe_clear"}


def test_kill_switch_off_is_jsonl_only(tmp_path):
    log, posts = _mklog(tmp_path, events_index_enabled=False)
    log.record("act", action={"type": "switch_on"}, result="ok")
    assert posts == []                                       # NO POST
    assert json.loads(_lines(tmp_path)[-1])["event"] == "act"  # jsonl still written


def test_bare_auditlog_without_target_never_posts(tmp_path):
    # pre-existing call shape (tests/tools): path only → jsonl-only by construction
    log = AuditLog(tmp_path / "a.jsonl")
    hits: list = []
    log._post = lambda *a: hits.append(a)
    log.record("tick", phase="night")
    assert hits == [] and len((tmp_path / "a.jsonl").read_text().splitlines()) == 1


def test_sink_failure_never_raises_and_jsonl_landed_first(tmp_path):
    log, _ = _mklog(tmp_path)
    seen: dict = {}

    def failing_post(url, body, timeout):
        seen["lines"] = _lines(tmp_path)          # jsonl state AT post time
        raise URLError("cluster down")

    log._post = failing_post
    log.record("read_state_failed", result="timed out")   # must NOT raise into the loop
    assert len(seen["lines"]) == 1                        # jsonl FIRST: line was already there
    assert json.loads(seen["lines"][-1])["event"] == "read_state_failed"
    assert len(_lines(tmp_path)) == 1                     # …and it survives the failure


def test_post_errors_throttle_to_one_stderr_line_per_hour(tmp_path, capsys):
    log, _ = _mklog(tmp_path)

    def failing_post(url, body, timeout):
        raise URLError("cluster down")

    log._post = failing_post
    log.record("tick", phase="night")
    log.record("tick", phase="night")
    err = capsys.readouterr().err
    assert err.count("[audit-index]") == 1                # ONE throttled line ≥1h apart
    assert "cluster down" in err
    assert len(_lines(tmp_path)) == 2                     # audit stays full fidelity


def test_utc_ts_is_tomorrow_in_local_tz_index_uses_local_date(tmp_path):
    from gridguard.audit import _local_date   # wished-for helper (spec §10b date math)
    # PIN (date math): 2026-10-19T23:30Z = 2026-10-20 01:30 CEST (Madrid DST
    # runs to 2026-10-25) → the doc lands in the LOCAL day: …-2026.10.20/_doc
    assert _local_date("2026-10-19T23:30:00+00:00", MADRID) == date(2026, 10, 20)
    # naive ts = local wall-clock by contract → stays on the 19th
    assert _local_date("2026-10-19T23:30:00", MADRID) == date(2026, 10, 19)
    # same pin through the whole writer path
    log, posts = _mklog(tmp_path)
    log._now = lambda: datetime(2026, 10, 19, 23, 30, 0, tzinfo=timezone.utc)
    log.record("night_need", inputs={"need_wh": 800})
    assert posts[0][0].endswith("/gridguard-events-2026.10.20/_doc")


def test_realdeps_start_and_push_flow_into_decision_index(tmp_path, monkeypatch):
    """§2: start + push need NO new call site — they flow through the one writer."""
    class Stub:
        def __init__(self, *a, **k): pass

    monkeypatch.setattr("gridguard.main.CloudClient", Stub)
    monkeypatch.setattr("gridguard.main.LanClient", Stub)
    monkeypatch.setattr("gridguard.main.BmsSource", Stub)
    monkeypatch.setattr("gridguard.main.ntfy_push", lambda *a, **k: True)
    cfg = load_config({**GOOD, "LOG_DIR": str(tmp_path)})
    deps = RealDeps(cfg)
    assert deps.audit_log.os_url == OS_URL                 # cfg wiring
    assert deps.audit_log.events_index == "gridguard-events"
    posts: list = []
    deps.audit_log._post = lambda url, body, timeout: posts.append((url, body, timeout))
    deps.audit("start", result="dry_run=True")             # run()'s first record
    deps.push("GRID ON", "reason: night need 800 Wh")            # §9.7 push → same writer
    events = [json.loads(b)["event"] for _, b, _ in posts]
    assert events == ["start", "push"]
    assert all(u.startswith(f"{OS_URL}/gridguard-events-") for u, _, _ in posts)
    assert json.loads(posts[1][1])["dry_run"] is True      # cfg.dry_run flows
    # dual = both truths: the same two records are in the jsonl
    assert [json.loads(x)["event"] for x in _lines(tmp_path)] == ["start", "push"]


def test_record_echoes_each_event_to_stdout_tee_style(tmp_path, capsys):
    """Owner2026-10-21: OpenSearch stays THE one source (index = query truth,
    jsonl = robustness truth); the console additionally SHOWS every audit record
    like a `tee`, so `docker logs` reveals what the loop decided instead of a
    silent container. The echo is purely additive — the record and the dual-write
    are untouched."""
    log, posts = _mklog(tmp_path)
    log.record("tick", phase="morning")
    log.record("grid_off", inputs={"ground": "drain", "remaining_ah": 125.5})
    out = capsys.readouterr().out
    assert "tick" in out and "morning" in out
    assert "grid_off" in out and "drain" in out


def test_index_write_failure_still_echoes_and_still_raises_nothing(tmp_path, capsys):
    """A failing dual-write never eats the loop: the throttled stderr note stays,
    the jsonl line stays, AND the stdout echo stays (console shows the attempt)."""
    log, _ = _mklog(tmp_path)
    log._post = lambda url, body, timeout: (_ for _ in ()).throw(URLError("down"))
    log.record("tick", phase="night")                       # must not raise
    cap = capsys.readouterr()
    assert "tick" in cap.out                                # console saw the event
    assert "[audit-index] dual-write failed" in cap.err     # throttled stderr note
    assert json.loads(_lines(tmp_path)[0])["event"] == "tick"  # jsonl robustness truth
