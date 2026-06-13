import json
from datetime import datetime, timezone

import scripts.check_latest_review_snapshot as snapshot_script


def test_expected_latest_review_date_before_cutoff_uses_previous_business_day():
    now = datetime(2026, 5, 27, 20, 0, tzinfo=timezone.utc)
    assert snapshot_script._expected_latest_review_date(now).isoformat() == "2026-05-26"


def test_expected_latest_review_date_after_cutoff_uses_same_day():
    now = datetime(2026, 5, 27, 22, 0, tzinfo=timezone.utc)
    assert snapshot_script._expected_latest_review_date(now).isoformat() == "2026-05-27"


def test_expected_latest_review_date_weekend_uses_previous_friday():
    now = datetime(2026, 5, 30, 12, 0, tzinfo=timezone.utc)  # Saturday
    assert snapshot_script._expected_latest_review_date(now).isoformat() == "2026-05-29"


def test_decision_packet_extracts_top_level_fields():
    snapshot = {
        "review_date": "2026-05-26",
        "daily_review": {
            "summary": "Quiet session.",
            "confidence": 0.9,
            "worked_well": ["Avoided weak tape."],
            "did_not_work": ["No executions."],
            "recommendations": [{"category": "instrumentation", "reason": "Track saves"}],
        },
        "broker_account_snapshot": {
            "buying_power": 2200.0,
            "trading_blocked": False,
        },
        "metrics": {
            "trade_summary": {"total_trades": 0},
            "gate_activity": {"event_counts": {"signal_consensus_veto": 12, "trade_gated": 4}},
            "blocked_opportunities": {
                "missed_winners": [{"ticker": "AMZN"}, {"ticker": "AMZN"}, {"ticker": "TSLA"}],
                "bad_avoids": [{"ticker": "NVDA"}, {"ticker": "PLTR"}],
            },
            "shadow_universe": [
                {"ticker": "MU", "theme": "semis", "mentions": 5, "evidence_days": 1, "review_candidate": True},
                {"ticker": "VRT", "theme": "ai_power", "mentions": 4, "evidence_days": 1, "review_candidate": True},
            ],
            "near_miss_distribution": {"runner_count": 1},
        },
    }

    packet = snapshot_script._decision_packet(snapshot)

    assert packet["summary"] == "Quiet session."
    assert packet["what_failed"]["missed_runner_tickers"] == ["AMZN", "TSLA"]
    assert packet["what_failed"]["top_gate_reasons"][0] == {
        "event": "signal_consensus_veto",
        "count": 12,
    }
    assert packet["what_worked"]["bad_avoid_tickers"] == ["NVDA", "PLTR"]
    assert packet["needs_more_data"]["shadow_review_candidates"][0]["ticker"] == "MU"
    assert packet["broker_context"]["buying_power"] == 2200.0
