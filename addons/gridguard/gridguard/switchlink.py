"""eWeLink channels: encrypted LAN zeroconf + cloud API. See spec Appendix A."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import subprocess
import time


class ChannelError(RuntimeError):
    pass


def _openssl(args: list[str], data: bytes) -> bytes:
    p = subprocess.run(["openssl", "enc", *args], input=data, capture_output=True)
    if p.returncode != 0:
        raise ChannelError(f"openssl: {p.stderr.decode(errors='replace').strip()}")
    return p.stdout


def _aes_enc_b64(plaintext: str, key_hex: str, iv: bytes) -> str:
    ct = _openssl(["-aes-128-cbc", "-K", key_hex, "-iv", iv.hex()], plaintext.encode())
    return base64.b64encode(ct).decode()


def _aes_dec_str(b64data: str, key_hex: str, iv_b64: str) -> str:
    raw = _openssl(["-d", "-aes-128-cbc", "-K", key_hex,
                    "-iv", base64.b64decode(iv_b64).hex(), "-nopad"],
                   base64.b64decode(b64data))
    if raw and 1 <= raw[-1] <= 16:
        raw = raw[:-raw[-1]]
    return raw.decode()


def build_zeroconf_body(deviceid: str, devicekey: str, data: dict,
                        now_ms: int | None = None) -> dict:
    key_hex = hashlib.md5(devicekey.encode()).hexdigest()
    iv = os.urandom(16)
    plain = json.dumps(data, separators=(",", ":"))
    return {
        "sequence": str(now_ms if now_ms is not None else int(time.time() * 1000)),
        "deviceid": deviceid,
        "selfApikey": "123",
        "encrypt": True,
        "data": _aes_enc_b64(plain, key_hex, iv),
        "iv": base64.b64encode(iv).decode(),
    }


def decrypt_response(body: dict, devicekey: str) -> dict | None:
    if "iv" not in body or not isinstance(body.get("data"), str):
        return None
    key_hex = hashlib.md5(devicekey.encode()).hexdigest()
    try:
        return json.loads(_aes_dec_str(body["data"], key_hex, body["iv"]))
    except (ChannelError, json.JSONDecodeError, UnicodeDecodeError) as e:
        raise ChannelError(f"response decrypt failed: {e}") from e


import socket
import urllib.error
import urllib.request

_LAN_GAP_S = 0.25
_LAN_ATTEMPTS = 3
_APP_ID = "R8Oq3y0eSZSYdKccHlrQzT1ACCOUT9Gv"
_APP_SECRET = bytes.fromhex("31766535516b3947586655684b416e3173766e4b7770416c78586b4d61727275")
_API = {"cn": "https://cn-apia.coolkit.cn", "as": "https://as-apia.coolkit.cc",
        "us": "https://us-apia.coolkit.cc", "eu": "https://eu-apia.coolkit.cc"}
_REGION_CC = {"+34": "eu", "+49": "eu", "+33": "eu", "+41": "eu", "+43": "eu", "+44": "eu", "+1": "us"}


def _http_json(url: str, data: dict | None = None, headers: dict | None = None,
               timeout: float = 15.0) -> dict:
    body = json.dumps(data).encode() if data is not None else None
    hdrs = dict(headers or {})
    if data is not None:
        # Task1 spike finding: CoolKit answers 400 without this header on JSON bodies
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=body, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"error": e.code, "msg": e.read().decode(errors="replace")[:300]}
    except (urllib.error.URLError, TimeoutError, socket.timeout) as e:
        raise ChannelError(f"http unreachable: {e}") from e


class LanClient:
    """POST http://ip:8081/zeroconf/<command>, encrypted. Pacing + retries per spec Appendix A."""

    def __init__(self, ip: str, deviceid: str, devicekey: str, timeout: float = 6.0):
        self.ip, self.deviceid, self.devicekey, self.timeout = ip, deviceid, devicekey, timeout
        self._last_send = 0.0

    def send(self, command: str, data: dict) -> dict:
        body = build_zeroconf_body(self.deviceid, self.devicekey, data)
        payload = json.dumps(body).encode()
        url = f"http://{self.ip}:8081/zeroconf/{command}"
        last: Exception | None = None
        for attempt in range(_LAN_ATTEMPTS):
            wait = _LAN_GAP_S - (time.monotonic() - self._last_send)
            if wait > 0:
                time.sleep(wait)
            req = urllib.request.Request(
                url, data=payload,
                headers={"Content-Type": "application/json", "Connection": "close"})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    raw = r.read()
                self._last_send = time.monotonic()
                if not raw:
                    raise ChannelError("empty response")
                return json.loads(raw.decode())
            except urllib.error.HTTPError as e:
                raw = e.read()
                self._last_send = time.monotonic()
                if raw:
                    return json.loads(raw.decode())
                last = ChannelError(f"http {e.code}, empty body")
            except (ConnectionResetError, BrokenPipeError) as e:
                self._last_send = time.monotonic()
                last = e                      # single-threaded device: pause+retry
                time.sleep(0.3)
            except (urllib.error.URLError, TimeoutError, socket.timeout, OSError, json.JSONDecodeError) as e:
                last = e
                time.sleep(0.5)
        raise ChannelError(f"lan {command} failed: {last!r}")

    def set_switch(self, value: str) -> dict:
        return self.send("switch", {"switch": value})

    def is_alive(self) -> bool:
        try:
            self.send("signal_strength", {})   # fw answers 422 for reads — a response = alive
            return True
        except ChannelError:
            return False


