from datetime import date, datetime, timezone

import pytest

import backend.daily_review as daily_review


def test_collect_daily_metrics_tracks_bad_avoids_and_shadow_repeats(monkeypatch):
    review_day = date(2026, 5, 19)
    monkeypatch.setattr(daily_review, "get_recent_trades", lambda days=3: [
        {
            "ticker": "IWM",
            "side": "BUY",
            "pnl_eur": -1.5,
            "net_pnl_pct": -0.2,
            "hold_minutes": 8,
            "exit_reason": "stop_loss",
            "regime": "ranging",
            "exit_time": "2026-05-19T14:40:00+00:00",
        },
        {
            "ticker": "SPY",
            "side": "BUY",
            "pnl_eur": 2.0,
            "net_pnl_pct": 0.1,
            "hold_minutes": 12,
            "exit_reason": "take_profit",
            "regime": "ranging",
            "exit_time": "2026-05-19T15:10:00+00:00",
        },
    ])
    monkeypatch.setattr(daily_review, "get_blocked_opportunities", lambda days=3, limit=500: [
        {
            "ticker": "NVDA",
            "block_reason": "ev_negative",
            "block_stage": "ev",
            "created_at": "2026-05-19T14:00:00+00:00",
            "replay_checked_at": "2026-05-19T16:00:00+00:00",
            "max_favorable_pct": 0.2,
            "max_adverse_pct": -1.1,
            "close_after_pct": -0.4,
        },
        {
            "ticker": "XLE",
            "block_reason": "not_in_universe",
            "block_stage": "ranking",
            "created_at": "2026-05-19T14:10:00+00:00",
            "replay_checked_at": "2026-05-19T16:00:00+00:00",
            "max_favorable_pct": 1.2,
            "max_adverse_pct": -0.1,
            "close_after_pct": 0.8,
        },
    ])
    monkeypatch.setattr(daily_review, "get_recent_advisory_signals", lambda days=3, limit=500: [
        {"market": "EU", "mode": "shadow", "status": "shadow_logged", "created_at": "2026-05-19T08:00:00+00:00"}
    ])
    monkeypatch.setattr(daily_review, "get_open_trade_records", lambda: [{"ticker": "TLT", "side": "BUY"}])
    monkeypatch.setattr(daily_review, "get_logs", lambda level="INFO", limit=500: [
        {
            "event": "dynamic_universe_shadow_recommendations",
            "logged_at": "2026-05-19T14:00:00+00:00",
            "detail": {"shadow_candidates": [{"ticker": "XLE", "theme": "energy", "reason": "energy leading"}]},
        },
        {
            "event": "dynamic_universe_shadow_recommendations",
            "logged_at": "2026-05-19T14:10:00+00:00",
            "detail": {"shadow_candidates": [{"ticker": "XLE", "theme": "energy", "reason": "energy leading"}]},
        },
        {
            "event": "dynamic_universe_shadow_recommendations",
            "logged_at": "2026-05-19T14:20:00+00:00",
            "detail": {"shadow_candidates": [{"ticker": "XLE", "theme": "energy", "reason": "energy leading"}]},
        },
    ])
    monkeypatch.setenv("TICKER_UNIVERSE", "SPY,QQQ")

    metrics = daily_review.collect_daily_metrics(review_day)

    assert metrics["trade_summary"]["total_trades"] == 2
    assert metrics["trade_summary"]["total_pnl_eur"] == pytest.approx(0.5)
    assert metrics["blocked_opportunities"]["bad_avoids"][0]["ticker"] == "NVDA"
    assert metrics["blocked_opportunities"]["missed_winners"][0]["ticker"] == "XLE"
    assert metrics["shadow_universe"][0]["ticker"] == "XLE"
    assert metrics["shadow_universe"][0]["review_candidate"] is True
    assert metrics["deterministic_recommendations"][0]["variable"] == "TICKER_UNIVERSE"


def test_recommendations_are_normalized_to_human_approval():
    recs = daily_review._normalize_recommendations(
        [{
            "category": "risk",
            "variable": "RANGING_A_PLUS_MIN_COMPOSITE",
            "suggested_value": "0.30",
            "confidence": 0.8,
            "autonomy_level": "auto_apply",
            "auto_apply": True,
        }],
        [],
    )

    assert recs[0]["auto_apply"] is False
    assert recs[0]["autonomy_level"] == "human_approval"


def test_format_discord_review_is_compact_and_explicitly_read_only():
    metrics = {
        "review_date": "2026-05-19",
        "trade_summary": {"total_pnl_eur": -2.95, "total_trades": 3, "wins": 0, "losses": 3},
        "shadow_universe": [{"ticker": "XLE", "mentions": 5, "review_candidate": True}],
        "blocked_opportunities": {"bad_avoids": [{"ticker": "NVDA"}]},
    }
    review = {
        "summary": "Ranging day, low opportunity quality.",
        "worked_well": ["Trade cap limited churn."],
        "did_not_work": ["Weak A+ still entered."],
        "recommendations": [{"variable": "TICKER_UNIVERSE", "reason": "Add XLE", "confidence": 0.82}],
    }

    text = daily_review.format_discord_review(review, metrics)

    assert "EOD Review" in text
    assert "XLE" in text
    assert "No config changes were auto-applied" in text
