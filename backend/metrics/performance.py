"""
backend/metrics/performance.py
Performance metrics computed from a list of trade dicts from Supabase.
All functions return None / empty on insufficient or zero-division inputs.
"""
import math
from typing import Optional


def _chronological(trades: list) -> list:
    return sorted(trades, key=lambda t: str(t.get("created_at") or ""))


def compute_expectancy(trades: list) -> Optional[dict]:
    """
    Expected return per trade = win_rate × avg_win − loss_rate × avg_loss.
    Target: > 0.15% per trade.
    Returns dict with full breakdown, or None on empty input.
    """
    if not trades:
        return None
    wins   = [t.get("net_pnl_pct", 0) or 0 for t in trades if (t.get("net_pnl_pct") or 0) > 0]
    losses = [abs(t.get("net_pnl_pct", 0) or 0) for t in trades if (t.get("net_pnl_pct") or 0) <= 0]
    if not wins and not losses:
        return None
    n         = len(trades)
    win_rate  = len(wins)   / n
    loss_rate = len(losses) / n
    avg_win   = sum(wins)   / len(wins)   if wins   else 0.0
    avg_loss  = sum(losses) / len(losses) if losses else 0.0
    expectancy = win_rate * avg_win - loss_rate * avg_loss
    return {
        "expectancy_pct": round(expectancy, 4),
        "win_rate":        round(win_rate,  4),
        "loss_rate":       round(loss_rate, 4),
        "avg_win_pct":     round(avg_win,   4),
        "avg_loss_pct":    round(avg_loss,  4),
        "sample_size":     n,
    }


def compute_profit_factor(trades: list) -> Optional[float]:
    """Gross profit / gross loss. Returns None when no losses exist yet."""
    if not trades:
        return None
    gross_profit = sum(t.get("net_pnl_pct", 0) or 0 for t in trades if (t.get("net_pnl_pct") or 0) > 0)
    gross_loss   = abs(sum(t.get("net_pnl_pct", 0) or 0 for t in trades if (t.get("net_pnl_pct") or 0) < 0))
    if gross_loss == 0:
        return None
    return round(gross_profit / gross_loss, 3)


def compute_sharpe_ratio(trades: list, risk_free_rate: float = 0.04) -> Optional[float]:
    """
    Annualised Sharpe ratio on per-trade returns.
    Annualises by √252 assuming roughly one trade per trading day.
    Default risk_free_rate = 4% (approximate ECB rate 2026).
    """
    if len(trades) < 2:
        return None
    trades  = _chronological(trades)
    returns = [t.get("net_pnl_pct", 0) or 0 for t in trades]
    n       = len(returns)
    mean_r  = sum(returns) / n
    var     = sum((r - mean_r) ** 2 for r in returns) / (n - 1)
    std_r   = math.sqrt(var)
    if std_r == 0:
        return None
    daily_rf = risk_free_rate / 252
    return round((mean_r - daily_rf) / std_r * math.sqrt(252), 3)


def compute_calmar_ratio(trades: list) -> Optional[float]:
    """Annualized return / max drawdown (in pct units)."""
    if not trades:
        return None
    trades  = _chronological(trades)
    returns = [t.get("net_pnl_pct", 0) or 0 for t in trades]
    n       = len(returns)

    cumulative, running = [], 0.0
    for r in returns:
        running += r
        cumulative.append(running)

    max_dd, peak = 0.0, float("-inf")
    for c in cumulative:
        if c > peak:
            peak = c
        max_dd = max(max_dd, peak - c)

    if max_dd == 0:
        return None

    annualized_return = cumulative[-1] * 252 / n
    return round(annualized_return / max_dd, 3)


def compute_rolling_sharpe(trades: list, window: int = 20) -> list:
    """
    Rolling Sharpe (window trades) across the full trade list.
    Returns list of dicts: {trade_index, sharpe, created_at, degradation_warning}.
    degradation_warning=True when rolling Sharpe drops below 0.5
    for 3 consecutive windows.
    """
    if len(trades) < window:
        return []
    trades  = _chronological(trades)
    returns = [t.get("net_pnl_pct", 0) or 0 for t in trades]
    result  = []
    consecutive_low = 0
    for i in range(window - 1, len(returns)):
        window_rets = returns[i - window + 1 : i + 1]
        mean_r = sum(window_rets) / window
        var    = sum((r - mean_r) ** 2 for r in window_rets) / (window - 1)
        std_r  = math.sqrt(var)
        sharpe = mean_r / std_r * math.sqrt(window) if std_r > 0 else 0.0
        if sharpe < 0.5:
            consecutive_low += 1
        else:
            consecutive_low = 0
        result.append({
            "trade_index":         i,
            "sharpe":              round(sharpe, 3),
            "created_at":          trades[i].get("created_at"),
            "degradation_warning": consecutive_low >= 3,
        })
    return result


def compute_signal_attribution(trades: list) -> list:
    """
    For each signal, compute its contribution to profitable vs unprofitable trades.
    Signals with score > 0.3 get +1 for winning trades, -1 for losing trades.
    Returns list sorted by net_attribution descending.
    """
    if not trades:
        return []

    totals: dict  = {}
    wins_on: dict = {}
    pnl_on: dict  = {}
    counts: dict  = {}

    for trade in trades:
        pnl          = trade.get("net_pnl_pct", 0) or 0
        is_win       = pnl > 0
        signals_json = trade.get("signals_json") or {}
        if not isinstance(signals_json, dict):
            continue
        for sig_name, sig_data in signals_json.items():
            score = (
                sig_data.get("score", 0) or 0
                if isinstance(sig_data, dict)
                else float(sig_data) if isinstance(sig_data, (int, float)) else 0.0
            )
            if abs(score) <= 0.3:
                continue
            totals[sig_name]  = totals.get(sig_name, 0)  + (1 if is_win else -1)
            wins_on[sig_name] = wins_on.get(sig_name, 0) + (1 if is_win else 0)
            pnl_on[sig_name]  = pnl_on.get(sig_name, 0)  + pnl
            counts[sig_name]  = counts.get(sig_name, 0)  + 1

    rows = []
    for sig in totals:
        n = counts[sig]
        rows.append({
            "signal":               sig,
            "net_attribution":      totals[sig],
            "trades_influenced":    n,
            "win_rate_when_active": round(wins_on[sig] / n, 3) if n > 0 else 0.0,
            "avg_pnl_when_active":  round(pnl_on[sig]  / n, 4) if n > 0 else 0.0,
        })
    return sorted(rows, key=lambda x: x["net_attribution"], reverse=True)


