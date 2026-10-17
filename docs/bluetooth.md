# Bluetooth in Docker — how this stack reaches the host adapter

The question everyone asks first: *how can a container use the host's
Bluetooth adapter?* Short answer: **it doesn't use the adapter directly — it
talks to the host's BlueZ daemon over D-Bus.** That's the whole trick.

## How Linux Bluetooth actually works

```
 your code (bleak / dbus-next)
        │  D-Bus (Unix socket /run/dbus/system_bus_socket)
        ▼
 bluetoothd  (host system service, part of bluez)
        │  kernel HCI
        ▼
 Bluetooth adapter + BLE link  →  JK BMS
```

- `bluetoothd` owns the HCI adapter and the LE links.
- Apps (including the official tools and bleak) are D-Bus *clients*.
- The D-Bus system bus is a **Unix socket**, not a network endpoint — so it is
  unaffected by container network namespaces.

## What the Compose file does

```yaml
collector:
  volumes:
    - /run/dbus/system_bus_socket:/run/dbus/system_bus_socket:ro
```

That — plus one `security_opt` line — is the entire BLE integration:

```yaml
security_opt:
  - apparmor=unconfined
```

**Why AppArmor?** Ubuntu ships an AppArmor profile for Docker
(`docker-default`) that blocks D-Bus method calls from containers, which
kills bleak before it can even start scanning:

```
BleakDBusError: [org.freedesktop.DBus.Error.AccessDenied]
An AppArmor policy prevents this sender from sending this message
to this recipient; … member="AddMatch" …
```

Setting `apparmor=unconfined` for this one service is the standard fix; on
distros without that profile it is a no-op. Trade-off, stated honestly: this
container runs without AppArmor confinement (namespace/mount isolation
remain) — it runs the code of this repo, nothing else.

- ✅ no `privileged: true`
- ✅ no `network_mode: host`
- ✅ no `cap_add`, no device cgroups
- ✅ images stay portable across hosts

The container runs as root (default), and on a default Docker install
container UID 0 *is* host UID 0, so polkit/DBus policy sees a legitimate root
client of `bluetoothd`.

## Host prerequisites

```bash
sudo apt install bluez            # Debian/Ubuntu (usually already there)
systemctl status bluetooth        # must be active
bluetoothctl show                 # adapter present: "Powered: yes"
```

If the adapter is powered off: `bluetoothctl power on`.

**No pairing required** — the JK BMS connects as an open BLE peripheral; the
collector just needs the MAC (`JK_BMS_MAC`).

## Troubleshooting

| Symptom in `docker compose logs collector` | Fix |
|---|---|
| `waiting (BMS not advertising…)` | normal: the phone app (or a previous run) holds the link — close the app and wait |
| D-Bus / `No such file or directory` on `/run/dbus/system_bus_socket` | host has no system bus at that path (rare custom dbus setups) — check `ls -l /run/dbus/`, or run the native (non-Docker) install |
| `org.freedesktop.DBus.Error.AccessDenied` + `AppArmor policy` | the shipped compose already sets `apparmor=unconfined` — if you removed `security_opt`, restore it (see above) |
| scan finds nothing | adapter range/antenna — BLE range to a metal battery box is often the real issue; USB extension cables help |

### Verifying BLE works inside the container

The image is intentionally minimal (no `bluetoothctl`), so verify via logs:

```bash
docker compose logs -f collector
# healthy sequence:  "BMS advertising — connecting" → "connected"
curl -s localhost:8099/api/health          # -> ok
```

## Fallback: host networking

If your distro does something unusual (e.g. a rootless/unprivileged D-Bus
session the socket mount can't satisfy), the battle-tested fallback is:

```yaml
collector:
  network_mode: host
  environment:
    - HTTP_PORT=9099        # avoid clashing with anything on the host
  # the D-Bus mount stays as-is
```

Host networking is *not* needed for BLE itself — D-Bus is a Unix socket — but
it sidesteps exotic setups where policy checks depend on the network
namespace. Try the default first; it has been verified end-to-end on
Ubuntu 25.04 (2026-10-17): D-Bus mount + `apparmor=unconfined` → scan →
connect → ~1 Hz frames → per-minute indexed documents → charts.

## Single-central caveat

The JK BMS accepts **one** GATT client at a time. While the collector is
connected, the phone app shows "connect failed" — that's expected. The
collector's status pill shows `LIVE` / `scanning` / `waiting (…)`, and
`last frame age` tells you who currently holds the link. On SIGTERM the
container closes the LE link cleanly so the phone can take over immediately.
