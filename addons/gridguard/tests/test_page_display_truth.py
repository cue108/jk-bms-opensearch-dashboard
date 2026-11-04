"""Display-truth copy pins (owner brief 2026-10-26).

1) night_over detail: the event marks the transition INTO the night phase —
   the DAY window ended → night begins. The old copy
   "night window ended → phase night" was self-contradictory (event NAME
   night_over stays — it is contract; only the rendered detail changes).
2) The hero must render the write-confirmed announced time from
   decision.announced_on_at, with the plan ideal separately labelled, and
   the decision log must show each ladder write's programmed time.
"""
from gridguard.web_page import PAGE


def test_night_over_detail_states_the_day_window_truth():
    assert "day window ended → phase night" in PAGE
    assert "night window ended → phase night" not in PAGE


def test_hero_announced_reads_write_confirmed_field_with_plan_ideal_label():
    assert "announced_on_at" in PAGE          # hero time = write-confirmed source
    assert "announced_fires_at" in PAGE       # anchored for the spent/expired rule
    assert "plan ideal" in PAGE               # plan ideal stays separately labelled
    # absence rule: no confirmed write → no announced time (copy says so)
    assert "NOT write-confirmed" in PAGE


def test_decision_log_shows_programmed_time_of_each_ladder_write():
    # review nit (6a92dd1): pin the DETAIL-line context, not the bare token —
    # "fire_str" also appears in hero comments, so a reverted detail() line
    # must fail here; the page byte-golden stays the backstop
    assert 'if(i.fire_str) bits.push("programmed "+i.fire_str)' in PAGE


def test_history_note_names_write_confirmed_programs_and_labelled_markers():
    """Ruling #10: the timeline note must describe the ACTUAL boundary
    sources (write-confirmed programs + observations), and the plan ideal
    as a labelled marker — not as an announcement/boundary."""
    assert "write-confirmed timer programs" in PAGE
    assert "announced device-timer programs" not in PAGE
    assert "plan ideals appear as labelled markers" in PAGE


def test_history_renders_plan_marks_and_measurement_derived_starts():
    """The two ruling-#10 disclosures in the history markup: plan ideals as
    labelled context (plan_marks) and the fill-derived ON boundary marked
    as measurement-derived (s.src==='measured')."""
    assert "plan_marks" in PAGE
    assert 's.src==="measured"' in PAGE
    assert "start measured" in PAGE
