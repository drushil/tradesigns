from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pandas as pd
import pytest

import backend.advisory as advisory


def _cfg(**overrides):
    values = {
        "markets": {"US"},
        "live_markets": {"US"},
        "shadow_markets": {"EU"},
        "shadow_discord_markets": {"EU"},
        "capital_eur": 5000.0,
        "max_live_alerts_per_day": 3,
        "max_shadow_signals_per_day": 10,
        "max_shadow_discord_alerts_per_day": 1,
        "max_open_live_trades": 1,
        "max_live_trades_per_session": 2,
        "risk_per_trade_eur": 50.0,
        "max_daily_loss_eur": 150.0,
        "default_size_eur": 750.0,
        "a_plus_max_size_eur": 1500.0,
        "min_composite": 0.45,
        "min_watch_composite": 0.25,
        "min_watch_breakout_quality": 0.30,
        "min_ev_pct": 0.50,
        "min_breakout_quality": 0.45,
        "min_discord_grade": "A",
        "shadow_min_discord_grade": "A",
        "us_min_minutes_after_open": 15,
        "allow_short": False,
        "discord_webhook_url": "https://discord.invalid/webhook",
        "fx_rate": 1.08,
        "broker_profile": "trade_republic",
        "broker_tag": "trade_republic_de",
    }
    values.update(overrides)
    return advisory.AdvisoryConfig(**values)


def test_window_name_enforces_trade_republic_friendly_sessions():
    berlin = timezone(timedelta(hours=2))

    assert advisory._window_name("EU", datetime(2026, 5, 15, 6, 59, tzinfo=berlin)) is None
    assert advisory._window_name("EU", datetime(2026, 5, 15, 7, 0, tzinfo=berlin)) == "tr_morning_watch"
    assert advisory._window_name("EU", datetime(2026, 5, 15, 7, 30, tzinfo=berlin)) == "tr_morning_watch"
    assert advisory._window_name("EU", datetime(2026, 5, 15, 9, 0, tzinfo=berlin)) == "tr_morning_watch"
    assert advisory._window_name("EU", datetime(2026, 5, 15, 9, 14, tzinfo=berlin)) == "tr_morning_watch"
    assert advisory._window_name("EU", datetime(2026, 5, 15, 9, 15, tzinfo=berlin)) == "eu_open"
    assert advisory._window_name("EU", datetime(2026, 5, 15, 14, 30, tzinfo=berlin)) == "eu_catalyst_only"
    assert advisory._window_name("US", datetime(2026, 5, 15, 14, 59, tzinfo=berlin)) is None
    assert advisory._window_name("US", datetime(2026, 5, 15, 15, 15, tzinfo=berlin)) == "us_premarket"
    assert advisory._window_name("US", datetime(2026, 5, 15, 15, 30, tzinfo=berlin)) == "us_open"
    assert advisory._window_name("US", datetime(2026, 5, 15, 17, 30, tzinfo=berlin)) == "us_midday"
    assert advisory._window_name("US", datetime(2026, 5, 15, 20, 30, tzinfo=berlin)) == "us_power_hour"
    assert advisory._window_name("US", datetime(2026, 5, 15, 21, 30, tzinfo=berlin)) == "us_close"


def test_trade_republic_morning_start_can_be_lowered_after_data_verification(monkeypatch):
    berlin = timezone(timedelta(hours=2))
    monkeypatch.setenv("ADVISORY_TR_MORNING_START_MINUTES", "450")

    assert advisory._window_name("EU", datetime(2026, 5, 15, 7, 29, tzinfo=berlin)) is None
    assert advisory._window_name("EU", datetime(2026, 5, 15, 7, 30, tzinfo=berlin)) == "tr_morning_watch"


def test_entry_plan_caps_size_by_risk_and_capital():
    cfg = _cfg(risk_per_trade_eur=50.0, a_plus_max_size_eur=1800.0)

    plan = advisory._entry_plan(price=100.0, side="BUY", atr_pct=1.0, currency="USD", cfg=cfg, grade="A+")

    assert plan["suggested_size_eur"] == pytest.approx(1500.0)
    assert plan["risk_eur"] == pytest.approx(21.0)
    assert plan["stop_price"] < 100.0
    assert plan["target_1"] > 100.0
    assert plan["do_not_chase_price"] > plan["entry_max"]


def test_trend_1h_alignment_scores_direction(monkeypatch):
    import backend.signals.engine as signal_engine

    index = pd.date_range("2026-05-15T10:00:00Z", periods=9, freq="1h")
    bars = pd.DataFrame(
        {"Close": [100, 101, 102, 103, 104, 105, 106, 107, 108]},
        index=index,
    )
    monkeypatch.setattr(signal_engine, "_get_bars", lambda *args, **kwargs: bars, raising=False)

    result = advisory._trend_1h_alignment("AMZN", "BUY", 0.42)

    assert result["status"] == "ok"
    assert result["direction"] == "bullish"
    assert result["aligned"] is True
    assert result["bars"] == 9


def test_trade_card_is_actionable_for_manual_execution():
    cfg = _cfg()
    plan = advisory._entry_plan(price=100.0, side="BUY", atr_pct=1.0, currency="USD", cfg=cfg, grade="A")
    signal = {
        "id": 123,
        "mode": "live",
        "alert_stage": "trade",
        "market": "US",
        "data_symbol": "NVDA",
        "broker_display_name": "NVIDIA",
        "currency": "USD",
        "side": "BUY",
        "valid_until_cet": "15:42 Berlin",
        "time_exit_cet": "20:55 Berlin",
        "rationale": "A setup, VWAP +0.30, ORB +0.50",
        "grade": "A",
        "composite_score": 0.51,
        "ev_net_pct": 0.72,
        "fx_rate": 1.08,
        **plan,
    }

    card = advisory._format_trade_card(signal)

    assert "LIMIT BUY" in card
    assert "LIVE TRADE ALERT" in card
    assert "Action:" in card
    assert "avoid chasing above max" in card
    assert "Levels: stop" in card
    assert "Valid:" in card
    assert "Native ref:" not in card
    assert "EUR/USD" not in card
    assert "Composite:" not in card
    assert "EV: +0.72%" in card
    assert "Mark as taken:" in card
    assert "mark_id=123" in card


def test_shadow_trade_card_is_clearly_observation_only():
    cfg = _cfg()
    plan = advisory._entry_plan(price=180.0, side="BUY", atr_pct=0.9, currency="EUR", cfg=cfg, grade="A")
    signal = {
        "mode": "shadow",
        "market": "EU",
        "data_symbol": "SAP.DE",
        "broker_display_name": "SAP",
        "exchange": "Xetra",
        "currency": "EUR",
        "side": "BUY",
        "valid_until_cet": "09:57 Berlin",
        "time_exit_cet": "16:45 Berlin",
        "rationale": "A setup, VWAP +0.30, ORB +0.50",
        "grade": "A",
        "composite_score": 0.51,
        "ev_net_pct": 0.20,
        "fx_rate": 1.08,
        **plan,
    }

    card = advisory._format_trade_card(signal)

    assert "SHADOW OBSERVATION" in card
    assert "do not trade from shadow mode" in card
    assert "LIMIT BUY" in card
    assert "Native ref:" not in card


def test_watch_trade_card_is_not_a_buy_now_alert():
    cfg = _cfg()
    plan = advisory._entry_plan(price=100.0, side="BUY", atr_pct=1.0, currency="USD", cfg=cfg, grade="B")
    signal = {
        "mode": "live",
        "alert_stage": "watch",
        "market": "US",
        "data_symbol": "AMZN",
        "broker_display_name": "Amazon",
        "currency": "USD",
        "side": "BUY",
        "valid_until_cet": "15:52 Berlin",
        "time_exit_cet": "20:55 Berlin",
        "rationale": "B setup, VWAP +0.20, ORB +0.30",
        "grade": "B",
        "composite_score": 0.31,
        "ev_net_pct": 0.20,
        "fx_rate": 1.08,
        "late_chase_json": {},
        **plan,
    }

    card = advisory._format_trade_card(signal)

    assert "WATCH ONLY" in card
    assert "Early signal only" in card
    assert "LIVE TRADE ALERT" not in card
    assert "Pullback zone:" in card
    assert "€92." in card
    assert "Native ref:" not in card
    assert "do not chase >" not in card
    assert "Tentative size:" in card
    assert "Levels: stop" in card
    assert "Size/risk:" not in card
    assert "Exit: stop" not in card
    assert "EV:" not in card


def test_sell_trade_card_uses_direction_aware_action():
    cfg = _cfg(allow_short=True)
    plan = advisory._entry_plan(price=100.0, side="SELL", atr_pct=1.0, currency="USD", cfg=cfg, grade="A")
    signal = {
        "mode": "live",
        "alert_stage": "trade",
        "market": "US",
        "data_symbol": "TSLA",
        "broker_display_name": "Tesla",
        "currency": "USD",
        "side": "SELL",
        "valid_until_cet": "16:31 Berlin",
        "time_exit_cet": "20:55 Berlin",
        "rationale": "A setup, VWAP -0.80, ORB -0.80",
        "grade": "A",
        "composite_score": -0.48,
        "ev_net_pct": 0.70,
        "fx_rate": 1.08,
        **plan,
    }

    card = advisory._format_trade_card(signal)

    assert "LIMIT SELL" in card
    assert "sell/short only inside range" in card
    assert "buy only inside range" not in card


def test_downside_risk_card_is_not_short_recommendation():
    cfg = _cfg()
    plan = advisory._entry_plan(price=100.0, side="SELL", atr_pct=1.0, currency="USD", cfg=cfg, grade="B")
    signal = {
        "mode": "live",
        "alert_stage": "downside",
        "market": "US",
        "data_symbol": "AMD",
        "broker_display_name": "AMD",
        "currency": "USD",
        "side": "SELL",
        "valid_until_cet": "18:00 Berlin",
        "time_exit_cet": "21:55 Berlin",
        "rationale": "B setup, VWAP -0.60, ORB -0.40",
        "grade": "B",
        "composite_score": -0.34,
        "ev_net_pct": None,
        "fx_rate": 1.08,
        "signal_json": {"alert_stage": "downside"},
        **plan,
    }

    card = advisory._format_trade_card(signal)

    assert "DOWNSIDE RISK" in card
    assert "not a short-trade recommendation" in card
    assert "protect longs" in card


def test_late_chase_watch_card_explains_pullback_needed():
    cfg = _cfg()
    plan = advisory._entry_plan(price=100.0, side="BUY", atr_pct=1.0, currency="USD", cfg=cfg, grade="A")
    signal = {
        "mode": "live",
        "alert_stage": "watch",
        "market": "US",
        "data_symbol": "AMZN",
        "broker_display_name": "Amazon",
        "currency": "USD",
        "side": "BUY",
        "valid_until_cet": "16:31 Berlin",
        "time_exit_cet": "20:55 Berlin",
        "rationale": "A setup, VWAP -0.80, ORB +0.80",
        "grade": "A",
        "composite_score": 0.48,
        "ev_net_pct": 0.70,
        "fx_rate": 1.08,
        "late_chase_json": {"pct_deviation": 1.44, "threshold_pct": 0.67},
        **plan,
    }

    card = advisory._format_trade_card(signal)

    assert "WATCH ONLY" in card
    assert "extended" in card
    assert "setup is strong but extended" in card
    assert "wait for pullback" in card
    assert "LIVE TRADE ALERT" not in card
    assert "do not chase >" not in card
    assert "Tentative size:" in card
    assert "Levels: stop" in card
    assert "Native ref:" not in card
    assert "EV:" not in card


def test_runner_watch_card_distinguishes_fresh_entry_from_holder_context():
    cfg = _cfg()
    plan = advisory._entry_plan(price=150.0, side="BUY", atr_pct=1.0, currency="USD", cfg=cfg, grade="A")
    signal = {
        "mode": "live",
        "alert_stage": "watch",
        "market": "US",
        "data_symbol": "PLTR",
        "broker_display_name": "Palantir",
        "currency": "USD",
        "side": "BUY",
        "valid_until_cet": "16:31 Berlin",
        "time_exit_cet": "20:55 Berlin",
        "rationale": "A setup, VWAP +0.00, ORB +1.00",
        "grade": "A",
        "composite_score": 0.50,
        "ev_net_pct": 0.70,
        "fx_rate": 1.16,
        "late_chase_json": {"pct_deviation": 8.02, "threshold_pct": 0.85},
        "runner_context": {"prior_signal_id": 10, "prior_grade": "B", "prior_stage": "watch"},
        **plan,
    }

    card = advisory._format_trade_card(signal)

    assert "RUNNER WATCH" in card
    assert "same-day runner continuation" in card
    assert "fresh entry only on pullback" in card
    assert "wait for pullback into the band; no fresh chase" not in card


def test_extended_momentum_runner_card_warns_against_clean_fresh_entry():
    cfg = _cfg()
    plan = advisory._entry_plan(price=936.0, side="BUY", atr_pct=0.33, currency="USD", cfg=cfg, grade="C")
    signal = {
        "mode": "live",
        "alert_stage": "watch",
        "market": "US",
        "data_symbol": "MU",
        "broker_display_name": "Micron",
        "currency": "USD",
        "side": "BUY",
        "valid_until_cet": "16:00 Berlin",
        "time_exit_cet": "20:55 Berlin",
        "rationale": "C setup, VWAP -0.80, ORB +0.00",
        "grade": "C",
        "composite_score": 0.3246,
        "ev_net_pct": None,
        "fx_rate": 1.1519,
        "late_chase_json": {"pct_deviation": 2.59, "threshold_pct": 0.49},
        "runner_context": {
            "type": "extended_momentum_runner",
            "trend": {"ret_6h_pct": 9.38},
            "scores": {"relative_strength": 1.0, "tape_aggression": 0.34},
        },
        **plan,
    }

    card = advisory._format_trade_card(signal)

    assert "RUNNER WATCH" in card
    assert "extended runner" in card
    assert "extended momentum leader" in card
    assert "not a clean fresh entry" in card
    assert "buy only on pullback into the band" in card
    assert "protect fast near T1" in card


