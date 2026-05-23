"""
tests/test_entry_integration.py

4 mechanical integration tests that prove the agent.py refactor preserves
correct behaviour.  All are fully mocked — no network, no DB, no Alpaca.

1. test_state_bridge_is_alias
   Verifies that agent._open_trades / _signal_cache / _cycle_composites are
   the *same dict objects* as state._open_trades etc.  If this breaks, any
   mutation from entry.py would be invisible to agent.py.

2. test_execute_trade_candidate_populates_open_trades
   Drives _execute_trade_candidate with a minimal patched environment and
   asserts that state._open_trades["AAPL"] is populated with the correct side,
   entry_price, and all Codex evidence fields (playbook, session_window,
   data_quality_state, estimated_total_cost_pct).

3. test_record_llm_call_increments_agent_counter
   Calls agent._record_llm_call() directly and asserts that
   agent._llm_calls_this_hour increments.  This counter lives as a scalar
   global in agent.py and must *not* be bridged to state — this test confirms
   that path still works after the refactor.

4. test_evaluate_ticker_candidate_writes_cycle_composites
   Calls _evaluate_ticker_candidate with a gated result (pre_trade_gate
   returns False) and asserts that state._cycle_composites["AAPL"] is written
   to the composite value AND that agent._cycle_composites is the same object
   as state._cycle_composites.
"""
from __future__ import annotations

import pytest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Test 1 — state bridge is alias
# ---------------------------------------------------------------------------

def test_state_bridge_is_alias():
    """agent.* containers must be the exact same objects as state.* containers."""
    import backend.agent as agent
    import backend.runtime.state as state

    assert agent._open_trades is state._open_trades, (
        "_open_trades is a copy, not an alias — bridge is broken"
    )
    assert agent._signal_cache is state._signal_cache, (
        "_signal_cache is a copy, not an alias — bridge is broken"
    )
    assert agent._cycle_composites is state._cycle_composites, (
        "_cycle_composites is a copy, not an alias — bridge is broken"
    )


# ---------------------------------------------------------------------------
# Test 2 — _execute_trade_candidate populates state._open_trades with evidence
# ---------------------------------------------------------------------------

