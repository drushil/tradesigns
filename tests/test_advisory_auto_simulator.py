from datetime import datetime, timezone

import pandas as pd

import backend.advisory_auto.simulator as sim


def test_create_momentum_continuation_sim_for_high_grade_watch(monkeypatch):
    signal = {
        "id": 123,
        "created_at": "2026-06-10T14:35:00Z",
        "valid_until": "2026-06-10T15:05:00Z",
        "data_symbol": "NVDA",
        "market": "US",
        "side": "BUY",
        "grade": "A",
        "composite_score": 8.2,
        "breakout_quality": 7.5,
        "currency": "USD",
        "entry_min": 142.0,
        "entry_max": 143.0,
        "stop_price": 139.5,
        "target_1": 147.0,
        "target_2": 151.0,
        "suggested_size_eur": 500,
        "signal_json": {
            "alert_stage": "watch",
            "atr_data": {"current_price": 144.25},
        },
    }
    inserted = []

    monkeypatch.setattr(sim, "SIM_MOMENTUM_ENABLED", True)
    monkeypatch.setattr(sim, "SIM_MOMENTUM_MIN_GRADE", "A")
    monkeypatch.setattr(sim, "get_eligible_advisory_signals_for_simulation", lambda **_: [signal])
    monkeypatch.setattr(sim, "get_advisory_auto_sim_signal_ids", lambda: set())
    monkeypatch.setattr(sim, "insert_advisory_auto_simulation", lambda payload: inserted.append(payload) or {})

    assert sim._create_momentum_continuation_sims(market="US") == 1
    payload = inserted[0]
    assert payload["mode"] == "momentum_continuation"
    assert payload["status"] == "filled"
    assert payload["fill_price"] == 144.25
    assert payload["entry_policy_quality"] > 1


def test_create_momentum_continuation_skips_lower_grade_watch(monkeypatch):
    signal = {
        "id": 124,
        "created_at": "2026-06-10T14:35:00Z",
        "data_symbol": "TSLA",
        "market": "US",
        "side": "BUY",
        "grade": "B",
        "entry_min": 180.0,
        "entry_max": 181.0,
        "stop_price": 176.0,
        "signal_json": {"alert_stage": "watch", "atr_data": {"current_price": 182.0}},
    }

    monkeypatch.setattr(sim, "SIM_MOMENTUM_ENABLED", True)
    monkeypatch.setattr(sim, "SIM_MOMENTUM_MIN_GRADE", "A")
    monkeypatch.setattr(sim, "get_eligible_advisory_signals_for_simulation", lambda **_: [signal])
    monkeypatch.setattr(sim, "get_advisory_auto_sim_signal_ids", lambda: set())
    monkeypatch.setattr(sim, "insert_advisory_auto_simulation", lambda payload: (_ for _ in ()).throw(AssertionError(payload)))

    assert sim._create_momentum_continuation_sims(market="US") == 0


def test_eod_close_falls_back_to_last_available_close(monkeypatch):
    fill_at = datetime(2026, 6, 10, 15, 0, tzinfo=timezone.utc)
    session_close = datetime(2026, 6, 10, 20, 0, tzinfo=timezone.utc)
    captured = []
    open_fill = {
        "id": 55,
        "advisory_signal_id": 123,
        "data_symbol": "NVDA",
        "market": "US",
        "side": "BUY",
        "grade": "A",
        "mode": "momentum_continuation",
        "fill_at": fill_at.isoformat(),
        "fill_price": 100.0,
        "stop_price": 95.0,
        "status": "filled",
    }
    bars = pd.DataFrame(
        {"Close": [102.5]},
        index=pd.DatetimeIndex([session_close], tz=timezone.utc),
    )

    monkeypatch.setattr(sim, "get_open_filled_simulations", lambda **_: [open_fill])
    monkeypatch.setattr(sim, "_process_filled", lambda *_: {"mfe_pct": 0.0, "mae_pct": 0.0})
    monkeypatch.setattr(sim, "_fetch_1m_bars", lambda *_: bars)
    monkeypatch.setattr(sim, "update_advisory_auto_simulation", lambda sim_id, fields: captured.append((sim_id, fields)) or {})
    monkeypatch.setattr(sim, "log_event", lambda *_, **__: None)

    assert sim._eod_close_fills(market="US", now_utc=datetime(2026, 6, 10, 20, 31, tzinfo=timezone.utc)) == 1
    sim_id, update = captured[0]
    assert sim_id == 55
    assert update["status"] == "closed_eod_win"
    assert update["eod_close_price"] == 102.5
    assert update["r_multiple"] == 0.5
