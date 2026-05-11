"""
backend/broker/alpaca.py
All broker interactions go through here.
Paper trading on Alpaca (free). Swap base URL to go live.
"""
from __future__ import annotations

import os
import math
from datetime import datetime, timezone
from typing import List, Optional
from dotenv import load_dotenv

load_dotenv()


def _get_trading_client():
    from alpaca.trading.client import TradingClient
    paper = os.getenv("ALPACA_PAPER", "true").strip().lower() != "false"
    if not paper and os.getenv("ENABLE_LIVE_TRADING", "").strip().lower() != "true":
        raise RuntimeError(
            "Live trading is disabled. Set ENABLE_LIVE_TRADING=true only after paper validation."
        )
    return TradingClient(
        os.getenv("ALPACA_API_KEY"),
        os.getenv("ALPACA_SECRET_KEY"),
        paper=paper
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

        max_capital_eur = float(os.getenv("MAX_CAPITAL_DEPLOYED_EUR", "3000") or "3000")
        fx_rate         = float(os.getenv("EURUSD_RATE", "1.08") or "1.08")
        max_capital_usd = max_capital_eur * fx_rate

        alpaca_actual_usd = float(account.portfolio_value)
        real_cash         = float(account.cash)

        effective_portfolio = min(alpaca_actual_usd, max_capital_usd)
        effective_cash      = min(real_cash, max_capital_usd)

        return {
            "cash":                round(effective_cash, 2),
            "portfolio_value":     round(effective_portfolio, 2),
            "equity":              round(effective_portfolio, 2),
            "buying_power":        round(min(float(account.buying_power), max_capital_usd), 2),
            "currency":            account.currency,
            "status":              str(account.status),
            "alpaca_actual_usd":   round(alpaca_actual_usd, 2),
            "capital_ceiling_eur": max_capital_eur,
            "capital_ceiling_usd": round(max_capital_usd, 2),
            "fx_rate_used":        fx_rate,
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


def get_order_by_id(order_id: str) -> dict:
    """
    Fetch a single order by ID including bracket legs.
    Used to recover exit price when Alpaca closes a bracket autonomously.
    """
    try:
        from alpaca.trading.requests import GetOrderByIdRequest
        client = _get_trading_client()
        order  = client.get_order_by_id(order_id, GetOrderByIdRequest(nested=True))
        legs   = []
        if hasattr(order, "legs") and order.legs:
            for leg in order.legs:
                legs.append({
                    "id":           str(leg.id),
                    "type":         str(leg.order_type),
                    "status":       str(leg.status),
                    "filled_price": float(leg.filled_avg_price or 0),
                    "filled_qty":   float(leg.filled_qty or 0),
                    "side":         str(leg.side),
                })
        return {
            "id":           str(order.id),
            "ticker":       str(order.symbol),
            "status":       str(order.status),
            "filled_price": float(order.filled_avg_price or 0),
            "filled_qty":   float(order.filled_qty or 0),
            "legs":         legs,
        }
    except Exception as e:
        return {"error": str(e)[:120]}


def cancel_order_by_id(order_id: str) -> dict:
    """Best-effort cancellation for parent or bracket-leg orders."""
    try:
        client = _get_trading_client()
        client.cancel_order_by_id(order_id)
        return {"status": "cancel_requested", "order_id": str(order_id)}
    except Exception as e:
        return {"error": str(e), "order_id": str(order_id)}


def cancel_open_orders_for_symbol(ticker: str) -> List[dict]:
    """Cancel all open orders for a ticker before a manual close."""
    try:
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus

        client = _get_trading_client()
        req = GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[ticker], limit=50)
        orders = client.get_orders(req)
        results = []
        for order in orders:
            order_id = str(order.id)
            try:
                client.cancel_order_by_id(order_id)
                results.append({"status": "cancel_requested", "order_id": order_id})
            except Exception as e:
                results.append({"error": str(e), "order_id": order_id})
        return results
    except Exception as e:
        return [{"error": str(e), "ticker": ticker}]


# ── Order submission ──────────────────────────────────────────────────────────

def _round_price(price: float) -> float:
    return round(price, 2) if price >= 1 else round(price, 4)


def submit_market_order(ticker: str, side: str, qty: float,
                         stop_loss_pct: float = 2.0,
                         take_profit_pct: float = 2.0,
                         current_price: float = None) -> dict:
    """
    Submits a market order with an immediate stop-loss bracket.
    side: 'buy' | 'sell'
    """
    try:
        from alpaca.trading.requests import (
            MarketOrderRequest, TakeProfitRequest, StopLossRequest
        )
        from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce

        client = _get_trading_client()
        order_side = OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL
        use_bracket = os.getenv("USE_BRACKET_ORDERS", "true").strip().lower() != "false"

        qty = math.floor(qty) if use_bracket else round(qty, 6)
        if qty <= 0:
            return {"error": "quantity below 1 share after bracket sizing", "ticker": ticker, "side": side}

        kwargs = {}
        if use_bracket:
            if not current_price or current_price <= 0:
                return {"error": "current_price required for bracket order", "ticker": ticker, "side": side}
            stop_loss_pct = float(stop_loss_pct)
            take_profit_pct = float(take_profit_pct)
            if side.lower() == "buy":
                take_profit_price = current_price * (1 + take_profit_pct / 100)
                stop_price = current_price * (1 - stop_loss_pct / 100)
            else:
                take_profit_price = current_price * (1 - take_profit_pct / 100)
                stop_price = current_price * (1 + stop_loss_pct / 100)
            kwargs = {
                "order_class": OrderClass.BRACKET,
                "take_profit": TakeProfitRequest(limit_price=_round_price(take_profit_price)),
                "stop_loss": StopLossRequest(stop_price=_round_price(stop_price)),
            }

        req = MarketOrderRequest(
            symbol       = ticker,
            qty          = qty,
            side         = order_side,
            time_in_force= TimeInForce.DAY,
            **kwargs,
        )
        order = client.submit_order(req)

        return {
            "order_id":   str(order.id),
            "ticker":     ticker,
            "side":       side,
            "qty":        float(order.qty or qty),
            "status":     str(order.status),
            "order_class": "bracket" if use_bracket else "market",
            "submitted_at": datetime.utcnow().isoformat(),
        }
    except Exception as e:
        return {"error": str(e), "ticker": ticker, "side": side}


def submit_stop_order(ticker: str, side: str, qty: float,
                      stop_price: float,
                      time_in_force: str = "gtc") -> dict:
    """
    Submit a standalone protective stop order.
    side is the closing side: 'sell' protects a long, 'buy' protects a short.
    """
    try:
        from alpaca.trading.requests import StopOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce

        qty = math.floor(float(qty or 0))
        if qty <= 0:
            return {"error": "quantity below 1 share after stop sizing", "ticker": ticker, "side": side}

        tif = TimeInForce.GTC if str(time_in_force).lower() == "gtc" else TimeInForce.DAY
        order_side = OrderSide.SELL if side.lower() == "sell" else OrderSide.BUY
        req = StopOrderRequest(
            symbol=ticker,
            qty=qty,
            side=order_side,
            time_in_force=tif,
            stop_price=_round_price(float(stop_price)),
        )
        order = _get_trading_client().submit_order(req)
        return {
            "order_id": str(order.id),
            "ticker": ticker,
            "side": side,
            "qty": float(order.qty or qty),
            "stop_price": _round_price(float(stop_price)),
            "status": str(order.status),
            "order_class": "protective_stop",
            "time_in_force": str(tif),
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


def close_partial_position(ticker: str, qty: float, side: str) -> dict:
    """
    Close a fractional part of an open position via a plain market order.
    side: 'sell' to reduce a long, 'buy' to reduce a short.
    """
    try:
        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce

        close_qty = round(qty, 6)
        if close_qty <= 0:
            return {"error": "partial_qty_zero", "ticker": ticker}
        client = _get_trading_client()
        req = MarketOrderRequest(
            symbol=ticker,
            qty=close_qty,
            side=OrderSide.SELL if side.lower() == "sell" else OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
        )
        order = client.submit_order(req)
        return {
            "order_id": str(order.id),
            "ticker": ticker,
            "side": side,
            "qty": float(order.qty or close_qty),
            "status": str(order.status),
        }
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

def _signal_score(signals: dict, name: str) -> float:
    try:
        return float((signals or {}).get(name, {}).get("score", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def _event_risk_intraday_probe_allowed(side: str, composite_score: float,
                                       profile: dict, market_regime: str = None,
                                       signals: dict = None,
                                       is_swing_candidate: bool = False) -> tuple[bool, str]:
    """
    Earnings/filing windows are overnight risk guards, not automatic bans.
    Allow only exceptional intraday momentum probes with reduced sizing.
    """
    if is_swing_candidate:
        return False, "swing entries blocked during event-risk window"

    if not profile.get("allow_event_risk_intraday_probes", True):
        return False, "event-risk intraday probes disabled"

    now_utc = datetime.now(timezone.utc)
    latest_entry_hour = int(profile.get("event_risk_latest_entry_utc_hour", 19))
    if now_utc.hour >= latest_entry_hour:
        return False, "too late for event-risk intraday probe"

    signals = signals or {}
    mean_reversion = _signal_score(signals, "mean_reversion")
    if mean_reversion >= float(profile.get("event_risk_mean_reversion_block_score", 0.5)):
        return False, "mean-reversion blocked during event-risk window"

    side = str(side or "").lower()
    min_score = float(profile.get("event_risk_probe_min_score", 0.32))
    min_macd = float(profile.get("event_risk_probe_min_macd", 0.35))
    min_tape = float(profile.get("event_risk_probe_min_tape", 0.25))
    min_rel = float(profile.get("event_risk_probe_min_relative_strength", 0.35))

    macd = _signal_score(signals, "macd_crossover")
    tape = _signal_score(signals, "tape_aggression")
    rel_strength = _signal_score(signals, "relative_strength")
    news = _signal_score(signals, "news_sentiment")
    vwap = _signal_score(signals, "vwap_deviation")
    market = str(market_regime or "").lower()

    if side == "buy":
        aligned = (
            composite_score >= min_score
            and macd >= min_macd
            and tape >= min_tape
            and rel_strength >= min_rel
            and news >= 0
            and vwap <= 0
            and market in {"bull", "transitioning", ""}
        )
    else:
        aligned = (
            composite_score <= -min_score
            and macd <= -min_macd
            and tape <= -min_tape
            and rel_strength <= -min_rel
            and news <= 0
            and vwap >= 0
            and market not in {"bull"}
        )

    if not aligned:
        return False, (
            "event-risk probe quality below threshold "
            f"(score={composite_score:.3f}, macd={macd:.2f}, tape={tape:.2f}, "
            f"rel={rel_strength:.2f}, vwap={vwap:.2f})"
        )

    return True, "event_risk_intraday_probe"


def pre_trade_gate(ticker: str, side: str, size_eur: float,
                   composite_score: float, profile: dict,
                   portfolio_state: dict,
                   market_regime: str = None,
                   signals: dict = None,
                   current_swing_count: int = 0,
                   is_swing_candidate: bool = False) -> tuple[bool, str]:
    """
    Hard rule checks before any order is submitted.
    Returns (allow: bool, reason: str).
    """
    drawdown = portfolio_state.get("drawdown_today", 0.0)
    vix      = portfolio_state.get("vix", 20.0)
    cash_pct = portfolio_state.get("cash_pct", 100.0)
    trades_today = portfolio_state.get("trades_today", 0)
    allowed = profile.get("allowed_instruments", [])
    open_tickers = {p.get("ticker") for p in portfolio_state.get("positions", [])}

    # Dominant-signal veto: block a BUY when any single signal is at floor (-0.8 or worse),
    # or a SELL when any single signal is at ceiling (+0.8 or better).
    # Prevents composite averaging from papering over a screaming counter-signal.
    _VETO_THRESHOLD = float(profile.get("dominant_signal_veto_threshold", 0.8))
    if signals and _VETO_THRESHOLD > 0:
        signal_scores = []
        for signal_name, value in signals.items():
            if not isinstance(value, dict):
                continue
            score = value.get("score", 0)
            if signal_name == "vwap_deviation":
                macd = float(signals.get("macd_crossover", {}).get("score", 0) or 0)
                tape = float(signals.get("tape_aggression", {}).get("score", 0) or 0)
                rel_strength = float(signals.get("relative_strength", {}).get("score", 0) or 0)
                news = float(signals.get("news_sentiment", {}).get("score", 0) or 0)
                bullish_breakout = (
                    side.lower() == "buy"
                    and score <= -_VETO_THRESHOLD
                    and macd > 0.15
                    and tape > 0
                    and rel_strength > 0
                    and news >= 0
                    and str(market_regime or "").lower() in {"bull", "transitioning", ""}
                )
                bearish_breakout = (
                    side.lower() == "sell"
                    and score >= _VETO_THRESHOLD
                    and macd < -0.15
                    and tape < 0
                    and rel_strength < 0
                    and news <= 0
                )
                if bullish_breakout or bearish_breakout:
                    continue
            signal_scores.append((signal_name, score))
        if side.lower() == "buy" and signal_scores:
            signal_name, worst = min(signal_scores, key=lambda item: item[1])
            if worst <= -_VETO_THRESHOLD:
                return False, f"dominant bearish signal veto: {signal_name}={worst:.2f}"
        elif side.lower() == "sell" and signal_scores:
            signal_name, best = max(signal_scores, key=lambda item: item[1])
            if best >= _VETO_THRESHOLD:
                return False, f"dominant bullish signal veto on short: {signal_name}={best:.2f}"

    # Max concurrent swing positions — enforced before any swing promotion
    if is_swing_candidate:
        max_concurrent = int(profile.get("max_concurrent_swings", 2))
        if current_swing_count >= max_concurrent:
            return False, f"max_concurrent_swings_reached ({current_swing_count}/{max_concurrent})"

    if allowed and ticker not in allowed and not profile.get("allow_individual_stocks", False):
        return False, f"{ticker} not allowed for profile"

    if ticker in open_tickers:
        return False, f"position already open for {ticker}"

    # Earnings proximity guard — block new entries within 2 days of a 10-Q/10-K filing
    try:
        from backend.earnings.scanner import get_cached_earnings_guard
        eg = get_cached_earnings_guard()
        info = eg.get(ticker, {})
        if info.get("blocked"):
            days = info.get("days_to_filing")
            event_ok, event_reason = _event_risk_intraday_probe_allowed(
                side, composite_score, profile,
                market_regime=market_regime,
                signals=signals,
                is_swing_candidate=is_swing_candidate,
            )
            if event_ok:
                return True, f"{event_reason}: filing in {days}d ({info.get('filing_date')})"
            return False, f"earnings_guard: filing in {days}d ({info.get('filing_date')})"
    except Exception:
        pass

    if side.lower() == "sell" and not profile.get("allow_short_selling", False):
        return False, "short selling disabled for profile"

    if side.lower() == "sell":
        if float(profile.get("max_short_position_pct", 0) or 0) <= 0:
            return False, "short position cap is zero for profile"
        min_short_score = profile.get("min_short_signal_score", profile["min_signal_score"])
        if str(market_regime or "").lower() == "bull":
            min_short_score = profile.get("bull_short_signal_score", min_short_score)
        if abs(composite_score) < min_short_score:
            return False, f"short signal below threshold ({composite_score:.3f} < {min_short_score})"

    if drawdown >= profile["max_drawdown_pct"]:
        return False, f"max drawdown hit ({drawdown:.1f}% ≥ {profile['max_drawdown_pct']}%)"

    if vix > profile["vix_ceiling"]:
        return False, f"VIX too high ({vix:.0f} > {profile['vix_ceiling']})"

    if cash_pct < profile["cash_buffer_pct"]:
        return False, f"insufficient cash ({cash_pct:.1f}% < {profile['cash_buffer_pct']}%)"

    if abs(composite_score) < profile["min_signal_score"]:
        return False, f"signal below threshold ({composite_score:.3f} < {profile['min_signal_score']})"

    if trades_today >= profile.get("max_trades_per_day", 8):
        return False, f"daily trade limit reached ({trades_today})"

    return True, "pass"


def scan_for_extreme_dips(tickers: list, portfolio_state: dict = None,
                          macro_regime: str = "normal") -> list:
    """
    Scans tickers for extreme dip-buy setups using daily bars.

    Returns list of opportunity dicts sorted by dip_score descending.
    Each dict contains: ticker, type, pct_from_high, rsi, dip_score,
    conviction, hold_days, stop_multiplier, size_multiplier.
    """
    import numpy as np
    import yfinance as yf

    _ENERGY_DIPS = {"XLE", "XOM", "USO", "GLD"}
    opportunities = []

    for ticker in tickers:
        try:
            df = yf.download(ticker, period="30d", interval="1d",
                             progress=False, auto_adjust=True)
            if df is None or df.empty or len(df) < 20:
                continue

            close     = df["Close"].squeeze()
            price_now = float(close.iloc[-1])
            high_20d  = float(close.rolling(20).max().iloc[-1])
            pct_down  = (high_20d - price_now) / high_20d * 100 if high_20d > 0 else 0.0

            # Daily RSI
            delta = close.diff()
            gain  = delta.clip(lower=0).rolling(14).mean()
            loss  = (-delta.clip(upper=0)).rolling(14).mean()
            rs    = gain / loss.replace(0, np.nan)
            rsi   = float((100 - 100 / (1 + rs)).iloc[-1])
            if np.isnan(rsi):
                continue

            is_energy = ticker.upper() in _ENERGY_DIPS

            scan_base = {
                "ticker":        ticker,
                "price":         round(price_now, 4),
                "high_20d":      round(high_20d, 4),
                "pct_from_high": round(pct_down, 2),
                "rsi":           round(rsi, 1),
                "macro_regime":  macro_regime,
            }

            # Special case: macro-backed energy dip (the Apr-9 XLE scenario)
            if macro_regime == "geopolitical_shock" and is_energy and pct_down > 20:
                dip_score = min(1.0, pct_down / 20 * 0.6 + max(0, 30 - rsi) / 30 * 0.4)
                if dip_score > 0.3:
                    opportunities.append({
                        **scan_base,
                        "type":            "macro_energy_dip",
                        "dip_score":       round(dip_score, 3),
                        "conviction":      0.90,
                        "hold_days":       3,
                        "stop_multiplier": 2.0,
                        "size_multiplier": 1.5,
                    })
                continue

            # Standard extreme dip: >25% from 20-day high AND RSI < 28
            if pct_down > 25 and rsi < 28:
                # Skip if macro is hostile to this ticker
                if macro_regime == "geopolitical_shock" and not is_energy:
                    continue

                dip_score = min(1.0, (pct_down / 25) * max(0, 30 - rsi) / 30)
                if dip_score > 0.6:
                    opportunities.append({
                        **scan_base,
                        "type":            "extreme_dip",
                        "dip_score":       round(dip_score, 3),
                        "conviction":      0.85,
                        "hold_days":       3,
                        "stop_multiplier": 2.0,
                        "size_multiplier": 1.5,
                    })
        except Exception:
            continue

    return sorted(opportunities, key=lambda x: x["dip_score"], reverse=True)


def compute_position_size(ticker: str, total_capital: float, profile: dict,
                          conviction: float, atr_data: dict = None,
                          regime_state=None) -> dict:
    """
    ATR risk sizing. Returns the full sizing calculation for learning.
    atr_pct is accepted from compute_atr as a percentage value.
    """
    atr_data = atr_data or {}
    target_risk_eur = total_capital * 0.01

    raw_atr_pct = atr_data.get("atr_pct")
    if raw_atr_pct is None:
        atr_fraction = float(profile.get("stop_loss_pct", 2.0)) / 100
    else:
        atr_fraction = float(raw_atr_pct) / 100

    stop_multiplier = 2.0 if getattr(regime_state, "intraday_regime", "") == "high_vol" else 1.5
    stop_distance_pct = max(0.001, atr_fraction * stop_multiplier)
    base_size_eur = target_risk_eur / stop_distance_pct

    conviction_scalar = 0.5 + max(0.0, min(float(conviction or 0), 1.0))
    market_regime = getattr(regime_state, "market_regime", "bull")
    intraday_regime = getattr(regime_state, "intraday_regime", "")
    regime_scalar = {
        "bull": 1.0,
        "transitioning": 0.7,
        "bear": 0.6,
        "high_vol": 0.5,
    }.get(market_regime, 1.0)
    if intraday_regime == "high_vol":
        regime_scalar = min(regime_scalar, 0.5)

    vix = float(getattr(regime_state, "vix", 20.0) or 20.0)
    vix_scalar = 1.0 if vix < 25 else max(0.4, 1 - (vix - 25) / 50)

    final_size = base_size_eur * conviction_scalar * regime_scalar * vix_scalar
    max_size = total_capital * profile["max_position_pct"] / 100
    min_size = 5.0

    size_eur = round(max(min_size, min(final_size, max_size)), 2)
    fx_rate  = float(os.getenv("EURUSD_RATE", "1.08") or "1.08")
    result = {
        "ticker":           ticker,
        "size_eur":         size_eur,
        "stop_pct":         round(stop_distance_pct * 100, 3),
        "target_risk_eur":  round(target_risk_eur, 2),
        "conviction_scalar": round(conviction_scalar, 2),
        "regime_scalar":    regime_scalar,
        "vix_scalar":       round(vix_scalar, 2),
        "atr_pct":          round(atr_fraction * 100, 3),
        "stop_multiplier":  stop_multiplier,
    }
    try:
        import logging
        logging.getLogger(__name__).info(
            "position_sized ticker=%s size_eur=%.2f capital_base_eur=%.0f pct_of_capital=%.1f%%",
            ticker, size_eur, total_capital / fx_rate,
            size_eur / total_capital * 100 if total_capital else 0,
        )
    except Exception:
        pass
    return result
