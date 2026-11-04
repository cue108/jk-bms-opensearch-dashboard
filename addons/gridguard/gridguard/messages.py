# gridguard/messages.py
MSG = {
    "de": {
        "emergency_on": "NOTSCHUTZ: Netz eingeschaltet (Zelle {min_v:.2f} V).",
        "rescue_set": "Nachtplan: Rettungs-Timer {time} gesetzt.",
        "safe_clear": "Nachtplan: Batterie trägt — nichts getan (Min {min_v:.2f} V, Trend {slope:+.3f} V/h).",
        "grid_off": "MAIN AUS: Rest {remaining:.1f} Ah, Solar {solar:.1f} A (SOC {soc} %).",
        "low_capacity_on": "AKKU TIEF: Netz eingeschaltet (Zellen {v} V korrigiert).",
        "planner_set": "Nachtplan: {what} um {time} programmiert.",
        "no_channel": "KEIN KANAL: Aktion {action} nicht ausführbar — Status prüfen!",
        "night_plan": "Nachtplan final: {plan}",
        # §27 (owner 2026-10-27): the final push LEADS with the write-confirmed
        # armed time; the plan ideal rides LAST and only when it differs.
        # TWO constraints pin this wording (review F1 of 42adc55):
        # (a) the payload reads the LAST HH:MM of the newest NIGHT PLAN push text
        #     as planned_on_at (web_payload.py) → the ideal MUST come last;
        # (b) RealDeps.push journals the text CAPPED at 60 chars (the secret
        #     guard — main.py push) and the payload parses that CAPPED journal →
        #     every variant must RENDER ≤ 60 chars or the ideal is cut off and
        #     the page mislabels the armed as the plan ideal. Pinned by
        #     test_final_push_closing (chain: render → [:60] → payload regex).
        # Measured: de armed+ideal = 58, de armed-only = 56, no-timer = 25.
        "night_plan_armed": "Nachtplan final: Schaltzeitpunkt {armed} (write-confirmed)",
        "night_plan_armed_ideal": "Nachtplan final: {armed} (write-confirmed) · Planideal {ideal}",
        "timer_fallback": "Timer nicht setzbar — Netz ab sofort an (Ersatzstrategie).",
        # §9.7: no "grid on:" prefix anymore — the title already carries the state
        "grid_hold": "{reason}",
        "night_release": "Nacht-Freigabe: Netz aus — {available:.0f} Wh vorhanden ≥ {factor:.1f}× Nachtbedarf {need:.0f} Wh.",
        "manual_override": "Schalter jetzt {to} (um {at}) — kein GridGuard-Eingriff, manuell erkannt.",
    },
    "en": {
        "emergency_on": "EMERGENCY: grid ON (cell {min_v:.2f} V).",
        "rescue_set": "Night plan: rescue timer {time} set.",
        "safe_clear": "Night plan: battery carries — nothing done (min {min_v:.2f} V, slope {slope:+.3f} V/h).",
        "grid_off": "MAIN OFF: {remaining:.1f} Ah left, {solar:.1f} A solar (SOC {soc} %).",
        "low_capacity_on": "LOW BATTERY: grid ON (cells {v} V corrected).",
        "planner_set": "Night plan: {what} programmed for {time}.",
        "no_channel": "NO CHANNEL: action {action} not executable — check status!",
        "night_plan": "Night plan final: {plan}",
        # §27 parallel to the German dict (armed leads, ideal last when differing).
        # EN keeps the parenthesised "(write-confirmed)" in the ARMED-ONLY variant
        # but renders the IDEAL variant without the parens: EN's family prefix +
        # the full "plan ideal" label + "write-confirmed" sit at EXACTLY 60 chars
        # with parens = zero margin on the journal cap (see the DE block above;
        # cap chain pinned in test_final_push_closing). Measured: en armed+ideal
        # = 58, en armed-only = 53, no-timer = 26 — all ≤ 60.
        "night_plan_armed": "Night plan final: switch time {armed} (write-confirmed)",
        "night_plan_armed_ideal": "Night plan final: {armed} write-confirmed · plan ideal {ideal}",
        "timer_fallback": "Timer not settable — grid ON now (fallback strategy).",
        # §9.7: no "Grid on:" prefix anymore — the title already carries the state
        "grid_hold": "{reason}",
        "night_release": "Night release: grid off — {available:.0f} Wh available ≥ {factor:.1f}× night need {need:.0f} Wh.",
        "manual_override": "switch now {to} (at {at}) — not a GridGuard act, manual override recognized.",
    },
}


def msg(lang: str, key: str, **kw) -> str:
    template = MSG.get(lang, MSG["en"])[key]
    return template.format(**kw)


# Push titles (owner ruling 2026-11-04, "English ONLY" + compatibility):
# the STORED and queried event `kind` is the stable ENGLISH key; the ntfy
# DISPLAY title stays localized so the phone keeps its language (de titles =
# the former German kind values, byte-identical to what history carries).
TITLES = {
    "de": {"NIGHT PLAN": "NACHTPLAN", "GRID ON": "NETZ AN",
           "NO CHANNEL": "KEIN KANAL", "EMERGENCY": "NOTSCHUTZ",
           "LOW BATTERY": "AKKU TIEF", "MAIN OFF": "MAIN AUS",
           "MANUAL": "MANUELL"},
    "en": {"NIGHT PLAN": "NIGHT PLAN", "GRID ON": "GRID ON",
           "NO CHANNEL": "NO CHANNEL", "EMERGENCY": "EMERGENCY",
           "LOW BATTERY": "LOW BATTERY", "MAIN OFF": "MAIN OFF",
           "MANUAL": "MANUAL"},
}


def title(kind: str, lang: str) -> str:
    """ntfy display title for an event kind — identity in en, the German
    labels in de; unknown kinds pass through (display-truth: never invent)."""
    return TITLES.get(lang, TITLES["en"]).get(kind, kind)
