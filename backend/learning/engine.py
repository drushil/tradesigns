"""
backend/learning/engine.py
The learning engine that makes the agent smarter over time.
Runs attribution after every trade, updates signal weights via EWA,
and generates weekly LLM insight digests.
"""
import os
import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta, date
from typing import Optional
try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv():
        return False

load_dotenv()

MIN_TRADES_TO_UPDATE = int(os.getenv("MIN_TRADES_TO_UPDATE", "15"))
EV_MIN_SAMPLE_SIZE   = int(os.getenv("EV_MIN_SAMPLE_SIZE", "8"))
EV_SHRINKAGE_TRADES  = int(os.getenv("EV_SHRINKAGE_TRADES", "20"))
EV_MIN_NET_PCT       = float(os.getenv("EV_MIN_NET_PCT", "0.03"))
DECAY_FACTOR         = 0.94 # EWA decay (~17 trade half-life)
LEARNING_RATE        = 0.05
MAX_WEIGHT           = 0.55
MIN_WEIGHT           = 0.03

# LLM model selection — override via GitHub Variable / .env to change without a code deploy.
# llama-3.3-70b-versatile: same free tier as 8b-instant, stronger reasoning,
#   better conviction calibration, supports JSON mode → cleaner parse path.
GROQ_DECISION_MODEL = os.getenv("GROQ_DECISION_MODEL", "llama-3.3-70b-versatile")
GROQ_SHADOW_DECISION_MODEL = os.getenv("GROQ_SHADOW_DECISION_MODEL", "llama-3.1-8b-instant")

_client = None


def _get_client():
    global _client
    if _client is None:
        from groq import Groq
        _client = Groq(api_key=os.getenv("GROQ_API_KEY"))
    return _client


# ── Attribution ───────────────────────────────────────────────────────────────

def attribute_signals(trade: dict) -> dict:
    """
    Given a completed trade record, compute how much credit each signal
    deserves for the outcome. Returns {signal_name: attribution_score}.
    """
    signals_at_entry = trade.get("signals_json", {})
    net_pnl          = trade.get("net_pnl_pct", 0.0) or 0.0
    outcome_magnitude = min(abs(net_pnl) / 1.0, 2.0)

    attributions = {}
    trade_side = str(trade.get("side") or "BUY").upper()
    for signal_name, signal_data in signals_at_entry.items():
        if isinstance(signal_data, dict):
            signal_score = signal_data.get("score", 0.0)
        else:
            signal_score = float(signal_data)

        directional_score = signal_score if trade_side == "BUY" else -signal_score
        signal_strength = abs(directional_score)

        # Did signal direction match outcome?
        if net_pnl > 0:
            direction_match = 1.0 if directional_score > 0 else -0.5
        else:
            direction_match = -1.0 if directional_score > 0 else 0.3

        raw_attr = signal_strength * direction_match * outcome_magnitude
        attributions[signal_name] = round(raw_attr, 4)

    return attributions


# ── EWA Weight Engine ─────────────────────────────────────────────────────────

class SignalWeightEngine:
    def __init__(self, priors: dict):
        self.weights  = {k: v for k, v in priors.items()}
        self.history  = {k: [] for k in priors}
        self.counts   = {k: 0  for k in priors}

    def _ewm_mean(self, series: list) -> float:
        if not series:
            return 0.0
        result = weight_sum = 0.0
        for i, val in enumerate(reversed(series[-50:])):  # cap at 50
            w = DECAY_FACTOR ** i
            result     += val * w
            weight_sum += w
        return result / weight_sum if weight_sum else 0.0

    def update(self, attributions: dict):
        for sig, attr in attributions.items():
            if sig not in self.weights:
                continue
            self.history[sig].append(attr)
            self.counts[sig]  += 1
            if self.counts[sig] < MIN_TRADES_TO_UPDATE:
                continue  # anti-overfitting gate
            ewm = self._ewm_mean(self.history[sig])
            delta = LEARNING_RATE * ewm
            self.weights[sig] = max(MIN_WEIGHT,
                                    min(MAX_WEIGHT, self.weights[sig] + delta))
        self._normalise()

    def _normalise(self):
        total = sum(self.weights.values())
        if total > 0:
            self.weights = {k: round(v / total, 4) for k, v in self.weights.items()}

    def sufficient_data(self) -> bool:
        return any(c >= MIN_TRADES_TO_UPDATE for c in self.counts.values())