def test_runner_hold_card_uses_open_position_context():
    cfg = _cfg()
    plan = advisory._entry_plan(price=150.0, side="BUY", atr_pct=1.0, currency="USD", cfg=cfg, grade="A")
    signal = {
        "mode": "live",
        "alert_stage": "watch",
        "market": "US",
        "data_symbol": "PLTR",
        "broker_display_name": "Palantir",
        "currency": "USD",
        "side": "BUY",
        "valid_until_cet": "16:31 Berlin",
        "time_exit_cet": "20:55 Berlin",
        "rationale": "A setup, VWAP +0.00, ORB +1.00",
        "grade": "A",
        "composite_score": 0.50,
        "ev_net_pct": 0.70,
        "fx_rate": 1.16,
        "late_chase_json": {"pct_deviation": 8.02, "threshold_pct": 0.85},
        "runner_context": {"prior_signal_id": 10, "prior_grade": "B", "prior_stage": "watch"},
        "holding_context": {"pnl_pct": 4.25},
        **plan,
    }

    card = advisory._format_trade_card(signal)

    assert "RUNNER HOLD" in card
    assert "open position is +4.25%" in card
    assert "consider holding/trailing" in card


def test_pullback_confirmed_trade_card_closes_late_chase_loop():
    cfg = _cfg()
    plan = advisory._entry_plan(price=100.0, side="BUY", atr_pct=1.0, currency="USD", cfg=cfg, grade="A")
    signal = {
        "mode": "live",
        "alert_stage": "trade",
        "market": "US",
        "data_symbol": "AMZN",
        "broker_display_name": "Amazon",
        "currency": "USD",
        "side": "BUY",
        "valid_until_cet": "16:31 Berlin",
        "time_exit_cet": "20:55 Berlin",
        "rationale": "A setup, VWAP -0.10, ORB +0.80",
        "grade": "A",
        "composite_score": 0.48,
        "ev_net_pct": 0.70,
        "fx_rate": 1.08,
        "late_chase_json": {},
        "ignition_json": {},
        "pullback_confirmed": True,
        **plan,
    }

    card = advisory._format_trade_card(signal)

    assert "LIVE TRADE ALERT" in card
    assert "pullback confirmed" in card
    assert "entry band is valid again" in card


def test_mirror_trade_card_identifies_primary_us_symbol():
    cfg = _cfg()
    plan = advisory._entry_plan(price=180.0, side="BUY", atr_pct=0.9, currency="EUR", cfg=cfg, grade="A")
    signal = {
        "mode": "shadow",
        "market": "EU",
        "data_symbol": "NVD.DE",
        "broker_display_name": "NVIDIA (Xetra)",
        "exchange": "Xetra",
        "currency": "EUR",
        "listing_type": "eu_us_mirror",
        "primary_symbol": "NVDA",
        "side": "BUY",
        "valid_until_cet": "09:57 Berlin",
        "time_exit_cet": "16:45 Berlin",
        "rationale": "A setup, VWAP +0.30, ORB +0.50",
        "grade": "A",
        "composite_score": 0.51,
        "ev_net_pct": 0.20,
        "fx_rate": 1.08,
        **plan,
    }

    card = advisory._format_trade_card(signal)

    assert "Pre-Nasdaq mirror of NVDA" in card
    assert "early EU read" in card


def test_runner_context_requires_prior_b_plus_watch_or_trade():
    now = datetime(2026, 5, 29, 16, 0, tzinfo=timezone(timedelta(hours=2)))
    candidate = {
        "mode": "live",
        "alert_stage": "watch",
        "market": "US",
        "data_symbol": "PLTR",
        "side": "BUY",
        "grade": "A",
        "composite_score": 0.50,
        "late_chase_json": {"pct_deviation": 8.0},
        "signal_json": {
            "trend_1h": {"aligned": True},
            "atr_data": {"current_price": 157.0},
        },
    }
    old_c_ignition = [{
        "id": 9,
        "market": "US",
        "data_symbol": "PLTR",
        "side": "BUY",
        "grade": "C",
        "status": "skipped",
        "created_at": "2026-05-29T13:05:00+00:00",
        "signal_json": {"alert_stage": "ignition"},
    }]
    b_watch = [{
        "id": 10,
        "market": "US",
        "data_symbol": "PLTR",
        "side": "BUY",
        "grade": "B",
        "status": "skipped",
        "created_at": "2026-05-29T13:53:00+00:00",
        "signal_json": {"alert_stage": "watch"},
    }]

    assert advisory._runner_context(candidate, old_c_ignition, now, [], _cfg()) == {}
    runner = advisory._runner_context(candidate, b_watch, now, [], _cfg())
    assert runner["type"] == "runner_continuation"
    assert runner["prior_signal_id"] == 10


def test_extended_momentum_runner_context_captures_mu_like_pullback_setup():
    candidate = {
        "mode": "live",
        "alert_stage": "watch",
        "market": "US",
        "data_symbol": "MU",
        "side": "BUY",
        "grade": "C",
        "composite_score": 0.3246,
        "breakout_quality": 0.4482,
        "late_chase_json": {"pct_deviation": 2.59, "threshold_pct": 0.49},
        "signal_json": {
            "scores": {
                "relative_strength": 1.0,
                "bollinger_squeeze": 0.70,
                "tape_aggression": 0.342,
                "macd_crossover": 0.398,
                "vwap_deviation": -0.80,
                "rsi_divergence": -0.485,
            },
            "trend_1h": {
                "aligned": True,
                "direction": "bullish",
                "ret_3h_pct": 4.1713,
                "ret_6h_pct": 9.3836,
                "score": 1.0,
            },
        },
    }

    runner = advisory._extended_momentum_runner_context(candidate, [], _cfg())

    assert runner["type"] == "extended_momentum_runner"
    assert runner["reason"] == "extended_leader_pullback"
    assert runner["scores"]["relative_strength"] == pytest.approx(1.0)
    assert runner["trend"]["ret_6h_pct"] == pytest.approx(9.3836)


def test_extended_momentum_runner_context_rejects_weak_extended_watch():
    candidate = {
        "mode": "live",
        "alert_stage": "watch",
        "market": "US",
        "data_symbol": "AVGO",
        "side": "BUY",
        "grade": "C",
        "composite_score": 0.258,
        "breakout_quality": 0.419,
        "late_chase_json": {"pct_deviation": 2.0, "threshold_pct": 0.5},
        "signal_json": {
            "scores": {
                "relative_strength": 0.55,
                "bollinger_squeeze": 0.10,
                "tape_aggression": 0.05,
                "macd_crossover": 0.10,
            },
            "trend_1h": {
                "aligned": True,
                "direction": "bullish",
                "ret_3h_pct": 4.0,
                "ret_6h_pct": 7.0,
            },
        },
    }

    assert advisory._extended_momentum_runner_context(candidate, [], _cfg()) == {}


def test_runner_context_adds_holder_only_when_position_not_deep_underwater():
    now = datetime(2026, 5, 29, 16, 0, tzinfo=timezone(timedelta(hours=2)))
    candidate = {
        "mode": "live",
        "alert_stage": "watch",
        "market": "US",
        "data_symbol": "PLTR",
        "side": "BUY",
        "grade": "A",
        "composite_score": 0.50,
        "late_chase_json": {"pct_deviation": 8.0},
        "signal_json": {
            "trend_1h": {"aligned": True},
            "atr_data": {"current_price": 157.0},
        },
    }
    recent = [{
        "id": 10,
        "market": "US",
        "data_symbol": "PLTR",
        "side": "BUY",
        "grade": "B",
        "status": "skipped",
        "created_at": "2026-05-29T13:53:00+00:00",
        "signal_json": {"alert_stage": "watch"},
    }]
    open_position = {
        "id": 22,
        "data_symbol": "PLTR",
        "side": "BUY",
        "currency": "USD",
        "fx_rate": 1.16,
        "manual_entry_price": 150.0,
    }
    underwater_position = {**open_position, "manual_entry_price": 170.0}

    runner = advisory._runner_context(candidate, recent, now, [open_position], _cfg())
    underwater = advisory._runner_context(candidate, recent, now, [underwater_position], _cfg())

    assert runner["holder_context"]["position_id"] == 22
    assert runner["holder_context"]["pnl_pct"] == pytest.approx(4.6667)
    assert underwater["holder_context"] == {}
    assert underwater["position_context"]["meaningful_holder_context"] is False


def test_discord_alert_requires_a_grade_or_better():
    cfg = _cfg()

    assert advisory._should_send_discord({"mode": "live", "market": "US", "grade": "A"}, cfg) is True
    assert advisory._should_send_discord({"mode": "live", "market": "US", "grade": "A+"}, cfg) is True
    assert advisory._should_send_discord({"mode": "live", "market": "US", "grade": "B"}, cfg) is False
    assert advisory._should_send_discord({"mode": "shadow", "market": "EU", "grade": "C"}, cfg) is False


def test_shadow_trade_card_does_not_overstate_c_grade():
    cfg = _cfg()
    plan = advisory._entry_plan(price=180.0, side="BUY", atr_pct=0.9, currency="EUR", cfg=cfg, grade="C")
    signal = {
        "mode": "shadow",
        "market": "EU",
        "data_symbol": "SAP.DE",
        "broker_display_name": "SAP",
        "exchange": "Xetra",
        "currency": "EUR",
        "side": "BUY",
        "valid_until_cet": "09:57 Berlin",
        "time_exit_cet": "16:45 Berlin",
        "rationale": "C setup, VWAP +0.10",
        "grade": "C",
        "composite_score": 0.12,
        "ev_net_pct": -0.02,
        "fx_rate": 1.08,
        **plan,
    }

    card = advisory._format_trade_card(signal)

    assert "LOW-GRADE SHADOW BUY OPPORTUNITY" in card
    assert "VERY GOOD" not in card


def test_shadow_discord_uses_separate_min_grade():
    cfg = _cfg(shadow_min_discord_grade="B")

    assert advisory._should_send_discord({"mode": "shadow", "market": "EU", "grade": "B"}, cfg) is True
    assert advisory._should_send_discord({"mode": "shadow", "market": "EU", "grade": "C"}, cfg) is False


def test_ordered_markets_prioritizes_live_alerts_before_shadow_learning():
    cfg = _cfg(markets={"US", "EU"}, live_markets={"US"}, shadow_markets={"EU"})

    assert advisory._ordered_markets(cfg) == ["US", "EU"]


def test_eu_mirror_universe_is_metadata_tagged():
    mirrors = [
        item for item in advisory.ADVISORY_UNIVERSE["EU"]
        if item.get("listing_type") == "eu_us_mirror"
    ]

    assert len(advisory.ADVISORY_UNIVERSE["EU"]) == 24  # 12 native + 12 mirrors
    assert len(mirrors) == 12
    assert all(item.get("origin_market") == "US" for item in mirrors)
    assert all(item.get("primary_symbol") for item in mirrors)
    assert all(item.get("mirror_only_windows") == ["tr_morning_watch", "eu_open"] for item in mirrors)
    assert {"AVGO", "MU"}.issubset({item.get("primary_symbol") for item in mirrors})
    assert abs(sum(advisory.EU_MIRROR_WEIGHTS.values()) - 1.0) < 0.001


def test_us_avgo_is_high_priority_for_immediate_send():
    avgo = next(item for item in advisory.ADVISORY_UNIVERSE["US"] if item.get("data_symbol") == "AVGO")

    assert avgo["priority"] == "high"
    assert avgo["trade_target"] is True


def _make_fake_bars(n_rows, volume):
    """Build a MagicMock that satisfies all _data_quality access patterns."""
    from unittest.mock import MagicMock
    bars = MagicMock()
    bars.empty = False
    bars.__len__.return_value = n_rows
    # bars.index[-1].to_pydatetime() → a recent tz-aware datetime
    ts = MagicMock()
    ts.to_pydatetime.return_value = datetime.now(timezone.utc) - timedelta(minutes=2)
    bars.index.__getitem__.return_value = ts
    # bars.tail(20): "Volume" in tail → True; tail["Volume"].mean() → float
    recent = MagicMock()
    recent.__contains__.return_value = True
    recent.__getitem__.return_value.mean.return_value = float(volume)
    bars.tail.return_value = recent
    # bars["Close"].squeeze().iloc[-1] → 100.0
    bars.__getitem__.return_value.squeeze.return_value.iloc.__getitem__.return_value = 100.0
    return bars


def _make_ignition_bars(direction="up"):
    """Build a MagicMock satisfying _ignition_check access patterns (no real pandas needed)."""
    from unittest.mock import MagicMock

    closes = [100.00, 100.01, 100.02, 100.03, 100.04, 100.05, 100.10, 100.18, 100.28, 100.40, 100.55, 100.70]
    if direction == "down":
        closes = list(reversed(closes))
    volumes = [1000, 950, 980, 1020, 990, 1000, 3200, 3400, 3600, 3500, 3700, 3900]
    # window_bars = min(5, 12-1) = 5
    # close.iloc[-6] = closes[6]; close.iloc[-1] = closes[-1]
    # volume.iloc[-5:].mean() = mean(volumes[-5:])
    # volume.iloc[:-5].tail(20).mean() = mean(volumes[:-5])

    def _make_series(values):
        s = MagicMock()
        s.squeeze = MagicMock(return_value=s)

        def _iloc_getitem(idx):
            if isinstance(idx, slice):
                sliced = values[idx]
                sl = MagicMock()
                sl.__len__ = MagicMock(return_value=len(sliced))
                sl.mean = MagicMock(return_value=sum(sliced) / len(sliced) if sliced else 0.0)
                def _tail(n):
                    tv = sliced[-n:] if n < len(sliced) else sliced
                    tm = MagicMock()
                    tm.__len__ = MagicMock(return_value=len(tv))
                    tm.mean = MagicMock(return_value=sum(tv) / len(tv) if tv else 0.0)
                    return tm
                sl.tail = _tail
                return sl
            return values[idx]

        il = MagicMock()
        il.__getitem__ = MagicMock(side_effect=_iloc_getitem)
        s.iloc = il
        return s

    bars = MagicMock()
    bars.empty = False
    bars.__len__ = MagicMock(return_value=12)
    close_s = _make_series(closes)
    volume_s = _make_series(volumes)

    def _getitem(key):
        return close_s if key == "Close" else volume_s

    bars.__getitem__ = MagicMock(side_effect=_getitem)
    return bars


