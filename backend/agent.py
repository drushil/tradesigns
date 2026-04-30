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
from backend.signals.engine      import compute_all_signals, detect_regime
from backend.broker.alpaca       import (get_account, get_positions, submit_market_order,
                                          close_position, pre_trade_gate, compute_position_size)
from backend.learning.engine     import (RegimeAwareWeightEngine, attribute_signals,
                                          compute_expected_value, get_effective_profile,
                                          generate_weekly_insights, llm_signal_decision)
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
PROFILE  = get_profile(_env_value("RISK_PROFILE", "moderate"))
HORIZON  = _env_value("INVESTMENT_HORIZON", "short")
LLM_HOUR_LIMIT = _env_int("LLM_CALLS_PER_HOUR_LIMIT", 20)
IS_PAPER_TRADING = _env_value("ALPACA_PAPER", "true").lower() != "false"

# Global learning engine (persists in memory between cycles)
_learning_engine: Optional[RegimeAwareWeightEngine] = None
_llm_calls_this_hour = 0
_llm_hour_reset      = datetime.utcnow()
_open_trades         = {}   # {ticker: {entry_price, entry_time, stop_price, hold_minutes, ...}}


def _init_learning_engine() -> RegimeAwareWeightEngine:
    """Load latest weights from DB or use profile priors."""
    saved = get_latest_weights("global")
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

    # Drawdown today: compare to yesterday's equity snapshot
    from database.client import get_snapshots
    snaps = get_snapshots(days=2)
    prev_equity = snaps[1]["total_value_eur"] if len(snaps) >= 2 else equity
    drawdown = max(0, (prev_equity - equity) / prev_equity * 100)

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
            "hold_minutes": 30,
            "size_eur": float(record.get("size_eur") or 0),
            "side": record.get("side", "BUY"),
            "composite_score": float(record.get("composite_score") or 0),
            "signals_json": record.get("signals_json") or {},
            "regime": record.get("regime") or "ranging",
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
    regime          = detect_regime()
    effective_profile = _apply_execution_overrides(
        get_effective_profile(PROFILE, portfolio_state)
    )
    weights          = _learning_engine.get_weights(regime)
    recent_trades    = get_recent_trades(days=30)
    _hydrate_open_trades()

    log_event("INFO", "cycle_start", {
        "regime": regime, "equity": portfolio_state["equity"],
        "vix": portfolio_state["vix"], "tickers": TICKERS
    })

    if not TICKERS:
        log_event("ERROR", "no_tickers_configured", {
            "hint": "Set TICKER_UNIVERSE or leave it unset to use SPY,QQQ,GLD"
        })
        return

    for ticker in TICKERS:
        try:
            _process_ticker(ticker, regime, weights, effective_profile,
                            portfolio_state, recent_trades)
        except Exception as e:
            log_event("ERROR", f"ticker_error_{ticker}", {"error": str(e)})

    # Check open trades for stop-loss / time exit
    _check_exits(portfolio_state, effective_profile)

    # Save portfolio snapshot
    _save_snapshot(portfolio_state, regime)


