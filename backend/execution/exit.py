"""
backend/execution/exit.py
Exit monitoring: stop-loss, hold-deadline, thesis invalidation, partial
exit, chandelier trail, hold-score, and trade-close accounting.

Depends on:
  - stdlib
  - backend.runtime.state    (mutable runtime containers)
  - backend.runtime.env      (env helpers)
  - backend.execution.common (pure helpers)
  - backend.market.sector    (_is_leveraged_etf, _exposure_direction)
  - backend.market.timing    (timing windows)
  - backend.broker.alpaca    (close_position, etc.)
  - backend.learning.engine  (compute_hold_score, attribute_signals)
  - database.client          (DB writes)
"""
from __future__ import annotations
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import backend.runtime.state as state
from backend.runtime.env import _env_bool, _env_int, _env_float, _eur_to_usd
from backend.execution.common import _parse_dt, _directional_score, _trade_pnl_pct
from backend.market.sector import _is_leveraged_etf, _exposure_direction
from backend.market.timing import (
    _is_eod_intraday_cleanup_window, _is_eod_final_force_exit_window,
    _minutes_to_regular_close, _to_new_york_time,
)
from backend.broker.alpaca import (
    close_position, close_partial_position, submit_stop_order,
    get_order_by_id, cancel_order_by_id, cancel_open_orders_for_symbol,
)
from backend.learning.engine import compute_hold_score, attribute_signals
from database.client import (
    log_event, save_open_trade, close_open_trade_record,
    get_open_trade_records, insert_trade, get_recent_trades,
    save_signal_weights,
)


def _open_position_tickers(portfolio_state: dict) -> set[str]:
    return {str(p.get("ticker", "")).upper() for p in portfolio_state.get("positions", [])}


def _apply_learned_hold_extension(
    ticker: str,
    hold_minutes: int,
    conviction: float,
    composite: float,
    profile: dict,
    portfolio_state: dict,
) -> tuple[int, Optional[dict]]:
    """
    Extend high-quality intraday trades based on the latest learning that
    longer holds have produced better net P&L.
    """
    if not _env_bool("LEARNED_HOLD_EXTENSION_ENABLED", True):
        return hold_minutes, None

    min_conviction = _env_float(
        "LEARNED_HOLD_MIN_CONVICTION",
        float(profile.get("learned_hold_min_conviction", 0.80)),
    )
    min_score = _env_float(
        "LEARNED_HOLD_MIN_SIGNAL_SCORE",
        float(profile.get("learned_hold_min_signal_score", 0.35)),
    )
    if conviction < min_conviction or abs(composite) < min_score:
        return hold_minutes, None

    vix = float(portfolio_state.get("vix") or 20.0)
    vix_ceiling = float(profile.get("vix_ceiling", 50))
    if vix > vix_ceiling:
        return hold_minutes, None

    min_minutes = _env_int(
        "LEARNED_HOLD_MIN_MINUTES",
        int(profile.get("learned_hold_min_minutes", 120)),
    )
    max_minutes = _env_int(
        "LEARNED_HOLD_MAX_MINUTES",
        int(profile.get("learned_hold_max_minutes", 240)),
    )
    confidence_span = max(
        0.0,
        min(1.0, (conviction - min_conviction) / max(1.0 - min_conviction, 0.01)),
    )
    target_minutes = int(min_minutes + (max_minutes - min_minutes) * confidence_span)
    extended_hold = max(int(hold_minutes), min(max_minutes, max(min_minutes, target_minutes)))
    if extended_hold == hold_minutes:
        return hold_minutes, None

    return extended_hold, {
        "ticker": ticker.upper(),
        "previous_hold_minutes": int(hold_minutes),
        "extended_hold_minutes": int(extended_hold),
        "conviction": round(float(conviction), 3),
        "composite": round(float(composite), 4),
        "min_conviction": min_conviction,
        "min_signal_score": min_score,
        "vix": round(vix, 2),
        "source": "learning_longer_holds_all_tickers",
    }


def _log_short_candidate(event_name: str, ticker: str, composite: float,
                         reason: str = None, profile: dict = None,
                         regime_state = None, extra: dict = None):
    profile = profile or {}
    market_regime = getattr(regime_state, "market_regime", None)
    min_short_score = profile.get("min_short_signal_score", profile.get("min_signal_score"))
    if str(market_regime or "").lower() == "bull":
        min_short_score = profile.get("bull_short_signal_score", min_short_score)
    payload = {
        "ticker": ticker,
        "composite": composite,
        "reason": reason,
        "market_regime": market_regime,
        "intraday_regime": getattr(regime_state, "intraday_regime", None),
        "min_signal_score": profile.get("min_signal_score"),
        "min_short_signal_score": min_short_score,
        "allow_short_selling": profile.get("allow_short_selling", False),
    }
    if extra:
        payload.update(extra)
    log_event("INFO", event_name, payload)


def _time_exit_cooldown_active(ticker: str, recent_trades: list, profile: dict) -> Optional[dict]:
    cooldown_minutes = _env_int(
        "TIME_EXIT_COOLDOWN_MINUTES",
        int(profile.get("time_exit_cooldown_minutes", 60)),
    )
    if cooldown_minutes <= 0:
        return None

    now = datetime.utcnow()
    latest = None
    for trade in recent_trades or []:
        if str(trade.get("ticker", "")).upper() != ticker.upper():
            continue
        if trade.get("exit_reason") != "time_exit":
            continue
        closed_at = _parse_dt(
            trade.get("exit_time") or trade.get("created_at") or trade.get("closed_at")
        )
        if latest is None or closed_at > latest:
            latest = closed_at

    if latest is None:
        return None
    elapsed = (now - latest).total_seconds() / 60
    if elapsed < cooldown_minutes:
        return {
            "ticker": ticker,
            "minutes_since_time_exit": round(elapsed, 1),
            "cooldown_minutes": cooldown_minutes,
        }
    return None


def _ticker_loss_cooldown_active(ticker: str, side: str, recent_trades: list,
                                 profile: dict) -> Optional[dict]:
    """Pause a ticker after repeated same-day same-direction losses."""
    max_losses = _env_int("TICKER_DAILY_LOSS_COOLDOWN_COUNT", int(profile.get("ticker_daily_loss_cooldown_count", 2)))
    if max_losses <= 0:
        return None
    min_reentry_score = float(os.getenv(
        "TICKER_LOSS_REENTRY_MIN_SCORE",
        str(profile.get("ticker_loss_reentry_min_score", 0.55)),
    ))
    today = datetime.utcnow().date()
    losses = []
    for trade in recent_trades or []:
        if str(trade.get("ticker", "")).upper() != ticker.upper():
            continue
        if str(trade.get("side", "")).upper() != str(side or "").upper():
            continue
        closed_at = _parse_dt(trade.get("exit_time") or trade.get("created_at") or trade.get("closed_at"))
        if closed_at.date() != today:
            continue
        if float(trade.get("net_pnl_pct") or 0) < 0:
            losses.append(trade)
    if len(losses) >= max_losses:
        return {
            "ticker": ticker,
            "side": side,
            "losses_today": len(losses),
            "min_reentry_score": min_reentry_score,
        }
    return None


def _thesis_invalidated_cooldown_active(ticker: str, side: str, recent_trades: list,
                                        profile: dict) -> Optional[dict]:
    """DB-backed cooldown after fast thesis invalidation churn."""
    cooldown_minutes = _env_int(
        "THESIS_INVALIDATED_COOLDOWN_MINUTES",
        int(profile.get("thesis_invalidated_cooldown_minutes", 75)),
    )
    if cooldown_minutes <= 0:
        return None
    now = datetime.utcnow()
    latest = None
    for trade in recent_trades or []:
        if str(trade.get("ticker", "")).upper() != ticker.upper():
            continue
        if str(trade.get("side", "")).upper() != str(side or "").upper():
            continue
        if trade.get("exit_reason") != "thesis_invalidated":
            continue
        closed_at = _parse_dt(trade.get("exit_time") or trade.get("closed_at") or trade.get("created_at"))
        if latest is None or closed_at > latest:
            latest = closed_at
    if latest is None:
        return None
    elapsed = (now - latest).total_seconds() / 60
    if elapsed < cooldown_minutes:
        return {
            "ticker": ticker,
            "side": side,
            "minutes_since_thesis_invalidated": round(elapsed, 1),
            "cooldown_minutes": cooldown_minutes,
        }
    return None


