"""tests/test_grading.py — unit tests for backend/grading/engine.py"""
import pytest
from unittest.mock import MagicMock
from backend.grading.engine import (
    grade_setup, compute_sector_confirmation, get_ticker_percentile_rank,
    compute_percentile_thresholds, merge_percentile_window,
    effective_size_multiplier, grade_sort_key, SetupGrade,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_regime(market="bull", intraday="trending"):
    r = MagicMock()
    r.market_regime = market
    r.intraday_regime = intraday
    return r


def _bullish_signals(score=0.5):
    return {
        "macd_crossover":       {"score": score},
        "tape_aggression":      {"score": score},
        "relative_strength":    {"score": score},
        "order_book_imbalance": {"score": score},
        "bollinger_squeeze":    {"score": score},
        "rsi_divergence":       {"score": score},
        "news_sentiment":       {"score": score},
        "vwap_deviation":       {"score": -0.1},   # slightly above VWAP (fine for BUY)
    }


def _strong_db_percentiles(ticker="NVDA"):
    return {
        ticker: {
            "sample_count": 50,
            "p50": 0.10,
            "p70": 0.20,
            "p85": 0.30,
            "p90": 0.38,
            "p95": 0.45,
        }
    }


# ── grade_setup() ─────────────────────────────────────────────────────────────

class TestGradeSetup:
    def test_a_plus_all_conditions_met(self):
        grade = grade_setup(
            ticker="NVDA",
            composite=0.50,
            signals=_bullish_signals(0.5),
            regime_state=_make_regime("bull", "trending"),
            sector_conf=0.75,
            percentile_rank=90.0,
            orb_score=0.0,
            profile={},
        )
        assert grade.grade == "A+"
        assert grade.size_multiplier == 1.5
        assert grade.partial_exit_pct == 0.25
        assert grade.runner_atr_multiplier == 1.5
        assert grade.allow_leverage is True
        assert grade.confirmations >= 4

    def test_a_plus_via_orb_not_percentile(self):
        grade = grade_setup(
            ticker="NVDA",
            composite=0.40,
            signals=_bullish_signals(0.5),
            regime_state=_make_regime("bull", "trending"),
            sector_conf=0.75,
            percentile_rank=60.0,   # below 85 threshold
            orb_score=0.80,         # but ORB fires
            profile={},
        )
        assert grade.grade == "A+"
        assert grade.orb_active is True

    def test_a_plus_allows_above_vwap_when_momentum_confirms(self):
        signals = _bullish_signals(0.5)
        signals["vwap_deviation"]["score"] = -0.8
        grade = grade_setup(
            ticker="NVDA",
            composite=0.42,
            signals=signals,
            regime_state=_make_regime("bull", "trending"),
            sector_conf=0.75,
            percentile_rank=90.0,
            orb_score=0.0,
            profile={},
        )
        assert grade.grade == "A+"

    def test_a_grade(self):
        grade = grade_setup(
            ticker="QQQ",
            composite=0.30,
            signals=_bullish_signals(0.3),
            regime_state=_make_regime("bull", "trending"),
            sector_conf=0.55,
            percentile_rank=75.0,
            orb_score=0.0,
            profile={},
        )
        assert grade.grade == "A"
        assert grade.size_multiplier == 1.0
        assert grade.allow_leverage is False

    def test_b_grade_low_sector(self):
        # Only 2 signals aligned, low sector conf → B
        signals = _bullish_signals(0.0)
        signals["macd_crossover"]["score"] = 0.4
        signals["tape_aggression"]["score"] = 0.4
        grade = grade_setup(
            ticker="SPY",
            composite=0.20,
            signals=signals,
            regime_state=_make_regime("bull", "trending"),
            sector_conf=0.30,
            percentile_rank=55.0,
            orb_score=0.0,
            profile={},
        )
        assert grade.grade == "B"
        assert grade.size_multiplier == 0.6

    def test_c_grade_insufficient(self):
        grade = grade_setup(
            ticker="SPY",
            composite=0.03,
            signals=_bullish_signals(0.05),
            regime_state=_make_regime("bull", "ranging"),
            sector_conf=0.20,
            percentile_rank=30.0,
            orb_score=0.0,
            profile={},
        )
        assert grade.grade == "C"
        assert grade.size_multiplier == 0.0

    def test_a_plus_blocked_high_vol(self):
        grade = grade_setup(
            ticker="NVDA",
            composite=0.50,
            signals=_bullish_signals(0.5),
            regime_state=_make_regime("bull", "high_vol"),  # high_vol blocks A+
            sector_conf=0.75,
            percentile_rank=90.0,
            orb_score=0.0,
            profile={},
        )
        assert grade.grade in {"A", "B", "C"}

    def test_sell_bear_regime(self):
        grade = grade_setup(
            ticker="QQQ",
            composite=-0.45,
            signals={k: {"score": -v["score"]} for k, v in _bullish_signals(0.5).items()},
            regime_state=_make_regime("bear", "trending"),
            sector_conf=0.70,
            percentile_rank=88.0,
            orb_score=-0.6,
            profile={},
        )
        assert grade.grade == "A+"

    def test_buy_bear_regime_blocked(self):
        # BUY in bear market should not get A+
        grade = grade_setup(
            ticker="NVDA",
            composite=0.50,
            signals=_bullish_signals(0.5),
            regime_state=_make_regime("bear", "trending"),
            sector_conf=0.75,
            percentile_rank=92.0,
            orb_score=0.0,
            profile={},
        )
        assert grade.grade in {"B", "C"}


# ── compute_sector_confirmation() ────────────────────────────────────────────

class TestSectorConfirmation:
    def test_all_peers_aligned(self):
        composites = {"NVDA": 0.4, "SMH": 0.3, "AMD": 0.35, "AVGO": 0.25}
        sc = compute_sector_confirmation("NVDA", composites)
        assert sc == 1.0

    def test_no_peers_returns_neutral(self):
        composites = {"UNKNOWN": 0.4}
        sc = compute_sector_confirmation("UNKNOWN_TICKER", composites)
        assert sc == 0.5

    def test_half_peers_aligned(self):
        composites = {"NVDA": 0.4, "SMH": 0.3, "AMD": -0.3, "AVGO": -0.2}
        sc = compute_sector_confirmation("NVDA", composites)
        assert 0.3 < sc < 0.7

    def test_bear_direction_confirmed(self):
        composites = {"IBIT": -0.4, "COIN": -0.3, "MSTR": -0.35}
        sc = compute_sector_confirmation("IBIT", composites)
        assert sc == 1.0


# ── get_ticker_percentile_rank() ──────────────────────────────────────────────

class TestPercentileRank:
    def test_cold_start_above_threshold(self):
        rank = get_ticker_percentile_rank("NVDA", 0.30, {"NVDA": {"sample_count": 5}})
        assert rank == 75.0

    def test_cold_start_below_threshold(self):
        rank = get_ticker_percentile_rank("NVDA", 0.10, {"NVDA": {"sample_count": 5}})
        assert rank == 40.0

    def test_at_p95(self):
        db = _strong_db_percentiles("NVDA")
        rank = get_ticker_percentile_rank("NVDA", 0.50, db)
        assert rank == 97.0

    def test_between_p85_and_p90(self):
        db = _strong_db_percentiles("NVDA")
        rank = get_ticker_percentile_rank("NVDA", 0.35, db)
        assert rank == 86.0

    def test_below_p50(self):
        db = _strong_db_percentiles("NVDA")
        rank = get_ticker_percentile_rank("NVDA", 0.05, db)
        assert rank == 25.0

    def test_missing_ticker_cold_start(self):
        rank = get_ticker_percentile_rank("NOTEXIST", 0.25, {})
        assert rank == 75.0


# ── compute_percentile_thresholds() ──────────────────────────────────────────

class TestPercentileThresholds:
    def test_empty_window(self):
        result = compute_percentile_thresholds([])
        assert result["sample_count"] == 0
        assert result["p50"] is None

    def test_single_value(self):
        result = compute_percentile_thresholds([0.5])
        assert result["sample_count"] == 1
        assert result["p50"] == pytest.approx(0.5)

    def test_multiple_values(self):
        window = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
        result = compute_percentile_thresholds(window)
        assert result["sample_count"] == 10
        assert result["p50"] == pytest.approx(0.55, abs=0.05)
        assert result["p95"] == pytest.approx(0.955, abs=0.05)

    def test_uses_abs_values(self):
        window = [-0.5, -0.3, 0.3, 0.5]
        result = compute_percentile_thresholds(window)
        assert result["p50"] == pytest.approx(0.4, abs=0.05)


# ── merge_percentile_window() ────────────────────────────────────────────────

class TestMergeWindow:
    def test_appends_value(self):
        result = merge_percentile_window(0.3, [0.1, 0.2])
        assert result[-1] == 0.3
        assert len(result) == 3

    def test_caps_at_max_window(self):
        window = list(range(200))
        result = merge_percentile_window(999, window, max_window=200)
        assert len(result) == 200
        assert result[-1] == 999

    def test_empty_existing(self):
        result = merge_percentile_window(0.4, [])
        assert result == [0.4]


# ── effective_size_multiplier() ───────────────────────────────────────────────

class TestEffectiveSizeMultiplier:
    def test_a_plus_full(self):
        grade = SetupGrade("A+", 1.5, 0.25, 1.5, True, [], 5, 0.75, 90, False)
        result = effective_size_multiplier(grade, 1.0)
        assert result == pytest.approx(1.5)

    def test_a_plus_probe(self):
        grade = SetupGrade("A+", 1.5, 0.25, 1.5, True, [], 5, 0.75, 90, False)
        result = effective_size_multiplier(grade, 0.35)
        assert result == pytest.approx(min(1.5 * 0.35, 2.0), abs=0.01)

    def test_cap_at_2x(self):
        grade = SetupGrade("A+", 1.5, 0.25, 1.5, True, [], 5, 0.75, 90, False)
        result = effective_size_multiplier(grade, 1.5)
        assert result == pytest.approx(2.0)  # capped

    def test_b_grade(self):
        grade = SetupGrade("B", 0.6, 0.5, 0.8, False, [], 2, 0.4, 50, False)
        result = effective_size_multiplier(grade, 1.0)
        assert result == pytest.approx(0.6)


# ── grade_sort_key() ─────────────────────────────────────────────────────────

class TestGradeSortKey:
    def test_ordering(self):
        assert grade_sort_key("A+") > grade_sort_key("A")
        assert grade_sort_key("A") > grade_sort_key("B")
        assert grade_sort_key("B") > grade_sort_key("C")
        assert grade_sort_key("C") > grade_sort_key("X")
