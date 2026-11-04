"""§27 NIGHT PLAN "final" notification fix (owner report 2026-10-27, screenshot):
the final push must fire at the eve's CLOSING — the first tick at/after
window_end, when no further timer write for tonight is possible — and it must
carry the CONCRETE write-confirmed armed time, with the plan ideal LABELLED
when it differs.

THE INCIDENT (this file IS the fixture): the old trigger (first final-phase
tick, main.py pre-§27) pushed at 21:56 with 05:33; the eve then wrote 04:38
(21:57), 05:52 (21:58) and 04:11 (21:59:58) — the phone showed 05:33 while
the device was armed to 04:11 (page last-wins correct, push stale).

Two truths ride the eve (owner contract): the WRITE-CONFIRMED armed (device
truth — verified write / identical program / within-deadband keep) and the
PLAN IDEAL (the planner's crossing, re-projected every tick). The payload's
planned_on_at = the LAST HH:MM in the newest NIGHT PLAN push text
(web_payload.py:961-963) → the ideal must ride LAST in the push text, so the
page keeps its "plan ideal" label semantics byte-for-byte.

F1 (review of 42adc55): RealDeps.push journals that text CAPPED at 60 chars
(main.py push — the secret guard) and the audit dual-write ships the CAPPED
rec into the events index the payload parses — so every night_plan* variant
must also survive the full chain render → [:60] → payload regex (the teeth
test at the bottom; it is the pin that would have caught F1).
"""
import re
from datetime import datetime, timedelta

import pytest

from gridguard.config import load_config
from gridguard.main import AppState, RealDeps, restore_phase_flags, tick
from gridguard.messages import msg

from test_main_tick import (D, FLOOR_WH, GOOD, FakeDeps, NEED300, PACK_V, V400,
                            _StubAudit, _StubBms, _StubCloud, _StubLan, cfg)

CLOSING = datetime(2026, 10, 18, 22, 0, 58)      # first tick >= window_end (22:00)


def _fire(now: datetime, hhmm: str) -> datetime:
    """The planner target for a ladder time: its next occurrence (all ladder
    times are next-morning) + 30 s so strftime('%H:%M') floors onto the exact
    ladder minute regardless of the float rounding inside head/rate."""
    h, m = (int(x) for x in hhmm.split(":"))
    d = now.date() + timedelta(days=1 if h * 60 + m <= now.hour * 60 + now.minute else 0)
    return datetime(d.year, d.month, d.day, h, m) + timedelta(seconds=30)


def _ah_for(now: datetime, target: datetime) -> float:
    """Craft the BMS counter (remaining_ah) so the REAL planner's crossing
    t_fire = now + (available − floor) ÷ rate lands at `target` — the fixture
    drives the production arithmetic; the SUT is never mocked."""
    rate = 300.0 * cfg.safety_factor / 11.0       # need × safety ÷ 11 h (INSUFF edges)
    hours = (target - now).total_seconds() / 3600.0
    return (FLOOR_WH + rate * hours) / PACK_V


def _finals(deps):
    """Every NIGHT PLAN push whose text is the final summary."""
    return [t for p, t in deps.pushes if p == "NIGHT PLAN" and "final" in t]


def _eve_deps(**kw):
    kw.setdefault("min_v", V400)
    kw.setdefault("switch", "off")
    kw.setdefault("inventory_timers", [])
    kw.setdefault("buckets", NEED300)
    return FakeDeps(**kw)


def _payload_planned_on_at(journaled: str):
    """The payload's plan-ideal extraction, byte-mirrored from
    web_payload.py:961-963: the LAST HH:MM of the journaled push text;
    "no timer" anywhere in it → null (the absence rule)."""
    m = re.findall(r"\b(\d{1,2}:\d{2})\b", journaled)
    return m[-1] if m and "no timer" not in journaled else None


