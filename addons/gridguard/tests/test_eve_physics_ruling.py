"""Owner PHYSICS ruling 2026-10-26 (§19 + §20 refinement) — eve available is
amp-hour based, never the mid-range voltage snapshot.

THE INCIDENT (handoff §18, live 2026-10-26): the first fully-truthful eve wrote
10 programmings in 45 min with ZERO convergence (fire ladder 04:57/07:27/04:21/
03:29/05:25/05:59/05:13/06:41/03:43/03:10). Root cause: available_wh was a
per-run SNAPSHOT of the corrected-voltage SOC — a cycling compressor night load
sagged min_cell 3.269 vs 3.315 rested (46 mV), the LiFePO4 plateau turned that
into a huge sampled-SOC swing → ~0.9 kWh available-swing → at the ~200 W
modeled rate hours of crossing chaos. The two-learners disagreement was live
too: available_bms 1082 Wh vs voltage 925 Wh (157 Wh).

THE RULING (§19, refined §20): available = the BMS counter (remaining_ah — the
BMS's own internal amp-integration, restart-proof by construction) × V_avg, a
cheap voltage AVERAGE for the Wh conversion only. The corrected-voltage SOC
path is REMOVED from the eve's mid-range available; voltage keeps ONLY its
designed home — the deep/knee band (floor_wh + emergency/ah_tief, byte-
untouched, regression-pinned here) which is also the safety net for the
deliberate below-counter discharge strategy (§20: we WILL drive the pack below
the BMS-reported Ah to force its AH learning; the knee/deep machinery guards).
Plus the write DEADBAND: reprogram only on > 20 min crossing movement
(WRITE_DEADBAND_MIN, owner-approved 2026-10-26).
"""
from datetime import datetime, timedelta

import pytest

from gridguard.config import load_config
from gridguard.main import WRITE_DEADBAND_MIN
from gridguard.verdict import deep_floor_hit, soc_pct_from_v, voltage_floor_hit
from test_main_tick import D, FULL_AH, NEED300, PACK_V, FakeDeps, buckets
from gridguard.main import AppState, tick

# --- real incident constants (handoff §18 / brief) -----------------------------
NEED_WH = 2428.35                       # learned CLEAN-median night need (live)
LADDER = ["04:57", "07:27", "04:21", "03:29", "05:25",      # the 10 writes of
          "05:59", "05:13", "06:41", "03:43", "03:10"]      # 19:46Z..20:31Z
AVAIL_V_SNAP = 925.0                    # voltage-model available (live audit)
AVAIL_BMS = 1082.0                      # counter line (live audit, 157 Wh off)
SAG_LOADED_V = 3.269                    # min cell under the compressor load
SAG_RESTED_V = 3.315                    # … vs the rested/corrected value (46 mV)
PACK_V_REAL = 13.064                    # the real 4S pack voltage (observer §4)
RATE_W = NEED_WH / 11.0                 # modeled rate: safety 1.0 × need ÷ 11 h
                                         # ≈ 220.8 W („~200 W" in the ruling)

# incident cfg: host .env truth (SAFETY_FACTOR=1.0), the fixture uses the real
# pack voltage — the incident numbers must run on the incident arithmetic
INC = {"TZ": "Europe/Madrid", "WINDOW_START": "09:00", "WINDOW_END": "22:00",
       "EVE_START": "20:00", "SAFETY_FACTOR": "1.0",
       "OPENSEARCH_URL": "http://x", "BMS_INDEX": "i", "EWCLOUD_EMAIL": "a",
       "EWCLOUD_PASS": "b", "SW_MAIN_DEVICEID": "d", "SW_MAIN_DEVICEKEY": "k",
       "SW_MAIN_IP": "1.2.3.4", "NTFY_WEBHOOK": "https://n/t"}
inc_cfg = load_config(INC)
NEED2428 = buckets(NEED_WH, NEED_WH)   # CLEAN median → 2428.35 Wh