def compute_r_multiples(trades: list) -> dict:
    """
    R-multiple = net_pnl_pct / initial_risk_pct.
    Falls back to stop_loss_pct from trade record (default 2.5%) when prices unavailable.
    """
    if not trades:
        return {"avg_r": None, "median_r": None, "r_values": [], "positive_r": 0, "negative_r": 0}

    r_values = []
    for trade in trades:
        pnl   = trade.get("net_pnl_pct", 0) or 0
        entry = trade.get("entry_price") or 0
        stop  = trade.get("stop_price") or 0
        if entry > 0 and stop > 0:
            risk_pct = abs(entry - stop) / entry * 100
        else:
            risk_pct = float(trade.get("stop_pct_used") or 2.5)
        r_values.append(pnl / risk_pct if risk_pct > 0 else pnl)

    sorted_r   = sorted(r_values)
    mid        = len(sorted_r) // 2
    median_r   = sorted_r[mid] if len(sorted_r) % 2 else (sorted_r[mid - 1] + sorted_r[mid]) / 2
    avg_r      = sum(r_values) / len(r_values)
    positive_r = sum(1 for r in r_values if r > 0)
    negative_r = sum(1 for r in r_values if r <= 0)

    return {
        "avg_r":      round(avg_r,    3),
        "median_r":   round(median_r, 3),
        "r_values":   [round(r, 3) for r in r_values],
        "positive_r": positive_r,
        "negative_r": negative_r,
    }


def compute_strategy_health(trades: list) -> dict:
    """
    Aggregated GREEN / AMBER / RED / INSUFFICIENT_DATA assessment.

    GREEN:  expectancy > 0.15% AND profit_factor > 1.3 AND sharpe > 0.8 AND win_rate > 48%
    AMBER:  any one metric below threshold
    RED:    expectancy < 0 OR profit_factor < 1.0 OR rolling Sharpe declining 3+ windows
    INSUFFICIENT_DATA: fewer than 20 trades
    """
    MIN_TRADES = 20
    if len(trades) < MIN_TRADES:
        return {
            "status":  "INSUFFICIENT_DATA",
            "message": f"Need {MIN_TRADES} closed trades — have {len(trades)} so far.",
            "metrics": {},
            "issues":  [],
        }

    exp_data      = compute_expectancy(trades)
    profit_factor = compute_profit_factor(trades)
    sharpe        = compute_sharpe_ratio(trades)
    r_data        = compute_r_multiples(trades)
    rolling       = compute_rolling_sharpe(trades)

    expectancy_pct = exp_data["expectancy_pct"] if exp_data else None
    win_rate       = exp_data["win_rate"]        if exp_data else None
    avg_r          = r_data["avg_r"]             if r_data  else None

    degradation = any(p.get("degradation_warning") for p in rolling[-3:]) if rolling else False

    issues = []

    # RED conditions
    red = (
        (expectancy_pct is not None and expectancy_pct < 0)
        or (profit_factor is not None and profit_factor < 1.0)
        or degradation
    )

    if red:
        if expectancy_pct is not None and expectancy_pct < 0:
            issues.append(f"Negative expectancy ({expectancy_pct:+.3f}%) — strategy losing money on average")
        if profit_factor is not None and profit_factor < 1.0:
            issues.append(f"Profit factor {profit_factor:.2f} < 1.0 — losses exceed wins")
        if degradation:
            issues.append("Rolling Sharpe degrading for 3+ consecutive windows — strategy may have stopped working")
        status  = "RED"
        message = "Strategy is underperforming. Review signal weights and market regime."
    else:
        # GREEN conditions
        green = (
            expectancy_pct is not None and expectancy_pct > 0.15
            and profit_factor is not None and profit_factor > 1.3
            and sharpe is not None and sharpe > 0.8
            and win_rate is not None and win_rate > 0.48
        )
        if green:
            status  = "GREEN"
            message = "Strategy is healthy. All key metrics above threshold."
        else:
            status = "AMBER"
            if expectancy_pct is not None and expectancy_pct <= 0.15:
                issues.append(f"Expectancy {expectancy_pct:+.3f}% below 0.15% target")
            if profit_factor is not None and profit_factor <= 1.3:
                issues.append(f"Profit factor {profit_factor:.2f} below 1.3 target")
            if sharpe is not None and sharpe <= 0.8:
                issues.append(f"Sharpe {sharpe:.2f} below 0.8 target")
            if win_rate is not None and win_rate <= 0.48:
                issues.append(f"Win rate {win_rate:.1%} below 48% target")
            message = "Strategy is marginal. Monitor closely."

    return {
        "status":  status,
        "message": message,
        "metrics": {
            "expectancy_pct": expectancy_pct,
            "profit_factor":  profit_factor,
            "sharpe":         sharpe,
            "avg_r":          avg_r,
            "win_rate":       win_rate,
            "total_trades":   len(trades),
            "degradation_warning": degradation,
        },
        "issues": issues,
    }