def _ranging_stop_loss_cooldown_active(ticker: str, side: str, recent_trades: list,
                                       profile: dict) -> Optional[dict]:
    """Pause a same-ticker re-entry after a stop loss in choppy intraday regime."""
    cooldown_minutes = _env_int(
        "RANGING_STOP_LOSS_COOLDOWN_MINUTES",
        int(profile.get("ranging_stop_loss_cooldown_minutes", 90)),
    )
    if cooldown_minutes <= 0:
        return None
    now = datetime.utcnow()
    latest = None
    for trade in recent_trades or []:
        if str(trade.get("ticker", "")).upper() != ticker.upper():
            continue
        if str(trade.get("side", "")).upper() != str(side or "").upper():
            continue
        if trade.get("exit_reason") != "stop_loss":
            continue
        closed_at = _parse_dt(trade.get("exit_time") or trade.get("closed_at") or trade.get("created_at"))
        if latest is None or closed_at > latest:
            latest = closed_at
    if latest is None:
        return None
    elapsed = (now - latest).total_seconds() / 60
    if elapsed < cooldown_minutes:
        return {
            "ticker": ticker,
            "side": side,
            "minutes_since_stop_loss": round(elapsed, 1),
            "cooldown_minutes": cooldown_minutes,
        }
    return None


def _rehydrated_open_trade(record: dict) -> dict:
    sizing = record.get("sizing_json") or {}
    entry_price = float(record.get("entry_price") or 0)
    return {
        "entry_time": _parse_dt(record.get("entry_time") or record.get("created_at")),
        "entry_price": entry_price,
        "quantity": float(record.get("quantity") or record.get("submitted_qty") or 0),
        "stop_price": float(record.get("stop_price") or 0),
        "take_profit_price": float(record.get("take_profit_price") or 0),
        "hold_minutes": int(record.get("hold_minutes") or 30),
        "hold_days": int(record.get("hold_days") or 0),
        "horizon": record.get("horizon") or "short",
        "size_eur": float(record.get("size_eur") or 0),
        "size_usd": float(record.get("size_usd") or 0),
        "intended_size_eur": float(record.get("intended_size_eur") or record.get("size_eur") or 0),
        "executed_size_eur": float(record.get("executed_size_eur") or record.get("size_eur") or 0),
        "executed_size_usd": float(record.get("executed_size_usd") or record.get("size_usd") or 0),
        "submitted_qty": float(record.get("submitted_qty") or record.get("quantity") or 0),
        "implied_qty": float(record.get("implied_qty") or record.get("quantity") or 0),
        "bracket_floor_qty_loss_pct": float(record.get("bracket_floor_qty_loss_pct") or 0),
        "side": record.get("side", "BUY"),
        "composite_score": float(record.get("composite_score") or 0),
        "signals_json": record.get("signals_json") or {},
        "regime": record.get("regime") or "ranging",
        "exposure_direction": record.get("exposure_direction"),
        "strategy_family": record.get("strategy_family"),
        "regime_debug_json": record.get("regime_debug_json") or {},
        "macro_regime": record.get("macro_regime"),
        "macro_multiplier": float(record.get("macro_multiplier") or 1.0),
        "dip_type": record.get("dip_type"),
        "sizing_json": sizing,
        "mean_reversion_trade": bool(record.get("mean_reversion_trade") or False),
        "swing_trade": bool(record.get("swing_trade") or False),
        "promoted_to_swing": bool(record.get("promoted_to_swing") or False),
        "promoted_at": record.get("promoted_at"),
        "initial_horizon": record.get("initial_horizon") or record.get("horizon") or "short",
        "swing_conviction": float(record.get("swing_conviction") or 0),
        "swing_reasons": record.get("swing_reasons") or [],
        "highest_price_since_entry": float(
            record.get("highest_price_since_entry") or entry_price or 0
        ),
        "trailing_stop_price": float(record.get("trailing_stop_price") or 0),
        "stop_multiplier": float(record.get("stop_multiplier") or sizing.get("stop_multiplier") or 1.5),
        "stop_pct": float(record.get("stop_pct") or sizing.get("stop_pct") or 0),
        "atr_pct": float(record.get("atr_pct") or sizing.get("atr_pct") or 0),
        "atr_raw": float(record.get("atr_raw") or sizing.get("atr_raw") or 0),
        "max_hold_minutes": int(record.get("max_hold_minutes") or record.get("hold_minutes") or 30),
        "daily_reeval_count": int(record.get("daily_reeval_count") or 0),
        "hold_extension_count": int(record.get("hold_extension_count") or 0),
        "hold_decision_json": record.get("hold_decision_json") or {},
        "peak_directional_score": float(record.get("peak_directional_score") or 0),
        "protective_stop_order_id": record.get("protective_stop_order_id"),
        "llm_conviction": float(record.get("llm_conviction") or 0),
        "llm_rationale": record.get("llm_rationale") or "",
        "order_id": record.get("order_id"),
        "client_order_id": record.get("client_order_id"),
        # Grading / partial-exit fields (added in migration v3)
        "setup_grade":              record.get("setup_grade"),
        "sector_confirmation":      record.get("sector_confirmation"),
        "partial_target_price":     float(record["partial_target_price"]) if record.get("partial_target_price") is not None else None,
        "partial_exit_pct":         float(record.get("partial_exit_pct") or 0.5),
        "partial_exit_done":        bool(record.get("partial_exit_done") or False),
        "partial_exit_qty":         float(record.get("partial_exit_qty") or 0),
        "runner_atr_mult":          float(record.get("runner_atr_mult") or 0.8),
        "runner_stop_price":        float(record["runner_stop_price"]) if record.get("runner_stop_price") is not None else None,
        "vwap_thesis_strike_count": int(record.get("vwap_thesis_strike_count") or 0),
        # Phase 1: runner trail + breakeven promotion
        "breakeven_stop_set":           bool(record.get("breakeven_stop_set") or False),
        "runner_trail_update_count":    int(record.get("runner_trail_update_count") or 0),
        "runner_trail_last_update_at":  record.get("runner_trail_last_update_at"),
        # Phase 2: hold score
        "hold_score_latest":  record.get("hold_score_latest"),
        "hold_score_min":     record.get("hold_score_min"),
        "hold_score_max":     record.get("hold_score_max"),
        "trim_done":          bool(record.get("trim_done") or False),
    }