def _make_fx_bars(rate=1.0923):
    idx = pd.date_range(end=pd.Timestamp.now(tz="UTC"), periods=3, freq="D")
    return pd.DataFrame({
        "Close": [rate - 0.002, rate - 0.001, rate],
        "High": [rate] * 3,
        "Low": [rate - 0.004] * 3,
        "Open": [rate - 0.003] * 3,
        "Volume": [0, 0, 0],
    }, index=idx)


def _make_fx_multiindex_bars(rate=1.1664):
    idx = pd.date_range(end=pd.Timestamp.now(tz="UTC"), periods=3, freq="D")
    cols = pd.MultiIndex.from_tuples([
        ("Close", "EURUSD=X"),
        ("High", "EURUSD=X"),
        ("Low", "EURUSD=X"),
        ("Open", "EURUSD=X"),
        ("Volume", "EURUSD=X"),
    ])
    return pd.DataFrame([
        [rate - 0.002, rate, rate - 0.004, rate - 0.003, 0],
        [rate - 0.001, rate, rate - 0.004, rate - 0.003, 0],
        [rate, rate, rate - 0.004, rate - 0.003, 0],
    ], columns=cols, index=idx)


def test_fetch_latest_eurusd_rate_extracts_multiindex_close(monkeypatch):
    monkeypatch.setattr(advisory, "_get_bars", lambda *args, **kwargs: _make_fx_multiindex_bars(1.1664))

    rate = advisory._fetch_latest_eurusd_rate()

    assert rate == pytest.approx(1.1664)


def test_fetch_latest_eurusd_rate_falls_back_when_shared_bars_reject_fx(monkeypatch):
    class FakeYf:
        @staticmethod
        def download(*args, **kwargs):
            return _make_fx_bars(1.1652)

    monkeypatch.setattr(advisory, "_get_bars", lambda *args, **kwargs: None)
    monkeypatch.setitem(__import__("sys").modules, "yfinance", FakeYf)

    rate = advisory._fetch_latest_eurusd_rate()

    assert rate == pytest.approx(1.1652)


def test_eu_mirror_data_quality_uses_relaxed_rows_but_volume_floor(monkeypatch):
    monkeypatch.setattr(advisory, "_get_bars", lambda *a, **kw: _make_fake_bars(25, 500))

    native = advisory._data_quality("SAP.DE", "EU")               # 25 rows < 45 → too_few_bars
    mirror = advisory._data_quality("NVD.DE", "EU", listing_type="eu_us_mirror")  # 25 rows >= 20, vol=500 → ok

    assert native["reason"] == "too_few_bars"
    assert mirror["ok"] is True
    assert mirror["avg_recent_volume"] == pytest.approx(500.0)


def test_eu_mirror_data_quality_blocks_thin_volume(monkeypatch):
    monkeypatch.setattr(advisory, "_get_bars", lambda *a, **kw: _make_fake_bars(25, 250))

    result = advisory._data_quality("NVD.DE", "EU", listing_type="eu_us_mirror")

    assert result["ok"] is False
    assert result["reason"] == "eu_mirror_thin_volume"


def test_us_data_quality_relaxes_early_session_rows(monkeypatch):
    monkeypatch.setattr(advisory, "_get_bars", lambda *a, **kw: _make_fake_bars(12, 10000))

    result = advisory._data_quality("AMZN", "US")

    assert result["ok"] is True
    assert result["early_session_relaxed"] is True
    assert result["required_rows"] == 30


def test_daily_fx_rate_uses_same_day_cache_without_fetch(monkeypatch):
    monkeypatch.setattr(advisory, "_today_utc_date", lambda: "2026-05-27")
    monkeypatch.setattr(advisory, "get_fx_rate_cache", lambda *args, **kwargs: {
        "pair": "EURUSD",
        "rate_date": "2026-05-27",
        "rate": 1.0912,
        "source": "yfinance_daily",
        "fetched_at": "2026-05-27T06:00:00+00:00",
    })
    monkeypatch.setattr(advisory, "_get_bars", lambda *args, **kwargs: pytest.fail("cache hit should not fetch FX bars"))

    fx = advisory._resolve_daily_fx_rate()

    assert fx["rate"] == pytest.approx(1.0912)
    assert fx["source"] == "yfinance_daily"


def test_daily_fx_rate_fetches_once_and_caches_on_miss(monkeypatch):
    # Patch _fetch_latest_eurusd_rate directly rather than routing through
    # _get_bars / _make_fx_bars.  The conftest mocks pd.Timestamp = MagicMock
    # when real pandas is absent, which makes pd.Timestamp.now() explode inside
    # _make_fx_bars.  This test is about _resolve_daily_fx_rate caching logic;
    # the fetch internals are tested separately.
    writes = []

    monkeypatch.setattr(advisory, "_today_utc_date", lambda: "2026-05-27")
    monkeypatch.setattr(advisory, "get_fx_rate_cache", lambda *args, **kwargs: None)
    monkeypatch.setattr(advisory, "_fetch_latest_eurusd_rate", lambda: 1.0945)
    monkeypatch.setattr(advisory, "upsert_fx_rate_cache", lambda pair, rate, source, rate_date=None, meta=None: (
        writes.append((pair, rate, source, rate_date, meta)) or {"fetched_at": "2026-05-27T07:00:00+00:00"}
    ))

    fx = advisory._resolve_daily_fx_rate()

    assert fx["rate"] == pytest.approx(1.0945)
    assert fx["source"] == "yfinance_daily"
    assert writes == [("EURUSD", pytest.approx(1.0945), "yfinance_daily", "2026-05-27", {"symbol": "EURUSD=X"})]


def test_daily_fx_rate_uses_env_fallback_when_cache_and_fetch_fail(monkeypatch):
    monkeypatch.setenv("EURUSD_RATE", "1.22")
    monkeypatch.setattr(advisory, "_today_utc_date", lambda: "2026-05-27")
    monkeypatch.setattr(advisory, "get_fx_rate_cache", lambda *args, **kwargs: None)
    monkeypatch.setattr(advisory, "_get_bars", lambda *args, **kwargs: None)

    fx = advisory._resolve_daily_fx_rate()

    assert fx["rate"] == pytest.approx(1.22)
    assert fx["source"] == "env_fallback"


def test_daily_fx_rate_returns_unavailable_when_env_also_missing(monkeypatch):
    monkeypatch.delenv("EURUSD_RATE", raising=False)
    monkeypatch.setattr(advisory, "_today_utc_date", lambda: "2026-05-27")
    monkeypatch.setattr(advisory, "get_fx_rate_cache", lambda *args, **kwargs: None)
    monkeypatch.setattr(advisory, "_get_bars", lambda *args, **kwargs: None)

    fx = advisory._resolve_daily_fx_rate()

    assert fx["rate"] == 0.0
    assert fx["source"] == "unavailable"


def test_daily_fx_rate_ignores_implausible_env_value(monkeypatch):
    monkeypatch.setenv("EURUSD_RATE", "42.0")
    monkeypatch.setattr(advisory, "_today_utc_date", lambda: "2026-05-27")
    monkeypatch.setattr(advisory, "get_fx_rate_cache", lambda *args, **kwargs: None)
    monkeypatch.setattr(advisory, "_get_bars", lambda *args, **kwargs: None)

    fx = advisory._resolve_daily_fx_rate()

    assert fx["rate"] == 0.0
    assert fx["source"] == "unavailable"


def test_ignition_debug_logs_when_env_flag_set(monkeypatch):
    monkeypatch.setenv("ADVISORY_IGNITION_DEBUG", "true")
    logs = []
    monkeypatch.setattr(advisory, "log_event",
                        lambda level, event, detail: logs.append((level, event, detail)))

    # Insufficient bars path: only 4 rows returned.
    monkeypatch.setattr(advisory, "_get_bars", lambda *a, **kw: _make_fake_bars(4, 1000))
    result = advisory._ignition_check("AMZN", "BUY", 0.10, {"atr_pct": 0.3})

    assert result == {}
    diag = [d for lvl, ev, d in logs if ev == "advisory_ignition_debug"]
    assert diag, "expected debug log when ADVISORY_IGNITION_DEBUG is set"
    assert diag[0]["reason"] == "insufficient_bars"
    assert diag[0]["bar_count"] == 4
    assert diag[0]["symbol"] == "AMZN"


def test_ignition_debug_silent_when_env_flag_unset(monkeypatch):
    monkeypatch.delenv("ADVISORY_IGNITION_DEBUG", raising=False)
    logs = []
    monkeypatch.setattr(advisory, "log_event",
                        lambda level, event, detail: logs.append((level, event, detail)))

    monkeypatch.setattr(advisory, "_get_bars", lambda *a, **kw: _make_fake_bars(4, 1000))
    advisory._ignition_check("AMZN", "BUY", 0.10, {"atr_pct": 0.3})

    assert not [d for lvl, ev, d in logs if ev == "advisory_ignition_debug"]


def test_expired_sent_signal_does_not_count_as_open_live_signal():
    now = datetime(2026, 5, 15, 14, 0, tzinfo=timezone.utc)
    expired = {
        "status": "sent",
        "valid_until": "2026-05-15T13:59:00+00:00",
    }
    entered = {
        "status": "entered",
        "valid_until": "2026-05-15T13:59:00+00:00",
    }

    assert advisory._is_open_live_signal(expired, now) is False
    assert advisory._is_open_live_signal(entered, now) is True


def test_duplicate_suppression_is_current_session_and_ticker_specific():
    berlin = timezone(timedelta(hours=2))
    now = datetime(2026, 5, 15, 15, 45, tzinfo=berlin)
    recent = [
        {
            "market": "US",
            "data_symbol": "NVDA",
            "status": "sent",
            "created_at": "2026-05-15T13:35:00+00:00",
            "valid_until": "2026-05-15T13:50:00+00:00",
        },
        {
            "market": "US",
            "data_symbol": "AMD",
            "status": "sent",
            "created_at": "2026-05-15T13:35:00+00:00",
            "valid_until": "2026-05-15T13:50:00+00:00",
        },
    ]

    assert advisory._alerted_symbol_in_session(recent, "NVDA", "US", now) is True
    assert advisory._alerted_symbol_in_session(recent, "AAPL", "US", now) is False


def test_morning_alert_does_not_count_in_us_afternoon_session():
    berlin = timezone(timedelta(hours=2))
    afternoon = datetime(2026, 5, 15, 20, 15, tzinfo=berlin)
    signal = {
        "market": "US",
        "status": "sent",
        "created_at": "2026-05-15T13:40:00+00:00",
    }

    assert advisory._is_signal_in_current_session(signal, afternoon) is False


def test_recent_watch_suppression_matches_watch_validity_window():
    berlin = timezone(timedelta(hours=2))
    now = datetime(2026, 5, 15, 16, 30, tzinfo=berlin)
    recent = [
        {
            "market": "US",
            "data_symbol": "AMZN",
            "status": "skipped",
            "created_at": "2026-05-15T14:00:00+00:00",
        },
        {
            "market": "US",
            "data_symbol": "NVDA",
            "status": "skipped",
            "created_at": "2026-05-15T13:44:00+00:00",
        },
    ]

    assert advisory._recent_watch_signal_in_session(recent, "AMZN", "US", now)["data_symbol"] == "AMZN"
    assert advisory._recent_watch_signal_in_session(recent, "NVDA", "US", now) is None


def test_watch_signal_counts_only_current_session_watches():
    berlin = timezone(timedelta(hours=2))
    now = datetime(2026, 5, 15, 16, 30, tzinfo=berlin)
    recent = [
        {
            "market": "US",
            "data_symbol": "AMZN",
            "status": "skipped",
            "created_at": "2026-05-15T14:00:00+00:00",
        },
        {
            "market": "US",
            "data_symbol": "AMZN",
            "status": "sent",
            "created_at": "2026-05-15T14:05:00+00:00",
        },
        {
            "market": "US",
            "data_symbol": "META",
            "status": "skipped",
            "created_at": "2026-05-15T12:50:00+00:00",
        },
    ]

    counts, total = advisory._watch_signal_counts_in_session(recent, now)

    assert counts == {("US", "AMZN"): 1}
    assert total == 1


def test_watch_repeat_allows_material_strengthening():
    recent = {
        "status": "skipped",
        "grade": "C",
        "composite_score": 0.27,
        "breakout_quality": 0.35,
    }
    stronger_grade = {"alert_stage": "watch", "grade": "B", "composite_score": 0.30, "breakout_quality": 0.36}
    stronger_composite = {"alert_stage": "watch", "grade": "C", "composite_score": 0.39, "breakout_quality": 0.36}
    stale_repeat = {"alert_stage": "watch", "grade": "C", "composite_score": 0.30, "breakout_quality": 0.36}

    assert advisory._watch_repeat_blocked(recent, stronger_grade) is False
    assert advisory._watch_repeat_blocked(recent, stronger_composite) is False
    assert advisory._watch_repeat_blocked(recent, stale_repeat) is True


def test_watch_repeat_allows_ignition_to_escalate_to_watch():
    recent = {
        "status": "skipped",
        "grade": "C",
        "composite_score": 0.12,
        "breakout_quality": 0.18,
        "signal_json": {"alert_stage": "ignition"},
    }
    stronger_watch = {"alert_stage": "watch", "grade": "C", "composite_score": 0.20, "breakout_quality": 0.22}
    stale_ignition = {"alert_stage": "ignition", "grade": "C", "composite_score": 0.15, "breakout_quality": 0.22}

    assert advisory._watch_repeat_blocked(recent, stronger_watch) is False
    assert advisory._watch_repeat_blocked(recent, stale_ignition) is True


