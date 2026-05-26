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
        "min_ev_pct": 0.50,
        "min_breakout_quality": 0.45,
        "min_discord_grade": "A",
        "shadow_min_discord_grade": "A",
        "us_min_minutes_after_open": 15,
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
    assert "Quick action:" in card
    assert "do not chase" in card
    assert "Exit: stop" in card
    assert "valid until" in card
    assert "FX: 1.0800" in card


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
    assert "DO NOT TRADE YET" in card
    assert "Observation plan:" in card


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
    assert "EU-hours early read on US momentum" in card


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

    assert len(advisory.ADVISORY_UNIVERSE["EU"]) == 18
    assert len(mirrors) == 10
    assert all(item.get("origin_market") == "US" for item in mirrors)
    assert all(item.get("primary_symbol") for item in mirrors)
    assert all(item.get("mirror_only_windows") == ["eu_open"] for item in mirrors)
    assert abs(sum(advisory.EU_MIRROR_WEIGHTS.values()) - 1.0) < 0.001


def test_eu_mirror_data_quality_uses_relaxed_rows_but_volume_floor(monkeypatch):
    class FakeYf:
        @staticmethod
        def download(*args, **kwargs):
            idx = pd.date_range(
                datetime.now(timezone.utc) - timedelta(minutes=24),
                periods=25,
                freq="min",
                tz=timezone.utc,
            )
            return pd.DataFrame({"Close": [100.0] * 25, "Volume": [500] * 25}, index=idx)

    monkeypatch.setitem(__import__("sys").modules, "yfinance", FakeYf)

    native = advisory._data_quality("SAP.DE", "EU")
    mirror = advisory._data_quality("NVD.DE", "EU", listing_type="eu_us_mirror")

    assert native["reason"] == "too_few_bars"
    assert mirror["ok"] is True
    assert mirror["avg_recent_volume"] == pytest.approx(500)


def test_eu_mirror_data_quality_blocks_thin_volume(monkeypatch):
    class FakeYf:
        @staticmethod
        def download(*args, **kwargs):
            idx = pd.date_range(
                datetime.now(timezone.utc) - timedelta(minutes=24),
                periods=25,
                freq="min",
                tz=timezone.utc,
            )
            return pd.DataFrame({"Close": [100.0] * 25, "Volume": [250] * 25}, index=idx)

    monkeypatch.setitem(__import__("sys").modules, "yfinance", FakeYf)

    result = advisory._data_quality("NVD.DE", "EU", listing_type="eu_us_mirror")

    assert result["ok"] is False
    assert result["reason"] == "eu_mirror_thin_volume"


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
    assert "DO NOT TRADE YET" in sent[0]


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
