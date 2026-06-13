"""
backend/runtime/helpers.py
Stateless query helpers used by run_signal_cycle and other agent entry points.

All shared mutable state (_signal_cache, PROFILE, IS_PAPER_TRADING) is accessed
via backend.runtime.state so mutations/reassignments propagate correctly.
No scalar agent-module globals are used here (those stay in agent.py).
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

import backend.runtime.state as state
from backend.runtime.env import _env_int
from backend.signals.engine import compute_all_signals
from backend.broker.alpaca import get_account, get_positions
from backend.learning.engine import RegimeAwareWeightEngine, build_weight_engine_from_trades
from database.client import (
    get_recent_trades, get_latest_weights, get_logs,
)


# ---------------------------------------------------------------------------
# Signal cache
# ---------------------------------------------------------------------------

def _get_cached_signals(ticker: str, weights: dict, regime_state) -> dict:
    """Return signals from cache if fresh, otherwise fetch and cache."""
    now = datetime.now(timezone.utc)
    cached = state._signal_cache.get(ticker)
    if cached is not None:
        age = (now - cached[0]).total_seconds()
        if age < state._SIGNAL_CACHE_TTL_SECONDS:
            return cached[1]
    # Evict all expired entries while we're here (keeps dict bounded to TICKERS set)
    expired = [k for k, v in state._signal_cache.items()
               if (now - v[0]).total_seconds() >= state._SIGNAL_CACHE_TTL_SECONDS]
    for k in expired:
        state._signal_cache.pop(k, None)
    result = compute_all_signals(ticker, weights, regime_state=regime_state)
    state._signal_cache[ticker] = (now, result)
    return result


# ---------------------------------------------------------------------------
# Learning engine factory
# ---------------------------------------------------------------------------

def _init_learning_engine() -> RegimeAwareWeightEngine:
    """Load latest weights from DB or use profile priors."""
    saved = get_latest_weights("global")
    trades = get_recent_trades(days=_env_int("LEARNING_LOOKBACK_DAYS", 120))
    replayable = [t for t in trades if t.get("signals_json") and t.get("net_pnl_pct") is not None]
    if replayable:
        return build_weight_engine_from_trades(state.PROFILE["signal_weights"], replayable)
    priors = saved if saved else state.PROFILE["signal_weights"]
    return RegimeAwareWeightEngine(priors)


# ---------------------------------------------------------------------------
# Portfolio state snapshot
# ---------------------------------------------------------------------------

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
            # Only count signal-driven intraday entries (entry.py path always sets
            # mean_reversion_trade). Swing/dip entries and stale-cleanup orders come
            # from orders.py which never sets this field, so they appear as None here
            # and must be excluded — otherwise pre-market swing orders inflate the
            # count and prematurely exhaust the ranging_regime daily cap.
            and (l.get("detail") or {}).get("mean_reversion_trade") is not None
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


def _get_portfolio_state() -> dict:
    account   = get_account()
    if "error" in account:
        return {"broker_error": account["error"], "equity": 0, "cash": 0, "positions": []}
    positions = get_positions()
    equity    = account.get("portfolio_value", 100.0)
    cash      = account.get("cash", 100.0)
    fx_rate   = float(os.getenv("EURUSD_RATE", "1.08") or "1.08")
    unrealized_pl_usd = sum(float(p.get("unrealized_pl") or 0) for p in positions)
    net_market_value_usd = sum(float(p.get("market_value") or 0) for p in positions)
    gross_market_value_usd = sum(abs(float(p.get("market_value") or 0)) for p in positions)

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
    start_usd = start_eur * fx_rate
    drawdown  = max(0.0, (start_usd - equity) / start_usd * 100)

    return {
        "equity":       round(equity, 2),
        "cash":         round(cash, 2),
        "equity_eur":   round(equity / fx_rate, 2),
        "cash_eur":     round(cash / fx_rate, 2),
        "fx_rate":      fx_rate,
        "broker_equity_usd": account.get("alpaca_actual_usd"),
        "broker_cash_usd": account.get("alpaca_cash_usd"),
        "buying_power_usd": account.get("buying_power"),
        "trading_blocked": bool(account.get("trading_blocked")),
        "account_blocked": bool(account.get("account_blocked")),
        "capital_ceiling_eur": account.get("capital_ceiling_eur"),
        "capital_ceiling_usd": account.get("capital_ceiling_usd"),
        "unrealized_pnl_usd": round(unrealized_pl_usd, 2),
        "unrealized_pnl_eur": round(unrealized_pl_usd / fx_rate, 2),
        "net_market_value_usd": round(net_market_value_usd, 2),
        "gross_market_value_usd": round(gross_market_value_usd, 2),
        "cash_pct":     round(cash / equity * 100, 1) if equity > 0 else 100.0,
        "positions":    positions,
        "vix":          round(vix, 1),
        "drawdown_today": round(drawdown, 3),
        "trades_today": _count_trades_today(),
        "consecutive_losses": _count_consecutive("loss"),
        "consecutive_wins":   _count_consecutive("win"),
    }


# ---------------------------------------------------------------------------
# Runtime config check
# ---------------------------------------------------------------------------

def _missing_runtime_config() -> list[str]:
    required = [
        "ALPACA_API_KEY",
        "ALPACA_SECRET_KEY",
        "GROQ_API_KEY",
        "SUPABASE_URL",
        "SUPABASE_ANON_KEY",
        "SUPABASE_SERVICE_KEY",
    ]
    if not state.IS_PAPER_TRADING and os.getenv("ENABLE_LIVE_TRADING", "").strip().lower() != "true":
        required.append("ENABLE_LIVE_TRADING")
    return [key for key in required if not os.getenv(key)]
