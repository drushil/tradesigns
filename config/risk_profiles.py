"""
config/risk_profiles.py
Defines all risk profiles. The active profile is loaded from .env
and passed through every layer of the agent.

2026-05-23 — three calibration changes applied to all profiles:
  1. take_profit_pct raised so TP >= 1.5× stop_loss_pct (aligns with
     min_reward_risk_ratio; previously most profiles failed _reward_risk_block)
  2. paper_overrides min_signal_score floored at 0.15 for all profiles
     (previously 0.05–0.10, generating near-random cold-start learning data)
  3. signal_weights rebalanced: reduce news_sentiment (0.07–0.08),
     put_call_ratio (0.03–0.04), order_book_imbalance (0.14–0.18);
     increase tape_aggression and relative_strength as primary leading signals.
     RSI and Bollinger kept flat (confirmation, not leading).

2026-05-23 (exit/sizing cycle) — three further changes applied to all profiles:
  4. min_grade_required set to "A" across all profiles — B grade is shadow-only
     until post-May-22 evidence confirms it has positive edge (1/8 win rate,
     -€11.75 net from Apr-30 to May-21 is not a tuning problem).
  5. allow_b_grade_exploration set to False — no tiny B exploration trades;
     _record_blocked_opportunity handles shadow tracking automatically.
  6. ranging_atr_stop_multiple added (1.5) — in ranging regime the stop is
     widened to max(profile_stop, ATR × 1.5) with proportional size reduction,
     giving trades room to reach the time-exit positive drift zone rather than
     being stopped out by intraday noise (37 stop-outs = -€196.92 Apr-May).
"""