def _hydrate_open_trades(broker_positions: list[dict] = None):
    """
    Rebuild runtime trade memory from persistent DB rows, then reconcile it to broker state.
    GitHub Actions runners are stateless, so the DB row is runtime memory between cycles.
    """
    records = get_open_trade_records()
    position_tickers = None
    if broker_positions is not None:
        position_tickers = _open_position_tickers({"positions": broker_positions})

    rebuilt = {}
    stale = []
    broker_closed = []
    for record in records:
        ticker = record.get("ticker")
        if not ticker:
            continue
        ticker = str(ticker).upper()
        if record.get("closed_at"):
            stale.append((ticker, "closed_at_present"))
            continue
        if position_tickers is not None and ticker not in position_tickers:
            broker_closed.append((ticker, record))
            continue
        trade = _rehydrated_open_trade(record)
        missing = [
            key for key in ("entry_time", "entry_price", "side", "order_id")
            if not trade.get(key)
        ]
        if missing:
            log_event("WARN", "open_trade_rehydrate_missing_fields", {
                "ticker": ticker,
                "missing": missing,
            })
        rebuilt[ticker] = trade

    for ticker, reason in stale:
        close_open_trade_record(ticker, reason)
        log_event("WARN", "stale_open_trade_reconciled", {
            "ticker": ticker,
            "reason": reason,
        })

    for ticker, record in broker_closed:
        trade = _rehydrated_open_trade(record)
        missing = [
            key for key in ("entry_time", "entry_price", "side", "order_id")
            if not trade.get(key)
        ]
        if missing:
            close_open_trade_record(ticker, "not_in_broker_positions")
            log_event("WARN", "stale_open_trade_reconciled", {
                "ticker": ticker,
                "reason": "not_in_broker_positions",
                "missing": missing,
            })
            continue

        # The broker no longer has the position, but Alpaca may have closed it
        # through a bracket/protective stop while this stateless runner slept.
        # Route through _close_trade so fill recovery writes the trades row.
        state._open_trades[ticker] = trade
        _close_trade(
            ticker,
            trade,
            exit_price=float(trade.get("entry_price") or 0),
            exit_reason="not_in_broker_positions",
        )

    memory_stale = set(state._open_trades) - set(rebuilt)
    if memory_stale:
        log_event("INFO", "open_trade_memory_rebuilt", {
            "removed": sorted(memory_stale),
            "loaded": sorted(rebuilt),
        })
    state._open_trades.clear()
    state._open_trades.update(rebuilt)


# ── PDT (Pattern Day Trader) tracking ────────────────────────────────────────

def _record_day_trade(ticker: str):
    """Record a same-day round trip for PDT monitoring."""
    state._day_trade_log.append((datetime.utcnow().date(), ticker))


def _count_day_trades_5d() -> int:
    """Count round trips (same-day open + close) in the last 5 calendar days."""
    cutoff = datetime.utcnow().date() - timedelta(days=7)
    return sum(1 for d, _ in state._day_trade_log if d >= cutoff)


def _check_pdt_warning(ticker: str, count: int):
    from backend.agent import _send_discord_alert  # lazy — avoids circular at load time
    log_event("WARN", "pdt_warning", {
        "day_trade_count_5d": count,
        "trigger_ticker": ticker,
        "note": "Approaching 4-round-trip PDT limit",
    })
    _send_discord_alert(
        f"PDT WARNING: {count} day trades in 5 days "
        f"(last: {ticker}). Stop at 3 to avoid PDT violation on live account."
    )


def _check_thesis_invalidation(ticker: str, trade: dict) -> Optional[str]:
    """
    2 consecutive cycles where price is on the wrong side of VWAP kills the breakout thesis.
    Uses only the signal cache — zero extra API calls.
    OR: 1 VWAP strike + tape deterioration → immediate exit.
    """
    cached = state._signal_cache.get(ticker)
    if cached is None:
        return None
    signals_now = cached[1].get("signals", {})
    vwap_val = signals_now.get("vwap_deviation") or {}
    tape_val = signals_now.get("tape_aggression") or {}
    vwap_score = float(vwap_val.get("score", 0) if isinstance(vwap_val, dict) else 0)
    tape_score = float(tape_val.get("score", 0) if isinstance(tape_val, dict) else 0)

    side = trade.get("side", "BUY")
    direction = 1 if side == "BUY" else -1

    # vwap_score > 0 = price BELOW VWAP. For a BUY breakout that's a thesis threat.
    # vwap_score < 0 = price ABOVE VWAP. For a SELL breakout that's a thesis threat.
    vwap_against = (direction == 1 and vwap_score > 0.2) or (direction == -1 and vwap_score < -0.2)
    tape_against = (tape_score * direction) < -0.2

    if vwap_against:
        count = int(state._open_trades[ticker].get("vwap_thesis_strike_count", 0)) + 1
        state._open_trades[ticker]["vwap_thesis_strike_count"] = count
        save_open_trade(ticker, state._open_trades[ticker])
        if count >= 2:
            log_event("INFO", "thesis_invalidated_vwap_2strike", {
                "ticker": ticker, "vwap_score": round(vwap_score, 3),
                "strikes": count, "side": side,
            })
            return "thesis_invalidated"
        if tape_against:
            log_event("INFO", "thesis_invalidated_vwap_tape", {
                "ticker": ticker, "vwap_score": round(vwap_score, 3),
                "tape_score": round(tape_score, 3), "side": side,
            })
            return "thesis_invalidated"
    else:
        if state._open_trades[ticker].get("vwap_thesis_strike_count", 0) != 0:
            state._open_trades[ticker]["vwap_thesis_strike_count"] = 0
            save_open_trade(ticker, state._open_trades[ticker])
    return None


def _trim_position(ticker: str, trade: dict, current_price: float, trim_pct: float):
    """
    Reduce an open position by trim_pct when hold_score indicates degraded conviction
    but the trade is still profitable. Distinct from the grade-driven partial exit:
    - partial exit fires on a price target (take some profit)
    - trim fires on conviction decay (reduce risk while still green)
    One-shot per trade: guarded by trade["trim_done"].
    """
    total_qty = float(trade.get("quantity") or 0)
    trim_qty  = round(total_qty * trim_pct, 6)
    if trim_qty <= 0:
        return

    side       = trade.get("side", "BUY")
    close_side = "sell" if side == "BUY" else "buy"
    result     = close_partial_position(ticker, trim_qty, close_side)
    if result.get("error"):
        log_event("WARN", "hold_score_trim_failed", {
            "ticker": ticker, "trim_qty": trim_qty, "error": result["error"],
        })
        return

    remaining_qty = total_qty - trim_qty
    state._open_trades[ticker]["quantity"] = remaining_qty
    state._open_trades[ticker]["trim_done"] = True
    save_open_trade(ticker, state._open_trades[ticker])

    log_event("TRADE", "hold_score_trim_executed", {
        "ticker":        ticker,
        "side":          side,
        "trim_qty":      trim_qty,
        "remaining_qty": remaining_qty,
        "trim_pct":      trim_pct,
        "current_price": round(current_price, 4),
        "order_id":      result.get("order_id"),
    })