def test_run_advisory_cycle_logs_and_sends_single_best_live_signal(monkeypatch):
    berlin = timezone(timedelta(hours=2))
    saved = []
    sent = []

    monkeypatch.setattr(advisory, "load_config", lambda: _cfg())
    monkeypatch.setattr(advisory, "_now_cet", lambda: datetime(2026, 5, 15, 15, 45, tzinfo=berlin))
    monkeypatch.setattr(advisory, "ADVISORY_UNIVERSE", {
        "US": [{"data_symbol": "NVDA", "broker_display_name": "NVIDIA", "exchange": "NASDAQ", "currency": "USD"}]
    })
    monkeypatch.setattr(advisory, "get_recent_advisory_signals", lambda **kwargs: [])
    monkeypatch.setattr(advisory, "get_recent_trades", lambda days=90: [])
    monkeypatch.setattr(advisory, "_data_quality", lambda symbol, market, listing_type=None: {
        "ok": True, "last_price": 100.0, "rows": 90, "age_minutes": 1.0, "avg_recent_volume": 100000,
    })
    monkeypatch.setattr(advisory, "detect_regime", lambda symbol: SimpleNamespace(
        market_regime="bull", intraday_regime="trending",
    ))
    monkeypatch.setattr(advisory, "compute_all_signals", lambda symbol, weights, regime_state=None: {
        "composite_score": 0.62,
        "signals": {
            "vwap_deviation": {"score": 0.55},
            "macd_crossover": {"score": 0.55},
            "relative_strength": {"score": 0.50},
            "orb": {"score": 0.65, "meta": {"active": True}},
            "news_sentiment": {"score": 0.10},
        },
        "atr_data": {"atr_pct": 1.0},
    })
    monkeypatch.setattr(advisory, "compute_expected_value", lambda *args, **kwargs: {
        "net_ev_pct": 0.82, "confidence": 0.74,
    })
    monkeypatch.setattr(advisory, "_market_context", lambda market: {"market": market})
    monkeypatch.setattr(advisory, "insert_advisory_signal", lambda signal: saved.append(signal) or {"id": 1})
    monkeypatch.setattr(advisory, "update_advisory_exit_status", lambda signal_id, update: update)
    monkeypatch.setattr(advisory, "_send_discord", lambda text, webhook_url: sent.append(text) or True)
    monkeypatch.setattr(advisory, "log_event", lambda *args, **kwargs: None)

    result = advisory.run_advisory_cycle()

    assert result["emitted"] == 1
    assert result["live_sent_today"] == 1
    assert len(saved) == 1
    assert saved[0]["status"] == "sent"
    assert advisory._parse_dt(saved[0]["valid_until"]) == datetime(2026, 5, 15, 14, 0, tzinfo=timezone.utc)
    assert len(sent) == 1
    assert "NVDA" in sent[0]


def test_run_advisory_cycle_batches_diagnostic_writes(monkeypatch):
    # Scan snapshots + scan logs must be flushed in a single bulk round-trip
    # each at cycle end, not one write per ticker.
    berlin = timezone(timedelta(hours=2))
    snap_calls = []
    log_calls = []

    monkeypatch.setattr(advisory, "load_config", lambda: _cfg())
    monkeypatch.setattr(advisory, "_now_cet", lambda: datetime(2026, 5, 15, 15, 45, tzinfo=berlin))
    monkeypatch.setattr(advisory, "ADVISORY_UNIVERSE", {
        "US": [{"data_symbol": "NVDA", "broker_display_name": "NVIDIA", "exchange": "NASDAQ", "currency": "USD"}]
    })
    monkeypatch.setattr(advisory, "get_recent_advisory_signals", lambda **kwargs: [])
    monkeypatch.setattr(advisory, "get_recent_trades", lambda days=90: [])
    monkeypatch.setattr(advisory, "_data_quality", lambda symbol, market, listing_type=None: {
        "ok": True, "last_price": 100.0, "rows": 90, "age_minutes": 1.0, "avg_recent_volume": 100000,
    })
    monkeypatch.setattr(advisory, "detect_regime", lambda symbol: SimpleNamespace(
        market_regime="bull", intraday_regime="trending",
    ))
    monkeypatch.setattr(advisory, "compute_all_signals", lambda symbol, weights, regime_state=None: {
        "composite_score": 0.62,
        "signals": {
            "vwap_deviation": {"score": 0.55},
            "macd_crossover": {"score": 0.55},
            "relative_strength": {"score": 0.50},
            "orb": {"score": 0.65, "meta": {"active": True}},
            "news_sentiment": {"score": 0.10},
        },
        "atr_data": {"atr_pct": 1.0},
    })
    monkeypatch.setattr(advisory, "compute_expected_value", lambda *a, **k: {"net_ev_pct": 0.82, "confidence": 0.74})
    monkeypatch.setattr(advisory, "_market_context", lambda market: {"market": market})
    monkeypatch.setattr(advisory, "insert_advisory_signal", lambda signal: {"id": 1})
    monkeypatch.setattr(advisory, "update_advisory_exit_status", lambda signal_id, update: update)
    monkeypatch.setattr(advisory, "_send_discord", lambda *a, **k: True)
    monkeypatch.setattr(advisory, "log_event", lambda *a, **k: None)
    monkeypatch.setattr(advisory, "bulk_upsert_advisory_scan_snapshots",
                        lambda rows: snap_calls.append(rows) or {"written": len(rows)})
    monkeypatch.setattr(advisory, "bulk_insert_advisory_scan_logs",
                        lambda rows: log_calls.append(rows) or {"written": len(rows)})

    advisory.run_advisory_cycle()

    # One bulk flush each, carrying a list — not per-ticker writes.
    assert len(snap_calls) == 1
    assert len(log_calls) == 1
    assert isinstance(snap_calls[0], list) and len(snap_calls[0]) >= 1
    assert isinstance(log_calls[0], list) and len(log_calls[0]) >= 1


def test_run_advisory_cycle_flushes_early_continue_market_before_next_market(monkeypatch):
    berlin = timezone(timedelta(hours=2))
    flush_events = []

    cfg = _cfg(
        markets={"US", "EU"},
        live_markets={"US"},
        shadow_markets={"EU"},
        shadow_discord_markets={"EU"},
    )
    monkeypatch.setattr(advisory, "load_config", lambda: cfg)
    monkeypatch.setattr(advisory, "_now_cet", lambda: datetime(2026, 5, 15, 14, 0, tzinfo=berlin))
    monkeypatch.setattr(advisory, "_ordered_markets", lambda cfg: ["US", "EU"])
    monkeypatch.setattr(advisory, "ADVISORY_UNIVERSE", {
        "US": [{"data_symbol": "NVDA", "broker_display_name": "NVIDIA", "exchange": "NASDAQ", "currency": "USD"}],
        "EU": [{"data_symbol": "SAP.DE", "broker_display_name": "SAP", "exchange": "XETRA", "currency": "EUR"}],
    })
    monkeypatch.setattr(advisory, "get_recent_advisory_signals", lambda **kwargs: [])
    monkeypatch.setattr(advisory, "get_recent_trades", lambda days=90: [])
    monkeypatch.setattr(advisory, "_is_broker_supported",
                        lambda item, cfg: flush_events.append(("broker_check", item["data_symbol"])) or False)
    monkeypatch.setattr(advisory, "log_event", lambda *a, **k: None)
    monkeypatch.setattr(advisory, "bulk_upsert_advisory_scan_snapshots",
                        lambda rows: flush_events.append(("flush", [r["market"] for r in rows])) or {"written": len(rows)})
    monkeypatch.setattr(advisory, "bulk_insert_advisory_scan_logs",
                        lambda rows: {"written": len(rows)})

    advisory.run_advisory_cycle()

    assert flush_events[0] == ("flush", ["US"])
    assert flush_events[1] == ("broker_check", "SAP.DE")


def test_run_advisory_cycle_failed_bulk_writes_do_not_count_as_written(monkeypatch):
    berlin = timezone(timedelta(hours=2))
    logs = []

    monkeypatch.setattr(advisory, "load_config", lambda: _cfg())
    monkeypatch.setattr(advisory, "_now_cet", lambda: datetime(2026, 5, 15, 15, 45, tzinfo=berlin))
    monkeypatch.setattr(advisory, "ADVISORY_UNIVERSE", {
        "US": [{"data_symbol": "NVDA", "broker_display_name": "NVIDIA", "exchange": "NASDAQ", "currency": "USD"}]
    })
    monkeypatch.setattr(advisory, "get_recent_advisory_signals", lambda **kwargs: [])
    monkeypatch.setattr(advisory, "get_recent_trades", lambda days=90: [])
    monkeypatch.setattr(advisory, "_data_quality", lambda symbol, market, listing_type=None: {
        "ok": True, "last_price": 100.0, "rows": 90, "age_minutes": 1.0, "avg_recent_volume": 100000,
    })
    monkeypatch.setattr(advisory, "detect_regime", lambda symbol: SimpleNamespace(
        market_regime="bull", intraday_regime="trending",
    ))
    monkeypatch.setattr(advisory, "compute_all_signals", lambda symbol, weights, regime_state=None: {
        "composite_score": 0.62,
        "signals": {
            "vwap_deviation": {"score": 0.55},
            "macd_crossover": {"score": 0.55},
            "relative_strength": {"score": 0.50},
            "orb": {"score": 0.65, "meta": {"active": True}},
            "news_sentiment": {"score": 0.10},
        },
        "atr_data": {"atr_pct": 1.0},
    })
    monkeypatch.setattr(advisory, "compute_expected_value", lambda *a, **k: {"net_ev_pct": 0.82, "confidence": 0.74})
    monkeypatch.setattr(advisory, "_market_context", lambda market: {"market": market})
    monkeypatch.setattr(advisory, "insert_advisory_signal", lambda signal: {"id": 1})
    monkeypatch.setattr(advisory, "update_advisory_exit_status", lambda signal_id, update: update)
    monkeypatch.setattr(advisory, "_send_discord", lambda *a, **k: True)
    monkeypatch.setattr(advisory, "log_event", lambda level, event, detail=None: logs.append((event, detail or {})))
    monkeypatch.setattr(advisory, "bulk_upsert_advisory_scan_snapshots",
                        lambda rows: {"error": "snapshot boom"})
    monkeypatch.setattr(advisory, "bulk_insert_advisory_scan_logs",
                        lambda rows: {"error": "scanlog boom"})

    advisory.run_advisory_cycle()

    timing = next(detail for event, detail in logs if event == "advisory_cycle_timing")
    assert timing["snapshot_rows"] == 0
    assert timing["scanlog_rows"] == 0
    assert any(event == "advisory_scan_snapshot_bulk_failed" for event, _ in logs)
    assert any(event == "advisory_scan_log_bulk_failed" for event, _ in logs)


def test_trade_alert_creates_virtual_a_grade_entry(monkeypatch):
    berlin = timezone(timedelta(hours=2))
    saved = []
    updates = []

    monkeypatch.setattr(advisory, "load_config", lambda: _cfg())
    monkeypatch.setattr(advisory, "_now_cet", lambda: datetime(2026, 5, 15, 15, 45, tzinfo=berlin))
    monkeypatch.setattr(advisory, "ADVISORY_UNIVERSE", {
        "US": [{"data_symbol": "NVDA", "broker_display_name": "NVIDIA", "exchange": "NASDAQ", "currency": "USD"}]
    })
    monkeypatch.setattr(advisory, "get_recent_advisory_signals", lambda **kwargs: [])
    monkeypatch.setattr(advisory, "get_recent_trades", lambda days=90: [])
    monkeypatch.setattr(advisory, "_data_quality", lambda symbol, market, listing_type=None: {
        "ok": True, "last_price": 100.0, "rows": 90, "age_minutes": 1.0, "avg_recent_volume": 100000,
    })
    monkeypatch.setattr(advisory, "detect_regime", lambda symbol: SimpleNamespace(
        market_regime="bull", intraday_regime="trending",
    ))
    monkeypatch.setattr(advisory, "compute_all_signals", lambda symbol, weights, regime_state=None: {
        "composite_score": 0.62,
        "signals": {
            "vwap_deviation": {"score": 0.55},
            "macd_crossover": {"score": 0.55},
            "relative_strength": {"score": 0.50},
            "orb": {"score": 0.65, "meta": {"active": True}},
            "news_sentiment": {"score": 0.10},
        },
        "atr_data": {"atr_pct": 1.0},
    })
    monkeypatch.setattr(advisory, "compute_expected_value", lambda *args, **kwargs: {
        "net_ev_pct": 0.82, "confidence": 0.74,
    })
    monkeypatch.setattr(advisory, "_market_context", lambda market: {"market": market})
    monkeypatch.setattr(advisory, "insert_advisory_signal", lambda signal: saved.append(signal) or {"id": 77})
    monkeypatch.setattr(advisory, "update_advisory_exit_status", lambda signal_id, update: updates.append((signal_id, update)) or update)
    monkeypatch.setattr(advisory, "_send_discord", lambda *args, **kwargs: True)
    monkeypatch.setattr(advisory, "log_event", lambda *args, **kwargs: None)

    result = advisory.run_advisory_cycle()

    assert result["emitted"] == 1
    assert updates[0][0] == 77
    assert updates[0][1]["status"] == "entered"
    assert updates[0][1]["entry_triggered"] is True
    assert updates[0][1]["exit_monitor_json"]["virtual_entry"] is True


