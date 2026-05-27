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
    }
    values.update(overrides)
    return advisory.AdvisoryConfig(**values)


def test_window_name_enforces_trade_republic_friendly_sessions():
    berlin = timezone(timedelta(hours=2))

    assert advisory._window_name("EU", datetime(2026, 5, 15, 9, 14, tzinfo=berlin)) is None
    assert advisory._window_name("EU", datetime(2026, 5, 15, 9, 15, tzinfo=berlin)) == "eu_open"
    assert advisory._window_name("EU", datetime(2026, 5, 15, 14, 30, tzinfo=berlin)) == "eu_catalyst_only"
    assert advisory._window_name("US", datetime(2026, 5, 15, 14, 59, tzinfo=berlin)) is None
    assert advisory._window_name("US", datetime(2026, 5, 15, 15, 15, tzinfo=berlin)) == "us_premarket"
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
    assert "DO NOT CHASE" in card
    assert "Early signal only" in card
    assert "LIVE TRADE ALERT" not in card
    assert "BUY NOW" not in card
    assert "Watch plan: prepare BUY AMZN only on pullback into" in card
    assert "do not chase >" not in card
    assert "Tentative levels:" in card
    assert "Pullback plan:" in card
    assert "Size/risk:" not in card
    assert "Exit: stop" not in card
    assert "EV: 0.20%" in card


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
    assert "IS EXTENDED" in card
    assert "Execution gate says this move is extended" in card
    assert "Wait for a pullback" in card
    assert "LIVE TRADE ALERT" not in card
    assert "do not chase >" not in card
    assert "Tentative levels:" in card
    assert "Pullback plan:" in card
    assert "EV: 0.70%" in card


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
    assert "Pullback confirmed" in card
    assert "entry band is now valid" in card


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