def _check_hold_score(
    ticker: str,
    trade: dict,
    current_price: float,
    hold_elapsed: float,
    hold_target: float,
    profile: dict,
) -> Optional[str]:
    """
    Compute the hold score each cycle and update trade state.

    Always computed and logged (observability from day 1).
    Actions gated by separate env vars — start with extend only:
    - HOLD_SCORE_EXTEND_ENABLED=true   extend hold on strong score
    - HOLD_SCORE_TRIM_ENABLED=false    trim position on weak score (enable after validation)
    - HOLD_SCORE_EXIT_ENABLED=false    force-exit on very weak score (enable after validation)

    Returns an exit reason string if exit fires, otherwise None.
    """
    if not _env_bool("HOLD_SCORE_ENABLED", True):
        return None

    cached = state._signal_cache.get(ticker)
    if cached is None:
        return None
    current_signals = cached[1].get("signals", {})

    result = compute_hold_score(
        ticker=ticker,
        trade=trade,
        current_signals=current_signals,
        hold_elapsed_minutes=hold_elapsed,
    )

    hold_score     = result["hold_score"]
    recommendation = result["recommendation"]

    # Update rolling min/max on the trade record
    prev_min = trade.get("hold_score_min")
    prev_max = trade.get("hold_score_max")
    new_min  = hold_score if prev_min is None else min(prev_min, hold_score)
    new_max  = hold_score if prev_max is None else max(prev_max, hold_score)

    state._open_trades[ticker]["hold_score_latest"] = hold_score
    state._open_trades[ticker]["hold_score_min"]    = new_min
    state._open_trades[ticker]["hold_score_max"]    = new_max
    save_open_trade(ticker, state._open_trades[ticker])

    log_event("INFO", "hold_score_computed", {
        "ticker":          ticker,
        "hold_score":      hold_score,
        "recommendation":  recommendation,
        "confidence":      result["confidence"],
        "exhaustion":      result["exhaustion_active"],
        "hold_elapsed":    round(hold_elapsed, 1),
        "components":      result["components"],
    })

    pnl_pct = _trade_pnl_pct(trade, current_price)

    # Force exit — only when HOLD_SCORE_EXIT_ENABLED=true (default off)
    if (
        _env_bool("HOLD_SCORE_EXIT_ENABLED", False)
        and recommendation == "exit"
    ):
        log_event("INFO", "hold_score_exit_triggered", {
            "ticker": ticker, "hold_score": hold_score, "pnl_pct": round(pnl_pct, 4),
        })
        return "hold_score_collapsed"

    # Trim — only when HOLD_SCORE_TRIM_ENABLED=true (default off) and trade is profitable
    if (
        _env_bool("HOLD_SCORE_TRIM_ENABLED", False)
        and recommendation == "trim"
        and pnl_pct > 0
        and not trade.get("trim_done")
        and not trade.get("partial_exit_done")
    ):
        trim_pct = _env_float("HOLD_SCORE_TRIM_PCT", 0.33)
        _trim_position(ticker, trade, current_price, trim_pct)

    # Extend — when HOLD_SCORE_EXTEND_ENABLED=true (default on) and not already extended
    if (
        _env_bool("HOLD_SCORE_EXTEND_ENABLED", True)
        and recommendation == "extend"
        and int(trade.get("hold_extension_count") or 0) == 0
    ):
        extend_minutes = _env_int("HOLD_SCORE_EXTEND_MINUTES", 30)
        new_target = int(hold_target) + extend_minutes
        state._open_trades[ticker]["max_hold_minutes"] = new_target
        state._open_trades[ticker]["hold_extension_count"] = 1
        save_open_trade(ticker, state._open_trades[ticker])
        log_event("INFO", "hold_score_extend_triggered", {
            "ticker":          ticker,
            "hold_score":      hold_score,
            "old_target":      hold_target,
            "new_target":      new_target,
            "extend_minutes":  extend_minutes,
        })

    return None


def _check_breakeven_promotion(ticker: str, trade: dict, current_price: float):
    """
    Once MFE reaches breakeven_atr_mult × atr_pct, move the in-memory stop to just
    above (for longs) / below (for shorts) the entry price so a reversal cannot turn
    a winner into a full loss.

    For trades still on the original bracket: only the in-memory stop_price is updated;
    the broker-side bracket leg remains as a catastrophic backstop.
    For trades with a live protective_stop_order_id: we cancel+replace at breakeven.
    One-shot: guarded by breakeven_stop_set to prevent re-firing.
    """
    if not _env_bool("BREAKEVEN_PROMOTION_ENABLED", True):
        return
    if trade.get("breakeven_stop_set") or trade.get("partial_exit_done"):
        return

    entry_price = float(trade.get("entry_price") or 0)
    atr_pct = float(trade.get("atr_pct") or 0)
    if entry_price <= 0 or atr_pct <= 0:
        return

    pnl_pct = _trade_pnl_pct(trade, current_price)
    threshold_pct = atr_pct * _env_float("BREAKEVEN_ATR_MULT", 0.6)
    if pnl_pct < threshold_pct:
        return

    side = trade.get("side", "BUY")
    tick = entry_price * 0.001  # 0.1% buffer
    if side == "BUY":
        new_stop = entry_price + tick
        old_stop = float(trade.get("stop_price") or 0)
        if new_stop <= old_stop:
            state._open_trades[ticker]["breakeven_stop_set"] = True
            save_open_trade(ticker, state._open_trades[ticker])
            return
    else:
        new_stop = entry_price - tick
        old_stop = float(trade.get("stop_price") or 0)
        if old_stop > 0 and new_stop >= old_stop:
            state._open_trades[ticker]["breakeven_stop_set"] = True
            save_open_trade(ticker, state._open_trades[ticker])
            return

    if trade.get("protective_stop_order_id"):
        stop_order = _replace_protective_stop_order(ticker, state._open_trades[ticker], new_stop)
        if stop_order.get("error"):
            log_event("WARN", "breakeven_promotion_failed", {
                "ticker": ticker,
                "error": stop_order["error"],
                "new_stop": round(new_stop, 4),
            })
            return
    else:
        # Bracket still active — update in-memory stop only; broker bracket is the backstop
        state._open_trades[ticker]["stop_price"] = round(new_stop, 4)

    state._open_trades[ticker]["breakeven_stop_set"] = True
    save_open_trade(ticker, state._open_trades[ticker])

    log_event("INFO", "breakeven_promoted", {
        "ticker": ticker,
        "side": side,
        "entry_price": round(entry_price, 4),
        "new_stop": round(new_stop, 4),
        "old_stop": round(old_stop, 4),
        "pnl_pct": round(pnl_pct, 4),
        "threshold_pct": round(threshold_pct, 4),
        "atr_pct": round(atr_pct, 4),
        "has_protective_stop_order": bool(trade.get("protective_stop_order_id")),
    })


def _update_intraday_runner_stop(ticker: str, trade: dict, current_price: float):
    """
    Every cycle after partial exit is done, ratchet the runner stop upward using the
    same chandelier mechanism that already works for promoted swing trades.
    Calls _replace_protective_stop_order() (cancel + resubmit GTC stop) only when the
    new candidate stop is strictly better than the current runner_stop_price.
    """
    if not _env_bool("RUNNER_ACTIVE_TRAIL_ENABLED", True):
        return
    if not trade.get("partial_exit_done"):
        return
    if not trade.get("runner_stop_price"):
        return

    side = trade.get("side", "BUY")
    entry_price = float(trade.get("entry_price") or current_price)
    prev_highest = float(trade.get("highest_price_since_entry") or entry_price)

    if side == "BUY":
        new_highest = max(current_price, prev_highest)
    else:
        new_highest = min(current_price, prev_highest) if prev_highest > 0 else current_price

    state._open_trades[ticker]["highest_price_since_entry"] = new_highest

    runner_atr_mult = float(trade.get("runner_atr_mult") or 0.8)
    try:
        from backend.signals.engine import compute_atr
        atr_info = compute_atr(ticker)
        atr_raw = float(atr_info.get("atr_raw") or (current_price * 0.025))
    except Exception:
        atr_raw = current_price * 0.025

    if side == "BUY":
        candidate_stop = new_highest - atr_raw * runner_atr_mult
        old_stop = float(trade.get("runner_stop_price") or 0)
        should_trail = candidate_stop > old_stop
    else:
        candidate_stop = new_highest + atr_raw * runner_atr_mult
        old_stop = float(trade.get("runner_stop_price") or 0)
        should_trail = old_stop <= 0 or candidate_stop < old_stop

    if not should_trail:
        return

    stop_order = _replace_protective_stop_order(ticker, state._open_trades[ticker], candidate_stop)
    if stop_order.get("error"):
        log_event("WARN", "runner_stop_trail_failed", {
            "ticker": ticker,
            "error": stop_order["error"],
            "candidate_stop": round(candidate_stop, 4),
        })
        return

    trail_count = int(trade.get("runner_trail_update_count") or 0) + 1
    state._open_trades[ticker]["runner_stop_price"] = round(candidate_stop, 4)
    state._open_trades[ticker]["runner_trail_update_count"] = trail_count
    state._open_trades[ticker]["runner_trail_last_update_at"] = datetime.utcnow().isoformat()
    save_open_trade(ticker, state._open_trades[ticker])

    log_event("INFO", "runner_stop_trailed", {
        "ticker": ticker,
        "side": side,
        "old_stop": round(old_stop, 4),
        "new_stop": round(candidate_stop, 4),
        "highest_since_entry": round(new_highest, 4),
        "current_price": round(current_price, 4),
        "atr_raw": round(atr_raw, 4),
        "runner_atr_mult": runner_atr_mult,
        "trail_count": trail_count,
    })