def _real_deps(monkeypatch):
    """RealDeps with test_main_tick's in-memory client stubs — the REAL push
    path: ntfy captured (never a real call), journal = deps.audit_log.events."""
    monkeypatch.setattr("gridguard.main.CloudClient", _StubCloud)
    monkeypatch.setattr("gridguard.main.LanClient", _StubLan)
    monkeypatch.setattr("gridguard.main.BmsSource", _StubBms)
    monkeypatch.setattr("gridguard.audit.AuditLog", _StubAudit)
    pushed = []
    monkeypatch.setattr("gridguard.main.ntfy_push",
                        lambda webhook, title, text: pushed.append((title, text)))
    deps = RealDeps(load_config(GOOD))
    deps.pushes = pushed
    return deps


# --- (a) TIMING: the incident ladder as the fixture ----------------------------
LADDER = [(D(21, 56), "05:33"), (D(21, 57), "04:38"), (D(21, 58), "05:52"),
          (datetime(2026, 10, 18, 21, 59, 58), "04:11")]


def test_no_final_push_before_the_last_write_settled_armed_at_closing():
    # RED on the old code: the push landed at the FIRST ladder tick (21:56)
    # with 05:33 — three writes followed before the eve closed.
    st = AppState()
    st.morning_done = True
    deps = _eve_deps()
    for i, (now, hhmm) in enumerate(LADDER):
        deps.remaining_ah = _ah_for(now, _fire(now, hhmm))
        tick(cfg, st, deps, now)
        assert _finals(deps) == [], \
            f"final push already left at ladder tick {i} ({now:%H:%M:%S}) — before the last write"
    # the ladder really programmed the incident values (last-wins, write world)
    assert st.last_planned_fire.strftime("%H:%M") == "04:11"
    assert st.plan_ideal.strftime("%H:%M") == "04:11"
    tick(cfg, st, deps, CLOSING)                       # the eve's closing: phase NIGHT
    finals = _finals(deps)
    assert len(finals) == 1                            # exactly one, after the last write
    assert "04:11" in finals[0]                        # the SETTLED armed
    for superseded in ("05:33", "04:38", "05:52"):
        assert superseded not in finals[0]             # never a superseded generation
    # equal truths → one time, no plan-ideal suffix, exactly one clock time
    assert "plan ideal" not in finals[0]
    assert len(re.findall(r"\b\d{1,2}:\d{2}\b", finals[0])) == 1
    assert finals[0].startswith("Night plan final: switch time 04:11 (write-confirmed)")


# --- (b) CONTENT: armed leads, ideal labelled when differing -------------------
class DeadbandFake(FakeDeps):
    """§19 deadband world (tonight's physics): the DEVICE keeps its last write;
    a handoff whose crossing moved within the 20-min deadband writes nothing —
    the executor reports the DEVICE truth (armed_local), while the planner
    re-projects its ideal every tick."""
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.device_armed = None

    def apply_plan(self, plan):
        self.plan_seen.append((plan.actions[0][0] if plan.actions else None,
                               list(plan.actions)))
        kind, data = plan.actions[0]
        if kind != "failsafe_timer":
            return True
        if self.device_armed is None:
            self.device_armed = data["at_local"]     # the eve's first write lands
        data["armed_local"] = self.device_armed      # deadband: device keeps ITS time
        return True