class CloudClient:
    """CoolKit v2 (official) — login, device read, switch write, timers (per spike finding)."""

    def __init__(self, email: str, password: str, cc: str, timer_api_supported: bool = True):
        self.email, self.password, self.cc = email, password, cc
        self.timer_api_supported = timer_api_supported
        self._auth: dict | None = None
        self._region = _REGION_CC.get(cc, "eu")

    def login(self) -> None:
        payload = {"password": self.password, "countryCode": self.cc, "email": self.email}
        data = json.dumps(payload).encode()
        hdr = {"Authorization": "Sign " + base64.b64encode(
                   hmac.new(_APP_SECRET, data, hashlib.sha256).digest()).decode(),
               "Content-Type": "application/json", "X-CK-Appid": _APP_ID}
        resp = _http_json(f"{_API[self._region]}/v2/user/login", data=payload, headers=hdr)
        if resp.get("error") == 10004:
            self._region = resp["data"]["region"]
            resp = _http_json(f"{_API[self._region]}/v2/user/login", data=payload, headers=hdr)
        if resp.get("error") != 0:
            raise ChannelError(f"cloud login failed: {resp}")
        self._auth = resp["data"]

    @property
    def _headers(self) -> dict:
        if self._auth is None:
            self.login()
        return {"Authorization": "Bearer " + self._auth["at"], "X-CK-Appid": _APP_ID}

    @staticmethod
    def _is_auth_error(resp) -> bool:
        """CoolKit dead-token shapes: HTTP-level 401 dict ("cannot found access
        token info") or region-hint 10004 surfacing on a request."""
        if not isinstance(resp, dict):
            return False
        return resp.get("error") in (401, 10004) \
            or "access token" in str(resp.get("msg", "")).lower()

    def _request_with_reauth(self, url: str, data: dict | None = None) -> dict:
        """Hotfix: access tokens die (TTL ~5h, live 2026-10-18). On an auth-ish
        failure discard the token, log in ONCE, retry the request EXACTLY once;
        a failing retry falls through to the caller's normal error handling."""
        resp = _http_json(url, data=data, headers=self._headers)
        if self._is_auth_error(resp):
            self._auth = None
            self.login()
            resp = _http_json(url, data=data, headers=self._headers)
        return resp

    def get_device(self, deviceid: str) -> dict:
        resp = self._request_with_reauth(f"{_API[self._region]}/v2/device/thing?num=0")
        if resp.get("error") != 0:
            raise ChannelError(f"thing list failed: {resp}")
        for item in resp["data"]["thingList"]:
            dev = item.get("itemData", {})
            if dev.get("deviceid") == deviceid:
                return dev
        raise ChannelError(f"device {deviceid} not in account")

    def get_inventory(self, deviceid: str) -> dict:
        dev = self.get_device(deviceid)
        params = dev.get("params") or {}
        return {"switch": params.get("switch"), "timers": list(params.get("timers") or [])}

    def set_switch(self, deviceid: str, value: str) -> dict:
        return self._status(deviceid, {"switch": value})

    def set_timers(self, deviceid: str, timers: list) -> dict:
        if not self.timer_api_supported:
            raise ChannelError("timer-api-unsupported (phase-0 spike finding)")
        return self._status(deviceid, {"timers": timers})

    def _status(self, deviceid: str, params: dict) -> dict:
        resp = self._request_with_reauth(f"{_API[self._region]}/v2/device/thing/status",
                                         data={"type": 1, "id": deviceid, "params": params})
        if resp.get("error") != 0:
            raise ChannelError(f"cloud status write failed: {resp}")
        return resp


def deactivate_grid_timers(timers: list[dict]) -> list[dict]:
    """§12.2 actuator discipline: BOTH timer classes are disabled
    (enabled:0), never deleted; input unchanged. Owner matrix:
    ON→ON harmless, ON→OFF minor, OFF→ON CRITICAL CASE (night grid drop),
    OFF→OFF harmless — therefore: every armed timer is disarmed."""
    return [{**t, "enabled": 0} for t in timers]
