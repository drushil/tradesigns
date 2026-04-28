"""
backend/broker/alpaca.py
All broker interactions go through here.
Paper trading on Alpaca (free). Swap base URL to go live.
"""
import os
from datetime import datetime
from typing import Optional
from dotenv import load_dotenv

load_dotenv()


def _get_trading_client():
    from alpaca.trading.client import TradingClient
    return TradingClient(
        os.getenv("ALPACA_API_KEY"),
        os.getenv("ALPACA_SECRET_KEY"),
        paper=True
    )


def _get_data_client():
    from alpaca.data.historical import StockHistoricalDataClient
    return StockHistoricalDataClient(
        os.getenv("ALPACA_API_KEY"),
        os.getenv("ALPACA_SECRET_KEY")
    )


# ── Account & portfolio ───────────────────────────────────────────────────────

def get_account() -> dict:
    try:
        client  = _get_trading_client()
        account = client.get_account()
        return {
            "cash":           float(account.cash),
            "portfolio_value": float(account.portfolio_value),
            "equity":          float(account.equity),
            "buying_power":    float(account.buying_power),
            "currency":        account.currency,
            "status":          account.status,
        }
    except Exception as e:
        return {"error": str(e)}


def get_positions() -> list:
    try:
        client    = _get_trading_client()
        positions = client.get_all_positions()
        return [
            {
                "ticker":       p.symbol,
                "qty":          float(p.qty),
                "avg_entry":    float(p.avg_entry_price),
                "current_price": float(p.current_price),
                "market_value": float(p.market_value),
                "unrealized_pl": float(p.unrealized_pl),
                "unrealized_plpc": float(p.unrealized_plpc) * 100,
                "side":         p.side,
            }
            for p in positions
        ]
    except Exception as e:
        return []


def get_orders(status: str = "all", limit: int = 50) -> list:
    try:
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums   import QueryOrderStatus
        client = _get_trading_client()
        req    = GetOrdersRequest(status=QueryOrderStatus.ALL, limit=limit)
        orders = client.get_orders(req)
        return [
            {
                "id":          str(o.id),
                "ticker":      o.symbol,
                "side":        str(o.side),
                "qty":         float(o.qty or 0),
                "filled_qty":  float(o.filled_qty or 0),
                "filled_price": float(o.filled_avg_price or 0),
                "status":      str(o.status),
                "created_at":  str(o.created_at),
                "type":        str(o.order_type),
            }
            for o in orders
        ]
    except Exception as e:
        return []


# ── Order submission ──────────────────────────────────────────────────────────

def submit_market_order(ticker: str, side: str, qty: float,
                         stop_loss_pct: float = 2.0) -> dict:
    """
    Submits a market order with an immediate stop-loss bracket.
    side: 'buy' | 'sell'
    """
    try:
        from alpaca.trading.requests import MarketOrderRequest, TrailingStopOrderRequest
        from alpaca.trading.enums    import OrderSide, TimeInForce

        client = _get_trading_client()
        order_side = OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL

        req = MarketOrderRequest(
            symbol       = ticker,
            qty          = round(qty, 6),
            side         = order_side,
            time_in_force= TimeInForce.DAY,
        )
        order = client.submit_order(req)

        return {
            "order_id":   str(order.id),
            "ticker":     ticker,
            "side":       side,
            "qty":        float(order.qty or qty),
            "status":     str(order.status),
            "submitted_at": datetime.utcnow().isoformat(),
        }
    except Exception as e:
        return {"error": str(e), "ticker": ticker, "side": side}


def close_position(ticker: str) -> dict:
    """Closes an open position entirely via market order."""
    try:
        client = _get_trading_client()
        result = client.close_position(ticker)
        return {"status": "closed", "ticker": ticker, "order_id": str(result.id)}
    except Exception as e:
        return {"error": str(e), "ticker": ticker}


def close_all_positions() -> dict:
    """Emergency close-all for circuit breaker."""
    try:
        client = _get_trading_client()
        client.close_all_positions(cancel_orders=True)
        return {"status": "all_closed"}
    except Exception as e:
        return {"error": str(e)}


# ── Risk gate ─────────────────────────────────────────────────────────────────

def pre_trade_gate(ticker: str, side: str, size_eur: float,
                   composite_score: float, profile: dict,
                   portfolio_state: dict) -> tuple[bool, str]:
    """
    Hard rule checks before any order is submitted.
    Returns (allow: bool, reason: str).
    """
    drawdown = portfolio_state.get("drawdown_today", 0.0)
    vix      = portfolio_state.get("vix", 20.0)
    cash_pct = portfolio_state.get("cash_pct", 100.0)
    trades_today = portfolio_state.get("trades_today", 0)

    if drawdown >= profile["max_drawdown_pct"]:
        return False, f"max drawdown hit ({drawdown:.1f}% ≥ {profile['max_drawdown_pct']}%)"

    if vix > profile["vix_ceiling"]:
        return False, f"VIX too high ({vix:.0f} > {profile['vix_ceiling']})"

    if cash_pct < profile["cash_buffer_pct"]:
        return False, f"insufficient cash ({cash_pct:.1f}% < {profile['cash_buffer_pct']}%)"

    if abs(composite_score) < profile["min_signal_score"]:
        return False, f"signal below threshold ({composite_score:.3f} < {profile['min_signal_score']})"

    if ticker not in profile.get("allowed_instruments", []):
        return False, f"{ticker} not in allowed instruments"

    if trades_today >= profile.get("max_trades_per_day", 8):
        return False, f"daily trade limit reached ({trades_today})"

    return True, "pass"


def compute_position_size(total_capital: float, profile: dict,
                           conviction: float) -> float:
    """Returns position size in EUR."""
    base     = total_capital * profile["capital_per_trade_pct"] / 100
    scalar   = min(conviction / max(profile["min_conviction"], 0.01), 1.5)
    max_pos  = total_capital * profile["max_position_pct"] / 100
    return min(base * scalar, max_pos)
