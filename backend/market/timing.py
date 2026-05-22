"""
backend/market/timing.py
Market-hours utilities: timezone conversion, session windows,
EOD guards, and HORIZON helpers.

Depends on:
  - backend.runtime.env  (env helpers)
  - backend.runtime.state (SWING_TICKERS, HORIZON — as module, not destructured)
"""
from __future__ import annotations
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from backend.runtime.env import _env_int, _env_bool
import backend.runtime.state as state

# ---------------------------------------------------------------------------
# New York timezone (try zoneinfo → pytz → fixed-offset fallback)
# ---------------------------------------------------------------------------
try:
    from zoneinfo import ZoneInfo
    NY_TZ = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover
    try:
        import pytz
        NY_TZ = pytz.timezone("America/New_York")
    except Exception:
        NY_TZ = None


# ---------------------------------------------------------------------------
# Core timezone helpers
# ---------------------------------------------------------------------------

def _nth_weekday(year: int, month: int, weekday: int, occurrence: int) -> date:
    """Return the *occurrence*-th *weekday* (0=Mon) of *month*/*year*."""
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + (occurrence - 1) * 7)


def _to_new_york_time(now: datetime) -> datetime:
    """Convert *now* (any tz, or naive UTC) to New York local time."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    if NY_TZ is not None:
        return now.astimezone(NY_TZ)

    # DST-aware fixed-offset fallback when zoneinfo/pytz unavailable
    utc_now = now.astimezone(timezone.utc)
    year = utc_now.year
    dst_start = datetime.combine(
        _nth_weekday(year, 3, 6, 2), datetime.min.time(), timezone.utc
    ).replace(hour=7)
    dst_end = datetime.combine(
        _nth_weekday(year, 11, 6, 1), datetime.min.time(), timezone.utc
    ).replace(hour=6)
    offset = -4 if dst_start <= utc_now < dst_end else -5
    return utc_now.astimezone(timezone(timedelta(hours=offset)))


# ---------------------------------------------------------------------------
# Session predicates
# ---------------------------------------------------------------------------

def is_regular_us_market_hours(now: datetime = None) -> bool:
    """Return True during regular US equity hours: Mon-Fri 09:30–16:00 ET."""
    now = now or datetime.now(timezone.utc)
    ny_now = _to_new_york_time(now)
    if ny_now.weekday() >= 5:
        return False
    market_open  = ny_now.replace(hour=9,  minute=30, second=0, microsecond=0)
    market_close = ny_now.replace(hour=16, minute=0,  second=0, microsecond=0)
    return market_open <= ny_now < market_close


def _should_run_swing_recheck(now: datetime = None) -> bool:
    """True once per day during the configured NY swing re-eval open window."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    ny_now  = _to_new_york_time(now)
    hour    = _env_int("SWING_REEVAL_NY_HOUR", 9)
    minute  = _env_int("SWING_REEVAL_NY_MINUTE", 35)
    window  = _env_int("SWING_REEVAL_WINDOW_MINUTES", 5)
    current = ny_now.hour * 60 + ny_now.minute
    target  = hour * 60 + minute
    return ny_now.weekday() < 5 and 0 <= current - target < window


def _is_eod_intraday_cleanup_window(now: datetime) -> bool:
    """True during the 30-min window before the regular US market close."""
    if not _env_bool("EOD_INTRADAY_CLEANUP_ENABLED", True):
        return False
    if not is_regular_us_market_hours(now):
        return False
    ny_now  = _to_new_york_time(now)
    close   = (
        _env_int("EOD_CLEANUP_NY_CLOSE_HOUR", 16) * 60
        + _env_int("EOD_CLEANUP_NY_CLOSE_MINUTE", 0)
    )
    current = ny_now.hour * 60 + ny_now.minute
    return close - _env_int("EOD_CLEANUP_BUFFER_MINUTES", 30) <= current < close


def _minutes_to_regular_close(now: datetime = None) -> Optional[int]:
    """Minutes until regular US market close, or None outside regular hours."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    if not is_regular_us_market_hours(now):
        return None
    ny_now  = _to_new_york_time(now)
    close   = (
        _env_int("EOD_CLEANUP_NY_CLOSE_HOUR", 16) * 60
        + _env_int("EOD_CLEANUP_NY_CLOSE_MINUTE", 0)
    )
    current = ny_now.hour * 60 + ny_now.minute
    return max(0, close - current)


def _minutes_since_regular_open(now: datetime = None) -> Optional[int]:
    """Minutes since regular US market open, or None outside regular hours."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    if not is_regular_us_market_hours(now):
        return None
    ny_now       = _to_new_york_time(now)
    open_minutes = 9 * 60 + 30
    current      = ny_now.hour * 60 + ny_now.minute
    return max(0, current - open_minutes)


def _is_eod_final_force_exit_window(now: datetime = None) -> bool:
    """True in the final EOD window where intraday positions must be closed."""
    minutes_to_close = _minutes_to_regular_close(now)
    if minutes_to_close is None:
        return False
    return minutes_to_close <= _env_int("EOD_FINAL_FORCE_EXIT_MINUTES", 5)


def _is_new_intraday_entry_too_late(ticker: str, now: datetime = None) -> Optional[dict]:
    """Block fresh intraday entries near close unless the ticker is swing-eligible."""
    minutes_to_close = _minutes_to_regular_close(now)
    if minutes_to_close is None:
        return None
    buffer_minutes = _env_int("EOD_NEW_ENTRY_BLOCK_MINUTES", 25)
    ticker = str(ticker or "").upper()
    if minutes_to_close < buffer_minutes and ticker not in state.SWING_TICKERS:
        return {
            "ticker": ticker,
            "minutes_to_close": minutes_to_close,
            "buffer_minutes": buffer_minutes,
            "reason": "eod_new_intraday_entry_block",
        }
    return None


def _leveraged_etf_max_hold_window(now: datetime = None) -> bool:
    """True if we are past the leveraged-ETF max-hold cutoff (default 3:45 PM ET)."""
    now = now or datetime.now(timezone.utc)
    ny_now = _to_new_york_time(now)
    cutoff_minutes = 15 * 60 + 45  # 3:45 PM default
    return (ny_now.hour * 60 + ny_now.minute) >= cutoff_minutes


# ---------------------------------------------------------------------------
# HORIZON helpers
# ---------------------------------------------------------------------------

def _allows_intraday() -> bool:
    return state.HORIZON in {"short", "intraday", "both"}


def _allows_swing() -> bool:
    return state.HORIZON in {"mid", "swing", "both"}
