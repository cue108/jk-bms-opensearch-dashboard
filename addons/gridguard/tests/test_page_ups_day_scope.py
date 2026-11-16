"""Backend-truth display for the UPS layer: every claim the page renders
comes from payload facts the backend already publishes —
  - forecast3[].gate_ok === false → the backend's own per-day gate (the
    flagged bad day the enforcement acts on)
  - reserve_tier inputs           → the 3-day window record (tier, why, set)
  - tier_hold                     → hold evidence (engaged or never)
  - decision.soc_pct              → the live SOC vs the payload threshold
No client clock, no derived "today", no extra backend field — the pins
below fail if a client-side derivation or a removed publication resurfaces.
"""
from gridguard.web_page import PAGE


def test_flagged_days_come_from_the_backends_own_forecast_gate():
    assert "function flaggedDays" in PAGE
    assert "gate_ok===false" in PAGE
    assert "function flagScopeTxt" in PAGE
    # reverted backend publications / client-clock derivations must not return
    assert "tier_active" not in PAGE
    assert "applies_on" not in PAGE
    assert "function ggToday" not in PAGE
    assert "upsInForceToday" not in PAGE


def test_effect_sentence_is_scoped_to_the_flagged_day():
    assert ('return scope? "on "+scope+": "+core : core+" (outlook window)";'
            in PAGE)
    assert '" · drain stops at "+t+" %"' in PAGE


def test_strip_states_flagged_day_and_hold_evidence():
    assert "flagged bad day" in PAGE             # the backend's per-day gate
    assert "\u2014 no hold" in PAGE              # the backend never engaged one
    assert "\u2014 no floor" in PAGE             # tier 0 holds nothing


def test_status_reports_hold_evidence_not_released_guesses():
    assert 'else if(t===0) st="no floor";' in PAGE
    assert 'else if(!holdMatch) st="no hold";' in PAGE
    assert "st-nohold" in PAGE                   # span class + the CSS rule
    assert ".st-nohold {" in PAGE


def test_actor_plan_chip_tells_the_armed_truth_when_it_diverges_from_the_plan():
    """The chip must state the armed-vs-plan divergence from payload facts
    only — never imply a full drain the armed timer contradicts."""
    assert "D.planned_on_at!==D.announced_on_at" in PAGE
    assert "the battery will NOT be emptied tonight" in PAGE


def test_actor_plan_chip_keeps_the_doctrine_label_only_in_the_equal_branch():
    # "battery-first release" may only render when armed time == plan ideal
    # (the equal case); the divergent branch must carry the truth instead
    assert ("night plan: GRID OFF until then — battery-first release</span>"
            in PAGE)
    assert ("? \"<span class='chip off'>night plan: GRID OFF until "
            "then") not in PAGE.replace(") :", "):")  # no duplicate label
