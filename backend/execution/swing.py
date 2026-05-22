"""
backend/execution/swing.py
Swing-promotion and swing-cycle execution helpers.

Depends on:
  - stdlib
  - backend.runtime.state    (mutable containers + scalars)
  - backend.runtime.env      (env helpers)
  - backend.execution.common (pure helpers)
  - backend.execution.exit   (close helpers + hydrate)
  - backend.execution.orders (price + order submission)
  - backend.market.sector    (_is_leveraged_etf)
  - backend.market.timing    (_allows_swing)
  - backend.execution.gates  (_overnight_event_risk_active)
  - backend.signals.engine   (detect_regime, detect_momentum_swing, ...)
  - backend.broker.alpaca    (submit_stop_order, scan_for_extreme_dips, ...)
  - backend.learning.engine  (get_effective_profile)
  - database.client          (save_open_trade, log_event)

Lazy imports (at call time, not module load) are used for:
  - backend.agent._get_cached_signals      (would be circular at load)
  - backend.agent._get_portfolio_state     (would be circular at load)
  - backend.agent._apply_execution_overrides (would be circular at load)
  - backend.agent._send_discord_alert      (would be circular at load)
"""
from __future__ import annotations
import os
from datetime import date, datetime
from typing import Optional

import backend.runtime.state as state
from backend.runtime.env import _env_float, _env_int
from backend.execution.common import (
    _trade_pnl_pct, _deterministic_action, _make_order_ref,
)
from backend.market.sector import _is_leveraged_etf
from backend.market.timing import _allows_swing
from backend.execution.gates import _overnight_event_risk_active
from backend.execution.exit import (
    _cancel_bracket_orders_for_manual_exit, _hydrate_open_trades,
    _open_position_tickers, _close_trade,
)
from backend.execution.orders import (
    _current_daily_price, _stop_pct_from_atr, _submit_horizon_order,
)
from backend.broker.alpaca import (
    submit_stop_order, scan_for_extreme_dips,
)
from backend.learning.engine import get_effective_profile
from database.client import save_open_trade, log_event


# ---------------------------------------------------------------------------
# Momentum swing promotion
# ---------------------------------------------------------------------------