def test_run_advisory_cycle_sends_high_priority_live_before_rest_of_scan(monkeypatch):
    berlin = timezone(timedelta(hours=2))
    sent = []
    scan_events = []
    logs = []

    monkeypatch.setattr(advisory, "load_config", lambda: _cfg())
    monkeypatch.setattr(advisory, "_now_cet", lambda: datetime(2026, 5, 15, 15, 45, tzinfo=berlin))
    monkeypatch.setattr(advisory, "ADVISORY_UNIVERSE", {
        "US": [
            {"data_symbol": "PLTR", "broker_display_name": "Palantir", "exchange": "NASDAQ", "currency": "USD", "priority": "high"},
            {"data_symbol": "SLOW", "broker_display_name": "Slow Co", "exchange": "NASDAQ", "currency": "USD", "priority": "medium"},
        ]
    })
    monkeypatch.setattr(advisory, "get_recent_advisory_signals", lambda **kwargs: [])
    monkeypatch.setattr(advisory, "get_recent_trades", lambda days=90: [])
    monkeypatch.setattr(advisory, "_market_context", lambda market: {"market": market})

    def candidate(symbol):
        return {
            "market": "US",
            "mode": "live",
            "status": "skipped",
            "alert_stage": "watch",
            "data_symbol": symbol,
            "broker_display_name": symbol,
            "exchange": "NASDAQ",
            "currency": "USD",
            "side": "BUY",
            "priority": "high" if symbol == "PLTR" else "medium",
            "trade_target": True,
            "benchmark_only": False,
            "grade": "A",
            "composite_score": 0.48,
            "ev_net_pct": 0.4,
            "breakout_quality": 0.7,
            "valid_until": "2026-05-15T14:30:00+00:00",
            "time_exit_at": "2026-05-15T18:55:00+00:00",
            "signal_json": {},
        }

    def fake_scan(item, *args, **kwargs):
        symbol = item["data_symbol"]
        scan_events.append((symbol, len(sent)))
        return candidate(symbol)

    monkeypatch.setattr(advisory, "_scan_candidate", fake_scan)
    monkeypatch.setattr(advisory, "_format_trade_card", lambda signal: f"CARD {signal['data_symbol']}")
    monkeypatch.setattr(advisory, "insert_advisory_signal", lambda signal: {"id": 100 + len(scan_events)})
    monkeypatch.setattr(advisory, "_send_discord", lambda text, webhook_url: sent.append(text) or True)
    monkeypatch.setattr(advisory, "log_event", lambda level, event, detail=None: logs.append((event, detail or {})))

    result = advisory.run_advisory_cycle()

    assert result["emitted"] == 2
    assert scan_events == [("PLTR", 0), ("SLOW", 1)]
    assert sent == ["CARD PLTR", "CARD SLOW"]
    assert any(event == "advisory_high_priority_scan_complete" for event, _ in logs)
    timing = next(detail for event, detail in logs if event == "advisory_cycle_timing")
    assert timing["immediate_live_sent"] == 1
    assert timing["first_live_discord_elapsed_s"] is not None


def test_run_advisory_cycle_dedups_discord_within_cycle(monkeypatch):
    berlin = timezone(timedelta(hours=2))
    sent = []

    monkeypatch.setattr(advisory, "load_config", lambda: _cfg())
    monkeypatch.setattr(advisory, "_now_cet", lambda: datetime(2026, 5, 15, 15, 45, tzinfo=berlin))
    monkeypatch.setattr(advisory, "ADVISORY_UNIVERSE", {
        "US": [
            {"data_symbol": "PLTR", "broker_display_name": "Palantir", "exchange": "NASDAQ", "currency": "USD", "priority": "high"},
            {"data_symbol": "PLTR", "broker_display_name": "Palantir duplicate", "exchange": "NASDAQ", "currency": "USD", "priority": "high"},
        ]
    })
    monkeypatch.setattr(advisory, "get_recent_advisory_signals", lambda **kwargs: [])
    monkeypatch.setattr(advisory, "get_recent_trades", lambda days=90: [])
    monkeypatch.setattr(advisory, "_scan_candidate", lambda item, *args, **kwargs: {
        "market": "US",
        "mode": "live",
        "status": "skipped",
        "alert_stage": "watch",
        "data_symbol": item["data_symbol"],
        "broker_display_name": item["broker_display_name"],
        "exchange": "NASDAQ",
        "currency": "USD",
        "side": "BUY",
        "priority": item.get("priority", "medium"),
        "trade_target": True,
        "benchmark_only": False,
        "grade": "A",
        "composite_score": 0.48,
        "ev_net_pct": 0.4,
        "breakout_quality": 0.7,
        "valid_until": "2026-05-15T14:30:00+00:00",
        "time_exit_at": "2026-05-15T18:55:00+00:00",
        "signal_json": {},
    })
    monkeypatch.setattr(advisory, "_format_trade_card", lambda signal: f"CARD {signal['broker_display_name']}")
    monkeypatch.setattr(advisory, "insert_advisory_signal", lambda signal: {"id": 100})
    monkeypatch.setattr(advisory, "_send_discord", lambda text, webhook_url: sent.append(text) or True)
    monkeypatch.setattr(advisory, "log_event", lambda *args, **kwargs: None)

    result = advisory.run_advisory_cycle()

    assert result["emitted"] == 2
    assert sent == ["CARD Palantir"]


def test_benchmark_only_live_ticker_does_not_consume_alert_cap(monkeypatch):
    berlin = timezone(timedelta(hours=2))
    saved = []
    sent = []
    virtual_positions = []

    monkeypatch.setattr(advisory, "load_config", lambda: _cfg(max_live_alerts_per_day=1, max_live_trades_per_session=1))
    monkeypatch.setattr(advisory, "_now_cet", lambda: datetime(2026, 5, 15, 15, 45, tzinfo=berlin))
    monkeypatch.setattr(advisory, "ADVISORY_UNIVERSE", {
        "US": [
            {
                "data_symbol": "SPY",
                "broker_display_name": "SPDR S&P 500 ETF",
                "exchange": "NYSE Arca",
                "currency": "USD",
                "benchmark_only": True,
                "trade_target": False,
                "priority": "high",
            },
            {"data_symbol": "AMD", "broker_display_name": "AMD", "exchange": "NASDAQ", "currency": "USD"},
        ]
    })
    monkeypatch.setattr(advisory, "get_recent_advisory_signals", lambda **kwargs: [])
    monkeypatch.setattr(advisory, "get_recent_trades", lambda days=90: [])
    monkeypatch.setattr(advisory, "_data_quality", lambda symbol, market, listing_type=None: {
        "ok": True, "last_price": 100.0, "rows": 90, "age_minutes": 1.0, "avg_recent_volume": 100000,
    })
    monkeypatch.setattr(advisory, "detect_regime", lambda symbol: SimpleNamespace(
        market_regime="bull", intraday_regime="trending",
    ))
    monkeypatch.setattr(advisory, "compute_all_signals", lambda symbol, weights, regime_state=None: {
        "composite_score": 0.62,
        "signals": {
            "vwap_deviation": {"score": 0.55},
            "macd_crossover": {"score": 0.55},
            "relative_strength": {"score": 0.50},
            "orb": {"score": 0.65, "meta": {"active": True}},
            "news_sentiment": {"score": 0.10},
        },
        "atr_data": {"atr_pct": 1.0},
    })
    monkeypatch.setattr(advisory, "compute_expected_value", lambda *args, **kwargs: {
        "net_ev_pct": 0.82, "confidence": 0.74,
    })
    monkeypatch.setattr(advisory, "_market_context", lambda market: {"market": market})
    monkeypatch.setattr(advisory, "insert_advisory_signal", lambda signal: saved.append(signal) or {"id": len(saved)})
    monkeypatch.setattr(advisory, "create_virtual_position", lambda record: virtual_positions.append(record) or {"id": 1})
    monkeypatch.setattr(advisory, "_send_discord", lambda text, webhook_url: sent.append(text) or True)
    monkeypatch.setattr(advisory, "log_event", lambda *args, **kwargs: None)

    result = advisory.run_advisory_cycle()

    assert result["live_sent_today"] == 1
    assert [s["data_symbol"] for s in saved] == ["SPY", "AMD"]
    assert saved[0]["status"] == "benchmark_logged"
    assert saved[1]["status"] == "sent"
    assert len(sent) == 1
    assert "AMD" in sent[0]
    assert virtual_positions[0]["data_symbol"] == "AMD"
    assert virtual_positions[0]["session_window"] == "us_open"


def test_run_advisory_cycle_waits_for_us_open_bars(monkeypatch):
    berlin = timezone(timedelta(hours=2))
    saved = []
    logs = []

    monkeypatch.setattr(advisory, "load_config", lambda: _cfg(us_min_minutes_after_open=15))
    monkeypatch.setattr(advisory, "_now_cet", lambda: datetime(2026, 5, 15, 15, 35, tzinfo=berlin))
    monkeypatch.setattr(advisory, "ADVISORY_UNIVERSE", {
        "US": [{"data_symbol": "NVDA", "broker_display_name": "NVIDIA", "exchange": "NASDAQ", "currency": "USD"}]
    })
    monkeypatch.setattr(advisory, "get_recent_advisory_signals", lambda **kwargs: [])
    monkeypatch.setattr(advisory, "get_recent_trades", lambda days=90: [])
    monkeypatch.setattr(advisory, "insert_advisory_signal", lambda signal: saved.append(signal) or {"id": 1})
    monkeypatch.setattr(advisory, "log_event", lambda level, event, detail=None: logs.append((level, event, detail)))

    result = advisory.run_advisory_cycle()

    assert result["emitted"] == 0
    assert saved == []
    assert any(event == "advisory_live_waiting_for_us_open_bars" for _, event, _ in logs)


def test_run_advisory_cycle_skips_entries_not_supported_by_active_broker(monkeypatch):
    berlin = timezone(timedelta(hours=2))
    snapshots = []

    monkeypatch.setattr(advisory, "load_config", lambda: _cfg(broker_tag="trade_republic_de"))
    monkeypatch.setattr(advisory, "_now_cet", lambda: datetime(2026, 5, 15, 15, 45, tzinfo=berlin))
    monkeypatch.setattr(advisory, "ADVISORY_UNIVERSE", {
        "US": [{
            "data_symbol": "ONLYSCALABLE",
            "broker_display_name": "Only Scalable",
            "exchange": "NASDAQ",
            "currency": "USD",
            "broker_tags": ["scalable_de"],
        }]
    })
    monkeypatch.setattr(advisory, "get_recent_advisory_signals", lambda **kwargs: [])
    monkeypatch.setattr(advisory, "get_recent_trades", lambda days=90: [])
    monkeypatch.setattr(advisory, "compute_all_signals", lambda *args, **kwargs: pytest.fail("unsupported broker entry should not scan"))
    monkeypatch.setattr(advisory, "upsert_advisory_scan_snapshot", lambda snapshot: snapshots.append(snapshot) or {})
    monkeypatch.setattr(advisory, "log_event", lambda *args, **kwargs: None)

    result = advisory.run_advisory_cycle()

    assert result["emitted"] == 0
    assert snapshots[0]["gate_reason"] == "broker_not_supported"


def test_us_premarket_window_can_emit_watch_without_open_wait(monkeypatch):
    berlin = timezone(timedelta(hours=2))
    saved = []
    sent = []
    logs = []

    monkeypatch.setattr(advisory, "load_config", lambda: _cfg(us_min_minutes_after_open=15))
    monkeypatch.setattr(advisory, "_now_cet", lambda: datetime(2026, 5, 15, 15, 15, tzinfo=berlin))
    monkeypatch.setattr(advisory, "ADVISORY_UNIVERSE", {
        "US": [{"data_symbol": "AMZN", "broker_display_name": "Amazon", "exchange": "NASDAQ", "currency": "USD"}]
    })
    monkeypatch.setattr(advisory, "get_recent_advisory_signals", lambda **kwargs: [])
    monkeypatch.setattr(advisory, "get_recent_trades", lambda days=90: [])
    monkeypatch.setattr(advisory, "_data_quality", lambda symbol, market, listing_type=None: {
        "ok": True, "last_price": 100.0, "rows": 10, "age_minutes": 1.0,
        "avg_recent_volume": 100000, "early_session_relaxed": True,
    })
    monkeypatch.setattr(advisory, "detect_regime", lambda symbol: SimpleNamespace(
        market_regime="bull", intraday_regime="trending",
    ))
    monkeypatch.setattr(advisory, "compute_all_signals", lambda symbol, weights, regime_state=None: {
        "composite_score": 0.36,
        "signals": {
            "vwap_deviation": {"score": 0.05, "meta": {"pct_deviation": 0.10}},
            "macd_crossover": {"score": 0.55},
            "relative_strength": {"score": 0.45},
            "orb": {"score": 0.20, "meta": {"active": False}},
            "news_sentiment": {"score": 0.05},
        },
        "atr_data": {"atr_pct": 0.50},
    })
    monkeypatch.setattr(advisory, "compute_expected_value", lambda *args, **kwargs: {
        "net_ev_pct": 0.20, "confidence": 0.40,
    })
    monkeypatch.setattr(advisory, "_market_context", lambda market: {"market": market})
    monkeypatch.setattr(advisory, "insert_advisory_signal", lambda signal: saved.append(signal) or {"id": 1})
    monkeypatch.setattr(advisory, "_send_discord", lambda text, webhook_url: sent.append(text) or True)
    monkeypatch.setattr(advisory, "log_event", lambda level, event, detail=None: logs.append((level, event, detail)))

    result = advisory.run_advisory_cycle()

    assert result["emitted"] == 1
    assert saved[0]["alert_stage"] == "watch"
    assert saved[0]["status"] == "skipped"
    assert sent and "WATCH ONLY" in sent[0]
    assert not any(event == "advisory_live_waiting_for_us_open_bars" for _, event, _ in logs)


