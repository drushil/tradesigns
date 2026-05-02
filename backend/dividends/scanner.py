"""
backend/dividends/scanner.py
Scans the ticker universe for upcoming ex-dividend dates.
Advisory only — the signal engine uses results as a mild composite overlay.
All yfinance calls are guarded: any failure returns an empty list.
"""
import os
import time
from datetime import date
from typing import Optional

from backend.sweep.agent import get_broker_env

# Module-level 1-hour cache
_div_cache: list = []
_div_cache_ts: float = 0.0
_DIV_CACHE_TTL = 3600  # seconds


def scan_dividend_calendar(tickers: list) -> list:
    """
    For each ticker, fetch ex-dividend date and yield via yfinance.
    Returns opportunities where yield > threshold and ex-date is 1–5 days away.
    Results are cached for 1 hour.
    """
    global _div_cache, _div_cache_ts

    if tickers and (time.time() - _div_cache_ts) < _DIV_CACHE_TTL:
        return _div_cache

    if not os.getenv("DIVIDEND_SCAN_ENABLED", "true").lower() == "true":
        return []

    min_yield = float(os.getenv("DIVIDEND_MIN_YIELD_PCT", "1.5"))
    results   = []
    today     = date.today()

    try:
        import yfinance as yf
    except ImportError:
        return []

    for ticker in tickers:
        try:
            t        = yf.Ticker(ticker)
            info     = t.info or {}
            calendar = t.calendar

            # Extract ex-dividend date from calendar
            next_ex_date = None
            if calendar is not None:
                if hasattr(calendar, "get"):
                    ex_val = calendar.get("Ex-Dividend Date") or calendar.get("exDividendDate")
                    if ex_val is not None:
                        if hasattr(ex_val, "date"):
                            next_ex_date = ex_val.date()
                        else:
                            try:
                                from datetime import datetime
                                next_ex_date = datetime.utcfromtimestamp(int(ex_val)).date()
                            except Exception:
                                pass

            if next_ex_date is None:
                continue

            days_to_ex = (next_ex_date - today).days
            if not (1 <= days_to_ex <= 5):
                continue

            raw_yield = info.get("dividendYield") or 0.0
            dividend_yield = float(raw_yield) * 100
            if dividend_yield < min_yield:
                continue

            dividend_amount = float(info.get("lastDividendValue") or 0.0)
            opp_score = _score_opportunity(days_to_ex, dividend_yield)

            results.append({
                "ticker":            ticker,
                "next_ex_date":      str(next_ex_date),
                "days_to_ex":        days_to_ex,
                "dividend_amount":   round(dividend_amount, 4),
                "dividend_yield":    round(dividend_yield, 2),
                "opportunity_score": opp_score,
                "broker_env":        get_broker_env(),
            })
        except Exception:
            continue

    _div_cache    = results
    _div_cache_ts = time.time()
    return results


def get_cached_dividend_scan() -> list:
    """Return the last cached dividend scan without re-fetching."""
    return _div_cache


def _score_opportunity(days_to_ex: int, yield_pct: float) -> float:
    """Returns 0.0–1.0 combining urgency (days) and yield size."""
    if days_to_ex == 1:
        date_score = 1.0
    elif days_to_ex == 2:
        date_score = 0.8
    elif days_to_ex == 3:
        date_score = 0.6
    else:
        date_score = 0.3

    yield_score = min(1.0, yield_pct / 4.0)  # normalise at 4% yield
    return round(date_score * 0.6 + yield_score * 0.4, 3)


def log_dividend_opportunity(opportunity: dict):
    """Log high-score dividend opportunities to Supabase. Best-effort."""
    try:
        from database.client import get_client
        db = get_client(write=True)
        # action_taken: simulation always = logged_only;
        # live agent sets order_submitted when it decides to enter
        action = "logged_only" if not (get_broker_env() == "ibkr_live") else "logged_only"
        db.table("dividend_opportunities").insert({
            "broker_env":       opportunity.get("broker_env"),
            "ticker":           opportunity.get("ticker"),
            "next_ex_date":     opportunity.get("next_ex_date"),
            "days_to_ex":       opportunity.get("days_to_ex"),
            "dividend_amount":  opportunity.get("dividend_amount"),
            "dividend_yield":   opportunity.get("dividend_yield"),
            "opportunity_score": opportunity.get("opportunity_score"),
            "action_taken":     action,
        }).execute()
    except Exception as e:
        print(f"[DIV_LOG_FAILED] {e}")
