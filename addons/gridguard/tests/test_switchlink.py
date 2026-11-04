import base64, hashlib, json, subprocess
from pathlib import Path

from gridguard.switchlink import ChannelError, build_zeroconf_body, decrypt_response

# Identity lives in ONE test config (fixtures/switchlink-identity.json) —
# not hardcoded here; the file doubles as a worked example for repo users
# (owner ruling 2026-11-04).
IDENT = json.loads((Path(__file__).resolve().parent / "fixtures"
                    / "switchlink-identity.json").read_text())
KEY = IDENT["SW_MAIN_DEVICEKEY"]

def _plain_of(body, devicekey):
    key_hex = hashlib.md5(devicekey.encode()).hexdigest()
    p = subprocess.run(["openssl", "enc", "-d", "-aes-128-cbc", "-K", key_hex,
                        "-iv", base64.b64decode(body["iv"]).hex(), "-nopad"],
                       input=base64.b64decode(body["data"]), capture_output=True)
    raw = p.stdout
    return json.loads(raw[:-raw[-1]].decode())

def test_body_shape_and_roundtrip():
    b = build_zeroconf_body(IDENT["SW_MAIN_DEVICEID"], KEY, {"switch": "off"}, now_ms=1792261163592)
    assert set(b) == {"sequence", "deviceid", "selfApikey", "encrypt", "data", "iv"}
    assert b["sequence"] == "1792261163592"
    assert b["deviceid"] == IDENT["SW_MAIN_DEVICEID"] and b["selfApikey"] == "123" and b["encrypt"] is True
    assert _plain_of(b, KEY) == {"switch": "off"}

def test_sequence_is_13_digits_when_generated():
    b = build_zeroconf_body("d", KEY, {})
    assert b["sequence"].isdigit() and len(b["sequence"]) == 13

def test_decrypt_response_roundtrip():
    # device-style response: encrypted data + iv
    src = build_zeroconf_body("d", KEY, {})
    resp = {"seq": 1, "error": 0, "data": src["data"], "iv": src["iv"], "encrypt": True}
    assert decrypt_response(resp, KEY) == {}

def test_decrypt_response_plain_and_error_bodies():
    assert decrypt_response({"seq": 7, "error": 422}, KEY) is None
    assert decrypt_response({"error": 0, "data": {"switch": "off"}}, KEY) is None