class RegimeAwareWeightEngine:
    """Maintains separate weight sets per market regime + a global set."""

    REGIMES = ["global", "trending", "ranging", "high_vol", "news_driven"]

    def __init__(self, priors: dict):
        self.engines = {r: SignalWeightEngine(priors.copy()) for r in self.REGIMES}
        self._priors  = priors

    def update(self, attributions: dict, regime: str):
        self.engines["global"].update(attributions)
        if regime in self.engines:
            self.engines[regime].update(attributions)

    def get_weights(self, regime: str = "global") -> dict:
        if regime in self.engines and self.engines[regime].sufficient_data():
            rw = self.engines[regime].weights
            gw = self.engines["global"].weights
            # Blend: 70% regime-specific + 30% global
            return {k: round(rw[k] * 0.7 + gw.get(k, 0) * 0.3, 4) for k in rw}
        return self.engines["global"].weights

    def all_weights(self) -> dict:
        return {r: self.engines[r].weights for r in self.REGIMES}


def build_weight_engine_from_trades(priors: dict, trades: list) -> RegimeAwareWeightEngine:
    """
    Replays historical closed trades into a fresh engine.

    GitHub Actions-style scheduled runs are short-lived processes, so in-memory
    EWA history is not enough. Rebuilding from stored trades lets the learning
    threshold and decay actually accumulate across runs.
    """
    engine = RegimeAwareWeightEngine(priors)
    chronological = sorted(
        [t for t in trades if t.get("signals_json") and t.get("net_pnl_pct") is not None],
        key=lambda t: str(t.get("created_at") or ""),
    )
    for trade in chronological:
        engine.update(attribute_signals(trade), trade.get("regime") or "global")
    return engine


# ── Expected Value Gate ───────────────────────────────────────────────────────

