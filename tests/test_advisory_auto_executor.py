import backend.advisory_auto.executor as executor
from types import SimpleNamespace


def _signal(**overrides):
    row = {
        "id": 101,
        "data_symbol": "NVDA",
        "grade": "A",
        "side": "BUY",
        "created_at": "2026-06-16T13:45:00+00:00",
        "entry_min": 100.0,
        "entry_max": 102.0,
        "do_not_chase_price": 103.0,
        "stop_price": 98.0,
        "target_1": 106.0,
        "suggested_size_eur": 1000.0,
        "composite_score": 0.72,
        "signal_json": {"alert_stage": "trade"},
        "fx_rate": 1.08,
    }
    row.update(overrides)
    return row


def test_dry_run_only_keeps_eligible_decision_without_order(monkeypatch):
    decisions = []
    orders = []
    signal = _signal()

    monkeypatch.setattr(executor, "DRY_RUN", True)
    monkeypatch.setattr(executor, "PAPER_EXECUTION", False)
    monkeypatch.setattr(executor, "get_active_advisory_auto_signals", lambda limit=100: [])
    monkeypatch.setattr(executor, "get_advisory_auto_daily_pnl", lambda: 0)
    monkeypatch.setattr(executor, "get_advisory_auto_open_count", lambda: 0)
    monkeypatch.setattr(executor, "_get_alpaca_positions", lambda: {})
    monkeypatch.setattr(executor, "_get_alpaca_open_orders", lambda: set())
    monkeypatch.setattr(executor, "get_advisory_auto_eligible", lambda **_: [signal])
    monkeypatch.setattr(executor, "_get_current_price", lambda ticker: 101.0)
    monkeypatch.setattr(executor, "mark_advisory_auto_decision",
                        lambda *args, **kwargs: decisions.append((args, kwargs)) or {})
    monkeypatch.setattr(executor, "_submit_paper_bracket_order",
                        lambda *args, **kwargs: orders.append((args, kwargs)) or {})
    monkeypatch.setattr(executor, "log_event", lambda *args, **kwargs: None)

    result = executor.run_advisory_auto_cycle()

    assert result["dry_run"] is True
    assert result["paper_execution"] is False
    assert len(result["eligible"]) == 1
    assert result["submitted"] == []
    assert orders == []
    assert decisions[0][0][:2] == (101, "eligible")


def test_paper_execution_submits_order_and_preserves_eligible_log(monkeypatch):
    decisions = []
    signal = _signal()

    monkeypatch.setattr(executor, "DRY_RUN", True)
    monkeypatch.setattr(executor, "PAPER_EXECUTION", True)
    monkeypatch.setattr(executor, "get_active_advisory_auto_signals", lambda limit=100: [])
    monkeypatch.setattr(executor, "get_advisory_auto_daily_pnl", lambda: 0)
    monkeypatch.setattr(executor, "get_advisory_auto_open_count", lambda: 0)
    monkeypatch.setattr(executor, "_get_alpaca_positions", lambda: {})
    monkeypatch.setattr(executor, "_get_alpaca_open_orders", lambda: set())
    monkeypatch.setattr(executor, "get_advisory_auto_eligible", lambda **_: [signal])
    monkeypatch.setattr(executor, "_get_current_price", lambda ticker: 101.0)
    monkeypatch.setattr(executor, "mark_advisory_auto_decision",
                        lambda *args, **kwargs: decisions.append((args, kwargs)) or {})
    monkeypatch.setattr(executor, "_submit_paper_bracket_order", lambda sig, current, size_eur: {
        "order_id": "ord-123",
        "client_order_id": "advauto-101-nvda",
        "submitted_qty": 10,
        "limit_price": 101.0,
        "take_profit_price": 106.0,
        "stop_price": 98.0,
        "status": "accepted",
    })
    monkeypatch.setattr(executor, "log_event", lambda *args, **kwargs: None)

    result = executor.run_advisory_auto_cycle()

    assert result["dry_run"] is True
    assert result["paper_execution"] is True
    assert len(result["eligible"]) == 1
    assert result["submitted"][0]["order_id"] == "ord-123"
    assert decisions[0][0][:2] == (101, "eligible")
    assert decisions[1][0][:2] == (101, "submitted")
    assert decisions[1][1]["extra_fields"] == {"auto_order_id": "ord-123"}


def test_paper_order_levels_use_entry_band_and_targets():
    levels = executor._paper_order_levels(_signal(), current_price=101.3, size_eur=1000)

    assert "error" not in levels
    assert levels["ticker"] == "NVDA"
    assert levels["limit_price"] == 101.3
    assert levels["take_profit_price"] == 106.0
    assert levels["stop_price"] == 98.0
    assert levels["qty"] == 10


def test_reconcile_closed_order_writes_trade_and_closes_signal(monkeypatch):
    updates = []
    trades = []
    signal = _signal(
        auto_status="filled",
        auto_order_id="ord-123",
        auto_fill_price=101.0,
        auto_fill_qty=10,
    )
    order = SimpleNamespace(
        status="filled",
        filled_avg_price=101.0,
        filled_qty=10,
        filled_at="2026-06-16T14:00:00+00:00",
        legs=[
            SimpleNamespace(
                id="leg-tp",
                status="filled",
                filled_qty=10,
                filled_avg_price=106.0,
                order_type="limit",
            )
        ],
    )

    monkeypatch.setattr(executor, "get_active_advisory_auto_signals", lambda limit=100: [signal])
    monkeypatch.setattr(executor, "_get_auto_order", lambda order_id: order)
    monkeypatch.setattr(executor, "insert_trade", lambda trade: trades.append(trade) or trade)
    monkeypatch.setattr(executor, "update_advisory_auto_fields",
                        lambda signal_id, fields: updates.append((signal_id, fields)) or {})
    monkeypatch.setattr(executor, "log_event", lambda *args, **kwargs: None)

    result = executor._reconcile_active_orders()

    assert result["closed"] == 1
    assert trades[0]["trade_source"] == "advisory_auto"
    assert trades[0]["pnl_eur"] == 46.3
    assert updates[-1] == (101, {
        "auto_status": "closed",
        "auto_pnl_eur": 46.3,
        "auto_exit_reason": "take_profit",
    })
