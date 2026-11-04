# addons/gridguard/gridguard/webservice.py
"""The addon's own web surface: /health + page+payload routes, served from a
crash-netted daemon thread INSIDE the worker process — it may never kill the
loop (start is crash-netted). Stdlib only."""
import json
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .bms import _search
# Module-level (NOT inside create_server): the test seam patches
# gridguard.webservice.fetch_gridguard and do_GET must resolve the name
# through the module namespace at call time — a function-local import would
# bind a closure capture the patch can never reach.
from .web_page import PAGE
from .web_payload import fetch_gridguard

VERSION = "1.0.0"
CONTRACT = 1
_ID, _TITLE, _ORDER, _HASHES = "gridguard", "Energie Management", 20, ["grid"]


def health_payload(os_url: str, events_index: str) -> dict:
    """Self-description + heartbeat (spec §3.2). Freshness = the pill's own
    rule (JS): cad = 1800 s at night else 600 s; fresh iff age <= 2*cad."""
    idx = events_index if events_index.endswith("*") else events_index + "-*"
    age_s = newest_ts = phase = None
    try:
        res = _search({"size": 1, "sort": [{"ts": "desc"}],
                       "query": {"term": {"event": "tick"}},
                       "_source": ["ts", "phase"]},
                      os_url, idx, 5.0)
        hit = (res.get("hits", {}).get("hits") or [{}])[0].get("_source", {})
        newest_ts, phase = hit.get("ts"), hit.get("phase")
        if newest_ts:
            t = datetime.fromisoformat(newest_ts)
            age_s = max((datetime.now(timezone.utc) - t).total_seconds(), 0.0)
    except Exception:                                # noqa: BLE001 — honest dead
        age_s, newest_ts, phase = None, None, None
    cad = 1800 if phase == "night" else 600
    fresh = age_s is not None and age_s <= 2 * cad
    return {"id": _ID, "title": _TITLE, "order": _ORDER, "version": VERSION,
            "contract_version": CONTRACT, "fresh": fresh,
            "age_s": age_s, "newest_ts": newest_ts, "hashes": list(_HASHES)}


def create_server(host, port, os_url, events_index):
    class Handler(BaseHTTPRequestHandler):
        server_version, sys_version = "gridguard", ""
        def log_message(self, *_): pass
        def _send(self, code, body, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers(); self.wfile.write(body)
        def do_GET(self):
            from urllib.parse import urlparse
            path = urlparse(self.path).path
            if path == "/":
                self._send(200, PAGE.encode(), "text/html; charset=utf-8")
            elif path == "/api/gridguard":
                try:
                    self._send(200, json.dumps(fetch_gridguard()).encode(),
                               "application/json")
                except Exception as exc:               # noqa: BLE001 — 502 parity
                    self._send(502, json.dumps({"error": str(exc)}).encode(),
                               "application/json")
            elif path == "/health":
                self._send(200, json.dumps(health_payload(os_url, events_index)).encode(),
                           "application/json")
            else:
                self._send(404, b"not found", "text/plain")
    return ThreadingHTTPServer((host, port), Handler)


def start(cfg) -> None:
    """Daemon web thread inside the worker — a failure must never kill the
    loop (owner safety doctrine): start wrapped, honest line, worker on."""
    import os, threading
    try:
        srv = create_server("0.0.0.0", int(os.environ.get("WEB_PORT", "8101")),
                            cfg.os_url, cfg.events_index)
        threading.Thread(target=srv.serve_forever, daemon=True,
                         name="gridguard-web").start()
        print(f"gridguard web on :{srv.server_address[1]}", flush=True)
    except Exception as exc:                          # noqa: BLE001
        print(f"[gridguard-web] start failed: {exc}", flush=True)
        return None
