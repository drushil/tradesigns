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
# Context quality decision
# ---------------------------------------------------------------------------

def test_context_quality_blocks_shadow_only_data():
    """Shadow-only evidence can be collected, but it must not execute live."""
    import backend.execution.entry as entry

    decision = entry._context_quality_decision(
        {"data_quality_state": "shadow_only", "session_window": "morning_trend"},
        {},
    )

    assert decision["allowed"] is False
    assert decision["multiplier"] == pytest.approx(0.0)
    assert decision["reason"] == "data_quality_shadow_only"


def test_context_quality_scales_midday_to_probe_size():
    """Midday is allowed, but only at reduced context size."""
    import backend.execution.entry as entry

    decision = entry._context_quality_decision(
        {"data_quality_state": "executable", "session_window": "midday"},
        {},
    )

    assert decision["allowed"] is True
    assert decision["multiplier"] == pytest.approx(0.35)
    assert decision["reason"] == "session_window_midday_multiplier"


def test_context_quality_blocks_opening_noise():
    """The first minutes after open remain shadow/evidence time, not live execution."""
    import backend.execution.entry as entry

    decision = entry._context_quality_decision(
        {"data_quality_state": "executable", "session_window": "opening_noise"},
        {},
    )

    assert decision["allowed"] is False
    assert decision["multiplier"] == pytest.approx(0.0)
    assert decision["reason"] == "session_window_opening_noise_blocked"


def test_horizon_order_blocks_shadow_only_before_price_fetch(monkeypatch):
    """Swing/dip horizon orders must not bypass the live data-quality floor."""
    import backend.execution.orders as orders

    monkeypatch.setattr(
        orders,
        "_current_daily_price",
        lambda ticker: pytest.fail("price fetch should not run after a shadow-only context block"),
    )
    logged = []
    monkeypatch.setattr(orders, "log_event", lambda level, event, data=None: logged.append((event, data or {})))

    result = orders._submit_horizon_order(
        ticker="AMD",
        side="BUY",
        conviction=0.70,
        profile={"stop_loss_pct": 2.0, "take_profit_pct": 3.0},
        portfolio_state={"equity": 5000.0},
        regime="ranging",
        horizon="swing",
        stop_loss_pct=2.0,
        hold_days=3,
        composite_score=0.198,
        signals_json={"swing_score": {"score": 0.198, "meta": {"rsi": 73}}},
        atr_data={},
        sizing_json={"size_eur": 750.0},
    )

    assert result["error"] == "data_quality_shadow_only"
    assert result["setup_context"]["data_quality_state"] == "shadow_only"
    assert logged[-1][0] == "horizon_context_quality_entry_block"


def test_horizon_context_quality_blocks_opening_noise():
    """Executable data is still blocked in the first noisy regular-session minutes."""
    import backend.execution.orders as orders

    decision = orders._horizon_context_quality_decision(
        {"data_quality_state": "executable", "session_window": "opening_noise"},
        {},
    )

    assert decision["allowed"] is False
    assert decision["reason"] == "session_window_opening_noise_blocked"


def test_advisory_do_not_chase_blocks_buy_above_latest_limit():
    """Automated entries should respect a recent advisory's do-not-chase ceiling."""
    import backend.execution.orders as orders

    block = orders._advisory_do_not_chase_block(
        "AMD",
        "BUY",
        504.88,
        {},
        recent_advisories=[
            {
                "id": 567,
                "symbol": "AMD",
                "data_symbol": "AMD",
                "side": "BUY",
                "grade": "A",
                "do_not_chase_price": 497.96,
                "signal_json": {"alert_stage": "watch"},
            }
        ],
    )

    assert block["reason"] == "advisory_do_not_chase"
    assert block["advisory_signal_id"] == 567
    assert block["current_price"] == pytest.approx(504.88)
    assert block["do_not_chase_price"] == pytest.approx(497.96)


def test_advisory_do_not_chase_ignores_expired_signal():
    """Yesterday or expired morning advisories must not block valid later entries."""
    import backend.execution.orders as orders

    block = orders._advisory_do_not_chase_block(
        "AMD",
        "BUY",
        504.88,
        {},
        recent_advisories=[
            {
                "id": 567,
                "symbol": "AMD",
                "data_symbol": "AMD",
                "side": "BUY",
                "grade": "A",
                "valid_until": "2000-01-01T10:35:00Z",
                "do_not_chase_price": 497.96,
                "signal_json": {"alert_stage": "watch"},
            }
        ],
    )

    assert block is None


