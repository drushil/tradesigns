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


def test_watch_stage_rejects_when_too_extended(monkeypatch):
    # Watch rests a limit at the band; reject only when price has run more than
    # MAX_CHASE_PCT above entry_max. entry_max=102, 1% → max_allowed=103.02.
    decisions = []
    signal = _signal(signal_json={"alert_stage": "watch"})

    monkeypatch.setattr(executor, "DRY_RUN", True)
    monkeypatch.setattr(executor, "PAPER_EXECUTION", True)
    monkeypatch.setattr(executor, "ALLOWED_STAGES", {"trade", "watch"})
    monkeypatch.setattr(executor, "MAX_CHASE_PCT", 1.0)
    monkeypatch.setattr(executor, "get_active_advisory_auto_signals", lambda limit=100: [])
    monkeypatch.setattr(executor, "get_advisory_auto_daily_pnl", lambda: 0)
    monkeypatch.setattr(executor, "get_advisory_auto_open_count", lambda: 0)
    monkeypatch.setattr(executor, "get_advisory_auto_pending_count", lambda: 0)
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
    assert result["skipped"][0]["reason"] == "skipped_chase:103.50>103.02"


def _watch_cycle_mocks(monkeypatch, signal, current, decisions):
    monkeypatch.setattr(executor, "DRY_RUN", True)
    monkeypatch.setattr(executor, "PAPER_EXECUTION", True)
    monkeypatch.setattr(executor, "ALLOWED_STAGES", {"trade", "watch"})
    monkeypatch.setattr(executor, "MAX_CHASE_PCT", 1.0)
    monkeypatch.setattr(executor, "get_active_advisory_auto_signals", lambda limit=100: [])
    monkeypatch.setattr(executor, "get_advisory_auto_daily_pnl", lambda: 0)
    monkeypatch.setattr(executor, "get_advisory_auto_open_count", lambda: 0)
    monkeypatch.setattr(executor, "get_advisory_auto_pending_count", lambda: 0)
    monkeypatch.setattr(executor, "_get_alpaca_positions", lambda: {})
    monkeypatch.setattr(executor, "_get_alpaca_open_orders", lambda: set())
    monkeypatch.setattr(executor, "get_advisory_auto_eligible", lambda **_: [signal])
    monkeypatch.setattr(executor, "_get_current_price", lambda ticker: current)
    monkeypatch.setattr(executor, "_submit_paper_bracket_order", lambda *a, **k: {
        "order_id": "o", "client_order_id": "c", "submitted_qty": 1,
        "limit_price": 102.0, "take_profit_price": 106.0, "stop_price": 98.0, "status": "accepted",
    })
    monkeypatch.setattr(executor, "mark_advisory_auto_decision",
                        lambda *a, **k: decisions.append((a, k)) or {})
    monkeypatch.setattr(executor, "log_event", lambda *a, **k: None)


def test_watch_rejects_below_entry_band(monkeypatch):
    decisions = []
    signal = _signal(signal_json={"alert_stage": "watch"})  # band [100,102], stop 98
    _watch_cycle_mocks(monkeypatch, signal, current=99.0, decisions=decisions)
    result = executor.run_advisory_auto_cycle()
    assert result["eligible"] == []
    assert result["skipped"][0]["reason"].startswith("skipped_below_entry_band")


def test_watch_rejects_below_stop(monkeypatch):
    decisions = []
    signal = _signal(signal_json={"alert_stage": "watch"})  # stop 98
    _watch_cycle_mocks(monkeypatch, signal, current=97.5, decisions=decisions)
    result = executor.run_advisory_auto_cycle()
    assert result["eligible"] == []
    assert result["skipped"][0]["reason"].startswith("skipped_below_stop")


def test_watch_allows_slightly_above_band(monkeypatch):
    # 102.5 is above entry_max (102) but under max_allowed (103.02) → rest a limit.
    decisions = []
    signal = _signal(signal_json={"alert_stage": "watch"})
    _watch_cycle_mocks(monkeypatch, signal, current=102.5, decisions=decisions)
    result = executor.run_advisory_auto_cycle()
    assert len(result["eligible"]) == 1
    assert result["submitted"][0]["order_id"] == "o"


