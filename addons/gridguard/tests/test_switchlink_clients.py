"""Unit tests for switchlink part2 clients + Task5 review-minor fixes.

All network I/O is monkeypatched — no real device or cloud calls ever happen.
"""
import base64
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path

import pytest
from gridguard.switchlink import ChannelError, CloudClient, LanClient, decrypt_response

# Identity lives in ONE test config (fixtures/switchlink-identity.json) —
# not hardcoded here; the file doubles as a worked example for repo users
# (owner ruling 2026-11-04).
IDENT = json.loads((Path(__file__).resolve().parent / "fixtures"
                    / "switchlink-identity.json").read_text())
KEY = IDENT["SW_MAIN_DEVICEKEY"]


def test_decrypt_response_invalid_utf8_maps_to_channelerror():
    # Task5 review M1: corrupt ciphertext that decrypts to invalid UTF-8 must
    # surface as ChannelError, not leak UnicodeDecodeError.
    key_hex = hashlib.md5(KEY.encode()).hexdigest()
    iv = os.urandom(16)
    p = subprocess.run(["openssl", "enc", "-aes-128-cbc", "-K", key_hex, "-iv", iv.hex()],
                       input=b"\xff\xfe\x01", capture_output=True)
    assert p.returncode == 0, p.stderr
    body = {"data": base64.b64encode(p.stdout).decode(),
            "iv": base64.b64encode(iv).decode(), "encrypt": True}
    with pytest.raises(ChannelError):
        decrypt_response(body, KEY)


def test_lan_send_builds_encrypted_post(monkeypatch):
    captured = {}

    class FakeResp:
        def __init__(self, payload): self._p = payload
        def read(self): return json.dumps(self._p).encode()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=0):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode())
        captured["conn"] = req.get_header("Connection")
        return FakeResp({"seq": 1, "error": 0})

    monkeypatch.setattr("gridguard.switchlink.urllib.request.urlopen", fake_urlopen)
    lan = LanClient(IDENT["SW_MAIN_IP"], IDENT["SW_MAIN_DEVICEID"], KEY)
    out = lan.set_switch("off")
    assert out == {"seq": 1, "error": 0}
    assert captured["url"] == f"http://{IDENT['SW_MAIN_IP']}:8081/zeroconf/switch"
    assert captured["body"]["encrypt"] is True and "data" in captured["body"]
    assert captured["conn"] == "close"

def test_lan_paces_requests(monkeypatch):
    times = []
    class FakeResp:
        def read(self): return b'{"error":0}'
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr("gridguard.switchlink.urllib.request.urlopen",
                        lambda req, timeout=0: (times.append(time.time()), FakeResp())[1])
    lan = LanClient("1.2.3.4", "d", "k")
    lan.send("info", {}); lan.send("info", {})
    assert times[1] - times[0] >= 0.2

def test_cloud_error_zero_required(monkeypatch):
    class FakeResp:
        def read(self): return json.dumps({"error": 500, "msg": "nope"}).encode()
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr("gridguard.switchlink.urllib.request.urlopen", lambda req, timeout=0: FakeResp())
    c = CloudClient("a@b.c", "pw", "+34")
    c._auth = {"at": "tok"}          # skip login in unit test
    with pytest.raises(ChannelError):
        c.set_switch("1000375ad1", "off")


# --- Hotfix round: CoolKit token self-heal on 401 (live spam 2026-10-18) -------
def _login_urls(urls):
    return [u for u in urls if u.endswith("/user/login")]


def test_get_device_reauths_once_on_401_and_retries(monkeypatch):
    # CoolKit access token dies (TTL ~5h) → first thing-list call gets the 401
    # dict → ONE fresh login → the retried call must return the device.
    urls = []
    def fake_http_json(url, data=None, headers=None, timeout=15.0):
        urls.append(url)
        if url.endswith("/user/login"):
            return {"error": 0, "data": {"at": "tok-new"}}
        if len([u for u in urls if u.endswith("thing?num=0")]) == 1:
            return {"error": 401, "msg": "cannot found access token info"}
        return {"error": 0, "data": {"thingList": [
            {"itemData": {"deviceid": "dev1", "params": {"switch": "off"}}}]}}
    monkeypatch.setattr("gridguard.switchlink._http_json", fake_http_json)
    c = CloudClient("a@b.c", "pw", "+34")
    dev = c.get_device("dev1")
    assert dev["deviceid"] == "dev1" and dev["params"]["switch"] == "off"
    assert len(_login_urls(urls)) == 2        # initial login + exactly ONE re-auth