def _check_partial_exit(ticker: str, trade: dict, current_price: float):
    """
    Execute a partial exit if the price has reached the grade-defined target.
    Updates _open_trades with runner stop after the partial.
    No-op if partial target is not set or already done.
    """
    if trade.get("partial_exit_done"):
        return

    partial_target = trade.get("partial_target_price")
    if not partial_target:
        return

    side = trade.get("side", "BUY")
    target_hit = (side == "BUY" and current_price >= partial_target) or \
                 (side == "SELL" and current_price <= partial_target)
    if not target_hit:
        return

    total_qty = float(trade.get("quantity") or 0)
    if total_qty <= 0:
        return

    partial_pct = float(trade.get("partial_exit_pct") or 0.5)
    close_qty = round(total_qty * partial_pct, 6)
    if close_qty <= 0:
        return

    cancel_results = _cancel_bracket_orders_for_manual_exit(ticker, trade)
    if cancel_results:
        log_event("INFO", "bracket_orders_cancelled_for_partial_exit", {
            "ticker": ticker,
            "results": cancel_results[:4],
        })

    close_side = "sell" if side == "BUY" else "buy"
    result = close_partial_position(ticker, close_qty, close_side)
    if result.get("error"):
        log_event("WARN", "partial_exit_failed", {
            "ticker": ticker, "close_qty": close_qty, "error": result["error"],
        })
        return

    remaining_qty = total_qty - close_qty
    runner_atr_mult = float(trade.get("runner_atr_mult") or 1.0)

    # Compute runner trailing stop from current price
    try:
        from backend.signals.engine import compute_atr
        atr_info = compute_atr(ticker)
        atr_raw = float(atr_info.get("atr_raw") or (current_price * 0.025))
    except Exception:
        atr_raw = current_price * 0.025

    if side == "BUY":
        runner_stop = current_price - atr_raw * runner_atr_mult
        stop_side = "sell"
    else:
        runner_stop = current_price + atr_raw * runner_atr_mult
        stop_side = "buy"

    stop_order = submit_stop_order(ticker, stop_side, remaining_qty, runner_stop, time_in_force="day")
    if stop_order.get("error"):
        log_event("WARN", "runner_stop_submit_failed", {
            "ticker": ticker,
            "remaining_qty": remaining_qty,
            "runner_stop": round(runner_stop, 4),
            "error": stop_order["error"],
        })

    state._open_trades[ticker]["partial_exit_done"] = True
    state._open_trades[ticker]["partial_exit_qty"] = close_qty
    state._open_trades[ticker]["quantity"] = remaining_qty
    state._open_trades[ticker]["runner_stop_price"] = round(runner_stop, 4)
    if not stop_order.get("error"):
        state._open_trades[ticker]["protective_stop_order_id"] = stop_order.get("order_id")

    save_result = save_open_trade(ticker, state._open_trades[ticker])
    if save_result.get("error"):
        # Partial exit executed at broker but state not persisted — next cold-start
        # will hydrate partial_exit_done=False and risk a duplicate partial exit.
        log_event("ERROR", "partial_exit_save_failed", {
            "ticker": ticker,
            "error": save_result["error"],
            "partial_exit_done": True,
            "remaining_qty": remaining_qty,
        })

    log_event("TRADE", "partial_exit_executed", {
        "ticker": ticker, "side": side, "close_qty": close_qty,
        "remaining_qty": remaining_qty, "partial_pct": partial_pct,
        "exit_price": current_price, "partial_target": round(partial_target, 4),
        "runner_stop": round(runner_stop, 4), "runner_atr_mult": runner_atr_mult,
        "order_id": result.get("order_id"),
        "runner_stop_order_id": stop_order.get("order_id"),
        "state_persisted": not bool(save_result.get("error")),
    })


def _recover_bracket_fill(trade: dict) -> Optional[dict]:
    """
    Query Alpaca order history to recover exit price when a bracket leg
    (stop-loss or take-profit) was triggered autonomously by Alpaca.
    Returns {"exit_price", "exit_reason", "close_order_id"} or None.
    """
    order_id = trade.get("order_id")
    if not order_id:
        return None
    try:
        order = get_order_by_id(str(order_id))
        if "error" in order:
            return None
        for leg in order.get("legs", []):
            if "filled" not in str(leg.get("status", "")).lower():
                continue
            price = float(leg.get("filled_price") or 0)
            if not price:
                continue
            order_type = str(leg.get("type", "")).lower()
            # limit leg = take-profit; stop leg = stop-loss
            reason = "take_profit" if "limit" in order_type else "stop_loss"
            return {
                "exit_price":     price,
                "exit_reason":    reason,
                "close_order_id": leg.get("id"),
            }
    except Exception:
        pass
    return None


def _recover_protective_stop_fill(trade: dict) -> Optional[dict]:
    """Recover fill details when a standalone protective stop already closed the position."""
    order_id = trade.get("protective_stop_order_id")
    if not order_id:
        return None
    try:
        order = get_order_by_id(str(order_id))
        if "error" in order or "filled" not in str(order.get("status", "")).lower():
            return None
        price = float(order.get("filled_price") or 0)
        if not price:
            return None
        return {
            "exit_price": price,
            "exit_reason": "chandelier_stop",
            "close_order_id": order.get("id") or order_id,
        }
    except Exception:
        return None


def _cancel_protective_stop_order(trade: dict) -> Optional[dict]:
    order_id = trade.get("protective_stop_order_id")
    if not order_id:
        return None
    return cancel_order_by_id(str(order_id))


def _replace_protective_stop_order(ticker: str, trade: dict,
                                   stop_price: float) -> dict:
    """Replace the broker-side protective stop when a trailing stop ratchets."""
    cancel_result = _cancel_protective_stop_order(trade)
    if cancel_result and cancel_result.get("error"):
        return {"error": cancel_result["error"], "cancel_result": cancel_result}

    close_side = "sell" if trade.get("side", "BUY") == "BUY" else "buy"
    order = submit_stop_order(
        ticker=ticker,
        side=close_side,
        qty=float(trade.get("quantity") or 0),
        stop_price=stop_price,
        time_in_force="gtc",
    )
    if "error" not in order:
        trade["protective_stop_order_id"] = order.get("order_id")
        trade["stop_price"] = round(float(stop_price), 4)
        trade["trailing_stop_price"] = round(float(stop_price), 4)
    return order


def _cancel_bracket_orders_for_manual_exit(ticker: str, trade: dict) -> list[dict]:
    """
    Time exits can conflict with open bracket legs that reserve shares.
    Cancel those legs first, then submit the manual close.
    """
    order_id = trade.get("order_id")
    results = []

    if order_id:
        order = get_order_by_id(str(order_id))
        if "error" in order:
            results.append({"order_id": order_id, "error": order["error"]})
        else:
            terminal = {"filled", "canceled", "cancelled", "expired", "rejected"}
            for leg in order.get("legs", []):
                status = str(leg.get("status", "")).lower()
                leg_id = leg.get("id")
                if not leg_id or any(state in status for state in terminal):
                    continue
                results.append(cancel_order_by_id(str(leg_id)))

    if results:
        time.sleep(1)
    symbol_results = cancel_open_orders_for_symbol(ticker)
    if symbol_results:
        results.extend(symbol_results)
        time.sleep(1)
    return results


