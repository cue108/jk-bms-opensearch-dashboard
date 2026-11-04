from gridguard.config import load_config
from gridguard.reach import discover_wlan, operstate_up, reachable

GOOD = {"TZ": "Europe/Madrid", "WINDOW_START": "09:00", "WINDOW_END": "22:00",
        "EVE_START": "20:00", "RESCUE_AT": "04:00", "OPENSEARCH_URL": "http://x",
        "BMS_INDEX": "i", "EWCLOUD_EMAIL": "a", "EWCLOUD_PASS": "b",
        "SW_MAIN_DEVICEID": "d", "SW_MAIN_DEVICEKEY": "k", "SW_MAIN_IP": "1.2.3.4",
        "NTFY_WEBHOOK": "https://n/t"}

def test_operstate_up(tmp_path):
    (tmp_path / "wlanX").mkdir()
    (tmp_path / "wlanX" / "operstate").write_text("up\n")
    assert operstate_up("wlanX", sysfs_base=str(tmp_path)) is True

def test_operstate_down_and_missing(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "operstate").write_text("down\n")
    assert operstate_up("a", sysfs_base=str(tmp_path)) is False
    assert operstate_up("nope", sysfs_base=str(tmp_path)) is False     # FileNotFoundError → False
    assert operstate_up("a", sysfs_base="/definitely/missing") is False


# --- owner2026-10-21: host50 has exactly ONE WiFi dongle — DISCOVER it -------------
def test_reach_discovers_the_single_dongle(tmp_path):
    # the canonical wireless marker, never a pinned MAC-derived name (it rots on swap)
    (tmp_path / "wlx801f02c60130").mkdir()
    (tmp_path / "wlx801f02c60130" / "wireless").mkdir()
    (tmp_path / "wlx801f02c60130" / "operstate").write_text("up\n")
    cfg = load_config({**GOOD, "WLAN_IFACE": ""})               # "" = auto-discover
    assert discover_wlan(sysfs_base=str(tmp_path)) == "wlx801f02c60130"
    assert reachable(cfg, sysfs_base=str(tmp_path)) is True
    (tmp_path / "wlx801f02c60130" / "operstate").write_text("down\n")
    assert reachable(cfg, sysfs_base=str(tmp_path)) is False    # the AP-timer night


def test_reach_no_dongle_is_unreachable(tmp_path):
    (tmp_path / "eth0").mkdir()                                 # wired, no wireless marker
    cfg = load_config({**GOOD, "WLAN_IFACE": ""})
    assert discover_wlan(sysfs_base=str(tmp_path)) is None
    assert reachable(cfg, sysfs_base=str(tmp_path)) is False


def test_reach_configured_name_still_wins(tmp_path):
    # the env override stays as an escape hatch (multi-dongle future), not the default
    (tmp_path / "wlan9").mkdir()
    (tmp_path / "wlan9" / "operstate").write_text("up\n")
    cfg = load_config({**GOOD, "WLAN_IFACE": "wlan9"})
    assert reachable(cfg, sysfs_base=str(tmp_path)) is True
