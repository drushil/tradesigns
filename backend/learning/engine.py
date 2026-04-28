"""
backend/learning/engine.py
The learning engine that makes the agent smarter over time.
Runs attribution after every trade, updates signal weights via EWA,
and generates weekly LLM insight digests.
"""
import os
import json
from datetime import datetime, timedelta, date
from typing import Optional
import anthropic
from dotenv import load_dotenv

load_dotenv()

MIN_TRADES_TO_UPDATE = 15   # lower for paper trading bootstrap
DECAY_FACTOR         = 0.94 # EWA decay (~17 trade half-life)
LEARNING_RATE        = 0.05
MAX_WEIGHT           = 0.55
MIN_WEIGHT           = 0.03

_client: Optional[anthropic.Anthropic] = None


def _get_client():
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
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
    for signal_name, signal_data in signals_at_entry.items():
        if isinstance(signal_data, dict):
            signal_score = signal_data.get("score", 0.0)
        else:
            signal_score = float(signal_data)

        signal_strength = abs(signal_score)

        # Did signal direction match outcome?
        if net_pnl > 0:
            direction_match = 1.0 if signal_score > 0 else -0.5
        else:
            direction_match = -1.0 if signal_score > 0 else 0.3

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


# ── Expected Value Gate ───────────────────────────────────────────────────────

def compute_expected_value(composite_score: float, size_eur: float,
                           trades: list, regime: str) -> dict:
    """
    Computes expected value of a potential trade net of all costs.
    Blocks trades with negative EV.
    """
    # Filter similar historical trades (same regime, similar score)
    similar = [
        t for t in trades
        if t.get("regime") == regime
        and abs((t.get("composite_score") or 0) - composite_score) < 0.15
        and t.get("net_pnl_pct") is not None
    ]

    if len(similar) < 8:
        return {"ev": None, "decision": "proceed",
                "reason": f"insufficient history ({len(similar)} similar trades)"}

    pnl_pcts  = [t["net_pnl_pct"] for t in similar]
    wins      = [p for p in pnl_pcts if p > 0]
    losses    = [p for p in pnl_pcts if p <= 0]
    win_rate  = len(wins) / len(pnl_pcts)
    avg_gain  = sum(wins)   / len(wins)   if wins   else 0.0
    avg_loss  = abs(sum(losses) / len(losses)) if losses else 0.0

    # Real costs
    commission   = max(1.25, size_eur * 0.0005)
    est_slippage = size_eur * 0.0008
    llm_cost_eur = 0.002
    total_cost_pct = (commission + est_slippage + llm_cost_eur) / size_eur * 100

    gross_ev = (win_rate * avg_gain) - ((1 - win_rate) * avg_loss)
    net_ev   = gross_ev - total_cost_pct

    return {
        "win_rate":       round(win_rate, 3),
        "avg_gain_pct":   round(avg_gain, 3),
        "avg_loss_pct":   round(avg_loss, 3),
        "gross_ev_pct":   round(gross_ev, 3),
        "total_cost_pct": round(total_cost_pct, 4),
        "net_ev_pct":     round(net_ev, 4),
        "sample_size":    len(similar),
        "decision":       "proceed" if net_ev > 0.03 else "block",
        "reason":         f"net EV {net_ev:.3f}% vs 0.03% threshold",
    }


# ── Dynamic risk tightening ───────────────────────────────────────────────────