FLOOR_WH_INC = soc_pct_from_v(inc_cfg.true_floor_v) / 100.0 * FULL_AH * PACK_V_REAL


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _crossing(available_wh: float, now: datetime, night_len: float = 11.0,
              need: float = NEED_WH, safety: float = 1.0,
              floor_wh: float = FLOOR_WH_INC) -> datetime:
    """The removed planner crossing math, mirrored (t_fire = now + head/rate) —
    used to REPRODUCE the incident from the observed numbers, never as logic."""
    rate = need * safety / max(1.0, night_len)
    return now + timedelta(hours=max(0.0, available_wh - floor_wh) / rate)


# --- (fixture truth) the real incident pins ------------------------------------
def test_incident_ladder_and_sag_numbers_are_pinned():
    """The red fixture IS tonight's incident — these constants come from the
    live audit trail, not from invented test data."""
    spread = max(map(_minutes, LADDER)) - min(map(_minutes, LADDER))
    assert spread == 257                       # 03:10 → 07:27 = 4 h 17 min
    assert len(LADDER) == 10                   # 10 writes in 45 min, zero convergence
    assert round(SAG_RESTED_V - SAG_LOADED_V, 3) == 0.046   # the 46 mV sag pair
    assert round(AVAIL_BMS - AVAIL_V_SNAP, 1) == 157.0      # two-learners live
    assert WRITE_DEADBAND_MIN == 20            # owner-approved 2026-10-26
    # the incident spread dwarfs the deadband by an order of magnitude
    assert spread > 10 * WRITE_DEADBAND_MIN


def test_removed_voltage_path_bounces_the_crossing_by_hours():
    """RED EVIDENCE (why the soc_pct_v path is gone): the observed sag pair fed
    through the removed snapshot formula (soc_pct_from_v × full_ah × pack_v)
    swings the crossing by HOURS at the real modeled rate. (The IR correction
    under-corrected the compressor dynamics — tonight's evidence: the 925-vs-
    1082 disagreement and the ladder itself; the fixture feeds the OBSERVED
    sampled pair.)"""
    avail_rested = soc_pct_from_v(SAG_RESTED_V) / 100.0 * FULL_AH * PACK_V_REAL
    avail_loaded = soc_pct_from_v(SAG_LOADED_V) / 100.0 * FULL_AH * PACK_V_REAL
    assert avail_rested - avail_loaded > 600.0             # ~0.68 kWh swing
    now = D(20, 10)
    c_hi, c_lo = _crossing(avail_rested, now), _crossing(avail_loaded, now)
    spread_min = (c_hi - c_lo).total_seconds() / 60.0
    assert spread_min >= 180.0                             # ≥ 3 h of chaos
    assert spread_min >= 9 * WRITE_DEADBAND_MIN            # … 9× the deadband