def _try_promote_to_swing(ticker: str, trade: dict, current_price: float,
                          profile: dict, regime_state=None) -> bool:
    """
    Called at intraday time_exit boundary. If momentum is still intact and
    the trade is profitable or only within a controlled loss floor, promote it
    to a 3-5 day swing instead of closing.
    Returns True if promoted (caller should skip the close).
    """
    ticker = str(ticker or "").upper()
    if ticker not in state.SWING_TICKERS:
        log_event("INFO", "eod_carry_blocked_not_swing_ticker", {"ticker": ticker})
        return False

    if (trade.get("entry_price") or 0) <= 0:
        return False
    entry_price = float(trade.get("entry_price") or 0)
    pnl_pct = _trade_pnl_pct(trade, current_price)
    stop_pct_for_floor = float(
        trade.get("stop_pct")
        or profile.get("stop_loss_pct")
        or 2.5
    )
    max_loss_r = _env_float(
        "EOD_CARRY_MAX_LOSS_R",
        float(profile.get("eod_carry_max_loss_r", 0.5)),
    )
    max_carry_loss_pct = -1 * abs(stop_pct_for_floor) * max_loss_r
    if pnl_pct < max_carry_loss_pct:
        log_event("INFO", "eod_carry_blocked_loss_too_deep", {
            "ticker": ticker,
            "pnl_pct": round(pnl_pct, 4),
            "max_carry_loss_pct": round(max_carry_loss_pct, 4),
            "max_loss_r": max_loss_r,
        })
        return False

    # Don't promote mean-reversion trades
    if trade.get("mean_reversion_trade"):
        return False

    # Never promote leveraged ETFs to swing — daily decay makes overnight holds dangerous
    if _is_leveraged_etf(ticker, profile):
        return False

    overnight_event_risk = _overnight_event_risk_active(ticker)
    if overnight_event_risk:
        log_event("INFO", "eod_carry_blocked_event_risk", {
            "ticker": ticker,
            "event_risk": overnight_event_risk,
        })
        return False

    # Check concurrent swing limit before running expensive signal computation
    open_swing_count = sum(1 for d in state._open_trades.values() if d.get("swing_trade"))
    max_swings = int(profile.get("max_concurrent_swings", 2))
    if open_swing_count >= max_swings:
        log_event("INFO", "swing_promotion_blocked_concurrent", {
            "ticker": ticker,
            "open_swings": open_swing_count,
            "max_swings": max_swings,
        })
        return False

    overnight_count = sum(
        1 for t, d in state._open_trades.items()
        if t != ticker and d.get("swing_trade")
    )
    max_overnight = _env_int(
        "MAX_OVERNIGHT_CARRIES",
        int(profile.get("max_overnight_carries", 1)),
    )
    if overnight_count >= max_overnight:
        log_event("INFO", "eod_carry_blocked_overnight_cap", {
            "ticker": ticker,
            "open_overnight_carries": overnight_count,
            "max_overnight_carries": max_overnight,
        })
        return False

    try:
        from backend.agent import _get_cached_signals, detect_regime, detect_momentum_swing
        weights = state._learning_engine.get_weights("trending") if state._learning_engine else profile["signal_weights"]
        regime_state = regime_state or detect_regime(ticker)
        signal_result = _get_cached_signals(ticker, weights, regime_state)
        swing_check = detect_momentum_swing(ticker, signal_result, regime_state, profile)
    except Exception as e:
        log_event("WARN", "swing_promotion_signal_error", {"ticker": ticker, "error": str(e)[:80]})
        return False

    if not swing_check.get("swing_detected"):
        return False

    hold_days = swing_check["hold_days"]
    hold_minutes = swing_check["hold_minutes"]
    stop_multiplier = swing_check["stop_multiplier"]

    atr_data = signal_result.get("atr_data", {})
    atr_pct = atr_data.get("atr_pct") or 2.5
    atr_raw = atr_data.get("atr_raw") or (entry_price * atr_pct / 100)
    stop_pct = max(0.5, min(12.0, float(atr_pct) * stop_multiplier))

    side = trade.get("side", "BUY")
    if side == "BUY":
        stop_price = entry_price * (1 - stop_pct / 100)
        chandelier_stop = current_price - (atr_raw * stop_multiplier)
        protective_stop_price = max(stop_price, chandelier_stop)
        # Never tighter than 1 ATR below entry — prevents intraday-tight stops
        # surviving into a multi-day swing hold
        protective_stop_price = min(protective_stop_price, entry_price - atr_raw)
        protective_side = "sell"
    else:
        stop_price = entry_price * (1 + stop_pct / 100)
        chandelier_stop = current_price + (atr_raw * stop_multiplier)
        protective_stop_price = min(stop_price, chandelier_stop)
        protective_stop_price = max(protective_stop_price, entry_price + atr_raw)
        protective_side = "buy"

    cancel_results = _cancel_bracket_orders_for_manual_exit(ticker, trade)
    cancel_errors = [r for r in cancel_results if r.get("error")]
    if cancel_errors:
        log_event("WARN", "swing_promotion_bracket_cancel_failed", {
            "ticker": ticker,
            "errors": cancel_errors[:4],
        })
        return False

    protective_order = submit_stop_order(
        ticker=ticker,
        side=protective_side,
        qty=float(trade.get("quantity") or 0),
        stop_price=protective_stop_price,
        time_in_force="gtc",
    )
    if protective_order.get("error"):
        log_event("WARN", "swing_promotion_stop_order_failed", {
            "ticker": ticker,
            "error": protective_order["error"],
            "stop_price": round(protective_stop_price, 4),
        })
        return False

    state._open_trades[ticker].update({
        "swing_trade":              True,
        "promoted_to_swing":        True,
        "promoted_at":              datetime.utcnow().isoformat(),
        "initial_horizon":          trade.get("horizon", "short"),
        "horizon":                  "swing",
        "hold_minutes":             hold_minutes,
        "max_hold_minutes":         hold_minutes,
        "stop_multiplier":          stop_multiplier,
        "swing_conviction":         swing_check["conviction"],
        "swing_reasons":            swing_check["reasons"],
        "highest_price_since_entry": max(current_price, entry_price),
        "trailing_stop_price":      round(protective_stop_price, 4),
        "stop_price":               round(protective_stop_price, 4),
        "stop_pct":                 stop_pct,
        "protective_stop_order_id": protective_order.get("order_id"),
        "hold_decision_json": {
            "promoted_at_pnl_pct": round(pnl_pct, 3),
            "eod_decision":        "carry_overnight" if pnl_pct <= 0 else "promote_swing",
            "max_carry_loss_pct":  round(max_carry_loss_pct, 4),
            "swing_check":         swing_check,
            "cancelled_bracket_legs": cancel_results,
            "protective_stop_order": protective_order,
        },
    })

    save_result = save_open_trade(ticker, state._open_trades[ticker])
    if save_result.get("error"):
        # swing_trade=True exists in memory but not DB — next cold-start will
        # treat this as an intraday trade and may EOD-exit it incorrectly.
        log_event("ERROR", "swing_promotion_save_failed", {
            "ticker": ticker,
            "error": save_result["error"],
            "swing_trade": True,
            "protective_stop_order_id": protective_order.get("order_id"),
        })

    log_event("INFO", "swing_promoted", {
        "ticker":          ticker,
        "hold_days":       hold_days,
        "conviction":      swing_check["conviction"],
        "reasons":         swing_check["reasons"],
        "pnl_at_promotion": round(pnl_pct, 3),
        "protective_stop_order_id": protective_order.get("order_id"),
        "state_persisted": not bool(save_result.get("error")),
    })
    from backend.agent import _send_discord_alert
    _send_discord_alert(
        f"Swing promoted: {ticker} "
        f"{hold_days}-day hold · "
        f"Conviction: {swing_check['conviction']:.0%} · "
        f"P&L at promotion: {pnl_pct:+.1f}%"
    )
    return True


