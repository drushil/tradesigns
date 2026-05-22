import sys
import types


supabase = types.ModuleType("supabase")
supabase.create_client = lambda *args, **kwargs: None
supabase.Client = object
sys.modules.setdefault("supabase", supabase)

from backend import agent


def test_rehydrated_open_trade_restores_runtime_fields():
    trade = agent._rehydrated_open_trade({
        "ticker": "ARM",
        "created_at": "2026-05-13T13:00:00+00:00",
        "entry_time": "2026-05-13T13:30:00+00:00",
        "entry_price": 210.5,
        "quantity": 2,
        "submitted_qty": 2,
        "side": "BUY",
        "order_id": "order-1",
        "sizing_json": {"atr_pct": 1.4, "stop_pct": 2.1, "stop_multiplier": 1.5},
        "partial_exit_done": True,
        "partial_exit_qty": 1,
        "highest_price_since_entry": 217.0,
        "vwap_thesis_strike_count": 2,
        "setup_grade": "A+",
        "breakeven_stop_set": True,
        "runner_trail_update_count": 3,
        "runner_trail_last_update_at": "2026-05-13T14:05:00+00:00",
        "hold_score_latest": 0.42,
        "hold_score_min": -0.12,
        "hold_score_max": 0.63,
        "trim_done": True,
    })

    assert trade["entry_price"] == 210.5
    assert trade["quantity"] == 2
    assert trade["partial_exit_done"] is True
    assert trade["partial_exit_qty"] == 1
    assert trade["highest_price_since_entry"] == 217.0
    assert trade["vwap_thesis_strike_count"] == 2
    assert trade["atr_pct"] == 1.4
    assert trade["stop_pct"] == 2.1
    assert trade["setup_grade"] == "A+"
    assert trade["breakeven_stop_set"] is True
    assert trade["runner_trail_update_count"] == 3
    assert trade["runner_trail_last_update_at"] == "2026-05-13T14:05:00+00:00"
    assert trade["hold_score_latest"] == 0.42
    assert trade["hold_score_min"] == -0.12
    assert trade["hold_score_max"] == 0.63
    assert trade["trim_done"] is True


def test_hydrate_open_trades_closes_stale_db_rows(monkeypatch):
    records = [
        {"ticker": "AMD", "status": "open", "entry_price": 100, "side": "BUY"},
        {"ticker": "MSTR", "status": "open", "closed_at": "2026-05-13T14:00:00+00:00"},
        {"ticker": "PLTR", "status": "open", "entry_price": 20, "side": "BUY"},
    ]
    closed = []
    logs = []

    import backend.execution.exit as exit_mod
    monkeypatch.setattr(exit_mod, "get_open_trade_records", lambda: records)
    monkeypatch.setattr(exit_mod, "close_open_trade_record", lambda ticker, reason=None: closed.append((ticker, reason)))
    monkeypatch.setattr(exit_mod, "log_event", lambda level, event, detail=None: logs.append((level, event, detail)))

    agent._open_trades.clear()
    agent._open_trades["OLD"] = {"entry_price": 1}
    agent._hydrate_open_trades([
        {"ticker": "AMD"},
    ])

    assert sorted(agent._open_trades) == ["AMD"]
    assert ("MSTR", "closed_at_present") in closed
    assert ("PLTR", "not_in_broker_positions") in closed
    assert any(event == "stale_open_trade_reconciled" for _, event, _ in logs)


def test_hydrate_open_trades_records_recovered_broker_side_close(monkeypatch):
    record = {
        "ticker": "SOXL",
        "status": "open",
        "entry_time": "2026-05-14T13:52:00+00:00",
        "entry_price": 188.05,
        "quantity": 5,
        "side": "BUY",
        "order_id": "parent-order",
        "size_eur": 870.97,
        "size_usd": 940.65,
        "composite_score": 0.248,
        "llm_conviction": 0.8,
        "signals_json": {},
        "regime": "ranging",
        "strategy_family": "trend_following",
        "exposure_direction": "long_market",
        "setup_grade": "A+",
    }
    inserted = []
    closed = []
    logs = []

    import backend.execution.exit as exit_mod
    import backend.runtime.state as rt_state
    monkeypatch.setattr(exit_mod, "get_open_trade_records", lambda: [record])
    monkeypatch.setattr(exit_mod, "close_position", lambda ticker: {"error": "position not found"})
    monkeypatch.setattr(exit_mod, "_cancel_bracket_orders_for_manual_exit", lambda ticker, trade: [])
    monkeypatch.setattr(exit_mod, "_recover_protective_stop_fill", lambda trade: None)
    monkeypatch.setattr(exit_mod, "_recover_bracket_fill", lambda trade: {
        "exit_price": 182.36,
        "exit_reason": "stop_loss",
        "close_order_id": "stop-leg",
    })
    monkeypatch.setattr(exit_mod, "insert_trade", lambda trade: inserted.append(trade) or {})
    monkeypatch.setattr(exit_mod, "close_open_trade_record", lambda ticker, reason=None: closed.append((ticker, reason)))
    monkeypatch.setattr(exit_mod, "log_event", lambda level, event, detail=None: logs.append((level, event, detail)))
    rt_state._learning_engine = None

    agent._open_trades.clear()
    agent._hydrate_open_trades([])

    assert len(inserted) == 1
    assert inserted[0]["ticker"] == "SOXL"
    assert inserted[0]["exit_price"] == 182.36
    assert inserted[0]["exit_reason"] == "stop_loss"
    assert inserted[0]["close_order_id"] == "stop-leg"
    assert ("SOXL", "stop_loss") in closed
    assert any(event == "bracket_fill_recovered" for _, event, _ in logs)
    assert "SOXL" not in agent._open_trades


def test_execution_overrides_default_to_a_grade_with_b_exploration(monkeypatch):
    monkeypatch.delenv("MIN_GRADE_REQUIRED", raising=False)
    monkeypatch.delenv("ALLOW_B_GRADE_EXPLORATION", raising=False)

    profile = agent._apply_execution_overrides({
        "paper_overrides": {},
        "min_signal_score": 0.1,
        "min_conviction": 0.3,
    })

    assert profile["min_grade_required"] == "A"
    assert profile["allow_b_grade_exploration"] is False
    assert profile["b_grade_size_multiplier"] == 0.20
