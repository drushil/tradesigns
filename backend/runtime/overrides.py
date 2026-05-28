"""
backend/runtime/overrides.py
Profile enrichment: applies trading-system defaults and env-var overrides to a
risk-profile dict before each signal cycle.

Pure function — no DB, no broker calls, no side effects.
IS_PAPER_TRADING is read from backend.runtime.state (bridged there by agent.py
at startup) so callers don't need to pass it.
"""
from __future__ import annotations

import os

import backend.runtime.state as state
from backend.runtime.env import _env_bool, _env_float, _env_int, _env_value


def _apply_execution_overrides(profile: dict) -> dict:
    """Stamp system defaults and env overrides onto a copy of *profile*."""
    p = profile.copy()
    p.setdefault("min_grade_required", "A")
    p.setdefault("allow_b_grade_exploration", False)
    p.setdefault("b_grade_size_multiplier", 0.20)
    p.setdefault("ev_reduced_size_floor_pct", -0.02)
    p.setdefault("ev_probe_floor_pct", -0.10)
    p.setdefault("ev_breakout_probe_min_quality", 0.65)
    p.setdefault("ev_reduced_size_multiplier", 0.65)
    p.setdefault("ev_probe_size_multiplier", 0.35)
    p.setdefault("allow_event_risk_intraday_probes", True)
    p.setdefault("event_risk_probe_min_score", 0.32)
    p.setdefault("event_risk_probe_min_macd", 0.35)
    p.setdefault("event_risk_probe_min_tape", 0.25)
    p.setdefault("event_risk_probe_min_relative_strength", 0.35)
    p.setdefault("event_risk_probe_size_multiplier", 0.30)
    p.setdefault("event_risk_max_hold_minutes", 30)
    p.setdefault("event_risk_stop_multiplier", 0.75)
    p.setdefault("event_risk_min_stop_pct", 0.25)
    p.setdefault("event_risk_latest_entry_utc_hour", 19)
    p.setdefault("max_new_intraday_trades_per_cycle", 2)
    p.setdefault("pdt_protection_enabled", True)
    p.setdefault("pdt_max_day_trades_5d", 3)
    p.setdefault("pdt_min_equity_usd", 25000)
    p.setdefault("leveraged_etf_stop_scalar", 1.35)
    p.setdefault("a_plus_full_size_max_atr_pct", 2.5)
    p.setdefault("a_plus_full_size_max_stop_pct", 5.0)
    p.setdefault("high_atr_stop_threshold_pct", 1.0)
    p.setdefault("high_atr_stop_multiplier", 2.5)
    p.setdefault("grade_ev_override_negative_min_samples", 10)
    p.setdefault("probe_floor_inflation_max_multiple", 1.25)
    p.setdefault("ranging_regime_size_multiplier", 0.35)
    p.setdefault("ranging_max_trades_per_day", 6)
    p.setdefault("ranging_atr_stop_multiple", 1.5)      # widen stop to ATR×1.5 in ranging
    p.setdefault("ranging_min_grade_required", "A+")
    p.setdefault("ranging_a_grade_min_breakout_quality", 0.80)
    p.setdefault("ranging_a_grade_min_ev_pct", 0.25)
    p.setdefault("ranging_a_plus_min_composite", 0.25)
    p.setdefault("ranging_a_plus_min_breakout_quality", 0.70)
    p.setdefault("ranging_a_plus_min_ev_pct", 0.20)
    p.setdefault("ranging_max_notional_eur", 1200)
    p.setdefault("ranging_leveraged_min_ev_pct", 0.25)
    p.setdefault("ranging_probe_enabled", True)
    p.setdefault("ranging_probe_allowed_grades", "A+,A")
    p.setdefault("ranging_probe_shadow_grades", "B")
    p.setdefault("ranging_probe_size_multiplier", 0.35)
    p.setdefault("ranging_probe_grade_b_shadow_size_multiplier", 0.20)
    p.setdefault("ranging_probe_grade_b_min_composite", 0.40)
    p.setdefault("ranging_probe_grade_b_min_breakout_quality", 0.60)
    p.setdefault("ranging_probe_grade_b_promote_min_samples", 8)
    p.setdefault("ranging_probe_grade_b_promote_min_win_rate", 0.55)
    p.setdefault("ranging_probe_grade_b_promote_requires_mfe_gt_mae", True)
    p.setdefault("ranging_probe_grade_b_promote_size_multiplier", 0.20)
    p.setdefault("ranging_probe_min_ev_pct", 0.03)
    p.setdefault("ranging_probe_min_composite", 0.20)
    p.setdefault("ranging_probe_min_breakout_quality", 0.35)
    p.setdefault("ranging_probe_min_aligned_signals", 2)
    p.setdefault("ranging_probe_min_macd", 0.10)
    p.setdefault("ranging_probe_min_tape", 0.10)
    p.setdefault("ranging_probe_min_relative_strength", 0.25)
    p.setdefault("ranging_probe_max_tape_against", 0.05)
    p.setdefault("ranging_probe_min_sector_relative_pct", -0.50)
    p.setdefault("ranging_probe_blocked_themes", "")
    p.setdefault("thesis_invalidated_cooldown_minutes", 75)
    p.setdefault("ranging_stop_loss_cooldown_minutes", 90)
    p.setdefault("min_reward_risk_ratio", 1.5)
    p.setdefault("ranging_min_reward_risk_ratio", 2.0)
    p.setdefault("signal_consensus_min_count", 3)
    p.setdefault("ranging_signal_consensus_min_count", 4)
    p.setdefault("ranging_core_consensus_min_count", 2)
    p.setdefault("ranging_core_consensus_enabled", True)
    p.setdefault("signal_consensus_min_strength", 0.15)
    p.setdefault("max_open_positions_per_theme", 2)
    p.setdefault("allow_a_plus_llm_hold_override", False)
    p.setdefault("alignment_orb_veto_threshold", 0.50)
    p.setdefault("alignment_tape_veto_threshold", 0.40)
    p.setdefault("alignment_put_call_veto_threshold", 0.50)
    p.setdefault("alignment_rsi_veto_threshold", 0.50)
    p.setdefault("sector_momentum_bonus_enabled", True)
    p.setdefault("sector_momentum_lookback_period", "5d")
    p.setdefault("sector_momentum_leadership_threshold_pct", 2.0)
    p.setdefault("sector_momentum_max_bonus", 0.15)
    p.setdefault("theme_max_candidates_per_cycle", 2)
    p.setdefault("theme_max_leveraged_candidates_per_cycle", 1)
    p.setdefault("dynamic_universe_shadow_enabled", True)
    p.setdefault("dynamic_universe_max_shadow_per_theme", 2)
    # Phase 1: runner trail + breakeven promotion
    p.setdefault("runner_active_trail_enabled", True)
    p.setdefault("breakeven_promotion_enabled", True)
    p.setdefault("breakeven_atr_mult", 0.6)
    # Phase 2: hold score
    p.setdefault("hold_score_enabled", True)
    p.setdefault("hold_score_extend_enabled", True)    # on: upside only, low risk
    p.setdefault("hold_score_trim_enabled", False)     # off: enable after validation
    p.setdefault("hold_score_exit_enabled", False)     # off: enable after validation
    p.setdefault("hold_score_extend_minutes", 30)
    p.setdefault("hold_score_trim_pct", 0.33)
    # Phase 3: entry precision
    p.setdefault("late_chase_block_enabled", True)
    p.setdefault("late_chase_atr_mult", 1.5)
    p.setdefault("rvol_gate_enabled", True)
    p.setdefault("rvol_min_multiplier", 1.3)
    p.setdefault("vwap_1m_confirm_enabled", True)
    # Phase 4: advisory-only gate intelligence
    p.setdefault("crypto_internal_align_enabled", True)
    p.setdefault("gate_controller_enabled", True)
    p.setdefault("b_shadow_promote_enabled", True)
    # Context quality: evidence-layer execution sizing
    p.setdefault("context_quality_enabled", True)
    p.setdefault("context_quality_block_shadow_only", True)
    p.setdefault("context_quality_opening_noise_multiplier", 0.0)
    p.setdefault("context_quality_opening_drive_multiplier", 1.0)
    p.setdefault("context_quality_morning_trend_multiplier", 1.0)
    p.setdefault("context_quality_midday_multiplier", 0.35)
    p.setdefault("context_quality_afternoon_momentum_multiplier", 1.0)
    p.setdefault("context_quality_pre_close_multiplier", 0.55)
    p.setdefault("context_quality_after_close_multiplier", 0.0)
    p.setdefault("context_quality_outside_hours_multiplier", 0.0)
    p.setdefault("context_quality_unknown_multiplier", 0.50)
    p.setdefault("advisory_chase_block_enabled", True)
    if state.IS_PAPER_TRADING:
        for key, value in p.get("paper_overrides", {}).items():
            p[key] = value
    if os.getenv("MIN_GRADE_REQUIRED"):
        p["min_grade_required"] = os.getenv("MIN_GRADE_REQUIRED", "").strip().upper()
    if os.getenv("ALLOW_B_GRADE_EXPLORATION") is not None:
        p["allow_b_grade_exploration"] = _env_bool("ALLOW_B_GRADE_EXPLORATION", False)
    if os.getenv("B_GRADE_SIZE_MULTIPLIER"):
        p["b_grade_size_multiplier"] = _env_float("B_GRADE_SIZE_MULTIPLIER", p["b_grade_size_multiplier"])
    if os.getenv("GRADE_EV_OVERRIDE_NEGATIVE_MIN_SAMPLES"):
        p["grade_ev_override_negative_min_samples"] = _env_int(
            "GRADE_EV_OVERRIDE_NEGATIVE_MIN_SAMPLES",
            int(p["grade_ev_override_negative_min_samples"]),
        )
    if os.getenv("PROBE_FLOOR_INFLATION_MAX_MULTIPLE"):
        p["probe_floor_inflation_max_multiple"] = _env_float(
            "PROBE_FLOOR_INFLATION_MAX_MULTIPLE",
            p["probe_floor_inflation_max_multiple"],
        )
    if os.getenv("HIGH_ATR_STOP_THRESHOLD_PCT"):
        p["high_atr_stop_threshold_pct"] = _env_float("HIGH_ATR_STOP_THRESHOLD_PCT", p["high_atr_stop_threshold_pct"])
    if os.getenv("HIGH_ATR_STOP_MULTIPLIER"):
        p["high_atr_stop_multiplier"] = _env_float("HIGH_ATR_STOP_MULTIPLIER", p["high_atr_stop_multiplier"])
    if os.getenv("PDT_PROTECTION_ENABLED") is not None:
        p["pdt_protection_enabled"] = _env_bool("PDT_PROTECTION_ENABLED", True)
    if os.getenv("PDT_MAX_DAY_TRADES_5D"):
        p["pdt_max_day_trades_5d"] = _env_int(
            "PDT_MAX_DAY_TRADES_5D",
            int(p["pdt_max_day_trades_5d"]),
        )
    if os.getenv("PDT_MIN_EQUITY_USD"):
        p["pdt_min_equity_usd"] = _env_float("PDT_MIN_EQUITY_USD", p["pdt_min_equity_usd"])
    if os.getenv("RANGING_MAX_TRADES_PER_DAY"):
        p["ranging_max_trades_per_day"] = _env_int("RANGING_MAX_TRADES_PER_DAY", int(p["ranging_max_trades_per_day"]))
    if os.getenv("RANGING_ATR_STOP_MULTIPLE"):
        p["ranging_atr_stop_multiple"] = _env_float("RANGING_ATR_STOP_MULTIPLE", p["ranging_atr_stop_multiple"])
    if os.getenv("RANGING_REGIME_SIZE_MULTIPLIER"):
        p["ranging_regime_size_multiplier"] = _env_float("RANGING_REGIME_SIZE_MULTIPLIER", p["ranging_regime_size_multiplier"])
    if os.getenv("RANGING_A_PLUS_MIN_COMPOSITE"):
        p["ranging_a_plus_min_composite"] = _env_float("RANGING_A_PLUS_MIN_COMPOSITE", p["ranging_a_plus_min_composite"])
    if os.getenv("RANGING_A_PLUS_MIN_BREAKOUT_QUALITY"):
        p["ranging_a_plus_min_breakout_quality"] = _env_float(
            "RANGING_A_PLUS_MIN_BREAKOUT_QUALITY",
            p["ranging_a_plus_min_breakout_quality"],
        )
    if os.getenv("RANGING_A_PLUS_MIN_EV_PCT"):
        p["ranging_a_plus_min_ev_pct"] = _env_float("RANGING_A_PLUS_MIN_EV_PCT", p["ranging_a_plus_min_ev_pct"])
    if os.getenv("RANGING_A_GRADE_MIN_BREAKOUT_QUALITY"):
        p["ranging_a_grade_min_breakout_quality"] = _env_float(
            "RANGING_A_GRADE_MIN_BREAKOUT_QUALITY",
            p["ranging_a_grade_min_breakout_quality"],
        )
    if os.getenv("RANGING_A_GRADE_MIN_EV_PCT"):
        p["ranging_a_grade_min_ev_pct"] = _env_float("RANGING_A_GRADE_MIN_EV_PCT", p["ranging_a_grade_min_ev_pct"])
    if os.getenv("RANGING_PROBE_ENABLED"):
        p["ranging_probe_enabled"] = _env_bool("RANGING_PROBE_ENABLED", bool(p["ranging_probe_enabled"]))
    if os.getenv("RANGING_PROBE_ALLOWED_GRADES"):
        p["ranging_probe_allowed_grades"] = _env_value(
            "RANGING_PROBE_ALLOWED_GRADES",
            str(p["ranging_probe_allowed_grades"]),
        )
    if os.getenv("RANGING_PROBE_SHADOW_GRADES"):
        p["ranging_probe_shadow_grades"] = _env_value(
            "RANGING_PROBE_SHADOW_GRADES",
            str(p["ranging_probe_shadow_grades"]),
        )
    if os.getenv("RANGING_PROBE_SIZE_MULTIPLIER"):
        p["ranging_probe_size_multiplier"] = _env_float(
            "RANGING_PROBE_SIZE_MULTIPLIER",
            p["ranging_probe_size_multiplier"],
        )
    if os.getenv("RANGING_PROBE_GRADE_B_SHADOW_SIZE_MULTIPLIER"):
        p["ranging_probe_grade_b_shadow_size_multiplier"] = _env_float(
            "RANGING_PROBE_GRADE_B_SHADOW_SIZE_MULTIPLIER",
            p["ranging_probe_grade_b_shadow_size_multiplier"],
        )
    if os.getenv("RANGING_PROBE_GRADE_B_MIN_COMPOSITE"):
        p["ranging_probe_grade_b_min_composite"] = _env_float(
            "RANGING_PROBE_GRADE_B_MIN_COMPOSITE",
            p["ranging_probe_grade_b_min_composite"],
        )
    if os.getenv("RANGING_PROBE_GRADE_B_MIN_BREAKOUT_QUALITY"):
        p["ranging_probe_grade_b_min_breakout_quality"] = _env_float(
            "RANGING_PROBE_GRADE_B_MIN_BREAKOUT_QUALITY",
            p["ranging_probe_grade_b_min_breakout_quality"],
        )
    if os.getenv("RANGING_PROBE_GRADE_B_PROMOTE_MIN_SAMPLES"):
        p["ranging_probe_grade_b_promote_min_samples"] = _env_int(
            "RANGING_PROBE_GRADE_B_PROMOTE_MIN_SAMPLES",
            int(p["ranging_probe_grade_b_promote_min_samples"]),
        )
    if os.getenv("RANGING_PROBE_GRADE_B_PROMOTE_MIN_WIN_RATE"):
        p["ranging_probe_grade_b_promote_min_win_rate"] = _env_float(
            "RANGING_PROBE_GRADE_B_PROMOTE_MIN_WIN_RATE",
            p["ranging_probe_grade_b_promote_min_win_rate"],
        )
    if os.getenv("RANGING_PROBE_GRADE_B_PROMOTE_REQUIRES_MFE_GT_MAE"):
        p["ranging_probe_grade_b_promote_requires_mfe_gt_mae"] = _env_bool(
            "RANGING_PROBE_GRADE_B_PROMOTE_REQUIRES_MFE_GT_MAE",
            bool(p["ranging_probe_grade_b_promote_requires_mfe_gt_mae"]),
        )
    if os.getenv("RANGING_PROBE_GRADE_B_PROMOTE_SIZE_MULTIPLIER"):
        p["ranging_probe_grade_b_promote_size_multiplier"] = _env_float(
            "RANGING_PROBE_GRADE_B_PROMOTE_SIZE_MULTIPLIER",
            p["ranging_probe_grade_b_promote_size_multiplier"],
        )
    if os.getenv("RANGING_PROBE_MIN_EV_PCT"):
        p["ranging_probe_min_ev_pct"] = _env_float("RANGING_PROBE_MIN_EV_PCT", p["ranging_probe_min_ev_pct"])
    if os.getenv("RANGING_PROBE_MIN_COMPOSITE"):
        p["ranging_probe_min_composite"] = _env_float(
            "RANGING_PROBE_MIN_COMPOSITE",
            p["ranging_probe_min_composite"],
        )
    if os.getenv("RANGING_PROBE_MIN_BREAKOUT_QUALITY"):
        p["ranging_probe_min_breakout_quality"] = _env_float(
            "RANGING_PROBE_MIN_BREAKOUT_QUALITY",
            p["ranging_probe_min_breakout_quality"],
        )
    if os.getenv("RANGING_PROBE_MIN_ALIGNED_SIGNALS"):
        p["ranging_probe_min_aligned_signals"] = _env_int(
            "RANGING_PROBE_MIN_ALIGNED_SIGNALS",
            int(p["ranging_probe_min_aligned_signals"]),
        )
    if os.getenv("RANGING_PROBE_MIN_MACD"):
        p["ranging_probe_min_macd"] = _env_float("RANGING_PROBE_MIN_MACD", p["ranging_probe_min_macd"])
    if os.getenv("RANGING_PROBE_MIN_TAPE"):
        p["ranging_probe_min_tape"] = _env_float("RANGING_PROBE_MIN_TAPE", p["ranging_probe_min_tape"])
    if os.getenv("RANGING_PROBE_MIN_RELATIVE_STRENGTH"):
        p["ranging_probe_min_relative_strength"] = _env_float(
            "RANGING_PROBE_MIN_RELATIVE_STRENGTH",
            p["ranging_probe_min_relative_strength"],
        )
    if os.getenv("RANGING_PROBE_MAX_TAPE_AGAINST"):
        p["ranging_probe_max_tape_against"] = _env_float(
            "RANGING_PROBE_MAX_TAPE_AGAINST",
            p["ranging_probe_max_tape_against"],
        )
    if os.getenv("RANGING_PROBE_MIN_SECTOR_RELATIVE_PCT"):
        p["ranging_probe_min_sector_relative_pct"] = _env_float(
            "RANGING_PROBE_MIN_SECTOR_RELATIVE_PCT",
            p["ranging_probe_min_sector_relative_pct"],
        )
    if os.getenv("RANGING_PROBE_BLOCKED_THEMES"):
        p["ranging_probe_blocked_themes"] = _env_value(
            "RANGING_PROBE_BLOCKED_THEMES",
            str(p["ranging_probe_blocked_themes"]),
        )
    if os.getenv("RANGING_MAX_NOTIONAL_EUR"):
        p["ranging_max_notional_eur"] = _env_float("RANGING_MAX_NOTIONAL_EUR", p["ranging_max_notional_eur"])
    if os.getenv("THESIS_INVALIDATED_COOLDOWN_MINUTES"):
        p["thesis_invalidated_cooldown_minutes"] = _env_int(
            "THESIS_INVALIDATED_COOLDOWN_MINUTES",
            int(p["thesis_invalidated_cooldown_minutes"]),
        )
    if os.getenv("RANGING_STOP_LOSS_COOLDOWN_MINUTES"):
        p["ranging_stop_loss_cooldown_minutes"] = _env_int(
            "RANGING_STOP_LOSS_COOLDOWN_MINUTES",
            int(p["ranging_stop_loss_cooldown_minutes"]),
        )
    if os.getenv("MIN_REWARD_RISK_RATIO"):
        p["min_reward_risk_ratio"] = _env_float("MIN_REWARD_RISK_RATIO", p["min_reward_risk_ratio"])
    if os.getenv("RANGING_MIN_REWARD_RISK_RATIO"):
        p["ranging_min_reward_risk_ratio"] = _env_float(
            "RANGING_MIN_REWARD_RISK_RATIO",
            p["ranging_min_reward_risk_ratio"],
        )
    if os.getenv("SIGNAL_CONSENSUS_MIN_COUNT"):
        p["signal_consensus_min_count"] = _env_int(
            "SIGNAL_CONSENSUS_MIN_COUNT",
            int(p["signal_consensus_min_count"]),
        )
    if os.getenv("RANGING_SIGNAL_CONSENSUS_MIN_COUNT"):
        p["ranging_signal_consensus_min_count"] = _env_int(
            "RANGING_SIGNAL_CONSENSUS_MIN_COUNT",
            int(p["ranging_signal_consensus_min_count"]),
        )
    if os.getenv("RANGING_CORE_CONSENSUS_ENABLED"):
        p["ranging_core_consensus_enabled"] = _env_bool(
            "RANGING_CORE_CONSENSUS_ENABLED",
            bool(p["ranging_core_consensus_enabled"]),
        )
    if os.getenv("RANGING_CORE_CONSENSUS_MIN_COUNT"):
        p["ranging_core_consensus_min_count"] = _env_int(
            "RANGING_CORE_CONSENSUS_MIN_COUNT",
            int(p["ranging_core_consensus_min_count"]),
        )
    if os.getenv("SIGNAL_CONSENSUS_MIN_STRENGTH"):
        p["signal_consensus_min_strength"] = _env_float(
            "SIGNAL_CONSENSUS_MIN_STRENGTH",
            p["signal_consensus_min_strength"],
        )
    if os.getenv("MAX_OPEN_POSITIONS_PER_THEME"):
        p["max_open_positions_per_theme"] = _env_int(
            "MAX_OPEN_POSITIONS_PER_THEME",
            int(p["max_open_positions_per_theme"]),
        )
    if os.getenv("SECTOR_MOMENTUM_BONUS_ENABLED") is not None:
        p["sector_momentum_bonus_enabled"] = _env_bool("SECTOR_MOMENTUM_BONUS_ENABLED", True)
    if os.getenv("SECTOR_MOMENTUM_LEADERSHIP_THRESHOLD_PCT"):
        p["sector_momentum_leadership_threshold_pct"] = _env_float(
            "SECTOR_MOMENTUM_LEADERSHIP_THRESHOLD_PCT",
            p["sector_momentum_leadership_threshold_pct"],
        )
    if os.getenv("SECTOR_MOMENTUM_MAX_BONUS"):
        p["sector_momentum_max_bonus"] = _env_float("SECTOR_MOMENTUM_MAX_BONUS", p["sector_momentum_max_bonus"])
    if os.getenv("THEME_MAX_CANDIDATES_PER_CYCLE"):
        p["theme_max_candidates_per_cycle"] = _env_int(
            "THEME_MAX_CANDIDATES_PER_CYCLE",
            int(p["theme_max_candidates_per_cycle"]),
        )
    if os.getenv("THEME_MAX_LEVERAGED_CANDIDATES_PER_CYCLE"):
        p["theme_max_leveraged_candidates_per_cycle"] = _env_int(
            "THEME_MAX_LEVERAGED_CANDIDATES_PER_CYCLE",
            int(p["theme_max_leveraged_candidates_per_cycle"]),
        )
    if os.getenv("DYNAMIC_UNIVERSE_SHADOW_ENABLED") is not None:
        p["dynamic_universe_shadow_enabled"] = _env_bool("DYNAMIC_UNIVERSE_SHADOW_ENABLED", True)
    short_override = os.getenv("ALLOW_SHORT_SELLING")
    if short_override is not None and short_override.strip():
        p["allow_short_selling"] = short_override.strip().lower() == "true"
    # Phase 1: runner trail + breakeven promotion env overrides
    if os.getenv("RUNNER_ACTIVE_TRAIL_ENABLED") is not None:
        p["runner_active_trail_enabled"] = _env_bool("RUNNER_ACTIVE_TRAIL_ENABLED", True)
    if os.getenv("BREAKEVEN_PROMOTION_ENABLED") is not None:
        p["breakeven_promotion_enabled"] = _env_bool("BREAKEVEN_PROMOTION_ENABLED", True)
    if os.getenv("BREAKEVEN_ATR_MULT"):
        p["breakeven_atr_mult"] = _env_float("BREAKEVEN_ATR_MULT", 0.6)
    # Phase 2: hold score env overrides
    if os.getenv("HOLD_SCORE_ENABLED") is not None:
        p["hold_score_enabled"] = _env_bool("HOLD_SCORE_ENABLED", True)
    if os.getenv("HOLD_SCORE_EXTEND_ENABLED") is not None:
        p["hold_score_extend_enabled"] = _env_bool("HOLD_SCORE_EXTEND_ENABLED", True)
    if os.getenv("HOLD_SCORE_TRIM_ENABLED") is not None:
        p["hold_score_trim_enabled"] = _env_bool("HOLD_SCORE_TRIM_ENABLED", False)
    if os.getenv("HOLD_SCORE_EXIT_ENABLED") is not None:
        p["hold_score_exit_enabled"] = _env_bool("HOLD_SCORE_EXIT_ENABLED", False)
    if os.getenv("HOLD_SCORE_EXTEND_MINUTES"):
        p["hold_score_extend_minutes"] = _env_int("HOLD_SCORE_EXTEND_MINUTES", 30)
    if os.getenv("HOLD_SCORE_TRIM_PCT"):
        p["hold_score_trim_pct"] = _env_float("HOLD_SCORE_TRIM_PCT", 0.33)
    if os.getenv("LATE_CHASE_BLOCK_ENABLED") is not None:
        p["late_chase_block_enabled"] = _env_bool("LATE_CHASE_BLOCK_ENABLED", True)
    if os.getenv("LATE_CHASE_ATR_MULT"):
        p["late_chase_atr_mult"] = _env_float("LATE_CHASE_ATR_MULT", 1.5)
    if os.getenv("RVOL_GATE_ENABLED") is not None:
        p["rvol_gate_enabled"] = _env_bool("RVOL_GATE_ENABLED", True)
    if os.getenv("RVOL_MIN_MULTIPLIER"):
        p["rvol_min_multiplier"] = _env_float("RVOL_MIN_MULTIPLIER", 1.3)
    if os.getenv("VWAP_1M_CONFIRM_ENABLED") is not None:
        p["vwap_1m_confirm_enabled"] = _env_bool("VWAP_1M_CONFIRM_ENABLED", True)
    if os.getenv("CRYPTO_INTERNAL_ALIGN_ENABLED") is not None:
        p["crypto_internal_align_enabled"] = _env_bool("CRYPTO_INTERNAL_ALIGN_ENABLED", True)
    if os.getenv("GATE_CONTROLLER_ENABLED") is not None:
        p["gate_controller_enabled"] = _env_bool("GATE_CONTROLLER_ENABLED", True)
    if os.getenv("B_SHADOW_PROMOTE_ENABLED") is not None:
        p["b_shadow_promote_enabled"] = _env_bool("B_SHADOW_PROMOTE_ENABLED", True)
    if os.getenv("CONTEXT_QUALITY_ENABLED") is not None:
        p["context_quality_enabled"] = _env_bool("CONTEXT_QUALITY_ENABLED", True)
    if os.getenv("CONTEXT_QUALITY_BLOCK_SHADOW_ONLY") is not None:
        p["context_quality_block_shadow_only"] = _env_bool("CONTEXT_QUALITY_BLOCK_SHADOW_ONLY", True)
    if os.getenv("CONTEXT_QUALITY_OPENING_NOISE_MULTIPLIER"):
        p["context_quality_opening_noise_multiplier"] = _env_float(
            "CONTEXT_QUALITY_OPENING_NOISE_MULTIPLIER",
            p["context_quality_opening_noise_multiplier"],
        )
    if os.getenv("CONTEXT_QUALITY_OPENING_DRIVE_MULTIPLIER"):
        p["context_quality_opening_drive_multiplier"] = _env_float(
            "CONTEXT_QUALITY_OPENING_DRIVE_MULTIPLIER",
            p["context_quality_opening_drive_multiplier"],
        )
    if os.getenv("CONTEXT_QUALITY_MORNING_TREND_MULTIPLIER"):
        p["context_quality_morning_trend_multiplier"] = _env_float(
            "CONTEXT_QUALITY_MORNING_TREND_MULTIPLIER",
            p["context_quality_morning_trend_multiplier"],
        )
    if os.getenv("CONTEXT_QUALITY_MIDDAY_MULTIPLIER"):
        p["context_quality_midday_multiplier"] = _env_float(
            "CONTEXT_QUALITY_MIDDAY_MULTIPLIER",
            p["context_quality_midday_multiplier"],
        )
    if os.getenv("CONTEXT_QUALITY_AFTERNOON_MOMENTUM_MULTIPLIER"):
        p["context_quality_afternoon_momentum_multiplier"] = _env_float(
            "CONTEXT_QUALITY_AFTERNOON_MOMENTUM_MULTIPLIER",
            p["context_quality_afternoon_momentum_multiplier"],
        )
    if os.getenv("CONTEXT_QUALITY_PRE_CLOSE_MULTIPLIER"):
        p["context_quality_pre_close_multiplier"] = _env_float(
            "CONTEXT_QUALITY_PRE_CLOSE_MULTIPLIER",
            p["context_quality_pre_close_multiplier"],
        )
    if os.getenv("CONTEXT_QUALITY_AFTER_CLOSE_MULTIPLIER"):
        p["context_quality_after_close_multiplier"] = _env_float(
            "CONTEXT_QUALITY_AFTER_CLOSE_MULTIPLIER",
            p["context_quality_after_close_multiplier"],
        )
    if os.getenv("CONTEXT_QUALITY_OUTSIDE_HOURS_MULTIPLIER"):
        p["context_quality_outside_hours_multiplier"] = _env_float(
            "CONTEXT_QUALITY_OUTSIDE_HOURS_MULTIPLIER",
            p["context_quality_outside_hours_multiplier"],
        )
    if os.getenv("CONTEXT_QUALITY_UNKNOWN_MULTIPLIER"):
        p["context_quality_unknown_multiplier"] = _env_float(
            "CONTEXT_QUALITY_UNKNOWN_MULTIPLIER",
            p["context_quality_unknown_multiplier"],
        )
    if os.getenv("ADVISORY_CHASE_BLOCK_ENABLED") is not None:
        p["advisory_chase_block_enabled"] = _env_bool("ADVISORY_CHASE_BLOCK_ENABLED", True)
    return p
