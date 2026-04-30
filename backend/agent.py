"""
backend/agent.py
Main agent loop. Runs on a schedule, ties together:
signals → risk gate → EV check → LLM decision → execution → learning → logging
"""
import os
import time
import asyncio
from datetime import datetime, timedelta
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

from config.risk_profiles        import get_profile
from backend.signals.engine      import (compute_all_signals, compute_swing_score,
                                          detect_regime, detect_macro_regime,
                                          compute_atr, latest_macro_headlines,
                                          scan_for_macro_shock)
from backend.broker.alpaca       import (get_account, get_positions, submit_market_order,
                                          close_position, pre_trade_gate, compute_position_size,
                                          scan_for_extreme_dips)
from backend.learning.engine     import (RegimeAwareWeightEngine, attribute_signals,
                                          compute_expected_value, get_effective_profile,
                                          generate_weekly_insights, llm_signal_decision,
                                          build_weight_engine_from_trades)
from database.client             import (insert_trade, insert_signal, get_recent_trades,
                                          save_signal_weights, get_latest_weights,
                                          save_snapshot, save_learning, log_event, get_logs,
                                          save_open_trade, get_open_trade_records,
                                          close_open_trade_record)

def _env_value(key: str, default: str) -> str:
    value = os.getenv(key)
    if value is None or not value.strip():
        return default
    return value.strip()


def _env_int(key: str, default: int) -> int:
    try:
        return int(_env_value(key, str(default)))
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(_env_value(key, str(default)))
    except ValueError:
        return default


TICKERS  = [t.strip().upper() for t in _env_value("TICKER_UNIVERSE", "SPY,QQQ,GLD").split(",") if t.strip()]
SWING_TICKERS = [t.strip().upper() for t in _env_value("SWING_TICKERS", "").split(",") if t.strip()]
PROFILE  = get_profile(_env_value("RISK_PROFILE", "moderate"))
HORIZON  = _env_value("INVESTMENT_HORIZON", "short")
LLM_HOUR_LIMIT = _env_int("LLM_CALLS_PER_HOUR_LIMIT", 20)
IS_PAPER_TRADING = _env_value("ALPACA_PAPER", "true").lower() != "false"

# Global learning engine (persists in memory between cycles)
_learning_engine: Optional[RegimeAwareWeightEngine] = None
_llm_calls_this_hour = 0
_llm_hour_reset      = datetime.utcnow()
_open_trades         = {}   # {ticker: {entry_price, entry_time, stop_price, hold_minutes, ...}}
_swing_trades        = {}   # {ticker: {entry_price, entry_time, hold_days, ...}}
_last_shock_refresh  = None
_last_shock_result   = {
    "shock_detected": False,
    "classification": "NORMAL",
    "affected_sectors": [],
    "direction": "mixed",
    "reason": "not_scanned",
}


def _allows_intraday() -> bool:
    return HORIZON in {"short", "intraday", "both"}


def _allows_swing() -> bool:
    return HORIZON in {"mid", "swing", "both"}


def _open_position_tickers(portfolio_state: dict) -> set[str]:
    return {str(p.get("ticker", "")).upper() for p in portfolio_state.get("positions", [])}


def _should_run_swing_recheck(now: datetime = None) -> bool:
    """Run swing re-evaluation once in the configured market-open window."""
    if not _allows_swing():
        return False
    now = now or datetime.utcnow()
    hour = _env_int("SWING_REEVAL_UTC_HOUR", 14)
    minute = _env_int("SWING_REEVAL_UTC_MINUTE", 0)
    window = _env_int("SWING_REEVAL_WINDOW_MINUTES", 5)
    current = now.hour * 60 + now.minute
    target = hour * 60 + minute
    return 0 <= current - target < window


def _init_learning_engine() -> RegimeAwareWeightEngine:
    """Load latest weights from DB or use profile priors."""
    saved = get_latest_weights("global")
    trades = get_recent_trades(days=_env_int("LEARNING_LOOKBACK_DAYS", 120))
    replayable = [t for t in trades if t.get("signals_json") and t.get("net_pnl_pct") is not None]
    if replayable:
        return build_weight_engine_from_trades(PROFILE["signal_weights"], replayable)
    priors = saved if saved else PROFILE["signal_weights"]
    return RegimeAwareWeightEngine(priors)