def compute_expected_value(composite_score: float, size_eur: float,
                           trades: list, regime: str,
                           setup_context: dict = None,
                           profile: dict = None) -> dict:
    """
    Computes expected value of a potential trade net of all costs.
    Uses EV to choose full/reduced/probe sizing, blocking only when the
    setup quality does not justify the learned downside.
    """
    setup_context = setup_context or {}
    profile = profile or {}
    breakout_quality = float(setup_context.get("breakout_quality") or 0)
    event_risk = bool(setup_context.get("event_risk_intraday_probe"))
    strategy_family = str(setup_context.get("strategy_family") or "")
    momentum_setup = strategy_family in {"trend_following", "signal_composite"} and breakout_quality > 0

    reduced_floor = float(profile.get("ev_reduced_size_floor_pct", -0.02))
    probe_floor = float(profile.get("ev_probe_floor_pct", -0.10))
    breakout_min_quality = float(profile.get("ev_breakout_probe_min_quality", 0.65))
    reduced_multiplier = float(profile.get("ev_reduced_size_multiplier", 0.65))
    probe_multiplier = float(profile.get("ev_probe_size_multiplier", 0.35))
    event_multiplier = float(profile.get("event_risk_probe_size_multiplier", probe_multiplier))

    # Filter similar historical trades (same regime, similar score)
    if size_eur <= 0:
        return {
            "ev": None,
            "decision": "block",
            "ev_decision": "blocked",
            "size_multiplier": 0.0,
            "reason": "position size must be positive",
            "sample_size": 0,
            "confidence": 0.0,
            "exploration": False,
        }

    side = "BUY" if composite_score > 0 else "SELL"
    similar = [
        t for t in trades
        if t.get("regime") == regime
        and abs((t.get("composite_score") or 0) - composite_score) < 0.15
        and t.get("net_pnl_pct") is not None
        and ("side" not in t or t.get("side") == side)
    ]

    if len(similar) < EV_MIN_SAMPLE_SIZE:
        allow_exploration = os.getenv("EV_ALLOW_EXPLORATION", "true").strip().lower() != "false"
        if allow_exploration:
            if event_risk:
                ev_decision, multiplier = "event_probe_size", event_multiplier
            elif momentum_setup and breakout_quality >= breakout_min_quality:
                ev_decision, multiplier = "probe_size", probe_multiplier
            else:
                ev_decision, multiplier = "exploration_full_size", 1.0
            return {
                "ev": None,
                "decision": "proceed",
                "ev_decision": ev_decision,
                "size_multiplier": round(multiplier, 3),
                "sample_size": len(similar),
                "exploration": True,
                "confidence": 0.0,
                "breakout_quality": round(breakout_quality, 3),
                "reason": f"insufficient history ({len(similar)} similar trades)",
            }
        return {
            "ev": None,
            "decision": "block",
            "ev_decision": "blocked",
            "size_multiplier": 0.0,
            "sample_size": len(similar),
            "exploration": False,
            "confidence": 0.0,
            "breakout_quality": round(breakout_quality, 3),
            "reason": f"insufficient history ({len(similar)} similar trades)",
        }

    pnl_pcts  = [t["net_pnl_pct"] for t in similar]
    wins      = [p for p in pnl_pcts if p > 0]
    losses    = [p for p in pnl_pcts if p <= 0]
    win_rate  = len(wins) / len(pnl_pcts)
    avg_gain  = sum(wins)   / len(wins)   if wins   else 0.0
    avg_loss  = abs(sum(losses) / len(losses)) if losses else 0.0

    # Alpaca charges $0 commission; only slippage + LLM cost apply
    est_slippage   = size_eur * 0.0008
    llm_cost_eur   = 0.002
    total_cost_pct = (est_slippage + llm_cost_eur) / size_eur * 100

    gross_ev = (win_rate * avg_gain) - ((1 - win_rate) * avg_loss)
    shrinkage = len(similar) / (len(similar) + max(EV_SHRINKAGE_TRADES, 0))
    gross_ev *= shrinkage
    net_ev   = gross_ev - total_cost_pct

    if net_ev > EV_MIN_NET_PCT:
        decision, ev_decision, multiplier = "proceed", "full_size", 1.0
    elif net_ev >= reduced_floor:
        decision, ev_decision, multiplier = "proceed", "reduced_size", reduced_multiplier
    elif event_risk and breakout_quality >= breakout_min_quality and net_ev >= probe_floor:
        decision, ev_decision, multiplier = "proceed", "event_probe_size", event_multiplier
    elif momentum_setup and breakout_quality >= breakout_min_quality and net_ev >= probe_floor:
        decision, ev_decision, multiplier = "proceed", "probe_size", probe_multiplier
    else:
        decision, ev_decision, multiplier = "block", "blocked", 0.0

    return {
        "win_rate":       round(win_rate, 3),
        "avg_gain_pct":   round(avg_gain, 3),
        "avg_loss_pct":   round(avg_loss, 3),
        "gross_ev_pct":   round(gross_ev, 3),
        "total_cost_pct": round(total_cost_pct, 4),
        "net_ev_pct":     round(net_ev, 4),
        "sample_size":    len(similar),
        "confidence":     round(shrinkage, 3),
        "exploration":    False,
        "decision":       decision,
        "ev_decision":    ev_decision,
        "size_multiplier": round(multiplier, 3),
        "breakout_quality": round(breakout_quality, 3),
        "reason":         f"net EV {net_ev:.3f}% vs {EV_MIN_NET_PCT:.3f}% threshold",
    }


# ── Dynamic risk tightening ───────────────────────────────────────────────────