def _process_ticker(ticker, regime, weights, profile, portfolio_state, recent_trades):
    """Signal → gate → EV → LLM → order."""
    # 1. Compute signals
    signal_result = compute_all_signals(ticker, weights)
    composite     = signal_result["composite_score"]
    signals_snap  = signal_result["signals"]
    news_headline = (signals_snap.get("news_sentiment", {})
                    .get("meta", {}).get("latest_headline", ""))

    # 2. Pre-trade gate (hard rules)
    capital_base = _trading_capital(portfolio_state["equity"])
    size_eur = compute_position_size(capital_base, profile, 0.7)
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
        "earnings_days":          signals_snap.get("earnings_proximity", {}).get("meta", {}).get("days_to_earnings"),
        "earnings_mult":          signals_snap.get("earnings_proximity", {}).get("meta", {}).get("earnings_multiplier", 1.0),
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
    suggested_action = llm_result.get("action", "HOLD")
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

    if conviction < profile["min_conviction"]:
        log_event("INFO", "conviction_below_threshold", {
            "ticker": ticker,
            "conviction": conviction,
            "min_conviction": profile["min_conviction"],
        })
        return

    # 6. Size and submit order
    final_size = compute_position_size(capital_base, profile, conviction)
    max_notional = _env_float(
        "MAX_NOTIONAL_PER_TRADE_EUR",
        profile.get("max_trade_notional_eur", final_size),
    )
    final_size = min(final_size, max_notional)

    import yfinance as yf
    bar = yf.download(ticker, period="1d", interval="1m",
                      progress=False, auto_adjust=True)
    if bar.empty:
        log_event("WARN", "price_unavailable", {"ticker": ticker})
        return
    current_price = float(bar["Close"].squeeze().iloc[-1])
    qty = final_size / current_price

    order = submit_market_order(
        ticker       = ticker,
        side         = action.lower(),
        qty          = round(qty, 6),
        stop_loss_pct= llm_result.get("stop_loss_pct", profile["stop_loss_pct"]),
        take_profit_pct= profile.get("take_profit_pct", profile["stop_loss_pct"] * 1.2),
        current_price= current_price,
    )

    if "error" in order:
        log_event("ERROR", "order_failed", {"ticker": ticker, "error": order["error"]})
        return

    # Track open trade for exit monitoring
    if action == "BUY":
        stop_price = current_price * (1 - profile["stop_loss_pct"] / 100)
        take_profit_price = current_price * (1 + profile.get("take_profit_pct", 2.0) / 100)
    else:
        stop_price = current_price * (1 + profile["stop_loss_pct"] / 100)
        take_profit_price = current_price * (1 - profile.get("take_profit_pct", 2.0) / 100)

    _open_trades[ticker] = {
        "entry_time":    datetime.utcnow(),
        "entry_price":   current_price,
        "stop_price":    stop_price,
        "take_profit_price": take_profit_price,
        "hold_minutes":  llm_result.get("hold_minutes", 30),
        "size_eur":      final_size,
        "side":          action,
        "composite_score": composite,
        "signals_json":  {k: {"score": v["score"]} for k, v in signals_snap.items()},
        "regime":        regime,
        "llm_conviction": conviction,
        "llm_rationale": llm_result.get("rationale", ""),
        "order_id":      order.get("order_id"),
    }
    save_open_trade(ticker, _open_trades[ticker])

    log_event("TRADE", "order_submitted", {
        "ticker": ticker, "side": action,
        "size_eur": round(final_size, 2), "conviction": conviction,
        "composite": composite, "order_class": order.get("order_class"),
        "rationale": llm_result.get("rationale")
    })


def _check_exits(portfolio_state, profile):
    """Check all open trades for stop-loss or time-based exit."""
    import yfinance as yf
    now = datetime.utcnow()

    for ticker, trade in list(_open_trades.items()):
        try:
            bar = yf.download(ticker, period="1d", interval="1m",
                             progress=False, auto_adjust=True)
            if bar.empty:
                continue
            current_price = float(bar["Close"].squeeze().iloc[-1])
            entry_time    = trade["entry_time"]
            hold_elapsed  = (now - entry_time).seconds / 60
            stop_price    = trade["stop_price"]
            hold_target   = trade["hold_minutes"]

            exit_reason = None
            if trade["side"] == "SELL":
                if current_price >= stop_price:
                    exit_reason = "stop_loss"
            elif current_price <= stop_price:
                exit_reason = "stop_loss"

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
        "size_eur":        round(size_eur, 2),
        "pnl_pct":         round(pnl_pct, 4),
        "net_pnl_pct":     round(net_pnl_pct, 4),
        "pnl_eur":         round(pnl_pct / 100 * size_eur, 2),
        "hold_minutes":    int((datetime.utcnow() - trade["entry_time"]).seconds / 60),
        "exit_reason":     exit_reason,
        "regime":          trade["regime"],
        "composite_score": trade["composite_score"],
        "llm_conviction":  trade["llm_conviction"],
        "llm_rationale":   trade["llm_rationale"],
        "signals_json":    trade["signals_json"],
        "commission_eur":  0.0,
        "slippage_eur":    round(slippage, 4),
        "llm_cost_eur":    llm_cost,
        "risk_profile":    PROFILE.get("_name", "moderate"),
        "horizon":         HORIZON,
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
            regime=trade["regime"],
            weights=weights.get(trade["regime"], weights["global"]),
            trade_count=sum(1 for _ in get_recent_trades(days=90)),
            trigger="trade_update"
        )

    log_event("TRADE", "trade_closed", {
        "ticker": ticker, "net_pnl_pct": round(net_pnl_pct, 3),
        "exit_reason": exit_reason, "hold_min": trade_record["hold_minutes"]
    })


def _save_snapshot(portfolio_state, regime):
    from database.client import get_snapshots
    snaps = get_snapshots(days=1)
    equity = portfolio_state["equity"]

    # Safely handle None, empty strings, or missing keys. If starting capital is
    # unset, anchor to current equity so a 100k Alpaca paper account does not
    # look like a 99,900% gain from the old 100 EUR default.
    raw_capital = os.getenv("STARTING_CAPITAL_EUR", "100")
    if not raw_capital or raw_capital.strip() == "":
        start_equity = equity
    else:
        start_equity = float(raw_capital)
    if snaps:
        start_equity = max(start_equity,
                           snaps[-1].get("total_value_eur", start_equity))

    cum_pnl = (equity - start_equity) / start_equity * 100 if start_equity else 0.0
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