def _check_exits(portfolio_state, profile):
    """Check all open trades for stop-loss, chandelier stop, or time-based exit."""
    import yfinance as yf
    from backend.signals.engine import detect_regime, compute_atr
    from backend.execution.swing import _try_promote_to_swing
    now = datetime.now(timezone.utc).replace(tzinfo=None)  # naive UTC for hold_elapsed arithmetic
    now_aware = datetime.now(timezone.utc)
    eod_cleanup = _is_eod_intraday_cleanup_window(now_aware)
    eod_final_force = _is_eod_final_force_exit_window(now_aware)
    _regime_cache: dict[str, object] = {}  # per-call cache: ticker → regime_state

    for ticker, trade in list(state._open_trades.items()):
        # Traditional horizon-swing trades (dip buys, ETF swings) are managed by
        # run_swing_cycle / re_evaluate_swing_positions — skip here.
        # Promoted momentum swings (promoted_to_swing=True) do get intraday
        # chandelier-stop checks.
        if trade.get("horizon") == "swing" and not trade.get("promoted_to_swing"):
            continue
        try:
            bar = yf.download(ticker, period="1d", interval="1m",
                             progress=False, auto_adjust=True)
            if bar.empty:
                continue
            current_price = float(bar["Close"].squeeze().iloc[-1])
            entry_time    = trade["entry_time"]
            hold_elapsed  = (now - entry_time).total_seconds() / 60
            hold_target   = trade.get("max_hold_minutes") or trade["hold_minutes"]

            exit_reason = None

            # Leveraged ETFs must be closed by profile's max_hold cutoff (default 3:45 PM ET)
            if exit_reason is None and _is_leveraged_etf(ticker, profile):
                max_hold_min = int(profile.get("leveraged_etf_max_hold_minutes", 345))
                ny_now = _to_new_york_time(datetime.now(timezone.utc))
                market_open_minutes = ny_now.hour * 60 + ny_now.minute - (9 * 60 + 30)
                if market_open_minutes >= max_hold_min:
                    exit_reason = "leveraged_etf_time_exit"
                    log_event("INFO", "leveraged_etf_time_exit", {
                        "ticker": ticker,
                        "market_open_minutes": market_open_minutes,
                        "max_hold_minutes": max_hold_min,
                    })

            if trade.get("promoted_to_swing"):
                # Chandelier trailing stop: highest_since_entry - (ATR × stop_mult)
                entry_price = trade.get("entry_price", current_price)
                prev_highest = trade.get("highest_price_since_entry", entry_price)
                new_highest  = max(current_price, prev_highest)
                state._open_trades[ticker]["highest_price_since_entry"] = new_highest

                atr_info = compute_atr(ticker)
                atr_raw  = atr_info.get("atr_raw") or (entry_price * 0.025)
                stop_mult = float(trade.get("stop_multiplier", 2.5))
                if trade.get("side", "BUY") == "BUY":
                    chandelier_stop = new_highest - (atr_raw * stop_mult)
                    old_stop = float(trade.get("trailing_stop_price") or trade.get("stop_price") or 0)
                    should_replace_stop = chandelier_stop > old_stop
                else:
                    chandelier_stop = min(current_price, prev_highest) + (atr_raw * stop_mult)
                    old_stop = float(trade.get("trailing_stop_price") or trade.get("stop_price") or 0)
                    should_replace_stop = old_stop <= 0 or chandelier_stop < old_stop

                if should_replace_stop:
                    stop_order = _replace_protective_stop_order(ticker, state._open_trades[ticker], chandelier_stop)
                    if stop_order.get("error"):
                        log_event("WARN", "protective_stop_replace_failed", {
                            "ticker": ticker,
                            "error": stop_order["error"],
                            "stop_price": round(chandelier_stop, 4),
                        })
                        exit_reason = "circuit_breaker"
                    else:
                        save_open_trade(ticker, state._open_trades[ticker])

                pnl_pct = _trade_pnl_pct(trade, current_price)
                if exit_reason:
                    pass
                elif trade.get("side", "BUY") == "BUY" and current_price <= chandelier_stop:
                    exit_reason = "chandelier_stop"
                elif trade.get("side") == "SELL" and current_price >= chandelier_stop:
                    exit_reason = "chandelier_stop"
                elif pnl_pct >= 8.0:
                    # Hard-coded take-profit — not configurable
                    exit_reason = "take_profit_8pct"
                # Time exit for promoted swings is handled by re_evaluate_swing_positions (daily)

            else:
                # Normal intraday stop/take-profit/time exit

                # For extended trades check momentum decay each cycle before stop/time checks
                if exit_reason is None and int(trade.get("hold_extension_count") or 0) > 0:
                    exit_reason = _check_momentum_exit(ticker, trade, profile)

                # Phase 1: Move stop to breakeven once MFE crosses breakeven_atr_mult × ATR
                if exit_reason is None and not trade.get("partial_exit_done"):
                    _check_breakeven_promotion(ticker, trade, current_price)

                # Partial exit + runner check (before stop/TP — runner may have its own stop)
                if exit_reason is None and not trade.get("partial_exit_done"):
                    _check_partial_exit(ticker, trade, current_price)

                # Phase 1: Ratchet runner stop upward every cycle after partial exit fires
                if exit_reason is None and trade.get("partial_exit_done"):
                    _update_intraday_runner_stop(ticker, trade, current_price)

                # Phase 2: Compute hold score; optionally extend, trim, or exit
                if exit_reason is None:
                    exit_reason = _check_hold_score(
                        ticker, trade, current_price, hold_elapsed, hold_target, profile
                    )
                    hold_target = state._open_trades.get(ticker, {}).get("max_hold_minutes") or hold_target

                # Thesis invalidation: 2 consecutive VWAP-against closes
                if exit_reason is None and not trade.get("mean_reversion_trade"):
                    exit_reason = _check_thesis_invalidation(ticker, trade)

                # Stop-loss and take-profit: runner_stop_price takes precedence once active
                stop_price = float(trade.get("runner_stop_price") or 0) or trade["stop_price"]
                take_profit_price = trade.get("take_profit_price")

                if trade["side"] == "SELL":
                    if current_price >= stop_price:
                        exit_reason = "stop_loss"
                    elif take_profit_price and current_price <= take_profit_price:
                        exit_reason = "take_profit"
                else:
                    if current_price <= stop_price:
                        exit_reason = "stop_loss"
                    elif take_profit_price and current_price >= take_profit_price:
                        exit_reason = "take_profit"

                if exit_reason is None:
                    if eod_cleanup:
                        # Near close: promote/carry strong swing-eligible trades, close everything else.
                        if eod_final_force:
                            pnl_pct_eod = _trade_pnl_pct(trade, current_price)
                            log_event("INFO", "eod_intraday_cleanup_exit", {
                                "ticker": ticker,
                                "pnl_pct": round(pnl_pct_eod, 4),
                                "outcome": "loser" if pnl_pct_eod < 0 else (
                                    "flat" if pnl_pct_eod < 0.05 else "winner_final_window"
                                ),
                                "eod_decision": "final_window_force_exit",
                                "minutes_to_close": _minutes_to_regular_close(now_aware),
                            })
                            exit_reason = "eod_cleanup"
                        else:
                            # Regime is cached per-call to avoid duplicate detect_regime fetches.
                            cached_regime = _regime_cache.get(ticker)
                            if cached_regime is None:
                                cached_regime = detect_regime(ticker)
                                _regime_cache[ticker] = cached_regime
                            if _try_promote_to_swing(ticker, trade, current_price, profile, cached_regime):
                                eod_decision = (
                                    "carry_overnight"
                                    if _trade_pnl_pct(trade, current_price) <= 0
                                    else "promote_swing"
                                )
                                log_event("INFO", "eod_intraday_promoted", {
                                    "ticker": ticker,
                                    "pnl_pct": round(_trade_pnl_pct(trade, current_price), 4),
                                    "eod_decision": eod_decision,
                                })
                                continue
                            pnl_pct_eod = _trade_pnl_pct(trade, current_price)
                            log_event("INFO", "eod_intraday_cleanup_exit", {
                                "ticker": ticker,
                                "pnl_pct": round(pnl_pct_eod, 4),
                                "outcome": "loser" if pnl_pct_eod < 0 else (
                                    "flat" if pnl_pct_eod < 0.05 else "winner_not_swing_qualified"
                                ),
                                "eod_decision": "force_exit",
                            })
                            exit_reason = "eod_cleanup"
                    elif hold_elapsed >= hold_target:
                        exit_reason = _handle_hold_deadline(
                            ticker, trade, current_price, profile, portfolio_state
                        )
                        if exit_reason is None:
                            continue

            if exit_reason:
                _close_trade(ticker, trade, current_price, exit_reason)
        except Exception as e:
            log_event("ERROR", f"exit_check_{ticker}", {"error": str(e)})