def get_effective_profile(base_profile: dict, portfolio_state: dict) -> dict:
    """
    Dynamically tightens or expands risk based on current portfolio conditions.

    Dynamic risk budget layers (applied in priority order):
      1. Hard daily-loss stop: drawdown >= 3% → no new trades at all
      2. Reduced mode:         drawdown >= 2% → minimum grade A only, reduced sizes
      3. Consecutive losses:   >= 3 losses    → halve size, raise conviction
      4. High VIX:             VIX > 25       → reduce size 30%, cap hold time
      5. Drawdown approaching: > 60% of limit → scale down further
      6. Risk-on loosening:    5+ wins        → small size increase (capped)
    """
    p = base_profile.copy()
    consecutive_losses = portfolio_state.get("consecutive_losses", 0)
    drawdown_today     = portfolio_state.get("drawdown_today", 0.0)
    consecutive_wins   = portfolio_state.get("consecutive_wins", 0)
    vix                = portfolio_state.get("vix", 20.0)

    # Layer 1: Hard daily-loss stop — no new entries
    if drawdown_today >= 3.0:
        p["max_trades_per_day"]    = 0
        p["min_signal_score"]      = 99.0   # effectively blocks everything
        p["daily_loss_limit_hit"]  = True
        p["capital_per_trade_pct"] = round(p["capital_per_trade_pct"], 2)
        p["risk_per_trade_pct"]    = round(p.get("risk_per_trade_pct", 1.0), 3)
        return p

    # Layer 2: Reduced mode — A-grade and above only, smaller sizes
    if drawdown_today >= 2.0:
        p["capital_per_trade_pct"] *= 0.5
        p["risk_per_trade_pct"]     = p.get("risk_per_trade_pct", 1.0) * 0.5
        p["min_conviction"]        = min(0.80, p["min_conviction"] + 0.10)
        p["min_grade_required"]    = "A"    # grade engine respects this
        p["reduced_mode"]          = True

    # Layer 3: Consecutive losses
    if consecutive_losses >= 3:
        p["capital_per_trade_pct"] *= 0.5
        p["risk_per_trade_pct"]     = p.get("risk_per_trade_pct", 1.0) * 0.5
        p["min_conviction"]        = min(0.85, p["min_conviction"] + 0.15)

    # Layer 4: High VIX
    if vix > 25:
        p["capital_per_trade_pct"] *= 0.7
        p["risk_per_trade_pct"]     = p.get("risk_per_trade_pct", 1.0) * 0.7
        p["max_hold_minutes"]       = min(p["max_hold_minutes"], 30)

    # Layer 5: Drawdown approaching limit
    drawdown_ratio = drawdown_today / max(p["max_drawdown_pct"], 0.01)
    if drawdown_ratio > 0.6:
        drawdown_scalar = max(0.3, 1 - drawdown_ratio * 0.5)
        p["capital_per_trade_pct"] *= drawdown_scalar
        p["risk_per_trade_pct"]     = p.get("risk_per_trade_pct", 1.0) * drawdown_scalar

    # Layer 6: Risk-on loosening after sustained winning
    if consecutive_wins >= 5:
        base_cap = base_profile["capital_per_trade_pct"] * 1.2
        p["capital_per_trade_pct"] = min(
            p["capital_per_trade_pct"] * 1.1, base_cap
        )
        base_risk = base_profile.get("risk_per_trade_pct", 1.0) * 1.2
        p["risk_per_trade_pct"] = min(
            p.get("risk_per_trade_pct", 1.0) * 1.1, base_risk
        )

    p["capital_per_trade_pct"] = round(p["capital_per_trade_pct"], 2)
    p["risk_per_trade_pct"] = round(p.get("risk_per_trade_pct", 1.0), 3)
    return p


# ── Weekly LLM Digest ─────────────────────────────────────────────────────────

def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return default