# ---------------------------------------------------------------------------
# Dip-buy scan
# ---------------------------------------------------------------------------

def _run_dip_buy_scan(tickers: list[str], portfolio_state: dict, macro_regime: str,
                      profile: dict, regime_state=None):
    opportunities = scan_for_extreme_dips(tickers, portfolio_state, macro_regime)
    log_event("SIGNAL", "extreme_dip_scan_complete", {
        "macro_regime": macro_regime,
        "tickers_scanned": len(tickers),
        "opportunities": len(opportunities),
    })

    open_tickers = _open_position_tickers(portfolio_state) | set(state._open_trades.keys())
    for opp in opportunities:
        log_event("SIGNAL", "extreme_dip_detected", opp)
        ticker = opp["ticker"]
        if ticker in open_tickers:
            log_event("INFO", "extreme_dip_skipped_open_position", {
                "ticker": ticker,
                "dip_score": opp.get("dip_score"),
            })
            continue
        stop_pct, atr_data = _stop_pct_from_atr(
            ticker,
            multiplier=opp.get("stop_multiplier", 2.0),
            fallback=profile.get("stop_loss_pct", 2.0) * 2,
        )
        order = _submit_horizon_order(
            ticker=ticker,
            side="BUY",
            conviction=opp.get("conviction", 0.85),
            profile=profile,
            portfolio_state=portfolio_state,
            regime="news_driven" if macro_regime == "geopolitical_shock" else "ranging",
            horizon="swing",
            stop_loss_pct=stop_pct,
            hold_days=opp.get("hold_days", 3),
            size_multiplier=opp.get("size_multiplier", 1.5),
            composite_score=opp.get("dip_score", 0.0),
            signals_json={"extreme_dip": {"score": opp.get("dip_score", 0.0), "meta": opp}},
            rationale=f"{opp.get('type')} dip buy: {opp.get('pct_from_high')}% below 20d high, RSI {opp.get('rsi')}",
            macro_regime=macro_regime,
            macro_multiplier=1.0,
            dip_type=opp.get("type"),
            regime_state=regime_state,
            atr_data=atr_data,
            order_ref=_make_order_ref("dip", ticker, opp.get("type"), date.today().isoformat()),
        )
        if "error" not in order:
            open_tickers.add(ticker)


# ---------------------------------------------------------------------------
# Daily swing re-evaluation
# ---------------------------------------------------------------------------

