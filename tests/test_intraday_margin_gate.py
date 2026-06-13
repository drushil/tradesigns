from backend.broker.alpaca import pre_trade_gate


def _profile(**overrides):
    profile = {
        "allowed_instruments": [],
        "allow_individual_stocks": True,
        "allow_short_selling": True,
        "max_short_position_pct": 10.0,
        "min_short_signal_score": 0.10,
        "bull_short_signal_score": 0.10,
        "dominant_signal_veto_threshold": 0.0,
        "max_drawdown_pct": 20.0,
        "vix_ceiling": 50,
        "cash_buffer_pct": 5.0,
        "min_signal_score": 0.10,
        "max_trades_per_day": 20,
    }
    profile.update(overrides)
    return profile


def _portfolio(**overrides):
    portfolio = {
        "drawdown_today": 0.0,
        "vix": 18.0,
        "cash_pct": 80.0,
        "trades_today": 0,
        "positions": [],
        "broker_equity_usd": 5000.0,
        "buying_power_usd": 2000.0,
        "fx_rate": 1.08,
        "trading_blocked": False,
        "account_blocked": False,
    }
    portfolio.update(overrides)
    return portfolio


def test_pre_trade_gate_blocks_when_order_exceeds_buying_power():
    # 500 EUR * 1.08 = 540 USD > 400 USD available
    allowed, reason = pre_trade_gate(
        "AMZN", "buy", 500.0, 0.35, _profile(), _portfolio(buying_power_usd=400.0)
    )

    assert allowed is False
    assert "insufficient intraday buying power" in reason


def test_pre_trade_gate_allows_when_order_within_buying_power():
    allowed, reason = pre_trade_gate(
        "AMZN", "buy", 500.0, 0.35, _profile(), _portfolio()
    )

    assert allowed is True
    assert reason == "pass"


def test_pre_trade_gate_skips_margin_check_when_buying_power_unknown():
    allowed, reason = pre_trade_gate(
        "AMZN", "buy", 500.0, 0.35, _profile(), _portfolio(buying_power_usd=0)
    )

    assert allowed is True
    assert reason == "pass"


def test_pre_trade_gate_blocks_when_broker_account_blocked():
    allowed, reason = pre_trade_gate(
        "AMZN", "buy", 500.0, 0.35, _profile(), _portfolio(trading_blocked=True)
    )

    assert allowed is False
    assert reason == "broker account trading blocked"