def _get_portfolio_state() -> dict:
    account   = get_account()
    if "error" in account:
        return {"broker_error": account["error"], "equity": 0, "cash": 0, "positions": []}
    positions = get_positions()
    equity    = account.get("portfolio_value", 100.0)
    cash      = account.get("cash", 100.0)

    # VIX
    try:
        import yfinance as yf
        vix_df = yf.download("^VIX", period="1d", interval="1h",
                             progress=False, auto_adjust=True)
        vix = float(vix_df["Close"].iloc[-1].item()) if not vix_df.empty else 20.0
    except Exception:
        vix = 20.0

    # Drawdown always measured against STARTING_CAPITAL_EUR converted to USD.
    # This ensures the circuit breaker fires at the correct EUR loss amount
    # regardless of the Alpaca paper account's $100k default.
    start_eur = float(os.getenv("STARTING_CAPITAL_EUR", "3000"))
    fx_rate   = float(os.getenv("EURUSD_RATE", "1.08") or "1.08")
    start_usd = start_eur * fx_rate
    drawdown  = max(0.0, (start_usd - equity) / start_usd * 100)

    return {
        "equity":       round(equity, 2),
        "cash":         round(cash, 2),
        "cash_pct":     round(cash / equity * 100, 1) if equity > 0 else 100.0,
        "positions":    positions,
        "vix":          round(vix, 1),
        "drawdown_today": round(drawdown, 3),
        "trades_today": _count_trades_today(),
        "consecutive_losses": _count_consecutive("loss"),
        "consecutive_wins":   _count_consecutive("win"),
    }


def _count_trades_today() -> int:
    trades = get_recent_trades(days=1)
    today  = datetime.utcnow().date()
    closed_count = sum(1 for t in trades
                       if t.get("created_at", "")[:10] == str(today))
    try:
        trade_logs = get_logs(level="TRADE", limit=200)
        submitted_count = sum(
            1 for l in trade_logs
            if l.get("event") == "order_submitted"
            and (l.get("logged_at") or "")[:10] == str(today)
        )
        return max(closed_count, submitted_count)
    except Exception:
        return closed_count


def _count_consecutive(outcome: str) -> int:
    trades = get_recent_trades(days=7)
    count  = 0
    for t in trades:
        pnl = t.get("net_pnl_pct", 0) or 0
        is_win = pnl > 0
        if outcome == "win" and is_win:
            count += 1
        elif outcome == "loss" and not is_win:
            count += 1
        else:
            break
    return count


def _can_call_llm() -> bool:
    global _llm_calls_this_hour, _llm_hour_reset
    now = datetime.utcnow()
    if (now - _llm_hour_reset).seconds >= 3600:
        _llm_calls_this_hour = 0
        _llm_hour_reset      = now
    return _llm_calls_this_hour < LLM_HOUR_LIMIT


def _record_llm_call():
    global _llm_calls_this_hour
    _llm_calls_this_hour += 1


def _send_telegram_alert(text: str) -> bool:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return False
    try:
        import requests
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=10,
        )
        return resp.ok
    except Exception:
        return False


def _refresh_macro_shock_if_needed() -> dict:
    global _last_shock_refresh, _last_shock_result
    now = datetime.utcnow()
    if _last_shock_refresh and (now - _last_shock_refresh).total_seconds() < 15 * 60:
        return _last_shock_result

    headlines = latest_macro_headlines(limit_per_ticker=5)
    shock_result = scan_for_macro_shock(headlines)
    _last_shock_refresh = now
    _last_shock_result = shock_result

    if shock_result.get("shock_detected"):
        log_event("SIGNAL", "macro_shock_detected", shock_result)
        _send_telegram_alert(
            "Macro shock detected\n"
            f"Classification: {shock_result.get('classification')}\n"
            f"Direction: {shock_result.get('direction')}\n"
            f"Affected: {', '.join(shock_result.get('affected_sectors') or [])}\n"
            f"Reason: {shock_result.get('reason')}"
        )
    elif "unavailable" in str(shock_result.get("reason", "")):
        log_event("WARN", "macro_shock_scan_unavailable", shock_result)
    return shock_result


def _missing_runtime_config() -> list[str]:
    required = [
        "ALPACA_API_KEY",
        "ALPACA_SECRET_KEY",
        "GROQ_API_KEY",
        "SUPABASE_URL",
        "SUPABASE_ANON_KEY",
        "SUPABASE_SERVICE_KEY",
    ]
    if not IS_PAPER_TRADING and os.getenv("ENABLE_LIVE_TRADING", "").strip().lower() != "true":
        required.append("ENABLE_LIVE_TRADING")
    return [key for key in required if not os.getenv(key)]