def build_weekly_eod_evidence(daily_reviews: list) -> dict:
    """Aggregate daily EOD reviews into evidence governance for weekly learning."""
    if not daily_reviews:
        return {
            "review_days": 0,
            "guardrail": (
                "No daily EOD evidence supplied. Do not recommend trading logic changes "
                "unless trade data alone shows an urgent safety issue."
            ),
        }

    trade_totals = {"trades": 0, "wins": 0, "losses": 0, "pnl_eur": 0.0}
    gate_counts = Counter()
    missed = Counter()
    runners = Counter()
    bad_avoids = Counter()
    direction_errors = Counter()
    shadow = defaultdict(lambda: {"mentions": 0, "days": set(), "theme": None})
    near_total = near_checked = near_runners = 0
    rec_counts = Counter()

    for review in daily_reviews:
        day = str(review.get("review_date") or review.get("created_at") or "")[:10]
        metrics = review.get("metrics_json") or {}
        review_json = review.get("review_json") or {}
        trade = metrics.get("trade_summary") or {}
        trade_totals["trades"] += int(trade.get("total_trades") or 0)
        trade_totals["wins"] += int(trade.get("wins") or 0)
        trade_totals["losses"] += int(trade.get("losses") or 0)
        trade_totals["pnl_eur"] += _safe_float(trade.get("total_pnl_eur"))

        for name, count in ((metrics.get("gate_activity") or {}).get("event_counts") or {}).items():
            gate_counts[str(name)] += int(count or 0)

        blocked = metrics.get("blocked_opportunities") or {}
        for item in blocked.get("missed_winners") or []:
            ticker = str(item.get("ticker") or "unknown").upper()
            missed[ticker] += 1
            if item.get("runner_severity") == "runner":
                runners[ticker] += 1
        for item in blocked.get("bad_avoids") or []:
            bad_avoids[str(item.get("ticker") or "unknown").upper()] += 1

        near = metrics.get("near_miss_distribution") or {}
        near_total += int(near.get("total") or 0)
        near_checked += int(near.get("checked") or 0)
        near_runners += int(near.get("runner_count") or 0)

        for item in metrics.get("direction_error_candidates") or []:
            direction_errors[str(item.get("ticker") or "unknown").upper()] += 1

        for item in metrics.get("shadow_universe") or []:
            ticker = str(item.get("ticker") or "").upper()
            if not ticker:
                continue
            shadow[ticker]["mentions"] += int(item.get("mentions") or 0)
            shadow[ticker]["theme"] = item.get("theme")
            if day:
                shadow[ticker]["days"].add(day)

        for rec in (review.get("recommendations_json") or review_json.get("recommendations") or []):
            key = str(rec.get("variable") or rec.get("category") or rec.get("reason") or "unknown")
            rec_counts[key] += 1

    return {
        "review_days": len(daily_reviews),
        "trade_totals": {
            **trade_totals,
            "pnl_eur": round(trade_totals["pnl_eur"], 2),
            "win_rate_pct": round(
                trade_totals["wins"] / trade_totals["trades"] * 100, 1
            ) if trade_totals["trades"] else 0.0,
        },
        "gate_event_totals": dict(gate_counts.most_common(12)),
        "missed_winner_tickers": dict(missed.most_common(10)),
        "runner_tickers": dict(runners.most_common(10)),
        "bad_avoid_tickers": dict(bad_avoids.most_common(10)),
        "near_miss_distribution": {
            "total": near_total,
            "checked": near_checked,
            "runner_count": near_runners,
            "sample_size_ready": near_checked >= 20 and len(daily_reviews) >= 10,
        },
        "direction_error_tickers": dict(direction_errors.most_common(10)),
        "shadow_candidates": [
            {
                "ticker": ticker,
                "theme": data["theme"],
                "mentions": data["mentions"],
                "evidence_days": len(data["days"]),
            }
            for ticker, data in sorted(
                shadow.items(),
                key=lambda item: (len(item[1]["days"]), item[1]["mentions"]),
                reverse=True,
            )[:12]
        ],
        "repeated_recommendations": dict(rec_counts.most_common(8)),
        "decision_guardrails": {
            "daily_reviews_are_observation": True,
            "trading_logic_change_requires": ">=10-20 comparable cases across multiple days/regimes",
            "near_threshold_change_requires": "at least 14 days or 20 checked near-threshold cases, whichever comes later",
            "universe_promotion_requires": "at least 2 evidence_days and human approval",
            "operational_bugs": "fix immediately",
        },
    }