def test_get_device_401_on_retry_raises_without_loop(monkeypatch):
    urls = []
    def fake_http_json(url, data=None, headers=None, timeout=15.0):
        urls.append(url)
        if url.endswith("/user/login"):
            return {"error": 0, "data": {"at": "tok-new"}}
        return {"error": 401, "msg": "cannot found access token info"}
    monkeypatch.setattr("gridguard.switchlink._http_json", fake_http_json)
    c = CloudClient("a@b.c", "pw", "+34")
    with pytest.raises(ChannelError):
        c.get_device("dev1")
    assert len(_login_urls(urls)) == 2                       # exactly one re-auth
    assert len([u for u in urls if u.endswith("thing?num=0")]) == 2   # retry once, no loop


def test_status_write_reauths_once_on_401(monkeypatch):
    urls = []
    def fake_http_json(url, data=None, headers=None, timeout=15.0):
        urls.append(url)
        if url.endswith("/user/login"):
            return {"error": 0, "data": {"at": "tok-new"}}
        if len([u for u in urls if u.endswith("thing/status")]) == 1:
            return {"error": 401, "msg": "cannot found access token info"}
        return {"error": 0}
    monkeypatch.setattr("gridguard.switchlink._http_json", fake_http_json)
    c = CloudClient("a@b.c", "pw", "+34")
    assert c.set_switch("dev1", "on") == {"error": 0}        # write survives token death
    assert len(_login_urls(urls)) == 2


from gridguard.switchlink import deactivate_grid_timers

def test_deactivate_grid_timers_disarms_both_classes_keeps_input():
    timers = [
        {"type": "repeat", "do": {"switch": "on"}, "enabled": 1, "atLocal": "0 4 * * *"},
        {"type": "repeat", "do": {"switch": "off"}, "enabled": 1, "atLocal": "0 6 * * 1"},
        {"type": "repeat", "do": {"switch": "on"}, "enabled": 0, "atLocal": "30 22 * * *"},
    ]
    out = deactivate_grid_timers(timers)
    assert [t["enabled"] for t in out] == [0, 0, 0]          # BOTH classes disarmed (§12.2)
    assert out[1]["atLocal"] == "0 6 * * 1"                  # non-enabled fields preserved
    assert out[2]["enabled"] == 0                            # already disarmed stays disarmed
    assert [t["enabled"] for t in timers] == [1, 1, 0]       # input not mutated


def test_matrix_on_timer_onto_on_state_is_disarmed():
    out = deactivate_grid_timers([{"enabled": 1, "do": {"switch": "on"}}])
    assert out[0]["enabled"] == 0

def test_matrix_on_timer_onto_off_state_is_disarmed():
    # ON-timer that WOULD switch: harmless problem, still gets blocked
    out = deactivate_grid_timers([{"enabled": 1, "do": {"switch": "on"}}])
    assert out[0]["enabled"] == 0

def test_matrix_off_timer_onto_on_state_is_disarmed():
    # CRITICAL-case class: must be blocked unconditionally
    out = deactivate_grid_timers([{"enabled": 1, "do": {"switch": "off"}}])
    assert out[0]["enabled"] == 0

def test_matrix_keeps_order_and_input_untouched():
    src = [{"enabled": 1, "do": {"switch": "off"}}, {"enabled": 1, "do": {"switch": "on"}}]
    out = deactivate_grid_timers(src)
    assert [t["enabled"] for t in out] == [0, 0]
    assert [t["enabled"] for t in src] == [1, 1]


def test_get_inventory_reads_switch_and_timers_from_one_get_device(monkeypatch):
    # Task5 read-first: ONE thing-list read yields switch + timers (cloud device truth).
    calls = []
    timers = [{"type": "repeat", "do": {"switch": "on"}, "enabled": 1, "atLocal": "0 4 * * *"}]
    def fake_http_json(url, data=None, headers=None, timeout=15.0):
        calls.append(url)
        if url.endswith("/user/login"):
            return {"error": 0, "data": {"at": "tok"}}
        return {"error": 0, "data": {"thingList": [
            {"itemData": {"deviceid": "dev1", "params": {"switch": "off", "timers": timers}}}]}}
    monkeypatch.setattr("gridguard.switchlink._http_json", fake_http_json)
    c = CloudClient("a@b.c", "pw", "+34")
    assert c.get_inventory("dev1") == {"switch": "off", "timers": timers}
    assert len([u for u in calls if u.endswith("thing?num=0")]) == 1   # single get_device read