def _apply_execution_overrides(profile: dict) -> dict:
    p = profile.copy()
    if IS_PAPER_TRADING:
        for key, value in p.get("paper_overrides", {}).items():
            p[key] = value
    return p


def _trading_capital(equity: float) -> float:
    raw = os.getenv("TRADING_CAPITAL_EUR") or os.getenv("STARTING_CAPITAL_EUR")
    if raw and raw.strip():
        try:
            return min(float(raw), equity)
        except ValueError:
            pass
    return equity


def _deterministic_action(composite: float) -> str:
    return "BUY" if composite > 0 else "SELL"


def _parse_dt(value):
    if isinstance(value, datetime):
        return value
    if not value:
        return datetime.utcnow()
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return datetime.utcnow()


def _hydrate_open_trades():
    for record in get_open_trade_records():
        ticker = record.get("ticker")
        if not ticker or ticker in _open_trades:
            continue
        _open_trades[ticker] = {
            "entry_time": _parse_dt(record.get("entry_time") or record.get("created_at")),
            "entry_price": float(record.get("entry_price") or 0),
            "stop_price": float(record.get("stop_price") or 0),
            "take_profit_price": float(record.get("take_profit_price") or 0),
            "hold_minutes": int(record.get("hold_minutes") or 30),
            "hold_days": int(record.get("hold_days") or 0),
            "horizon": record.get("horizon") or "short",
            "size_eur": float(record.get("size_eur") or 0),
            "side": record.get("side", "BUY"),
            "composite_score": float(record.get("composite_score") or 0),
            "signals_json": record.get("signals_json") or {},
            "regime": record.get("regime") or "ranging",
            "macro_regime": record.get("macro_regime"),
            "macro_multiplier": float(record.get("macro_multiplier") or 1.0),
            "dip_type": record.get("dip_type"),
            "sizing_json": record.get("sizing_json") or {},
            "mean_reversion_trade": bool(record.get("mean_reversion_trade") or False),
            "swing_trade": bool(record.get("swing_trade") or False),
            "llm_conviction": float(record.get("llm_conviction") or 0),
            "llm_rationale": record.get("llm_rationale") or "",
            "order_id": record.get("order_id"),
        }


# ── Core cycle ────────────────────────────────────────────────────────────────

def run_signal_cycle():
    """Main cycle: compute signals → gate → decide → execute."""
    global _learning_engine

    missing_config = _missing_runtime_config()
    if missing_config:
        log_event("ERROR", "runtime_config_missing", {
            "missing": missing_config,
            "hint": "Set these as GitHub Actions secrets before the agent can trade.",
        })
        return

    if _learning_engine is None:
        _learning_engine = _init_learning_engine()

    portfolio_state = _get_portfolio_state()
    if portfolio_state.get("broker_error"):
        log_event("ERROR", "broker_account_unavailable", {
            "error": portfolio_state["broker_error"],
        })
        return
    regime_state    = detect_regime()
    regime          = regime_state.intraday_regime
    shock_result    = _refresh_macro_shock_if_needed()
    macro_regime, macro_meta = detect_macro_regime(return_meta=True)
    effective_profile = _apply_execution_overrides(
        get_effective_profile(PROFILE, portfolio_state)
    )
    weights          = _learning_engine.get_weights(regime)
    recent_trades    = get_recent_trades(days=30)
    _hydrate_open_trades()

    log_event("INFO", "cycle_start", {
        "regime": regime, "equity": portfolio_state["equity"],
        "vix": portfolio_state["vix"], "tickers": TICKERS,
        "horizon": HORIZON, "macro_regime": macro_regime,
        "macro_meta": macro_meta,
        "regime_state": regime_state.to_dict(),
        "shock_result": shock_result,
    })

    if not TICKERS:
        log_event("ERROR", "no_tickers_configured", {
            "hint": "Set TICKER_UNIVERSE or leave it unset to use SPY,QQQ,GLD"
        })
        return

    if _allows_intraday():
        for ticker in TICKERS:
            try:
                _process_ticker(ticker, regime, weights, effective_profile,
                                portfolio_state, recent_trades,
                                regime_state, shock_result)
            except Exception as e:
                log_event("ERROR", f"ticker_error_{ticker}", {"error": str(e)})

    _run_dip_buy_scan(TICKERS, portfolio_state, macro_regime, effective_profile, regime_state)

    if _should_run_swing_recheck():
        run_swing_cycle(
            portfolio_state=portfolio_state,
            profile=effective_profile,
            regime=regime,
            regime_state=regime_state,
            macro_regime=macro_regime,
        )

    # Check open trades for stop-loss / time exit
    _check_exits(portfolio_state, effective_profile)

    # Save portfolio snapshot
    _save_snapshot(portfolio_state, regime)


