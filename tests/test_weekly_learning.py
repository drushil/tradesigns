import backend.learning.engine as learning


def test_build_weekly_eod_evidence_aggregates_daily_reviews():
    reviews = [
        {
            "review_date": "2026-05-18",
            "metrics_json": {
                "trade_summary": {"total_trades": 2, "wins": 1, "losses": 1, "total_pnl_eur": 3.5},
                "gate_activity": {"event_counts": {"signal_consensus_veto": 4}},
                "blocked_opportunities": {
                    "missed_winners": [
                        {"ticker": "ARM", "runner_severity": "runner"},
                        {"ticker": "XLE", "runner_severity": "minor"},
                    ],
                    "bad_avoids": [{"ticker": "NVDA"}],
                },
                "near_miss_distribution": {"total": 3, "checked": 2, "runner_count": 1},
                "direction_error_candidates": [{"ticker": "SMH"}],
                "shadow_universe": [{"ticker": "CVX", "theme": "energy", "mentions": 5}],
            },
            "recommendations_json": [{"variable": "TICKER_UNIVERSE"}],
        },
        {
            "review_date": "2026-05-19",
            "metrics_json": {
                "trade_summary": {"total_trades": 1, "wins": 0, "losses": 1, "total_pnl_eur": -1.0},
                "gate_activity": {"event_counts": {"signal_consensus_veto": 2}},
                "blocked_opportunities": {
                    "missed_winners": [{"ticker": "ARM", "runner_severity": "runner"}],
                    "bad_avoids": [{"ticker": "NVDA"}],
                },
                "near_miss_distribution": {"total": 4, "checked": 3, "runner_count": 1},
                "direction_error_candidates": [{"ticker": "SMH"}],
                "shadow_universe": [{"ticker": "CVX", "theme": "energy", "mentions": 4}],
            },
            "review_json": {"recommendations": [{"variable": "TICKER_UNIVERSE"}]},
        },
    ]

    evidence = learning.build_weekly_eod_evidence(reviews)

    assert evidence["review_days"] == 2
    assert evidence["trade_totals"]["trades"] == 3
    assert evidence["trade_totals"]["pnl_eur"] == 2.5
    assert evidence["gate_event_totals"]["signal_consensus_veto"] == 6
    assert evidence["runner_tickers"]["ARM"] == 2
    assert evidence["bad_avoid_tickers"]["NVDA"] == 2
    assert evidence["near_miss_distribution"]["checked"] == 5
    assert evidence["near_miss_distribution"]["sample_size_ready"] is False
    assert evidence["direction_error_tickers"]["SMH"] == 2
    assert evidence["shadow_candidates"][0]["ticker"] == "CVX"
    assert evidence["shadow_candidates"][0]["evidence_days"] == 2
    assert evidence["repeated_recommendations"]["TICKER_UNIVERSE"] == 2


def test_generate_weekly_insights_can_run_from_daily_reviews_without_trades(monkeypatch):
    captured = {}

    class _Message:
        content = '[{"insight":"Near misses observed","action":"observe_only","confidence":0.7,"category":"signals","action_class":"observe_only","sample_size":5,"evidence_days":2}]'

    class _Choice:
        message = _Message()

    class _Completions:
        @staticmethod
        def create(**kwargs):
            captured["prompt"] = kwargs["messages"][0]["content"]
            return type("Resp", (), {"choices": [_Choice()]})()

    class _Client:
        chat = type("Chat", (), {"completions": _Completions()})()

    monkeypatch.setattr(learning, "_get_client", lambda: _Client())

    insights = learning.generate_weekly_insights([], daily_reviews=[{
        "review_date": "2026-05-19",
        "metrics_json": {
            "trade_summary": {"total_trades": 0},
            "near_miss_distribution": {"total": 5, "checked": 5, "runner_count": 1},
        },
    }])

    assert insights[0]["action_class"] == "observe_only"
    assert "WEEKLY EOD EVIDENCE" in captured["prompt"]
    assert "Near-threshold trading changes require" in captured["prompt"]