def test_pending_cap_blocks_when_book_full(monkeypatch):
    decisions = []
    signal = _signal(signal_json={"alert_stage": "watch"})
    _watch_cycle_mocks(monkeypatch, signal, current=101.0, decisions=decisions)
    monkeypatch.setattr(executor, "MAX_PENDING_ORDERS", 8)
    monkeypatch.setattr(executor, "get_advisory_auto_pending_count", lambda: 8)  # book full
    result = executor.run_advisory_auto_cycle()
    assert result["eligible"] == []
    assert result["skipped"][0]["reason"].startswith("skipped_pending_cap")


def test_filled_cap_halts_cycle(monkeypatch):
    signal = _signal(signal_json={"alert_stage": "watch"})
    _watch_cycle_mocks(monkeypatch, signal, current=101.0, decisions=[])
    monkeypatch.setattr(executor, "MAX_POSITIONS", 3)
    monkeypatch.setattr(executor, "get_advisory_auto_open_count", lambda: 3)  # filled at cap
    result = executor.run_advisory_auto_cycle()
    assert result.get("halted") is True
    assert result["halt_reason"] == executor._SKIP_POSITION_CAP


def test_paper_levels_reject_below_stop():
    levels = executor._paper_order_levels(_signal(), current_price=97.0, size_eur=1000)  # stop 98
    assert levels["error"] == "price_below_stop"


def test_paper_levels_reject_below_entry_band():
    levels = executor._paper_order_levels(_signal(), current_price=99.5, size_eur=1000)  # min 100
    assert levels["error"] == "price_below_entry_band"


def test_paper_levels_min_one_share_when_affordable():
    # Tiny allocation would floor to 0 shares, but 1 share ($101) fits under the
    # per-position cap (15% of 30k ≈ €4,500), so take 1 share.
    levels = executor._paper_order_levels(_signal(), current_price=101.0, size_eur=10)
    assert "error" not in levels
    assert levels["qty"] == 1


def test_paper_levels_below_min_size_when_one_share_exceeds_cap(monkeypatch):
    # 1 share priced above the per-position cap → clean sizing skip, not an error.
    monkeypatch.setattr(executor, "CAPITAL_EUR", 1000.0)  # cap = €150 ≈ $162
    sig = _signal(entry_min=200.0, entry_max=205.0, stop_price=190.0, target_1=230.0)
    levels = executor._paper_order_levels(sig, current_price=202.0, size_eur=10)
    assert levels["error"] == "below_min_size"


def test_reconcile_cancels_pending_below_stop(monkeypatch):
    signal = _signal(id=500, data_symbol="ARM", auto_status="submitted",
                     auto_order_id="ord-p", stop_price=400.0)
    updates, cancels = [], []
    monkeypatch.setattr(executor, "get_active_advisory_auto_signals", lambda limit=100: [signal])
    monkeypatch.setattr(executor, "_get_auto_order",
                        lambda oid: SimpleNamespace(status="accepted", legs=[]))
    monkeypatch.setattr(executor, "_get_current_price", lambda t: 398.0)  # below stop 400
    monkeypatch.setattr(executor, "_cancel_symbol_orders", lambda t: cancels.append(t))
    monkeypatch.setattr(executor, "update_advisory_auto_fields",
                        lambda sid, f: updates.append((sid, f)) or {})
    monkeypatch.setattr(executor, "log_event", lambda *a, **k: None)

    res = executor._reconcile_active_orders({}, set(),
                                            now_utc=datetime(2026, 6, 18, 18, 0, tzinfo=timezone.utc))

    assert res["pending_cancelled"] == 1
    assert cancels == ["ARM"]
    assert updates[0][1]["auto_exit_reason"] == "cancelled_below_stop"


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


def test_grade_meets_paper_min_normalizes_case(monkeypatch):
    monkeypatch.setattr(executor, "MIN_PAPER_GRADE", "a")
    assert executor._grade_meets_paper_min("a+")
    assert executor._grade_meets_paper_min("a")
    assert not executor._grade_meets_paper_min("b")


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


def _filled_no_exit_order():
    """A bracket parent that filled but whose protective legs are gone."""
    return SimpleNamespace(
        status="filled", filled_avg_price=100.0, filled_qty=5,
        filled_at="2026-06-16T19:53:00+00:00", legs=[],
    )


