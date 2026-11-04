#!/usr/bin/env python3
"""Phase-0 spike: verify eWeLink cloud write + timer API. Usage:
  spike_cloud.py read
  spike_cloud.py noop <deviceid> <on|off>     # requires owner approval, value must equal current state
  spike_cloud.py timers <deviceid>            # write-back the CURRENT timers unchanged (idempotent probe)
  spike_cloud.py timers_lifecycle <deviceid>  # add ONE far-future test timer, verify read-back, remove, verify gone
  spike_cloud.py once_probe <deviceid>        # discover accepted ONCE-timer format (enabled:0, far future, removed after each try)
  spike_cloud.py enabled_probe <deviceid> <offset_min> <0|1>  # near-future ON-timer, poll cloud state through fire+95s, cleanup
"""
import json, os, sys, time, base64, hashlib, hmac, urllib.request, urllib.error

API = {"cn": "https://cn-apia.coolkit.cn", "as": "https://as-apia.coolkit.cc",
       "us": "https://us-apia.coolkit.cc", "eu": "https://eu-apia.coolkit.cc"}
APP_ID = "R8Oq3y0eSZSYdKccHlrQzT1ACCOUT9Gv"
APP_SECRET = bytes.fromhex("31766535516b3947586655684b416e3173766e4b7770416c78586b4d61727275")
REGION_CC = {"+34": "eu", "+49": "eu", "+33": "eu", "+41": "eu", "+43": "eu", "+44": "eu", "+1": "us"}


def call(url, data=None, headers=None):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"error": e.code, "msg": e.read().decode(errors="replace")[:300]}


def login():
    email, pw, cc = os.environ["EWCLOUD_EMAIL"], os.environ["EWCLOUD_PASS"], os.environ.get("EWCLOUD_CC", "+34")
    region = REGION_CC.get(cc, "eu")
    payload = {"password": pw, "countryCode": cc, "email": email}
    data = json.dumps(payload).encode()
    hdr = {"Authorization": "Sign " + base64.b64encode(hmac.new(APP_SECRET, data, hashlib.sha256).digest()).decode(),
           "Content-Type": "application/json", "X-CK-Appid": APP_ID}
    resp = call(f"{API[region]}/v2/user/login", data=payload, headers=hdr)
    if resp.get("error") == 10004:
        region = resp["data"]["region"]
        resp = call(f"{API[region]}/v2/user/login", data=payload, headers=hdr)
    if resp.get("error") != 0:
        sys.exit(f"login failed: {resp}")
    return region, resp["data"]


