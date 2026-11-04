"""R5' reachability truth: the host's WLAN association. Device uplink = single gate.

owner config-doctrine (2026-10-21): a config may hold only what does NOT change —
ssid, password, identity keys. Interface names, MACs and USB ports are MOMENTARY
STATES: the software must discover them itself (replace the dongle, move the port,
nothing restarts, nothing hard-fails, the actor access never crashes on a stale
snapshot). The association itself = the host's job with its known credentials;
this module only FINDS the dongle and READS its state."""
from __future__ import annotations

import os

from .config import Config


def operstate_up(iface: str, sysfs_base: str = "/sys/class/net") -> bool:
    try:
        with open(f"{sysfs_base}/{iface}/operstate", encoding="ascii") as f:
            return f.read().strip() == "up"
    except OSError:
        return False


def discover_wlan(sysfs_base: str = "/sys/class/net") -> str | None:
    """Find THE WiFi dongle by the canonical sysfs `wireless` marker — never by a
    pinned name (a snapshot that rots on every swap/re-enumeration). Deterministic
    (sorted); None = no dongle present → unreachable, never an exception."""
    try:
        names = sorted(os.listdir(sysfs_base))
    except OSError:
        return None
    for n in names:
        if os.path.isdir(f"{sysfs_base}/{n}/wireless"):
            return n
    return None


def reachable(cfg: Config, sysfs_base: str = "/sys/class/net") -> bool:
    iface = cfg.wlan_iface or discover_wlan(sysfs_base)   # "" (default) = auto-discover
    if not iface:
        return False
    return operstate_up(iface, sysfs_base)