def test_execute_trade_candidate_populates_open_trades():
    """
    _execute_trade_candidate must write side, entry_price, and all Codex
    evidence fields into state._open_trades[ticker].
    """
    import backend.runtime.state as state
    import backend.execution.entry as entry

    state._open_trades.clear()

    # Minimal candidate dict that _execute_trade_candidate expects
    fake_regime_state = SimpleNamespace(
        intraday_regime="trending",
        market_regime="bull",
        vix=18.0,
    )
    setup_context = {
        "playbook":                  "morning_vwap_reclaim",
        "playbook_lifecycle":        "tagged",
        "session_window":            "morning_trend",
        "primary_factor":            "semis",
        "factor_bucket":             "growth",
        "regime_key":                "bull_trending",
        "data_quality_state":        "executable",
        "data_quality":              {},
        "cost_estimate":             {},
        "estimated_spread_pct":      0.05,
        "estimated_total_cost_pct":  0.16,
        "candidate_rank_score":      0.55,
        "theme":                     "semis",
    }
    candidate = {
        "ticker":              "AAPL",
        "ticker_regime":       "trending",
        "ticker_regime_state": fake_regime_state,
        "signal_result": {
            "composite_score": 0.38,
            "signals": {
                "vwap_deviation": {"score": 0.2},
                "tape_aggression": {"score": 0.3},
            },
            "atr_data": {"atr_pct": 0.8, "atr_raw": 1.2},
            "mean_reversion_signal": False,
        },
        "composite":     0.38,
        "signals_snap": {
            "vwap_deviation": {"score": 0.2},
            "tape_aggression": {"score": 0.3},
        },
        "atr_data":      {"atr_pct": 0.8, "atr_raw": 1.2},
        "regime_debug":  {},
        "news_headline": "",
        "setup_context": setup_context,
        "ev_result": {
            "decision":        "allow",
            "ev_decision":     "full_size",
            "size_multiplier": 1.0,
            "net_ev_pct":      0.12,
        },
        "ev_blocked":    False,
        "capital_base":  5000.0,
        "action_hint":   "BUY",
        "setup_grade":   None,
        "orb_score":     0.0,
        "signal_id":     None,
    }
    profile = {
        "min_conviction":        0.30,
        "stop_loss_pct":         2.0,
        "take_profit_pct":       4.0,
        "min_reward_risk_ratio": 1.5,
        "ranging_min_reward_risk_ratio": 2.0,
        "max_hold_minutes":      45,
        "min_hold_minutes":      5,
        "a_plus_full_size_max_atr_pct":  2.5,
        "a_plus_full_size_max_stop_pct": 5.0,
        "allow_a_plus_llm_hold_override": False,
        "probe_floor_inflation_max_multiple": 3.0,
        "ranging_regime_size_multiplier": 0.35,
        "ranging_max_notional_eur": 0,
        "max_trade_notional_eur": 10000,
    }
    portfolio_state = {
        "equity":   5400.0,
        "cash":     3000.0,
        "vix":      18.0,
        "positions": [],
    }

    # Stub sizing result
    sizing_result = {
        "size_eur":    300.0,
        "size_usd":    324.0,
        "stop_pct":    2.0,
        "atr_pct":     0.8,
        "stop_multiplier": 1.0,
    }
    # Stub LLM result
    llm_result = {
        "action":       "BUY",
        "conviction":   0.72,
        "hold_minutes": 30,
        "stop_loss_pct": 2.0,
        "rationale":    "strong momentum",
    }
    # Stub order result
    order_result = {
        "order_id":       "ord-test-001",
        "client_order_id": "ts-1-aapl-b",
        "order_class":    "bracket",
        "qty":            2.0,
    }

    # Build a one-row DataFrame-like for yfinance stub
    import sys
    yf_stub = sys.modules.get("yfinance")
    close_series = MagicMock()
    close_series.squeeze.return_value.iloc.__getitem__ = MagicMock(return_value=150.0)
    df_stub = MagicMock()
    df_stub.empty = False
    df_stub.__getitem__ = MagicMock(return_value=close_series)
    yf_download_mock = MagicMock(return_value=df_stub)

    with patch("backend.agent._can_call_llm", return_value=True), \
         patch("backend.agent._record_llm_call"), \
         patch("backend.execution.entry.compute_position_size", return_value=sizing_result), \
         patch("backend.execution.entry.llm_signal_decision", return_value=llm_result), \
         patch("backend.execution.entry.submit_market_order", return_value=order_result), \
         patch("backend.execution.entry.save_open_trade", return_value={}), \
         patch("backend.execution.entry.log_event"), \
         patch("backend.execution.entry._apply_learned_hold_extension",
               return_value=(30, None)), \
         patch("backend.execution.entry._reward_risk_block", return_value=None), \
         patch("backend.execution.entry._probe_floor_inflation_block", return_value=None), \
         patch("yfinance.download", yf_download_mock):

        entry._execute_trade_candidate(candidate, profile, portfolio_state)

    trade = state._open_trades.get("AAPL")
    assert trade is not None, "state._open_trades['AAPL'] was not written"
    assert trade["side"] == "BUY"
    assert trade["entry_price"] == pytest.approx(150.0)

    # Evidence fields (Codex layer — must survive refactor)
    assert trade["playbook"] == "morning_vwap_reclaim",          "playbook missing"
    assert trade["session_window"] == "morning_trend",           "session_window missing"
    assert trade["data_quality_state"] == "executable",          "data_quality_state missing"
    assert trade["estimated_total_cost_pct"] == pytest.approx(0.16), \
        "estimated_total_cost_pct missing"

    # Cleanup
    state._open_trades.clear()


# ---------------------------------------------------------------------------
# Test 3 — _record_llm_call increments agent scalar counter
# ---------------------------------------------------------------------------

def test_record_llm_call_increments_agent_counter():
    """
    agent._record_llm_call() must increment agent._llm_calls_this_hour.
    This counter is a scalar global in agent.py (not in state) — the refactor
    must not have moved or renamed it.
    """
    import backend.agent as agent

    before = agent._llm_calls_this_hour
    agent._record_llm_call()
    assert agent._llm_calls_this_hour == before + 1

    # restore to avoid polluting other tests
    agent._llm_calls_this_hour = before


# ---------------------------------------------------------------------------
# Test 4 — _evaluate_ticker_candidate writes _cycle_composites
# ---------------------------------------------------------------------------