def test_final_push_leads_with_the_armed_and_labels_the_differing_plan_ideal():
    st = AppState()
    st.morning_done = True
    deps = DeadbandFake(min_v=V400, switch="off", inventory_timers=[], buckets=NEED300)
    deps.remaining_ah = _ah_for(D(20, 1), _fire(D(20, 1), "05:26"))
    tick(cfg, st, deps, D(20, 1))                    # the eve's write: device 05:26
    deps.remaining_ah = _ah_for(D(21, 56), _fire(D(21, 56), "05:40"))
    tick(cfg, st, deps, D(21, 56))                   # ideal drifts 14 min ≤ deadband → no write
    # THE DISCREPANCY (§27-A): armed = DEVICE truth (05:26), ideal = plan (05:40)
    assert st.last_planned_fire.strftime("%H:%M") == "05:26"
    assert st.plan_ideal.strftime("%H:%M") == "05:40"
    tick(cfg, st, deps, CLOSING)
    finals = _finals(deps)
    assert len(finals) == 1
    text = finals[0]
    # F1-shortened wording (≤60 for the journal cap) — pinned at the rendered
    # contract level; the full chain render → [:60] → payload regex is pinned
    # by test_night_plan_variant_survives_the_real_push_journal_cap below.
    assert "05:26" in text and "05:40" in text
    assert text.index("05:26") < text.index("05:40")   # armed FIRST (contract)
    assert "write-confirmed" in text                    # the armed's label
    assert "plan ideal" in text                         # the ideal's label
    assert len(text) <= 60                              # journal cap (F1)
    # payload contract (web_payload.py:961-963): planned_on_at = the LAST time in
    # the newest NIGHT PLAN text → the ideal must ride LAST (page label unchanged)
    assert re.findall(r"\b(\d{1,2}:\d{2})\b", text)[-1] == "05:40"
    # the final_plan audit gains both inputs (additive, §27-4)
    fin = [f for e, f in deps.audits if e == "final_plan"]
    assert fin and fin[0]["inputs"] == {"armed": "05:26", "plan_ideal": "05:40"}
    assert fin[0]["action"] == {"type": "timer"}
    assert fin[0]["verdict"] == "rescue_timer"


def test_final_push_states_the_armed_alone_when_no_plan_ideal_is_latched():
    st = AppState()
    st.morning_done = True
    deps = _eve_deps()
    deps.remaining_ah = _ah_for(D(20, 1), _fire(D(20, 1), "05:26"))
    tick(cfg, st, deps, D(20, 1))                    # eve write: armed + ideal set
    st.plan_ideal = None                             # ideal absent (compose case)
    tick(cfg, st, deps, CLOSING)
    finals = _finals(deps)
    assert len(finals) == 1
    text = finals[0]
    assert "switch time 05:26 (write-confirmed)" in text
    assert "plan ideal" not in text and "Planideal" not in text
    times = re.findall(r"\b(\d{1,2}:\d{2})\b", text)
    assert times == ["05:26"]                        # one time; payload m[-1] = the armed


def test_final_push_keeps_no_timer_honesty_when_nothing_was_written():
    # eve ran, plan kept the grid (no failsafe handoff) → armed None → the
    # current honest fallback stays verbatim; and never before the closing.
    st = AppState()
    st.morning_done = True
    deps = FakeDeps(remaining_ah=100.0, switch="on", inventory_timers=[], buckets=[])
    tick(cfg, st, deps, D(20, 10))                   # eve: conservative keep (no timer)
    assert _finals(deps) == []                       # and NOT during the eve either
    tick(cfg, st, deps, CLOSING)
    finals = _finals(deps)
    assert len(finals) == 1 and "no timer" in finals[0]
    # the payload's plan-ideal rule keys on "no timer" in the text
    # (web_payload.py:962 → planned_on_at stays null) — wording must survive
    assert finals[0] == "Night plan final: no timer"


# --- (c) LATCH: one push per eve; day-rollover re-arms -------------------------
def test_one_final_push_per_eve_and_rollover_rearms_it():
    st = AppState()
    st.morning_done = True
    deps = _eve_deps()
    deps.remaining_ah = _ah_for(D(20, 1), _fire(D(20, 1), "05:26"))
    tick(cfg, st, deps, D(20, 1))                        # day-1 eve
    tick(cfg, st, deps, CLOSING)                         # day-1 closing → push #1
    assert len(_finals(deps)) == 1
    tick(cfg, st, deps, datetime(2026, 10, 18, 22, 0, 59))   # repeated closing tick
    tick(cfg, st, deps, datetime(2026, 10, 18, 22, 30))      # night probe cadence
    assert len(_finals(deps)) == 1                           # still exactly one
    # day rollover: resets final_pushed/eve_ran BEFORE the phase runs (main.py:130)
    tick(cfg, st, deps, datetime(2026, 10, 19, 0, 10))       # rollover tick (NIGHT)
    assert len(_finals(deps)) == 1                           # no spurious midnight push
    d2 = datetime(2026, 10, 19, 20, 1)
    deps.remaining_ah = _ah_for(d2, _fire(d2, "04:44"))
    tick(cfg, st, deps, d2)                                  # day-2 eve
    tick(cfg, st, deps, datetime(2026, 10, 19, 22, 0, 58))   # day-2 closing → push #2
    finals = _finals(deps)
    assert len(finals) == 2
    assert "04:44" in finals[-1] and "05:26" not in finals[-1]


