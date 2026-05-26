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
        "pdt_protection_enabled": True,
        "pdt_max_day_trades_5d": 3,
        "pdt_min_equity_usd": 25000,
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
        "daytrade_count": 3,
        "trading_blocked": False,
        "account_blocked": False,
    }
    portfolio.update(overrides)
    return portfolio


def test_pre_trade_gate_blocks_when_pdt_limit_reached_under_25k():
    allowed, reason = pre_trade_gate(
        "AMZN", "buy", 500.0, 0.35, _profile(), _portfolio()
    )

    assert allowed is False
    assert "pdt_protection" in reason


def test_pre_trade_gate_allows_when_pdt_count_below_limit():
    allowed, reason = pre_trade_gate(
        "AMZN", "buy", 500.0, 0.35, _profile(), _portfolio(daytrade_count=2)
    )

    assert allowed is True
    assert reason == "pass"


def test_pre_trade_gate_allows_pdt_guard_to_be_disabled():
    allowed, reason = pre_trade_gate(
        "AMZN",
        "buy",
        500.0,
        0.35,
        _profile(pdt_protection_enabled=False),
        _portfolio(),
    )

    assert allowed is True
    assert reason == "pass"
