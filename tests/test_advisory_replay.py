from datetime import datetime, timedelta, timezone

from backend.analytics import replay


def test_replay_one_advisory_signal_scores_all_forward_windows(monkeypatch):
    created_at = datetime.now(timezone.utc) - timedelta(minutes=90)
    signal = {
        "id": 42,
        "created_at": created_at.isoformat(),
        "data_symbol": "AMZN",
        "side": "BUY",
        "entry_min": 214.0,
        "entry_max": 216.0,
        "grade": "A",
        "status": "skipped",
        "signal_json": {"alert_stage": "watch"},
    }

    def fake_window(ticker, side, start_at, reference_price, windows, period="5d"):
        assert ticker == "AMZN"
        assert side == "BUY"
        assert reference_price == 215.0
        assert windows == (5, 15, 30, 60)
        return {
            "forward_returns": {5: 0.25, 15: 0.7, 30: 1.1, 60: 0.6},
            "max_favorable_pct": 1.4,
            "max_adverse_pct": -0.2,
            "close_after_pct": 0.6,
            "bars_seen": 61,
            "bars_by_window": {"5": 6, "15": 16, "30": 31, "60": 61},
            "reference_price": 215.0,
            "start": created_at.isoformat(),
            "end": (created_at + timedelta(minutes=60)).isoformat(),
            "side": "BUY",
        }

    monkeypatch.setattr(replay, "_advisory_price_window", fake_window)

    result = replay._replay_one_advisory_signal(signal)

    assert result["forward_return_5m"] == 0.25
    assert result["forward_return_15m"] == 0.7
    assert result["forward_return_30m"] == 1.1
    assert result["forward_return_60m"] == 0.6
    assert result["forward_scored_at"]
    assert result["advisory_replay_json"]["status"] == "complete"
    assert result["advisory_replay_json"]["alert_stage"] == "watch"


def test_replay_one_advisory_signal_keeps_partial_rows_open(monkeypatch):
    created_at = datetime.now(timezone.utc) - timedelta(minutes=20)
    signal = {
        "id": 43,
        "created_at": created_at.isoformat(),
        "data_symbol": "META",
        "side": "BUY",
        "entry_min": 300.0,
        "entry_max": 302.0,
    }

    monkeypatch.setattr(replay, "_advisory_price_window", lambda *args, **kwargs: {
        "forward_returns": {5: 0.1, 15: 0.3},
        "max_favorable_pct": 0.5,
        "max_adverse_pct": -0.1,
        "close_after_pct": 0.3,
        "bars_seen": 16,
        "bars_by_window": {"5": 6, "15": 16},
        "reference_price": 301.0,
        "start": created_at.isoformat(),
        "end": (created_at + timedelta(minutes=15)).isoformat(),
    })

    result = replay._replay_one_advisory_signal(signal)

    assert result["forward_return_5m"] == 0.1
    assert result["forward_return_15m"] == 0.3
    assert result["forward_return_30m"] is None
    assert result["forward_return_60m"] is None
    assert result["forward_scored_at"] is None
    assert result["advisory_replay_json"]["status"] == "partial"


def test_replay_advisory_signals_updates_pending_rows(monkeypatch):
    signal = {
        "id": 44,
        "created_at": (datetime.now(timezone.utc) - timedelta(minutes=90)).isoformat(),
        "data_symbol": "AMZN",
        "side": "BUY",
        "entry_min": 214.0,
        "entry_max": 216.0,
    }
    updates = []
    logs = []

    monkeypatch.setattr(replay, "get_unscored_advisory_signals", lambda **kwargs: [signal])
    monkeypatch.setattr(replay, "_replay_one_advisory_signal", lambda row: {
        "forward_return_5m": 0.2,
        "forward_return_15m": 0.4,
        "forward_return_30m": 0.8,
        "forward_return_60m": 0.5,
        "forward_scored_at": datetime.now(timezone.utc).isoformat(),
        "advisory_replay_json": {"status": "complete"},
    })
    monkeypatch.setattr(
        replay,
        "update_advisory_signal_replay",
        lambda signal_id, payload: updates.append((signal_id, payload)) or {"id": signal_id},
    )
    monkeypatch.setattr(replay, "log_event", lambda level, event, payload: logs.append((level, event, payload)))

    replay._replay_advisory_signals()

    assert updates[0][0] == 44
    assert updates[0][1]["forward_return_60m"] == 0.5
    assert logs[-1][1] == "advisory_replay_complete"
    assert logs[-1][2]["finalized"] == 1