def re_evaluate_swing_positions():
    """
    Runs once per day at market open (09:35 EST / 14:35 UTC).
    Re-scores each promoted momentum swing position and decides:
    HOLD (extend), EXIT (close now), or TIGHTEN (trail stop on profit).
    """
    from backend.agent import _get_portfolio_state, _apply_execution_overrides
    from backend.signals.engine import detect_regime, compute_all_signals

    portfolio_state = _get_portfolio_state()
    _hydrate_open_trades(portfolio_state.get("positions", []))
    open_swings = [
        t for t, data in state._open_trades.items()
        if data.get("promoted_to_swing") is True
    ]

    if not open_swings:
        log_event("INFO", "swing_reeval_no_positions", {})
        return

    log_event("INFO", "swing_reeval_start", {"positions": open_swings})

    profile = _apply_execution_overrides(
        get_effective_profile(state.PROFILE, portfolio_state)
    )
    weights = (state._learning_engine.get_weights("trending")
               if state._learning_engine else profile["signal_weights"])
    regime = detect_regime()

    for ticker in open_swings:
        try:
            pos = state._open_trades[ticker]
            result = compute_all_signals(ticker, weights, regime_state=regime)
            composite = result["composite_score"]

            entry_price   = pos.get("entry_price", 0)
            current_price = _current_daily_price(ticker)
            if not current_price:
                continue
            pnl_pct = _trade_pnl_pct(pos, current_price)

            # Increment daily reeval counter
            state._open_trades[ticker]["daily_reeval_count"] = int(pos.get("daily_reeval_count", 0)) + 1

            exit_reasons = []

            if regime.market_regime == "bear":
                exit_reasons.append("regime_turned_bear")

            if composite < -0.20:
                exit_reasons.append("momentum_reversed")

            earn = (result["signals"]
                    .get("earnings_proximity", {})
                    .get("meta", {}))
            days_to_earn = earn.get("days_to_earnings")
            if days_to_earn is not None and days_to_earn <= 1:
                exit_reasons.append("earnings_tomorrow")

            if result.get("shock_detected"):
                exit_reasons.append("macro_shock")

            if pnl_pct >= 8.0:
                # Hard-coded take-profit — prevents greed overriding discipline
                exit_reasons.append("take_profit_8pct")

            # Check max hold days
            entry_time = pos.get("entry_time") or datetime.utcnow()
            days_held  = (datetime.utcnow() - entry_time).days
            max_days   = pos.get("max_hold_minutes", 1950) // 390
            if days_held >= max_days:
                exit_reasons.append("time_exit")

            if exit_reasons:
                log_event("INFO", "swing_exit_triggered", {
                    "ticker":    ticker,
                    "reasons":   exit_reasons,
                    "pnl_pct":   round(pnl_pct, 3),
                    "composite": composite,
                })
                _close_trade(ticker, pos, current_price, exit_reasons[0])
                from backend.agent import _send_discord_alert
                _send_discord_alert(
                    f"Swing exit: {ticker} "
                    f"P&L: {pnl_pct:+.1f}% "
                    f"Reason: {exit_reasons[0]}"
                )
                continue

            # Tighten stop if profitable — trail stop to lock in gains
            if pnl_pct > 3.0:
                old_stop_pct = float(pos.get("stop_pct", 2.5))
                new_stop_pct = old_stop_pct * 0.75
                state._open_trades[ticker]["stop_pct"] = new_stop_pct
                log_event("INFO", "swing_stop_tightened", {
                    "ticker":       ticker,
                    "pnl_pct":      round(pnl_pct, 3),
                    "new_stop_pct": round(new_stop_pct, 3),
                })

            days_remaining = max(0, max_days - days_held)
            save_open_trade(ticker, state._open_trades[ticker])
            log_event("INFO", "swing_hold_confirmed", {
                "ticker":          ticker,
                "composite":       composite,
                "pnl_pct":         round(pnl_pct, 3),
                "days_held":       days_held,
                "days_remaining":  days_remaining,
                "reeval_count":    state._open_trades[ticker]["daily_reeval_count"],
            })

        except Exception as e:
            log_event("ERROR", "swing_reeval_error",
                      {"ticker": ticker, "error": str(e)[:80]})


# ---------------------------------------------------------------------------
# Swing cycle entry point
# ---------------------------------------------------------------------------