def test_run_advisory_cycle_blocks_watch_after_symbol_cap(monkeypatch):
    berlin = timezone(timedelta(hours=2))
    saved = []
    logs = []
    monkeypatch.setenv("ADVISORY_MAX_WATCH_ALERTS_PER_SYMBOL_PER_SESSION", "1")
    monkeypatch.setattr(advisory, "load_config", lambda: _cfg())
    monkeypatch.setattr(advisory, "_now_cet", lambda: datetime(2026, 5, 15, 15, 50, tzinfo=berlin))
    monkeypatch.setattr(advisory, "ADVISORY_UNIVERSE", {
        "US": [{"data_symbol": "AMZN", "broker_display_name": "Amazon", "exchange": "NASDAQ", "currency": "USD"}]
    })
    monkeypatch.setattr(advisory, "get_recent_advisory_signals", lambda **kwargs: [
        {
            "market": "US",
            "mode": "live",
            "data_symbol": "AMZN",
            "status": "skipped",
            "grade": "C",
            "composite_score": 0.10,
            "breakout_quality": 0.10,
            "signal_json": {"alert_stage": "ignition"},
            "created_at": "2026-05-15T13:40:00+00:00",
        }
    ] if kwargs.get("mode") == "live" else [])
    monkeypatch.setattr(advisory, "get_recent_trades", lambda days=90: [])
    monkeypatch.setattr(advisory, "_data_quality", lambda symbol, market, listing_type=None: {
        "ok": True, "last_price": 100.0, "rows": 12, "age_minutes": 1.0,
        "avg_recent_volume": 100000, "early_session_relaxed": True,
    })
    monkeypatch.setattr(advisory, "detect_regime", lambda symbol: SimpleNamespace(
        market_regime="bull", intraday_regime="trending",
    ))
    monkeypatch.setattr(advisory, "compute_all_signals", lambda symbol, weights, regime_state=None: {
        "composite_score": 0.36,
        "signals": {
            "vwap_deviation": {"score": 0.05, "meta": {"pct_deviation": 0.10}},
            "macd_crossover": {"score": 0.55},
            "relative_strength": {"score": 0.45},
            "orb": {"score": 0.20, "meta": {"active": False}},
            "news_sentiment": {"score": 0.05},
        },
        "atr_data": {"atr_pct": 0.50},
    })
    monkeypatch.setattr(advisory, "compute_expected_value", lambda *args, **kwargs: {
        "net_ev_pct": 0.20, "confidence": 0.40,
    })
    monkeypatch.setattr(advisory, "_market_context", lambda market: {"market": market})
    monkeypatch.setattr(advisory, "insert_advisory_signal", lambda signal: saved.append(signal) or {"id": 1})
    monkeypatch.setattr(advisory, "_send_discord", lambda *args, **kwargs: True)
    monkeypatch.setattr(advisory, "log_event", lambda level, event, detail=None: logs.append((level, event, detail)))

    result = advisory.run_advisory_cycle()

    assert result["emitted"] == 0
    assert saved == []
    assert any(event == "advisory_watch_blocked_symbol_cap" for _, event, _ in logs)


def test_run_advisory_cycle_suppresses_duplicate_ticker_but_expiry_frees_open_cap(monkeypatch):
    berlin = timezone(timedelta(hours=2))
    saved = []
    sent = []
    recent_live = [
        {
            "market": "US",
            "data_symbol": "NVDA",
            "status": "sent",
            "created_at": "2026-05-15T13:35:00+00:00",
            "valid_until": "2026-05-15T13:50:00+00:00",
        }
    ]

    monkeypatch.setattr(advisory, "load_config", lambda: _cfg())
    monkeypatch.setattr(advisory, "_now_cet", lambda: datetime(2026, 5, 15, 16, 5, tzinfo=berlin))
    monkeypatch.setattr(advisory, "ADVISORY_UNIVERSE", {
        "US": [
            {"data_symbol": "NVDA", "broker_display_name": "NVIDIA", "exchange": "NASDAQ", "currency": "USD"},
            {"data_symbol": "AMD", "broker_display_name": "AMD", "exchange": "NASDAQ", "currency": "USD"},
        ]
    })
    monkeypatch.setattr(advisory, "get_recent_advisory_signals", lambda **kwargs: recent_live)
    monkeypatch.setattr(advisory, "get_recent_trades", lambda days=90: [])
    monkeypatch.setattr(advisory, "_data_quality", lambda symbol, market, listing_type=None: {
        "ok": True, "last_price": 100.0, "rows": 90, "age_minutes": 1.0, "avg_recent_volume": 100000,
    })
    monkeypatch.setattr(advisory, "detect_regime", lambda symbol: SimpleNamespace(
        market_regime="bull", intraday_regime="trending",
    ))

    def fake_signals(symbol, weights, regime_state=None):
        return {
            "composite_score": 0.62,
            "signals": {
                "vwap_deviation": {"score": 0.55},
                "macd_crossover": {"score": 0.55},
                "relative_strength": {"score": 0.50},
                "orb": {"score": 0.65, "meta": {"active": True}},
                "news_sentiment": {"score": 0.10},
            },
            "atr_data": {"atr_pct": 1.0},
        }

    monkeypatch.setattr(advisory, "compute_all_signals", fake_signals)
    monkeypatch.setattr(advisory, "compute_expected_value", lambda *args, **kwargs: {
        "net_ev_pct": 0.82, "confidence": 0.74,
    })
    monkeypatch.setattr(advisory, "_market_context", lambda market: {"market": market})
    monkeypatch.setattr(advisory, "insert_advisory_signal", lambda signal: saved.append(signal) or {"id": 1})
    monkeypatch.setattr(advisory, "_send_discord", lambda text, webhook_url: sent.append(text) or True)
    monkeypatch.setattr(advisory, "log_event", lambda *args, **kwargs: None)

    result = advisory.run_advisory_cycle()

    assert result["emitted"] == 1
    assert saved[0]["data_symbol"] == "AMD"
    assert sent and "AMD" in sent[0]


def test_run_advisory_cycle_keeps_eu_shadow_separate_from_live_alerts(monkeypatch):
    berlin = timezone(timedelta(hours=2))
    saved = []
    sent = []

    monkeypatch.setattr(advisory, "load_config", lambda: _cfg(markets={"EU"}, live_markets={"US"}, shadow_markets={"EU"}))
    monkeypatch.setattr(advisory, "_now_cet", lambda: datetime(2026, 5, 15, 9, 45, tzinfo=berlin))
    monkeypatch.setattr(advisory, "ADVISORY_UNIVERSE", {
        "EU": [{"data_symbol": "SAP.DE", "broker_display_name": "SAP", "exchange": "Xetra", "currency": "EUR"}]
    })
    monkeypatch.setattr(advisory, "get_recent_advisory_signals", lambda **kwargs: [])
    monkeypatch.setattr(advisory, "get_recent_trades", lambda days=90: [])
    monkeypatch.setattr(advisory, "_data_quality", lambda symbol, market, listing_type=None: {
        "ok": True, "last_price": 180.0, "rows": 90, "age_minutes": 1.0, "avg_recent_volume": 100000,
    })
    monkeypatch.setattr(advisory, "detect_regime", lambda symbol: SimpleNamespace(
        market_regime="bull", intraday_regime="trending",
    ))
    monkeypatch.setattr(advisory, "compute_all_signals", lambda symbol, weights, regime_state=None: {
        "composite_score": 0.48,
        "signals": {
            "vwap_deviation": {"score": 0.45},
            "macd_crossover": {"score": 0.40},
            "relative_strength": {"score": 0.35},
            "orb": {"score": 0.10, "meta": {"active": False}},
            "news_sentiment": {"score": 0.05},
        },
        "atr_data": {"atr_pct": 0.9},
    })
    monkeypatch.setattr(advisory, "compute_expected_value", lambda *args, **kwargs: {
        "net_ev_pct": 0.12, "confidence": 0.50,
    })
    monkeypatch.setattr(advisory, "_market_context", lambda market: {"market": market})
    monkeypatch.setattr(advisory, "insert_advisory_signal", lambda signal: saved.append(signal) or {"id": 1})
    monkeypatch.setattr(advisory, "_send_discord", lambda text, webhook_url: sent.append(text) or True)
    monkeypatch.setattr(advisory, "log_event", lambda *args, **kwargs: None)

    result = advisory.run_advisory_cycle()

    assert result["emitted"] == 1
    assert saved[0]["mode"] == "shadow"
    assert saved[0]["status"] == "shadow_logged"
    assert len(sent) == 1
    assert "SHADOW OBSERVATION" in sent[0]
    assert "do not trade from shadow mode" in sent[0]


def test_eu_early_gate_ignores_stale_prior_shadow(monkeypatch):
    berlin = timezone(timedelta(hours=2))
    calls = []
    saved = []

    monkeypatch.setenv("ADVISORY_EU_EARLY_GATE_COMPOSITE", "0.15")
    monkeypatch.setattr(advisory, "load_config", lambda: _cfg(markets={"EU"}, live_markets={"US"}, shadow_markets={"EU"}))
    monkeypatch.setattr(advisory, "_now_cet", lambda: datetime(2026, 5, 15, 9, 45, tzinfo=berlin))
    monkeypatch.setattr(advisory, "ADVISORY_UNIVERSE", {
        "EU": [{"data_symbol": "SAP.DE", "broker_display_name": "SAP", "exchange": "Xetra", "currency": "EUR"}]
    })
    monkeypatch.setattr(advisory, "get_recent_advisory_signals", lambda **kwargs: [
        {
            "data_symbol": "SAP.DE",
            "mode": "shadow",
            "composite_score": 0.02,
            "created_at": "2026-05-14T07:45:00+00:00",
        }
    ] if kwargs.get("mode") == "shadow" else [])
    monkeypatch.setattr(advisory, "get_recent_trades", lambda days=90: [])
    monkeypatch.setattr(advisory, "_data_quality", lambda symbol, market, listing_type=None: {
        "ok": True, "last_price": 180.0, "rows": 90, "age_minutes": 1.0, "avg_recent_volume": 100000,
    })
    monkeypatch.setattr(advisory, "detect_regime", lambda symbol: SimpleNamespace(
        market_regime="bull", intraday_regime="trending",
    ))

    def fake_signals(symbol, weights, regime_state=None):
        calls.append(symbol)
        return {
            "composite_score": 0.48,
            "signals": {
                "vwap_deviation": {"score": 0.45},
                "macd_crossover": {"score": 0.40},
                "relative_strength": {"score": 0.35},
                "orb": {"score": 0.10, "meta": {"active": False}},
                "news_sentiment": {"score": 0.05},
            },
            "atr_data": {"atr_pct": 0.9},
        }

    monkeypatch.setattr(advisory, "compute_all_signals", fake_signals)
    monkeypatch.setattr(advisory, "compute_expected_value", lambda *args, **kwargs: {
        "net_ev_pct": 0.12, "confidence": 0.50,
    })
    monkeypatch.setattr(advisory, "_market_context", lambda market: {"market": market})
    monkeypatch.setattr(advisory, "insert_advisory_signal", lambda signal: saved.append(signal) or {"id": 1})
    monkeypatch.setattr(advisory, "_send_discord", lambda *args, **kwargs: True)
    monkeypatch.setattr(advisory, "log_event", lambda *args, **kwargs: None)

    result = advisory.run_advisory_cycle()

    assert calls == ["SAP.DE"]
    assert result["emitted"] == 1
    assert saved[0]["data_symbol"] == "SAP.DE"


def test_eu_early_gate_skips_recent_flat_shadow(monkeypatch):
    berlin = timezone(timedelta(hours=2))
    logs = []

    monkeypatch.setenv("ADVISORY_EU_EARLY_GATE_COMPOSITE", "0.15")
    monkeypatch.setattr(advisory, "load_config", lambda: _cfg(markets={"EU"}, live_markets={"US"}, shadow_markets={"EU"}))
    monkeypatch.setattr(advisory, "_now_cet", lambda: datetime(2026, 5, 15, 9, 45, tzinfo=berlin))
    monkeypatch.setattr(advisory, "ADVISORY_UNIVERSE", {
        "EU": [{"data_symbol": "SAP.DE", "broker_display_name": "SAP", "exchange": "Xetra", "currency": "EUR"}]
    })
    monkeypatch.setattr(advisory, "get_recent_advisory_signals", lambda **kwargs: [
        {
            "data_symbol": "SAP.DE",
            "mode": "shadow",
            "composite_score": 0.02,
            "created_at": "2026-05-15T07:40:00+00:00",
        }
    ] if kwargs.get("mode") == "shadow" else [])
    monkeypatch.setattr(advisory, "get_recent_trades", lambda days=90: [])
    monkeypatch.setattr(advisory, "compute_all_signals", lambda *args, **kwargs: pytest.fail("recent flat EU shadow should be skipped"))
    monkeypatch.setattr(advisory, "insert_advisory_signal", lambda signal: pytest.fail("skip should not persist"))
    monkeypatch.setattr(advisory, "log_event", lambda level, event, detail=None: logs.append((event, detail or {})))

    result = advisory.run_advisory_cycle()

    assert result["emitted"] == 0
    assert any(event == "advisory_eu_early_gate_skip" for event, _ in logs)


def test_eu_mirror_is_skipped_outside_mirror_window(monkeypatch):
    berlin = timezone(timedelta(hours=2))

    monkeypatch.setattr(advisory, "_data_quality", lambda *args, **kwargs: pytest.fail("mirror should skip before data fetch"))

    result = advisory._scan_candidate(
        {
            "data_symbol": "NVD.DE",
            "broker_display_name": "NVIDIA (Xetra)",
            "exchange": "Xetra",
            "currency": "EUR",
            "origin_market": "US",
            "listing_type": "eu_us_mirror",
            "primary_symbol": "NVDA",
            "mirror_only_windows": ["eu_open"],
        },
        "EU",
        "shadow",
        _cfg(),
        [],
        datetime(2026, 5, 15, 14, 30, tzinfo=berlin),
    )

    assert result is None


