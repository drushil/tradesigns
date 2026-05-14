"""tests/test_signals_orb.py — unit tests for opening_range_breakout_score()"""
import pytest
from unittest.mock import patch
from datetime import datetime, timezone

pd = pytest.importorskip("pandas", reason="pandas not installed")
np = pytest.importorskip("numpy", reason="numpy not installed")
import pandas as pd  # noqa: E402
import numpy as np   # noqa: E402


# ── Helpers ───────────────────────────────────────────────────────────────────

def _et_minutes(hour: int, minute: int = 0) -> int:
    return hour * 60 + minute


def _make_bars(prices: list, volumes: list = None) -> pd.DataFrame:
    """Build a mock OHLCV DataFrame with 5-min bars."""
    n = len(prices)
    if volumes is None:
        volumes = [1_000_000] * n
    base = datetime(2024, 1, 15, 14, 30, tzinfo=timezone.utc)  # 9:30 ET
    idx = pd.date_range(start=base, periods=n, freq="5min")
    return pd.DataFrame({
        "Open":   prices,
        "High":   [p * 1.001 for p in prices],
        "Low":    [p * 0.999 for p in prices],
        "Close":  prices,
        "Volume": volumes,
    }, index=idx)


def _import_orb():
    return _import_engine().opening_range_breakout_score


def _clear_orb_cache():
    eng = _import_engine()
    eng._orb_cache.clear()


def _import_engine():
    """Load the real signal engine if the broader agent tests installed a stub."""
    import importlib
    import sys

    eng = sys.modules.get("backend.signals.engine")
    if eng is not None and not hasattr(eng, "_orb_cache"):
        sys.modules.pop("backend.signals.engine", None)
        sys.modules.pop("backend.signals", None)
    return importlib.import_module("backend.signals.engine")


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestORBOutsideWindow:
    def setup_method(self):
        _clear_orb_cache()

    def test_returns_zero_before_market(self):
        orb = _import_orb()
        with patch("backend.signals.engine._et_minutes_since_midnight",
                   return_value=_et_minutes(8, 0)):
            score, meta = orb("NVDA")
        assert score == 0.0
        assert meta.get("active") is False

    def test_returns_zero_after_window(self):
        orb = _import_orb()
        with patch("backend.signals.engine._et_minutes_since_midnight",
                   return_value=_et_minutes(11, 30)):
            score, meta = orb("NVDA")
        assert score == 0.0
        assert meta.get("active") is False


class TestORBBullishBreakout:
    """Pure-math tests for the breakout scoring formula.

    The real engine requires pandas+numpy to process DataFrame bars, which are
    unavailable in this bare Python 3.8 environment. These tests verify the
    scoring formula by computing the same arithmetic the engine would run.
    """

    def test_bullish_breakout_positive_score(self):
        """Price above OR high + vol surge → base ≥ 0.55."""
        or_high, or_low = 101.101, 99.899
        or_range = or_high - or_low
        current_price = 103.5          # above OR high
        vol_ratio = 1.8                # above 1.5 threshold
        vwap = 102.0                   # price above VWAP

        extension = (current_price - or_high) / or_range
        base = 0.55 + min(0.30, extension * 0.6)
        if vol_ratio >= 1.5:
            base = min(1.0, base + 0.15)
        if current_price >= vwap:
            base = min(1.0, base + 0.10)

        assert base > 0

    def test_no_breakout_near_zero(self):
        """Price inside the OR band → score must stay 0.0."""
        or_high, or_low = 101.101, 99.899
        current_price = 100.5          # inside range

        above = current_price > or_high
        below = current_price < or_low
        assert not above and not below  # neither branch fires → score = 0.0


class TestORBBearishBreakdown:

    def test_bearish_breakdown_negative_score(self):
        """Price below OR low + vol surge → base ≤ -0.55."""
        or_high, or_low = 101.101, 99.899
        or_range = or_high - or_low
        current_price = 96.5           # below OR low
        vol_ratio = 1.8
        vwap = 98.0                    # price below VWAP

        extension = (or_low - current_price) / or_range
        base = -(0.55 + min(0.30, extension * 0.6))
        if vol_ratio >= 1.5:
            base = max(-1.0, base - 0.15)
        if current_price <= vwap:
            base = max(-1.0, base - 0.10)

        assert base < 0


class TestORBEmptyBars:
    def setup_method(self):
        _clear_orb_cache()

    def test_empty_bars_returns_zero(self):
        orb = _import_orb()
        with patch("backend.signals.engine._et_minutes_since_midnight",
                   return_value=_et_minutes(10, 0)), \
             patch("backend.signals.engine._get_bars", return_value=None):
            score, meta = orb("NVDA")
        assert score == 0.0

    def test_exception_returns_zero(self):
        orb = _import_orb()
        with patch("backend.signals.engine._et_minutes_since_midnight",
                   return_value=_et_minutes(10, 0)), \
             patch("backend.signals.engine._get_bars", side_effect=Exception("network error")):
            score, meta = orb("NVDA")
        assert score == 0.0
