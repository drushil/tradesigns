"""
backend/portfolio/advisor.py
Advisory portfolio layer — observation and recommendation only, no execution.

Runs weekly (Sunday 17:00 UTC) via run_portfolio_review().
Scores each open position's thesis validity and produces hold/trim/add/exit
recommendations written to portfolio_reviews. No trades are placed.
Execution authority is granted only after 8-10 weeks of validated recommendations.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional

# ── Static sector map (extended by yfinance fallback) ─────────────────────────
_SECTOR_MAP: dict[str, str] = {
    "SPY": "broad_market", "QQQ": "tech", "IWM": "small_cap",
    "DIA": "broad_market", "VTI": "broad_market",
    "GLD": "commodities", "SLV": "commodities", "USO": "commodities",
    "TLT": "bonds",       "IEF": "bonds",       "SHY": "bonds",
    "HYG": "bonds",       "LQD": "bonds",
    "XLK": "tech",        "XLF": "financials",  "XLE": "energy",
    "XLV": "healthcare",  "XLI": "industrials", "XLY": "consumer_disc",
    "XLP": "consumer_stap","XLU": "utilities",   "XLRE": "real_estate",
    "AAPL": "tech",  "MSFT": "tech",  "NVDA": "tech",  "GOOGL": "tech",
    "AMZN": "tech",  "META": "tech",  "TSLA": "tech",  "AMD": "tech",
    "INTC": "tech",  "CRM": "tech",
    "JPM": "financials", "BAC": "financials", "GS": "financials",
    "MS":  "financials", "V": "financials",   "MA": "financials",
    "XOM": "energy",  "CVX": "energy",
    "JNJ": "healthcare", "UNH": "healthcare", "PFE": "healthcare",
    "PG": "consumer_stap", "KO": "consumer_stap",
    "VZ": "telecom",  "T": "telecom",
}

# Sector concentration alert threshold
_MAX_SECTOR_PCT = float(os.getenv("PORTFOLIO_MAX_SECTOR_PCT", "35"))
_MAX_SINGLE_PCT = float(os.getenv("PORTFOLIO_MAX_SINGLE_PCT", "20"))
_MIN_CASH_PCT   = float(os.getenv("PORTFOLIO_MIN_CASH_PCT",   "15"))


def get_sector(ticker: str) -> str:
    """Return sector for ticker, falling back to yfinance info."""
    ticker = ticker.upper()
    if ticker in _SECTOR_MAP:
        return _SECTOR_MAP[ticker]
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info
        raw = info.get("sector", "unknown")
        return raw.lower().replace(" ", "_") if raw else "unknown"
    except Exception:
        return "unknown"


# ── Thesis validity scoring ───────────────────────────────────────────────────

def score_thesis(trade: dict, signal_result: dict, profile: dict) -> dict:
    """
    Score thesis validity for an open position.
    Returns: {"status": "valid"|"weakening"|"broken", "reasons": [...]}
    """
    reasons = []
    composite   = float(signal_result.get("composite_score") or 0)
    side        = trade.get("side", "BUY")
    directional = composite if side == "BUY" else -composite

    entry_price  = float(trade.get("entry_price") or 0)
    current_price = float(trade.get("_current_price") or entry_price)
    pnl_pct = (current_price - entry_price) / entry_price * 100 if entry_price else 0

    stop_loss_pct   = float(profile.get("stop_loss_pct", 2.0))
    min_signal      = float(profile.get("min_signal_score", 0.10))

    # ── Broken conditions ─────────────────────────────────────────────────────
    if pnl_pct <= -stop_loss_pct * 1.5:
        reasons.append(f"pnl_{pnl_pct:.2f}pct_exceeds_stop")
        return {"status": "broken", "reasons": reasons, "pnl_pct": round(pnl_pct, 3), "directional_score": round(directional, 3)}

    if directional < -0.30:
        reasons.append(f"signal_strongly_reversed_{directional:.2f}")
        return {"status": "broken", "reasons": reasons, "pnl_pct": round(pnl_pct, 3), "directional_score": round(directional, 3)}

    # ── Weakening conditions ──────────────────────────────────────────────────
    if directional < min_signal:
        reasons.append(f"signal_below_threshold_{directional:.2f}")

    if pnl_pct < 0:
        reasons.append(f"position_underwater_{pnl_pct:.2f}pct")

    days_held = int(trade.get("hold_days_actual") or
                    (datetime.utcnow() - _parse_dt(trade.get("entry_time"))).days)
    target_hold = int(trade.get("target_hold_days") or
                      int(trade.get("max_hold_minutes", 390)) // 390)
    if target_hold > 0 and days_held > target_hold * 1.5:
        reasons.append(f"held_{days_held}d_beyond_target_{target_hold}d")

    if reasons:
        return {"status": "weakening", "reasons": reasons, "pnl_pct": round(pnl_pct, 3), "directional_score": round(directional, 3)}

    return {"status": "valid", "reasons": ["all_checks_pass"], "pnl_pct": round(pnl_pct, 3), "directional_score": round(directional, 3)}


def _parse_dt(value) -> datetime:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return datetime.utcnow()


# ── Portfolio exposure ────────────────────────────────────────────────────────

def compute_exposure(open_trades: dict, equity_eur: float) -> dict:
    """
    Return portfolio-level exposure metrics from open trades.
    """
    sector_exposure: dict[str, float] = {}
    long_eur = short_eur = 0.0

    for ticker, trade in open_trades.items():
        size = float(trade.get("size_eur") or 0)
        side = trade.get("side", "BUY")
        sector = get_sector(ticker)

        sector_exposure[sector] = sector_exposure.get(sector, 0) + size
        if side == "BUY":
            long_eur += size
        else:
            short_eur += size

    total_deployed = long_eur + short_eur
    cash_eur = max(0.0, equity_eur - total_deployed)

    sector_pct = {
        s: round(v / equity_eur * 100, 1) if equity_eur else 0
        for s, v in sector_exposure.items()
    }
    alerts = []
    for sector, pct in sector_pct.items():
        if pct > _MAX_SECTOR_PCT:
            alerts.append(f"sector_{sector}_{pct:.0f}pct_exceeds_{_MAX_SECTOR_PCT:.0f}pct_limit")

    for ticker, trade in open_trades.items():
        size = float(trade.get("size_eur") or 0)
        single_pct = size / equity_eur * 100 if equity_eur else 0
        if single_pct > _MAX_SINGLE_PCT:
            alerts.append(f"{ticker}_{single_pct:.0f}pct_exceeds_{_MAX_SINGLE_PCT:.0f}pct_single_limit")

    if equity_eur and cash_eur / equity_eur * 100 < _MIN_CASH_PCT:
        alerts.append(f"cash_{cash_eur:.0f}eur_below_min_{_MIN_CASH_PCT:.0f}pct")

    return {
        "equity_eur":       round(equity_eur, 2),
        "total_deployed":   round(total_deployed, 2),
        "long_eur":         round(long_eur, 2),
        "short_eur":        round(short_eur, 2),
        "cash_eur":         round(cash_eur, 2),
        "deployed_pct":     round(total_deployed / equity_eur * 100, 1) if equity_eur else 0,
        "cash_pct":         round(cash_eur / equity_eur * 100, 1) if equity_eur else 0,
        "sector_pct":       sector_pct,
        "position_count":   len(open_trades),
        "alerts":           alerts,
    }


# ── Recommendation engine ─────────────────────────────────────────────────────

def generate_recommendation(
    ticker: str,
    trade: dict,
    thesis: dict,
    exposure: dict,
    profile: dict,
) -> dict:
    """
    Produce a single recommendation for an open position.
    Returns: {"recommendation": "hold"|"trim"|"add"|"exit"|"rebalance", "rationale": {...}}
    """
    status  = thesis["status"]
    pnl_pct = thesis["pnl_pct"]
    size    = float(trade.get("size_eur") or 0)
    equity  = float(exposure.get("equity_eur") or 1)
    weight  = round(size / equity * 100, 1) if equity else 0

    # Broken thesis — exit
    if status == "broken":
        return {
            "recommendation": "exit",
            "rationale": {
                "thesis_status": "broken",
                "reasons": thesis["reasons"],
                "pnl_pct": pnl_pct,
                "note": "Thesis invalidated — exit to prevent further loss.",
            },
        }

    # Overweight single position — trim regardless of thesis
    if weight > _MAX_SINGLE_PCT:
        return {
            "recommendation": "trim",
            "rationale": {
                "thesis_status": status,
                "weight_pct": weight,
                "max_single_pct": _MAX_SINGLE_PCT,
                "note": f"Position at {weight:.0f}% of portfolio — trim to {_MAX_SINGLE_PCT:.0f}% limit.",
            },
        }

    # Sector overweight — trim largest contributor
    sector = get_sector(ticker)
    if exposure["sector_pct"].get(sector, 0) > _MAX_SECTOR_PCT:
        return {
            "recommendation": "trim",
            "rationale": {
                "thesis_status": status,
                "sector": sector,
                "sector_pct": exposure["sector_pct"].get(sector),
                "note": f"{sector} sector over {_MAX_SECTOR_PCT:.0f}% — reduce exposure.",
            },
        }

    # Weakening thesis
    if status == "weakening":
        if pnl_pct < 0:
            return {
                "recommendation": "exit",
                "rationale": {
                    "thesis_status": "weakening",
                    "reasons": thesis["reasons"],
                    "pnl_pct": pnl_pct,
                    "note": "Thesis weakening and position underwater — exit.",
                },
            }
        return {
            "recommendation": "trim",
            "rationale": {
                "thesis_status": "weakening",
                "reasons": thesis["reasons"],
                "pnl_pct": pnl_pct,
                "note": "Thesis weakening but position in profit — trim, tighten stop.",
            },
        }

    # Valid thesis — hold, consider add if cash available
    if exposure["cash_pct"] > _MIN_CASH_PCT * 2 and pnl_pct > 1.0 and weight < _MAX_SINGLE_PCT * 0.7:
        return {
            "recommendation": "add",
            "rationale": {
                "thesis_status": "valid",
                "pnl_pct": pnl_pct,
                "cash_pct": exposure["cash_pct"],
                "note": "Thesis valid, cash available, position winning — consider adding.",
            },
        }

    return {
        "recommendation": "hold",
        "rationale": {
            "thesis_status": "valid",
            "pnl_pct": pnl_pct,
            "note": "Thesis intact — hold.",
        },
    }


# ── Weekly portfolio review entry point ──────────────────────────────────────

def run_portfolio_review() -> dict:
    """
    Score every open position and write recommendations to portfolio_reviews.
    Called weekly (Sunday 17:00 UTC). Returns the review summary dict.
    Observation and recommendation only — no trades are placed.
    """
    from database.client import (
        get_open_trade_records,
        get_snapshots,
        save_portfolio_review,
        log_event,
    )
    from backend.signals.engine import compute_all_signals

    open_records = get_open_trade_records()
    if not open_records:
        log_event("ADVISORY", "portfolio_review_skipped", {"reason": "no_open_trades"})
        return {"skipped": True, "reason": "no_open_trades"}

    # Latest equity from most recent snapshot; fall back to env var
    snapshots = get_snapshots(days=1)
    equity_eur = float(
        (snapshots[0].get("equity_eur") or 0) if snapshots
        else __import__("os").getenv("STARTING_CAPITAL_EUR", "100")
    )

    # Build open_trades dict keyed by ticker
    open_trades: dict[str, dict] = {r["ticker"]: r for r in open_records}

    # Fetch current prices + signals for each ticker
    signal_results: dict[str, dict] = {}
    for ticker in list(open_trades.keys()):
        try:
            sig = compute_all_signals(ticker)
            signal_results[ticker] = sig
            # Inject current price so score_thesis can compute pnl_pct
            if sig.get("current_price"):
                open_trades[ticker]["_current_price"] = sig["current_price"]
        except Exception as e:
            log_event("WARNING", "advisor_signal_fetch_failed", {"ticker": ticker, "error": str(e)[:120]})
            signal_results[ticker] = {"composite_score": 0}

    from config.risk_profiles import get_effective_profile
    profile = get_effective_profile()

    exposure = compute_exposure(open_trades, equity_eur)

    position_reviews: list[dict] = []
    summary: dict[str, int] = {"hold": 0, "add": 0, "trim": 0, "exit": 0}

    for ticker, trade in open_trades.items():
        sig = signal_results[ticker]
        thesis = score_thesis(trade, sig, profile)
        rec = generate_recommendation(ticker, trade, thesis, exposure, profile)

        position_reviews.append({
            "ticker":         ticker,
            "recommendation": rec["recommendation"],
            "thesis_status":  thesis["status"],
            "pnl_pct":        thesis["pnl_pct"],
            "directional":    thesis["directional_score"],
            "rationale":      rec["rationale"],
        })
        summary[rec["recommendation"]] = summary.get(rec["recommendation"], 0) + 1

    review = {
        "reviewed_at":      datetime.now(timezone.utc).isoformat(),
        "position_count":   len(open_trades),
        "equity_eur":       equity_eur,
        "exposure":         exposure,
        "positions":        position_reviews,
        "summary":          summary,
        "alerts":           exposure["alerts"],
    }

    save_portfolio_review(review)
    log_event("ADVISORY", "portfolio_review_complete", {
        "positions": len(position_reviews),
        "summary": summary,
        "alerts": len(exposure["alerts"]),
    })
    return review