def test_evaluate_ticker_candidate_writes_cycle_composites():
    """
    _evaluate_ticker_candidate must write state._cycle_composites[ticker]
    with the composite value, and agent._cycle_composites must be the same
    dict as state._cycle_composites.
    """
    import backend.agent as agent
    import backend.runtime.state as state
    import backend.execution.entry as entry

    state._cycle_composites.clear()

    COMPOSITE = 0.38

    fake_regime_state = SimpleNamespace(
        intraday_regime="trending",
        market_regime="bull",
        vix=18.0,
    )
    signal_result_stub = {
        "composite_score": COMPOSITE,
        "signals": {
            "vwap_deviation": {"score": 0.2, "meta": {}},
            "tape_aggression": {"score": 0.3, "meta": {}},
        },
        "atr_data":   {"atr_pct": 0.8, "atr_raw": 1.2, "volatility_regime": "normal"},
        "rvol_data":  {"rvol_available": False},
        "computed_at": "2026-05-23T14:00:00Z",
        "mean_reversion_signal": False,
        "orb_score": 0.0,
    }
    setup_context_stub = {
        "playbook":                  "morning_vwap_reclaim",
        "playbook_lifecycle":        "tagged",
        "session_window":            "morning_trend",
        "primary_factor":            "semis",
        "factor_bucket":             "growth",
        "regime_key":                "bull_trending",
        "data_quality_state":        "executable",
        "data_quality":              {},
        "cost_estimate":             {},
        "estimated_spread_pct":      0.05,
        "estimated_total_cost_pct":  0.16,
        "candidate_rank_score":      0.55,
        "theme":                     "semis",
        "action":                    "BUY",
        "composite":                 COMPOSITE,
        "intraday_regime":           "trending",
        "minutes_since_open":        90,
        "strategy_family":           "trend_following",
    }
    ev_result_stub = {
        "decision":        "allow",
        "ev_decision":     "full_size",
        "size_multiplier": 1.0,
        "net_ev_pct":      0.12,
    }
    sizing_stub = {"size_eur": 300.0, "size_usd": 324.0, "stop_pct": 2.0, "atr_pct": 0.8}
    signal_row_stub = {"id": 1}

    with patch("backend.agent.detect_regime", return_value=fake_regime_state), \
         patch("backend.execution.entry.compute_all_signals", return_value=signal_result_stub), \
         patch("backend.execution.entry.compute_position_size", return_value=sizing_stub), \
         patch("backend.execution.entry.pre_trade_gate",
               return_value=(False, "signal below threshold")), \
         patch("backend.execution.entry.insert_signal", return_value=signal_row_stub), \
         patch("backend.execution.entry.log_event"), \
         patch("backend.execution.entry._trade_setup_context",
               return_value=setup_context_stub), \
         patch("backend.execution.entry._record_blocked_opportunity"), \
         patch("backend.execution.entry._is_new_intraday_entry_too_late",
               return_value=None), \
         patch("backend.execution.entry._time_exit_cooldown_active", return_value=None), \
         patch("backend.execution.entry._thesis_invalidated_cooldown_active",
               return_value=None), \
         patch("backend.execution.entry._ranging_stop_loss_cooldown_active",
               return_value=None), \
         patch("backend.execution.entry._ticker_loss_cooldown_active", return_value=None), \
         patch("backend.execution.entry._threshold_block_detail", return_value={}):

        result = entry._evaluate_ticker_candidate(
            ticker          = "AAPL",
            regime          = "trending",
            weights         = {},
            profile         = {
                "min_signal_score": 0.10,
                "max_trades_per_day": 20,
                "max_open_positions": 5,
                "max_drawdown_pct": 20.0,
                "max_cash_deploy_pct": 80.0,
                "min_cash_reserve_pct": 20.0,
                "allow_short_selling": False,
                "max_short_position_pct": 0.0,
                "min_short_signal_score": 0.5,
                "bull_short_signal_score": 0.6,
                "signal_consensus_min_count": 2,
                "signal_consensus_min_strength": 0.10,
                "stop_loss_pct": 2.0,
                "take_profit_pct": 4.0,
                "min_reward_risk_ratio": 1.5,
                "ranging_min_reward_risk_ratio": 2.0,
            },
            portfolio_state = {
                "equity":   5400.0,
                "cash":     3000.0,
                "vix":      18.0,
                "positions": [],
                "drawdown_today": 0.0,
                "trades_today": 0,
                "consecutive_losses": 0,
            },
            recent_trades   = [],
            regime_state    = fake_regime_state,
            shock_result    = None,
        )

    # Gate returned False → _evaluate_ticker_candidate returns None
    assert result is None, "Expected None when pre_trade_gate returns False"

    # But composite was written to _cycle_composites before the gate check
    assert "AAPL" in state._cycle_composites, \
        "state._cycle_composites['AAPL'] was never written"
    assert state._cycle_composites["AAPL"] == pytest.approx(COMPOSITE), \
        f"Expected {COMPOSITE}, got {state._cycle_composites['AAPL']}"

    # Bridge check: agent module references the same dict
    assert agent._cycle_composites is state._cycle_composites, \
        "agent._cycle_composites is not the same object as state._cycle_composites"

    # Cleanup
    state._cycle_composites.clear()
