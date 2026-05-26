import json
from datetime import date, datetime, timezone
from pathlib import Path

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
            "max_favorable_pct": 2.4,
            "max_adverse_pct": -0.1,
            "close_after_pct": 1.8,
            "replay_result_json": {"runner_severity": "runner"},
        },
        {
            "ticker": "ARM",
            "action_hint": "BUY",
            "block_reason": "signal below threshold (0.047 < 0.05)",
            "block_stage": "gate",
            "created_at": "2026-05-19T14:20:00+00:00",
            "replay_checked_at": "2026-05-19T16:00:00+00:00",
            "max_favorable_pct": 3.1,
            "max_adverse_pct": -0.2,
            "close_after_pct": 2.2,
            "block_detail": {
                "kind": "signal_threshold",
                "score": 0.047,
                "threshold": 0.05,
                "threshold_gap": 0.003,
                "near_threshold": True,
            },
        },
        {
            "ticker": "SMH",
            "action_hint": "SELL",
            "block_reason": "short signal below threshold",
            "block_stage": "gate",
            "created_at": "2026-05-19T14:30:00+00:00",
            "replay_checked_at": "2026-05-19T16:00:00+00:00",
            "max_favorable_pct": 0.1,
            "max_adverse_pct": -3.0,
            "close_after_pct": -2.4,
        },
    ])
    monkeypatch.setattr(daily_review, "get_recent_signals", lambda hours=72, limit=1000: [
        {
            "ticker": "SMH",
            "created_at": "2026-05-19T14:29:00+00:00",
            "composite_score": -0.094,
            "rsi_divergence_score": -0.4,
            "vwap_deviation_score": 0.7,
            "tape_aggression_score": -0.2,
            "order_book_score": 0.1,
            "action_hint": "SELL",
            "regime": "ranging",
        }
    ])
    monkeypatch.setattr(daily_review, "get_recent_advisory_signals", lambda days=3, limit=500: [
        {"market": "EU", "mode": "shadow", "status": "shadow_logged", "created_at": "2026-05-19T08:00:00+00:00"}
    ])
    monkeypatch.setattr(daily_review, "get_open_trade_records", lambda: [{"ticker": "TLT", "side": "BUY"}])
    monkeypatch.setattr(daily_review, "get_account", lambda: {
        "equity": 5400.0,
        "cash": 1100.0,
        "buying_power": 2200.0,
        "daytrade_count": 3,
        "pattern_day_trader": False,
        "trading_blocked": False,
        "account_blocked": False,
        "status": "ACTIVE",
    })
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
        {
            "event": "signal_consensus_veto",
            "logged_at": "2026-05-19T14:25:00+00:00",
            "detail": {"ticker": "SPY", "aligned_count": 3},
        },
        {
            "event": "ranging_regime_candidate_block",
            "logged_at": "2026-05-19T14:26:00+00:00",
            "detail": {"ticker": "QQQ", "reason": "ranging_regime_grade_veto"},
        },
        {
            "event": "cycle_regime_observability",
            "logged_at": "2026-05-19T14:27:00+00:00",
            "detail": {"spy_trend_score": 0.2, "spy_trend_threshold": 0.8, "intraday_regimes": {"ranging": 2}},
        },
        {
            "event": "order_failed",
            "logged_at": "2026-05-19T14:28:00+00:00",
            "detail": {
                "ticker": "IBIT",
                "error": "40310100 PDT protection",
                "client_order_id": "ts-ibit-1",
            },
        },
    ])
    monkeypatch.setenv("TICKER_UNIVERSE", "SPY,QQQ")

    metrics = daily_review.collect_daily_metrics(review_day)

    assert metrics["trade_summary"]["total_trades"] == 2
    assert metrics["trade_summary"]["total_pnl_eur"] == pytest.approx(0.5)
    assert metrics["blocked_opportunities"]["bad_avoids"][0]["ticker"] == "NVDA"
    assert metrics["blocked_opportunities"]["missed_winners"][0]["ticker"] == "XLE"
    assert metrics["blocked_opportunities"]["missed_winners"][0]["runner_severity"] == "runner"
    assert metrics["near_miss_distribution"]["runner_count"] == 1
    assert metrics["near_miss_distribution"]["top_runners"][0]["ticker"] == "ARM"
    assert metrics["direction_error_candidates"][0]["ticker"] == "SMH"
    assert metrics["direction_error_candidates"][0]["signal_snapshot"]["action_hint"] == "SELL"
    assert metrics["shadow_universe"][0]["ticker"] == "XLE"
    assert metrics["shadow_universe"][0]["review_candidate"] is True
    assert metrics["gate_activity"]["event_counts"]["signal_consensus_veto"] == 1
    assert metrics["gate_activity"]["ranging_block_reasons"]["ranging_regime_grade_veto"] == 1
    assert metrics["broker_rejections"]["count"] == 1
    assert metrics["broker_rejections"]["grouped_by_ticker"]["IBIT"] == 1
    assert metrics["broker_account_snapshot"]["daytrade_count"] == 3
    assert metrics["deterministic_recommendations"][0]["variable"] == "TICKER_UNIVERSE"
    assert metrics["deterministic_recommendations"][0]["category"] == "universe_watch"
    assert metrics["deterministic_recommendations"][0]["command_text"] is None


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
        "near_miss_distribution": {"total": 2, "checked": 2, "runner_count": 1},
        "direction_error_candidates": [{"ticker": "SMH", "action": "SELL", "close_after_pct": -2.4}],
        "gate_activity": {"event_counts": {"signal_consensus_veto": 12, "ranging_regime_candidate_block": 5}},
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
    assert "Gate activity: consensus 12, ranging 5" in text
    assert "Near-miss watch: 2/2 checked, 1 runners" in text
    assert "Direction diagnostics: SMH SELL (-2.40%)" in text
    assert "No config changes were auto-applied" in text


def test_save_local_daily_review_snapshot_writes_latest_and_dated(tmp_path, monkeypatch):
    monkeypatch.setattr(daily_review, "_SNAPSHOT_DIR", tmp_path / "daily_reviews")
    review_day = date(2026, 5, 19)

    result = daily_review.save_local_daily_review_snapshot(
        review_day,
        {
            "trade_summary": {"total_trades": 0},
            "broker_account_snapshot": {"daytrade_count": 2},
            "broker_rejections": {"count": 1},
        },
        {"summary": "Quiet session", "confidence": 0.7, "worked_well": [], "did_not_work": []},
        {"id": 1},
        [{"category": "instrumentation", "reason": "Track rejects"}],
        "discord text",
        True,
    )

    assert result["ok"] is True
    latest = tmp_path / "daily_reviews" / "latest.json"
    dated = tmp_path / "daily_reviews" / "2026-05-19.json"
    assert latest.exists()
    assert dated.exists()

    payload = json.loads(latest.read_text(encoding="utf-8"))
    assert payload["review_date"] == "2026-05-19"
    assert payload["data_source"]["local_snapshot_saved"] is True
    assert payload["broker_account_snapshot"]["daytrade_count"] == 2
    assert payload["broker_rejections"]["count"] == 1


def test_save_succeeded_accepts_null_error_column():
    assert daily_review._save_succeeded({"id": 3, "error": None}) is True
    assert daily_review._save_succeeded({"id": 3, "error": ""}) is True
    assert daily_review._save_succeeded({"error": "boom"}) is False
    assert daily_review._save_succeeded({}) is False