def test_llm_block_records_reference_price_for_replay():
    """LLM vetoes should be replayable against the price known at signal time."""
    import backend.execution.entry as entry

    fake_regime_state = SimpleNamespace(
        intraday_regime="trending",
        market_regime="bull",
        vix=18.0,
    )
    setup_context = {
        "session_window": "morning_trend",
        "data_quality_state": "executable",
        "playbook": "morning_vwap_reclaim",
    }
    candidate = {
        "ticker": "AAPL",
        "ticker_regime": "trending",
        "ticker_regime_state": fake_regime_state,
        "signal_result": {
            "composite_score": 0.38,
            "signals": {"tape_aggression": {"score": 0.3}},
            "atr_data": {"atr_pct": 0.8, "atr_raw": 1.2, "current_price": 151.25},
            "current_price": 151.25,
        },
        "composite": 0.38,
        "signals_snap": {"tape_aggression": {"score": 0.3}},
        "atr_data": {"atr_pct": 0.8, "atr_raw": 1.2, "current_price": 151.25},
        "regime_debug": {},
        "news_headline": "",
        "setup_context": setup_context,
        "ev_result": {"decision": "allow", "ev_decision": "full_size", "size_multiplier": 1.0},
        "capital_base": 5000.0,
        "action_hint": "BUY",
        "setup_grade": None,
    }
    profile = {
        "min_conviction": 0.30,
        "stop_loss_pct": 2.0,
        "take_profit_pct": 4.0,
        "allow_a_plus_llm_hold_override": False,
    }
    llm_result = {
        "action": "HOLD",
        "conviction": 0.72,
        "hold_minutes": 30,
        "stop_loss_pct": 2.0,
        "rationale": "wait for cleaner confirmation",
    }

    with patch("backend.agent._can_call_llm", return_value=True), \
         patch("backend.agent._record_llm_call"), \
         patch("backend.execution.entry.llm_signal_decision", return_value=llm_result), \
         patch("backend.execution.entry.log_event"), \
         patch("backend.execution.entry._record_blocked_opportunity") as record_block:

        entry._execute_trade_candidate(candidate, profile, {"vix": 18.0})

    assert record_block.call_count == 1
    assert record_block.call_args.kwargs["reference_price"] == pytest.approx(151.25)
    assert record_block.call_args.args[6] == "llm"


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

def test_execute_trade_candidate_populates_open_trades(monkeypatch):
    """
    _execute_trade_candidate must write side, entry_price, and all Codex
    evidence fields into state._open_trades[ticker].
    """
    import backend.runtime.state as state
    import backend.execution.entry as entry

    state._open_trades.clear()
    monkeypatch.setenv("LLM_SHADOW_DECISION_ENABLED", "true")
    monkeypatch.setenv("GROQ_SHADOW_DECISION_MODEL", "llama-3.1-8b-instant")

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
        "signal_id":     77,
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
    primary_llm_result = {
        "action":       "BUY",
        "conviction":   0.72,
        "hold_minutes": 30,
        "stop_loss_pct": 2.0,
        "rationale":    "strong momentum",
        "model":        "llama-3.3-70b-versatile",
    }
    shadow_llm_result = {
        "action":       "HOLD",
        "conviction":   0.35,
        "hold_minutes": 0,
        "stop_loss_pct": 2.0,
        "rationale":    "8b wants more confirmation",
        "model":        "llama-3.1-8b-instant",
        "shadow":       True,
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
         patch("backend.execution.entry.llm_signal_decision",
               side_effect=[primary_llm_result, shadow_llm_result]), \
         patch("backend.execution.entry.submit_market_order", return_value=order_result), \
         patch("backend.execution.entry.save_open_trade", return_value={}), \
         patch("backend.execution.entry.update_signal", return_value={}) as update_signal, \
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
    assert trade["sizing_json"]["context_quality_multiplier"] == pytest.approx(1.0)
    assert trade["sizing_json"]["context_quality_reason"] == \
        "session_window_morning_trend_multiplier"
    assert trade["sizing_json"]["setup_context"]["llm_shadow"]["primary"]["action"] == "BUY"
    assert trade["sizing_json"]["setup_context"]["llm_shadow"]["shadow"]["action"] == "HOLD"
    update_signal.assert_called_once()
    assert update_signal.call_args.args[0] == 77
    assert update_signal.call_args.args[1]["llm_shadow_json"]["disagreement"]["action"] is True

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