def _process_ticker(ticker, regime, weights, profile, portfolio_state, recent_trades,
                    regime_state, shock_result):
    """Signal → gate → EV → LLM → order."""
    # 1. Compute signals
    signal_result = compute_all_signals(
        ticker, weights, regime_state=regime_state, shock_result=shock_result
    )
    composite     = signal_result["composite_score"]
    signals_snap  = signal_result["signals"]
    atr_data       = signal_result.get("atr_data") or {}
    news_headline = (signals_snap.get("news_sentiment", {})
                    .get("meta", {}).get("latest_headline", ""))

    # 2. Pre-trade gate (hard rules)
    capital_base = _trading_capital(portfolio_state["equity"])
    pre_size = compute_position_size(ticker, capital_base, profile, 0.7, atr_data, regime_state)
    size_eur = pre_size["size_eur"]
    gate_ok, gate_reason = pre_trade_gate(
        ticker, _deterministic_action(composite).lower(), size_eur, composite, profile, portfolio_state
    )

    # 3. Log signal to DB
    insert_signal({
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
        "earnings_days":          signals_snap.get("earnings_proximity", {}).get("meta", {}).get("days_to_earnings"),
        "earnings_mult":          signals_snap.get("earnings_proximity", {}).get("meta", {}).get("earnings_multiplier", 1.0),
        "macro_regime":           signal_result.get("macro_regime"),
        "macro_multiplier":       signal_result.get("macro_multiplier", 1.0),
        "regime_bull_bear":       signal_result.get("regime_bull_bear"),
        "shock_detected":         signal_result.get("shock_detected", False),
        "shock_classification":   signal_result.get("shock_classification"),
        "regime":                 regime,
        "vix":                    portfolio_state["vix"],
        "gated":                  not gate_ok,
        "gate_reason":            gate_reason if not gate_ok else None,
        "llm_called":             False,
    })

    if not gate_ok:
        log_event("INFO", "trade_gated", {
            "ticker": ticker,
            "composite": composite,
            "reason": gate_reason,
        })
        return

    # 4. EV check
    ev_result = compute_expected_value(composite, size_eur, recent_trades, regime)
    if ev_result["decision"] == "block":
        log_event("INFO", "ev_blocked", {"ticker": ticker, **ev_result})
        return

    # 5. LLM decision (gated by hourly limit)
    if not _can_call_llm():
        log_event("WARN", "llm_limit_hit", {"ticker": ticker})
        return

    llm_result = llm_signal_decision(ticker, composite, regime, news_headline, profile)
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
        log_event("INFO", "llm_hold_veto", {
            "ticker": ticker,
            "composite": composite,
            "rationale": llm_result.get("rationale", ""),
        })
        return
    if suggested_action in {"BUY", "SELL"} and suggested_action != action:
        log_event("INFO", "llm_direction_conflict", {
            "ticker": ticker,
            "composite": composite,
            "deterministic_action": action,
            "llm_action": suggested_action,
            "rationale": llm_result.get("rationale", ""),
        })
        return

    if conviction < profile["min_conviction"]:
        log_event("INFO", "conviction_below_threshold", {
            "ticker": ticker,
            "conviction": conviction,
            "min_conviction": profile["min_conviction"],
        })
        return

    # 6. Size and submit order
    sizing = compute_position_size(ticker, capital_base, profile, conviction, atr_data, regime_state)
    final_size = sizing["size_eur"]
    max_notional = _env_float(
        "MAX_NOTIONAL_PER_TRADE_EUR",
        profile.get("max_trade_notional_eur", final_size),
    )
    final_size = min(final_size, max_notional)
    sizing["size_eur"] = round(final_size, 2)

    import yfinance as yf
    bar = yf.download(ticker, period="1d", interval="1m",
                      progress=False, auto_adjust=True)
    if bar.empty:
        log_event("WARN", "price_unavailable", {"ticker": ticker})
        return
    current_price = float(bar["Close"].squeeze().iloc[-1])
    qty = final_size / current_price

    raw_hold_minutes = llm_result.get("hold_minutes", 30)
    try:
        hold_minutes = int(raw_hold_minutes)
    except (TypeError, ValueError):
        hold_minutes = 30
    mean_reversion_trade = bool(signal_result.get("mean_reversion_signal"))
    if mean_reversion_trade:
        hold_minutes = 2880
    else:
        hold_minutes = max(
            int(profile.get("min_hold_minutes", 1)),
            min(int(profile.get("max_hold_minutes", 60)), hold_minutes),
        )

    stop_loss_pct = sizing.get("stop_pct") or float(llm_result.get("stop_loss_pct", profile["stop_loss_pct"]))
    if mean_reversion_trade:
        raw_atr_pct = atr_data.get("atr_pct")
        if raw_atr_pct:
            stop_loss_pct = max(stop_loss_pct, round(float(raw_atr_pct) * 2.0, 3))

    order = submit_market_order(
        ticker       = ticker,
        side         = action.lower(),
        qty          = round(qty, 6),
        stop_loss_pct= stop_loss_pct,
        take_profit_pct= profile.get("take_profit_pct", profile["stop_loss_pct"] * 1.2),
        current_price= current_price,
    )

    if "error" in order:
        log_event("ERROR", "order_failed", {"ticker": ticker, "error": order["error"]})
        return

    # Track open trade for exit monitoring
    if action == "BUY":
        stop_price = current_price * (1 - stop_loss_pct / 100)
        take_profit_price = current_price * (1 + profile.get("take_profit_pct", 2.0) / 100)
    else:
        stop_price = current_price * (1 + stop_loss_pct / 100)
        take_profit_price = current_price * (1 - profile.get("take_profit_pct", 2.0) / 100)

    _open_trades[ticker] = {
        "entry_time":    datetime.utcnow(),
        "entry_price":   current_price,
        "quantity":      order.get("qty", round(qty, 6)),
        "stop_price":    stop_price,
        "take_profit_price": take_profit_price,
        "hold_minutes":  hold_minutes,
        "size_eur":      final_size,
        "side":          action,
        "composite_score": composite,
        "signals_json":  {k: {"score": v["score"]} for k, v in signals_snap.items()},
        "regime":        regime,
        "macro_regime":  signal_result.get("macro_regime"),
        "macro_multiplier": signal_result.get("macro_multiplier", 1.0),
        "horizon":       "short",
        "sizing_json":   sizing,
        "mean_reversion_trade": mean_reversion_trade,
        "swing_trade":   mean_reversion_trade,
        "llm_conviction": conviction,
        "llm_rationale": llm_result.get("rationale", ""),
        "order_id":      order.get("order_id"),
    }
    save_open_trade(ticker, _open_trades[ticker])

    log_event("TRADE", "order_submitted", {
        "ticker": ticker, "side": action,
        "size_eur": round(final_size, 2), "conviction": conviction,
        "composite": composite, "order_class": order.get("order_class"),
        "rationale": llm_result.get("rationale"),
        "sizing": sizing,
        "mean_reversion_trade": mean_reversion_trade,
    })


