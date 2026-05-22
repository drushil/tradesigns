"""
backend/execution/evidence.py
Observability-only evidence tags for playbook research.

These helpers deliberately do not block, size, or route trades. They attach the
metadata needed to evaluate one playbook at a time with honest costs, data
quality, and factor exposure before any future behavior promotion.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from backend.runtime.env import _env_float, _env_value
from backend.market.timing import _minutes_since_regular_open
from backend.market.sector import _ticker_theme
from backend.execution.common import _signal_score


_PRIMARY_FACTOR_BUCKETS = {
    "SPY": "broad_market",
    "IWM": "broad_market",
    "DIA": "broad_market",
    "QQQ": "broad_tech",
    "META": "broad_tech",
    "AMZN": "broad_tech",
    "AAPL": "broad_tech",
    "MSFT": "broad_tech",
    "GOOGL": "broad_tech",
    "PLTR": "broad_tech",
    "SMH": "semis",
    "NVDA": "semis",
    "AMD": "semis",
    "AVGO": "semis",
    "ARM": "semis",
    "MU": "semis",
    "SOXL": "leveraged_semis",
    "NVDL": "leveraged_semis",
    "TQQQ": "leveraged_tech",
    "IBIT": "crypto",
    "COIN": "crypto",
    "MSTR": "crypto",
    "XLE": "energy",
    "XOP": "energy",
    "XLF": "financials",
    "GLD": "defensive",
    "TLT": "rates",
    "IEF": "rates",
    "SGOV": "cash_like",
    "BIL": "cash_like",
}


def primary_factor_bucket(ticker: str) -> str:
    ticker = str(ticker or "").upper()
    return _PRIMARY_FACTOR_BUCKETS.get(ticker) or _ticker_theme(ticker) or "other"


def session_window(minutes_since_open: Optional[int]) -> str:
    if minutes_since_open is None:
        return "outside_regular_hours"
    try:
        minutes = int(minutes_since_open)
    except (TypeError, ValueError):
        return "unknown"
    if minutes < 15:
        return "opening_noise"
    if minutes <= 45:
        return "opening_drive"
    if minutes <= 120:
        return "morning_trend"
    if minutes <= 300:
        return "midday"
    if minutes <= 345:
        return "afternoon_momentum"
    if minutes <= 385:
        return "pre_close"
    return "after_close"


def _quote_meta(signals: dict) -> dict:
    meta = ((signals or {}).get("order_book_imbalance") or {}).get("meta") or {}
    return meta if isinstance(meta, dict) else {}


def _vwap_pct(signals: dict) -> Optional[float]:
    try:
        return float(
            (((signals or {}).get("vwap_deviation") or {}).get("meta") or {})
            .get("pct_deviation")
        )
    except (TypeError, ValueError):
        return None


def estimate_costs(signals: dict, setup_context: dict = None) -> dict:
    quote = _quote_meta(signals)
    try:
        spread_pct = float(quote.get("spread_pct"))
    except (TypeError, ValueError):
        spread_pct = None
    fallback_slippage = _env_float("PLAYBOOK_FALLBACK_SLIPPAGE_PCT", 0.08)
    estimated_spread = max(0.0, spread_pct) if spread_pct is not None else None
    total_cost = (estimated_spread if estimated_spread is not None else fallback_slippage) + fallback_slippage
    return {
        "spread_pct": round(estimated_spread, 4) if estimated_spread is not None else None,
        "fallback_slippage_pct": round(fallback_slippage, 4),
        "estimated_total_cost_pct": round(total_cost, 4),
        "cost_model": "spread_plus_fallback_slippage" if estimated_spread is not None else "fallback_slippage_only",
    }


def data_quality(signal_result: dict, signals: dict) -> dict:
    signal_result = signal_result or {}
    signals = signals or {}
    quote = _quote_meta(signals)
    rvol = signal_result.get("rvol_data") or {}
    atr = signal_result.get("atr_data") or {}
    computed_at = signal_result.get("computed_at")
    computed_age_seconds = None
    try:
        if computed_at:
            stamp = datetime.fromisoformat(str(computed_at).replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            computed_age_seconds = round((datetime.now(timezone.utc) - stamp).total_seconds())
    except Exception:
        computed_age_seconds = None

    missing = []
    if atr.get("atr_pct") is None:
        missing.append("atr")
    if _vwap_pct(signals) is None:
        missing.append("vwap_pct_deviation")
    if not quote or quote.get("source") in {None, "fallback_unavailable"}:
        missing.append("quote")

    state = "executable"
    if missing:
        state = "shadow_only"
    if computed_age_seconds is not None and computed_age_seconds > _env_float("PLAYBOOK_STALE_SIGNAL_SECONDS", 600):
        state = "shadow_only"
        missing.append("stale_signal")

    return {
        "state": state,
        "missing": sorted(set(missing)),
        "signal_computed_age_seconds": computed_age_seconds,
        "quote_source": quote.get("source"),
        "rvol_available": bool(rvol.get("rvol_available")),
        "atr_available": atr.get("atr_pct") is not None,
    }


def classify_playbook(ticker: str, action: str, signals: dict,
                      signal_result: dict, setup_context: dict) -> str:
    setup_context = setup_context or {}
    signals = signals or {}
    action = str(action or "").upper()
    direction = 1 if action == "BUY" else -1
    minutes = setup_context.get("minutes_since_open")
    window = session_window(minutes)
    factor = primary_factor_bucket(ticker)
    regime = str(setup_context.get("intraday_regime") or "").lower()
    strategy = str(setup_context.get("strategy_family") or "")
    orb = _signal_score(signals, "orb") * direction
    tape = _signal_score(signals, "tape_aggression") * direction
    macd = _signal_score(signals, "macd_crossover") * direction
    rel = _signal_score(signals, "relative_strength") * direction
    vwap_pct = _vwap_pct(signals)
    vwap_aligned = (
        (action == "BUY" and vwap_pct is not None and vwap_pct > 0)
        or (action == "SELL" and vwap_pct is not None and vwap_pct < 0)
    )

    if strategy == "mean_reversion":
        return "mean_reversion"
    if factor == "crypto" and max(macd, rel, tape) >= 0.15:
        return "crypto_momentum"
    if window == "opening_drive" and (orb >= 0.35 or (tape >= 0.20 and rel >= 0.15)):
        return "opening_drive_breakout"
    if window == "morning_trend" and vwap_aligned and rel >= 0.15 and max(macd, tape) >= 0.10:
        return "morning_vwap_reclaim"
    if window == "afternoon_momentum" and vwap_aligned and rel >= 0.15:
        return "afternoon_continuation"
    if setup_context.get("ranging_probe") or regime == "ranging":
        return "ranging_probe"
    if strategy == "trend_following":
        return "trend_following"
    return "signal_composite"


def enrich_setup_context(ticker: str, action: str, signals: dict,
                         signal_result: dict, setup_context: dict) -> dict:
    setup_context = setup_context or {}
    minutes = setup_context.get("minutes_since_open")
    if minutes is None:
        minutes = _minutes_since_regular_open()
        setup_context["minutes_since_open"] = minutes
    costs = estimate_costs(signals, setup_context)
    quality = data_quality(signal_result, signals)
    playbook = classify_playbook(ticker, action, signals, signal_result, setup_context)
    factor = primary_factor_bucket(ticker)
    window = session_window(minutes)
    lifecycle = _env_value("PLAYBOOK_DEFAULT_LIFECYCLE", "tagged")
    regime_key = "|".join([
        str(setup_context.get("intraday_regime") or "unknown"),
        window,
    ])

    setup_context.update({
        "playbook": playbook,
        "playbook_lifecycle": lifecycle,
        "session_window": window,
        "primary_factor": factor,
        "factor_bucket": factor,
        "regime_key": regime_key,
        "data_quality_state": quality["state"],
        "data_quality": quality,
        "cost_estimate": costs,
        "estimated_spread_pct": costs.get("spread_pct"),
        "estimated_total_cost_pct": costs.get("estimated_total_cost_pct"),
    })
    return setup_context
