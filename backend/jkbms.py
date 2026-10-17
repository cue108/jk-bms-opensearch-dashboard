"""JK BMS (Jikong 极空) BLE protocol — read-only implementation.

Protocol reverse-engineered by syssi/esphome-jk-bms (GPL-3.0, vendor/esphome-jk-bms).
This module only ever BUILDS read-request frames (registers 0x96/0x97); it contains
no code path that writes settings. Request frames:

    AA 55 90 EB | reg | 00 | value(u32le, zero) | pad.. | CRC8(sum of bytes 0..18)

Responses are 300-byte frames:

    55 AA EB 90 | type | ... | CRC8(sum of bytes 0..298) @ byte 299

    type 0x01 = settings, 0x02 = cell info, 0x03 = device info, 0x05 = logbook
"""

from __future__ import annotations

SERVICE_UUID = "0000ffe0-0000-1000-8000-00805f9b34fb"
CHAR_UUID = "0000ffe1-0000-1000-8000-00805f9b34fb"

COMMAND_CELL_INFO = 0x96
COMMAND_DEVICE_INFO = 0x97

FRAME_PREAMBLE = b"\x55\xaa\xeb\x90"
FRAME_SIZE = 300

TYPE_SETTINGS = 0x01
TYPE_CELL_INFO = 0x02
TYPE_DEVICE_INFO = 0x03
TYPE_LOGBOOK = 0x05

CHARGE_STATUS = {
    0: "No charge",
    1: "Normal charge",
    2: "Float",
    3: "Charge full",
    4: "Normal discharge",
    5: "Off",
}


def build_request(register: int, value: int = 0, length: int = 0) -> bytes:
    """Build a 20-byte read-request frame (CRC = 8-bit byte sum)."""
    frame = bytearray(20)
    frame[0:4] = b"\xaa\x55\x90\xeb"
    frame[4] = register & 0xFF
    frame[5] = length & 0xFF
    frame[6] = value & 0xFF
    frame[7] = (value >> 8) & 0xFF
    frame[8] = (value >> 16) & 0xFF
    frame[9] = (value >> 24) & 0xFF
    frame[19] = sum(frame[0:19]) & 0xFF
    return bytes(frame)


