"""
backend/execution/orders.py
Order submission helpers: ATR-based stop sizing, intraday price fetch, and
the main horizon order builder (_submit_horizon_order).

Depends on:
  - stdlib + yfinance + math
  - backend.runtime.env      (env helpers)
  - backend.runtime.state    (mutable _open_trades)
  - backend.execution.common (pure helpers)
  - backend.market.sector    (_exposure_direction)
  - backend.signals.engine   (compute_atr, detect_regime)
  - backend.broker.alpaca    (submit_market_order, compute_position_size)
  - database.client          (save_open_trade, log_event)
"""
from __future__ import annotations
import math
from datetime import datetime

import backend.runtime.state as state
from backend.runtime.env import _env_float, _env_value, _eur_to_usd, _eurusd_rate
from backend.execution.common import (
    _trading_capital, _cap_short_notional,
    _strategy_family, _regime_debug_payload,
)
from backend.market.sector import _exposure_direction
from database.client import save_open_trade, log_event


# ---------------------------------------------------------------------------
# Price helpers
# ---------------------------------------------------------------------------

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
    from backend.signals.engine import compute_atr
    atr_data = compute_atr(ticker)
    atr_pct = atr_data.get("atr_pct")
    if atr_pct:
        return max(0.5, min(12.0, float(atr_pct) * multiplier)), atr_data
    return fallback, atr_data


# ---------------------------------------------------------------------------
# Horizon order builder
# ---------------------------------------------------------------------------

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
    regime_state=None,
    atr_data: dict = None,
    sizing_json: dict = None,
    signal_id=None,
    order_ref=None,
) -> dict:
    from backend.signals.engine import detect_regime
    from backend.broker.alpaca import submit_market_order, compute_position_size

    capital_base = _trading_capital(portfolio_state["equity"])
    regime_state = regime_state or detect_regime()
    sizing = sizing_json or compute_position_size(
        ticker, capital_base, profile, conviction, atr_data or {}, regime_state
    )
    size_eur = sizing["size_eur"] * size_multiplier
    if side.upper() == "SELL":
        size_eur = _cap_short_notional(size_eur, capital_base, profile)
    max_notional = _env_float(
        "MAX_NOTIONAL_PER_TRADE_EUR",
        profile.get("max_trade_notional_eur", size_eur),
    )
    size_eur = min(size_eur, max_notional)
    intended_size_eur = float(size_eur or 0)
    sizing["size_eur"] = round(size_eur, 2)

    current_price = _current_daily_price(ticker)
    if not current_price:
        log_event("WARN", "price_unavailable", {"ticker": ticker, "horizon": horizon})
        return {"error": "price_unavailable"}

    size_usd = _eur_to_usd(size_eur)
    sizing["size_usd"] = round(size_usd, 2)
    qty = size_usd / current_price
    use_bracket_orders = _env_value("USE_BRACKET_ORDERS", "true").lower() != "false"
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
        log_event("INFO", "bracket_floor_preflight_block", {
            "ticker": ticker,
            "horizon": horizon,
            "size_eur": round(size_eur, 2),
            "size_usd": round(size_usd, 2),
            "current_price": round(current_price, 4),
            "implied_qty": round(qty, 6),
            "floor_qty": floor_qty,
            "reason": "bracket_floor_would_waste_trade",
        })
        return {"error": "bracket_floor_would_waste_trade", "ticker": ticker}
    take_profit_pct = profile.get("take_profit_pct", profile["stop_loss_pct"] * 1.2)
    order = submit_market_order(
        ticker          = ticker,
        side            = side.lower(),
        qty             = round(qty, 6),
        stop_loss_pct   = stop_loss_pct,
        take_profit_pct = take_profit_pct,
        current_price   = current_price,
        signal_id       = signal_id,
        order_ref       = order_ref,
    )
    if "error" in order:
        log_event("ERROR", "order_failed", {
            "ticker": ticker,
            "horizon": horizon,
            "error": order["error"],
            "client_order_id": order.get("client_order_id"),
        })
        return order

    submitted_qty = float(order.get("qty") or floor_qty or round(qty, 6))
    executed_size_usd = submitted_qty * current_price
    executed_size_eur = executed_size_usd / _eurusd_rate()
    sizing["submitted_qty"] = round(submitted_qty, 6)
    sizing["executed_size_usd"] = round(executed_size_usd, 2)
    sizing["executed_size_eur"] = round(executed_size_eur, 2)

    if side.upper() == "BUY":
        stop_price = current_price * (1 - stop_loss_pct / 100)
        take_profit_price = current_price * (1 + take_profit_pct / 100)
    else:
        stop_price = current_price * (1 + stop_loss_pct / 100)
        take_profit_price = current_price * (1 - take_profit_pct / 100)

    signal_context = {
        "signals": signals_json or {},
        "macro_regime": macro_regime,
        "macro_multiplier": macro_multiplier,
    }
    exposure_direction = _exposure_direction(ticker, side)
    strategy_family = _strategy_family(
        ticker, side, regime, signal_context,
        horizon=horizon, mean_reversion_trade=False,
    )
    record = {
        "entry_time": datetime.utcnow(),
        "entry_price": current_price,
        "quantity": submitted_qty,
        "submitted_qty": submitted_qty,
        "implied_qty": round(qty, 6),
        "stop_price": stop_price,
        "take_profit_price": take_profit_price,
        "hold_minutes": hold_minutes or 0,
        "hold_days": hold_days or 0,
        "size_eur": executed_size_eur,
        "size_usd": executed_size_usd,
        "intended_size_eur": intended_size_eur,
        "executed_size_eur": executed_size_eur,
        "executed_size_usd": executed_size_usd,
        "bracket_floor_qty_loss_pct": bracket_floor_qty_loss_pct,
        "atr_pct": sizing.get("atr_pct") or (atr_data or {}).get("atr_pct"),
        "atr_raw": (atr_data or {}).get("atr_raw"),
        "stop_pct": stop_loss_pct,
        "stop_multiplier": sizing.get("stop_multiplier"),
        "side": side.upper(),
        "composite_score": composite_score,
        "signals_json": signals_json or {},
        "regime": regime,
        "exposure_direction": exposure_direction,
        "strategy_family": strategy_family,
        "regime_debug_json": _regime_debug_payload(regime_state, signal_context),
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
        "client_order_id": order.get("client_order_id"),
    }
    state._open_trades[ticker] = record
    save_open_trade(ticker, record)

    log_event("TRADE", "order_submitted", {
        "ticker": ticker,
        "side": side.upper(),
        "horizon": horizon,
        "size_eur": round(executed_size_eur, 2),
        "intended_size_eur": round(intended_size_eur, 2),
        "submitted_qty": round(submitted_qty, 6),
        "implied_qty": round(qty, 6),
        "bracket_floor_qty_loss_pct": bracket_floor_qty_loss_pct,
        "conviction": conviction,
        "composite": composite_score,
        "order_class": order.get("order_class"),
        "client_order_id": order.get("client_order_id"),
        "rationale": rationale,
        "dip_type": dip_type,
        "sizing": sizing,
        "exposure_direction": exposure_direction,
        "strategy_family": strategy_family,
    })
    return order
