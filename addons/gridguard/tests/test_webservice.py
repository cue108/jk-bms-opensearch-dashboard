# addons/gridguard/tests/test_webservice.py
import json
from datetime import datetime, timedelta, timezone
from unittest import mock

from gridguard import webservice

NOW = datetime.now(timezone.utc)


def _tick_event(age_s):
    ts = (NOW - timedelta(seconds=age_s)).isoformat(timespec="seconds")
    return {"hits": {"hits": [{"_source": {"ts": ts, "phase": "day_hold"}}]}}


def _health(events_result):
    with mock.patch("gridguard.webservice._search", return_value=events_result):
        return webservice.health_payload("http://stub:9200", "gridguard-events")


def test_health_self_description_shape():
    h = _health(_tick_event(10))
    assert h["id"] == "gridguard"
    assert h["title"] == "Energie Management"          # verbatim (spec §3.2)
    assert h["order"] == 20
    assert h["contract_version"] == 1
    assert h["version"] == webservice.VERSION
    assert h["hashes"] == ["grid"]


def test_fresh_rule_is_exact_pill_parity():
    # awake cadence 600 → bound 2*600 = 1200 s (JS: a<=2*cad ⇒ non-dead)
    assert _health(_tick_event(1100))["fresh"] is True
    assert _health(_tick_event(1300))["fresh"] is False
    # night cadence 1800 → bound 3600 s (phase comes from the tick doc itself)
    night = {"hits": {"hits": [{"_source": {
        "ts": (NOW - timedelta(seconds=3500)).isoformat(timespec="seconds"),
        "phase": "night"}}]}}
    with mock.patch("gridguard.webservice._search", return_value=night):
        assert webservice.health_payload("http://x", "gridguard-events")["fresh"] is True


def test_no_tick_reports_honest_dead():
    h = _health({"hits": {"hits": []}})
    assert h["fresh"] is False and h["age_s"] is None and h["newest_ts"] is None


def _serve(os_url="http://stub:9200", events_index="gridguard-events"):
    srv = webservice.create_server("127.0.0.1", 0, os_url, events_index)
    import threading; threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def test_page_and_payload_routes():
    from gridguard.web_page import PAGE
    srv, base = _serve()
    try:
        import urllib.request
        with urllib.request.urlopen(base + "/", timeout=5) as r:
            assert r.status == 200 and r.read().decode() == PAGE
        with mock.patch("gridguard.webservice.fetch_gridguard",
                        return_value={"ok": True}):
            with urllib.request.urlopen(base + "/api/gridguard", timeout=5) as r:
                assert json.loads(r.read()) == {"ok": True}
        with mock.patch("gridguard.webservice.fetch_gridguard",
                        side_effect=RuntimeError("cluster down")):
            import urllib.error
            try:
                urllib.request.urlopen(base + "/api/gridguard", timeout=5)
                raise AssertionError("expected 502")
            except urllib.error.HTTPError as e:
                assert e.code == 502 and b"error" in e.read()
    finally:
        srv.shutdown()


def test_unknown_route_404():
    srv, base = _serve()
    try:
        import urllib.request, urllib.error
        try:
            urllib.request.urlopen(base + "/nope", timeout=5)
            raise AssertionError("expected 404")
        except urllib.error.HTTPError as e:
            assert e.code == 404
    finally:
        srv.shutdown()


def test_start_is_crash_netted_and_daemon():
    with mock.patch.object(webservice, "create_server",
                           side_effect=OSError("port taken")):
        assert webservice.start(None) is None          # must not raise