class FrameAssembler:
    """Accumulate BLE notifications into complete 300-byte frames."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[bytes]:
        out: list[bytes] = []
        if len(data) >= 4 and data[0:4] == FRAME_PREAMBLE:
            self._buf.clear()
        self._buf += data
        if len(self._buf) > 2 * FRAME_SIZE:
            self._buf.clear()  # runaway garbage protection
        while len(self._buf) >= FRAME_SIZE:
            frame = bytes(self._buf[:FRAME_SIZE])
            del self._buf[:FRAME_SIZE]
            crc = sum(frame[0 : FRAME_SIZE - 1]) & 0xFF
            if crc == frame[FRAME_SIZE - 1]:
                out.append(frame)
        return out


def u16(b: bytes, i: int) -> int:
    return int.from_bytes(b[i : i + 2], "little")


def s16(b: bytes, i: int) -> int:
    return int.from_bytes(b[i : i + 2], "little", signed=True)


def u32(b: bytes, i: int) -> int:
    return int.from_bytes(b[i : i + 4], "little")


def s32(b: bytes, i: int) -> int:
    return int.from_bytes(b[i : i + 4], "little", signed=True)


def _cstr(b: bytes, i: int, n: int) -> str:
    return bytes(b[i : i + n]).split(b"\x00")[0].decode(errors="replace")


def frame_type(frame: bytes) -> int:
    return frame[4]


def decode_device_info(frame: bytes) -> dict:
    return {
        "model": _cstr(frame, 6, 16),
        "hardware": _cstr(frame, 22, 8),
        "software": _cstr(frame, 30, 8),
        "name": _cstr(frame, 46, 16),
        "serial": _cstr(frame, 86, 11),
    }


def slots_from_model(model: str | None) -> int | None:
    """Max cell slots from model string, e.g. 'JK_B2A8S20P' -> 8."""
    if not model:
        return None
    import re
    m = re.search(r"A(\d+)S", model)
    return int(m.group(1)) if m else None


def detect_layout(frame: bytes) -> tuple[int, int]:
    """Pick (head_shift, tail_shift) by plausibility of pack voltage vs cell sum.

    Verified against hw 11.XW / sw 11.287 (JK_B2A8S20P, 4S pack): head uses the
    32S-style shift (+16), the scalar tail block sits at +32.
    """
    cells = [u16(frame, 6 + i * 2) * 0.001 for i in range(24)]
    live = [v for v in cells if v > 1.0]
    cell_sum = sum(live)
    n = len(live)
    head = 16
    for h in (16, 0):
        mask = u32(frame, 54 + h)
        if n and (mask & ((1 << n) - 1)) == ((1 << n) - 1):
            head = h
            break
    tail, best_err = 16, float("inf")
    for t in (16, 32):
        pack = u32(frame, 118 + t) * 0.001
        if n and 2.5 * n <= pack <= 4.3 * n:
            err = abs(pack - cell_sum)
            if err < best_err:
                tail, best_err = t, err
    return head, tail


def decode_cell_info(frame: bytes, layout: tuple[int, int] | None = None) -> dict:
    """Decode a type-0x02 cell-info frame (JK02 family, dual-shift layout aware)."""
    head, tail = layout if layout is not None else detect_layout(frame)
    cells = 24 + (tail // 2)
    n_slots = min(cells, 24)
    voltages = [u16(frame, 6 + i * 2) * 0.001 for i in range(n_slots)]
    resistances = [u16(frame, 64 + i * 2 + head) * 0.001 for i in range(n_slots)]
    enabled_mask = u32(frame, 54 + head)
    live = [i for i in range(n_slots) if (enabled_mask >> i) & 1 and voltages[i] > 1.0]
    if not live:
        live = [i for i in range(n_slots) if voltages[i] > 1.0]
    live_v = [voltages[i] for i in live]

    t = tail
    total_voltage = u32(frame, 118 + t) * 0.001
    current = s32(frame, 126 + t) * 0.001

    errors_mask = u32(frame, 134 + t)
    balancing = frame[140 + t] != 0

    result = {
        "layout": {"head_shift": head, "tail_shift": tail},
        "cell_count_active": len(live),
        "cell_count_config": cells,
        "enabled_mask": enabled_mask,
        "voltages": [round(v, 3) for v in voltages],
        "resistances": [round(r, 3) for r in resistances],
        "min_cell": (min(live, key=lambda i: voltages[i]) + 1) if live else None,
        "max_cell": (max(live, key=lambda i: voltages[i]) + 1) if live else None,
        "min_voltage": round(min(live_v), 4) if live else None,
        "max_voltage": round(max(live_v), 4) if live else None,
        "avg_voltage": round(sum(live_v) / len(live_v), 4) if live else None,
        "delta_voltage": round(max(live_v) - min(live_v), 4) if live else None,
        "pack_voltage": round(total_voltage, 3),
        "current": round(current, 3),
        # signed power derived from V x I (the BMS's own P field at 122+t is
        # unsigned magnitude — wrong sign during discharge)
        "power": round(total_voltage * current, 1),
        "mosfet_temperature": round(s16(frame, 112 + t) * 0.1, 1),
        "temperature_1": round(s16(frame, 130 + t) * 0.1, 1),
        "temperature_2": round(s16(frame, 132 + t) * 0.1, 1),
        "errors_mask": errors_mask,
        "balancing_current": round(s16(frame, 138 + t) * 0.001, 3),
        "balancer_mask": frame[140 + t],
        "balancing": balancing,
        "soc": frame[141 + t],
        "soh": frame[158 + t],
        "capacity_remaining": round(u32(frame, 142 + t) * 0.001, 1),
        "capacity_full": round(u32(frame, 146 + t) * 0.001, 1),
        "cycles": u32(frame, 150 + t),
        "total_cycle_capacity": round(u32(frame, 154 + t) * 0.001, 1),
        "precharging": bool(frame[159 + t]),
        "runtime_seconds": u32(frame, 162 + t),
        "charging": bool(frame[166 + t]),
        "discharging": bool(frame[167 + t]),
        "precharge_flag": bool(frame[168 + t]),
        "heating": bool(frame[183 + t]),
        "heating_current": round(s16(frame, 204 + t) * 0.001, 3),
        "emergency_countdown": u32(frame, 242 + t),
        "smart_sleep_countdown": u32(frame, 238 + t),
        "charge_status_time_elapsed": u16(frame, 246 + t),
        "charge_status_id": frame[248 + t],
    }
    result["charge_status"] = CHARGE_STATUS.get(result["charge_status_id"], "Unknown")
    return result


def decode_settings(frame: bytes) -> dict:
    """Decode a type-0x01 settings frame (read-only view of configured limits)."""
    f32 = lambda i: u32(frame, i) * 0.001
    return {
        "smart_sleep_voltage": round(f32(6), 3),
        "uv_protection": round(f32(10), 3),
        "uv_recovery": round(f32(14), 3),
        "ov_protection": round(f32(18), 3),
        "ov_recovery": round(f32(22), 3),
        "balance_trigger_voltage": round(f32(26), 3),
        "soc100_voltage": round(f32(30), 3),
        "soc0_voltage": round(f32(34), 3),
        "charge_request_voltage": round(f32(38), 3),
        "float_request_voltage": round(f32(42), 3),
        "power_off_voltage": round(f32(46), 3),
        "max_charge_current": round(f32(50), 2),
        "charge_oc_delay": u32(frame, 54),
        "charge_oc_recovery_time": u32(frame, 58),
        "max_discharge_current": round(f32(62), 2),
        "discharge_oc_delay": u32(frame, 66),
        "discharge_oc_recovery_time": u32(frame, 70),
        "short_circuit_recovery_time": u32(frame, 74),
        "max_balance_current": round(f32(78), 3),
        "charge_overtemp_protection": round(s32(frame, 82) * 0.1, 1),
        "charge_overtemp_recovery": round(s32(frame, 86) * 0.1, 1),
        "discharge_overtemp_protection": round(s32(frame, 90) * 0.1, 1),
        "discharge_overtemp_recovery": round(s32(frame, 94) * 0.1, 1),
        "charge_undertemp_protection": round(s32(frame, 98) * 0.1, 1),
        "charge_undertemp_recovery": round(s32(frame, 102) * 0.1, 1),
        "mosfet_overtemp_protection": round(s32(frame, 106) * 0.1, 1),
        "mosfet_overtemp_recovery": round(s32(frame, 110) * 0.1, 1),
        "cell_count": frame[114],
        "switch_charging": bool(frame[118]),
        "switch_discharging": bool(frame[122]),
        "switch_balancer": bool(frame[126]),
        "total_capacity": round(f32(130), 1),
        "short_circuit_delay": u32(frame, 134),
        "balance_start_voltage": round(f32(138), 3),
        "precharge_time": frame[274],
        "switch_heating": bool(frame[282] & 0x01),
        "switch_disable_temp_sensors": bool(frame[282] & 0x02),
        "switch_display_always_on": bool(frame[282] & 0x10),
        "switch_smart_sleep": bool(frame[282] & 0x40),
        "switch_disable_pcl": bool(frame[282] & 0x80),
        "switch_timed_stored_data": bool(frame[283] & 0x01),
        "switch_float_mode": bool(frame[283] & 0x02),
        "heating_start_temp": int.from_bytes(bytes([frame[284]]), "little", signed=True),
        "heating_stop_temp": int.from_bytes(bytes([frame[285]]), "little", signed=True),
        "smart_sleep_delay": frame[286],
        "discharge_undertemp_protection": int.from_bytes(bytes([frame[296]]), "little", signed=True),
        "discharge_undertemp_recovery": int.from_bytes(bytes([frame[297]]), "little", signed=True),
    }


def detect_offset(frame: bytes) -> int:
    """Auto-detect JK02 frame variant: 24S (offset 0) vs 32S (offset 16)."""
    for offset in (0, 16):
        cells = 24 + (offset // 2)
        mask = u32(frame, 54 + offset)
        live = [i for i in range(cells) if (mask >> i) & 1]
        if live and all(1.0 < u16(frame, 6 + i * 2 + offset) * 0.001 < 5.0 for i in live[:8]):
            return offset
    return 0