# --- (d) REGRESSION: rescue/release pushes + payload keys untouched ------------
def test_rescue_and_release_push_wording_is_byte_pinned():
    # rescue_set (20:1-class, "Rettungs-Timer … gesetzt") — unchanged, both dicts
    assert msg("de", "rescue_set", time="05:26") == \
        "Nachtplan: Rettungs-Timer 05:26 gesetzt."
    assert msg("en", "rescue_set", time="05:26") == \
        "Night plan: rescue timer 05:26 set."
    # night_release — unchanged, both dicts
    assert msg("de", "night_release", available=400.0, need=300.0, factor=1.2) == \
        "Nacht-Freigabe: Netz aus — 400 Wh vorhanden ≥ 1.2× Nachtbedarf 300 Wh."
    assert msg("en", "night_release", available=400.0, need=300.0, factor=1.2) == \
        "Night release: grid off — 400 Wh available ≥ 1.2× night need 300 Wh."


def test_rescue_push_end_to_end_during_the_eve_is_the_exact_old_text():
    st = AppState()
    st.morning_done = True
    deps = _eve_deps()
    deps.remaining_ah = _ah_for(D(20, 1), _fire(D(20, 1), "05:26"))
    tick(cfg, st, deps, D(20, 1))
    assert deps.pushes[0] == ("NIGHT PLAN", "Night plan: rescue timer 05:26 set.")


def test_release_push_end_to_end_is_the_exact_old_text():
    # eve-time release with the grid-off clear failing ONCE (the grid-off pre-emption
    # aborts; the eve's own release retries) — the past-tense release text pins:
    class DisableOnce(FakeDeps):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self._dis = 0

        def act(self, action, **kw):
            if action == "disable_on_timers":
                self._dis += 1
                if self._dis == 1:
                    self.acts.append((action, None, None, False, None))
                    return False
            return super().act(action, **kw)

    st = AppState()
    st.morning_done = True
    deps = DisableOnce(min_v=V400, remaining_ah=15.625, soc_pct=55.0, switch="on",
                       inventory_timers=[], buckets=NEED300, sun_ok=True)
    tick(cfg, st, deps, D(20, 10))
    assert ("NIGHT PLAN",
            "Night release: grid off — 400 Wh available ≥ 1.2× night need 300 Wh.") \
        in deps.pushes


# --- message templates (both dicts, German primary) ---------------------------
def test_message_templates_de_and_en_shapes():
    assert msg("de", "night_plan_armed", armed="04:11") == \
        "Nachtplan final: Schaltzeitpunkt 04:11 (write-confirmed)"            # 56
    # F1 (review): the ideal variants are SHORTENED to render ≤ 60 — the journal
    # caps the text at 60 and the payload parses the capped journal (cap chain
    # pinned below). de = 58 / en = 58 (EN drops the parens for cap margin).
    assert msg("de", "night_plan_armed_ideal", armed="04:11", ideal="05:33") == \
        "Nachtplan final: 04:11 (write-confirmed) · Planideal 05:33"          # 58
    assert msg("en", "night_plan_armed", armed="04:11") == \
        "Night plan final: switch time 04:11 (write-confirmed)"               # 53
    assert msg("en", "night_plan_armed_ideal", armed="04:11", ideal="05:33") == \
        "Night plan final: 04:11 write-confirmed · plan ideal 05:33"          # 58
    # the fallback key keeps its EXACT legacy wording (payload null-rule + tests)
    assert msg("de", "night_plan", plan="no timer") == "Nachtplan final: no timer"     # 25
    assert msg("en", "night_plan", plan="no timer") == "Night plan final: no timer"    # 26