def test_eod_flat_window_logic(monkeypatch):
    monkeypatch.setattr(executor, "EOD_FLAT_BUFFER_MIN", 10.0)
    # 19:55 UTC = 15:55 ET → 5 min before the 16:00 ET close → in window
    assert executor._is_eod_flat_window(datetime(2026, 6, 17, 19, 55, tzinfo=timezone.utc))
    # 20:30 UTC = 16:30 ET → 30 min after close → still flatten overnight stragglers
    assert executor._is_eod_flat_window(datetime(2026, 6, 17, 20, 30, tzinfo=timezone.utc))
    # 14:00 UTC = 10:00 ET → ~6h before close → not in window
    assert not executor._is_eod_flat_window(datetime(2026, 6, 17, 14, 0, tzinfo=timezone.utc))
    # next morning 13:00 UTC = 09:00 ET → ~7h to close → not in window
    assert not executor._is_eod_flat_window(datetime(2026, 6, 18, 13, 0, tzinfo=timezone.utc))


def test_orphan_guard_flattens_naked_filled_position(monkeypatch):
    # The NFLX case: filled days ago, no live protective order → naked.
    signal = _signal(id=13111, data_symbol="NFLX", auto_status="filled",
                     auto_order_id="ord-x", auto_fill_price=78.72, auto_fill_qty=5,
                     created_at="2026-06-16T19:53:00+00:00")
    flattened = []
    monkeypatch.setattr(executor, "get_active_advisory_auto_signals", lambda limit=100: [signal])
    monkeypatch.setattr(executor, "_get_auto_order", lambda oid: _filled_no_exit_order())
    monkeypatch.setattr(executor, "_flatten_and_record",
                        lambda sig, qty, reason: flattened.append((sig["id"], qty, reason)) or {})
    monkeypatch.setattr(executor, "EOD_FLAT_ENABLED", True)
    monkeypatch.setattr(executor, "ORPHAN_GUARD_ENABLED", True)
    monkeypatch.setattr(executor, "ORPHAN_MIN_AGE_MIN", 3.0)
    monkeypatch.setattr(executor, "_near_t1_protection_qty", lambda *a, **k: None)
    monkeypatch.setattr(executor, "log_event", lambda *a, **k: None)

    now = datetime(2026, 6, 17, 18, 0, tzinfo=timezone.utc)  # mid-session, not EOD
    res = executor._reconcile_active_orders(
        {"NFLX": {"qty": 5, "side": "long"}}, set(), now_utc=now)

    assert res["flattened_orphan"] == 1
    assert flattened == [(13111, 5.0, "orphan_flatten")]


def test_eod_flat_closes_open_position_near_close(monkeypatch):
    signal = _signal(id=200, data_symbol="AMD", auto_status="filled",
                     auto_order_id="ord-amd", auto_fill_price=500.0, auto_fill_qty=2,
                     created_at="2026-06-17T17:00:00+00:00")
    flattened = []
    monkeypatch.setattr(executor, "get_active_advisory_auto_signals", lambda limit=100: [signal])
    monkeypatch.setattr(executor, "_get_auto_order", lambda oid: _filled_no_exit_order())
    monkeypatch.setattr(executor, "_flatten_and_record",
                        lambda sig, qty, reason: flattened.append((sig["id"], qty, reason)) or {})
    monkeypatch.setattr(executor, "EOD_FLAT_ENABLED", True)
    monkeypatch.setattr(executor, "EOD_FLAT_BUFFER_MIN", 10.0)
    monkeypatch.setattr(executor, "_near_t1_protection_qty", lambda *a, **k: None)
    monkeypatch.setattr(executor, "log_event", lambda *a, **k: None)

    now = datetime(2026, 6, 17, 19, 55, tzinfo=timezone.utc)  # 5 min to close
    # EOD flattens even with a live protective order present.
    res = executor._reconcile_active_orders({"AMD": {"qty": 2}}, {"AMD"}, now_utc=now)

    assert res["flattened_eod"] == 1
    assert flattened == [(200, 2.0, "eod_flat")]