def _handle_hold_deadline(
    ticker: str,
    trade: dict,
    current_price: float,
    profile: dict,
    portfolio_state: dict,
) -> Optional[str]:
    """
    Decide what to do when an intraday trade reaches its hold deadline.
    Time is treated as a risk signal: exit weak trades, extend aligned winners,
    and let strong breakouts attempt swing promotion.
    """
    from backend.signals.engine import detect_regime
    from backend.agent import _get_cached_signals
    from backend.execution.swing import _try_promote_to_swing
    pnl_pct = _trade_pnl_pct(trade, current_price)
    side = trade.get("side", "BUY")
    entry_score = _directional_score(side, float(trade.get("composite_score") or 0.0))

    try:
        regime_state = detect_regime(ticker)
        weights = (
            state._learning_engine.get_weights(regime_state.intraday_regime)
            if state._learning_engine else profile["signal_weights"]
        )
        signal_result = _get_cached_signals(ticker, weights, regime_state)
        current_composite = float(signal_result.get("composite_score") or 0.0)
    except Exception as e:
        log_event("WARN", "hold_deadline_signal_error", {
            "ticker": ticker,
            "error": str(e)[:100],
        })
        current_composite = 0.0
        signal_result = {}

    current_score = _directional_score(side, current_composite)
    fade_score = _env_float(
        "HOLD_EXTENSION_FADE_SCORE",
        float(profile.get("hold_extension_fade_score", 0.10)),
    )
    aligned_score = _env_float(
        "HOLD_EXTENSION_MIN_SIGNAL_SCORE",
        float(profile.get("hold_extension_min_signal_score", 0.20)),
    )
    min_green = _env_float(
        "HOLD_EXTENSION_MIN_PNL_PCT",
        float(profile.get("hold_extension_min_pnl_pct", 0.05)),
    )
    extension_minutes = _env_int(
        "HOLD_EXTENSION_MINUTES",
        int(profile.get("hold_extension_minutes", 30)),
    )
    max_extensions = _env_int(
        "HOLD_EXTENSION_MAX_COUNT",
        int(profile.get("hold_extension_max_count", 2)),
    )
    extensions_used = int(trade.get("hold_extension_count") or 0)
    weakened = current_score < fade_score or current_score < entry_score * 0.5

    decision = {
        "ticker": ticker,
        "pnl_pct": round(pnl_pct, 4),
        "entry_directional_score": round(entry_score, 4),
        "current_directional_score": round(current_score, 4),
        "extensions_used": extensions_used,
    }

    if pnl_pct <= 0 and weakened:
        decision["decision"] = "exit_losing_or_flat_weakened"
        log_event("INFO", "hold_deadline_exit", decision)
        return "time_exit"

    if 0 < pnl_pct < min_green and weakened:
        decision["decision"] = "exit_small_green_momentum_faded"
        log_event("INFO", "hold_deadline_exit", decision)
        return "time_exit"

    # Only attempt swing promotion if signal is still aligned (not faded)
    if pnl_pct > 0 and current_score >= aligned_score and signal_result.get("composite_score") is not None:
        if _try_promote_to_swing(ticker, trade, current_price, profile):
            decision["decision"] = "promoted_to_swing"
            log_event("INFO", "hold_deadline_promoted", decision)
            return None

    if pnl_pct >= min_green and current_score >= aligned_score and extensions_used < max_extensions:
        state._open_trades[ticker]["hold_minutes"] = int(trade.get("hold_minutes") or 0) + extension_minutes
        state._open_trades[ticker]["hold_extension_count"] = extensions_used + 1
        # Capture momentum baseline for decay tracking on first extension
        if extensions_used == 0:
            state._open_trades[ticker]["peak_directional_score"] = round(current_score, 4)
        hold_decision = dict(trade.get("hold_decision_json") or {})
        hold_decision["deadline_extension"] = {
            **decision,
            "decision": "extend_aligned_winner",
            "extension_minutes": extension_minutes,
            "new_hold_minutes": state._open_trades[ticker]["hold_minutes"],
        }
        state._open_trades[ticker]["hold_decision_json"] = hold_decision
        save_open_trade(ticker, state._open_trades[ticker])
        log_event("INFO", "hold_deadline_extended", hold_decision["deadline_extension"])
        return None

    decision["decision"] = "exit_deadline_no_edge"
    log_event("INFO", "hold_deadline_exit", decision)
    return "time_exit"


def _check_momentum_exit(ticker: str, trade: dict, profile: dict) -> Optional[str]:
    """
    Per-cycle momentum health check for extended intraday trades.
    Uses only the intra-cycle signal cache — zero extra API calls.

    Single signal: peak decay — directional score dropped >40% from its
    peak since extension was granted. Other signals (VWAP recross, volume
    fade, score persistence) are deferred until validated by trade data.
    """
    cached = state._signal_cache.get(ticker)
    if cached is None:
        return None

    side = trade.get("side", "BUY")
    current_score = _directional_score(side, float(cached[1].get("composite_score") or 0))

    peak_score = float(trade.get("peak_directional_score") or current_score)
    if current_score > peak_score:
        state._open_trades[ticker]["peak_directional_score"] = round(current_score, 4)
    elif peak_score > 0.10 and current_score < peak_score * 0.60:
        log_event("INFO", "momentum_exit_triggered", {
            "ticker": ticker,
            "peak_score": round(peak_score, 4),
            "current_score": round(current_score, 4),
        })
        return "momentum_peak_decay"

    return None


