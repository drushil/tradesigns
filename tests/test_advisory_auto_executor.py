import backend.advisory_auto.executor as executor
from datetime import datetime, timezone
from types import SimpleNamespace


class SimpleNamespace_exc(Exception):
    """Mimics an Alpaca APIError carrying an HTTP status_code."""
    def __init__(self, status_code):
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


def _signal(**overrides):
    row = {
        "id": 101,
        "data_symbol": "NVDA",
        "grade": "A",
        "side": "BUY",
        "created_at": datetime.now(timezone.utc).isoformat(),
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
    monkeypatch.setattr(executor, "ALLOWED_STAGES", {"trade", "watch"})
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
    monkeypatch.setattr(executor, "ALLOWED_STAGES", {"trade", "watch"})
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


def test_watch_stage_can_be_eligible_with_limit_order(monkeypatch):
    decisions = []
    signal = _signal(signal_json={"alert_stage": "watch"})

    monkeypatch.setattr(executor, "DRY_RUN", True)
    monkeypatch.setattr(executor, "PAPER_EXECUTION", True)
    monkeypatch.setattr(executor, "ALLOWED_STAGES", {"trade", "watch"})
    monkeypatch.setattr(executor, "get_active_advisory_auto_signals", lambda limit=100: [])
    monkeypatch.setattr(executor, "get_advisory_auto_daily_pnl", lambda: 0)
    monkeypatch.setattr(executor, "get_advisory_auto_open_count", lambda: 0)
    monkeypatch.setattr(executor, "_get_alpaca_positions", lambda: {})
    monkeypatch.setattr(executor, "_get_alpaca_open_orders", lambda: set())
    monkeypatch.setattr(executor, "get_advisory_auto_eligible", lambda **_: [signal])
    monkeypatch.setattr(executor, "_get_current_price", lambda ticker: 102.8)
    monkeypatch.setattr(executor, "mark_advisory_auto_decision",
                        lambda *args, **kwargs: decisions.append((args, kwargs)) or {})
    monkeypatch.setattr(executor, "_submit_paper_bracket_order", lambda sig, current, size_eur: {
        "order_id": "ord-watch",
        "client_order_id": "advauto-101-nvda",
        "submitted_qty": 10,
        "limit_price": 102.0,
        "take_profit_price": 106.0,
        "stop_price": 98.0,
        "status": "accepted",
    })
    monkeypatch.setattr(executor, "log_event", lambda *args, **kwargs: None)

    result = executor.run_advisory_auto_cycle()

    assert len(result["eligible"]) == 1
    assert result["submitted"][0]["order_id"] == "ord-watch"
    assert decisions[0][0][:2] == (101, "eligible")


def test_watch_stage_still_respects_do_not_chase(monkeypatch):
    decisions = []
    signal = _signal(signal_json={"alert_stage": "watch"})

    monkeypatch.setattr(executor, "DRY_RUN", True)
    monkeypatch.setattr(executor, "PAPER_EXECUTION", True)
    monkeypatch.setattr(executor, "ALLOWED_STAGES", {"trade", "watch"})
    monkeypatch.setattr(executor, "get_active_advisory_auto_signals", lambda limit=100: [])
    monkeypatch.setattr(executor, "get_advisory_auto_daily_pnl", lambda: 0)
    monkeypatch.setattr(executor, "get_advisory_auto_open_count", lambda: 0)
    monkeypatch.setattr(executor, "_get_alpaca_positions", lambda: {})
    monkeypatch.setattr(executor, "_get_alpaca_open_orders", lambda: set())
    monkeypatch.setattr(executor, "get_advisory_auto_eligible", lambda **_: [signal])
    monkeypatch.setattr(executor, "_get_current_price", lambda ticker: 103.5)
    monkeypatch.setattr(executor, "mark_advisory_auto_decision",
                        lambda *args, **kwargs: decisions.append((args, kwargs)) or {})
    monkeypatch.setattr(executor, "log_event", lambda *args, **kwargs: None)

    result = executor.run_advisory_auto_cycle()

    assert result["eligible"] == []
    assert result["submitted"] == []
    assert result["skipped"][0]["reason"] == "skipped_chase:103.50>103.00"


def _base_cycle_mocks(monkeypatch, signal, decisions, current=101.0):
    monkeypatch.setattr(executor, "DRY_RUN", True)
    monkeypatch.setattr(executor, "PAPER_EXECUTION", True)
    monkeypatch.setattr(executor, "ALLOWED_STAGES", {"trade", "watch"})
    monkeypatch.setattr(executor, "get_active_advisory_auto_signals", lambda limit=100: [])
    monkeypatch.setattr(executor, "get_advisory_auto_daily_pnl", lambda: 0)
    monkeypatch.setattr(executor, "get_advisory_auto_open_count", lambda: 0)
    monkeypatch.setattr(executor, "_get_alpaca_positions", lambda: {})
    monkeypatch.setattr(executor, "_get_alpaca_open_orders", lambda: set())
    monkeypatch.setattr(executor, "get_advisory_auto_eligible", lambda **_: [signal])
    monkeypatch.setattr(executor, "_get_current_price", lambda ticker: current)
    monkeypatch.setattr(executor, "mark_advisory_auto_decision",
                        lambda *args, **kwargs: decisions.append((args, kwargs)) or {})


def test_paper_grade_floor_withholds_b_when_set_to_a(monkeypatch):
    decisions, orders, logs = [], [], []
    signal = _signal(grade="B")

    _base_cycle_mocks(monkeypatch, signal, decisions)
    monkeypatch.setattr(executor, "MIN_PAPER_GRADE", "A")
    monkeypatch.setattr(executor, "_submit_paper_bracket_order",
                        lambda *a, **k: orders.append((a, k)) or {})
    monkeypatch.setattr(executor, "log_event",
                        lambda *a, **k: logs.append(a))

    result = executor.run_advisory_auto_cycle()

    # Still eligible for dry-run tracking, but no paper order submitted.
    assert len(result["eligible"]) == 1
    assert result["submitted"] == []
    assert orders == []
    assert any(a[1] == "advisory_auto_paper_grade_withheld" for a in logs)
    # Only the 'eligible' decision is recorded — no 'submitted'.
    assert decisions[0][0][:2] == (101, "eligible")
    assert all(d[0][1] != "submitted" for d in decisions)


def test_paper_grade_floor_allows_a_when_set_to_a(monkeypatch):
    decisions, orders = [], []
    signal = _signal(grade="A")

    _base_cycle_mocks(monkeypatch, signal, decisions)
    monkeypatch.setattr(executor, "MIN_PAPER_GRADE", "A")
    monkeypatch.setattr(executor, "_submit_paper_bracket_order", lambda sig, current, size_eur: {
        "order_id": "ord-a", "client_order_id": "advauto-101-nvda",
        "submitted_qty": 10, "limit_price": 101.0, "take_profit_price": 106.0,
        "stop_price": 98.0, "status": "accepted",
    })
    monkeypatch.setattr(executor, "log_event", lambda *a, **k: None)

    result = executor.run_advisory_auto_cycle()

    assert result["submitted"][0]["order_id"] == "ord-a"


def test_grade_meets_paper_min_default_b_accepts_all(monkeypatch):
    monkeypatch.setattr(executor, "MIN_PAPER_GRADE", "B")
    assert executor._grade_meets_paper_min("A+")
    assert executor._grade_meets_paper_min("A")
    assert executor._grade_meets_paper_min("B")


def test_grade_meets_paper_min_a_plus_only(monkeypatch):
    monkeypatch.setattr(executor, "MIN_PAPER_GRADE", "A+")
    assert executor._grade_meets_paper_min("A+")
    assert not executor._grade_meets_paper_min("A")
    assert not executor._grade_meets_paper_min("B")


def test_is_transient_broker_error_classification():
    assert executor._is_transient_broker_error(SimpleNamespace_exc(500))
    assert executor._is_transient_broker_error(SimpleNamespace_exc(503))
    assert not executor._is_transient_broker_error(SimpleNamespace_exc(422))
    assert not executor._is_transient_broker_error(SimpleNamespace_exc(404))

    class ConnectionTimeout(Exception):
        pass
    assert executor._is_transient_broker_error(ConnectionTimeout())

    class ValueError2(Exception):
        pass
    assert not executor._is_transient_broker_error(ValueError2())


def test_submit_order_with_retry_recovers_after_transient_500(monkeypatch):
    monkeypatch.setattr(executor, "SUBMIT_MAX_RETRIES", 2)
    monkeypatch.setattr(executor, "SUBMIT_RETRY_DELAY_S", 0)
    monkeypatch.setattr(executor.time, "sleep", lambda *_: None)
    monkeypatch.setattr(executor, "log_event", lambda *a, **k: None)

    calls = {"n": 0}

    def _submit(req):
        calls["n"] += 1
        if calls["n"] == 1:
            raise SimpleNamespace_exc(500)
        return SimpleNamespace(id="ord-retry", status="accepted")

    monkeypatch.setattr(executor, "_get_auto_client",
                        lambda: SimpleNamespace(submit_order=_submit))

    order = executor._submit_order_with_retry(SimpleNamespace(symbol="NVDA"))
    assert str(order.id) == "ord-retry"
    assert calls["n"] == 2


def test_submit_order_with_retry_does_not_retry_4xx(monkeypatch):
    monkeypatch.setattr(executor, "SUBMIT_MAX_RETRIES", 2)
    monkeypatch.setattr(executor, "SUBMIT_RETRY_DELAY_S", 0)
    monkeypatch.setattr(executor.time, "sleep", lambda *_: None)
    monkeypatch.setattr(executor, "log_event", lambda *a, **k: None)

    calls = {"n": 0}

    def _submit(req):
        calls["n"] += 1
        raise SimpleNamespace_exc(422)

    monkeypatch.setattr(executor, "_get_auto_client",
                        lambda: SimpleNamespace(submit_order=_submit))

    try:
        executor._submit_order_with_retry(SimpleNamespace(symbol="NVDA"))
        assert False, "expected the 422 to propagate"
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 422
    assert calls["n"] == 1  # no retry on 4xx


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
