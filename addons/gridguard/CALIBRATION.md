# GridGuard calibration — status

- Measured: 2026-10-17 with `tools/calibrate.py` (prod index state: ingestion started 2026-10-16T20:19Z)
- Table (as produced by the run; the v1 `CHARGE_MIN_A` hint inside it is SUPERSEDED — see below):

```
date       maxChargeA radiationMJ/m2
2026-10-17       0.00    12.07
pick CHARGE_MIN_A just above bedeckt-morning peaks; WEATHER_MIN_J in J/m² (e.g.3_000_000 for a weak day), leave weather empty to disable
```

- Finding: index held ~1 day of data; morning 09–12 was discharge-only (no positive charge samples) → no percentiles possible.
- Owner decision (2026-10-17): DEFER — thresholds stay unset.
- CHARGE_MIN_A: **SUPERSEDED by SOLAR_\*** (v2, 2026-10-18) — dropped as a pre-LIVE gate; the v2 morning OFF path is the BMS-measured `SOLAR_OFF_A`/`SOLAR_HOLD_S` (plus `SOC_OFF_PCT`), no re-measure duty remains for `CHARGE_MIN_A`.
- WEATHER_MIN_J: **BOOTSTRAP SET** = `8000000` (8MJ≈2.2kWh) since19.10, LIVE checked from20.10 — v4 §12.1 release-GATE: it may only PREVENT a release (grid stays on), never force an action; empty/unset = conservative hold (an empty MIN_J gates the release shut, it does NOT "disable" the weather check). **unit contract (20.10): `radiation_sum_j` = real J/m²** — open-meteo `shortwave_radiation_sum` = MJ/day ×1e6 (plausibility roof50MJ → None = hold); careful when comparing: `tools/calibrate.py` column "radiationMJ/m2" = raw/MJ view, weather-index docs = JOULE (÷1e6). Calibrate the bootstrap against the actual measurement series (owner design: forecast vs. actual days) before the number counts as final.

## v2 (2026-10-18)

v2 gates ship as **engineering defaults — CALIBRATABLE**, owner re-measure
before LIVE. Defer status unchanged: **no live values until re-measured.**

| Key | Default | Status |
|---|---|---|
| `AH_ON` | `5` | CALIBRATABLE (engineering default, re-measure before LIVE) |
| `AH_OFF` | `10` | CALIBRATABLE (engineering default, re-measure before LIVE) |
| `SOLAR_OFF_A` | `10` | CALIBRATABLE (engineering default, re-measure before LIVE) |
| `SOLAR_HOLD_S` | `300` | CALIBRATABLE (engineering default, re-measure before LIVE) |
| `SOLAR_NOISE_A` | `2.0` | CALIBRATABLE (engineering default, re-measure before LIVE) |
| `SOC_OFF_PCT` | `50` | CALIBRATABLE (engineering default, re-measure before LIVE) |
| `NIGHT_LOAD_A` | `12` | CALIBRATABLE (engineering default, measured ≈12 A, re-measure before LIVE) |

- `THRESH_DEEP=2.90` is the **Safety-Default** (deep floor) — never calibrated without the owner.
- Evidence tool: `tools/calibrate.py` v2 columns per day — max positive `pack.current` (morning 09:00–12:00), min `remaining_ah` reached over 24 h, hours below `AH_ON` (reads `AH_ON` from env, default `5`), plus the daily radiation sum.
- Pre-LIVE duty (unchanged): re-run `tools/calibrate.py` after ≥14 real days and pick from the v2 columns; first run 2026-10-17 still counts only ~1 day of history → DEFER stands.

## v3 (2026-10-18)

v3 replaces the planner's fixed constants with **runtime-learned parameters**
(spec R3''): night load (median of qualifying night discharge) and the array
sun-run (`array_onset_h`/`array_offset_h`) are re-learned **every day** from
the rolling `HIST_DAYS` window (default 7) — the learned values are visible in
the JSONL journal (`night_learned` audit event), not in `.env`. Fixed
constants are **dead**: `NIGHT_LOAD_A` (the v2 table row above is historical)
and `RESCUE_AT` (fire time = computed `t_fire`) — no calibration duty remains
for either; the v2 gate defaults in the table stay as they are — `AH_ON` and
`SOC_OFF_PCT` are no longer read by the runtime.

## v4 (2026-10-18)

v4 replaces the amperage interpretation with **energy**: the night need is the
**cluster-integrated Wh** per night (spec §3 — one bucket per night window,
discharge side only), classified by quality (§4: only CLEAN buckets — a full
night with grid off — feed the need; MIXED/GRID_ON/SHORT/OPEN are excluded
because a half-run night distorts the need downward). The evening decision is
the single ratio `available ≥ SAFETY_FACTOR × need` (§3). **`SAFETY_FACTOR`
(default 1.2) is the recalibratable margin** — the owner's own thought for a
future feedback rule (spec §7.2: too-tight mornings → raise it, persistently
overfull with unused sun → lower it, bounded and journaled). The nightly
learned values (`need_wh`, CLEAN count, available Wh, safety) land in the
JSONL (`night_need` audit event), not in `.env`.

Since round C (spec §10b) the **Decision-Index** is live: every JSONL audit
line is dual-written to `gridguard-events-YYYY.MM.DD` (local date), the jsonl
stays the local truth and `EVENTS_INDEX=0` is the kill-switch.