def _check_exits(portfolio_state, profile):
    """Check all open trades for stop-loss or time-based exit."""
    import yfinance as yf
    now = datetime.utcnow()

    for ticker, trade in list(_open_trades.items()):
        if trade.get("horizon") == "swing":
            continue
        try:
            bar = yf.download(ticker, period="1d", interval="1m",
                             progress=False, auto_adjust=True)
            if bar.empty:
                continue
            current_price = float(bar["Close"].squeeze().iloc[-1])
            entry_time    = trade["entry_time"]
            hold_elapsed  = (now - entry_time).total_seconds() / 60
            stop_price    = trade["stop_price"]
            take_profit_price = trade.get("take_profit_price")
            hold_target   = trade["hold_minutes"]

            exit_reason = None
            if trade["side"] == "SELL":
                if current_price >= stop_price:
                    exit_reason = "stop_loss"
                elif take_profit_price and current_price <= take_profit_price:
                    exit_reason = "take_profit"
            elif current_price <= stop_price:
                exit_reason = "stop_loss"
            elif take_profit_price and current_price >= take_profit_price:
                exit_reason = "take_profit"

            if hold_elapsed >= hold_target and exit_reason is None:
                exit_reason = "time_exit"

            if exit_reason:
                _close_trade(ticker, trade, current_price, exit_reason)
        except Exception as e:
            log_event("ERROR", f"exit_check_{ticker}", {"error": str(e)})