def devices(region, auth):
    h = {"Authorization": "Bearer " + auth["at"], "X-CK-Appid": APP_ID, "Content-Type": "application/json"}
    resp = call(f"{API[region]}/v2/device/thing?num=0", headers=h)
    return h, [i["itemData"] for i in resp["data"]["thingList"] if "deviceid" in i.get("itemData", {})]


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "read"
    region, auth = login()
    h, devs = devices(region, auth)
    if cmd == "read":
        for d in devs:
            print(d["name"], d["deviceid"], "switch=", d.get("params", {}).get("switch"),
                  "timers=", len(d.get("params", {}).get("timers", [])))
        return
    did = sys.argv[2]
    dev = next(d for d in devs if d["deviceid"] == did)
    if cmd == "noop":
        val = sys.argv[3]
        cur = dev["params"]["switch"]
        assert val == cur, f"NOOP safety: sending {val} but current is {cur}"
        r = call(f"{API[region]}/v2/device/thing/status", data={"type": 1, "id": did, "params": {"switch": val}}, headers=h, )
        print("noop result:", r)
    elif cmd == "timers":
        cur = dev["params"].get("timers", [])
        r = call(f"{API[region]}/v2/device/thing/status",
                 data={"type": 1, "id": did, "params": {"timers": cur}}, headers=h)
        print("timer write-back result:", r)
        print("after:", call(f"{API[region]}/v2/device/thing/status?type=1&id={did}", headers=h))
    elif cmd == "timers_lifecycle":
        url = f"{API[region]}/v2/device/thing/status"
        base = dev["params"].get("timers", [])

        def read_timers():
            return call(f"{url}?type=1&id={did}", headers=h).get("data", {}).get("params", {}).get("timers", [])

        when = time.time() + 12 * 3600
        loc, utc = time.localtime(when), time.gmtime(when)
        t = {"type": "repeat", "coolkit_timer_type": "repeat", "enabled": 1, "do": {"switch": "on"},
             "at": f"{utc.tm_min} {utc.tm_hour} * * *", "atLocal": f"{loc.tm_min} {loc.tm_hour} * * *"}
        print("baseline timers:", json.dumps(base))
        print("sent test timer:", json.dumps(t))
        print("create:", call(url, data={"type": 1, "id": did, "params": {"timers": base + [t]}}, headers=h))
        cur = read_timers()
        new = [x for x in cur if x not in base]
        print("read-back timers:", json.dumps(cur))
        print("created timer as accepted:", json.dumps(new[0] if new else None))
        print("remove:", call(url, data={"type": 1, "id": did, "params": {"timers": base}}, headers=h))
        cur2 = read_timers()
        print("after removal timers:", json.dumps(cur2))
        print("lifecycle OK" if cur2 == base else "lifecycle MISMATCH")
    elif cmd == "once_probe":
        if not dev.get("online"):
            sys.exit(f"{did} is offline (wifi dark window) — refusing live probe")
        url = f"{API[region]}/v2/device/thing/status"
        base = dev["params"].get("timers", [])

        def read_t():
            return call(f"{url}?type=1&id={did}", headers=h).get("data", {}).get("params", {}).get("timers", [])

        ms = str(int(time.mktime(time.strptime("2026-12-24 12:00", "%Y-%m-%d %H:%M"))) * 1000)
        variants = [
            {"type": "once", "coolkit_timer_type": "once", "enabled": 0, "do": {"switch": "on"},
             "at": "2026-12-24T12:00:00", "atLocal": "2026-12-24T12:00:00"},
            {"type": "once", "coolkit_timer_type": "once", "enabled": 0, "do": {"switch": "on"},
             "at": ms, "atLocal": ms},
            {"type": "once", "coolkit_timer_type": "once", "enabled": 0, "do": {"switch": "on"},
             "at": "0 12 24 12 *", "atLocal": "0 12 24 12 *"},
        ]
        try:
            for i, t in enumerate(variants, 1):
                print(f"--- variant {i} request: {json.dumps(t)}")
                r = call(url, data={"type": 1, "id": did, "params": {"timers": base + [t]}}, headers=h)
                print(f"variant {i} create response: {json.dumps(r)}")
                new = [x for x in read_t() if x not in base]
                print(f"variant {i} read-back new timers: {json.dumps(new)}")
                if new:
                    print(f"variant {i} stored verbatim (== sent): {json.dumps(new[0] == t)}")
                rm = call(url, data={"type": 1, "id": did, "params": {"timers": base}}, headers=h)
                print(f"variant {i} remove response: {json.dumps(rm)}")
                print(f"variant {i} after-remove count: {len(read_t())} (baseline {len(base)})")
        finally:
            print("cleanup write-back:", json.dumps(call(url, data={"type": 1, "id": did, "params": {"timers": base}}, headers=h)))
            cur = read_t()
            print("cleanup read-back:", json.dumps(cur))
            print("cleanup OK" if cur == base else "cleanup MISMATCH")
    elif cmd == "enabled_probe":
        off_min, en = int(sys.argv[3]), int(sys.argv[4])
        if not dev.get("online"):
            sys.exit(f"{did} is offline (wifi dark window) — refusing live probe")
        url = f"{API[region]}/v2/device/thing/status"
        base = dev["params"].get("timers", [])
        sw0 = dev["params"].get("switch")

        def state():
            p = call(f"{url}?type=1&id={did}", headers=h).get("data", {}).get("params", {})
            return p.get("switch"), p.get("timers", [])

        fire = time.time() + off_min * 60
        loc, utc = time.localtime(fire), time.gmtime(fire)
        t = {"type": "repeat", "coolkit_timer_type": "repeat", "enabled": en, "do": {"switch": "on"},
             "at": f"{utc.tm_min} {utc.tm_hour} * * *", "atLocal": f"{loc.tm_min} {loc.tm_hour} * * *"}
        try:
            print(f"fire at local {time.strftime('%Y-%m-%d %H:%M:%S', loc)} (cron at={t['at']} atLocal={t['atLocal']}), enabled={en}")
            print("sent test timer:", json.dumps(t))
            print("create:", json.dumps(call(url, data={"type": 1, "id": did, "params": {"timers": base + [t]}}, headers=h)))
            sw, tm = state()
            print(f"[{time.strftime('%H:%M:%S')}] after create: switch={sw} timers={len(tm)}")
            while time.time() < fire + 95:
                time.sleep(15)
                sw, tm = state()
                print(f"[{time.strftime('%H:%M:%S')}] t={time.time() - fire:+.0f}s vs fire: switch={sw} timers={len(tm)}")
        finally:
            print("cleanup remove test timers:", json.dumps(call(url, data={"type": 1, "id": did, "params": {"timers": base}}, headers=h)))
            sw, tm = state()
            if sw != sw0:
                print(f"cleanup switch {sw} != baseline {sw0}, restoring:", json.dumps(
                    call(url, data={"type": 1, "id": did, "params": {"switch": sw0}}, headers=h)))
                sw, tm = state()
            print("cleanup final timers:", json.dumps(tm))
            print("cleanup OK" if (sw == sw0 and tm == base) else "cleanup MISMATCH")


if __name__ == "__main__":
    main()