def test_orphan_guard_skips_fresh_fill(monkeypatch):
    # _signal_age_minutes measures against wall-clock now, so the fill must be
    # wall-clock-fresh to exercise the "too young" branch. The reconcile now_utc
    # is held mid-session (14:00 ET) so the EOD path doesn't fire instead.
    signal = _signal(id=201, data_symbol="TSLA", auto_status="filled",
                     auto_order_id="ord-t", auto_fill_qty=3,
                     created_at=datetime.now(timezone.utc).isoformat())  # age ~0
    flattened = []
    monkeypatch.setattr(executor, "get_active_advisory_auto_signals", lambda limit=100: [signal])
    monkeypatch.setattr(executor, "_get_auto_order", lambda oid: _filled_no_exit_order())
    monkeypatch.setattr(executor, "_flatten_and_record", lambda *a, **k: flattened.append(a) or {})
    monkeypatch.setattr(executor, "ORPHAN_MIN_AGE_MIN", 3.0)
    monkeypatch.setattr(executor, "EOD_FLAT_ENABLED", True)
    monkeypatch.setattr(executor, "_near_t1_protection_qty", lambda *a, **k: None)
    monkeypatch.setattr(executor, "log_event", lambda *a, **k: None)

    mid_session = datetime(2026, 6, 17, 18, 0, tzinfo=timezone.utc)  # 14:00 ET
    res = executor._reconcile_active_orders({"TSLA": {"qty": 3}}, set(), now_utc=mid_session)

    assert res["flattened_orphan"] == 0
    assert flattened == []


def test_orphan_guard_skips_position_with_open_order(monkeypatch):
    now = datetime(2026, 6, 17, 18, 0, tzinfo=timezone.utc)
    signal = _signal(id=202, data_symbol="MSFT", auto_status="filled",
                     auto_order_id="ord-m", auto_fill_qty=4,
                     created_at="2026-06-16T15:00:00+00:00")  # old enough
    flattened = []
    monkeypatch.setattr(executor, "get_active_advisory_auto_signals", lambda limit=100: [signal])
    monkeypatch.setattr(executor, "_get_auto_order", lambda oid: _filled_no_exit_order())
    monkeypatch.setattr(executor, "_flatten_and_record", lambda *a, **k: flattened.append(a) or {})
    monkeypatch.setattr(executor, "_near_t1_protection_qty", lambda *a, **k: None)
    monkeypatch.setattr(executor, "log_event", lambda *a, **k: None)

    # MSFT has a live protective order → not orphaned.
    res = executor._reconcile_active_orders({"MSFT": {"qty": 4}}, {"MSFT"}, now_utc=now)

    assert res["flattened_orphan"] == 0
    assert flattened == []


def test_near_t1_protection_flattens_before_eod_and_orphan(monkeypatch):
    signal = _signal(id=400, data_symbol="ARM", auto_status="filled",
                     auto_order_id="ord-arm", auto_fill_price=400.0, auto_fill_qty=5,
                     created_at="2026-06-17T15:00:00+00:00")
    flattened = []
    monkeypatch.setattr(executor, "get_active_advisory_auto_signals", lambda limit=100: [signal])
    monkeypatch.setattr(executor, "_get_auto_order", lambda oid: _filled_no_exit_order())
    monkeypatch.setattr(executor, "_flatten_and_record",
                        lambda sig, qty, reason: flattened.append((sig["id"], qty, reason)) or {})
    # Near-T1 fires → should win over EOD/orphan.
    monkeypatch.setattr(executor, "_near_t1_protection_qty", lambda sig, order: 5.0)
    monkeypatch.setattr(executor, "EOD_FLAT_ENABLED", True)
    monkeypatch.setattr(executor, "log_event", lambda *a, **k: None)

    # Even in the EOD window, near-T1 takes priority.
    now = datetime(2026, 6, 17, 19, 55, tzinfo=timezone.utc)
    res = executor._reconcile_active_orders({"ARM": {"qty": 5}}, set(), now_utc=now)

    assert res["near_t1_protected"] == 1
    assert res["flattened_eod"] == 0
    assert flattened == [(400, 5.0, "near_t1_protection")]