def _close_trade(ticker: str, trade: dict, exit_price: float, exit_reason: str):
    """Close a position and record the trade outcome for learning."""
    global _learning_engine

    result = close_position(ticker)
    close_error = result.get("error")
    if close_error:
        if "position not found" in str(close_error).lower():
            close_open_trade_record(ticker, "stale_no_position")
            if ticker in _open_trades:
                del _open_trades[ticker]
            log_event("WARN", "stale_open_trade_removed", {
                "ticker": ticker,
                "exit_reason": exit_reason,
                "error": close_error,
                "note": "No closed trade was recorded because Alpaca has no matching position.",
            })
            return
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

    size_eur    = trade["size_eur"]
    slippage    = size_eur * 0.0008   # Alpaca = $0 commission
    llm_cost    = 0.002
    net_pnl_pct = pnl_pct - (slippage + llm_cost) / size_eur * 100

    trade_record = {
        "ticker":          ticker,
        "side":            trade["side"],
        "entry_price":     round(entry_price, 4),
        "exit_price":      round(exit_price, 4),
        "quantity":        trade.get("quantity"),
        "stop_price":      round(float(trade.get("stop_price") or 0), 4),
        "take_profit_price": round(float(trade.get("take_profit_price") or 0), 4),
        "size_eur":        round(size_eur, 2),
        "pnl_pct":         round(pnl_pct, 4),
        "net_pnl_pct":     round(net_pnl_pct, 4),
        "pnl_eur":         round(net_pnl_pct / 100 * size_eur, 2),
        "hold_minutes":    int((datetime.utcnow() - trade["entry_time"]).total_seconds() / 60),
        "exit_reason":     exit_reason,
        "regime":          trade["regime"],
        "macro_regime":    trade.get("macro_regime"),
        "macro_multiplier": trade.get("macro_multiplier"),
        "composite_score": trade["composite_score"],
        "llm_conviction":  trade["llm_conviction"],
        "llm_rationale":   trade["llm_rationale"],
        "signals_json":    trade["signals_json"],
        "dip_type":        trade.get("dip_type"),
        "sizing_json":     trade.get("sizing_json"),
        "mean_reversion_trade": bool(trade.get("mean_reversion_trade")),
        "swing_trade":     bool(trade.get("swing_trade") or trade.get("horizon") == "swing"),
        "order_id":        trade.get("order_id"),
        "close_order_id":  result.get("order_id"),
        "close_error":     close_error,
        "commission_eur":  0.0,
        "slippage_eur":    round(slippage, 4),
        "llm_cost_eur":    llm_cost,
        "risk_profile":    PROFILE.get("_name", "moderate"),
        "horizon":         trade.get("horizon") or HORIZON,
    }

    insert_trade(trade_record)
    close_open_trade_record(ticker, exit_reason)
    del _open_trades[ticker]

    # Update learning engine
    attributions = attribute_signals(trade_record)
    if _learning_engine:
        _learning_engine.update(attributions, trade["regime"])
        weights = _learning_engine.all_weights()
        save_signal_weights(
            regime="global",
            weights=weights["global"],
            trade_count=sum(1 for _ in get_recent_trades(days=90)),
            trigger="trade_update"
        )
        save_signal_weights(
            regime=trade["regime"],
            weights=weights.get(trade["regime"], weights["global"]),
            trade_count=sum(1 for _ in get_recent_trades(days=90)),
            trigger="trade_update"
        )

    log_event("TRADE", "trade_closed", {
        "ticker": ticker, "net_pnl_pct": round(net_pnl_pct, 3),
        "exit_reason": exit_reason, "hold_min": trade_record["hold_minutes"]
    })


def _current_daily_price(ticker: str) -> float | None:
    try:
        import yfinance as yf
        bar = yf.download(ticker, period="5d", interval="1d",
                          progress=False, auto_adjust=True)
        if bar.empty:
            return None
        return float(bar["Close"].squeeze().iloc[-1])
    except Exception:
        return None


