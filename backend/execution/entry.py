"""
backend/execution/entry.py
Per-ticker evaluation and trade execution pipeline.

Contains:
  - _evaluate_ticker_candidate  — signal → gate → EV → ranked candidate dict
  - _execute_trade_candidate    — LLM → order for an already-ranked candidate
  - _process_ticker             — compatibility wrapper (evaluate + execute in one call)

State access pattern: all mutable agent-level state (_open_trades, _signal_cache,
_cycle_composites, _learning_engine) is read/written via backend.runtime.state so that
mutations propagate to every importer of those containers.

Lazy imports from backend.agent are used for _can_call_llm, _record_llm_call, and
detect_regime to:
  (a) avoid circular imports at module load time, and
  (b) preserve monkeypatch compatibility in tests that patch agent.*
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Optional

import backend.runtime.state as state
from backend.runtime.env import (
    _env_float, _env_value, _eurusd_rate, _eur_to_usd,
)
from backend.execution.common import (
    _trading_capital, _deterministic_action, _cap_short_notional,
    _regime_debug_payload, _strategy_family,
)
from backend.execution.gates import (
    _alignment_veto, _signal_consensus_block, _reward_risk_block,
    _theme_open_exposure_block, _llm_rationale_mentions_conflict,
    _probe_floor_inflation_block, _late_chase_block, _rvol_block,
    _trade_setup_context, _record_blocked_opportunity,
    _threshold_block_detail,
)
from backend.execution.exit import (
    _apply_learned_hold_extension, _log_short_candidate,
    _time_exit_cooldown_active, _ticker_loss_cooldown_active,
    _thesis_invalidated_cooldown_active, _ranging_stop_loss_cooldown_active,
)
from backend.grading.engine import (
    SetupGrade, a_plus_hard_blocks, effective_size_multiplier,
)
from backend.market.sector import (
    _exposure_direction, _is_leveraged_etf, _leveraged_etf_stop_scalar,
)
from backend.market.timing import (
    _is_new_intraday_entry_too_late, _leveraged_etf_max_hold_window,
)
from backend.broker.alpaca import pre_trade_gate, compute_position_size, submit_market_order
from backend.learning.engine import compute_expected_value, llm_signal_decision
from backend.signals.engine import compute_all_signals
from database.client import insert_signal, save_open_trade, log_event


# ---------------------------------------------------------------------------
# Context quality sizing
# ---------------------------------------------------------------------------

def _context_quality_decision(setup_context: dict, profile: dict) -> dict:
    """Translate evidence-layer context into a live execution multiplier."""
    setup_context = setup_context or {}
    profile = profile or {}
    if not bool(profile.get("context_quality_enabled", True)):
        return {"allowed": True, "multiplier": 1.0, "reason": "context_quality_disabled"}

    data_quality = str(setup_context.get("data_quality_state") or "unknown").lower()
    if data_quality == "shadow_only" and bool(profile.get("context_quality_block_shadow_only", True)):
        return {
            "allowed": False,
            "multiplier": 0.0,
            "reason": "data_quality_shadow_only",
            "data_quality_state": data_quality,
        }

    window = str(setup_context.get("session_window") or "unknown").lower()
    multipliers = {
        "opening_noise": float(profile.get("context_quality_opening_noise_multiplier", 0.0)),
        "opening_drive": float(profile.get("context_quality_opening_drive_multiplier", 1.0)),
        "morning_trend": float(profile.get("context_quality_morning_trend_multiplier", 1.0)),
        "midday": float(profile.get("context_quality_midday_multiplier", 0.35)),
        "afternoon_momentum": float(profile.get("context_quality_afternoon_momentum_multiplier", 1.0)),
        "pre_close": float(profile.get("context_quality_pre_close_multiplier", 0.55)),
        "after_close": float(profile.get("context_quality_after_close_multiplier", 0.0)),
        "outside_regular_hours": float(profile.get("context_quality_outside_hours_multiplier", 0.0)),
        "unknown": float(profile.get("context_quality_unknown_multiplier", 0.50)),
    }
    multiplier = max(0.0, min(1.0, multipliers.get(window, multipliers["unknown"])))
    if multiplier <= 0:
        return {
            "allowed": False,
            "multiplier": 0.0,
            "reason": f"session_window_{window}_blocked",
            "session_window": window,
            "data_quality_state": data_quality,
        }
    return {
        "allowed": True,
        "multiplier": multiplier,
        "reason": f"session_window_{window}_multiplier",
        "session_window": window,
        "data_quality_state": data_quality,
    }


# ---------------------------------------------------------------------------
# Per-ticker evaluation
# ---------------------------------------------------------------------------

def _evaluate_ticker_candidate(ticker, regime, weights, profile, portfolio_state,
                                recent_trades, regime_state, shock_result):
    """Signal → gate → EV. Returns a ranked candidate dict if execution should be considered."""
    # Lazy import to break circular dep and preserve monkeypatch on agent.detect_regime
    from backend.agent import detect_regime

    ticker_regime_state = detect_regime(ticker)
    ticker_regime = ticker_regime_state.intraday_regime
    if state._learning_engine:
        weights = state._learning_engine.get_weights(ticker_regime)
    action_hint = None

    # 1. Compute signals (also warms the intra-cycle cache)
    signal_result = compute_all_signals(
        ticker, weights, regime_state=ticker_regime_state, shock_result=shock_result
    )
    state._signal_cache[ticker] = (datetime.now(timezone.utc), signal_result)
    composite     = signal_result["composite_score"]
    # Capture for sector confirmation (used after the full evaluation loop)
    state._cycle_composites[ticker] = composite
    signals_snap  = signal_result["signals"]
    atr_data       = signal_result.get("atr_data") or {}
    regime_debug   = _regime_debug_payload(ticker_regime_state, signal_result)
    news_headline = (signals_snap.get("news_sentiment", {})
                    .get("meta", {}).get("latest_headline", ""))

    # 2. Pre-trade gate (hard rules)
    capital_base = _trading_capital(portfolio_state["equity"])
    action_hint = _deterministic_action(composite)
    eod_entry_block = _is_new_intraday_entry_too_late(ticker)
    if eod_entry_block:
        setup_context = _trade_setup_context(
            ticker, action_hint, composite, signals_snap, signal_result,
            ticker_regime_state, gate_reason=eod_entry_block["reason"],
        )
        log_event("INFO", "eod_new_entry_block", {
            "ticker": ticker,
            "composite": round(composite, 4),
            **eod_entry_block,
        })
        _record_blocked_opportunity(
            ticker, action_hint, composite, signals_snap, setup_context,
            ticker_regime, "time", eod_entry_block["reason"],
        )
        return
    pre_size = compute_position_size(ticker, capital_base, profile, 0.7, atr_data, ticker_regime_state)
    size_eur = pre_size["size_eur"]
    if action_hint == "SELL":
        size_eur = _cap_short_notional(size_eur, capital_base, profile)
    cooldown = _time_exit_cooldown_active(ticker, recent_trades, profile)
    if cooldown:
        log_event("INFO", "time_exit_cooldown", cooldown)
        return
    thesis_cooldown = _thesis_invalidated_cooldown_active(ticker, action_hint, recent_trades, profile)
    if thesis_cooldown:
        log_event("INFO", "thesis_invalidated_cooldown", thesis_cooldown)
        return
    if str(ticker_regime or "").lower() == "ranging":
        stop_loss_cooldown = _ranging_stop_loss_cooldown_active(ticker, action_hint, recent_trades, profile)
        if stop_loss_cooldown:
            log_event("INFO", "ranging_stop_loss_cooldown", stop_loss_cooldown)
            return
    loss_cooldown = _ticker_loss_cooldown_active(ticker, action_hint, recent_trades, profile)
    if loss_cooldown and abs(composite) < float(loss_cooldown["min_reentry_score"]):
        loss_cooldown["composite"] = round(composite, 4)
        log_event("INFO", "ticker_loss_cooldown", loss_cooldown)
        return
    if str(ticker_regime or "").lower() == "ranging":
        ranging_cap = int(profile.get("ranging_max_trades_per_day", 6))
        if ranging_cap > 0 and int(portfolio_state.get("trades_today") or 0) >= ranging_cap:
            reason = f"ranging_regime_daily_trade_cap ({portfolio_state.get('trades_today')}/{ranging_cap})"
            log_event("INFO", "ranging_regime_trade_cap", {
                "ticker": ticker,
                "trades_today": portfolio_state.get("trades_today"),
                "ranging_max_trades_per_day": ranging_cap,
            })
            setup_context = _trade_setup_context(
                ticker, action_hint, composite, signals_snap, signal_result,
                ticker_regime_state, gate_reason=reason,
            )
            _record_blocked_opportunity(
                ticker, action_hint, composite, signals_snap, setup_context,
                ticker_regime, "regime", reason,
            )
            return
    gate_ok, gate_reason = pre_trade_gate(
        ticker, action_hint.lower(), size_eur, composite, profile, portfolio_state,
        market_regime=getattr(ticker_regime_state, "market_regime", None),
        signals=signals_snap,
    )

    # Leveraged ETF pre-entry gate (A+-only, VIX cap, no entries after 3:45 PM ET)
    if _is_leveraged_etf(ticker, profile):
        vix_now = float(portfolio_state.get("vix") or 20.0)
        lev_vix_ceiling = float(profile.get("leveraged_etf_vix_ceiling", 22))
        if vix_now >= lev_vix_ceiling:
            log_event("INFO", "leveraged_etf_vix_block", {
                "ticker": ticker, "vix": vix_now, "ceiling": lev_vix_ceiling,
            })
            return
        if _leveraged_etf_max_hold_window():
            log_event("INFO", "leveraged_etf_time_block", {
                "ticker": ticker, "reason": "past_3_45pm_et_no_new_entries",
            })
            return
        # Leveraged ETFs are A+-only entries
        # setup_grade not yet computed here — defer to the grading pass via ev_blocked flag
        # but we mark the candidate so the grading pass can enforce the A+ requirement
        gate_reason = gate_reason or {}
        if isinstance(gate_reason, dict):
            gate_reason["leveraged_etf"] = True

    setup_context = _trade_setup_context(
        ticker, action_hint, composite, signals_snap, signal_result,
        ticker_regime_state, gate_reason=gate_reason,
    )
    strategy_family_hint = setup_context["strategy_family"]
    alignment_veto = _alignment_veto(ticker, action_hint, signals_snap, profile)
    if alignment_veto:
        reason = alignment_veto["reason"]
        log_event("INFO", "signal_alignment_veto", {
            "ticker": ticker,
            "action": action_hint,
            "composite": round(composite, 4),
            **alignment_veto,
        })
        _record_blocked_opportunity(
            ticker, action_hint, composite, signals_snap, setup_context,
            ticker_regime, "signal_alignment", reason,
        )
        return
    consensus_block = _signal_consensus_block(action_hint, signals_snap, ticker_regime, profile)
    if consensus_block:
        reason = consensus_block["reason"]
        log_event("INFO", "signal_consensus_veto", {
            "ticker": ticker,
            "action": action_hint,
            "composite": round(composite, 4),
            "regime": ticker_regime,
            **consensus_block,
        })
        _record_blocked_opportunity(
            ticker, action_hint, composite, signals_snap, setup_context,
            ticker_regime, "signal_consensus", reason,
        )
        return
    theme_exposure_block = _theme_open_exposure_block(ticker, profile)
    if theme_exposure_block:
        reason = theme_exposure_block["reason"]
        log_event("INFO", "theme_open_exposure_cap", {
            "ticker": ticker,
            "action": action_hint,
            "composite": round(composite, 4),
            **theme_exposure_block,
        })
        _record_blocked_opportunity(
            ticker, action_hint, composite, signals_snap, setup_context,
            ticker_regime, "exposure", reason,
        )
        return

    late_chase_block = _late_chase_block(action_hint, signals_snap, atr_data, profile)
    if late_chase_block:
        log_event("INFO", "late_chase_block", {
            "ticker": ticker,
            "action": action_hint,
            "composite": round(composite, 4),
            **late_chase_block,
        })
        _record_blocked_opportunity(
            ticker, action_hint, composite, signals_snap, setup_context,
            ticker_regime, "entry_quality", late_chase_block["reason"],
            reference_price=signal_result.get("current_price"),
            block_detail=late_chase_block,
        )
        return

    rvol_block = _rvol_block(signal_result, profile)
    if rvol_block:
        log_event("INFO", "rvol_gate_block", {
            "ticker": ticker,
            "action": action_hint,
            "composite": round(composite, 4),
            **rvol_block,
        })
        _record_blocked_opportunity(
            ticker, action_hint, composite, signals_snap, setup_context,
            ticker_regime, "entry_quality", rvol_block["reason"],
            reference_price=signal_result.get("current_price"),
            block_detail=rvol_block,
        )
        return

    # 3. Log signal to DB. Grade metadata is updated after the full cycle
    # because sector confirmation depends on all tickers' composites.
    signal_row = insert_signal({
        "ticker":                 ticker,
        "composite_score":        composite,
        "order_book_score":       signals_snap.get("order_book_imbalance", {}).get("score", 0),
        "tape_aggression_score":  signals_snap.get("tape_aggression", {}).get("score", 0),
        "rsi_divergence_score":   signals_snap.get("rsi_divergence", {}).get("score", 0),
        "news_sentiment_score":   signals_snap.get("news_sentiment", {}).get("score", 0),
        "vwap_deviation_score":   signals_snap.get("vwap_deviation", {}).get("score", 0),
        "macd_score":             signals_snap.get("macd_crossover", {}).get("score", 0),
        "rel_strength_score":     signals_snap.get("relative_strength", {}).get("score", 0),
        "bollinger_score":        signals_snap.get("bollinger_squeeze", {}).get("score", 0),
        "put_call_score":         signals_snap.get("put_call_ratio", {}).get("score", 0),
        "atr_pct":                atr_data.get("atr_pct"),
        "atr_stop_pct":           atr_data.get("suggested_stop_pct"),
        "volatility_regime":      atr_data.get("volatility_regime"),
        "earnings_days":          signals_snap.get("earnings_proximity", {}).get("meta", {}).get("days_to_earnings"),
        "earnings_mult":          signals_snap.get("earnings_proximity", {}).get("meta", {}).get("earnings_multiplier", 1.0),
        "macro_regime":           signal_result.get("macro_regime"),
        "macro_multiplier":       signal_result.get("macro_multiplier", 1.0),
        "market_regime":          getattr(ticker_regime_state, "market_regime", None),
        "regime_bull_bear":       signal_result.get("regime_bull_bear"),
        "shock_detected":         signal_result.get("shock_detected", False),
        "shock_classification":   signal_result.get("shock_classification"),
        "yield_curve":           getattr(ticker_regime_state, "yield_curve", None),
        "yield_curve_state":     getattr(ticker_regime_state, "yield_curve_state", None),
        "regime":                 ticker_regime,
        "action_hint":            action_hint,
        "exposure_direction":     _exposure_direction(ticker, action_hint),
        "strategy_family":        strategy_family_hint,
        "playbook":               setup_context.get("playbook"),
        "playbook_lifecycle":     setup_context.get("playbook_lifecycle"),
        "session_window":         setup_context.get("session_window"),
        "primary_factor":         setup_context.get("primary_factor"),
        "factor_bucket":          setup_context.get("factor_bucket"),
        "regime_key":             setup_context.get("regime_key"),
        "data_quality_state":     setup_context.get("data_quality_state"),
        "data_quality_json":      setup_context.get("data_quality") or {},
        "cost_estimate_json":     setup_context.get("cost_estimate") or {},
        "estimated_spread_pct":   setup_context.get("estimated_spread_pct"),
        "estimated_total_cost_pct": setup_context.get("estimated_total_cost_pct"),
        "regime_debug_json":      regime_debug,
        "vix":                    portfolio_state["vix"],
        "gated":                  not gate_ok,
        "gate_reason":            gate_reason if not gate_ok else None,
        "llm_called":             False,
        "orb_score":              signal_result.get("orb_score", 0.0),
    })
    signal_id = signal_row.get("id") if isinstance(signal_row, dict) else None

    if not gate_ok:
        log_event("INFO", "trade_gated", {
            "ticker": ticker,
            "composite": composite,
            "reason": gate_reason,
        })
        if action_hint == "SELL":
            _log_short_candidate(
                "short_candidate_gated", ticker, composite, gate_reason,
                profile, ticker_regime_state,
            )
        _record_blocked_opportunity(
            ticker, action_hint, composite, signals_snap, setup_context,
            ticker_regime, "gate", gate_reason,
            block_detail=(
                _threshold_block_detail(
                    action_hint, composite, profile,
                    market_regime=getattr(ticker_regime_state, "market_regime", None),
                )
                if "signal below threshold" in str(gate_reason)
                else {}
            ),
        )
        return

    # 4. EV check
    ev_result = compute_expected_value(
        composite, size_eur, recent_trades, ticker_regime,
        setup_context=setup_context,
        profile=profile,
    )
    ev_blocked = ev_result["decision"] == "block"
    if ev_blocked:
        # Don't hard-block here — carry forward to grading pass.
        # A+/A grades will override with probe size; B/C will be dropped there.
        log_event("INFO", "ev_blocked_pending_grade", {"ticker": ticker, **ev_result})
    if ev_result.get("ev_decision") not in {None, "full_size", "exploration_full_size"}:
        log_event("INFO", "ev_sizing_adjusted", {
            "ticker": ticker,
            "composite": composite,
            "ev_decision": ev_result.get("ev_decision"),
            "size_multiplier": ev_result.get("size_multiplier"),
            "breakout_quality": ev_result.get("breakout_quality"),
            "reason": ev_result.get("reason"),
            "setup": setup_context,
        })

    return {
        "ticker": ticker,
        "ticker_regime": ticker_regime,
        "ticker_regime_state": ticker_regime_state,
        "signal_result": signal_result,
        "composite": composite,
        "signals_snap": signals_snap,
        "atr_data": atr_data,
        "regime_debug": regime_debug,
        "news_headline": news_headline,
        "setup_context": setup_context,
        "ev_result": ev_result,
        "ev_blocked": ev_blocked,
        "capital_base": capital_base,
        "action_hint": action_hint,
        "orb_score": signal_result.get("orb_score", 0.0),
        "signal_id": signal_id,
    }


# ---------------------------------------------------------------------------
# Trade execution
# ---------------------------------------------------------------------------

def _execute_trade_candidate(candidate: dict, profile: dict, portfolio_state: dict):
    """LLM → order for an already-ranked candidate."""
    # Lazy imports to break circular dep and preserve monkeypatch on agent.*
    from backend.agent import _can_call_llm, _record_llm_call

    ticker = candidate["ticker"]
    ticker_regime = candidate["ticker_regime"]
    ticker_regime_state = candidate["ticker_regime_state"]
    signal_result = candidate["signal_result"]
    composite = candidate["composite"]
    signals_snap = candidate["signals_snap"]
    atr_data = candidate["atr_data"]
    regime_debug = candidate["regime_debug"]
    news_headline = candidate["news_headline"]
    setup_context = candidate["setup_context"]
    ev_result = candidate["ev_result"]
    capital_base = candidate["capital_base"]
    context_quality = _context_quality_decision(setup_context, profile)
    if not context_quality.get("allowed", True):
        log_event("INFO", "context_quality_entry_block", {
            "ticker": ticker,
            "action": candidate.get("action_hint"),
            "composite": round(float(composite or 0), 4),
            **context_quality,
        })
        _record_blocked_opportunity(
            ticker, candidate.get("action_hint"), composite, signals_snap, setup_context,
            ticker_regime, "entry_quality", context_quality.get("reason", "context_quality_block"),
            ev_result=ev_result,
            block_detail=context_quality,
        )
        return

    # ── A+ setup grade — override soft LLM blocks ────────────────────────────
    setup_grade: Optional[SetupGrade] = candidate.get("setup_grade")
    is_a_plus = setup_grade is not None and setup_grade.grade == "A+"
    hard_blocks = a_plus_hard_blocks()
    action = _deterministic_action(composite)

    # 5. LLM decision (gated by hourly limit)
    llm_result = None
    if not _can_call_llm():
        if is_a_plus:
            # A+ escalation: skip LLM, proceed with probe size
            log_event("INFO", "a_plus_llm_limit_override", {
                "ticker": ticker, "composite": composite,
                "grade": setup_grade.grade, "action": action,
            })
            llm_result = {
                "action": action,
                "conviction": max(abs(composite), 0.55),
                "hold_minutes": int(profile.get("max_hold_minutes", 45)),
                "stop_loss_pct": float(profile.get("stop_loss_pct", 2.0)),
                "rationale": "a_plus_llm_limit_override",
            }
            ev_result = ev_result.copy()
            ev_result["size_multiplier"] = min(ev_result.get("size_multiplier", 1.0), 0.35)
            ev_result["ev_decision"] = "a_plus_probe"
        else:
            log_event("WARN", "llm_limit_hit", {"ticker": ticker})
            _record_blocked_opportunity(
                ticker, candidate.get("action_hint"), composite, signals_snap, setup_context,
                ticker_regime, "llm", "llm_limit_hit", ev_result=ev_result,
            )
            return

    if llm_result is None:
        llm_result = llm_signal_decision(
            ticker, composite, ticker_regime, news_headline, profile,
            signal_scores   = signals_snap,
            atr_data        = atr_data,
            regime_context  = {
                "market_regime": getattr(ticker_regime_state, "market_regime", ""),
                "vix":           portfolio_state.get("vix", ""),
            },
        )
        _record_llm_call()
    suggested_action = str(llm_result.get("action", "HOLD")).upper()
    action = _deterministic_action(composite)
    raw_llm_conviction = llm_result.get("conviction", 0)
    llm_conviction = raw_llm_conviction if isinstance(raw_llm_conviction, (int, float)) else 0
    conviction = max(abs(composite), float(llm_conviction or 0))
    log_event("SIGNAL", "llm_decision", {
        "ticker": ticker,
        "composite": composite,
        "deterministic_action": action,
        "llm_action": suggested_action,
        "conviction": conviction,
        "rationale": llm_result.get("rationale", ""),
    })

    if suggested_action == "HOLD":
        allow_hold_override = bool(profile.get("allow_a_plus_llm_hold_override", False))
        if is_a_plus and allow_hold_override and not _llm_rationale_mentions_conflict(llm_result):
            # A+ escalation: override LLM HOLD — deterministic action with probe size
            log_event("INFO", "a_plus_llm_hold_override", {
                "ticker": ticker, "composite": composite,
                "grade": setup_grade.grade, "llm_rationale": llm_result.get("rationale", ""),
            })
            suggested_action = action
            # Force probe size — don't go full size on an LLM-overridden entry
            ev_result = ev_result.copy()
            ev_result["size_multiplier"] = min(ev_result.get("size_multiplier", 1.0), 0.35)
            ev_result["ev_decision"] = "a_plus_probe"
        else:
            log_event("INFO", "llm_hold_veto", {
                "ticker": ticker,
                "composite": composite,
                "rationale": llm_result.get("rationale", ""),
                "grade": setup_grade.grade if setup_grade else None,
                "a_plus_hold_override_enabled": allow_hold_override,
            })
            _record_blocked_opportunity(
                ticker, action, composite, signals_snap, setup_context,
                ticker_regime, "llm",
                llm_result.get("rationale", "llm_hold_veto"),
                ev_result=ev_result,
            )
            if action == "SELL":
                _log_short_candidate(
                    "short_candidate_llm_hold", ticker, composite,
                    llm_result.get("rationale", "llm_hold"), profile, ticker_regime_state,
                    {"llm_conviction": llm_conviction},
                )
            return
    if suggested_action in {"BUY", "SELL"} and suggested_action != action:
        log_event("INFO", "llm_direction_conflict", {
            "ticker": ticker,
            "composite": composite,
            "deterministic_action": action,
            "llm_action": suggested_action,
            "rationale": llm_result.get("rationale", ""),
        })
        _record_blocked_opportunity(
            ticker, action, composite, signals_snap, setup_context,
            ticker_regime, "llm",
            llm_result.get("rationale", "llm_direction_conflict"),
            ev_result=ev_result,
        )
        if action == "SELL":
            _log_short_candidate(
                "short_candidate_llm_conflict", ticker, composite,
                llm_result.get("rationale", "llm_direction_conflict"),
                profile, ticker_regime_state,
                {"llm_action": suggested_action, "llm_conviction": llm_conviction},
            )
        return
    if suggested_action in {"BUY", "SELL"} and _llm_rationale_mentions_conflict(llm_result):
        log_event("INFO", "llm_rationale_conflict_veto", {
            "ticker": ticker,
            "composite": composite,
            "llm_action": suggested_action,
            "rationale": llm_result.get("rationale", ""),
        })
        _record_blocked_opportunity(
            ticker, action, composite, signals_snap, setup_context,
            ticker_regime, "llm",
            llm_result.get("rationale", "llm_rationale_conflict_veto"),
            ev_result=ev_result,
        )
        return

    if conviction < profile["min_conviction"]:
        log_event("INFO", "conviction_below_threshold", {
            "ticker": ticker,
            "conviction": conviction,
            "min_conviction": profile["min_conviction"],
        })
        _record_blocked_opportunity(
            ticker, action, composite, signals_snap, setup_context,
            ticker_regime, "conviction", "conviction_below_threshold",
            ev_result=ev_result,
        )
        if action == "SELL":
            _log_short_candidate(
                "short_candidate_conviction_blocked", ticker, composite,
                "conviction_below_threshold", profile, ticker_regime_state,
                {"conviction": conviction, "min_conviction": profile["min_conviction"]},
            )
        return

    # 6. Size and submit order
    sizing = compute_position_size(ticker, capital_base, profile, conviction, atr_data, ticker_regime_state)
    base_stop_pct = float(sizing.get("stop_pct") or profile.get("stop_loss_pct", 2.0))

    # Ranging regime: widen the stop to an ATR-based floor so intraday noise doesn't
    # prematurely hit the bracket before time-exit drift can develop.
    # EUR risk is preserved by proportionally shrinking position size (same notional risk,
    # more price room).  Profile key: ranging_atr_stop_multiple (default 1.5).
    if str(getattr(ticker_regime_state, "intraday_regime", "")).lower() == "ranging":
        _raw_atr = (atr_data or {}).get("atr_pct")
        _atr_stop_mult = float(profile.get("ranging_atr_stop_multiple", 1.5))
        if _raw_atr and _atr_stop_mult > 0:
            _atr_floor_stop = round(float(_raw_atr) * _atr_stop_mult, 3)
            if _atr_floor_stop > base_stop_pct:
                _risk_scale = base_stop_pct / _atr_floor_stop
                sizing["size_eur"] = round(float(sizing["size_eur"]) * _risk_scale, 2)
                sizing["stop_pct"] = round(_atr_floor_stop, 3)
                sizing["ranging_atr_stop_widened"] = True
                sizing["ranging_atr_stop_detail"] = {
                    "original_stop_pct": round(base_stop_pct, 3),
                    "atr_pct":           round(float(_raw_atr), 3),
                    "atr_stop_multiple": _atr_stop_mult,
                    "widened_stop_pct":  round(_atr_floor_stop, 3),
                    "size_scale":        round(_risk_scale, 4),
                }
                base_stop_pct = _atr_floor_stop
                log_event("INFO", "ranging_atr_stop_widened", {
                    "ticker": ticker,
                    **sizing["ranging_atr_stop_detail"],
                })

    stop_scalar = _leveraged_etf_stop_scalar(ticker, profile)
    if stop_scalar > 1.0:
        adjusted_stop_pct = min(12.0, max(base_stop_pct, base_stop_pct * stop_scalar))
        if adjusted_stop_pct > base_stop_pct:
            risk_scale = base_stop_pct / adjusted_stop_pct
            sizing["size_eur"] = round(float(sizing["size_eur"]) * risk_scale, 2)
            sizing["stop_pct"] = round(adjusted_stop_pct, 3)
            sizing["volatility_stop_adjustment"] = {
                "reason": "leveraged_etf_stop_scalar",
                "base_stop_pct": round(base_stop_pct, 3),
                "adjusted_stop_pct": round(adjusted_stop_pct, 3),
                "risk_scale": round(risk_scale, 4),
                "stop_scalar": round(stop_scalar, 3),
            }
            log_event("INFO", "volatility_stop_adjusted", {
                "ticker": ticker,
                **sizing["volatility_stop_adjustment"],
            })
    final_size = sizing["size_eur"]
    ev_size_multiplier = float(ev_result.get("size_multiplier") or 1.0)
    # Apply grade multiplier on top of EV multiplier (capped at 2.0× to prevent runaway sizing)
    if setup_grade is not None and setup_grade.size_multiplier > 0:
        combined_mult = effective_size_multiplier(setup_grade, ev_size_multiplier)
    else:
        combined_mult = ev_size_multiplier
    if setup_grade is not None and setup_grade.grade == "A+":
        atr_pct_for_quality = float(sizing.get("atr_pct") or 0)
        stop_pct_for_quality = float(sizing.get("stop_pct") or 0)
        max_atr = float(profile.get("a_plus_full_size_max_atr_pct", 2.5))
        max_stop = float(profile.get("a_plus_full_size_max_stop_pct", 5.0))
        if atr_pct_for_quality > max_atr or stop_pct_for_quality > max_stop:
            combined_mult = min(combined_mult, ev_size_multiplier)
            sizing["a_plus_size_capped"] = True
            sizing["a_plus_size_cap_reason"] = {
                "atr_pct": round(atr_pct_for_quality, 3),
                "max_atr_pct": max_atr,
                "stop_pct": round(stop_pct_for_quality, 3),
                "max_stop_pct": max_stop,
            }
            log_event("INFO", "a_plus_size_capped_by_volatility", {
                "ticker": ticker,
                **sizing["a_plus_size_cap_reason"],
            })
        # A+ grade boost (1.5×) is not warranted in ranging — the extra confirmation
        # signals don't overcome ranging noise.  Cap to EV-only sizing, identical to A.
        if str(getattr(ticker_regime_state, "intraday_regime", "")).lower() == "ranging" \
                and combined_mult > ev_size_multiplier:
            sizing["a_plus_ranging_size_capped"] = True
            log_event("INFO", "a_plus_ranging_grade_boost_suppressed", {
                "ticker":               ticker,
                "grade":                "A+",
                "uncapped_combined_mult": round(combined_mult, 3),
                "capped_to_ev_mult":    round(ev_size_multiplier, 3),
                "reason":               "grade_boost_not_applicable_in_ranging",
            })
            combined_mult = ev_size_multiplier
    if str(getattr(ticker_regime_state, "intraday_regime", "")).lower() == "ranging":
        ranging_scalar = max(0.0, min(1.0, float(profile.get("ranging_regime_size_multiplier", 0.35))))
        combined_mult *= ranging_scalar
        ranging_cap = float(profile.get("ranging_max_notional_eur", 0) or 0)
        if ranging_cap > 0:
            sizing["ranging_max_notional_eur"] = round(ranging_cap, 2)
        sizing["ranging_regime_size_scalar"] = round(ranging_scalar, 3)
        log_event("INFO", "ranging_regime_size_reduced", {
            "ticker": ticker,
            "scalar": round(ranging_scalar, 3),
            "combined_size_multiplier": round(combined_mult, 3),
        })
    context_multiplier = float(context_quality.get("multiplier", 1.0))
    combined_mult *= context_multiplier
    sizing["context_quality_multiplier"] = round(context_multiplier, 3)
    sizing["context_quality_reason"] = context_quality.get("reason")
    sizing["context_quality_detail"] = context_quality
    if context_multiplier < 1.0:
        log_event("INFO", "context_quality_size_reduced", {
            "ticker": ticker,
            "multiplier": round(context_multiplier, 3),
            "combined_size_multiplier": round(combined_mult, 3),
            **context_quality,
        })
    final_size *= combined_mult
    if action == "SELL":
        final_size = _cap_short_notional(final_size, capital_base, profile)
    max_notional = _env_float(
        "MAX_NOTIONAL_PER_TRADE_EUR",
        profile.get("max_trade_notional_eur", final_size),
    )
    if str(getattr(ticker_regime_state, "intraday_regime", "")).lower() == "ranging":
        ranging_cap = _env_float("RANGING_MAX_NOTIONAL_EUR", float(profile.get("ranging_max_notional_eur", max_notional) or max_notional))
        if ranging_cap > 0:
            max_notional = min(max_notional, ranging_cap)
    final_size = min(final_size, max_notional)
    sizing["size_eur"] = round(final_size, 2)
    sizing["ev_decision"] = ev_result.get("ev_decision")
    sizing["ev_size_multiplier"] = round(ev_size_multiplier, 3)
    sizing["grade_size_multiplier"] = round(setup_grade.size_multiplier, 3) if setup_grade else 1.0
    sizing["combined_size_multiplier"] = round(combined_mult, 3)
    sizing["ev_result"] = ev_result
    sizing["setup_context"] = setup_context

    import yfinance as yf
    bar = yf.download(ticker, period="1d", interval="1m",
                      progress=False, auto_adjust=True)
    if bar.empty:
        log_event("WARN", "price_unavailable", {"ticker": ticker})
        _record_blocked_opportunity(
            ticker, action, composite, signals_snap, setup_context,
            ticker_regime, "price", "price_unavailable",
            ev_result=ev_result,
        )
        return
    current_price = float(bar["Close"].squeeze().iloc[-1])
    intended_size_eur = float(final_size or 0)
    use_bracket_orders = _env_value("USE_BRACKET_ORDERS", "true").lower() != "false"

    if setup_grade is not None and setup_grade.grade in {"A+", "A"} and current_price > 0:
        min_grade_shares = _env_float("GRADE_MIN_EXECUTABLE_SHARES", 2.0)
        min_buffer = max(0.0, _env_float("GRADE_MIN_NOTIONAL_BUFFER_PCT", 0.5)) / 100
        min_notional_eur = (current_price * min_grade_shares * (1 + min_buffer)) / _eurusd_rate()
        if final_size < min_notional_eur:
            capped_min_size = min(min_notional_eur, max_notional)
            if capped_min_size > final_size:
                final_size = capped_min_size
                sizing["grade_min_executable_shares"] = min_grade_shares
                sizing["grade_min_notional_eur"] = round(min_notional_eur, 2)
                sizing["grade_min_notional_buffer_pct"] = round(min_buffer * 100, 3)
                sizing["grade_min_notional_applied"] = True
                log_event("INFO", "grade_min_notional_applied", {
                    "ticker": ticker,
                    "grade": setup_grade.grade,
                    "previous_size_eur": round(intended_size_eur, 2),
                    "new_size_eur": round(final_size, 2),
                    "min_shares": min_grade_shares,
                    "buffer_pct": round(min_buffer * 100, 3),
                    "current_price": round(current_price, 4),
                })
            else:
                sizing["grade_min_notional_applied"] = False
                sizing["grade_min_notional_capped"] = True
                sizing["grade_min_notional_eur"] = round(min_notional_eur, 2)
                sizing["grade_min_notional_buffer_pct"] = round(min_buffer * 100, 3)

    inflation_block = _probe_floor_inflation_block(
        ev_result.get("ev_decision"),
        bool(sizing.get("grade_min_notional_applied")),
        intended_size_eur,
        final_size,
        profile,
    )
    if inflation_block:
        reason = "probe_floor_inflation_block"
        log_event("INFO", reason, {
            "ticker": ticker,
            "ev_decision": ev_result.get("ev_decision"),
            "intended_size_eur": round(intended_size_eur, 2),
            "floor_size_eur": round(final_size, 2),
            **inflation_block,
        })
        _record_blocked_opportunity(
            ticker, action, composite, signals_snap, setup_context,
            ticker_regime, "sizing", reason,
            ev_result=ev_result, reference_price=current_price,
        )
        return

    final_size_usd = _eur_to_usd(final_size)
    sizing["size_eur"] = round(final_size, 2)
    sizing["size_usd"] = round(final_size_usd, 2)
    qty = final_size_usd / current_price
    floor_qty = math.floor(qty) if use_bracket_orders else round(qty, 6)
    bracket_floor_qty_loss_pct = (
        round(max(0.0, (qty - floor_qty) / qty * 100), 4)
        if use_bracket_orders and qty > 0 else 0.0
    )
    sizing["intended_size_eur"] = round(intended_size_eur, 2)
    sizing["implied_qty"] = round(qty, 6)
    sizing["floor_qty"] = floor_qty
    sizing["bracket_floor_qty_loss_pct"] = bracket_floor_qty_loss_pct

    if use_bracket_orders and floor_qty < 1:
        reason = "bracket_floor_would_waste_trade"
        log_event("INFO", "bracket_floor_preflight_block", {
            "ticker": ticker,
            "grade": setup_grade.grade if setup_grade else None,
            "final_size_eur": round(final_size, 2),
            "size_usd": round(final_size_usd, 2),
            "current_price": round(current_price, 4),
            "implied_qty": round(qty, 6),
            "floor_qty": floor_qty,
            "ev_decision": ev_result.get("ev_decision"),
        })
        _record_blocked_opportunity(
            ticker, action, composite, signals_snap, setup_context,
            ticker_regime, "sizing", reason,
            ev_result=ev_result, reference_price=current_price,
        )
        return

    raw_hold_minutes = llm_result.get("hold_minutes", 30)
    try:
        hold_minutes = int(raw_hold_minutes)
    except (TypeError, ValueError):
        hold_minutes = 30
    mean_reversion_trade = bool(signal_result.get("mean_reversion_signal"))
    event_risk_probe = bool(setup_context.get("event_risk_intraday_probe"))
    hold_extension = None
    if mean_reversion_trade:
        hold_minutes = 2880
    else:
        hold_minutes = max(
            int(profile.get("min_hold_minutes", 1)),
            min(int(profile.get("max_hold_minutes", 60)), hold_minutes),
        )
        if event_risk_probe:
            hold_minutes = min(
                hold_minutes,
                int(profile.get("event_risk_max_hold_minutes", 30)),
            )
        else:
            hold_minutes, hold_extension = _apply_learned_hold_extension(
                ticker=ticker,
                hold_minutes=hold_minutes,
                conviction=conviction,
                composite=composite,
                profile=profile,
                portfolio_state=portfolio_state,
            )
            if hold_extension:
                log_event("INFO", "learned_hold_extended", hold_extension)

    stop_loss_pct = sizing.get("stop_pct") or float(llm_result.get("stop_loss_pct", profile["stop_loss_pct"]))
    if mean_reversion_trade:
        raw_atr_pct = atr_data.get("atr_pct")
        if raw_atr_pct:
            stop_loss_pct = max(stop_loss_pct, round(float(raw_atr_pct) * 2.0, 3))
    if event_risk_probe:
        stop_loss_pct = max(
            float(profile.get("event_risk_min_stop_pct", 0.25)),
            stop_loss_pct * float(profile.get("event_risk_stop_multiplier", 0.75)),
        )
        sizing["event_risk_intraday_only"] = True

    profile_tp_pct = float(profile.get("take_profit_pct", profile["stop_loss_pct"] * 1.2))
    # Use the same R:R threshold that _reward_risk_block will apply so TP is always
    # pre-scaled to satisfy the veto — not just the non-ranging 1.5× default.
    _tp_is_ranging = str(getattr(ticker_regime_state, "intraday_regime", "")).lower() == "ranging"
    min_rr = float(profile.get(
        "ranging_min_reward_risk_ratio" if _tp_is_ranging else "min_reward_risk_ratio",
        2.0 if _tp_is_ranging else 1.5,
    ))
    dynamic_tp_pct = round(stop_loss_pct * min_rr, 4)
    take_profit_pct = max(profile_tp_pct, dynamic_tp_pct)
    if take_profit_pct > profile_tp_pct:
        # Stop (ATR-derived or profile) exceeded the profile TP default — scale up TP
        # to satisfy the ranging/non-ranging R:R threshold applied by _reward_risk_block.
        log_event("INFO", "take_profit_dynamic_enforcement", {
            "ticker":         ticker,
            "stop_loss_pct":  round(stop_loss_pct, 4),
            "profile_tp_pct": round(profile_tp_pct, 4),
            "dynamic_tp_pct": round(dynamic_tp_pct, 4),
            "applied_tp_pct": round(take_profit_pct, 4),
            "min_rr":         min_rr,
            "is_ranging":     _tp_is_ranging,
        })
    rr_block = _reward_risk_block(stop_loss_pct, take_profit_pct, ticker_regime, profile)
    if rr_block:
        log_event("INFO", "reward_risk_veto", {
            "ticker": ticker,
            "action": action,
            "composite": round(composite, 4),
            "regime": ticker_regime,
            **rr_block,
        })
        _record_blocked_opportunity(
            ticker, action, composite, signals_snap, setup_context,
            ticker_regime, "reward_risk", rr_block["reason"],
            ev_result=ev_result, reference_price=current_price,
        )
        return
    sizing["take_profit_pct"] = round(take_profit_pct, 4)
    sizing["reward_risk_ratio"] = round(take_profit_pct / max(float(stop_loss_pct or 0), 0.0001), 4)

    order = submit_market_order(
        ticker          = ticker,
        side            = action.lower(),
        qty             = round(qty, 6),
        stop_loss_pct   = stop_loss_pct,
        take_profit_pct = take_profit_pct,
        current_price   = current_price,
        signal_id       = candidate.get("signal_id"),
    )

    if "error" in order:
        log_event("ERROR", "order_failed", {"ticker": ticker, "error": order["error"],
                                             "client_order_id": order.get("client_order_id")})
        return

    submitted_qty = float(order.get("qty") or floor_qty or round(qty, 6))
    executed_size_usd = submitted_qty * current_price
    executed_size_eur = executed_size_usd / _eurusd_rate()
    sizing["submitted_qty"] = round(submitted_qty, 6)
    sizing["executed_size_usd"] = round(executed_size_usd, 2)
    sizing["executed_size_eur"] = round(executed_size_eur, 2)

    # Track open trade for exit monitoring
    if action == "BUY":
        stop_price = current_price * (1 - stop_loss_pct / 100)
        take_profit_price = current_price * (1 + take_profit_pct / 100)
    else:
        stop_price = current_price * (1 + stop_loss_pct / 100)
        take_profit_price = current_price * (1 - take_profit_pct / 100)

    exposure_direction = _exposure_direction(ticker, action)
    strategy_family = _strategy_family(
        ticker, action, ticker_regime, signal_result,
        horizon="short", mean_reversion_trade=mean_reversion_trade,
    )

    # Compute grade-differentiated partial exit target and runner ATR stop
    atr_raw = float(atr_data.get("atr_raw") or (current_price * float(atr_data.get("atr_pct", 2.5)) / 100))
    if setup_grade is not None and setup_grade.grade in {"A+", "A", "B"}:
        partial_atr_mult = 1.5 if setup_grade.grade == "A+" else 1.2
        if action == "BUY":
            partial_target_price = current_price + atr_raw * partial_atr_mult
        else:
            partial_target_price = current_price - atr_raw * partial_atr_mult
        partial_exit_pct = setup_grade.partial_exit_pct
        runner_atr_mult = setup_grade.runner_atr_multiplier
    else:
        partial_target_price = None
        partial_exit_pct = 0.5
        runner_atr_mult = 0.8

    state._open_trades[ticker] = {
        "entry_time":    datetime.utcnow(),
        "entry_price":   current_price,
        "quantity":      submitted_qty,
        "submitted_qty": submitted_qty,
        "implied_qty":   round(qty, 6),
        "stop_price":    stop_price,
        "take_profit_price": take_profit_price,
        "hold_minutes":  hold_minutes,
        "hold_extension_count": 0,
        "hold_decision_json": hold_extension,
        "size_eur":      executed_size_eur,
        "size_usd":      executed_size_usd,
        "intended_size_eur": intended_size_eur,
        "executed_size_eur": executed_size_eur,
        "executed_size_usd": executed_size_usd,
        "bracket_floor_qty_loss_pct": bracket_floor_qty_loss_pct,
        "atr_pct":       sizing.get("atr_pct") or atr_data.get("atr_pct"),
        "atr_raw":       atr_data.get("atr_raw"),
        "stop_pct":      stop_loss_pct,
        "stop_multiplier": sizing.get("stop_multiplier"),
        "side":          action,
        "composite_score": composite,
        "signals_json":  {k: {"score": v["score"]} for k, v in signals_snap.items()},
        "regime":        ticker_regime,
        "exposure_direction": exposure_direction,
        "strategy_family": strategy_family,
        "playbook": setup_context.get("playbook"),
        "playbook_lifecycle": setup_context.get("playbook_lifecycle"),
        "session_window": setup_context.get("session_window"),
        "primary_factor": setup_context.get("primary_factor"),
        "factor_bucket": setup_context.get("factor_bucket"),
        "regime_key": setup_context.get("regime_key"),
        "data_quality_state": setup_context.get("data_quality_state"),
        "data_quality_json": setup_context.get("data_quality") or {},
        "cost_estimate_json": setup_context.get("cost_estimate") or {},
        "estimated_spread_pct": setup_context.get("estimated_spread_pct"),
        "estimated_total_cost_pct": setup_context.get("estimated_total_cost_pct"),
        "regime_debug_json": regime_debug,
        "macro_regime":  signal_result.get("macro_regime"),
        "macro_multiplier": signal_result.get("macro_multiplier", 1.0),
        "horizon":       "short",
        "sizing_json":   sizing,
        "mean_reversion_trade": mean_reversion_trade,
        "swing_trade":   False if event_risk_probe else mean_reversion_trade,
        "llm_conviction": conviction,
        "llm_rationale": llm_result.get("rationale", ""),
        "order_id":      order.get("order_id"),
        "client_order_id": order.get("client_order_id"),
        # Grade metadata
        "setup_grade":   setup_grade.grade if setup_grade else None,
        "sector_confirmation": setup_grade.sector_confirmation if setup_grade else None,
        "percentile_rank": setup_grade.percentile_rank if setup_grade else None,
        "grade_reasons": setup_grade.reasons if setup_grade else [],
        # Partial exit + runner tracking
        "partial_target_price": partial_target_price,
        "partial_exit_pct":    partial_exit_pct,
        "partial_exit_done":   False,
        "partial_exit_qty":    0.0,
        "runner_atr_mult":     runner_atr_mult,
        "runner_stop_price":   None,
        # Thesis invalidation strike counter
        "vwap_thesis_strike_count": 0,
        # Phase 1: runner trail + breakeven promotion
        "breakeven_stop_set":           False,
        "runner_trail_update_count":    0,
        "runner_trail_last_update_at":  None,
        # Phase 2: hold score
        "hold_score_latest":            None,
        "hold_score_min":               None,
        "hold_score_max":               None,
        "trim_done":                    False,
    }
    save_open_trade(ticker, state._open_trades[ticker])

    log_event("TRADE", "order_submitted", {
        "ticker": ticker, "side": action,
        "size_eur": round(executed_size_eur, 2),
        "intended_size_eur": round(intended_size_eur, 2),
        "submitted_qty": round(submitted_qty, 6),
        "implied_qty": round(qty, 6),
        "bracket_floor_qty_loss_pct": bracket_floor_qty_loss_pct,
        "conviction": conviction,
        "composite": composite, "order_class": order.get("order_class"),
        "client_order_id": order.get("client_order_id"),
        "rationale": llm_result.get("rationale"),
        "sizing": sizing,
        "mean_reversion_trade": mean_reversion_trade,
        "event_risk_intraday_probe": event_risk_probe,
        "ev_decision": ev_result.get("ev_decision"),
        "exposure_direction": exposure_direction,
        "strategy_family": strategy_family,
        "playbook": setup_context.get("playbook"),
        "playbook_lifecycle": setup_context.get("playbook_lifecycle"),
        "session_window": setup_context.get("session_window"),
        "primary_factor": setup_context.get("primary_factor"),
        "data_quality_state": setup_context.get("data_quality_state"),
        "estimated_total_cost_pct": setup_context.get("estimated_total_cost_pct"),
    })


# ---------------------------------------------------------------------------
# Compatibility wrapper
# ---------------------------------------------------------------------------

def _process_ticker(ticker, regime, weights, profile, portfolio_state, recent_trades,
                    regime_state, shock_result):
    """Compatibility wrapper for one-off ticker processing."""
    candidate = _evaluate_ticker_candidate(
        ticker, regime, weights, profile, portfolio_state, recent_trades,
        regime_state, shock_result,
    )
    if candidate:
        _execute_trade_candidate(candidate, profile, portfolio_state)
