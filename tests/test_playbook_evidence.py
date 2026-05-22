from backend.execution.evidence import (
    classify_playbook,
    enrich_setup_context,
    primary_factor_bucket,
    session_window,
)


def _signal(score, **meta):
    return {"score": score, "meta": meta}


def test_primary_factor_bucket_is_mutually_exclusive_starter_map():
    assert primary_factor_bucket("NVDA") == "semis"
    assert primary_factor_bucket("QQQ") == "broad_tech"
    assert primary_factor_bucket("IBIT") == "crypto"


def test_session_window_classification():
    assert session_window(20) == "opening_drive"
    assert session_window(90) == "morning_trend"
    assert session_window(320) == "afternoon_momentum"
    assert session_window(None) == "outside_regular_hours"


def test_classify_opening_drive_breakout_from_core_evidence():
    signals = {
        "orb": _signal(0.42),
        "tape_aggression": _signal(0.22),
        "relative_strength": _signal(0.18),
        "vwap_deviation": _signal(0.15, pct_deviation=0.2),
    }
    setup_context = {
        "minutes_since_open": 20,
        "strategy_family": "trend_following",
        "intraday_regime": "trending",
    }

    assert (
        classify_playbook("NVDA", "BUY", signals, {"atr_data": {"atr_pct": 1.0}}, setup_context)
        == "opening_drive_breakout"
    )


def test_enrich_setup_context_adds_observability_without_decision_flags():
    signals = {
        "order_book_imbalance": _signal(0.05, spread_pct=0.03, source="alpaca"),
        "vwap_deviation": _signal(0.1, pct_deviation=0.12),
        "relative_strength": _signal(0.2),
        "macd_crossover": _signal(0.11),
    }
    signal_result = {
        "computed_at": "2026-05-22T12:00:00Z",
        "atr_data": {"atr_pct": 0.8},
        "rvol_data": {"rvol_available": True},
    }
    setup_context = {
        "minutes_since_open": 80,
        "strategy_family": "trend_following",
        "intraday_regime": "trending",
    }

    enriched = enrich_setup_context("AMD", "BUY", signals, signal_result, setup_context)

    assert enriched["playbook"] == "morning_vwap_reclaim"
    assert enriched["playbook_lifecycle"] == "tagged"
    assert enriched["primary_factor"] == "semis"
    assert enriched["session_window"] == "morning_trend"
    assert enriched["data_quality_state"] in {"executable", "shadow_only"}
    assert enriched["estimated_total_cost_pct"] == 0.11