def _stop_pct_from_atr(ticker: str, multiplier: float, fallback: float) -> tuple[float, dict]:
    atr_data = compute_atr(ticker)
    atr_pct = atr_data.get("atr_pct")
    if atr_pct:
        return max(0.5, min(12.0, float(atr_pct) * multiplier)), atr_data
    return fallback, atr_data


def _submit_horizon_order(
    ticker: str,
    side: str,
    conviction: float,
    profile: dict,
    portfolio_state: dict,
    regime: str,
    horizon: str,
    stop_loss_pct: float,
    hold_days: int = None,
    hold_minutes: int = None,
    size_multiplier: float = 1.0,
    composite_score: float = 0.0,
    signals_json: dict = None,
    rationale: str = "",
    macro_regime: str = None,
    macro_multiplier: float = None,
    dip_type: str = None,
    regime_state = None,
    atr_data: dict = None,
    sizing_json: dict = None,
) -> dict:
    capital_base = _trading_capital(portfolio_state["equity"])
    regime_state = regime_state or detect_regime()
    sizing = sizing_json or compute_position_size(
        ticker, capital_base, profile, conviction, atr_data or {}, regime_state
    )
    size_eur = sizing["size_eur"] * size_multiplier
    max_notional = _env_float(
        "MAX_NOTIONAL_PER_TRADE_EUR",
        profile.get("max_trade_notional_eur", size_eur),
    )
    size_eur = min(size_eur, max_notional)
    sizing["size_eur"] = round(size_eur, 2)

    current_price = _current_daily_price(ticker)
    if not current_price:
        log_event("WARN", "price_unavailable", {"ticker": ticker, "horizon": horizon})
        return {"error": "price_unavailable"}

    qty = size_eur / current_price
    take_profit_pct = profile.get("take_profit_pct", profile["stop_loss_pct"] * 1.2)
    order = submit_market_order(
        ticker=ticker,
        side=side.lower(),
        qty=round(qty, 6),
        stop_loss_pct=stop_loss_pct,
        take_profit_pct=take_profit_pct,
        current_price=current_price,
    )
    if "error" in order:
        log_event("ERROR", "order_failed", {
            "ticker": ticker,
            "horizon": horizon,
            "error": order["error"],
        })
        return order

    if side.upper() == "BUY":
        stop_price = current_price * (1 - stop_loss_pct / 100)
        take_profit_price = current_price * (1 + take_profit_pct / 100)
    else:
        stop_price = current_price * (1 + stop_loss_pct / 100)
        take_profit_price = current_price * (1 - take_profit_pct / 100)

    record = {
        "entry_time": datetime.utcnow(),
        "entry_price": current_price,
        "quantity": order.get("qty", round(qty, 6)),
        "stop_price": stop_price,
        "take_profit_price": take_profit_price,
        "hold_minutes": hold_minutes or 0,
        "hold_days": hold_days or 0,
        "size_eur": size_eur,
        "side": side.upper(),
        "composite_score": composite_score,
        "signals_json": signals_json or {},
        "regime": regime,
        "macro_regime": macro_regime,
        "macro_multiplier": macro_multiplier,
        "horizon": horizon,
        "dip_type": dip_type,
        "sizing_json": sizing,
        "mean_reversion_trade": False,
        "swing_trade": horizon == "swing",
        "llm_conviction": conviction,
        "llm_rationale": rationale,
        "order_id": order.get("order_id"),
    }
    _open_trades[ticker] = record
    save_open_trade(ticker, record)

    log_event("TRADE", "order_submitted", {
        "ticker": ticker,
        "side": side.upper(),
        "horizon": horizon,
        "size_eur": round(size_eur, 2),
        "conviction": conviction,
        "composite": composite_score,
        "order_class": order.get("order_class"),
        "rationale": rationale,
        "dip_type": dip_type,
        "sizing": sizing,
    })
    return order


def _run_dip_buy_scan(tickers: list[str], portfolio_state: dict, macro_regime: str,
                      profile: dict, regime_state=None):
    opportunities = scan_for_extreme_dips(tickers, portfolio_state, macro_regime)
    log_event("SIGNAL", "extreme_dip_scan_complete", {
        "macro_regime": macro_regime,
        "tickers_scanned": len(tickers),
        "opportunities": len(opportunities),
    })

    open_tickers = _open_position_tickers(portfolio_state) | set(_open_trades.keys())
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
        )
        if "error" not in order:
            open_tickers.add(ticker)