def _close_trade(ticker: str, trade: dict, exit_price: float, exit_reason: str):
    """Close a position and record the trade outcome for learning."""
    cancel_results = []
    protective_cancel = _cancel_protective_stop_order(trade)
    if protective_cancel:
        log_event("INFO", "protective_stop_cancelled_for_manual_exit", {
            "ticker": ticker,
            "result": protective_cancel,
        })

    cancel_results = _cancel_bracket_orders_for_manual_exit(ticker, trade)
    if cancel_results:
        log_event("INFO", "open_orders_cancelled_for_exit", {
            "ticker": ticker,
            "exit_reason": exit_reason,
            "results": cancel_results[:4],
        })

    result = close_position(ticker)
    close_error = result.get("error")
    if close_error:
        if "position not found" in str(close_error).lower():
            # Position was closed by broker-side protection — try to recover fill price.
            recovered = _recover_protective_stop_fill(trade) or _recover_bracket_fill(trade)
            if recovered:
                exit_price  = recovered["exit_price"]
                exit_reason = recovered["exit_reason"]
                close_error = None
                result      = {"order_id": recovered.get("close_order_id")}
                log_event("INFO", "bracket_fill_recovered", {
                    "ticker":       ticker,
                    "exit_price":   exit_price,
                    "exit_reason":  exit_reason,
                    "close_order":  recovered.get("close_order_id"),
                })
                # Fall through to normal trade recording below
            else:
                close_open_trade_record(ticker, "stale_no_position")
                if ticker in state._open_trades:
                    del state._open_trades[ticker]
                log_event("WARN", "stale_open_trade_unrecoverable", {
                    "ticker":      ticker,
                    "exit_reason": exit_reason,
                    "error":       close_error,
                    "note":        "Position not found and bracket fill could not be recovered.",
                })
                return
        else:
            log_event("WARN", "close_position_failed_recording_estimate", {
                "ticker": ticker,
                "error": close_error,
                "exit_reason": exit_reason,
            })
            return
    entry_price = trade["entry_price"]
    pnl_pct     = (exit_price - entry_price) / entry_price * 100
    if trade["side"] == "SELL":
        pnl_pct = -pnl_pct

    size_eur    = float(trade.get("executed_size_eur") or trade.get("size_eur") or 0)
    size_usd    = float(trade.get("executed_size_usd") or trade.get("size_usd") or _eur_to_usd(size_eur))
    if size_eur <= 0:
        size_eur = float(trade.get("intended_size_eur") or 0)
        size_usd = _eur_to_usd(size_eur)
    if size_eur <= 0:
        log_event("WARN", "trade_close_missing_exposure", {
            "ticker": ticker,
            "exit_reason": exit_reason,
        })
        size_eur = 1.0
        size_usd = _eur_to_usd(size_eur)
    slippage    = size_eur * 0.0008   # Alpaca = $0 commission
    llm_cost    = 0.002
    net_pnl_pct = pnl_pct - (slippage + llm_cost) / size_eur * 100

    now_close = datetime.utcnow()
    hold_minutes_actual = int((now_close - trade["entry_time"]).total_seconds() / 60)
    hold_days_actual    = max(0, int((now_close - trade["entry_time"]).total_seconds() // 86400))

    # PDT tracking: same-day round trips on BUY trades
    entry_dt = trade.get("entry_time") or now_close
    if entry_dt.date() == now_close.date() and trade.get("side") == "BUY":
        _record_day_trade(ticker)
        day_trade_count = _count_day_trades_5d()
        log_event("INFO", "day_trade_recorded", {
            "ticker": ticker, "count_5d": day_trade_count,
        })
        if day_trade_count >= 3:
            _check_pdt_warning(ticker, day_trade_count)

    trade_record = {
        "ticker":          ticker,
        "side":            trade["side"],
        "entry_price":     round(entry_price, 4),
        "exit_price":      round(exit_price, 4),
        "quantity":        trade.get("quantity"),
        "submitted_qty":   trade.get("submitted_qty") or trade.get("quantity"),
        "implied_qty":     trade.get("implied_qty"),
        "stop_price":      round(float(trade.get("stop_price") or 0), 4),
        "take_profit_price": round(float(trade.get("take_profit_price") or 0), 4),
        "size_eur":        round(size_eur, 2),
        "size_usd":        round(size_usd, 2),
        "intended_size_eur": round(float(trade.get("intended_size_eur") or size_eur), 2),
        "executed_size_eur": round(size_eur, 2),
        "executed_size_usd": round(size_usd, 2),
        "bracket_floor_qty_loss_pct": trade.get("bracket_floor_qty_loss_pct"),
        "pnl_pct":         round(pnl_pct, 4),
        "net_pnl_pct":     round(net_pnl_pct, 4),
        "pnl_eur":         round(net_pnl_pct / 100 * size_eur, 2),
        "entry_time":      trade["entry_time"].isoformat() + "Z",
        "exit_time":       now_close.isoformat() + "Z",
        "hold_minutes":    hold_minutes_actual,
        "hold_days_actual": hold_days_actual,
        "exit_reason":     exit_reason,
        "exit_trigger":    exit_reason,
        "regime":          trade["regime"],
        "macro_regime":    trade.get("macro_regime"),
        "macro_multiplier": trade.get("macro_multiplier"),
        "composite_score": trade["composite_score"],
        "llm_conviction":  trade["llm_conviction"],
        "llm_rationale":   trade["llm_rationale"],
        "signals_json":    trade["signals_json"],
        "exposure_direction": trade.get("exposure_direction") or _exposure_direction(ticker, trade["side"]),
        "strategy_family": trade.get("strategy_family"),
        "regime_debug_json": trade.get("regime_debug_json") or {},
        "dip_type":        trade.get("dip_type"),
        "sizing_json":     trade.get("sizing_json"),
        "mean_reversion_trade": bool(trade.get("mean_reversion_trade")),
        "swing_trade":     bool(trade.get("swing_trade") or trade.get("horizon") == "swing"),
        "promoted_to_swing": bool(trade.get("promoted_to_swing")),
        "promoted_at":     trade.get("promoted_at"),
        "initial_horizon": trade.get("initial_horizon") or trade.get("horizon") or state.HORIZON,
        "swing_conviction": trade.get("swing_conviction"),
        "swing_reasons":   trade.get("swing_reasons") or [],
        "stop_multiplier": trade.get("stop_multiplier"),
        "trailing_stop_price": trade.get("trailing_stop_price"),
        "highest_price_since_entry": trade.get("highest_price_since_entry"),
        "daily_reeval_count": int(trade.get("daily_reeval_count") or 0),
        "hold_extension_count": int(trade.get("hold_extension_count") or 0),
        "hold_decision_json": trade.get("hold_decision_json"),
        "protective_stop_order_id": trade.get("protective_stop_order_id"),
        "order_id":        trade.get("order_id"),
        "client_order_id": trade.get("client_order_id"),
        "close_order_id":  result.get("order_id"),
        "close_error":     close_error,
        "commission_eur":  0.0,
        "slippage_eur":    round(slippage, 4),
        "llm_cost_eur":    llm_cost,
        "risk_profile":    state.PROFILE.get("_name", "moderate"),
        "horizon":         trade.get("horizon") or state.HORIZON,
        "atr_at_entry":    trade.get("atr_pct"),
        "stop_pct_used":   round(float(trade.get("stop_pct") or state.PROFILE.get("stop_loss_pct", 2.5)), 4),
        "r_multiple":      round(net_pnl_pct / max(float(trade.get("stop_pct") or state.PROFILE.get("stop_loss_pct", 2.5)), 0.1), 4),
        "setup_grade":     trade.get("setup_grade"),
        "partial_exit_done": bool(trade.get("partial_exit_done")),
        "entry_tranche_count": int(trade.get("entry_tranche_count") or 1),
        # Phase 1: runner trail + breakeven
        "breakeven_stop_set":        bool(trade.get("breakeven_stop_set")),
        "runner_trail_update_count": int(trade.get("runner_trail_update_count") or 0),
        "runner_trail_last_update_at": trade.get("runner_trail_last_update_at"),
        # Phase 2: hold score
        "hold_score_latest": trade.get("hold_score_latest"),
        "hold_score_min":  trade.get("hold_score_min"),
        "hold_score_max":  trade.get("hold_score_max"),
        "trim_done":       bool(trade.get("trim_done")),
    }

    insert_trade(trade_record)
    close_open_trade_record(ticker, exit_reason)
    del state._open_trades[ticker]

    # Update learning engine
    attributions = attribute_signals(trade_record)
    if state._learning_engine:
        state._learning_engine.update(attributions, trade["regime"])
        weights = state._learning_engine.all_weights()
        recent_count = sum(1 for _ in get_recent_trades(days=90))
        save_signal_weights(
            regime="global",
            weights=weights["global"],
            trade_count=recent_count,
            trigger="trade_update"
        )
        save_signal_weights(
            regime=trade["regime"],
            weights=weights.get(trade["regime"], weights["global"]),
            trade_count=recent_count,
            trigger="trade_update"
        )

    log_event("TRADE", "trade_closed", {
        "ticker": ticker, "net_pnl_pct": round(net_pnl_pct, 3),
        "exit_reason": exit_reason, "hold_min": trade_record["hold_minutes"]
    })