def run_swing_cycle(portfolio_state: dict = None, profile: dict = None,
                    regime: str = None, macro_regime: str = None,
                    regime_state=None):
    """Daily swing re-evaluation and entry scan for SWING_TICKERS."""
    from backend.agent import _get_portfolio_state, _apply_execution_overrides
    from backend.signals.engine import detect_regime, detect_macro_regime, compute_swing_score

    if not _allows_swing():
        return
    if not state.SWING_TICKERS:
        return

    portfolio_state = portfolio_state or _get_portfolio_state()
    if portfolio_state.get("broker_error"):
        log_event("ERROR", "swing_broker_account_unavailable", {
            "error": portfolio_state["broker_error"],
        })
        return

    profile = profile or _apply_execution_overrides(get_effective_profile(state.PROFILE, portfolio_state))
    regime_state = regime_state or detect_regime()
    regime = regime or regime_state.intraday_regime
    if macro_regime is None:
        macro_regime = detect_macro_regime()

    _hydrate_open_trades(portfolio_state.get("positions", []))
    open_tickers = _open_position_tickers(portfolio_state) | set(state._open_trades.keys())
    log_event("INFO", "swing_cycle_start", {
        "tickers": state.SWING_TICKERS,
        "macro_regime": macro_regime,
        "open_tickers": sorted(open_tickers),
    })

    # Re-evaluate existing swing positions.
    for ticker, trade in list(state._open_trades.items()):
        if trade.get("horizon") != "swing":
            continue
        current_price = _current_daily_price(ticker)
        if not current_price:
            continue
        score, meta = compute_swing_score(ticker)
        elapsed_days = max(0, (datetime.utcnow() - trade["entry_time"]).days)
        exit_reason = None
        if trade["side"] == "BUY":
            if current_price <= trade["stop_price"]:
                exit_reason = "stop_loss"
            elif trade.get("take_profit_price") and current_price >= trade["take_profit_price"]:
                exit_reason = "take_profit"
            elif score < -0.15:
                exit_reason = "signal_reversal"
        elif trade["side"] == "SELL" and score > 0.15:
            exit_reason = "signal_reversal"
        if elapsed_days >= int(trade.get("hold_days") or 3) and exit_reason is None:
            exit_reason = "time_exit"
        log_event("SIGNAL", "swing_recheck", {
            "ticker": ticker,
            "score": round(score, 4),
            "elapsed_days": elapsed_days,
            "hold_days": trade.get("hold_days"),
            "exit_reason": exit_reason,
            "meta": meta,
        })
        if exit_reason:
            _close_trade(ticker, trade, current_price, exit_reason)

    open_tickers = _open_position_tickers(portfolio_state) | set(state._open_trades.keys())
    for ticker in state.SWING_TICKERS:
        if ticker in open_tickers:
            continue
        score, meta = compute_swing_score(ticker)
        action = _deterministic_action(score)
        if action == "SELL" and not profile.get("allow_short_selling", False):
            log_event("INFO", "swing_short_gated", {"ticker": ticker, "score": score})
            continue
        min_score = float(os.getenv("SWING_MIN_SCORE", profile.get("min_signal_score", 0.25)))
        if abs(score) < min_score:
            log_event("INFO", "swing_signal_below_threshold", {
                "ticker": ticker,
                "score": round(score, 4),
                "min_score": min_score,
            })
            continue
        stop_pct, atr_data = _stop_pct_from_atr(
            ticker,
            multiplier=2.0,
            fallback=profile.get("stop_loss_pct", 2.0) * 2,
        )
        conviction = max(0.65, min(0.90, abs(score)))
        _submit_horizon_order(
            ticker=ticker,
            side=action,
            conviction=conviction,
            profile=profile,
            portfolio_state=portfolio_state,
            regime=regime,
            horizon="swing",
            stop_loss_pct=stop_pct,
            hold_days=_env_int("SWING_HOLD_DAYS", 3),
            size_multiplier=1.0,
            composite_score=score,
            signals_json={"swing_score": {"score": score, "meta": meta}, "atr": {"score": 0, "meta": atr_data}},
            rationale="daily swing score entry",
            macro_regime=macro_regime,
            macro_multiplier=1.0,
            regime_state=regime_state,
            atr_data=atr_data,
            order_ref=_make_order_ref("swing", ticker, action, date.today().isoformat()),
        )
