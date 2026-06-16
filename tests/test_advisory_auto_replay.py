from datetime import datetime, timezone

import pandas as pd
import pytest

from backend.advisory_auto import replay

# These tests build real pandas DataFrames with a DatetimeIndex. In bare
# environments conftest stubs pandas as a MagicMock (no DatetimeIndex), so the
# whole module is skipped there; it runs in full CI where requirements install pandas.
pytestmark = pytest.mark.skipif(
    not hasattr(pd, "DatetimeIndex"),
    reason="requires real pandas (bare-env conftest stub lacks DataFrame/DatetimeIndex)",
)


def _bars(rows):
    """rows: list of (minute, low, high, close)."""
    idx = pd.DatetimeIndex(
        [datetime(2026, 6, 10, 14, m, tzinfo=timezone.utc) for m, *_ in rows]
    )
    return pd.DataFrame(
        {"Low": [r[1] for r in rows], "High": [r[2] for r in rows], "Close": [r[3] for r in rows]},
        index=idx,
    )


def _sim(**over):
    s = {"id": 1, "data_symbol": "AMD", "side": "BUY",
         "fill_at": "2026-06-10T14:00:00Z", "fill_price": 100.0,
         "stop_price": 98.0, "target_1": 104.0, "target_2": 108.0,
         "mode": "watch_pullback", "entry_policy": "watch_pullback"}
    s.update(over)
    return s


def test_replay_books_stop_at_stop_price():
    bars = _bars([(1, 100.0, 101.0, 100.5), (2, 97.5, 99.0, 98.2)])  # low 97.5 <= stop 98
    res = replay.replay_one(_sim(), 0.8, 0.5, bars=bars)
    assert res["status"] == "hit_stop"
    assert res["exit_price"] == 98.0
    assert res["r"] == -1.0  # (98-100)/(100-98)


def test_replay_books_target_1_at_t1():
    bars = _bars([(1, 100.0, 101.0, 100.5), (2, 102.0, 105.0, 104.5)])  # high 105 >= t1 104
    res = replay.replay_one(_sim(), 0.8, 0.5, bars=bars)
    assert res["status"] == "hit_target_1"
    assert res["exit_price"] == 104.0
    assert res["r"] == 2.0  # (104-100)/2


def test_replay_near_t1_protection_triggers_on_retrace():
    # Run to 103.5 (mfe 3.5% >= arm 0.8*4% = 3.2%) on a bar that stays above
    # the retrace trigger (peak - 0.5R = 102.5), then give back below it.
    bars = _bars([(1, 103.0, 103.5, 103.2), (2, 102.3, 103.0, 102.4)])
    res = replay.replay_one(_sim(), 0.8, 0.5, bars=bars)
    assert res["status"] == "hit_near_t1_protection"
    assert res["exit_price"] == 102.4  # books at the bar close
    assert res["r"] > 0


def test_lower_arm_catches_a_giveback_the_default_misses():
    # Peak 103.0 = 75% to T1 (arm 0.8 needs 3.2%, so does NOT arm at 3.0%),
    # then price gives back and rolls to a stop. arm 0.6 (=2.4%) arms and saves it.
    bars = _bars([(1, 100.0, 103.0, 102.8), (2, 102.0, 102.5, 102.1), (3, 97.5, 99.0, 98.0)])
    default = replay.replay_one(_sim(), 0.8, 0.5, bars=bars)
    lowered = replay.replay_one(_sim(), 0.6, 0.5, bars=bars)
    assert default["status"] == "hit_stop"
    assert default["r"] == -1.0
    assert lowered["status"] == "hit_near_t1_protection"
    assert lowered["r"] > default["r"]  # give-back protection converts the loss


def test_replay_no_terminal_marks_eod():
    bars = _bars([(1, 99.5, 101.0, 100.8), (2, 99.8, 101.5, 101.2)])  # never hits stop/t1
    res = replay.replay_one(_sim(), 0.8, 0.5, bars=bars)
    assert res["status"] == "closed_eod_win"
    assert res["exit_price"] == 101.2


def test_stored_bar_path_round_trips_to_same_result():
    # A sim with a stored bars_json must replay identically to one fed live bars.
    bars = _bars([(1, 100.0, 101.0, 100.5), (2, 102.0, 105.0, 104.5)])  # hits T1
    live = replay.replay_one(_sim(), 0.8, 0.5, bars=bars)
    stored_sim = _sim(bars_json={
        "date": "2026-06-10",
        "bars": [["14:01", 100.0, 101.0, 100.5], ["14:02", 102.0, 105.0, 104.5]],
    })
    # bars=None forces it to read bars_json (no network).
    from_storage = replay.replay_one(stored_sim, 0.8, 0.5, bars=None)
    assert from_storage["status"] == live["status"] == "hit_target_1"
    assert from_storage["r"] == live["r"] == 2.0


def test_classify_entry_buckets():
    # placement, entry_max, t1, tol, dnc
    assert replay.classify_entry(100.1, 100.0, 104.0, 0.15, 1.5) == "in_or_near_band"  # within 0.15% tol
    assert replay.classify_entry(105.0, 100.0, 104.0, 0.15, 1.5) == "stale_past_t1"     # past T1
    assert replay.classify_entry(102.0, 100.0, 104.0, 0.15, 1.5) == "too_extended"      # >1.5% above
    assert replay.classify_entry(101.0, 100.0, 104.0, 0.15, 1.5) == "reroute_candidate" # above band, below T1


def test_analyze_missed_entry_reroute_recovers_a_winner():
    # Placement 101 (above band top 100, below T1 104) -> reroute candidate;
    # price then runs to T1.
    bars = _bars([(1, 100.8, 101.2, 101.0), (2, 103.5, 105.0, 104.6)])
    sim = _sim(simulated_at="2026-06-10T14:00:00Z", entry_min=99.0, entry_max=100.0,
               stop_price=98.0, target_1=104.0, target_2=108.0)
    a = replay.analyze_missed_entry(sim, bars=bars)
    assert a["classification"] == "reroute_candidate"
    assert a["momentum_r"] == 1.0  # fill 101, stop 98 (R=3); exit T1 104 -> +3/3 = 1.0


def test_analyze_missed_entry_stale_is_no_trade():
    bars = _bars([(1, 104.5, 105.0, 104.8)])  # placement 104.8 already past T1 104
    sim = _sim(simulated_at="2026-06-10T14:00:00Z", entry_min=99.0, entry_max=100.0,
               stop_price=98.0, target_1=104.0)
    a = replay.analyze_missed_entry(sim, bars=bars)
    assert a["classification"] == "stale_past_t1"
    assert a["momentum_r"] is None  # correctly not rerouted


def test_sweep_reports_per_policy_rows(monkeypatch):
    bars = _bars([(1, 100.0, 105.0, 104.5)])  # immediate T1
    monkeypatch.setattr(replay, "_fetch_1m_bars", lambda *a, **k: bars)
    rows = replay.sweep([_sim()], arms=[0.6, 0.8], retraces=[0.5])
    assert len(rows) == 2  # 2 arms x 1 retrace x 1 policy
    assert all(row["policy"] == "watch_pullback" for row in rows)
    assert all(row["n"] == 1 for row in rows)
