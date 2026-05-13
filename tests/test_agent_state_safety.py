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


def test_hydrate_open_trades_closes_stale_db_rows(monkeypatch):
    records = [
        {"ticker": "AMD", "status": "open", "entry_price": 100, "side": "BUY"},
        {"ticker": "MSTR", "status": "open", "closed_at": "2026-05-13T14:00:00+00:00"},
        {"ticker": "PLTR", "status": "open", "entry_price": 20, "side": "BUY"},
    ]
    closed = []
    logs = []

    monkeypatch.setattr(agent, "get_open_trade_records", lambda: records)
    monkeypatch.setattr(agent, "close_open_trade_record", lambda ticker, reason=None: closed.append((ticker, reason)))
    monkeypatch.setattr(agent, "log_event", lambda level, event, detail=None: logs.append((level, event, detail)))

    agent._open_trades.clear()
    agent._open_trades["OLD"] = {"entry_price": 1}
    agent._hydrate_open_trades([
        {"ticker": "AMD"},
    ])

    assert sorted(agent._open_trades) == ["AMD"]
    assert ("MSTR", "closed_at_present") in closed
    assert ("PLTR", "not_in_broker_positions") in closed
    assert any(event == "stale_open_trade_reconciled" for _, event, _ in logs)


def test_execution_overrides_default_to_a_grade_with_b_exploration(monkeypatch):
    monkeypatch.delenv("MIN_GRADE_REQUIRED", raising=False)
    monkeypatch.delenv("ALLOW_B_GRADE_EXPLORATION", raising=False)

    profile = agent._apply_execution_overrides({
        "paper_overrides": {},
        "min_signal_score": 0.1,
        "min_conviction": 0.3,
    })

    assert profile["min_grade_required"] == "A"
    assert profile["allow_b_grade_exploration"] is True
    assert profile["b_grade_size_multiplier"] == 0.20