def run_swing_cycle(portfolio_state: dict = None, profile: dict = None,
                    regime: str = None, macro_regime: str = None,
                    regime_state = None):
    """Daily swing re-evaluation and entry scan for SWING_TICKERS."""
    if not _allows_swing():
        return
    if not SWING_TICKERS:
        return

    portfolio_state = portfolio_state or _get_portfolio_state()
    if portfolio_state.get("broker_error"):
        log_event("ERROR", "swing_broker_account_unavailable", {
            "error": portfolio_state["broker_error"],
        })
        return

    profile = profile or _apply_execution_overrides(get_effective_profile(PROFILE, portfolio_state))
    regime_state = regime_state or detect_regime()
    regime = regime or regime_state.intraday_regime
    if macro_regime is None:
        macro_regime = detect_macro_regime()

    _hydrate_open_trades()
    open_tickers = _open_position_tickers(portfolio_state) | set(_open_trades.keys())
    log_event("INFO", "swing_cycle_start", {
        "tickers": SWING_TICKERS,
        "macro_regime": macro_regime,
        "open_tickers": sorted(open_tickers),
    })

    # Re-evaluate existing swing positions.
    for ticker, trade in list(_open_trades.items()):
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

    open_tickers = _open_position_tickers(portfolio_state) | set(_open_trades.keys())
    for ticker in SWING_TICKERS:
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
        )


def _save_snapshot(portfolio_state, regime):
    from database.client import get_snapshots
    snaps = get_snapshots(days=1)
    equity = portfolio_state["equity"]

    # Baseline is STARTING_CAPITAL_EUR converted to USD so the comparison is
    # apples-to-apples: equity (USD-capped) vs start_usd.
    raw_capital = os.getenv("STARTING_CAPITAL_EUR", "3000")
    fx_rate     = float(os.getenv("EURUSD_RATE", "1.08") or "1.08")
    start_equity_usd = float(raw_capital) * fx_rate if raw_capital and raw_capital.strip() else equity

    cum_pnl = (equity - start_equity_usd) / start_equity_usd * 100 if start_equity_usd else 0.0
    cum_pnl = max(-9999.0, min(9999.0, cum_pnl))

    save_snapshot({
        "total_value_eur":    equity,
        "cash_eur":           portfolio_state["cash"],
        "daily_pnl_pct":      -portfolio_state["drawdown_today"],
        "cumulative_pnl_pct": round(cum_pnl, 3),
        "drawdown_pct":       portfolio_state["drawdown_today"],
        "open_positions":     portfolio_state["positions"],
        "trades_today":       portfolio_state["trades_today"],
        "llm_calls_today":    _llm_calls_this_hour,
        "llm_cost_today":     round(_llm_calls_this_hour * 0.001, 4),
    })


# ── Weekly digest (called by scheduler) ──────────────────────────────────────

def run_weekly_digest():
    from database.client import get_recent_trades, save_learning
    trades = get_recent_trades(days=7)
    if not trades:
        return
    insights = generate_weekly_insights(trades)
    from datetime import date
    save_learning(
        week_start      = date.today(),
        insights        = insights,
        trades_analysed = len(trades)
    )
    log_event("LEARNING", "weekly_digest", {"insights": len(insights)})
    return insights


# ── Scheduler entry point ─────────────────────────────────────────────────────

def start_scheduler():
    from apscheduler.schedulers.blocking import BlockingScheduler
    import pytz

    scheduler = BlockingScheduler(timezone=pytz.utc)

    # Main signal cycle: every 5 minutes during market hours
    scheduler.add_job(run_signal_cycle, "cron",
                      day_of_week="mon-fri",
                      hour="14-20",          # 14-20 UTC = 9am-3pm EST
                      minute="*/5")

    # Weekly digest: Sunday evening
    scheduler.add_job(run_weekly_digest, "cron",
                      day_of_week="sun", hour=18, minute=0)

    log_event("INFO", "scheduler_started", {"tickers": TICKERS, "profile": PROFILE.get("_name")})
    print(f"Agent started | Profile: {PROFILE['display_name']} | Tickers: {TICKERS}")
    scheduler.start()


if __name__ == "__main__":
    start_scheduler()
