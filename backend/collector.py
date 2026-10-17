"""JK BMS read-only collector (BLE -> state API -> OpenSearch).

Continuously polls the JK BMS over Bluetooth Low Energy (single-central:
whichever client connects first holds the link — this collector wins over the
phone app while running), decodes every frame type the BMS delivers (cell info
@1 Hz, device info, settings), and serves:

    http://0.0.0.0:8099/api/state   full JSON state (live snapshot)
    http://0.0.0.0:8099/api/health  "ok" while frames stream

When OPENSEARCH_URL is set, one document per minute is indexed into a daily
index (INDEX_PREFIX-YYYY.MM.DD, date computed in INDEX_TZ) — no cron needed.

Strictly read-only: only registers 0x96 (cell info) and 0x97 (device info) are
ever requested; no settings writes exist in this code base.

Configuration: environment variables (see .env.example / README).
"""

from __future__ import annotations

import copy
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# validate configuration before importing heavy/environment-dependent modules
CONFIG = {
    "address": os.environ.get("JK_BMS_MAC", "").strip(),
    "http_host": os.environ.get("HTTP_HOST", "0.0.0.0"),
    "http_port": int(os.environ.get("HTTP_PORT", "8099")),
    "scan_timeout": 20,
    "frame_timeout_s": 20,  # no frame for this long -> re-request stream
    "reconnect_backoff_s": 5,
}
if not CONFIG["address"]:
    raise SystemExit(
        "JK_BMS_MAC is not set — the Bluetooth MAC of your BMS is required.\n"
        "Find it with e.g. `bluetoothctl scan on` or the JK app, then set it\n"
        "in .env (see README, section Quickstart).")

import jkbms
from bleak import BleakClient, BleakScanner

OS_URL = os.environ.get("OPENSEARCH_URL", "").rstrip("/")
INDEX_PREFIX = os.environ.get("INDEX_PREFIX", "jkbms")
INDEX_TZ = os.environ.get("INDEX_TZ", "UTC")

log = logging.getLogger("jkbms")


# ---------------------------------------------------------------- state ----

STATE: dict = {
    "device": {"address": CONFIG["address"], "name": None, "model": None,
               "hardware": None, "software": None, "serial": None},
    "live": {"status": "starting", "connected": False, "streaming": False,
             "last_frame": None, "last_frame_age_s": None, "frames_total": 0,
             "frames_cell_info": 0, "frames_settings": 0, "frames_device_info": 0,
             "layout": None, "started": datetime.now(timezone.utc).isoformat()},
    "cells": {},
    "pack": {},
    "soc": {},
    "temps": {},
    "status": {},
    "settings": {},
}
_lock = threading.Lock()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def set_status(status: str, **kw) -> None:
    with _lock:
        STATE["live"]["status"] = status
        STATE["live"].update(kw)


def update_from_frame(ftype: int, frame: bytes) -> None:
    global _layout_cache
    with _lock:
        STATE["live"]["frames_total"] += 1
        STATE["live"]["last_frame"] = now_iso()
        STATE["live"]["last_frame_age_s"] = 0.0
        if ftype == jkbms.TYPE_CELL_INFO:
            STATE["live"]["last_raw_cell_frame"] = frame.hex()
            layout = _layout_cache if _layout_cache is not None else jkbms.detect_layout(frame)
            _layout_cache = layout
            d = jkbms.decode_cell_info(frame, layout)
            STATE["live"]["frames_cell_info"] += 1
            STATE["live"]["layout"] = {"head_shift": layout[0], "tail_shift": layout[1]}
            STATE["live"]["streaming"] = True
            slots = jkbms.slots_from_model(STATE["device"].get("model"))
            STATE["cells"] = {
                "count_active": d["cell_count_active"],
                "count_config": slots or d["cell_count_config"],
                "voltages": d["voltages"],
                "resistances": d["resistances"],
                "min_cell": d["min_cell"], "max_cell": d["max_cell"],
                "min_voltage": d["min_voltage"], "max_voltage": d["max_voltage"],
                "avg_voltage": d["avg_voltage"], "delta_voltage": d["delta_voltage"],
                "enabled_mask": d["enabled_mask"],
            }
            STATE["pack"] = {
                "voltage": d["pack_voltage"], "current": d["current"],
                "power": d["power"],
            }
            cur = d["current"]
            rem = d["capacity_remaining"]
            full = d["capacity_full"]
            # direction-aware time prediction (linear extrapolation)
            eta_empty_h = round(rem / -cur, 2) if (cur < -0.2 and rem is not None) else None
            eta_full_h = (round((full - rem) / cur, 2)
                          if (cur > 0.2 and rem is not None and full) else None)
            STATE["soc"] = {
                "soc": d["soc"], "soh": d["soh"],
                "remaining_ah": rem, "full_ah": full,
                "cycles": d["cycles"], "total_cycle_capacity_ah": d["total_cycle_capacity"],
                "runtime_seconds": d["runtime_seconds"],
                "eta_empty_h": eta_empty_h, "eta_full_h": eta_full_h,
            }
            STATE["temps"] = {
                "t1": d["temperature_1"], "t2": d["temperature_2"],
                "mosfet": d["mosfet_temperature"], "heating": d["heating"],
                "heating_current": d["heating_current"],
            }
            STATE["status"] = {
                "charging": d["charging"], "discharging": d["discharging"],
                "precharging": d["precharge_flag"], "balancing": d["balancing"],
                "balancing_current": d["balancing_current"], "balancer_mask": d["balancer_mask"],
                "charge_status": d["charge_status"], "charge_status_id": d["charge_status_id"],
                "errors_mask": d["errors_mask"],
                "emergency_countdown": d["emergency_countdown"],
                "smart_sleep_countdown": d["smart_sleep_countdown"],
                "charge_status_time_elapsed": d["charge_status_time_elapsed"],
            }
        elif ftype == jkbms.TYPE_DEVICE_INFO:
            STATE["live"]["frames_device_info"] += 1
            STATE["device"].update(jkbms.decode_device_info(frame))
        elif ftype == jkbms.TYPE_SETTINGS:
            STATE["live"]["frames_settings"] += 1
            STATE["live"]["last_raw_settings_frame"] = frame.hex()
            STATE["settings"] = jkbms.decode_settings(frame)


