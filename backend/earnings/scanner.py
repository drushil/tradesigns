"""
backend/earnings/scanner.py
Earnings proximity guard using SEC EDGAR (free, no API key required).

Fetches 10-Q and 10-K filing dates to block new entries within 2 days of a
filing — protecting positions from earnings-driven gap risk.

Results are cached in memory for 6 hours per cycle run. ETFs and tickers with
no SEC CIK match are treated as unblocked (blocked=False).
"""
from __future__ import annotations

import time
import requests
from datetime import date, timedelta
from typing import Optional

# ── Module-level caches ───────────────────────────────────────────────────────
_cik_map: dict[str, str] = {}           # ticker → zero-padded CIK string
_cik_map_ts: float        = 0.0
_CIK_MAP_TTL              = 86400       # 24 hours

_earnings_guard: dict[str, dict] = {}   # ticker → result dict
_earnings_guard_ts: float         = 0.0
_EARNINGS_GUARD_TTL               = 21600  # 6 hours

_EDGAR_HEADERS = {
    "User-Agent": "TradeSigns tradesigns-agent/1.0 contact@tradesigns.app",
    "Accept":     "application/json",
}
_BLOCK_DAYS = 2   # block entries this many days either side of a filing date


# ── CIK lookup ────────────────────────────────────────────────────────────────

def _fetch_cik_map() -> dict[str, str]:
    """
    Download the SEC company tickers JSON (~5 MB) and invert it to
    {TICKER: zero-padded-CIK}. Cached for 24 hours.
    """
    try:
        resp = requests.get(
            "https://www.sec.gov/files/company_tickers.json",
            headers=_EDGAR_HEADERS,
            timeout=10,
        )
        resp.raise_for_status()
        raw = resp.json()
        return {
            v["ticker"].upper(): str(v["cik_str"]).zfill(10)
            for v in raw.values()
            if v.get("ticker") and v.get("cik_str")
        }
    except Exception:
        return {}


def _get_cik(ticker: str) -> Optional[str]:
    global _cik_map, _cik_map_ts
    now_ts = time.time()
    if not _cik_map or now_ts - _cik_map_ts > _CIK_MAP_TTL:
        _cik_map    = _fetch_cik_map()
        _cik_map_ts = now_ts
    return _cik_map.get(ticker.upper())


# ── EDGAR filing lookup ───────────────────────────────────────────────────────

def _fetch_recent_filings(cik: str) -> list[str]:
    """
    Return a list of recent 10-Q / 10-K filing dates (YYYY-MM-DD strings)
    for the given zero-padded CIK.
    Uses filingDate because the guard blocks around the actual SEC filing day.
    """
    try:
        url  = f"https://data.sec.gov/submissions/CIK{cik}.json"
        resp = requests.get(url, headers=_EDGAR_HEADERS, timeout=10)
        resp.raise_for_status()
        data    = resp.json()
        recent  = data.get("filings", {}).get("recent", {})
        forms   = recent.get("form", [])
        f_dates = recent.get("filingDate", [])

        earnings_dates = []
        for i, form in enumerate(forms):
            if form in ("10-Q", "10-K"):
                d = f_dates[i] if i < len(f_dates) else None
                if d:
                    earnings_dates.append(d)
        return earnings_dates
    except Exception:
        return []


# ── Public API ────────────────────────────────────────────────────────────────

def scan_earnings_guard(tickers: list) -> dict[str, dict]:
    """
    For each ticker fetch EDGAR filings and determine if a new entry should
    be blocked due to earnings proximity.

    Returns:
        {ticker: {"blocked": bool, "days_to_filing": int|None,
                  "filing_date": str|None, "source": "edgar"|"no_cik"}}

    Results are cached for 6 hours. ETFs / tickers with no CIK → blocked=False.
    """
    global _earnings_guard, _earnings_guard_ts

    now_ts = time.time()
    if _earnings_guard and now_ts - _earnings_guard_ts < _EARNINGS_GUARD_TTL:
        return _earnings_guard

    today   = date.today()
    results: dict[str, dict] = {}

    for ticker in tickers:
        cik = _get_cik(ticker)
        if not cik:
            results[ticker] = {
                "blocked": False, "days_to_filing": None,
                "filing_date": None, "source": "no_cik",
            }
            continue

        filing_dates = _fetch_recent_filings(cik)
        closest_days = None
        closest_date = None

        for d_str in filing_dates:
            try:
                d    = date.fromisoformat(d_str)
                diff = abs((d - today).days)
                if closest_days is None or diff < closest_days:
                    closest_days = diff
                    closest_date = d_str
            except ValueError:
                continue

        blocked = closest_days is not None and closest_days <= _BLOCK_DAYS
        results[ticker] = {
            "blocked":         blocked,
            "days_to_filing":  closest_days,
            "filing_date":     closest_date,
            "source":          "edgar",
        }

    _earnings_guard    = results
    _earnings_guard_ts = now_ts
    return results


def get_cached_earnings_guard() -> dict[str, dict]:
    """Return the last computed earnings guard without re-fetching."""
    return _earnings_guard