def compute_hold_score(
    ticker: str,
    trade: dict,
    current_signals: dict,
    hold_elapsed_minutes: float = 0.0,
) -> dict:
    """
    Separate 'should we stay in?' score, decoupled from the entry composite.

    Entry composite rewards acceleration at the moment of entry.
    Hold score rewards persistence: are signals still aligned now that we're in?

    Signal weights differ from entry composite:
    - tape_aggression (0.30) — primary continuation signal
    - relative_strength (0.25) — is this ticker still leading its sector?
    - macd_crossover (0.20) — is momentum structure intact?
    - vwap_structure (binary ±0.30) — is price on the right side of VWAP?
    - rsi_divergence (0.10) — supporting
    - news_sentiment (0.05) — drives entry, not continuation

    Penalties subtracted from raw score:
    - exhaustion_penalty: price stretched > 0.5 VWAP-score units from VWAP
    - time_decay_penalty: mild drag in last 20% of the hold window

    Returns a dict with hold_score [-1..1], recommendation, confidence, components.
    """
    def _sig(name: str) -> float:
        v = (current_signals or {}).get(name) or {}
        if isinstance(v, dict):
            return float(v.get("score", 0) or 0)
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    side             = str(trade.get("side") or "BUY").upper()
    direction        = 1 if side == "BUY" else -1
    is_mean_reversion = bool(trade.get("mean_reversion_trade"))

    def _c(v: float) -> float:
        return max(-1.0, min(1.0, v))

    # Directional signal scores (positive = good for the trade direction)
    tape    = _c(_sig("tape_aggression")    * direction)
    rel_str = _c(_sig("relative_strength")  * direction)
    macd    = _c(_sig("macd_crossover")     * direction)
    rsi     = _c(_sig("rsi_divergence")     * direction)
    news    = _c(_sig("news_sentiment")     * direction)
    vwap_raw = _sig("vwap_deviation")  # not flipped — see below

    # VWAP structure: binary ±0.30 based on which side of VWAP price is on.
    # vwap_score > 0 = price BELOW VWAP; vwap_score < 0 = price ABOVE VWAP.
    # Trend trade (BUY): want price ABOVE VWAP (vwap_raw < -0.1 = good).
    # Mean-reversion trade (BUY): bought below VWAP, closing toward it = good.
    if is_mean_reversion:
        vwap_struct = max(0.0, 0.30 - abs(vwap_raw) * 1.5)
    else:
        if (direction == 1 and vwap_raw < -0.10) or (direction == -1 and vwap_raw > 0.10):
            vwap_struct = 0.30
        elif (direction == 1 and vwap_raw > 0.20) or (direction == -1 and vwap_raw < -0.20):
            vwap_struct = -0.30
        else:
            vwap_struct = 0.0

    tape_c    = tape    * 0.30
    rel_c     = rel_str * 0.25
    macd_c    = macd    * 0.20
    rsi_c     = rsi     * 0.10
    news_c    = news    * 0.05

    raw_score = tape_c + rel_c + macd_c + rsi_c + news_c + vwap_struct

    # Exhaustion penalty: price stretched far from VWAP
    # vwap score magnitude > 0.5 ≈ >0.33% deviation. ATR-stretch proxy.
    exhaustion_mag = abs(vwap_raw)
    exhaustion_penalty = 0.0
    exhaustion_active  = False
    if exhaustion_mag > 0.50:
        exhaustion_penalty = min(0.25, (exhaustion_mag - 0.50) * 0.50)
        exhaustion_active  = True

    # Time decay: mild drag in last 20% of hold window
    time_decay_penalty = 0.0
    hold_target = float(trade.get("max_hold_minutes") or trade.get("hold_minutes") or 30)
    decay_start = hold_target * 0.80
    if hold_elapsed_minutes > decay_start and hold_target > decay_start:
        decay_ratio        = min(1.0, (hold_elapsed_minutes - decay_start) / max(hold_target * 0.20, 1))
        time_decay_penalty = decay_ratio * 0.15

    hold_score = max(-1.0, min(1.0, raw_score - exhaustion_penalty - time_decay_penalty))

    # Recommendation thresholds (conservative defaults)
    exit_thresh   = float(os.getenv("HOLD_SCORE_EXIT_THRESHOLD",   "-0.50"))
    trim_thresh   = float(os.getenv("HOLD_SCORE_TRIM_THRESHOLD",   "0.10"))
    extend_thresh = float(os.getenv("HOLD_SCORE_EXTEND_THRESHOLD", "0.45"))

    if hold_score >= extend_thresh:
        recommendation = "extend"
    elif hold_score > trim_thresh:
        recommendation = "hold"
    elif hold_score > exit_thresh:
        recommendation = "trim"
    else:
        recommendation = "exit"

    confidence = min(1.0, abs(hold_score) * 2.0)

    return {
        "hold_score":        round(hold_score, 4),
        "recommendation":    recommendation,
        "confidence":        round(confidence, 3),
        "exhaustion_active": exhaustion_active,
        "components": {
            "tape_contribution":              round(tape_c,     4),
            "relative_strength_contribution": round(rel_c,      4),
            "macd_contribution":              round(macd_c,     4),
            "rsi_contribution":               round(rsi_c,      4),
            "news_contribution":              round(news_c,     4),
            "vwap_structure_contribution":    round(vwap_struct, 4),
            "exhaustion_penalty":             round(-exhaustion_penalty,  4),
            "time_decay_penalty":             round(-time_decay_penalty,  4),
        },
    }