_layout_cache: tuple[int, int] | None = None


# ------------------------------------------------------------ BLE loop ----

async def ble_main() -> None:
    addr = CONFIG["address"]
    while True:
        try:
            set_status("scanning", connected=False, streaming=False)
            devs = await BleakScanner.discover(timeout=CONFIG["scan_timeout"], return_adv=True)
            if addr.lower() not in {a.lower() for a in devs}:
                set_status("waiting (BMS not advertising — phone app may be connected)",
                           connected=False, streaming=False)
                continue

            log.info("BMS advertising — connecting")
            set_status("connecting", connected=False, streaming=False)
            async with BleakClient(addr, timeout=25) as client:
                set_status("connected", connected=True)
                log.info("connected")
                assembler = jkbms.FrameAssembler()
                got = {"last": time.monotonic()}

                def on_notify(_sender: object, data: bytearray) -> None:
                    got["last"] = time.monotonic()
                    for frame in assembler.feed(bytes(data)):
                        update_from_frame(jkbms.frame_type(frame), frame)

                await client.start_notify(jkbms.CHAR_UUID, on_notify)
                await client.write_gatt_char(jkbms.CHAR_UUID,
                                             jkbms.build_request(jkbms.COMMAND_DEVICE_INFO),
                                             response=False)
                await asyncio.sleep(1.0)
                await client.write_gatt_char(jkbms.CHAR_UUID,
                                             jkbms.build_request(jkbms.COMMAND_CELL_INFO),
                                             response=False)

                while client.is_connected:
                    idle = time.monotonic() - got["last"]
                    if idle > CONFIG["frame_timeout_s"]:
                        log.warning("no frames for %.0fs — re-requesting stream", idle)
                        await client.write_gatt_char(
                            jkbms.CHAR_UUID,
                            jkbms.build_request(jkbms.COMMAND_CELL_INFO), response=False)
                        got["last"] = time.monotonic()
                    with _lock:
                        STATE["live"]["last_frame_age_s"] = round(
                            (datetime.now(timezone.utc) -
                             datetime.fromisoformat(STATE["live"]["last_frame"])
                             ).total_seconds(), 1) if STATE["live"]["last_frame"] else None
                    await asyncio.sleep(5)
                log.warning("connection dropped")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # BLE flakiness must never kill the daemon
            log.error("BLE loop error: %s: %s", type(exc).__name__, exc)
            set_status(f"reconnecting ({type(exc).__name__})", connected=False, streaming=False)
        await asyncio.sleep(CONFIG["reconnect_backoff_s"])


# ---------------------------------------------------------- HTTP layer ----

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: object) -> None:  # quiet access log
        pass

    def _send(self, code: int, ctype: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/" or self.path.startswith("/index"):
            landing = ("jk-bms collector API\n\n"
                       "  /api/state   full JSON state\n"
                       "  /api/health  stream health\n\n"
                       "The web UI is served by the frontend service.\n")
            self._send(200, "text/plain; charset=utf-8", landing.encode())
        elif self.path.startswith("/api/state"):
            with _lock:
                body = json.dumps(STATE).encode()
            self._send(200, "application/json", body)
        elif self.path.startswith("/api/health"):
            with _lock:
                ok = bool(STATE["live"]["streaming"])
            self._send(200, "text/plain", b"ok\n" if ok else b"no-stream\n")
        else:
            self._send(404, "text/plain", b"not found\n")


# ------------------------------------------------------ OpenSearch push ----

def push_loop() -> None:
    """Index one document per minute (aligned to the minute boundary) while
    OPENSEARCH_URL is set; failures are logged and retried next minute."""
    if not OS_URL:
        log.info("OPENSEARCH_URL not set — running without indexing")
        return
    import os_push
    while True:
        now = time.time()
        time.sleep(max(1.0, 60.0 - (now % 60.0)))
        with _lock:
            snapshot = copy.deepcopy(STATE)
        try:
            os_push.push_state(snapshot, os_url=OS_URL,
                               index_prefix=INDEX_PREFIX, tz_name=INDEX_TZ)
        except Exception as exc:
            log.error("index push failed: %s: %s", type(exc).__name__, exc)


def http_main() -> None:
    srv = ThreadingHTTPServer((CONFIG["http_host"], CONFIG["http_port"]), Handler)
    log.info("HTTP dashboard on %s:%s", CONFIG["http_host"], CONFIG["http_port"])
    srv.serve_forever()


async def _run_graceful() -> None:
    """Run the BLE loop; on SIGTERM/SIGINT cancel it so the BleakClient context
    manager closes the LE link cleanly (a killed-while-connected daemon leaves
    an orphaned kernel connection that locks out everyone, incl. the phone)."""
    import signal
    task = asyncio.create_task(ble_main())
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    await stop.wait()
    log.info("shutdown requested — closing BLE cleanly")
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    threading.Thread(target=http_main, daemon=True).start()
    threading.Thread(target=push_loop, daemon=True).start()
    asyncio.run(_run_graceful())


if __name__ == "__main__":
    main()