def test_live_scan_downgrades_late_chase_trade_to_watch(monkeypatch):
    berlin = timezone(timedelta(hours=2))

    monkeypatch.setattr(advisory, "_data_quality", lambda symbol, market, listing_type=None: {
        "ok": True, "last_price": 100.0, "rows": 30, "age_minutes": 1.0, "avg_recent_volume": 100000,
    })
    monkeypatch.setattr(advisory, "detect_regime", lambda symbol: SimpleNamespace(
        market_regime="bull", intraday_regime="trending",
    ))
    monkeypatch.setattr(advisory, "compute_all_signals", lambda symbol, weights, regime_state=None: {
        "composite_score": 0.48,
        "signals": {
            "vwap_deviation": {"score": -0.80, "meta": {"pct_deviation": 1.44}},
            "macd_crossover": {"score": 0.80},
            "relative_strength": {"score": 0.70},
            "orb": {"score": 0.70, "meta": {"active": True}},
            "news_sentiment": {"score": 0.05},
        },
        "atr_data": {"atr_pct": 0.40},
    })
    monkeypatch.setattr(advisory, "compute_expected_value", lambda *args, **kwargs: {
        "net_ev_pct": 0.82, "confidence": 0.74,
    })
    monkeypatch.setattr(advisory, "_market_context", lambda market: {"market": market})

    candidate = advisory._scan_candidate(
        {"data_symbol": "AMZN", "broker_display_name": "Amazon", "exchange": "NASDAQ", "currency": "USD"},
        "US",
        "live",
        _cfg(),
        [],
        datetime(2026, 5, 15, 16, 16, tzinfo=berlin),
    )

    assert candidate is not None
    assert candidate["alert_stage"] == "watch"
    assert candidate["status"] == "skipped"
    assert candidate["late_chase_json"]["reason"] == "late_chase"
    assert advisory._parse_dt(candidate["valid_until"]) == datetime(2026, 5, 15, 15, 1, tzinfo=timezone.utc)
    assert "WATCH ONLY" in candidate["message_text"]
    assert "LIVE TRADE ALERT" not in candidate["message_text"]


def test_live_scan_emits_early_watch_below_trade_threshold(monkeypatch):
    berlin = timezone(timedelta(hours=2))

    monkeypatch.setattr(advisory, "_data_quality", lambda symbol, market, listing_type=None: {
        "ok": True, "last_price": 100.0, "rows": 12, "age_minutes": 1.0,
        "avg_recent_volume": 100000, "early_session_relaxed": True,
    })
    monkeypatch.setattr(advisory, "detect_regime", lambda symbol: SimpleNamespace(
        market_regime="bull", intraday_regime="trending",
    ))
    monkeypatch.setattr(advisory, "compute_all_signals", lambda symbol, weights, regime_state=None: {
        "composite_score": 0.36,
        "signals": {
            "vwap_deviation": {"score": 0.05, "meta": {"pct_deviation": 0.10}},
            "macd_crossover": {"score": 0.55},
            "relative_strength": {"score": 0.45},
            "orb": {"score": 0.20, "meta": {"active": False}},
            "news_sentiment": {"score": 0.05},
        },
        "atr_data": {"atr_pct": 0.50},
    })
    monkeypatch.setattr(advisory, "compute_expected_value", lambda *args, **kwargs: {
        "net_ev_pct": 0.20, "confidence": 0.40,
    })
    monkeypatch.setattr(advisory, "_market_context", lambda market: {"market": market})
    monkeypatch.setattr(advisory, "_trend_1h_alignment", lambda symbol, side, composite: {
        "status": "ok",
        "aligned": False,
        "direction": "bearish",
        "score": -0.42,
        "side": side,
    })

    candidate = advisory._scan_candidate(
        {"data_symbol": "AMZN", "broker_display_name": "Amazon", "exchange": "NASDAQ", "currency": "USD"},
        "US",
        "live",
        _cfg(),
        [],
        datetime(2026, 5, 15, 15, 50, tzinfo=berlin),
    )

    assert candidate is not None
    assert candidate["alert_stage"] == "watch"
    assert candidate["status"] == "skipped"
    assert candidate["composite_score"] == pytest.approx(0.36)
    assert candidate["trend_1h_json"]["aligned"] is False
    assert candidate["signal_json"]["trend_1h"]["direction"] == "bearish"
    assert candidate["signal_json"]["display"]["entry_min_eur"] == pytest.approx(92.5185, rel=1e-4)
    assert candidate["signal_json"]["display"]["native_currency"] == "USD"
    assert "Early signal only" in candidate["message_text"]


def test_live_scan_suppresses_c_grade_watch_noise(monkeypatch):
    berlin = timezone(timedelta(hours=2))

    monkeypatch.setattr(advisory, "_data_quality", lambda symbol, market, listing_type=None: {
        "ok": True, "last_price": 100.0, "rows": 12, "age_minutes": 1.0,
        "avg_recent_volume": 100000, "early_session_relaxed": True,
    })
    monkeypatch.setattr(advisory, "detect_regime", lambda symbol: SimpleNamespace(
        market_regime="bull", intraday_regime="trending",
    ))
    monkeypatch.setattr(advisory, "compute_all_signals", lambda symbol, weights, regime_state=None: {
        "composite_score": 0.28,
        "signals": {
            "vwap_deviation": {"score": 0.05, "meta": {"pct_deviation": 0.10}},
            "macd_crossover": {"score": 0.25},
            "relative_strength": {"score": 0.20},
            "orb": {"score": 0.20, "meta": {"active": False}},
            "news_sentiment": {"score": 0.05},
        },
        "atr_data": {"atr_pct": 0.50},
    })

    candidate = advisory._scan_candidate(
        {"data_symbol": "AMZN", "broker_display_name": "Amazon", "exchange": "NASDAQ", "currency": "USD"},
        "US",
        "live",
        _cfg(),
        [],
        datetime(2026, 5, 15, 15, 50, tzinfo=berlin),
    )

    assert candidate is None


def test_live_scan_allows_c_grade_late_chase_watch(monkeypatch):
    berlin = timezone(timedelta(hours=2))

    monkeypatch.setattr(advisory, "_data_quality", lambda symbol, market, listing_type=None: {
        "ok": True, "last_price": 100.0, "rows": 12, "age_minutes": 1.0,
        "avg_recent_volume": 100000, "early_session_relaxed": True,
    })
    monkeypatch.setattr(advisory, "detect_regime", lambda symbol: SimpleNamespace(
        market_regime="bull", intraday_regime="trending",
    ))
    monkeypatch.setattr(advisory, "compute_all_signals", lambda symbol, weights, regime_state=None: {
        "composite_score": 0.28,
        "signals": {
            "vwap_deviation": {"score": 0.05, "meta": {"pct_deviation": 0.90}},
            "macd_crossover": {"score": 0.25},
            "relative_strength": {"score": 0.20},
            "orb": {"score": 0.20, "meta": {"active": False}},
            "news_sentiment": {"score": 0.05},
        },
        "atr_data": {"atr_pct": 0.40},
    })
    monkeypatch.setattr(advisory, "compute_expected_value", lambda *args, **kwargs: {
        "net_ev_pct": 0.05, "confidence": 0.30,
    })
    monkeypatch.setattr(advisory, "_market_context", lambda market: {"market": market})

    candidate = advisory._scan_candidate(
        {"data_symbol": "AMZN", "broker_display_name": "Amazon", "exchange": "NASDAQ", "currency": "USD"},
        "US",
        "live",
        _cfg(),
        [],
        datetime(2026, 5, 15, 15, 50, tzinfo=berlin),
    )

    assert candidate is not None
    assert candidate["grade"] == "C"
    assert candidate["alert_stage"] == "watch"
    assert candidate["late_chase_json"]["reason"] == "late_chase"
    assert "WATCH ONLY" in candidate["message_text"]


def test_live_scan_emits_downside_risk_without_short_permission(monkeypatch):
    berlin = timezone(timedelta(hours=2))

    monkeypatch.setattr(advisory, "_data_quality", lambda symbol, market, listing_type=None: {
        "ok": True, "last_price": 100.0, "rows": 60, "age_minutes": 1.0,
        "avg_recent_volume": 100000,
    })
    monkeypatch.setattr(advisory, "detect_regime", lambda symbol: SimpleNamespace(
        market_regime="bear", intraday_regime="trending",
    ))
    monkeypatch.setattr(advisory, "compute_all_signals", lambda symbol, weights, regime_state=None: {
        "composite_score": -0.38,
        "signals": {
            "vwap_deviation": {"score": -0.55},
            "macd_crossover": {"score": -0.45},
            "relative_strength": {"score": -0.35},
            "orb": {"score": -0.30, "meta": {"active": False}},
            "news_sentiment": {"score": -0.10},
        },
        "atr_data": {"atr_pct": 0.70},
    })
    monkeypatch.setattr(advisory, "compute_expected_value", lambda *args, **kwargs: {
        "net_ev_pct": -0.10, "confidence": 0.30,
    })
    monkeypatch.setattr(advisory, "_market_context", lambda market: {"market": market})

    candidate = advisory._scan_candidate(
        {"data_symbol": "AMD", "broker_display_name": "AMD", "exchange": "NASDAQ", "currency": "USD"},
        "US",
        "live",
        _cfg(allow_short=False),
        [],
        datetime(2026, 5, 15, 17, 30, tzinfo=berlin),
    )

    assert candidate is not None
    assert candidate["side"] == "SELL"
    assert candidate["alert_stage"] == "downside"
    assert candidate["status"] == "skipped"
    assert "DOWNSIDE RISK" in candidate["message_text"]


def test_live_scan_emits_momentum_ignition_below_watch_threshold(monkeypatch):
    berlin = timezone(timedelta(hours=2))

    monkeypatch.setattr(advisory, "_data_quality", lambda symbol, market, listing_type=None: {
        "ok": True, "last_price": 100.70, "rows": 12, "age_minutes": 1.0,
        "avg_recent_volume": 100000, "early_session_relaxed": True,
    })
    monkeypatch.setattr(advisory, "_get_bars", lambda *args, **kwargs: _make_ignition_bars())
    monkeypatch.setattr(advisory, "detect_regime", lambda symbol: SimpleNamespace(
        market_regime="bull", intraday_regime="trending",
    ))
    monkeypatch.setattr(advisory, "compute_all_signals", lambda symbol, weights, regime_state=None: {
        "composite_score": 0.12,
        "signals": {
            "vwap_deviation": {"score": 0.05, "meta": {"pct_deviation": 0.20}},
            "macd_crossover": {"score": 0.16},
            "relative_strength": {"score": 0.14},
            "orb": {"score": 0.10, "meta": {"active": False}},
            "news_sentiment": {"score": 0.02},
        },
        "atr_data": {"atr_pct": 0.50},
    })
    monkeypatch.setattr(advisory, "compute_expected_value", lambda *args, **kwargs: {
        "net_ev_pct": 0.03, "confidence": 0.25,
    })
    monkeypatch.setattr(advisory, "_market_context", lambda market: {"market": market})

    candidate = advisory._scan_candidate(
        {"data_symbol": "AMZN", "broker_display_name": "Amazon", "exchange": "NASDAQ", "currency": "USD"},
        "US",
        "live",
        _cfg(),
        [],
        datetime(2026, 5, 15, 15, 38, tzinfo=berlin),
    )

    assert candidate is not None
    assert candidate["grade"] == "C"
    assert candidate["alert_stage"] == "ignition"
    assert candidate["status"] == "skipped"
    assert candidate["ignition_json"]["reason"] == "momentum_ignition"
    assert candidate["signal_json"]["ignition"]["volume_ratio"] >= 2.0
    assert "MOMENTUM IGNITION" in candidate["message_text"]
    assert "LIVE TRADE ALERT" not in candidate["message_text"]


def test_live_scan_does_not_ignite_against_price_direction(monkeypatch):
    berlin = timezone(timedelta(hours=2))

    monkeypatch.setattr(advisory, "_data_quality", lambda symbol, market, listing_type=None: {
        "ok": True, "last_price": 100.0, "rows": 12, "age_minutes": 1.0,
        "avg_recent_volume": 100000, "early_session_relaxed": True,
    })
    monkeypatch.setattr(advisory, "_get_bars", lambda *args, **kwargs: _make_ignition_bars(direction="down"))
    monkeypatch.setattr(advisory, "detect_regime", lambda symbol: SimpleNamespace(
        market_regime="bull", intraday_regime="trending",
    ))
    monkeypatch.setattr(advisory, "compute_all_signals", lambda symbol, weights, regime_state=None: {
        "composite_score": 0.12,
        "signals": {
            "vwap_deviation": {"score": 0.05, "meta": {"pct_deviation": 0.20}},
            "macd_crossover": {"score": 0.16},
            "relative_strength": {"score": 0.14},
            "orb": {"score": 0.10, "meta": {"active": False}},
            "news_sentiment": {"score": 0.02},
        },
        "atr_data": {"atr_pct": 0.50},
    })

    candidate = advisory._scan_candidate(
        {"data_symbol": "AMZN", "broker_display_name": "Amazon", "exchange": "NASDAQ", "currency": "USD"},
        "US",
        "live",
        _cfg(),
        [],
        datetime(2026, 5, 15, 15, 38, tzinfo=berlin),
    )

    assert candidate is None


def test_eu_mirror_scan_uses_primary_symbol_news_and_metadata(monkeypatch):
    berlin = timezone(timedelta(hours=2))
    news_calls = []

    monkeypatch.setattr(advisory, "_data_quality", lambda symbol, market, listing_type=None: {
        "ok": True, "last_price": 180.0, "rows": 25, "age_minutes": 1.0, "avg_recent_volume": 1000,
    })
    monkeypatch.setattr(advisory, "detect_regime", lambda symbol: SimpleNamespace(
        market_regime="bull", intraday_regime="trending",
    ))
    monkeypatch.setattr(advisory, "compute_all_signals", lambda symbol, weights, regime_state=None: {
        "composite_score": 0.0,
        "signals": {
            "vwap_deviation": {"score": 0.20},
            "macd_crossover": {"score": 0.80},
            "relative_strength": {"score": 0.80},
            "tape_aggression": {"score": 0.40},
            "rsi_divergence": {"score": 0.0},
            "bollinger_squeeze": {"score": 0.0},
            "news_sentiment": {"score": 0.0},
            "orb": {"score": 0.60, "meta": {"active": True}},
        },
        "atr_data": {"atr_pct": 0.9},
    })

    def fake_news_score(symbol):
        news_calls.append(symbol)
        return 0.80, {}

    monkeypatch.setattr(advisory, "news_sentiment_score", fake_news_score)
    monkeypatch.setattr(advisory, "compute_expected_value", lambda *args, **kwargs: {
        "net_ev_pct": 0.12, "confidence": 0.50,
    })
    monkeypatch.setattr(advisory, "_market_context", lambda market: {"market": market})

    candidate = advisory._scan_candidate(
        {
            "data_symbol": "NVD.DE",
            "broker_display_name": "NVIDIA (Xetra)",
            "exchange": "Xetra",
            "currency": "EUR",
            "origin_market": "US",
            "listing_type": "eu_us_mirror",
            "primary_symbol": "NVDA",
            "mirror_only_windows": ["eu_open"],
        },
        "EU",
        "shadow",
        _cfg(),
        [],
        datetime(2026, 5, 15, 9, 45, tzinfo=berlin),
    )

    assert candidate is not None
    assert "NVDA" in news_calls
    assert candidate["listing_type"] == "eu_us_mirror"
    assert candidate["primary_symbol"] == "NVDA"
    assert candidate["origin_market"] == "US"
    assert candidate["signal_json"]["listing_type"] == "eu_us_mirror"
    assert candidate["signal_json"]["primary_symbol"] == "NVDA"