def get_effective_profile(base_profile: dict, portfolio_state: dict) -> dict:
    """Dynamically tightens risk when conditions deteriorate."""
    p = base_profile.copy()
    consecutive_losses = portfolio_state.get("consecutive_losses", 0)
    drawdown_today     = portfolio_state.get("drawdown_today", 0.0)
    consecutive_wins   = portfolio_state.get("consecutive_wins", 0)
    vix                = portfolio_state.get("vix", 20.0)

    # Tighten after 3+ consecutive losses
    if consecutive_losses >= 3:
        p["capital_per_trade_pct"] *= 0.5
        p["min_conviction"]        = min(0.85, p["min_conviction"] + 0.15)

    # Tighten in high-vol
    if vix > 25:
        p["capital_per_trade_pct"] *= 0.7
        p["max_hold_minutes"]       = min(p["max_hold_minutes"], 30)

    # Tighten as drawdown approaches limit
    drawdown_ratio = drawdown_today / max(p["max_drawdown_pct"], 0.01)
    if drawdown_ratio > 0.6:
        p["capital_per_trade_pct"] *= max(0.3, 1 - drawdown_ratio * 0.5)

    # Very slight loosening after 5+ consecutive wins (capped)
    if consecutive_wins >= 5:
        base_cap = base_profile["capital_per_trade_pct"] * 1.2
        p["capital_per_trade_pct"] = min(
            p["capital_per_trade_pct"] * 1.1, base_cap
        )

    p["capital_per_trade_pct"] = round(p["capital_per_trade_pct"], 2)
    return p


# ── Weekly LLM Digest ─────────────────────────────────────────────────────────

def generate_weekly_insights(trades: list) -> list:
    """
    Sends the week's trades to Claude Sonnet for qualitative pattern extraction.
    Returns a list of actionable insight dicts.
    Uses Sonnet (not Haiku) — called once/week so cost is minimal (~€0.05).
    """
    if len(trades) < 5:
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
    win_rate  = len(wins) / len(trades) * 100
    avg_pnl   = sum(t.get("net_pnl_pct", 0) for t in trades) / len(trades)

    prompt = f"""You are a quantitative trading analyst reviewing paper trading data.

SUMMARY STATS:
- Total trades: {len(trades)}
- Win rate: {win_rate:.1f}%
- Average net P&L: {avg_pnl:.3f}%

TRADE LOG (most recent 50):
{summary}

Analyse these trades and identify 4-6 CONCRETE, ACTIONABLE patterns.
Look for: time-of-day effects, signal combinations that work/fail,
regime-specific patterns, holding duration effects, ticker preferences.

Output ONLY a valid JSON array, no other text:
[
  {{
    "insight": "One clear factual observation from the data",
    "action": "Specific parameter or behaviour to change",
    "confidence": 0.0-1.0,
    "category": "signals|timing|risk|costs|regime"
  }}
]"""

    try:
        response = _get_client().messages.create(
            model="claude-sonnet-4-6",
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = response.content[0].text.strip()
        # Strip markdown fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        insights = json.loads(raw.strip())
        return insights if isinstance(insights, list) else []
    except Exception as e:
        return [{"insight": f"Analysis error: {str(e)[:100]}",
                 "action": "Check API key and trade data format",
                 "confidence": 0.0, "category": "error"}]


# ── LLM Signal Decision (Haiku — fast & cheap) ───────────────────────────────

def llm_signal_decision(ticker: str, composite_score: float,
                         regime: str, news_headline: str,
                         profile: dict) -> dict:
    """
    Fast Haiku call for signal interpretation.
    Cost: ~€0.001 per call. Gated by pre-trade rules.
    """
    prompt = f"""Ticker: {ticker}
Signal score: {composite_score:.3f} (range -1 to +1, positive=bullish)
Regime: {regime}
News: {news_headline[:120] if news_headline else 'none'}
Risk profile: {profile.get('display_name','moderate')}
Max hold: {profile.get('max_hold_minutes',60)} minutes
Stop loss: {profile.get('stop_loss_pct',2.0)}%

Reply ONLY with valid JSON, no other text:
{{"action":"BUY","conviction":0.72,"hold_minutes":30,"stop_loss_pct":2.0,"rationale":"momentum with news support"}}"""

    try:
        response = _get_client().messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=120,
            messages=[{"role": "user", "content": prompt}],
            system=(
                "You are a trading signal interpreter. "
                "Reply ONLY with the exact JSON format requested. "
                "No explanation, no markdown, just JSON."
            )
        )
        raw = response.content[0].text.strip()
        return json.loads(raw)
    except Exception as e:
        return {"action": "HOLD", "conviction": 0.0,
                "hold_minutes": 0, "rationale": f"llm_error: {str(e)[:50]}"}