# --- (a) THE RED: load cycle → monotone available, crossing inside deadband ----
def test_load_cycle_available_monotone_and_crossing_converges():
    """Synthetic compressor duty on the incident fixture: current alternates
    0 / −9 A on a 5-min grid, min cell sags 3.315 ↔ 3.269 (46 mV), the counter
    integrates the amps (−0.75 Ah per loaded 5 min). The eve ticks every 5 min
    over the observed 45-min ladder window (10 ticks). NEW truth: available =
    remaining_ah × V_avg — monotone descent, consecutive crossing movement
    inside the deadband. FAILS on the old snapshot code (oscillating SOC)."""
    st = AppState(); st.morning_done = True
    deps = FakeDeps(min_v=SAG_RESTED_V, charge=0.0, remaining_ah=82.83,
                    switch="off", inventory_timers=[], buckets=NEED2428,
                    pack_v=PACK_V_REAL, v_avg=13.05)
    crossings, availables, old_crossings = [], [], []
    for i in range(10):
        loaded = i % 2 == 0                       # 5-min duty grid
        deps.min_v = SAG_LOADED_V if loaded else SAG_RESTED_V
        deps.charge = -9.0 if loaded else 0.0
        deps.remaining_ah = 82.83 - 0.75 * ((i + 1) // 2)   # the counter integrates
        now = D(20, 1) + timedelta(minutes=5 * i)
        tick(inc_cfg, st, deps, now)
        night = [f for e, f in deps.audits if e == "night_need"][-1]["inputs"]
        availables.append(night["available_wh"])
        crossings.append(deps.plan_seen[-1][1][0][1]["at_local"])
        # the REMOVED path on the same fixture (red evidence, mirrored formula).
        # The OBSERVED sampled pair stands for the sampled corrected series:
        # tonight's steady-state IR correction under-corrected the compressor
        # dynamics (evidence: the 925-vs-1082 disagreement + the ladder spread
        # itself), so the raw sag pair is what the removed snapshot consumed.
        old = soc_pct_from_v(deps.min_v) / 100.0 * FULL_AH * PACK_V_REAL
        old_crossings.append(_crossing(old, now, need=NEED_WH))
    # NEW: monotone non-increasing available (real gentle descent = residual drift)
    assert all(b <= a for a, b in zip(availables, availables[1:]))
    assert availables[-1] < availables[0]                  # the night did drain
    # NEW: the crossing stays inside the deadband, tick to tick …
    moves = [(abs((b - a).total_seconds()) / 60.0)
             for a, b in zip(crossings, crossings[1:])]
    assert max(moves) <= WRITE_DEADBAND_MIN
    # … and over the whole ladder window (the incident drifted hours)
    assert abs((crossings[-1] - crossings[0]).total_seconds()) / 60.0 \
        <= 35.0
    # OLD path on the SAME fixture: hours of crossing chaos (the red evidence)
    old_spread = (max(old_crossings) - min(old_crossings)).total_seconds() / 60.0
    assert old_spread >= 180.0


def test_the_sag_itself_never_moves_available_at_a_fixed_counter():
    """The incident in one line: identical counter, only the voltage sagging —
    available must not twitch (the removed path swung ~0.68 kWh on this pair)."""
    st = AppState(); st.morning_done = True
    deps = FakeDeps(min_v=SAG_RESTED_V, charge=0.0, remaining_ah=82.83,
                    switch="off", inventory_timers=[], buckets=NEED2428,
                    pack_v=PACK_V_REAL, v_avg=13.05)
    tick(inc_cfg, st, deps, D(20, 1))
    a_rest = [f for e, f in deps.audits if e == "night_need"][-1]["inputs"]["available_wh"]
    deps.min_v, deps.charge = SAG_LOADED_V, -9.0           # the compressor kicks in
    tick(inc_cfg, st, deps, D(20, 6))
    a_sag = [f for e, f in deps.audits if e == "night_need"][-1]["inputs"]["available_wh"]
    assert a_rest == a_sag == 82.83 * 13.05                # counter × V_avg, exact


# --- (b) the Ah-anchor math (§20: the counter IS the anchor) — exact numbers ----
def test_available_is_counter_times_voltage_average_exact_numbers():
    st = AppState(); st.morning_done = True
    deps = FakeDeps(min_v=3.30, charge=0.0, remaining_ah=12.0, switch="off",
                    inventory_timers=[], buckets=NEED300,
                    pack_v=PACK_V_REAL, v_avg=13.05)
    tick(cfg := load_config({"TZ": "Europe/Madrid", "SAFETY_FACTOR": "1.2",
                             "OPENSEARCH_URL": "http://x", "BMS_INDEX": "i",
                             "EWCLOUD_EMAIL": "a", "EWCLOUD_PASS": "b",
                             "SW_MAIN_DEVICEID": "d", "SW_MAIN_DEVICEKEY": "k",
                             "SW_MAIN_IP": "1.2.3.4", "NTFY_WEBHOOK": "https://n/t"}),
         st, deps, D(20, 10))
    night = [f for e, f in deps.audits if e == "night_need"][-1]["inputs"]
    assert night["available_wh"] == pytest.approx(156.6)    # 12.0 Ah × 13.05 V
    assert night["remaining_ah"] == 12.0                    # the anchor, disclosed
    assert night["v_avg"] == 13.05                          # the conversion average
    assert night["available_bms_wh"] == pytest.approx(12.0 * PACK_V_REAL)
    # the crossing runs on exactly this available (floor = voltage curve — untouched)
    floor = soc_pct_from_v(cfg.true_floor_v) / 100.0 * FULL_AH * PACK_V_REAL
    rate = 300.0 * 1.2 / 11.0
    assert deps.plan_seen[-1][1][0][1]["at_local"] == \
        D(20, 10) + timedelta(hours=(12.0 * 13.05 - floor) / rate)

def test_unknown_counter_or_voltage_stays_conservative():
    """No counter or no voltage at all → available None → the ratio's
    conservative branch (keep_grid / bring_on_now), never a guessed number."""
    st = AppState(); st.morning_done = True
    deps = FakeDeps(remaining_ah=None, switch="off", inventory_timers=[],
                    buckets=NEED300, v_avg=None, pack_v=None)
    tick(load_config({"TZ": "Europe/Madrid", "SAFETY_FACTOR": "1.2",
                      "OPENSEARCH_URL": "http://x", "BMS_INDEX": "i",
                      "EWCLOUD_EMAIL": "a", "EWCLOUD_PASS": "b",
                      "SW_MAIN_DEVICEID": "d", "SW_MAIN_DEVICEKEY": "k",
                      "SW_MAIN_IP": "1.2.3.4", "NTFY_WEBHOOK": "https://n/t"}),
         st, deps, D(20, 10))
    # remaining_ah unknown → available None; the grid-off-suppression owns this
    # fixture's ON (voltage above the floor), so the honest observable is the
    # audit: available_wh must be None, never a fabricated number.
    night = [f for e, f in deps.audits if e == "night_need"][-1]["inputs"]
    assert night["available_wh"] is None


def test_v_avg_query_failure_falls_back_to_the_live_pack_voltage():
    """A failed average query is a consult side like every other: audited,
    and the LIVE pack voltage (a sensor value — doctrine: sensor data beats
    textbook constants) carries the conversion instead; both unknown → None."""
    class AvgBoom(FakeDeps):
        def pack_v_avg(self):
            raise RuntimeError("opensearch down")
    st = AppState(); st.morning_done = True
    deps = AvgBoom(remaining_ah=50.0, switch="off", inventory_timers=[],
                   buckets=NEED300, pack_v=PACK_V_REAL, v_avg=13.05)
    tick(load_config({"TZ": "Europe/Madrid", "SAFETY_FACTOR": "1.2",
                      "OPENSEARCH_URL": "http://x", "BMS_INDEX": "i",
                      "EWCLOUD_EMAIL": "a", "EWCLOUD_PASS": "b",
                      "SW_MAIN_DEVICEID": "d", "SW_MAIN_DEVICEKEY": "k",
                      "SW_MAIN_IP": "1.2.3.4", "NTFY_WEBHOOK": "https://n/t"}),
         st, deps, D(20, 10))
    assert any(e == "eve_input_unavailable" for e, _ in deps.audits)
    night = [f for e, f in deps.audits if e == "night_need"][-1]["inputs"]
    assert night["available_wh"] == 50.0 * PACK_V_REAL       # live pack_v fallback


# --- (e) restart-mid-eve: the counter carries the anchor (§20) -----------------
def test_restart_mid_eve_available_continues_from_the_counter_alone():
    """With the counter as the anchor there is NO in-memory anchor state to
    lose: a container restart mid-eve re-reads the counter and the available
    continues exactly where the physics is — no sagged re-anchor, no jump."""
    live = AppState(); live.morning_done = True
    deps = FakeDeps(min_v=3.30, remaining_ah=80.0, switch="off",
                    inventory_timers=[], buckets=NEED300)
    tick(load_config({"TZ": "Europe/Madrid", "SAFETY_FACTOR": "1.2",
                      "OPENSEARCH_URL": "http://x", "BMS_INDEX": "i",
                      "EWCLOUD_EMAIL": "a", "EWCLOUD_PASS": "b",
                      "SW_MAIN_DEVICEID": "d", "SW_MAIN_DEVICEKEY": "k",
                      "SW_MAIN_IP": "1.2.3.4", "NTFY_WEBHOOK": "https://n/t"}),
         live, deps, D(20, 40))
    long_running = [f for e, f in deps.audits if e == "night_need"][-1]["inputs"]
    restarted = AppState(); restarted.morning_done = True    # fresh process, 20:45
    tick(load_config({"TZ": "Europe/Madrid", "SAFETY_FACTOR": "1.2",
                      "OPENSEARCH_URL": "http://x", "BMS_INDEX": "i",
                      "EWCLOUD_EMAIL": "a", "EWCLOUD_PASS": "b",
                      "SW_MAIN_DEVICEID": "d", "SW_MAIN_DEVICEKEY": "k",
                      "SW_MAIN_IP": "1.2.3.4", "NTFY_WEBHOOK": "https://n/t"}),
         restarted, deps, D(20, 45))
    after_restart = [f for e, f in deps.audits if e == "night_need"][-1]["inputs"]
    assert after_restart["available_wh"] == long_running["available_wh"] \
        == 80.0 * PACK_V                                   # identical instrument


# --- relearn conservatism (§19/§20): the counter speaks, the knee guards -------
def test_zero_counter_is_conservative_never_a_voltage_guess():
    """Relearn/reset artifact (observer order #2): remaining_ah = 0 is a RESET,
    not emptiness — available 0 makes the ratio conservatively NOT ok (the
    safe direction, grid on); the mid-range voltage must never resurrect a
    guessed available in between (knee/deep machinery owns the below-zero
    region, see the regression pins below)."""
    st = AppState(); st.morning_done = True
    deps = FakeDeps(min_v=3.30, remaining_ah=0.0, switch="off",
                    inventory_timers=[], buckets=NEED300)
    tick(load_config({"TZ": "Europe/Madrid", "SAFETY_FACTOR": "1.2",
                      "OPENSEARCH_URL": "http://x", "BMS_INDEX": "i",
                      "EWCLOUD_EMAIL": "a", "EWCLOUD_PASS": "b",
                      "SW_MAIN_DEVICEID": "d", "SW_MAIN_DEVICEKEY": "k",
                      "SW_MAIN_IP": "1.2.3.4", "NTFY_WEBHOOK": "https://n/t"}),
         st, deps, D(20, 10))
    night = [f for e, f in deps.audits if e == "night_need"][-1]["inputs"]
    assert night["available_wh"] == 0.0
    from gridguard.planner import ratio_ok
    assert ratio_ok(night["available_wh"], 300.0,
                    load_config({"TZ": "Europe/Madrid", "SAFETY_FACTOR": "1.2",
                                 "OPENSEARCH_URL": "http://x", "BMS_INDEX": "i",
                                 "EWCLOUD_EMAIL": "a", "EWCLOUD_PASS": "b",
                                 "SW_MAIN_DEVICEID": "d", "SW_MAIN_DEVICEKEY": "k",
                                 "SW_MAIN_IP": "1.2.3.4",
                                 "NTFY_WEBHOOK": "https://n/t"})) is False


# --- (d) deep-floor + emergency/ah_tief byte-regression pins (§19/§20) ---------
# Voltage keeps ONLY its designed home: the deep/knee band — the floors and the
# emergency machinery are the SAFETY NET beneath the counter instrument (the
# deliberate below-counter discharge strategy, §20, drives into exactly this
# band). These pins are BYTE-faithful to the pre-wave behavior.
def test_deep_emergency_byte_regression_pin():
    st, deps = AppState(), FakeDeps(min_v=2.89, switch="off")
    tick(load_config({"TZ": "Europe/Madrid", "SAFETY_FACTOR": "1.2",
                      "OPENSEARCH_URL": "http://x", "BMS_INDEX": "i",
                      "EWCLOUD_EMAIL": "a", "EWCLOUD_PASS": "b",
                      "SW_MAIN_DEVICEID": "d", "SW_MAIN_DEVICEKEY": "k",
                      "SW_MAIN_IP": "1.2.3.4", "NTFY_WEBHOOK": "https://n/t"}),
         st, deps, D(14, 0))
    assert deps.acts == [("disable_on_timers", None, None, False, None),
                         ("switch_on", None, None, True, None)]
    ev = [f for e, f in deps.audits if e == "emergency_on"]
    assert ev and ev[0]["result"] == "ok" and ev[0]["action"] == {"type": "switch_on"}
    assert ev[0]["inputs"] == {"min_cell_v": 2.89, "min_cell_corr": 2.89,
                               "floor_v": 3.15, "remaining_ah": 100.0,
                               "charge_a": 0.0, "timer_clear": True}
    assert deps.pushes[0][0] == "EMERGENCY"


def test_low_capacity_ah_tief_byte_regression_pin():
    st, deps = AppState(), FakeDeps(min_v=3.14, remaining_ah=4.5, switch="off")
    tick(load_config({"TZ": "Europe/Madrid", "SAFETY_FACTOR": "1.2",
                      "OPENSEARCH_URL": "http://x", "BMS_INDEX": "i",
                      "EWCLOUD_EMAIL": "a", "EWCLOUD_PASS": "b",
                      "SW_MAIN_DEVICEID": "d", "SW_MAIN_DEVICEKEY": "k",
                      "SW_MAIN_IP": "1.2.3.4", "NTFY_WEBHOOK": "https://n/t"}),
         st, deps, D(21, 0))
    assert [a[0] for a in deps.acts] == ["disable_on_timers", "switch_on"]
    ev = [f for e, f in deps.audits if e == "low_capacity_on"]
    assert ev and ev[0]["inputs"] == {"min_cell_v": 3.14, "min_cell_corr": 3.14,
                                      "floor_v": 3.15, "remaining_ah": 4.5,
                                      "charge_a": 0.0, "timer_clear": True}
    assert deps.pushes[0][0] == "LOW BATTERY"


def test_knee_floor_catches_a_lying_high_counter():
    """The §19 pairing in action: the knee floor remains the SAFETY NET for a
    bad relearn anchor — a counter lying HIGH (100 Ah) cannot hold the pack in
    the deep band; the corrected cell voltage decides, the counter is disclosed."""
    st, deps = AppState(), FakeDeps(min_v=3.14, remaining_ah=100.0, switch="off")
    tick(load_config({"TZ": "Europe/Madrid", "SAFETY_FACTOR": "1.2",
                      "OPENSEARCH_URL": "http://x", "BMS_INDEX": "i",
                      "EWCLOUD_EMAIL": "a", "EWCLOUD_PASS": "b",
                      "SW_MAIN_DEVICEID": "d", "SW_MAIN_DEVICEKEY": "k",
                      "SW_MAIN_IP": "1.2.3.4", "NTFY_WEBHOOK": "https://n/t"}),
         st, deps, D(21, 0))
    assert [a[0] for a in deps.acts] == ["disable_on_timers", "switch_on"]
    ev = [f for e, f in deps.audits if e == "low_capacity_on"]
    assert ev and ev[0]["inputs"]["remaining_ah"] == 100.0    # disclosed, not trusted


def test_voltage_floor_truth_table_untouched():
    """The floor functions are byte-untouched by this wave — pin the ruling
    boundaries (§19: voltage keeps ONLY the deep/knee band)."""
    c = load_config({"TZ": "Europe/Madrid", "SAFETY_FACTOR": "1.2",
                     "OPENSEARCH_URL": "http://x", "BMS_INDEX": "i",
                     "EWCLOUD_EMAIL": "a", "EWCLOUD_PASS": "b",
                     "SW_MAIN_DEVICEID": "d", "SW_MAIN_DEVICEKEY": "k",
                     "SW_MAIN_IP": "1.2.3.4", "NTFY_WEBHOOK": "https://n/t"})
    assert deep_floor_hit(2.90, c) is True            # ≤ thresh_deep
    assert deep_floor_hit(2.91, c) is False
    assert deep_floor_hit(None, c) is False           # unknown = no hit (IN-guard)
    assert voltage_floor_hit(3.15, 0.0, c) is True    # corrected ≤ TRUE_FLOOR_V
    assert voltage_floor_hit(3.16, 0.0, c) is False
    assert voltage_floor_hit(None, 0.0, c) is True    # unknown = conservative hit
    # the IR correction path is unchanged: 3.234 V at −7 A → 3.269 rested (5 mΩ)
    assert voltage_floor_hit(3.234, -7.0, c) is False
    assert voltage_floor_hit(3.114, -7.0, c) is True   # 3.114 + 35 mV = 3.149 ≤ 3.15


# --- owner ruling family 2026-10-26 (§23 → WITHDRAWN in §24) ------------------
# The within-night realized-drain rate adoption was REJECTED by the owner: an
# in-night span-average is biased by WHERE in the load shape it samples. The
# prediction method stays: LEARNED whole-night energy ÷ night length = the
# mean rate ("NOTHING SHOULD CHANGE HERE"); the honest rate correction
# happens ACROSS nights via the existing CLEAN-night learning. What REMAINS of
# the depth expectation is proven below on the EXISTING wiring (zero code).
A0_WH = 1560.0                         # dusk available (counter × V_avg) fixture
FLOOR_A0 = soc_pct_from_v(inc_cfg.true_floor_v) / 100.0 * FULL_AH * PACK_V_REAL


def test_tier_zero_keeps_no_hidden_reserve_and_a_tier_raises_the_floor():
    """PROVE-ONLY (existing behavior, §23/§24): reserve tier 0 → the planner
    receives NO reserve floor (the deep voltage band stays the only floor =
    the deepest sanctioned point); a latched tier > 0 raises the floor through
    the EXISTING max() path (planner-pinned). Zero code change."""
    # tier 0: the crossing uses ONLY the voltage-curve floor …
    st = AppState(); st.morning_done = True; st.reserve_tier_pct = 0
    deps = FakeDeps(min_v=3.30, remaining_ah=A0_WH / 13.05, switch="off",
                    inventory_timers=[], buckets=NEED2428,
                    pack_v=PACK_V_REAL, v_avg=13.05)
    tick(inc_cfg, st, deps, D(20, 0))
    at0 = deps.plan_seen[-1][1][0][1]["at_local"]
    assert (at0 - D(20, 0)).total_seconds() == pytest.approx(
        (A0_WH - FLOOR_A0) / (NEED_WH / 11.0) * 3600.0, abs=1e-3)  # curve floor only
    # … and a latched 30 % tier raises the floor via the existing path: the
    # head above the raised band shrinks → the crossing moves EARLIER (toward
    # the band), never deeper than the reserve.
    st2 = AppState(); st2.morning_done = True; st2.reserve_tier_pct = 30
    deps2 = FakeDeps(min_v=3.30, remaining_ah=A0_WH / 13.05, switch="off",
                     inventory_timers=[], buckets=NEED2428,
                     pack_v=PACK_V_REAL, v_avg=13.05)
    tick(inc_cfg, st2, deps2, D(20, 0))
    at30 = deps2.plan_seen[-1][1][0][1]["at_local"]
    tier_floor = 0.30 * FULL_AH * PACK_V_REAL              # 30 % band × capacity × V
    assert (at30 - D(20, 0)).total_seconds() == pytest.approx(
        (A0_WH - tier_floor) / (NEED_WH / 11.0) * 3600.0, abs=1e-3)
    assert at30 < at0                                       # the tier pulled it in
