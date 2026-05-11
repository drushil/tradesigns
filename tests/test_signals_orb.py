"""tests/test_signals_orb.py — unit tests for opening_range_breakout_score()"""
import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone, timedelta

pd = pytest.importorskip("pandas", reason="pandas not installed")
np = pytest.importorskip("numpy", reason="numpy not installed")
import pandas as pd  # noqa: E402  (re-import after skip guard)
import numpy as np   # noqa: E402


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_ny_time(hour: int, minute: int = 0) -> datetime:
    """Return a UTC datetime that corresponds to h:m New York (non-DST = UTC-5)."""
    return datetime(2024, 1, 15, hour + 5, minute, 0, tzinfo=timezone.utc)


def _make_bars(prices: list[float], volumes: list[float] = None) -> pd.DataFrame:
    """Build a mock OHLCV DataFrame with 5-min bars."""
    n = len(prices)
    if volumes is None:
        volumes = [1_000_000] * n
    base = datetime(2024, 1, 15, 14, 30, tzinfo=timezone.utc)  # 9:30 ET
    idx = pd.date_range(start=base, periods=n, freq="5min")
    data = {
        "Open":   prices,
        "High":   [p * 1.001 for p in prices],
        "Low":    [p * 0.999 for p in prices],
        "Close":  prices,
        "Volume": volumes,
    }
    return pd.DataFrame(data, index=idx)


def _import_orb():
    from backend.signals.engine import opening_range_breakout_score
    return opening_range_breakout_score


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestORBOutsideWindow:
    def test_returns_zero_before_market(self):
        orb = _import_orb()
        # 8:00 ET = 13:00 UTC
        with patch("backend.signals.engine._to_new_york_time") as mock_tz:
            ny = MagicMock()
            ny.hour, ny.minute = 8, 0
            mock_tz.return_value = ny
            score, meta = orb("NVDA")
        assert score == 0.0
        assert meta.get("active") is False

    def test_returns_zero_after_window(self):
        orb = _import_orb()
        with patch("backend.signals.engine._to_new_york_time") as mock_tz:
            ny = MagicMock()
            ny.hour, ny.minute = 11, 30   # after 11:15 ET window
            mock_tz.return_value = ny
            score, meta = orb("NVDA")
        assert score == 0.0
        assert meta.get("active") is False


class TestORBBullishBreakout:
    def test_bullish_breakout_positive_score(self):
        orb = _import_orb()
        # OR bars (9:30-9:45): low low prices
        # Post-OR bars: price breaks above OR high
        or_prices = [100.0, 100.5, 101.0]   # OR range high = 101.01
        continuation = [102.0, 102.5, 103.0, 103.5]  # clearly above OR high
        all_prices = or_prices + continuation
        bars = _make_bars(all_prices)
        # Patch volume to 2× avg to confirm breakout
        bars["Volume"] = [500_000, 500_000, 500_000, 1_200_000, 1_100_000, 1_000_000, 900_000]

        with patch("backend.signals.engine._to_new_york_time") as mock_tz, \
             patch("yfinance.download", return_value=bars):
            ny = MagicMock()
            ny.hour, ny.minute = 10, 0    # inside primary window 9:45-10:30
            mock_tz.return_value = ny
            score, meta = orb("NVDA")

        assert score > 0, f"Expected positive score for bullish breakout, got {score}"
        assert meta.get("active") is True

    def test_no_breakout_near_zero(self):
        orb = _import_orb()
        # Price stays flat, no breakout
        flat_prices = [100.0] * 7
        bars = _make_bars(flat_prices)

        with patch("backend.signals.engine._to_new_york_time") as mock_tz, \
             patch("yfinance.download", return_value=bars):
            ny = MagicMock()
            ny.hour, ny.minute = 10, 0
            mock_tz.return_value = ny
            score, meta = orb("NVDA")

        assert abs(score) < 0.3, f"Expected near-zero score for flat price, got {score}"


class TestORBBearishBreakdown:
    def test_bearish_breakdown_negative_score(self):
        orb = _import_orb()
        or_prices = [100.0, 99.5, 99.0]   # OR range low = 98.901
        breakdown = [98.0, 97.5, 97.0, 96.5]  # clearly below OR low
        all_prices = or_prices + breakdown
        bars = _make_bars(all_prices)
        bars["Volume"] = [500_000, 500_000, 500_000, 1_200_000, 1_100_000, 1_000_000, 900_000]

        with patch("backend.signals.engine._to_new_york_time") as mock_tz, \
             patch("yfinance.download", return_value=bars):
            ny = MagicMock()
            ny.hour, ny.minute = 10, 0
            mock_tz.return_value = ny
            score, meta = orb("NVDA")

        assert score < 0, f"Expected negative score for bearish breakdown, got {score}"


class TestORBEmptyBars:
    def test_empty_bars_returns_zero(self):
        orb = _import_orb()
        empty = pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

        with patch("backend.signals.engine._to_new_york_time") as mock_tz, \
             patch("yfinance.download", return_value=empty):
            ny = MagicMock()
            ny.hour, ny.minute = 10, 0
            mock_tz.return_value = ny
            score, meta = orb("NVDA")

        assert score == 0.0

    def test_exception_returns_zero(self):
        orb = _import_orb()

        with patch("backend.signals.engine._to_new_york_time") as mock_tz, \
             patch("yfinance.download", side_effect=Exception("network error")):
            ny = MagicMock()
            ny.hour, ny.minute = 10, 0
            mock_tz.return_value = ny
            score, meta = orb("NVDA")

        assert score == 0.0