def test_shadow_discord_can_be_disabled_by_market(monkeypatch):
    berlin = timezone(timedelta(hours=2))
    saved = []
    sent = []

    monkeypatch.setattr(advisory, "load_config", lambda: _cfg(
        markets={"EU"},
        live_markets={"US"},
        shadow_markets={"EU"},
        shadow_discord_markets=set(),
    ))
    monkeypatch.setattr(advisory, "_now_cet", lambda: datetime(2026, 5, 15, 9, 45, tzinfo=berlin))
    monkeypatch.setattr(advisory, "ADVISORY_UNIVERSE", {
        "EU": [{"data_symbol": "SAP.DE", "broker_display_name": "SAP", "exchange": "Xetra", "currency": "EUR"}]
    })
    monkeypatch.setattr(advisory, "get_recent_advisory_signals", lambda **kwargs: [])
    monkeypatch.setattr(advisory, "get_recent_trades", lambda days=90: [])
    monkeypatch.setattr(advisory, "_data_quality", lambda symbol, market, listing_type=None: {
        "ok": True, "last_price": 180.0, "rows": 90, "age_minutes": 1.0, "avg_recent_volume": 100000,
    })
    monkeypatch.setattr(advisory, "detect_regime", lambda symbol: SimpleNamespace(
        market_regime="bull", intraday_regime="trending",
    ))
    monkeypatch.setattr(advisory, "compute_all_signals", lambda symbol, weights, regime_state=None: {
        "composite_score": 0.48,
        "signals": {
            "vwap_deviation": {"score": 0.45},
            "macd_crossover": {"score": 0.40},
            "relative_strength": {"score": 0.35},
            "orb": {"score": 0.10, "meta": {"active": False}},
            "news_sentiment": {"score": 0.05},
        },
        "atr_data": {"atr_pct": 0.9},
    })
    monkeypatch.setattr(advisory, "compute_expected_value", lambda *args, **kwargs: {
        "net_ev_pct": 0.12, "confidence": 0.50,
    })
    monkeypatch.setattr(advisory, "_market_context", lambda market: {"market": market})
    monkeypatch.setattr(advisory, "insert_advisory_signal", lambda signal: saved.append(signal) or {"id": 1})
    monkeypatch.setattr(advisory, "_send_discord", lambda text, webhook_url: sent.append(text) or True)
    monkeypatch.setattr(advisory, "log_event", lambda *args, **kwargs: None)

    result = advisory.run_advisory_cycle()

    assert result["emitted"] == 1
    assert saved[0]["mode"] == "shadow"
    assert sent == []


def test_eu_catalyst_score_uses_clean_symbol_and_broker_name(monkeypatch):
    calls = []

    def fake_news_score(alias):
        calls.append(alias)
        return (0.42 if alias == "ASML" else 0.0), {}

    monkeypatch.setattr(advisory, "news_sentiment_score", fake_news_score)

    score = advisory._eu_catalyst_score(
        {"data_symbol": "ASML.AS", "broker_display_name": "ASML"},
        {"news_sentiment": {"score": 0.05}},
    )

    assert score == pytest.approx(0.42)
    assert "ASML" in calls


def test_intraday_grade_cap_downgrades_opposing_orb_and_vwap():
    signals = {
        "orb": {"score": -0.81},
        "vwap_deviation": {"score": -0.73},
    }

    grade, detail = advisory._intraday_grade_cap("A", "BUY", signals, "us_open")

    assert grade == "B"
    assert detail["reason"] == "orb_vwap_intraday_grade_cap"
    assert detail["original_grade"] == "A"


def test_premium_setup_flag_tracks_double_max_alignment():
    signals = {
        "macd_crossover": {"score": 0.95},
        "relative_strength": {"score": 0.91},
    }

    result = advisory._premium_setup_flag("BUY", signals)

    assert result["premium_setup"] is True
    assert result["macd_score"] == pytest.approx(0.95)


def test_exit_monitor_sends_t1_recommendation_without_closing(monkeypatch):
    sent = []
    updates = []
    position = {
        "id": 42,
        "data_symbol": "AMD",
        "side": "BUY",
        "grade": "A",
        "currency": "USD",
        "fx_rate": 1.08,
        "manual_entry_price": 100.0,
        "stop_price": 98.0,
        "target_1": 104.0,
        "target_2": 108.0,
        "suggested_size_eur": 750.0,
        "exit_monitor_json": {"size_eur": 750.0, "alerts": []},
    }

    monkeypatch.setattr(advisory, "get_open_advisory_positions", lambda max_age_days=7: [position])
    monkeypatch.setattr(advisory, "_latest_native_price", lambda symbol: 104.5)
    monkeypatch.setattr(advisory, "_send_discord", lambda text, webhook_url: sent.append(text) or True)
    monkeypatch.setattr(advisory, "update_advisory_exit_status", lambda signal_id, update: updates.append((signal_id, update)) or update)

    emitted = advisory._monitor_open_positions(
        _cfg(discord_webhook_url="https://discord.test"),
        datetime(2026, 5, 15, 16, 0, tzinfo=timezone.utc),
    )

    assert emitted == [{"symbol": "AMD", "alert_type": "t1"}]
    assert "T1 HIT" in sent[0]
    assert updates[0][0] == 42
    assert updates[0][1]["t1_alerted"] is True
    assert "status" not in updates[0][1]


def test_exit_monitor_marks_t1_when_t2_fires(monkeypatch):
    sent = []
    updates = []
    position = {
        "id": 42,
        "data_symbol": "AMD",
        "side": "BUY",
        "grade": "A",
        "currency": "USD",
        "fx_rate": 1.08,
        "manual_entry_price": 100.0,
        "stop_price": 98.0,
        "target_1": 104.0,
        "target_2": 108.0,
        "suggested_size_eur": 750.0,
        "exit_monitor_json": {"size_eur": 750.0, "alerts": []},
    }

    monkeypatch.setattr(advisory, "get_open_advisory_positions", lambda max_age_days=7: [position])
    monkeypatch.setattr(advisory, "_latest_native_price", lambda symbol: 109.0)
    monkeypatch.setattr(advisory, "_send_discord", lambda text, webhook_url: sent.append(text) or True)
    monkeypatch.setattr(advisory, "update_advisory_exit_status", lambda signal_id, update: updates.append(update) or update)

    emitted = advisory._monitor_open_positions(
        _cfg(discord_webhook_url="https://discord.test"),
        datetime(2026, 5, 15, 16, 0, tzinfo=timezone.utc),
    )

    assert emitted == [{"symbol": "AMD", "alert_type": "t2"}]
    assert "T2 HIT" in sent[0]
    assert updates[0]["exit_monitor_json"]["alerts"] == ["t1", "t2"]


def test_exit_monitor_throttles_checked_heartbeat(monkeypatch):
    updates = []
    position = {
        "id": 42,
        "data_symbol": "AMD",
        "side": "BUY",
        "grade": "A",
        "currency": "USD",
        "fx_rate": 1.08,
        "manual_entry_price": 100.0,
        "stop_price": 90.0,
        "target_1": 110.0,
        "target_2": 120.0,
        "suggested_size_eur": 750.0,
        "exit_monitor_json": {
            "size_eur": 750.0,
            "alerts": ["checked"],
            "last_checked_at": "2026-05-15T15:55:00+00:00",
        },
    }

    monkeypatch.setattr(advisory, "get_open_advisory_positions", lambda max_age_days=7: [position])
    monkeypatch.setattr(advisory, "_latest_native_price", lambda symbol: 101.0)
    monkeypatch.setattr(advisory, "update_advisory_exit_status", lambda signal_id, update: updates.append(update) or update)

    emitted = advisory._monitor_open_positions(
        _cfg(discord_webhook_url="https://discord.test"),
        datetime(2026, 5, 15, 16, 0, tzinfo=timezone.utc),
    )

    assert emitted == []
    assert updates == []


def test_virtual_monitor_sends_runner_weakening_before_levels(monkeypatch):
    sent = []
    updates = []
    position = {
        "id": 77,
        "data_symbol": "NVDA",
        "side": "BUY",
        "grade": "A",
        "market": "US",
        "currency": "USD",
        "fx_rate": 1.10,
        "entry_price_native": 100.0,
        "stop_price": 95.0,
        "target_1": 110.0,
        "target_2": 120.0,
        "exit_monitor_json": {"size_eur": 1000.0, "alerts": []},
    }

    monkeypatch.setattr(advisory, "get_open_virtual_positions", lambda max_age_days=3: [position])
    monkeypatch.setattr(advisory, "_latest_native_price", lambda symbol: 104.0)
    monkeypatch.setattr(advisory, "_send_discord", lambda text, webhook_url: sent.append(text) or True)
    monkeypatch.setattr(advisory, "update_virtual_position", lambda position_id, update: updates.append((position_id, update)) or update)
    monkeypatch.setattr(advisory, "detect_regime", lambda symbol: SimpleNamespace(market_regime="bull", intraday_regime="trending"))
    monkeypatch.setattr(advisory, "_trend_1h_alignment", lambda symbol, side, composite: {"aligned": False, "direction": "down"})
    monkeypatch.setattr(
        advisory,
        "compute_all_signals",
        lambda symbol, weights, regime_state=None: {
            "composite_score": 0.12,
            "signals": {
                "vwap_deviation": {"score": -0.25},
                "tape_aggression": {"score": -0.20},
                "macd_crossover": {"score": -0.30},
                "relative_strength": {"score": 0.10},
                "orb": {"score": 0.0, "meta": {"active": False}},
            },
        },
    )

    emitted = advisory._monitor_virtual_positions(
        _cfg(discord_webhook_url="https://discord.test"),
        datetime(2026, 6, 2, 16, 0, tzinfo=timezone.utc),
    )

    assert emitted == [{"symbol": "NVDA", "alert_type": "runner_weakening", "virtual": True}]
    assert "RUNNER WEAKENING" in sent[0]
    assert "consider trimming" in sent[0]
    assert updates[0][0] == 77
    assert "status" not in updates[0][1]
    assert "closed_at" not in updates[0][1]
    assert "runner_weakening" in updates[0][1]["exit_monitor_json"]["alerts"]
    assert updates[0][1]["exit_monitor_json"]["runner_weakening_json"]["status"] == "weakening"


def test_virtual_monitor_does_not_repeat_runner_weakening(monkeypatch):
    sent = []
    updates = []
    position = {
        "id": 77,
        "data_symbol": "NVDA",
        "side": "BUY",
        "grade": "A",
        "market": "US",
        "currency": "USD",
        "fx_rate": 1.10,
        "entry_price_native": 100.0,
        "stop_price": 95.0,
        "target_1": 110.0,
        "target_2": 120.0,
        "exit_monitor_json": {"alerts": ["runner_weakening"]},
    }

    monkeypatch.setattr(advisory, "get_open_virtual_positions", lambda max_age_days=3: [position])
    monkeypatch.setattr(advisory, "_latest_native_price", lambda symbol: 104.0)
    monkeypatch.setattr(advisory, "_send_discord", lambda text, webhook_url: sent.append(text) or True)
    monkeypatch.setattr(advisory, "update_virtual_position", lambda position_id, update: updates.append((position_id, update)) or update)
    monkeypatch.setattr(advisory, "compute_all_signals", lambda *args, **kwargs: pytest.fail("weakening check should be skipped"))

    emitted = advisory._monitor_virtual_positions(
        _cfg(discord_webhook_url="https://discord.test"),
        datetime(2026, 6, 2, 16, 0, tzinfo=timezone.utc),
    )

    assert emitted == []
    assert sent == []
    assert updates == []


def test_benchmark_context_uses_running_bars_without_lookahead():
    idx = pd.to_datetime([
        "2026-06-14T19:59:00Z",
        "2026-06-15T13:30:00Z",
        "2026-06-15T13:31:00Z",
        "2026-06-15T13:32:00Z",
        "2026-06-15T13:33:00Z",
    ])
    bars = pd.DataFrame(
        {
            "Open": [50.0, 100.0, 101.0, 102.0, 200.0],
            "High": [51.0, 101.0, 102.0, 103.0, 201.0],
            "Low": [49.0, 99.0, 100.0, 101.0, 199.0],
            "Close": [50.0, 100.0, 101.0, 102.0, 200.0],
            "Volume": [1000, 10, 10, 10, 10],
        },
        index=idx,
    )

    payload = advisory._benchmark_bar_context(
        "SPY",
        bars,
        datetime(2026, 6, 15, 13, 32, 30, tzinfo=timezone.utc),
    )

    assert payload["status"] == "ok"
    assert payload["bars"] == 3
    assert payload["current"] == 102.0
    assert payload["session_open"] == 100.0
    assert payload["return_from_open_pct"] == pytest.approx(2.0)
    assert payload["running_vwap"] == pytest.approx(101.0)
    assert payload["vs_vwap_pct"] == pytest.approx(0.99, rel=1e-3)
