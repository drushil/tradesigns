from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import backend.advisory as advisory


def _cfg(**overrides):
    values = {
        "markets": {"US"},
        "live_markets": {"US"},
        "shadow_markets": {"EU"},
        "capital_eur": 5000.0,
        "max_live_alerts_per_day": 3,
        "max_shadow_signals_per_day": 10,
        "max_open_live_trades": 1,
        "max_live_trades_per_session": 2,
        "risk_per_trade_eur": 50.0,
        "max_daily_loss_eur": 150.0,
        "default_size_eur": 750.0,
        "a_plus_max_size_eur": 1500.0,
        "min_composite": 0.45,
        "min_ev_pct": 0.50,
        "min_breakout_quality": 0.45,
        "allow_short": False,
        "discord_webhook_url": "https://discord.invalid/webhook",
        "fx_rate": 1.08,
    }
    values.update(overrides)
    return advisory.AdvisoryConfig(**values)


def test_window_name_enforces_trade_republic_friendly_sessions():
    berlin = timezone(timedelta(hours=2))

    assert advisory._window_name("EU", datetime(2026, 5, 15, 9, 14, tzinfo=berlin)) is None
    assert advisory._window_name("EU", datetime(2026, 5, 15, 9, 15, tzinfo=berlin)) == "eu_open"
    assert advisory._window_name("EU", datetime(2026, 5, 15, 14, 30, tzinfo=berlin)) == "eu_catalyst_only"
    assert advisory._window_name("US", datetime(2026, 5, 15, 15, 29, tzinfo=berlin)) is None
    assert advisory._window_name("US", datetime(2026, 5, 15, 15, 30, tzinfo=berlin)) == "us_open"
    assert advisory._window_name("US", datetime(2026, 5, 15, 20, 30, tzinfo=berlin)) == "us_afternoon"


def test_entry_plan_caps_size_by_risk_and_capital():
    cfg = _cfg(risk_per_trade_eur=50.0, a_plus_max_size_eur=1800.0)

    plan = advisory._entry_plan(price=100.0, side="BUY", atr_pct=1.0, currency="USD", cfg=cfg, grade="A+")

    assert plan["suggested_size_eur"] == pytest.approx(1500.0)
    assert plan["risk_eur"] == pytest.approx(21.0)
    assert plan["stop_price"] < 100.0
    assert plan["target_1"] > 100.0
    assert plan["do_not_chase_price"] > plan["entry_max"]


def test_trade_card_is_actionable_for_manual_execution():
    cfg = _cfg()
    plan = advisory._entry_plan(price=100.0, side="BUY", atr_pct=1.0, currency="USD", cfg=cfg, grade="A")
    signal = {
        "mode": "live",
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
    assert "VERY GOOD BUY OPPORTUNITY" in card
    assert card.index("Quick action:") < card.index("Execution detail")
    assert "DO NOT CHASE" in card
    assert "Stop:" in card
    assert "Target 1:" in card
    assert "Valid until" in card
    assert "FX: 1.0800" in card


def test_ordered_markets_prioritizes_live_alerts_before_shadow_learning():
    cfg = _cfg(markets={"US", "EU"}, live_markets={"US"}, shadow_markets={"EU"})

    assert advisory._ordered_markets(cfg) == ["US", "EU"]


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
    monkeypatch.setattr(advisory, "_data_quality", lambda symbol, market: {
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
    monkeypatch.setattr(advisory, "_send_discord", lambda text, webhook_url: sent.append(text) or True)
    monkeypatch.setattr(advisory, "log_event", lambda *args, **kwargs: None)

    result = advisory.run_advisory_cycle()

    assert result["emitted"] == 1
    assert result["live_sent_today"] == 1
    assert len(saved) == 1
    assert saved[0]["status"] == "sent"
    assert len(sent) == 1
    assert "NVDA" in sent[0]


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
    monkeypatch.setattr(advisory, "_data_quality", lambda symbol, market: {
        "ok": True, "last_price": 180.0, "rows": 90, "age_minutes": 1.0, "avg_recent_volume": 100000,
    })
    monkeypatch.setattr(advisory, "detect_regime", lambda symbol: SimpleNamespace(
        market_regime="bull", intraday_regime="trending",
    ))
    monkeypatch.setattr(advisory, "compute_all_signals", lambda symbol, weights, regime_state=None: {
        "composite_score": 0.38,
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
    assert sent == []
