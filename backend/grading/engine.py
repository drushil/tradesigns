"""
backend/grading/engine.py
Setup grading engine: assigns A+/A/B/C grades to trade candidates based on
adaptive composite percentile rank, sector confirmation, and signal alignment.

Grade drives capital allocation, runner behaviour, and A+ do-not-miss escalation.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

# ── Sector correlation map ─────────────────────────────────────────────────────
# For each ticker, list the 2-4 most correlated peers to check for confirmation.
# Peers must be in TICKER_UNIVERSE to contribute composite scores at runtime.
SECTOR_MAP: dict[str, list[str]] = {
    # Semis cluster — NVDA leads, AMD/AVGO/SMH follow
    "NVDA":  ["SMH", "AMD", "AVGO"],
    "AMD":   ["NVDA", "SMH", "AVGO"],
    "SMH":   ["NVDA", "AMD", "AVGO"],
    "AVGO":  ["NVDA", "AMD", "SMH"],
    "MU":    ["NVDA", "AMD", "SMH"],
    "ARM":   ["NVDA", "AMD", "SMH"],
    # Broad tech / growth
    "QQQ":   ["SPY", "META", "NVDA"],
    "META":  ["QQQ", "PLTR"],
    "PLTR":  ["QQQ", "META"],
    "TSLA":  ["QQQ", "NVDA"],
    # Crypto proxy
    "IBIT":  ["COIN", "MSTR"],
    "COIN":  ["IBIT", "MSTR"],
    "MSTR":  ["COIN", "IBIT"],
    # Broad market
    "SPY":   ["QQQ", "IWM"],
    "IWM":   ["SPY", "QQQ"],
    # Macro / defensive
    "GLD":   ["TLT"],
    "TLT":   ["GLD"],
    # Energy
    "XOP":   ["XLF"],
    "XLF":   ["SPY"],
    # Leveraged (confirm via parent)
    "TQQQ":  ["QQQ", "NVDA"],
    "SOXL":  ["SMH", "NVDA", "AMD"],
    "NVDL":  ["NVDA", "SMH"],
}

# Minimum sample count before percentile logic is trusted; below this falls back to absolute threshold
_MIN_PERCENTILE_SAMPLES = 20
_COLD_START_THRESHOLD = 0.20   # absolute composite threshold used when history < 20 samples


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class SetupGrade:
    grade: str                      # "A+", "A", "B", "C"
    size_multiplier: float          # applied on top of profile capital_per_trade_pct
    partial_exit_pct: float         # fraction of position to exit at first target
    runner_atr_multiplier: float    # ATR multiplier for runner trailing stop
    allow_leverage: bool            # allow leveraged ETF substitution
    reasons: list[str]              # human-readable grade factors
    confirmations: int              # number of aligned individual signals
    sector_confirmation: float      # 0-1 peer alignment score
    percentile_rank: float          # 0-100 composite rank vs recent history
    orb_active: bool                # ORB signal contributed to grade

    def __str__(self) -> str:
        return (
            f"Grade={self.grade} size_mult={self.size_multiplier:.2f} "
            f"confs={self.confirmations} sector={self.sector_confirmation:.0%} "
            f"pct={self.percentile_rank:.0f}"
        )


# ── Sector confirmation ────────────────────────────────────────────────────────

def compute_sector_confirmation(ticker: str, all_cycle_composites: dict[str, float]) -> float:
    """
    Returns 0-1. Measures how many sector peers show aligned signals this cycle.
    0.5 = neutral (no peer data). 1.0 = all peers strongly aligned.

    Uses the composites already computed for this cycle — zero extra fetches.
    """
    ticker_up = ticker.upper()
    peers = SECTOR_MAP.get(ticker_up, [])
    if not peers:
        return 0.5  # no confirmation data available — neutral

    ticker_composite = all_cycle_composites.get(ticker_up, 0.0)
    direction = 1 if ticker_composite >= 0 else -1

    aligned = 0
    total = 0
    for peer in peers:
        peer_composite = all_cycle_composites.get(peer.upper())
        if peer_composite is None:
            continue
        total += 1
        if peer_composite * direction > 0.05:
            aligned += 1

    if total == 0:
        return 0.5
    return round(aligned / total, 3)


# ── Percentile ranking ────────────────────────────────────────────────────────

def get_ticker_percentile_rank(
    ticker: str,
    composite: float,
    db_percentiles: dict[str, dict],
) -> float:
    """
    Returns percentile rank (0-100) of `composite` vs the stored rolling window
    for this ticker. Falls back to 0 when history is too thin.

    db_percentiles: {ticker: {sample_count, p50, p70, p85, p90, p95}}
    """
    ticker_up = ticker.upper()
    data = db_percentiles.get(ticker_up, {})
    sample_count = int(data.get("sample_count") or 0)
    abs_composite = abs(composite)

    if sample_count < _MIN_PERCENTILE_SAMPLES:
        # Cold-start: use simple absolute threshold
        if abs_composite >= _COLD_START_THRESHOLD:
            return 75.0
        return 40.0

    p50 = float(data.get("p50") or 0)
    p70 = float(data.get("p70") or 0)
    p85 = float(data.get("p85") or 0)
    p90 = float(data.get("p90") or 0)
    p95 = float(data.get("p95") or 0)

    if abs_composite >= p95:
        return 97.0
    elif abs_composite >= p90:
        return 91.0
    elif abs_composite >= p85:
        return 86.0
    elif abs_composite >= p70:
        return 74.0
    elif abs_composite >= p50:
        return 54.0
    else:
        return 25.0


def compute_percentile_thresholds(window: list[float]) -> dict:
    """
    Given a list of absolute composite values, return percentile thresholds.
    """
    if not window:
        return {"sample_count": 0, "p50": None, "p70": None, "p85": None, "p90": None, "p95": None}

    sorted_vals = sorted(abs(v) for v in window)
    n = len(sorted_vals)

    def _pct(p: float) -> float:
        idx = (p / 100) * (n - 1)
        lo, hi = int(idx), min(int(idx) + 1, n - 1)
        frac = idx - lo
        return round(sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac, 4)

    return {
        "sample_count": n,
        "p50": _pct(50),
        "p70": _pct(70),
        "p85": _pct(85),
        "p90": _pct(90),
        "p95": _pct(95),
    }


def merge_percentile_window(
    composite: float,
    existing_window: list[float],
    max_window: int = 200,
) -> list[float]:
    """
    Append composite to the rolling window, capping at max_window entries.
    """
    window = list(existing_window or [])
    window.append(composite)
    if len(window) > max_window:
        window = window[-max_window:]
    return window


# ── Main grading function ─────────────────────────────────────────────────────

def grade_setup(
    ticker: str,
    composite: float,
    signals: dict,
    regime_state,              # RegimeState from signals.engine
    sector_conf: float,
    percentile_rank: float,    # 0-100
    orb_score: float,
    profile: dict,
) -> SetupGrade:
    """
    Grade a candidate setup A+/A/B/C.

    A+ requires ALL of:
      - Percentile >= 85 OR ORB breakout active (orb_score aligned and > 0.5)
      - Sector confirmation >= 0.67 (2+ of 3 peers aligned)
      - 4+ individual signals aligned with composite direction
      - Bull/transitioning regime for BUY; bear/transitioning for SELL
      - Not in high_vol intraday regime
      - VWAP not strongly against the trade (vwap_dev aligned or neutral)

    A requires:
      - Percentile >= 70
      - Sector confirmation >= 0.50
      - 3+ signals aligned
      - Acceptable regime

    B: 2+ signals aligned, composite above min threshold
    C: insufficient — system skips by default (EV gate may still allow probe)
    """
    direction = 1 if composite >= 0 else -1
    abs_composite = abs(composite)

    # ── Signal alignment count ────────────────────────────────────────────────
    _SIGNAL_NAMES = [
        "macd_crossover", "tape_aggression", "relative_strength",
        "order_book_imbalance", "bollinger_squeeze",
        "rsi_divergence", "news_sentiment",
    ]
    confirmations = 0
    for sig_name in _SIGNAL_NAMES:
        val = signals.get(sig_name)
        score = float((val or {}).get("score", 0) if isinstance(val, dict) else (val or 0))
        if score * direction > 0.08:
            confirmations += 1

    # ── ORB ───────────────────────────────────────────────────────────────────
    orb_active = abs(orb_score) > 0.5 and (orb_score * direction > 0)

    # VWAP: strongly above VWAP can be overextension, but for confirmed
    # momentum/ORB breakouts it is expected and should not block A+.
    vwap_val = signals.get("vwap_deviation")
    vwap_score = float((vwap_val or {}).get("score", 0) if isinstance(vwap_val, dict) else 0)
    # vwap_score > 0 means price is BELOW vwap (bullish for mean-reversion)
    # vwap_score < 0 means price is ABOVE vwap
    macd_score = float((signals.get("macd_crossover") or {}).get("score", 0) or 0)
    tape_score = float((signals.get("tape_aggression") or {}).get("score", 0) or 0)
    rel_score = float((signals.get("relative_strength") or {}).get("score", 0) or 0)
    momentum_confirms = (
        macd_score * direction > 0.25
        and tape_score * direction > 0.20
        and rel_score * direction > 0.20
    )
    vwap_not_against = (
        (direction == 1 and (vwap_score >= -0.5 or momentum_confirms or orb_active)) or
        (direction == -1 and (vwap_score <= 0.5 or momentum_confirms or orb_active))
    )

    # ── Regime ────────────────────────────────────────────────────────────────
    market_regime = getattr(regime_state, "market_regime", "transitioning")
    intraday_regime = getattr(regime_state, "intraday_regime", "ranging")

    regime_ok = (
        (direction == 1 and market_regime in {"bull", "transitioning"}) or
        (direction == -1 and market_regime in {"bear", "transitioning"})
    )
    not_high_vol = intraday_regime != "high_vol"

    # ── Grade determination ───────────────────────────────────────────────────
    reasons: list[str] = []

    a_plus_conditions = {
        "percentile_or_orb": percentile_rank >= 85 or orb_active,
        "sector_conf":        sector_conf >= 0.67,
        "confirmations":      confirmations >= 4,
        "regime_ok":          regime_ok,
        "not_high_vol":       not_high_vol,
        "vwap_ok":            vwap_not_against,
    }

    a_conditions = {
        "percentile":    percentile_rank >= 70,
        "sector_conf":   sector_conf >= 0.50,
        "confirmations": confirmations >= 3,
        "regime_ok":     regime_ok,
    }

    if all(a_plus_conditions.values()):
        grade = "A+"
        size_multiplier = 1.5
        partial_exit_pct = 0.25   # small partial — let winners run
        runner_atr_mult = 1.5
        allow_leverage = True
        reasons = [f"pct_{int(percentile_rank)}", f"sector_{sector_conf:.0%}",
                   f"{confirmations}sigs"]
        if orb_active:
            reasons.append("orb")
    elif all(a_conditions.values()):
        grade = "A"
        size_multiplier = 1.0
        partial_exit_pct = 0.40
        runner_atr_mult = 1.0
        allow_leverage = False
        reasons = [f"pct_{int(percentile_rank)}", f"{confirmations}sigs"]
    elif confirmations >= 2 and abs_composite > 0.05:
        grade = "B"
        size_multiplier = 0.6
        partial_exit_pct = 0.50
        runner_atr_mult = 0.8
        allow_leverage = False
        reasons = [f"{confirmations}sigs"]
    else:
        grade = "C"
        size_multiplier = 0.0
        partial_exit_pct = 1.0
        runner_atr_mult = 0.5
        allow_leverage = False
        reasons = ["insufficient_confirmation"]

    return SetupGrade(
        grade=grade,
        size_multiplier=size_multiplier,
        partial_exit_pct=partial_exit_pct,
        runner_atr_multiplier=runner_atr_mult,
        allow_leverage=allow_leverage,
        reasons=reasons,
        confirmations=confirmations,
        sector_confirmation=sector_conf,
        percentile_rank=percentile_rank,
        orb_active=orb_active,
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

_GRADE_SORT_ORDER = {"A+": 4, "A": 3, "B": 2, "C": 1}


def grade_sort_key(grade: str) -> int:
    return _GRADE_SORT_ORDER.get(grade, 0)


def effective_size_multiplier(setup_grade: SetupGrade, ev_size_multiplier: float) -> float:
    """
    Combine grade-based sizing with EV-based sizing.
    Grade multiplier caps at 2.0 to prevent runaway position sizes.
    """
    combined = setup_grade.size_multiplier * ev_size_multiplier
    return round(min(combined, 2.0), 3)


def a_plus_hard_blocks() -> set[str]:
    """Block reasons that apply even to A+ setups and cannot be overridden."""
    return {
        "daily_loss_limit",
        "max_drawdown",
        "no_data",
        "spread_too_wide",
        "already_open",
        "broker_error",
        "eod_cleanup",
        "too_late_session",
        "overexposed_sector",
    }