RISK_PROFILES = {
    "conservative": {
        "display_name": "Conservative",
        "max_drawdown_pct": 5.0,
        "max_position_pct": 8.0,
        "risk_per_trade_pct": 0.75,
        "capital_per_trade_pct": 2.0,
        "cash_buffer_pct": 30.0,
        "stop_loss_pct": 1.0,
        "take_profit_pct": 1.5,          # was 1.2 — now 1.5× stop (passes min_rr=1.5)
        "max_trade_notional_eur": 1500,
        "min_conviction": 0.75,
        "min_signal_score": 0.50,
        "paper_overrides": {
            "min_signal_score": 0.16,    # already fine
            "min_conviction": 0.42,
            "max_trades_per_day": 8,
            "min_hold_minutes": 10,
            "max_hold_minutes": 90,
        },
        "vix_ceiling": 20,
        "max_trades_per_day": 3,
        "min_hold_minutes": 30,
        "max_hold_minutes": 240,
        "allowed_instruments": ["SPY", "QQQ", "GLD", "TLT"],
        "allow_individual_stocks": False,
        "allow_leveraged_etfs": False,
        "allow_short_selling": False,
        "max_short_position_pct": 0.0,
        "min_short_signal_score": 0.60,
        "dominant_signal_veto_threshold": 0.65,
        "max_swing_hold_days": 2,
        "max_concurrent_swings": 1,
        "max_overnight_carries": 1,
        "eod_carry_max_loss_r": 0.35,
        "swing_conviction_threshold": 0.80,
        "learned_hold_min_conviction": 0.80,
        "learned_hold_min_signal_score": 0.35,
        "learned_hold_min_minutes": 120,
        "learned_hold_max_minutes": 240,
        "hold_extension_minutes": 15,
        "hold_extension_max_count": 2,
        "hold_extension_min_pnl_pct": 0.05,
        "hold_extension_min_signal_score": 0.20,
        "hold_extension_fade_score": 0.10,
        "time_exit_cooldown_minutes": 60,
        "min_grade_required": "A",          # B grade shadow-only — 1/8 win rate, -€11.75 net
        "allow_b_grade_exploration": False, # no tiny exploration trades; shadow tracked automatically
        "ranging_atr_stop_multiple": 1.5,   # widen ranging stop to ATR×1.5; proportional size cut
        "signal_weights": {
            "order_book_imbalance": 0.14,  # kept — defensive profile, lower turnover
            "tape_aggression":      0.15,  # was 0.10
            "rsi_divergence":       0.18,  # was 0.22 — reduced but still meaningful for conservative
            "news_sentiment":       0.08,  # was 0.16
            "vwap_deviation":       0.12,  # was 0.11
            "macd_crossover":       0.09,  # was 0.07
            "relative_strength":    0.12,  # was 0.06
            "bollinger_squeeze":    0.08,  # unchanged
            "put_call_ratio":       0.04,  # was 0.06
        },
    },
    "cautious": {
        "display_name": "Cautious",
        "max_drawdown_pct": 8.0,
        "max_position_pct": 12.0,
        "risk_per_trade_pct": 0.90,
        "capital_per_trade_pct": 3.0,
        "cash_buffer_pct": 25.0,
        "stop_loss_pct": 1.5,
        "take_profit_pct": 2.3,          # was 1.6 — now 1.53× stop (passes min_rr=1.5)
        "max_trade_notional_eur": 2500,
        "min_conviction": 0.70,
        "min_signal_score": 0.42,
        "paper_overrides": {
            "min_signal_score": 0.15,    # was 0.14 — floored to 0.15
            "min_conviction": 0.38,
            "max_trades_per_day": 12,
            "min_hold_minutes": 8,
            "max_hold_minutes": 60,
        },
        "vix_ceiling": 25,
        "max_trades_per_day": 5,
        "min_hold_minutes": 20,
        "max_hold_minutes": 180,
        "allowed_instruments": ["SPY", "QQQ", "GLD", "TLT", "IVV"],
        "allow_individual_stocks": False,
        "allow_leveraged_etfs": False,
        "allow_short_selling": False,
        "max_short_position_pct": 0.0,
        "min_short_signal_score": 0.50,
        "dominant_signal_veto_threshold": 0.72,
        "max_swing_hold_days": 3,
        "max_concurrent_swings": 1,
        "max_overnight_carries": 1,
        "eod_carry_max_loss_r": 0.40,
        "swing_conviction_threshold": 0.75,
        "learned_hold_min_conviction": 0.80,
        "learned_hold_min_signal_score": 0.35,
        "learned_hold_min_minutes": 120,
        "learned_hold_max_minutes": 240,
        "hold_extension_minutes": 20,
        "hold_extension_max_count": 2,
        "hold_extension_min_pnl_pct": 0.05,
        "hold_extension_min_signal_score": 0.20,
        "hold_extension_fade_score": 0.10,
        "time_exit_cooldown_minutes": 60,
        "min_grade_required": "A",
        "allow_b_grade_exploration": False,
        "ranging_atr_stop_multiple": 1.5,
        "signal_weights": {
            "order_book_imbalance": 0.15,  # unchanged — cautious keeps snapshot edge
            "tape_aggression":      0.17,  # was 0.13
            "rsi_divergence":       0.15,  # was 0.19
            "news_sentiment":       0.08,  # was 0.15
            "vwap_deviation":       0.11,  # was 0.10
            "macd_crossover":       0.09,  # was 0.07
            "relative_strength":    0.13,  # was 0.06
            "bollinger_squeeze":    0.08,  # was 0.09
            "put_call_ratio":       0.04,  # was 0.06
        },
    },
    "moderate": {
        "display_name": "Moderate",
        "max_drawdown_pct": 10.0,
        "max_position_pct": 15.0,
        "risk_per_trade_pct": 1.0,
        "capital_per_trade_pct": 5.0,
        "cash_buffer_pct": 15.0,
        "stop_loss_pct": 2.0,
        "take_profit_pct": 3.0,          # was 2.2 — now 1.5× stop (passes min_rr=1.5)
        "max_trade_notional_eur": 5000,
        "min_conviction": 0.60,
        "min_signal_score": 0.35,
        "paper_overrides": {
            "min_signal_score": 0.15,    # was 0.10 — floored to 0.15
            "min_conviction": 0.35,
            "max_trades_per_day": 30,
            "min_hold_minutes": 5,
            "max_hold_minutes": 30,
        },
        "vix_ceiling": 30,
        "max_trades_per_day": 8,
        "min_hold_minutes": 15,
        "max_hold_minutes": 90,
        "allowed_instruments": ["SPY", "QQQ", "GLD", "TLT", "AAPL", "TSLA"],
        "allow_individual_stocks": True,
        "allow_leveraged_etfs": False,
        "allow_short_selling": True,
        "max_short_position_pct": 8.0,
        "min_short_signal_score": 0.18,
        "bull_short_signal_score": 0.22,
        "dominant_signal_veto_threshold": 0.80,
        "max_swing_hold_days": 3,
        "max_concurrent_swings": 2,
        "max_overnight_carries": 1,
        "eod_carry_max_loss_r": 0.45,
        "swing_conviction_threshold": 0.70,
        "learned_hold_min_conviction": 0.80,
        "learned_hold_min_signal_score": 0.35,
        "learned_hold_min_minutes": 120,
        "learned_hold_max_minutes": 240,
        "hold_extension_minutes": 30,
        "hold_extension_max_count": 2,
        "hold_extension_min_pnl_pct": 0.05,
        "hold_extension_min_signal_score": 0.20,
        "hold_extension_fade_score": 0.10,
        "time_exit_cooldown_minutes": 60,
        "min_grade_required": "A",
        "allow_b_grade_exploration": False,
        "ranging_atr_stop_multiple": 1.5,
        "signal_weights": {
            "order_book_imbalance": 0.18,  # was 0.21
            "tape_aggression":      0.22,  # was 0.17
            "rsi_divergence":       0.10,  # was 0.11
            "news_sentiment":       0.08,  # was 0.14
            "vwap_deviation":       0.08,  # was 0.07
            "macd_crossover":       0.10,  # was 0.08
            "relative_strength":    0.14,  # was 0.07
            "bollinger_squeeze":    0.07,  # was 0.10
            "put_call_ratio":       0.03,  # was 0.05
        },
    },
    "growth": {
        "display_name": "Growth",
        "max_drawdown_pct": 15.0,
        "max_position_pct": 20.0,
        "risk_per_trade_pct": 1.25,
        "capital_per_trade_pct": 8.0,
        "cash_buffer_pct": 10.0,
        "stop_loss_pct": 2.5,
        "take_profit_pct": 3.8,          # was 3.0 — now 1.52× stop (passes min_rr=1.5)
        "max_trade_notional_eur": 8000,
        "min_conviction": 0.55,
        "min_signal_score": 0.30,
        "paper_overrides": {
            "min_signal_score": 0.15,    # was 0.08 — floored to 0.15
            "min_conviction": 0.32,
            "max_trades_per_day": 40,
            "min_hold_minutes": 5,
            "max_hold_minutes": 25,
        },
        "vix_ceiling": 35,
        "max_trades_per_day": 12,
        "min_hold_minutes": 10,
        "max_hold_minutes": 60,
        "allowed_instruments": ["SPY", "QQQ", "IWM", "GLD", "TLT", "XOP", "XLF", "NVDA", "TSLA", "IBIT", "SMH", "PLTR", "AVGO", "AMD", "META"],
        "allow_individual_stocks": True,
        "allow_leveraged_etfs": False,
        "allow_short_selling": True,
        "max_short_position_pct": 12.0,
        "min_short_signal_score": 0.15,
        "bull_short_signal_score": 0.19,
        "dominant_signal_veto_threshold": 0.85,
        "max_swing_hold_days": 5,
        "max_concurrent_swings": 2,
        "max_overnight_carries": 1,
        "eod_carry_max_loss_r": 0.50,
        "swing_conviction_threshold": 0.65,
        "learned_hold_min_conviction": 0.80,
        "learned_hold_min_signal_score": 0.35,
        "learned_hold_min_minutes": 120,
        "learned_hold_max_minutes": 240,
        "hold_extension_minutes": 30,
        "hold_extension_max_count": 3,
        "hold_extension_min_pnl_pct": 0.05,
        "hold_extension_min_signal_score": 0.18,
        "hold_extension_fade_score": 0.08,
        "time_exit_cooldown_minutes": 45,
        "min_grade_required": "A",
        "allow_b_grade_exploration": False,
        "ranging_atr_stop_multiple": 1.5,
        "signal_weights": {
            "order_book_imbalance": 0.18,  # was 0.21
            "tape_aggression":      0.24,  # was 0.20
            "rsi_divergence":       0.08,  # was 0.09
            "news_sentiment":       0.07,  # was 0.13
            "vwap_deviation":       0.08,  # was 0.07
            "macd_crossover":       0.10,  # was 0.09
            "relative_strength":    0.15,  # was 0.07
            "bollinger_squeeze":    0.07,  # was 0.10
            "put_call_ratio":       0.03,  # was 0.04
        },
    },
    "ultra_aggressive": {
        "display_name": "Ultra-Aggressive",
        "max_drawdown_pct": 20.0,
        "max_position_pct": 30.0,
        "risk_per_trade_pct": 2.0,
        "capital_per_trade_pct": 15.0,
        "cash_buffer_pct": 3.0,
        "stop_loss_pct": 3.5,
        "take_profit_pct": 5.3,          # was 4.5 — now 1.51× stop (passes min_rr=1.5)
        "max_trade_notional_eur": 15000,
        "min_conviction": 0.45,
        "min_signal_score": 0.20,
        "paper_overrides": {
            "min_signal_score": 0.15,    # was 0.05 — floored to 0.15
            "min_conviction": 0.28,
            "max_trades_per_day": 80,
            "min_hold_minutes": 2,
            "max_hold_minutes": 15,
        },
        "vix_ceiling": 22,           # strict VIX cap for leveraged ETF eligibility
        "max_trades_per_day": 30,
        "min_hold_minutes": 3,
        "max_hold_minutes": 30,
        "allowed_instruments": [
            "SPY", "QQQ", "IWM", "GLD", "TLT", "XOP", "XLE", "XLF",
            "NVDA", "AMD", "TSLA", "META", "AMZN", "PLTR", "AVGO",
            "SMH", "IBIT", "COIN", "MSTR", "ARM",
        ],
        "allow_individual_stocks": True,
        "allow_leveraged_etfs": True,    # TQQQ/SOXL gated by A+ grade + VIX < 22
        "leveraged_etf_tickers": ["TQQQ", "SOXL", "NVDL"],
        "leveraged_etf_max_hold_minutes": 345,  # exit by 3:45 PM ET — never swing
        "leveraged_etf_vix_ceiling": 22,
        "allow_short_selling": True,
        "max_short_position_pct": 15.0,
        "min_short_signal_score": 0.10,
        "bull_short_signal_score": 0.14,
        "dominant_signal_veto_threshold": 0.92,
        "max_swing_hold_days": 7,
        "max_concurrent_swings": 3,
        "max_overnight_carries": 2,
        "eod_carry_max_loss_r": 0.60,
        "swing_conviction_threshold": 0.58,
        "learned_hold_min_conviction": 0.80,
        "learned_hold_min_signal_score": 0.35,
        "learned_hold_min_minutes": 60,
        "learned_hold_max_minutes": 180,
        "hold_extension_minutes": 15,
        "hold_extension_max_count": 4,
        "hold_extension_min_pnl_pct": 0.03,
        "hold_extension_min_signal_score": 0.14,
        "hold_extension_fade_score": 0.06,
        "time_exit_cooldown_minutes": 20,
        "min_grade_required": "A",
        "allow_b_grade_exploration": False,
        "ranging_atr_stop_multiple": 1.5,
        "signal_weights": {
            "order_book_imbalance": 0.17,  # was 0.24
            "tape_aggression":      0.25,  # was 0.22
            "rsi_divergence":       0.06,  # unchanged
            "news_sentiment":       0.07,  # was 0.10
            "vwap_deviation":       0.08,  # was 0.06
            "macd_crossover":       0.11,  # unchanged
            "relative_strength":    0.16,  # was 0.07
            "bollinger_squeeze":    0.07,  # was 0.10
            "put_call_ratio":       0.03,  # was 0.04
        },
    },
    "aggressive": {
        "display_name": "Aggressive",
        "max_drawdown_pct": 20.0,
        "max_position_pct": 25.0,
        "risk_per_trade_pct": 1.5,
        "capital_per_trade_pct": 12.0,
        "cash_buffer_pct": 5.0,
        "stop_loss_pct": 3.0,
        "take_profit_pct": 4.5,          # was 3.8 — now 1.5× stop (passes min_rr=1.5)
        "max_trade_notional_eur": 12000,
        "min_conviction": 0.50,
        "min_signal_score": 0.25,
        "paper_overrides": {
            "min_signal_score": 0.15,    # was 0.06 — floored to 0.15
            "min_conviction": 0.30,
            "max_trades_per_day": 60,
            "min_hold_minutes": 3,
            "max_hold_minutes": 20,
        },
        "vix_ceiling": 50,
        "max_trades_per_day": 20,
        "min_hold_minutes": 5,
        "max_hold_minutes": 45,
        "allowed_instruments": ["SPY", "QQQ", "GLD", "TLT", "AAPL", "TSLA", "NVDA", "META", "AMZN"],
        "allow_individual_stocks": True,
        "allow_leveraged_etfs": False,
        "allow_short_selling": True,
        "max_short_position_pct": 15.0,
        "min_short_signal_score": 0.12,
        "bull_short_signal_score": 0.16,
        "dominant_signal_veto_threshold": 0.90,
        "max_swing_hold_days": 7,
        "max_concurrent_swings": 3,
        "max_overnight_carries": 2,
        "eod_carry_max_loss_r": 0.55,
        "swing_conviction_threshold": 0.60,
        "learned_hold_min_conviction": 0.80,
        "learned_hold_min_signal_score": 0.35,
        "learned_hold_min_minutes": 120,
        "learned_hold_max_minutes": 240,
        "hold_extension_minutes": 30,
        "hold_extension_max_count": 3,
        "hold_extension_min_pnl_pct": 0.05,
        "hold_extension_min_signal_score": 0.16,
        "hold_extension_fade_score": 0.08,
        "time_exit_cooldown_minutes": 30,
        "min_grade_required": "A",
        "allow_b_grade_exploration": False,
        "ranging_atr_stop_multiple": 1.5,
        "signal_weights": {
            "order_book_imbalance": 0.17,  # was 0.23
            "tape_aggression":      0.25,  # was 0.20
            "rsi_divergence":       0.06,  # was 0.07
            "news_sentiment":       0.07,  # was 0.11
            "vwap_deviation":       0.08,  # was 0.07
            "macd_crossover":       0.11,  # was 0.10
            "relative_strength":    0.16,  # was 0.07
            "bollinger_squeeze":    0.07,  # was 0.11
            "put_call_ratio":       0.03,  # was 0.04
        },
    },
}


def get_profile(name: str) -> dict:
    profile = RISK_PROFILES.get(name, RISK_PROFILES["moderate"]).copy()
    profile["_name"] = name
    return profile
