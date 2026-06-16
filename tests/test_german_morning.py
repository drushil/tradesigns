import backend.advisory_auto.german_morning as gm


def _sig(sym, grade, comp, day="2026-06-12", breakout=0.6, entry=(100.0, 101.0)):
    return {"data_symbol": sym, "grade": grade, "side": "BUY",
            "composite_score": comp, "breakout_quality": breakout,
            "currency": "USD", "entry_min": entry[0], "entry_max": entry[1],
            "created_at": f"{day}T18:00:00Z"}


def test_latest_session_picks_best_grade_per_symbol_from_newest_day(monkeypatch):
    rows = [
        _sig("NVDA", "A", 0.5, day="2026-06-12"),
        _sig("NVDA", "B", 0.4, day="2026-06-12"),   # dup symbol, weaker -> dropped
        _sig("AMD", "A+", 0.6, day="2026-06-12"),
        _sig("OLD", "A", 0.9, day="2026-06-10"),     # older session -> excluded
        _sig("LOW", "C", 0.9, day="2026-06-12"),     # below strong grades -> excluded
    ]
    monkeypatch.setattr(gm, "get_recent_advisory_signals", lambda **_: rows)
    picks, session = gm._latest_session_strong_names()
    syms = [p["data_symbol"] for p in picks]
    assert session == "2026-06-12"
    assert syms[0] == "AMD"          # A+ ranks first
    assert "NVDA" in syms and syms.count("NVDA") == 1  # deduped
    assert "OLD" not in syms and "LOW" not in syms


def test_overnight_futures_tone_thresholds():
    # tone is derived from the avg of es/nq; verify the card formats cleanly.
    card = gm._fmt_card({"es_pct": 0.4, "nq_pct": 0.7, "tone": "risk-on"},
                        [_sig("NVDA", "A", 0.5)], "2026-06-12")
    assert "risk-on" in card
    assert "NVDA" in card and "(A)" in card
    assert "limit only" in card.lower()
    assert "not* live US confirmation" in card or "not live US" in card.lower()


def test_card_handles_no_names():
    card = gm._fmt_card({"es_pct": None, "nq_pct": None, "tone": "unknown"}, [], None)
    assert "No strong prior-session names" in card


def test_pinned_symbol_always_included(monkeypatch):
    # SPCX graded C (not strong) but is pinned -> must still appear, tagged.
    rows = [_sig("NVDA", "A", 0.5, day="2026-06-12"),
            _sig("SPCX", "C", 0.2, day="2026-06-12")]
    monkeypatch.setattr(gm, "get_recent_advisory_signals", lambda **_: rows)
    monkeypatch.setattr(gm, "PINNED", {"SPCX"})
    picks, _ = gm._latest_session_strong_names()
    syms = [p["data_symbol"] for p in picks]
    assert "NVDA" in syms and "SPCX" in syms
    spcx = next(p for p in picks if p["data_symbol"] == "SPCX")
    assert spcx.get("_pinned") is True


def test_pinned_symbol_with_no_signal_shows_tracked(monkeypatch):
    monkeypatch.setattr(gm, "get_recent_advisory_signals",
                        lambda **_: [_sig("NVDA", "A", 0.5)])
    monkeypatch.setattr(gm, "PINNED", {"SPCX"})
    picks, _ = gm._latest_session_strong_names()
    spcx = next(p for p in picks if p["data_symbol"] == "SPCX")
    assert spcx.get("_no_signal") is True
    card = gm._fmt_card({"tone": "mixed"}, picks, "2026-06-12")
    assert "SPCX" in card and "no recent signal" in card


def test_run_disabled_short_circuits(monkeypatch):
    monkeypatch.setattr(gm, "GERMAN_MORNING_ENABLED", False)
    out = gm.run_german_morning_watch()
    assert out == {"ran": False, "reason": "disabled"}
