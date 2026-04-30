"""
backend/metrics/performance.py
Performance metrics computed from a list of trade dicts from Supabase.
All functions return None / empty on insufficient or zero-division inputs.
"""
import math
from typing import Optional


def _chronological(trades: list) -> list:
    return sorted(trades, key=lambda t: str(t.get("created_at") or ""))


def compute_expectancy(trades: list) -> Optional[float]:
    """Expected return per trade = win_rate × avg_win − loss_rate × avg_loss."""
    if not trades:
        return None
    wins   = [t.get("net_pnl_pct", 0) or 0 for t in trades if (t.get("net_pnl_pct") or 0) > 0]
    losses = [abs(t.get("net_pnl_pct", 0) or 0) for t in trades if (t.get("net_pnl_pct") or 0) <= 0]
    if not wins and not losses:
        return None
    n          = len(trades)
    win_rate   = len(wins) / n
    loss_rate  = len(losses) / n
    avg_win    = sum(wins) / len(wins) if wins else 0.0
    avg_loss   = sum(losses) / len(losses) if losses else 0.0
    return round(win_rate * avg_win - loss_rate * avg_loss, 4)


def compute_profit_factor(trades: list) -> Optional[float]:
    """Gross profit / gross loss. Returns None when no losses exist yet."""
    if not trades:
        return None
    gross_profit = sum(t.get("net_pnl_pct", 0) or 0 for t in trades if (t.get("net_pnl_pct") or 0) > 0)
    gross_loss   = abs(sum(t.get("net_pnl_pct", 0) or 0 for t in trades if (t.get("net_pnl_pct") or 0) < 0))
    if gross_loss == 0:
        return None
    return round(gross_profit / gross_loss, 3)


def compute_sharpe_ratio(trades: list, risk_free_rate: float = 0.0) -> Optional[float]:
    """
    Annualized Sharpe ratio on per-trade returns.
    Annualizes by √252 assuming roughly one trade per trading day.
    """
    if len(trades) < 2:
        return None
    trades = _chronological(trades)
    returns = [t.get("net_pnl_pct", 0) or 0 for t in trades]
    n       = len(returns)
    mean_r  = sum(returns) / n
    var     = sum((r - mean_r) ** 2 for r in returns) / (n - 1)
    std_r   = math.sqrt(var)
    if std_r == 0:
        return None
    return round((mean_r - risk_free_rate) / std_r * math.sqrt(252), 3)


def compute_calmar_ratio(trades: list) -> Optional[float]:
    """Annualized return / max drawdown (in pct units)."""
    if not trades:
        return None
    trades = _chronological(trades)
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

    total_return      = cumulative[-1]
    annualized_return = total_return * 252 / n
    return round(annualized_return / max_dd, 3)


def compute_rolling_sharpe(trades: list, window: int = 20) -> list:
    """
    Rolling Sharpe (window trades) across the full trade list.
    Returns list of dicts: {trade_index, sharpe, created_at}.
    """
    if len(trades) < window:
        return []
    trades = _chronological(trades)
    returns = [t.get("net_pnl_pct", 0) or 0 for t in trades]
    result  = []
    for i in range(window - 1, len(returns)):
        window_rets = returns[i - window + 1 : i + 1]
        mean_r = sum(window_rets) / window
        var    = sum((r - mean_r) ** 2 for r in window_rets) / (window - 1)
        std_r  = math.sqrt(var)
        sharpe = mean_r / std_r * math.sqrt(window) if std_r > 0 else 0.0
        result.append({
            "trade_index": i,
            "sharpe":      round(sharpe, 3),
            "created_at":  trades[i].get("created_at"),
        })
    return result


def compute_signal_attribution(trades: list) -> dict:
    """
    Average (signal_score × trade_pnl) per signal across all trades.
    Positive value = signal tended to precede profitable trades.
    """
    if not trades:
        return {}

    totals: dict = {}
    counts: dict = {}

    for trade in trades:
        pnl          = trade.get("net_pnl_pct", 0) or 0
        signals_json = trade.get("signals_json") or {}
        if not isinstance(signals_json, dict):
            continue
        for sig_name, sig_data in signals_json.items():
            score = (
                sig_data.get("score", 0) or 0
                if isinstance(sig_data, dict)
                else float(sig_data) if isinstance(sig_data, (int, float)) else 0.0
            )
            totals[sig_name] = totals.get(sig_name, 0.0) + score * pnl
            counts[sig_name] = counts.get(sig_name, 0) + 1

    return {
        sig: round(totals[sig] / counts[sig], 4)
        for sig in totals
        if counts.get(sig, 0) > 0
    }


def compute_r_multiples(trades: list) -> dict:
    """
    R-multiple = net_pnl_pct / initial_risk_pct.
    Falls back to raw pct when entry/stop prices are unavailable.
    """
    if not trades:
        return {"avg_r": None, "r_values": [], "positive_r": 0, "negative_r": 0}

    r_values = []
    for trade in trades:
        pnl   = trade.get("net_pnl_pct", 0) or 0
        entry = trade.get("entry_price") or 0
        stop  = trade.get("stop_price") or 0
        if entry > 0 and stop > 0:
            risk_pct = abs(entry - stop) / entry * 100
            r_values.append(pnl / risk_pct if risk_pct > 0 else pnl)
        else:
            r_values.append(pnl)

    avg_r      = sum(r_values) / len(r_values)
    positive_r = sum(1 for r in r_values if r > 0)
    negative_r = sum(1 for r in r_values if r <= 0)

    return {
        "avg_r":      round(avg_r, 3),
        "r_values":   [round(r, 3) for r in r_values],
        "positive_r": positive_r,
        "negative_r": negative_r,
    }
