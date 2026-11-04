# gridguard/notify.py
"""ntfy webhook push — advisory only, never raises into the loop."""
from __future__ import annotations

import sys
import urllib.request


def push(webhook: str, title: str, message: str, priority: str = "default") -> bool:
    try:
        req = urllib.request.Request(
            webhook, data=message.encode("utf-8"),
            headers={"Title": title, "Priority": priority}, method="POST")
        with urllib.request.urlopen(req, timeout=10):
            return True
    except Exception as e:                       # noqa: BLE001 — push must not kill the ruler
        print(f"[notify] push failed: {e}", file=sys.stderr, flush=True)
        return False
