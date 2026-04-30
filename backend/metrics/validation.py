"""
Validation helpers for deciding whether strategy changes are actually improving.

These functions work from closed trade records already stored in Supabase. They
do not replace bar-by-bar backtesting, but they provide a walk-forward sanity
check for thresholds, signal weights, and EV gates before promoting changes.
"""
import math
from statistics import mean


def _pnl(trade: dict) -> float:
    return float(trade.get("net_pnl_pct") or 0.0)


def _chronological(trades: list) -> list:
    return sorted(trades, key=lambda t: str(t.get("created_at") or ""))


def summarize_trades(trades: list) -> dict:
    if not trades:
        return {
            "trade_count": 0,
            "win_rate": None,
            "expectancy_pct": None,
            "profit_factor": None,
            "max_drawdown_pct": None,
        }

    returns = [_pnl(t) for t in trades]
    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r <= 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))

    equity = peak = max_dd = 0.0
    for r in returns:
        equity += r
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)

    return {
        "trade_count": len(trades),
        "win_rate": round(len(wins) / len(trades), 4),
        "expectancy_pct": round(mean(returns), 4),
        "profit_factor": round(gross_profit / gross_loss, 4) if gross_loss else None,
        "max_drawdown_pct": round(max_dd, 4),
    }


def walk_forward_splits(trades: list, train_size: int = 40, test_size: int = 20) -> list:
    """
    Produces rolling train/test summaries from closed trades.

    Use this to check that a filter or learned weighting scheme holds up on data
    that came after the calibration window.
    """
    ordered = _chronological(trades)
    if len(ordered) < train_size + test_size:
        return []

    splits = []
    start = 0
    while start + train_size + test_size <= len(ordered):
        train = ordered[start:start + train_size]
        test = ordered[start + train_size:start + train_size + test_size]
        splits.append({
            "start_index": start,
            "train": summarize_trades(train),
            "test": summarize_trades(test),
        })
        start += test_size
    return splits


def evaluate_score_thresholds(trades: list, thresholds=None) -> list:
    """
    Retrospectively evaluates absolute composite-score thresholds.

    This is not a full backtest because it only sees trades that were actually
    taken, but it quickly exposes thresholds that look good only by chance.
    """
    thresholds = thresholds or [0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.50]
    rows = []
    for threshold in thresholds:
        selected = [
            t for t in trades
            if abs(float(t.get("composite_score") or 0.0)) >= threshold
        ]
        summary = summarize_trades(selected)
        summary["threshold"] = threshold
        rows.append(summary)
    return rows


def signal_information_coefficients(trades: list) -> dict:
    """
    Computes simple Pearson IC between each entry signal score and realized P&L.
    Positive IC means higher signal values tended to align with higher returns.
    """
    signal_values = {}
    returns = []

    for trade in trades:
        signals = trade.get("signals_json") or {}
        if not isinstance(signals, dict):
            continue
        returns.append(_pnl(trade))
        for name, payload in signals.items():
            score = payload.get("score", 0.0) if isinstance(payload, dict) else payload
            try:
                signal_values.setdefault(name, []).append(float(score or 0.0))
            except (TypeError, ValueError):
                signal_values.setdefault(name, []).append(0.0)

    result = {}
    for name, values in signal_values.items():
        n = min(len(values), len(returns))
        if n < 5:
            continue
        xs = values[:n]
        ys = returns[:n]
        mx = mean(xs)
        my = mean(ys)
        cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
        sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
        sy = math.sqrt(sum((y - my) ** 2 for y in ys))
        result[name] = round(cov / (sx * sy), 4) if sx and sy else 0.0
    return result
