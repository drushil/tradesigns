"""
backend/execution/common.py
Shared pure helpers used by both entry and exit logic.

No state imports (no circular deps).  Depends only on:
  - stdlib
  - backend.runtime.env  (env helpers)
  - backend.market.sector  (constants: _INVERSE_ETFS, _PROBE_EV_DECISIONS)
"""
from __future__ import annotations
import os
import re
import time
from datetime import datetime
from typing import Optional

from backend.market.sector import _INVERSE_ETFS, _PROBE_EV_DECISIONS


# ---------------------------------------------------------------------------
# Capital / size helpers
# ---------------------------------------------------------------------------

def _trading_capital(equity: float) -> float:
    """Return the configured trading capital cap, or equity itself."""
    raw = os.getenv("TRADING_CAPITAL_EUR") or os.getenv("STARTING_CAPITAL_EUR")
    if raw and raw.strip():
        try:
            return min(float(raw), equity)
        except ValueError:
            pass
    return equity


def _cap_short_notional(size_eur: float, capital_base: float, profile: dict) -> float:
    short_cap_pct = profile.get("max_short_position_pct")
    if short_cap_pct is None:
        short_cap_pct = profile.get("max_position_pct", 0)
    return min(size_eur, capital_base * float(short_cap_pct) / 100)


# ---------------------------------------------------------------------------
# Order reference generation
# ---------------------------------------------------------------------------

def _deterministic_action(composite: float) -> str:
    return "BUY" if composite > 0 else "SELL"


def _make_order_ref(*parts) -> str:
    cleaned = [
        re.sub(r"[^a-zA-Z0-9]", "", str(part))[:16].lower()
        for part in parts
        if part is not None and str(part) != ""
    ]
    return "-".join(cleaned)[:48] or str(int(time.time() * 1000))


# ---------------------------------------------------------------------------
# Regime debug payload
# ---------------------------------------------------------------------------

def _regime_debug_payload(regime_state, signal_result: dict = None) -> dict:
    signal_result = signal_result or {}
    payload = regime_state.to_dict() if hasattr(regime_state, "to_dict") else {}
    payload.update({
        "macro_regime": signal_result.get("macro_regime"),
        "macro_multiplier": signal_result.get("macro_multiplier", 1.0),
        "regime_bull_bear": signal_result.get("regime_bull_bear"),
        "shock_detected": signal_result.get("shock_detected", False),
        "shock_classification": signal_result.get("shock_classification"),
    })
    return payload


# ---------------------------------------------------------------------------
# Strategy family classifier
# ---------------------------------------------------------------------------

def _strategy_family(ticker: str, side: str, regime: str, signal_result: dict,
                     horizon: str = "short", mean_reversion_trade: bool = False) -> str:
    ticker = str(ticker or "").upper()
    side = str(side or "").upper()
    regime = str(regime or "")
    horizon = str(horizon or "short")
    signal_result = signal_result or {}
    signals = signal_result.get("signals", {}) or {}

    if mean_reversion_trade or signal_result.get("mean_reversion_signal"):
        return "mean_reversion"
    if horizon == "swing":
        return "swing"
    if ticker in _INVERSE_ETFS:
        return "inverse_etf"
    if side == "SELL":
        return "direct_short"

    macd = signals.get("macd_crossover", {}).get("score", 0)
    rel_strength = signals.get("relative_strength", {}).get("score", 0)
    tape = signals.get("tape_aggression", {}).get("score", 0)
    if regime == "trending" or max(macd, rel_strength, tape) >= 0.35:
        return "trend_following"
    return "signal_composite"


# ---------------------------------------------------------------------------
# Signal scoring helpers
# ---------------------------------------------------------------------------

def _signal_score(signals: dict, name: str) -> float:
    try:
        return float((signals or {}).get(name, {}).get("score", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def _is_probe_ev_decision(ev_decision: str) -> bool:
    return str(ev_decision or "") in _PROBE_EV_DECISIONS


def _directional_score(side: str, composite: float) -> float:
    return float(composite) if side == "BUY" else -float(composite)


# ---------------------------------------------------------------------------
# Trade P&L helpers
# ---------------------------------------------------------------------------

def _parse_dt(value) -> datetime:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if not value:
        return datetime.utcnow()
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return datetime.utcnow()


def _trade_pnl_pct(trade: dict, current_price: float) -> float:
    entry_price = float(trade.get("entry_price") or 0)
    if entry_price <= 0:
        return 0.0
    pnl_pct = (current_price - entry_price) / entry_price * 100
    if trade.get("side") == "SELL":
        pnl_pct = -pnl_pct
    return pnl_pct