def test_near_t1_protection_qty_fires_on_scan_verdict(monkeypatch):
    import backend.advisory_auto.simulator as sim
    sig = _signal(id=401, data_symbol="ARM", auto_fill_price=400.0, auto_fill_qty=5,
                  stop_price=396.0, target_1=410.0, target_2=415.0)
    order = SimpleNamespace(filled_at="2026-06-17T15:00:00+00:00")

    monkeypatch.setattr(executor, "PAPER_NEAR_T1_ENABLED", True)
    monkeypatch.setattr(sim, "_fetch_1m_bars", lambda *a, **k: SimpleNamespace(empty=False))
    monkeypatch.setattr(sim, "_yfinance_symbol", lambda s: s)
    monkeypatch.setattr(sim, "_scan_bars_for_exit",
                        lambda *a, **k: ("hit_near_t1_protection", 408.0, None, 2.0, -0.1, 408.0))

    assert executor._near_t1_protection_qty(sig, order) == 5.0


def test_near_t1_protection_qty_none_on_stop_verdict(monkeypatch):
    import backend.advisory_auto.simulator as sim
    sig = _signal(id=402, data_symbol="ARM", auto_fill_price=400.0, auto_fill_qty=5,
                  stop_price=396.0, target_1=410.0, target_2=415.0)
    order = SimpleNamespace(filled_at="2026-06-17T15:00:00+00:00")

    monkeypatch.setattr(executor, "PAPER_NEAR_T1_ENABLED", True)
    monkeypatch.setattr(sim, "_fetch_1m_bars", lambda *a, **k: SimpleNamespace(empty=False))
    monkeypatch.setattr(sim, "_yfinance_symbol", lambda s: s)
    # The live bracket owns real stop/T1 exits — paper near-T1 must not act on them.
    monkeypatch.setattr(sim, "_scan_bars_for_exit",
                        lambda *a, **k: ("hit_stop", 395.0, None, 0.5, -1.1, None))

    assert executor._near_t1_protection_qty(sig, order) is None


def test_near_t1_protection_qty_disabled_short_circuits(monkeypatch):
    sig = _signal(id=403, auto_fill_price=400.0, auto_fill_qty=5,
                  stop_price=396.0, target_1=410.0)
    order = SimpleNamespace(filled_at="2026-06-17T15:00:00+00:00")
    monkeypatch.setattr(executor, "PAPER_NEAR_T1_ENABLED", False)
    assert executor._near_t1_protection_qty(sig, order) is None


def test_flatten_and_record_submits_sell_and_writes_trade(monkeypatch):
    sig = _signal(id=300, data_symbol="NFLX", auto_fill_price=78.72, auto_fill_qty=5)
    trades, updates = [], []
    monkeypatch.setattr(executor, "_cancel_symbol_orders", lambda t: None)
    monkeypatch.setattr(executor, "_submit_order_with_retry",
                        lambda req: SimpleNamespace(id="sell-1", filled_avg_price=76.0))
    monkeypatch.setattr(executor, "_get_current_price", lambda t: 76.0)
    monkeypatch.setattr(executor, "insert_trade", lambda p: trades.append(p) or p)
    monkeypatch.setattr(executor, "update_advisory_auto_fields",
                        lambda sid, f: updates.append((sid, f)) or {})
    monkeypatch.setattr(executor, "log_event", lambda *a, **k: None)

    trade = executor._flatten_and_record(sig, 5, "orphan_flatten")

    assert trade["exit_reason"] == "orphan_flatten"
    assert trade["exit_price"] == 76.0
    assert trade["trade_source"] == "advisory_auto"
    assert updates[0][0] == 300
    assert updates[0][1]["auto_status"] == "closed"
    assert updates[0][1]["auto_exit_reason"] == "orphan_flatten"


def test_build_trade_payload_pnl():
    sig = _signal(auto_fill_price=100.0)
    p = executor._build_trade_payload(
        sig, entry_price=100.0, qty=10, exit_price=95.0, exit_reason="eod_flat")
    assert p["exit_reason"] == "eod_flat"
    assert p["pnl_pct"] == -5.0
    assert p["pnl_eur"] == round((95 - 100) * 10 / 1.08, 2)  # -46.3
    assert p["trade_source"] == "advisory_auto"


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