def generate_weekly_insights(trades: list, daily_reviews: list = None) -> list:
    """
    Sends the week's trades to Claude Sonnet for qualitative pattern extraction.
    Returns a list of actionable insight dicts.
    Uses Sonnet (not Haiku) — called once/week so cost is minimal (~€0.05).
    """
    eod_evidence = build_weekly_eod_evidence(daily_reviews or [])

    if len(trades) < 5 and not daily_reviews:
        return [{"insight": "Insufficient trades for analysis (need ≥5)",
                 "action": "Continue paper trading to build history",
                 "confidence": 1.0}]

    # Build compact trade summary
    summary_rows = []
    for t in trades[-50:]:  # max 50 trades
        summary_rows.append(
            f"{t.get('ticker','?')} | {t.get('side','?')} | "
            f"score={t.get('composite_score',0):.2f} | "
            f"net_pnl={t.get('net_pnl_pct',0):.3f}% | "
            f"regime={t.get('regime','?')} | "
            f"hold={t.get('hold_minutes',0)}min | "
            f"exit={t.get('exit_reason','?')}"
        )
    summary = "\n".join(summary_rows)

    # Stats
    wins      = [t for t in trades if (t.get("net_pnl_pct") or 0) > 0]
    win_rate  = len(wins) / len(trades) * 100 if trades else 0
    avg_pnl   = sum(t.get("net_pnl_pct", 0) for t in trades) / len(trades) if trades else 0

    prompt = f"""You are a quantitative trading analyst reviewing paper trading data.

SUMMARY STATS:
- Total trades: {len(trades)}
- Win rate: {win_rate:.1f}%
- Average net P&L: {avg_pnl:.3f}%

TRADE LOG (most recent 50):
{summary}

WEEKLY EOD EVIDENCE:
{json.dumps(eod_evidence, default=str)[:10000]}

Analyse these trades and EOD evidence. Identify 4-6 CONCRETE patterns.
Look for: time-of-day effects, signal combinations that work/fail,
regime-specific patterns, holding duration effects, ticker preferences.
Also review repeated EOD observations: missed runners, bad avoids,
near-threshold distribution, direction-error candidates, shadow universe,
and repeated recommendations.

Separate observation from action:
- Operational bugs, schema gaps, broken logging, or safety issues can be urgent fixes.
- Trading logic changes require >=10-20 comparable cases across multiple days/regimes.
- Near-threshold trading changes require at least 14 days OR 20 checked near-threshold
  cases across multiple regimes, whichever comes later.
- Universe promotion requires >=2 evidence_days and human approval.
- If evidence is insufficient, say observe_only.

Output ONLY a valid JSON array, no other text:
[
  {{
    "insight": "One clear factual observation from the data",
    "action": "Specific next step, or observe_only if evidence is insufficient",
    "confidence": 0.0-1.0,
    "category": "signals|timing|risk|costs|regime|universe|instrumentation|operations",
    "action_class": "observe_only|ready_for_human_decision|urgent_fix|reject",
    "sample_size": 0,
    "evidence_days": 0
  }}
]"""

    try:
        response = _get_client().chat.completions.create(
            model=os.getenv("GROQ_DIGEST_MODEL", "llama-3.3-70b-versatile"),
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.choices[0].message.content.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        insights = json.loads(raw.strip())
        return insights if isinstance(insights, list) else []
    except Exception as e:
        return [{"insight": f"Analysis error: {str(e)[:100]}",
                 "action": "Check GROQ_API_KEY and trade data format",
                 "confidence": 0.0, "category": "error"}]


# ── LLM Signal Decision ──────────────────────────────────────────────────────

def llm_signal_decision(ticker: str, composite_score: float,
                         regime: str, news_headline: str,
                         profile: dict,
                         signal_scores: dict = None,
                         atr_data: dict = None,
                         regime_context: dict = None,
                         trade_context: dict = None,
                         model: str = None,
                         shadow: bool = False) -> dict:
    """
    Signal interpretation via Groq (GROQ_DECISION_MODEL, default llama-3.3-70b-versatile).

    Uses Groq JSON mode — the response is guaranteed to be valid JSON so no
    regex stripping needed.  Conviction is set by the model based on signal
    alignment, setup grade, and EV; it is not a canned value.

    Args:
        trade_context: optional dict with keys grade, breakout_quality, ev_net_pct —
                       supplied by the entry pipeline when available.
        model: optional model override for shadow/A-B decisions.
        shadow: marks this result as non-executing telemetry.
    """
    max_hold     = profile.get("max_hold_minutes", 60)
    stop_default = profile.get("stop_loss_pct", 2.0)

    # Individual signal breakdown
    signal_lines = ""
    if signal_scores:
        rows = []
        for sig, data in signal_scores.items():
            if sig == "earnings_proximity":
                continue
            score = data.get("score", 0) if isinstance(data, dict) else data
            if score is not None:
                rows.append(f"  {sig}: {float(score):+.3f}")
        if rows:
            signal_lines = "\nIndividual signals (each -1 to +1):\n" + "\n".join(rows)

    # Regime and volatility context
    market_regime = (regime_context or {}).get("market_regime", "")
    vix           = (regime_context or {}).get("vix", "")
    vix_str       = f"\nVIX: {vix}" if vix else ""
    regime_str    = f"Intraday: {regime}" + (f" | Market: {market_regime}" if market_regime else "")
    atr_str       = ""
    if atr_data:
        atr_pct = atr_data.get("atr_pct")
        vol_reg = atr_data.get("volatility_regime", "")
        if atr_pct:
            atr_str = f"\nATR: {float(atr_pct):.3f}% ({vol_reg})"

    # Setup quality line from entry pipeline
    ctx_lines = ""
    if trade_context:
        grade     = trade_context.get("grade")
        bq        = trade_context.get("breakout_quality")
        ev_pct    = trade_context.get("ev_net_pct")
        parts = []
        if grade:
            parts.append(f"Setup grade: {grade}")
        if bq is not None:
            try:
                parts.append(f"Breakout quality: {float(bq):.2f}")
            except (TypeError, ValueError):
                pass
        if ev_pct is not None:
            try:
                parts.append(f"Expected value: {float(ev_pct):+.3f}%")
            except (TypeError, ValueError):
                pass
        if parts:
            ctx_lines = "\n" + " | ".join(parts)

    prompt = f"""Ticker: {ticker}
Composite score: {composite_score:+.3f}  (scale: -1 bearish → +1 bullish){signal_lines}
Regime: {regime_str}{vix_str}{atr_str}{ctx_lines}
News: {news_headline[:150] if news_headline else 'none'}
Profile: {profile.get('display_name', 'moderate')} | Max hold: {max_hold} min | Default stop: {stop_default}%

Task: decide whether to act on this signal. Return a JSON object.
1. Count how many individual signals agree with the composite direction.
2. Set conviction proportional to agreement strength:
   - 0.2-0.4 = weak (1-2 signals align, rest flat or opposing)
   - 0.5-0.6 = moderate (majority align)
   - 0.7-0.85 = strong (most signals agree, composite > 0.25)
3. Return HOLD if signals conflict, composite is near zero, or VIX is elevated with weak alignment.
4. In bull market regimes, favour BUY when composite is positive and momentum signals align.
   Choose SELL only when bearish evidence is clear — not a minor pullback.
5. Setup grade A+ with breakout quality ≥ 0.75 is a high-conviction setup; use conviction 0.70+
   when signal alignment supports it.
6. If grade is B or C, require composite > 0.3 and clear signal agreement before going above 0.5 conviction.
7. Set hold_minutes and stop_loss_pct based on ATR and volatility regime.

JSON schema (respond with ONLY this object):
{{"action":"BUY|SELL|HOLD","conviction":0.0,"hold_minutes":0,"stop_loss_pct":0.0,"rationale":"one sentence"}}"""

    try:
        selected_model = model or GROQ_DECISION_MODEL
        response = _get_client().chat.completions.create(
            model=selected_model,
            max_tokens=150,
            temperature=0.2,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a quantitative trading signal interpreter. "
                        "Analyse signal alignment carefully and calibrate conviction accordingly. "
                        "Respond ONLY with the JSON object specified — no markdown, no commentary."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
        )
        raw = response.choices[0].message.content.strip()
        result = json.loads(raw)
        if isinstance(result, dict):
            result.setdefault("model", selected_model)
            result.setdefault("shadow", bool(shadow))
        return result
    except Exception as e:
        return {"action": "HOLD", "conviction": 0.0,
                "hold_minutes": 0, "rationale": f"llm_error: {str(e)[:50]}",
                "model": model or GROQ_DECISION_MODEL, "shadow": bool(shadow)}