# --- F1 teeth (review of 42adc55): chain the 60-char push-journal cap ----------
# RealDeps.push journals inputs.text = text[:60] (main.py — the SECRET GUARD,
# contract-pinned in test_main_tick: the cap exists to keep the webhook URI and
# friends out of the journal); the audit dual-write ships THAT capped rec into
# the events index, and the payload reads planned_on_at from it. The phone gets
# the FULL text — only the page's source is capped. Therefore EVERY variant must
# hold over the CHAIN: render → [:60] → payload regex. This is the pin that
# would have caught F1 (the 74-char DE template lost the ideal to the cap).
NIGHT_PLAN_CASES = [
    ("de", "night_plan", dict(plan="no timer"), None),          # null rule over the cap
    ("en", "night_plan", dict(plan="no timer"), None),
    ("de", "night_plan_armed", dict(armed="04:11"), "04:11"),  # equal case: the one
    ("en", "night_plan_armed", dict(armed="04:11"), "04:11"),  # time IS the ideal
    ("de", "night_plan_armed_ideal", dict(armed="04:11", ideal="05:33"), "05:33"),
    ("en", "night_plan_armed_ideal", dict(armed="04:11", ideal="05:33"), "05:33"),
]


@pytest.mark.parametrize("lang,key,kw,expect", NIGHT_PLAN_CASES,
                         ids=[f"{l}-{k}" for l, k, _kw, _e in NIGHT_PLAN_CASES])
def test_night_plan_variant_survives_the_real_push_journal_cap(
        monkeypatch, lang, key, kw, expect):
    deps = _real_deps(monkeypatch)
    text = msg(lang, key, **kw)
    deps.push("NIGHT PLAN", text)
    assert deps.pushes[-1] == ("NIGHT PLAN", text)   # ntfy (the phone) gets the FULL text
    journaled = [f for e, f in deps.audit_log.events if e == "push"][-1]["inputs"]["text"]
    assert journaled == text[:60]                   # the journal cap applies (contract)
    got = _payload_planned_on_at(journaled)
    assert got == expect, \
        f"{lang}/{key}: payload planned_on_at={got!r}, want {expect!r} — " \
        f"capped journal: {journaled!r}"
    assert len(text) <= 60, \
        f"{lang}/{key} renders {len(text)} chars — over the journal cap"
    if expect is not None and key == "night_plan_armed_ideal":
        # both truths must be PRESENT in what the page parses (armed first, ideal
        # last — the cap may never eat either end)
        assert kw["armed"] in journaled and kw["ideal"] in journaled


# --- M1 (review of 42adc55): restart that missed the eve = NO push -------------
def test_restart_that_missed_the_eve_stays_silent_at_the_closing():
    # eve_ran lives only in memory; restore_phase_flags restores the DURABLE
    # day flags (morning_done/night_over from the events index) but never the
    # eve roll — by design: THIS process never ran an eve, so it must not push
    # a fresh-state claim at the closing (the pre-§27 code was silent in that
    # situation too: its final block lived in _eve, which never ran).
    st = AppState()
    deps = _eve_deps()
    deps.phase_evidence = lambda since: [
        {"event": "grid_off", "result": "ok"},                  # morning_done seed
        {"event": "night_over", "ts": CLOSING.isoformat()}]     # night_over seed (<24 h)
    restore_phase_flags(st, deps, CLOSING, cfg)
    assert st.morning_done and st.night_over        # durable flags DID restore…
    assert st.eve_ran is False and not st.final_pushed   # …the eve roll does not
    tick(cfg, st, deps, CLOSING)                    # first tick after the restart
    assert _finals(deps) == []                      # no fresh-state push
    assert st.final_pushed is False
