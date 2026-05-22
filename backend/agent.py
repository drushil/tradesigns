"""
backend/agent.py
Main agent loop. Runs on a schedule, ties together:
signals → risk gate → EV check → LLM decision → execution → learning → logging
"""
from __future__ import annotations
import os
import time
import asyncio
import math
import re
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv

try:
    import tomllib
except Exception:  # pragma: no cover - Python < 3.11 fallback
    try:
        import tomli as tomllib
    except Exception:
        tomllib = None

try:
    from zoneinfo import ZoneInfo
    NY_TZ = ZoneInfo("America/New_York")
except Exception:
    try:
        import pytz
        NY_TZ = pytz.timezone("America/New_York")
    except Exception:
        NY_TZ = None

load_dotenv()

from config.risk_profiles        import get_profile
from backend.signals.engine      import (compute_all_signals, compute_swing_score,
                                          detect_regime, detect_macro_regime,
                                          compute_atr, latest_macro_headlines,
                                          scan_for_macro_shock,
                                          detect_momentum_swing,
                                          prefetch_newsapi_batch)
from backend.broker.alpaca       import (get_account, get_positions, submit_market_order,
                                          close_position, close_partial_position,
                                          pre_trade_gate, compute_position_size,
                                          scan_for_extreme_dips, get_order_by_id,
                                          cancel_order_by_id, submit_stop_order,
                                          cancel_open_orders_for_symbol)
from backend.learning.engine     import (RegimeAwareWeightEngine, attribute_signals,
                                          compute_expected_value, get_effective_profile,
                                          generate_weekly_insights, llm_signal_decision,
                                          build_weight_engine_from_trades,
                                          compute_hold_score)
from database.client             import (insert_trade, insert_signal, get_recent_trades,
                                          save_signal_weights, get_latest_weights,
                                          save_snapshot, save_learning, log_event, get_logs,
                                          save_open_trade, get_open_trade_records,
                                          close_open_trade_record,
                                          insert_blocked_opportunity,
                                          get_unchecked_blocked_opportunities,
                                          update_blocked_opportunity_replay,
                                          get_unchecked_closed_trades_for_replay,
                                          update_trade_post_exit_replay,
                                          update_signal,
                                          get_signal_percentiles,
                                          upsert_signal_percentiles)
from backend.grading.engine      import (grade_setup, compute_sector_confirmation,
                                          get_ticker_percentile_rank,
                                          merge_percentile_window,
                                          compute_percentile_thresholds,
                                          grade_sort_key, effective_size_multiplier,
                                          a_plus_hard_blocks, SetupGrade)
from backend.sweep.agent         import (compute_sweep_plan, execute_sweep,
                                          recall_sweep, has_active_sweep)
from backend.dividends.scanner   import (scan_dividend_calendar, log_dividend_opportunity)
from backend.runtime.env         import (_env_value, _env_int, _env_float, _env_bool,
                                          _eurusd_rate, _eur_to_usd)
from backend.market.timing       import (NY_TZ, _nth_weekday, _to_new_york_time,
                                          is_regular_us_market_hours,
                                          _should_run_swing_recheck,
                                          _is_eod_intraday_cleanup_window,
                                          _minutes_to_regular_close,
                                          _minutes_since_regular_open,
                                          _is_eod_final_force_exit_window,
                                          _is_new_intraday_entry_too_late,
                                          _leveraged_etf_max_hold_window,
                                          _allows_intraday, _allows_swing)
from backend.execution.common    import (_trading_capital, _deterministic_action,
                                          _make_order_ref, _cap_short_notional,
                                          _regime_debug_payload, _strategy_family,
                                          _signal_score, _is_probe_ev_decision,
                                          _directional_score, _parse_dt, _trade_pnl_pct)
from backend.analytics.replay    import (_parse_supabase_time, _replay_price_window,
                                          _replay_one_blocked_opportunity,
                                          _replay_blocked_opportunities,
                                          _closed_trade_replay_exit_reasons,
                                          _replay_one_closed_trade_exit,
                                          _replay_closed_trade_exits)
from backend.execution.orders    import (_current_daily_price, _stop_pct_from_atr,
                                          _submit_horizon_order)
from backend.execution.exit      import (_open_position_tickers,
                                          _apply_learned_hold_extension,
                                          _log_short_candidate,
                                          _time_exit_cooldown_active,
                                          _ticker_loss_cooldown_active,
                                          _thesis_invalidated_cooldown_active,
                                          _ranging_stop_loss_cooldown_active,
                                          _rehydrated_open_trade,
                                          _hydrate_open_trades,
                                          _record_day_trade, _count_day_trades_5d,
                                          _check_pdt_warning,
                                          _check_thesis_invalidation,
                                          _trim_position, _check_hold_score,
                                          _check_breakeven_promotion,
                                          _update_intraday_runner_stop,
                                          _check_partial_exit,
                                          _check_exits, _handle_hold_deadline,
                                          _check_momentum_exit,
                                          _recover_bracket_fill,
                                          _recover_protective_stop_fill,
                                          _cancel_protective_stop_order,
                                          _replace_protective_stop_order,
                                          _cancel_bracket_orders_for_manual_exit,
                                          _close_trade)
from backend.execution.gates     import (_alignment_veto, _signal_consensus_block,
                                          _reward_risk_block, _theme_open_exposure_block,
                                          _csv_upper_set, _ranging_probe_decision,
                                          _ranging_regime_block,
                                          _llm_rationale_mentions_conflict,
                                          _known_negative_grade_override_block,
                                          _probe_floor_inflation_block,
                                          _late_chase_block, _rvol_block,
                                          _vwap_1m_confirmation_downgrade,
                                          _event_risk_active,
                                          _overnight_event_risk_active,
                                          _breakout_quality, _time_of_day_rank_bonus,
                                          _candidate_rank_score, _trade_setup_context,
                                          _record_blocked_opportunity,
                                          _threshold_block_detail)
from backend.market.sector       import (_normalize_ticker_list, _sector_data,
                                          _sector_setting, _sector_default_tickers,
                                          _sector_members, _default_ticker_universe,
                                          _SECTOR_UNIVERSE, _ACTIVE_SECTORS,
                                          _SECTOR_CONFIG_WARNINGS,
                                          _THEME_MAP, _THEME_PROXIES,
                                          _THEME_PROXY_BASKETS, _DYNAMIC_CANDIDATE_POOL,
                                          _DEFAULT_CORE_TICKERS, _CONFIG_LEVERAGED_TICKERS,
                                          _INVERSE_ETFS, _DEFENSIVE_TICKERS,
                                          _INDEX_OR_ETF_TICKERS, _PROBE_EV_DECISIONS,
                                          _ticker_theme, _is_leveraged_etf,
                                          _leveraged_etf_stop_scalar,
                                          _exposure_direction,
                                          _return_pct_from_bars, _return_pcts_from_bars,
                                          _extract_close_series,
                                          _sector_proxy_symbols, _sector_proxy_return,
                                          _sector_momentum_snapshot,
                                          _apply_sector_momentum_to_candidate,
                                          _dynamic_universe_shadow_recommendations,
                                          _shadow_candidate_repeat_counts,
                                          _enrich_shadow_recommendation_repeats,
                                          _log_dynamic_universe_shadow,
                                          _theme_cap_candidates)


def _get_cached_signals(ticker: str, weights: dict, regime_state) -> dict:
    """Return signals from cache if fresh, otherwise fetch and cache."""
    now = datetime.now(timezone.utc)
    cached = _signal_cache.get(ticker)
    if cached is not None:
        age = (now - cached[0]).total_seconds()
        if age < _SIGNAL_CACHE_TTL_SECONDS:
            return cached[1]
    # Evict all expired entries while we're here (keeps dict bounded to TICKERS set)
    expired = [k for k, v in _signal_cache.items()
               if (now - v[0]).total_seconds() >= _SIGNAL_CACHE_TTL_SECONDS]
    for k in expired:
        _signal_cache.pop(k, None)
    result = compute_all_signals(ticker, weights, regime_state=regime_state)
    _signal_cache[ticker] = (now, result)
    return result


TICKERS  = [t.strip().upper() for t in _env_value("TICKER_UNIVERSE", _default_ticker_universe()).split(",") if t.strip()]
SWING_TICKERS = [t.strip().upper() for t in _env_value("SWING_TICKERS", "").split(",") if t.strip()]
PROFILE  = get_profile(_env_value("RISK_PROFILE", "moderate"))
HORIZON  = _env_value("INVESTMENT_HORIZON", "short")
LLM_HOUR_LIMIT = _env_int("LLM_CALLS_PER_HOUR_LIMIT", 20)
IS_PAPER_TRADING = _env_value("ALPACA_PAPER", "true").lower() != "false"

# Global learning engine (persists in memory between cycles)
_learning_engine: Optional[RegimeAwareWeightEngine] = None
_llm_calls_this_hour = 0
_llm_hour_reset      = datetime.utcnow()
_open_trades         = {}   # {ticker: {entry_price, entry_time, stop_price, hold_minutes, ...}}
_swing_trades        = {}   # {ticker: {entry_price, entry_time, hold_days, ...}}
_last_shock_refresh  = None
_day_trade_log: list = []   # [(date, ticker)] same-day round trips for PDT tracking
_last_shock_result   = {
    "shock_detected": False,
    "classification": "NORMAL",
    "affected_sectors": [],
    "direction": "mixed",
    "reason": "not_scanned",
}
# Per-cycle signal cache — keyed by ticker, expires after 8 min (one cycle apart at 10-min cadence)
_signal_cache: dict[str, tuple[datetime, dict]] = {}
_SIGNAL_CACHE_TTL_SECONDS = 480

# Composites collected across all tickers this cycle — used for sector confirmation
_cycle_composites: dict[str, float] = {}

# Percentile baseline from DB — loaded once per cycle
_cycle_db_percentiles: dict[str, dict] = {}
_logged_sector_config_warnings = False

# ── Sync runtime.state with agent.py's canonical containers ──────────────
# Imported as a module (not destructured) so that sub-modules can do:
#   import backend.runtime.state as state
#   state._open_trades[ticker] = {...}
# and see the same live dict that agent.py mutates via local names.
# For mutable containers we point state's attribute at agent's dict object
# so both names always refer to the same underlying object.
# For scalars, future sub-modules should call `state.X` directly (and
# agent.py must do `state.X = new_val` when re-binding them).
import backend.runtime.state as _rt_state  # noqa: E402
_rt_state.TICKERS           = TICKERS
_rt_state.SWING_TICKERS     = SWING_TICKERS
_rt_state.PROFILE           = PROFILE
_rt_state.HORIZON           = HORIZON
_rt_state.LLM_HOUR_LIMIT    = LLM_HOUR_LIMIT
_rt_state.IS_PAPER_TRADING  = IS_PAPER_TRADING
# Container aliasing — same dict/list objects, mutations propagate both ways
_rt_state._open_trades          = _open_trades
_rt_state._swing_trades         = _swing_trades
_rt_state._signal_cache         = _signal_cache
_rt_state._cycle_composites     = _cycle_composites
_rt_state._cycle_db_percentiles = _cycle_db_percentiles
_rt_state._day_trade_log        = _day_trade_log
_rt_state._last_shock_result    = _last_shock_result


def _run_signal_cycle_if_market_open():
    if is_regular_us_market_hours():
        run_signal_cycle()


# _open_position_tickers → moved to backend/execution/exit.py

def _init_learning_engine() -> RegimeAwareWeightEngine:
    """Load latest weights from DB or use profile priors."""
    saved = get_latest_weights("global")
    trades = get_recent_trades(days=_env_int("LEARNING_LOOKBACK_DAYS", 120))
    replayable = [t for t in trades if t.get("signals_json") and t.get("net_pnl_pct") is not None]
    if replayable:
        return build_weight_engine_from_trades(PROFILE["signal_weights"], replayable)
    priors = saved if saved else PROFILE["signal_weights"]
    return RegimeAwareWeightEngine(priors)


def _get_portfolio_state() -> dict:
    account   = get_account()
    if "error" in account:
        return {"broker_error": account["error"], "equity": 0, "cash": 0, "positions": []}
    positions = get_positions()
    equity    = account.get("portfolio_value", 100.0)
    cash      = account.get("cash", 100.0)
    fx_rate   = float(os.getenv("EURUSD_RATE", "1.08") or "1.08")
    unrealized_pl_usd = sum(float(p.get("unrealized_pl") or 0) for p in positions)
    net_market_value_usd = sum(float(p.get("market_value") or 0) for p in positions)
    gross_market_value_usd = sum(abs(float(p.get("market_value") or 0)) for p in positions)

    # VIX
    try:
        import yfinance as yf
        vix_df = yf.download("^VIX", period="1d", interval="1h",
                             progress=False, auto_adjust=True)
        vix = float(vix_df["Close"].iloc[-1].item()) if not vix_df.empty else 20.0
    except Exception:
        vix = 20.0

    # Drawdown always measured against STARTING_CAPITAL_EUR converted to USD.
    # This ensures the circuit breaker fires at the correct EUR loss amount
    # regardless of the Alpaca paper account's $100k default.
    start_eur = float(os.getenv("STARTING_CAPITAL_EUR", "3000"))
    start_usd = start_eur * fx_rate
    drawdown  = max(0.0, (start_usd - equity) / start_usd * 100)

    return {
        "equity":       round(equity, 2),
        "cash":         round(cash, 2),
        "equity_eur":   round(equity / fx_rate, 2),
        "cash_eur":     round(cash / fx_rate, 2),
        "fx_rate":      fx_rate,
        "broker_equity_usd": account.get("alpaca_actual_usd"),
        "broker_cash_usd": account.get("alpaca_cash_usd"),
        "capital_ceiling_eur": account.get("capital_ceiling_eur"),
        "capital_ceiling_usd": account.get("capital_ceiling_usd"),
        "unrealized_pnl_usd": round(unrealized_pl_usd, 2),
        "unrealized_pnl_eur": round(unrealized_pl_usd / fx_rate, 2),
        "net_market_value_usd": round(net_market_value_usd, 2),
        "gross_market_value_usd": round(gross_market_value_usd, 2),
        "cash_pct":     round(cash / equity * 100, 1) if equity > 0 else 100.0,
        "positions":    positions,
        "vix":          round(vix, 1),
        "drawdown_today": round(drawdown, 3),
        "trades_today": _count_trades_today(),
        "consecutive_losses": _count_consecutive("loss"),
        "consecutive_wins":   _count_consecutive("win"),
    }


def _count_trades_today() -> int:
    trades = get_recent_trades(days=1)
    today  = datetime.utcnow().date()
    closed_count = sum(1 for t in trades
                       if t.get("created_at", "")[:10] == str(today))
    try:
        trade_logs = get_logs(level="TRADE", limit=200)
        submitted_count = sum(
            1 for l in trade_logs
            if l.get("event") == "order_submitted"
            and (l.get("logged_at") or "")[:10] == str(today)
        )
        return max(closed_count, submitted_count)
    except Exception:
        return closed_count


def _count_consecutive(outcome: str) -> int:
    trades = get_recent_trades(days=7)
    count  = 0
    for t in trades:
        pnl = t.get("net_pnl_pct", 0) or 0
        is_win = pnl > 0
        if outcome == "win" and is_win:
            count += 1
        elif outcome == "loss" and not is_win:
            count += 1
        else:
            break
    return count


def _can_call_llm() -> bool:
    global _llm_calls_this_hour, _llm_hour_reset
    now = datetime.utcnow()
    if (now - _llm_hour_reset).seconds >= 3600:
        _llm_calls_this_hour = 0
        _llm_hour_reset      = now
    return _llm_calls_this_hour < LLM_HOUR_LIMIT


def _record_llm_call():
    global _llm_calls_this_hour
    _llm_calls_this_hour += 1


def _send_discord_alert(text: str) -> bool:
    webhook = os.getenv("DISCORD_WEBHOOK_URL", "")
    if not webhook:
        return False
    try:
        import requests
        resp = requests.post(webhook, json={"content": text}, timeout=10)
        return resp.ok
    except Exception:
        return False


def _refresh_macro_shock_if_needed() -> dict:
    global _last_shock_refresh, _last_shock_result
    now = datetime.utcnow()
    if _last_shock_refresh and (now - _last_shock_refresh).total_seconds() < 15 * 60:
        return _last_shock_result

    headlines = latest_macro_headlines(limit_per_ticker=5)
    shock_result = scan_for_macro_shock(headlines)
    _last_shock_refresh = now
    _last_shock_result = shock_result

    if shock_result.get("shock_detected"):
        log_event("SIGNAL", "macro_shock_detected", shock_result)
        _send_discord_alert(
            "Macro shock detected\n"
            f"Classification: {shock_result.get('classification')}\n"
            f"Direction: {shock_result.get('direction')}\n"
            f"Affected: {', '.join(shock_result.get('affected_sectors') or [])}\n"
            f"Reason: {shock_result.get('reason')}"
        )
    elif "unavailable" in str(shock_result.get("reason", "")):
        log_event("WARN", "macro_shock_scan_unavailable", shock_result)
    return shock_result


def _missing_runtime_config() -> list[str]:
    required = [
        "ALPACA_API_KEY",
        "ALPACA_SECRET_KEY",
        "GROQ_API_KEY",
        "SUPABASE_URL",
        "SUPABASE_ANON_KEY",
        "SUPABASE_SERVICE_KEY",
    ]
    if not IS_PAPER_TRADING and os.getenv("ENABLE_LIVE_TRADING", "").strip().lower() != "true":
        required.append("ENABLE_LIVE_TRADING")
    return [key for key in required if not os.getenv(key)]


def _apply_execution_overrides(profile: dict) -> dict:
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
    p.setdefault("leveraged_etf_stop_scalar", 1.35)
    p.setdefault("a_plus_full_size_max_atr_pct", 2.5)
    p.setdefault("a_plus_full_size_max_stop_pct", 5.0)
    p.setdefault("high_atr_stop_threshold_pct", 1.0)
    p.setdefault("high_atr_stop_multiplier", 2.5)
    p.setdefault("grade_ev_override_negative_min_samples", 10)
    p.setdefault("probe_floor_inflation_max_multiple", 1.25)
    p.setdefault("ranging_regime_size_multiplier", 0.35)
    p.setdefault("ranging_max_trades_per_day", 6)
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
    if IS_PAPER_TRADING:
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
    if os.getenv("RANGING_MAX_TRADES_PER_DAY"):
        p["ranging_max_trades_per_day"] = _env_int("RANGING_MAX_TRADES_PER_DAY", int(p["ranging_max_trades_per_day"]))
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
    return p


# _apply_learned_hold_extension .. _check_pdt_warning → moved to backend/execution/exit.py

# ── Momentum swing promotion ──────────────────────────────────────────────────

def _try_promote_to_swing(ticker: str, trade: dict, current_price: float,
                          profile: dict, regime_state=None) -> bool:
    """
    Called at intraday time_exit boundary. If momentum is still intact and
    the trade is profitable or only within a controlled loss floor, promote it
    to a 3-5 day swing instead of closing.
    Returns True if promoted (caller should skip the close).
    """
    ticker = str(ticker or "").upper()
    if ticker not in SWING_TICKERS:
        log_event("INFO", "eod_carry_blocked_not_swing_ticker", {"ticker": ticker})
        return False

    if (trade.get("entry_price") or 0) <= 0:
        return False
    entry_price = float(trade.get("entry_price") or 0)
    pnl_pct = _trade_pnl_pct(trade, current_price)
    stop_pct_for_floor = float(
        trade.get("stop_pct")
        or profile.get("stop_loss_pct")
        or 2.5
    )
    max_loss_r = _env_float(
        "EOD_CARRY_MAX_LOSS_R",
        float(profile.get("eod_carry_max_loss_r", 0.5)),
    )
    max_carry_loss_pct = -1 * abs(stop_pct_for_floor) * max_loss_r
    if pnl_pct < max_carry_loss_pct:
        log_event("INFO", "eod_carry_blocked_loss_too_deep", {
            "ticker": ticker,
            "pnl_pct": round(pnl_pct, 4),
            "max_carry_loss_pct": round(max_carry_loss_pct, 4),
            "max_loss_r": max_loss_r,
        })
        return False

    # Don't promote mean-reversion trades
    if trade.get("mean_reversion_trade"):
        return False

    # Never promote leveraged ETFs to swing — daily decay makes overnight holds dangerous
    if _is_leveraged_etf(ticker, profile):
        return False

    overnight_event_risk = _overnight_event_risk_active(ticker)
    if overnight_event_risk:
        log_event("INFO", "eod_carry_blocked_event_risk", {
            "ticker": ticker,
            "event_risk": overnight_event_risk,
        })
        return False

    # Check concurrent swing limit before running expensive signal computation
    open_swing_count = sum(1 for d in _open_trades.values() if d.get("swing_trade"))
    max_swings = int(profile.get("max_concurrent_swings", 2))
    if open_swing_count >= max_swings:
        log_event("INFO", "swing_promotion_blocked_concurrent", {
            "ticker": ticker,
            "open_swings": open_swing_count,
            "max_swings": max_swings,
        })
        return False

    overnight_count = sum(
        1 for t, d in _open_trades.items()
        if t != ticker and d.get("swing_trade")
    )
    max_overnight = _env_int(
        "MAX_OVERNIGHT_CARRIES",
        int(profile.get("max_overnight_carries", 1)),
    )
    if overnight_count >= max_overnight:
        log_event("INFO", "eod_carry_blocked_overnight_cap", {
            "ticker": ticker,
            "open_overnight_carries": overnight_count,
            "max_overnight_carries": max_overnight,
        })
        return False

    try:
        weights = _learning_engine.get_weights("trending") if _learning_engine else profile["signal_weights"]
        regime_state = regime_state or detect_regime(ticker)
        signal_result = _get_cached_signals(ticker, weights, regime_state)
        swing_check = detect_momentum_swing(ticker, signal_result, regime_state, profile)
    except Exception as e:
        log_event("WARN", "swing_promotion_signal_error", {"ticker": ticker, "error": str(e)[:80]})
        return False

    if not swing_check.get("swing_detected"):
        return False

    hold_days = swing_check["hold_days"]
    hold_minutes = swing_check["hold_minutes"]
    stop_multiplier = swing_check["stop_multiplier"]

    atr_data = signal_result.get("atr_data", {})
    atr_pct = atr_data.get("atr_pct") or 2.5
    atr_raw = atr_data.get("atr_raw") or (entry_price * atr_pct / 100)
    stop_pct = max(0.5, min(12.0, float(atr_pct) * stop_multiplier))

    side = trade.get("side", "BUY")
    if side == "BUY":
        stop_price = entry_price * (1 - stop_pct / 100)
        chandelier_stop = current_price - (atr_raw * stop_multiplier)
        protective_stop_price = max(stop_price, chandelier_stop)
        # Never tighter than 1 ATR below entry — prevents intraday-tight stops
        # surviving into a multi-day swing hold
        protective_stop_price = min(protective_stop_price, entry_price - atr_raw)
        protective_side = "sell"
    else:
        stop_price = entry_price * (1 + stop_pct / 100)
        chandelier_stop = current_price + (atr_raw * stop_multiplier)
        protective_stop_price = min(stop_price, chandelier_stop)
        protective_stop_price = max(protective_stop_price, entry_price + atr_raw)
        protective_side = "buy"

    cancel_results = _cancel_bracket_orders_for_manual_exit(ticker, trade)
    cancel_errors = [r for r in cancel_results if r.get("error")]
    if cancel_errors:
        log_event("WARN", "swing_promotion_bracket_cancel_failed", {
            "ticker": ticker,
            "errors": cancel_errors[:4],
        })
        return False

    protective_order = submit_stop_order(
        ticker=ticker,
        side=protective_side,
        qty=float(trade.get("quantity") or 0),
        stop_price=protective_stop_price,
        time_in_force="gtc",
    )
    if protective_order.get("error"):
        log_event("WARN", "swing_promotion_stop_order_failed", {
            "ticker": ticker,
            "error": protective_order["error"],
            "stop_price": round(protective_stop_price, 4),
        })
        return False

    _open_trades[ticker].update({
        "swing_trade":              True,
        "promoted_to_swing":        True,
        "promoted_at":              datetime.utcnow().isoformat(),
        "initial_horizon":          trade.get("horizon", "short"),
        "horizon":                  "swing",
        "hold_minutes":             hold_minutes,
        "max_hold_minutes":         hold_minutes,
        "stop_multiplier":          stop_multiplier,
        "swing_conviction":         swing_check["conviction"],
        "swing_reasons":            swing_check["reasons"],
        "highest_price_since_entry": max(current_price, entry_price),
        "trailing_stop_price":      round(protective_stop_price, 4),
        "stop_price":               round(protective_stop_price, 4),
        "stop_pct":                 stop_pct,
        "protective_stop_order_id": protective_order.get("order_id"),
        "hold_decision_json": {
            "promoted_at_pnl_pct": round(pnl_pct, 3),
            "eod_decision":        "carry_overnight" if pnl_pct <= 0 else "promote_swing",
            "max_carry_loss_pct":  round(max_carry_loss_pct, 4),
            "swing_check":         swing_check,
            "cancelled_bracket_legs": cancel_results,
            "protective_stop_order": protective_order,
        },
    })

    save_result = save_open_trade(ticker, _open_trades[ticker])
    if save_result.get("error"):
        # swing_trade=True exists in memory but not DB — next cold-start will
        # treat this as an intraday trade and may EOD-exit it incorrectly.
        log_event("ERROR", "swing_promotion_save_failed", {
            "ticker": ticker,
            "error": save_result["error"],
            "swing_trade": True,
            "protective_stop_order_id": protective_order.get("order_id"),
        })

    log_event("INFO", "swing_promoted", {
        "ticker":          ticker,
        "hold_days":       hold_days,
        "conviction":      swing_check["conviction"],
        "reasons":         swing_check["reasons"],
        "pnl_at_promotion": round(pnl_pct, 3),
        "protective_stop_order_id": protective_order.get("order_id"),
        "state_persisted": not bool(save_result.get("error")),
    })
    _send_discord_alert(
        f"Swing promoted: {ticker} "
        f"{hold_days}-day hold · "
        f"Conviction: {swing_check['conviction']:.0%} · "
        f"P&L at promotion: {pnl_pct:+.1f}%"
    )
    return True


# ── Percentile window update ─────────────────────────────────────────────────

def _update_signal_percentiles(cycle_composites: dict, db_percentiles: dict):
    """Best-effort per-ticker percentile window update after each cycle."""
    for ticker, composite in cycle_composites.items():
        try:
            existing = db_percentiles.get(ticker, {})
            window = list(existing.get("window_composites") or [])
            window = merge_percentile_window(composite, window, max_window=200)
            thresholds = compute_percentile_thresholds(window)
            thresholds["window_composites"] = window
            upsert_signal_percentiles(ticker, thresholds)
        except Exception:
            pass  # never crash the cycle on percentile writes


# ── Core cycle ────────────────────────────────────────────────────────────────

def run_signal_cycle():
    """Main cycle: compute signals → gate → decide → execute."""
    global _learning_engine, _logged_sector_config_warnings

    cycle_start_utc = datetime.now(timezone.utc)

    missing_config = _missing_runtime_config()
    if missing_config:
        log_event("ERROR", "runtime_config_missing", {
            "missing": missing_config,
            "hint": "Set these as GitHub Actions secrets before the agent can trade.",
        })
        return

    if _learning_engine is None:
        _learning_engine = _init_learning_engine()
        import backend.runtime.state as _rt_state
        _rt_state._learning_engine = _learning_engine

    portfolio_state = _get_portfolio_state()
    if portfolio_state.get("broker_error"):
        log_event("ERROR", "broker_account_unavailable", {
            "error": portfolio_state["broker_error"],
        })
        return
    regime_state    = detect_regime()
    regime          = regime_state.intraday_regime
    shock_result    = _refresh_macro_shock_if_needed()
    macro_regime, macro_meta = detect_macro_regime(return_meta=True)
    effective_profile = _apply_execution_overrides(
        get_effective_profile(PROFILE, portfolio_state)
    )
    weights          = _learning_engine.get_weights(regime)
    recent_trades    = get_recent_trades(days=30)
    _hydrate_open_trades(portfolio_state.get("positions", []))

    # Staleness guard: if this cycle started too late (queued behind a cancelled
    # run), skip signal computation and only run exit checks. Bracket orders
    # protect positions at the broker; signal decisions on stale data cause harm.
    cycle_age_seconds = (datetime.now(timezone.utc) - cycle_start_utc).total_seconds()
    stale_threshold = _env_int("CYCLE_STALENESS_THRESHOLD_SECONDS", 180)
    cycle_stale = cycle_age_seconds > stale_threshold
    if cycle_stale:
        log_event("WARN", "cycle_stale_exits_only", {
            "cycle_age_seconds": round(cycle_age_seconds),
            "threshold_seconds": stale_threshold,
        })
        _check_exits(portfolio_state, effective_profile)
        _save_snapshot(portfolio_state, regime)
        return

    log_event("INFO", "cycle_start", {
        "regime": regime, "equity": portfolio_state["equity"],
        "vix": portfolio_state["vix"], "tickers": TICKERS,
        "horizon": HORIZON, "macro_regime": macro_regime,
        "macro_meta": macro_meta,
        "regime_state": regime_state.to_dict(),
        "shock_result": shock_result,
    })
    if _SECTOR_CONFIG_WARNINGS and not _logged_sector_config_warnings:
        log_event("WARN", "sector_config_warnings", {"warnings": _SECTOR_CONFIG_WARNINGS})
        _logged_sector_config_warnings = True

    if not TICKERS:
        log_event("ERROR", "no_tickers_configured", {
            "hint": "Set TICKER_UNIVERSE or leave it unset to use config/sector_universe.toml core tickers"
        })
        return

    # Recall sweep if cash was parked and we're about to trade
    if has_active_sweep():
        recall_result = recall_sweep(reason="signal_cycle_starting")
        if recall_result.get("mode") == "simulation":
            log_event("INFO", "recall_simulated", recall_result)

    # Dividend opportunity scan (1hr cache — does not re-fetch per-ticker)
    if os.getenv("DIVIDEND_SCAN_ENABLED", "true").lower() == "true":
        try:
            div_opps = scan_dividend_calendar(TICKERS)
            for opp in div_opps:
                if opp.get("opportunity_score", 0) > 0.5:
                    log_dividend_opportunity(opp)
                    log_event("INFO", "dividend_opportunity", opp)
        except Exception as e:
            log_event("WARN", "dividend_scan_failed", {"error": str(e)[:100]})

    # Earnings proximity guard (6hr cache — populates cache read by pre_trade_gate)
    try:
        from backend.earnings.scanner import scan_earnings_guard
        eg = scan_earnings_guard(TICKERS)
        blocked = [t for t, v in eg.items() if v.get("blocked")]
        if blocked:
            log_event("INFO", "earnings_guard_active", {"blocked_tickers": blocked})
    except Exception as e:
        log_event("WARN", "earnings_guard_scan_failed", {"error": str(e)[:100]})

    if _allows_intraday():
        # Reset cycle-level state
        global _cycle_composites, _cycle_db_percentiles
        _cycle_composites.clear()
        _rt_state._cycle_composites = _cycle_composites

        # Pre-populate DB news cache for all tickers in one batch (2-3 NewsAPI calls
        # instead of up to 22). Per-ticker news_sentiment_score calls below hit the cache.
        try:
            prefetch_newsapi_batch(TICKERS)
        except Exception:
            pass

        candidates = []
        for ticker in TICKERS:
            try:
                candidate = _evaluate_ticker_candidate(
                    ticker, regime, weights, effective_profile,
                    portfolio_state, recent_trades,
                    regime_state, shock_result,
                )
                if candidate:
                    candidates.append(candidate)
            except Exception as e:
                log_event("ERROR", f"ticker_error_{ticker}", {"error": str(e)})

        # ── Grading pass (uses full cycle composites for sector confirmation) ─
        _cycle_db_percentiles.clear()
        try:
            _cycle_db_percentiles.update(
                get_signal_percentiles(list(_cycle_composites.keys())) or {}
            )
            _rt_state._cycle_db_percentiles = _cycle_db_percentiles
        except Exception as e:
            log_event("WARN", "percentile_load_failed", {"error": str(e)[:120]})

        # Update percentile windows for all tickers computed this cycle
        _update_signal_percentiles(_cycle_composites, _cycle_db_percentiles)
        sector_momentum = _sector_momentum_snapshot(TICKERS, effective_profile)
        _log_dynamic_universe_shadow(TICKERS, sector_momentum, effective_profile)
        regime_observed = Counter(
            str(c.get("ticker_regime") or "unknown") for c in candidates
        )
        log_event("INFO", "cycle_regime_observability", {
            "market_regime": getattr(regime_state, "market_regime", None),
            "intraday_regimes": dict(regime_observed),
            "spy_trend_score": getattr(regime_state, "trend_score", None),
            "spy_trend_threshold": getattr(regime_state, "trend_threshold", None),
            "spy_regime_reason": getattr(regime_state, "regime_reason", None),
        })

        min_grade = effective_profile.get("min_grade_required", "B")
        graded_candidates = []
        for candidate in candidates:
            try:
                t = candidate["ticker"]
                sector_conf = compute_sector_confirmation(t, _cycle_composites)
                pct_rank = get_ticker_percentile_rank(
                    t, candidate["composite"], _cycle_db_percentiles
                )
                setup_grade = grade_setup(
                    t,
                    candidate["composite"],
                    candidate["signals_snap"],
                    candidate["ticker_regime_state"],
                    sector_conf,
                    pct_rank,
                    candidate.get("orb_score", 0.0),
                    effective_profile,
                )
                setup_grade = _vwap_1m_confirmation_downgrade(
                    candidate, setup_grade, effective_profile
                )
                candidate["setup_grade"] = setup_grade
                candidate["setup_context"]["setup_grade"] = setup_grade.grade
                candidate["setup_context"]["sector_confirmation"] = setup_grade.sector_confirmation
                candidate["setup_context"]["percentile_rank"] = setup_grade.percentile_rank
                _apply_sector_momentum_to_candidate(candidate, sector_momentum)
                if candidate.get("signal_id"):
                    update_signal(candidate["signal_id"], {
                        "setup_grade": setup_grade.grade,
                        "sector_confirmation": setup_grade.sector_confirmation,
                        "percentile_rank": setup_grade.percentile_rank,
                        "orb_score": candidate.get("orb_score", 0.0),
                    })

                # EV-blocked candidates: A+/A override with probe size; B/C drop
                if candidate.get("ev_blocked"):
                    if setup_grade.grade in {"A+", "A"}:
                        known_negative = _known_negative_grade_override_block(
                            candidate.get("ev_result"), effective_profile
                        )
                        if known_negative:
                            log_event("INFO", "grade_ev_override_known_negative_block", {
                                "ticker": t,
                                "grade": setup_grade.grade,
                                **known_negative,
                            })
                            _record_blocked_opportunity(
                                t, candidate.get("action_hint"), candidate["composite"],
                                candidate["signals_snap"], candidate["setup_context"],
                                candidate["ticker_regime"], "ev",
                                "known_negative_ev_grade_override_block",
                                ev_result=candidate["ev_result"],
                            )
                            continue
                        ev_override = candidate["ev_result"].copy()
                        ev_override["size_multiplier"] = 0.35
                        ev_override["ev_decision"] = "grade_ev_override_probe"
                        ev_override["decision"] = "proceed"
                        candidate["ev_result"] = ev_override
                        log_event("INFO", "ev_block_overridden_by_grade", {
                            "ticker": t, "grade": setup_grade.grade,
                            "original_reason": candidate["ev_result"].get("reason"),
                        })
                    else:
                        _record_blocked_opportunity(
                            t, candidate.get("action_hint"), candidate["composite"],
                            candidate["signals_snap"], candidate["setup_context"],
                            candidate["ticker_regime"], "ev",
                            candidate["ev_result"].get("reason", "ev_blocked"),
                            ev_result=candidate["ev_result"],
                        )
                    continue

                ranging_block = _ranging_regime_block(
                    t, candidate["setup_context"], candidate.get("ev_result"),
                    setup_grade, effective_profile,
                    signals_snap=candidate.get("signals_snap"),
                )
                if ranging_block:
                    if ranging_block.get("probe"):
                        _probe = ranging_block["probe"]
                        _probe_event = (
                            "ranging_probe_shadow_b_candidate"
                            if _probe.get("reason_not_probed") == "b_grade_shadow_only"
                            else "ranging_probe_rejected"
                        )
                        log_event("INFO", _probe_event, _probe)
                    log_event("INFO", "ranging_regime_candidate_block", {
                        "ticker": t,
                        "composite": round(float(candidate.get("composite") or 0), 4),
                        **{k: v for k, v in ranging_block.items() if k != "probe"},
                    })
                    _record_blocked_opportunity(
                        t, candidate.get("action_hint"), candidate["composite"],
                        candidate["signals_snap"], candidate["setup_context"],
                        candidate["ticker_regime"], "regime",
                        ranging_block["reason"],
                        ev_result=candidate.get("ev_result"),
                    )
                    continue
                if candidate["setup_context"].get("ranging_probe"):
                    log_event("INFO", "ranging_probe_allowed", {
                        "ticker": t,
                        "composite": round(float(candidate.get("composite") or 0), 4),
                        **candidate["setup_context"].get("ranging_probe_detail", {}),
                    })

                # Leveraged ETFs require A+ grade — drop anything weaker
                if _is_leveraged_etf(t, effective_profile) and setup_grade.grade != "A+":
                    log_event("INFO", "leveraged_etf_grade_block", {
                        "ticker": t, "grade": setup_grade.grade,
                        "reason": "leveraged_etf_requires_a_plus",
                    })
                    _record_blocked_opportunity(
                        t, candidate.get("action_hint"), candidate["composite"],
                        candidate["signals_snap"], candidate["setup_context"],
                        candidate["ticker_regime"], "ranking",
                        "leveraged_etf_not_a_plus",
                        ev_result=candidate.get("ev_result"),
                    )
                    continue

                # Enforce minimum grade from dynamic risk budget. B setups may be kept only as tiny
                # exploration trades so learning can continue without letting them drive P&L.
                if grade_sort_key(setup_grade.grade) < grade_sort_key(min_grade):
                    if setup_grade.grade == "B" and effective_profile.get("allow_b_grade_exploration", False):
                        ev_override = (candidate.get("ev_result") or {}).copy()
                        original_multiplier = float(ev_override.get("size_multiplier") or 1.0)
                        ev_override["size_multiplier"] = min(
                            original_multiplier,
                            float(effective_profile.get("b_grade_size_multiplier", 0.20)),
                        )
                        ev_override["ev_decision"] = "b_grade_exploration_size"
                        candidate["ev_result"] = ev_override
                        log_event("INFO", "b_grade_exploration_sized", {
                            "ticker": t,
                            "grade": setup_grade.grade,
                            "min_grade": min_grade,
                            "size_multiplier": ev_override["size_multiplier"],
                        })
                        graded_candidates.append(candidate)
                        continue
                    log_event("INFO", "grade_below_minimum", {
                        "ticker": t, "grade": setup_grade.grade,
                        "min_grade": min_grade, "composite": candidate["composite"],
                    })
                    _record_blocked_opportunity(
                        t, candidate.get("action_hint"), candidate["composite"],
                        candidate["signals_snap"], candidate["setup_context"],
                        candidate["ticker_regime"], "ranking",
                        f"grade_{setup_grade.grade}_below_min_{min_grade}",
                        ev_result=candidate.get("ev_result"),
                    )
                    continue

                graded_candidates.append(candidate)
            except Exception as e:
                log_event("WARN", f"grade_error_{candidate['ticker']}", {"error": str(e)[:120]})
                _record_blocked_opportunity(
                    candidate["ticker"], candidate.get("action_hint"), candidate.get("composite", 0),
                    candidate.get("signals_snap"), candidate.get("setup_context"),
                    candidate.get("ticker_regime"), "ranking", "setup_grade_unavailable",
                    ev_result=candidate.get("ev_result"),
                )

        candidates = graded_candidates
        candidates, theme_skipped = _theme_cap_candidates(candidates, effective_profile)
        for skipped in theme_skipped:
            log_event("INFO", "candidate_theme_cap_skipped", {
                "ticker": skipped["ticker"],
                "theme": skipped.get("theme"),
                "reason": skipped.get("theme_cap_reason"),
                "rank_score": skipped.get("setup_context", {}).get("candidate_rank_score"),
            })
            _record_blocked_opportunity(
                skipped["ticker"],
                skipped.get("action_hint"),
                skipped.get("composite"),
                skipped.get("signals_snap"),
                skipped.get("setup_context"),
                skipped.get("ticker_regime"),
                "ranking",
                skipped.get("theme_cap_reason", "theme_cap"),
                ev_result=skipped.get("ev_result"),
            )
        if candidates:
            high_uniform = [
                c for c in candidates
                if (c.get("setup_grade") is not None
                    and float(c["setup_grade"].sector_confirmation or 0) >= 0.99
                    and float(c["setup_grade"].percentile_rank or 0) >= 95)
            ]
            if len(high_uniform) >= max(3, int(len(candidates) * 0.75)):
                log_event("WARN", "grading_metrics_uniform_high", {
                    "count": len(high_uniform),
                    "candidate_count": len(candidates),
                    "tickers": [c["ticker"] for c in high_uniform[:12]],
                    "reason": "sector_confirmation_and_percentile_rank_not_differentiating",
                })
        candidates.sort(
            key=lambda c: (
                grade_sort_key((c.get("setup_grade") or SetupGrade("B", 0.6, 0.5, 0.8, False, [], 0, 0.5, 40, False)).grade),
                float(c.get("setup_context", {}).get("candidate_rank_score") or 0),
                abs(float(c.get("composite") or 0)),
            ),
            reverse=True,
        )
        if candidates:
            max_per_cycle = _env_int(
                "MAX_NEW_INTRADAY_TRADES_PER_CYCLE",
                int(effective_profile.get("max_new_intraday_trades_per_cycle", 2)),
            )
            # Expand slot by 1 when 3+ full_size EV-approved setups are ready —
            # avoids dropping strong conviction tickers on high-conviction cycles.
            full_size_count = sum(
                1 for c in candidates
                if (c.get("ev_result") or {}).get("ev_decision") in ("full_size", "probe_size")
            )
            if full_size_count >= 3:
                max_per_cycle = max_per_cycle + 1
            log_event("INFO", "ranked_trade_candidates", {
                "selected": [c["ticker"] for c in candidates[:max_per_cycle]],
                "candidates": [
                    {
                        "ticker": c["ticker"],
                        "score": round(float(c.get("composite") or 0), 4),
                        "rank_score": c.get("setup_context", {}).get("candidate_rank_score"),
                        "theme": c.get("setup_context", {}).get("theme"),
                        "sector_momentum_multiplier": c.get("setup_context", {}).get("sector_momentum_multiplier"),
                        "breakout_quality": c.get("setup_context", {}).get("breakout_quality"),
                        "ev_decision": c.get("ev_result", {}).get("ev_decision"),
                        "strategy_family": c.get("setup_context", {}).get("strategy_family"),
                    }
                    for c in candidates
                ],
            })
            for skipped in candidates[max_per_cycle:]:
                log_event("INFO", "candidate_not_selected", {
                    "ticker": skipped["ticker"],
                    "reason": "lower ranked than selected candidates",
                    "rank_score": skipped.get("setup_context", {}).get("candidate_rank_score"),
                    "breakout_quality": skipped.get("setup_context", {}).get("breakout_quality"),
                    "ev_decision": skipped.get("ev_result", {}).get("ev_decision"),
                })
                _record_blocked_opportunity(
                    skipped["ticker"],
                    skipped.get("action_hint"),
                    skipped.get("composite"),
                    skipped.get("signals_snap"),
                    skipped.get("setup_context"),
                    skipped.get("ticker_regime"),
                    "ranking",
                    "lower ranked than selected candidates",
                    ev_result=skipped.get("ev_result"),
                )
            for candidate in candidates[:max_per_cycle]:
                try:
                    _execute_trade_candidate(candidate, effective_profile, portfolio_state)
                except Exception as e:
                    log_event("ERROR", f"candidate_execution_error_{candidate['ticker']}", {"error": str(e)})

    _run_dip_buy_scan(TICKERS, portfolio_state, macro_regime, effective_profile, regime_state)

    if _should_run_swing_recheck():
        run_swing_cycle(
            portfolio_state=portfolio_state,
            profile=effective_profile,
            regime=regime,
            regime_state=regime_state,
            macro_regime=macro_regime,
        )

    # Check open trades for stop-loss / time exit
    _check_exits(portfolio_state, effective_profile)

    # Save portfolio snapshot
    _save_snapshot(portfolio_state, regime)


def _evaluate_ticker_candidate(ticker, regime, weights, profile, portfolio_state, recent_trades,
                               regime_state, shock_result):
    """Signal → gate → EV. Returns a ranked candidate if execution should be considered."""
    ticker_regime_state = detect_regime(ticker)
    ticker_regime = ticker_regime_state.intraday_regime
    if _learning_engine:
        weights = _learning_engine.get_weights(ticker_regime)
    action_hint = None

    # 1. Compute signals (also warms the intra-cycle cache)
    signal_result = compute_all_signals(
        ticker, weights, regime_state=ticker_regime_state, shock_result=shock_result
    )
    _signal_cache[ticker] = (datetime.now(timezone.utc), signal_result)
    composite     = signal_result["composite_score"]
    # Capture for sector confirmation (used after the full evaluation loop)
    _cycle_composites[ticker] = composite
    signals_snap  = signal_result["signals"]
    atr_data       = signal_result.get("atr_data") or {}
    regime_debug   = _regime_debug_payload(ticker_regime_state, signal_result)
    news_headline = (signals_snap.get("news_sentiment", {})
                    .get("meta", {}).get("latest_headline", ""))

    # 2. Pre-trade gate (hard rules)
    capital_base = _trading_capital(portfolio_state["equity"])
    action_hint = _deterministic_action(composite)
    eod_entry_block = _is_new_intraday_entry_too_late(ticker)
    if eod_entry_block:
        setup_context = _trade_setup_context(
            ticker, action_hint, composite, signals_snap, signal_result,
            ticker_regime_state, gate_reason=eod_entry_block["reason"],
        )
        log_event("INFO", "eod_new_entry_block", {
            "ticker": ticker,
            "composite": round(composite, 4),
            **eod_entry_block,
        })
        _record_blocked_opportunity(
            ticker, action_hint, composite, signals_snap, setup_context,
            ticker_regime, "time", eod_entry_block["reason"],
        )
        return
    pre_size = compute_position_size(ticker, capital_base, profile, 0.7, atr_data, ticker_regime_state)
    size_eur = pre_size["size_eur"]
    if action_hint == "SELL":
        size_eur = _cap_short_notional(size_eur, capital_base, profile)
    cooldown = _time_exit_cooldown_active(ticker, recent_trades, profile)
    if cooldown:
        log_event("INFO", "time_exit_cooldown", cooldown)
        return
    thesis_cooldown = _thesis_invalidated_cooldown_active(ticker, action_hint, recent_trades, profile)
    if thesis_cooldown:
        log_event("INFO", "thesis_invalidated_cooldown", thesis_cooldown)
        return
    if str(ticker_regime or "").lower() == "ranging":
        stop_loss_cooldown = _ranging_stop_loss_cooldown_active(ticker, action_hint, recent_trades, profile)
        if stop_loss_cooldown:
            log_event("INFO", "ranging_stop_loss_cooldown", stop_loss_cooldown)
            return
    loss_cooldown = _ticker_loss_cooldown_active(ticker, action_hint, recent_trades, profile)
    if loss_cooldown and abs(composite) < float(loss_cooldown["min_reentry_score"]):
        loss_cooldown["composite"] = round(composite, 4)
        log_event("INFO", "ticker_loss_cooldown", loss_cooldown)
        return
    if str(ticker_regime or "").lower() == "ranging":
        ranging_cap = int(profile.get("ranging_max_trades_per_day", 6))
        if ranging_cap > 0 and int(portfolio_state.get("trades_today") or 0) >= ranging_cap:
            reason = f"ranging_regime_daily_trade_cap ({portfolio_state.get('trades_today')}/{ranging_cap})"
            log_event("INFO", "ranging_regime_trade_cap", {
                "ticker": ticker,
                "trades_today": portfolio_state.get("trades_today"),
                "ranging_max_trades_per_day": ranging_cap,
            })
            setup_context = _trade_setup_context(
                ticker, action_hint, composite, signals_snap, signal_result,
                ticker_regime_state, gate_reason=reason,
            )
            _record_blocked_opportunity(
                ticker, action_hint, composite, signals_snap, setup_context,
                ticker_regime, "regime", reason,
            )
            return
    gate_ok, gate_reason = pre_trade_gate(
        ticker, action_hint.lower(), size_eur, composite, profile, portfolio_state,
        market_regime=getattr(ticker_regime_state, "market_regime", None),
        signals=signals_snap,
    )

    # Leveraged ETF pre-entry gate (A+-only, VIX cap, no entries after 3:45 PM ET)
    if _is_leveraged_etf(ticker, profile):
        vix_now = float(portfolio_state.get("vix") or 20.0)
        lev_vix_ceiling = float(profile.get("leveraged_etf_vix_ceiling", 22))
        if vix_now >= lev_vix_ceiling:
            log_event("INFO", "leveraged_etf_vix_block", {
                "ticker": ticker, "vix": vix_now, "ceiling": lev_vix_ceiling,
            })
            return
        if _leveraged_etf_max_hold_window():
            log_event("INFO", "leveraged_etf_time_block", {
                "ticker": ticker, "reason": "past_3_45pm_et_no_new_entries",
            })
            return
        # Leveraged ETFs are A+-only entries
        # setup_grade not yet computed here — defer to the grading pass via ev_blocked flag
        # but we mark the candidate so the grading pass can enforce the A+ requirement
        gate_reason = gate_reason or {}
        if isinstance(gate_reason, dict):
            gate_reason["leveraged_etf"] = True

    setup_context = _trade_setup_context(
        ticker, action_hint, composite, signals_snap, signal_result,
        ticker_regime_state, gate_reason=gate_reason,
    )
    strategy_family_hint = setup_context["strategy_family"]
    alignment_veto = _alignment_veto(ticker, action_hint, signals_snap, profile)
    if alignment_veto:
        reason = alignment_veto["reason"]
        log_event("INFO", "signal_alignment_veto", {
            "ticker": ticker,
            "action": action_hint,
            "composite": round(composite, 4),
            **alignment_veto,
        })
        _record_blocked_opportunity(
            ticker, action_hint, composite, signals_snap, setup_context,
            ticker_regime, "signal_alignment", reason,
        )
        return
    consensus_block = _signal_consensus_block(action_hint, signals_snap, ticker_regime, profile)
    if consensus_block:
        reason = consensus_block["reason"]
        log_event("INFO", "signal_consensus_veto", {
            "ticker": ticker,
            "action": action_hint,
            "composite": round(composite, 4),
            "regime": ticker_regime,
            **consensus_block,
        })
        _record_blocked_opportunity(
            ticker, action_hint, composite, signals_snap, setup_context,
            ticker_regime, "signal_consensus", reason,
        )
        return
    theme_exposure_block = _theme_open_exposure_block(ticker, profile)
    if theme_exposure_block:
        reason = theme_exposure_block["reason"]
        log_event("INFO", "theme_open_exposure_cap", {
            "ticker": ticker,
            "action": action_hint,
            "composite": round(composite, 4),
            **theme_exposure_block,
        })
        _record_blocked_opportunity(
            ticker, action_hint, composite, signals_snap, setup_context,
            ticker_regime, "exposure", reason,
        )
        return

    late_chase_block = _late_chase_block(action_hint, signals_snap, atr_data, profile)
    if late_chase_block:
        log_event("INFO", "late_chase_block", {
            "ticker": ticker,
            "action": action_hint,
            "composite": round(composite, 4),
            **late_chase_block,
        })
        _record_blocked_opportunity(
            ticker, action_hint, composite, signals_snap, setup_context,
            ticker_regime, "entry_quality", late_chase_block["reason"],
            reference_price=signal_result.get("current_price"),
            block_detail=late_chase_block,
        )
        return

    rvol_block = _rvol_block(signal_result, profile)
    if rvol_block:
        log_event("INFO", "rvol_gate_block", {
            "ticker": ticker,
            "action": action_hint,
            "composite": round(composite, 4),
            **rvol_block,
        })
        _record_blocked_opportunity(
            ticker, action_hint, composite, signals_snap, setup_context,
            ticker_regime, "entry_quality", rvol_block["reason"],
            reference_price=signal_result.get("current_price"),
            block_detail=rvol_block,
        )
        return

    # 3. Log signal to DB. Grade metadata is updated after the full cycle
    # because sector confirmation depends on all tickers' composites.
    signal_row = insert_signal({
        "ticker":                 ticker,
        "composite_score":        composite,
        "order_book_score":       signals_snap.get("order_book_imbalance", {}).get("score", 0),
        "tape_aggression_score":  signals_snap.get("tape_aggression", {}).get("score", 0),
        "rsi_divergence_score":   signals_snap.get("rsi_divergence", {}).get("score", 0),
        "news_sentiment_score":   signals_snap.get("news_sentiment", {}).get("score", 0),
        "vwap_deviation_score":   signals_snap.get("vwap_deviation", {}).get("score", 0),
        "macd_score":             signals_snap.get("macd_crossover", {}).get("score", 0),
        "rel_strength_score":     signals_snap.get("relative_strength", {}).get("score", 0),
        "bollinger_score":        signals_snap.get("bollinger_squeeze", {}).get("score", 0),
        "put_call_score":         signals_snap.get("put_call_ratio", {}).get("score", 0),
        "atr_pct":                atr_data.get("atr_pct"),
        "atr_stop_pct":           atr_data.get("suggested_stop_pct"),
        "volatility_regime":      atr_data.get("volatility_regime"),
        "earnings_days":          signals_snap.get("earnings_proximity", {}).get("meta", {}).get("days_to_earnings"),
        "earnings_mult":          signals_snap.get("earnings_proximity", {}).get("meta", {}).get("earnings_multiplier", 1.0),
        "macro_regime":           signal_result.get("macro_regime"),
        "macro_multiplier":       signal_result.get("macro_multiplier", 1.0),
        "market_regime":          getattr(ticker_regime_state, "market_regime", None),
        "regime_bull_bear":       signal_result.get("regime_bull_bear"),
        "shock_detected":         signal_result.get("shock_detected", False),
        "shock_classification":   signal_result.get("shock_classification"),
        "yield_curve":           getattr(ticker_regime_state, "yield_curve", None),
        "yield_curve_state":     getattr(ticker_regime_state, "yield_curve_state", None),
        "regime":                 ticker_regime,
        "action_hint":            action_hint,
        "exposure_direction":     _exposure_direction(ticker, action_hint),
        "strategy_family":        strategy_family_hint,
        "playbook":               setup_context.get("playbook"),
        "playbook_lifecycle":     setup_context.get("playbook_lifecycle"),
        "session_window":         setup_context.get("session_window"),
        "primary_factor":         setup_context.get("primary_factor"),
        "factor_bucket":          setup_context.get("factor_bucket"),
        "regime_key":             setup_context.get("regime_key"),
        "data_quality_state":     setup_context.get("data_quality_state"),
        "data_quality_json":      setup_context.get("data_quality") or {},
        "cost_estimate_json":     setup_context.get("cost_estimate") or {},
        "estimated_spread_pct":   setup_context.get("estimated_spread_pct"),
        "estimated_total_cost_pct": setup_context.get("estimated_total_cost_pct"),
        "regime_debug_json":      regime_debug,
        "vix":                    portfolio_state["vix"],
        "gated":                  not gate_ok,
        "gate_reason":            gate_reason if not gate_ok else None,
        "llm_called":             False,
        "orb_score":              signal_result.get("orb_score", 0.0),
    })
    signal_id = signal_row.get("id") if isinstance(signal_row, dict) else None

    if not gate_ok:
        log_event("INFO", "trade_gated", {
            "ticker": ticker,
            "composite": composite,
            "reason": gate_reason,
        })
        if action_hint == "SELL":
            _log_short_candidate(
                "short_candidate_gated", ticker, composite, gate_reason,
                profile, ticker_regime_state,
            )
        _record_blocked_opportunity(
            ticker, action_hint, composite, signals_snap, setup_context,
            ticker_regime, "gate", gate_reason,
            block_detail=(
                _threshold_block_detail(
                    action_hint, composite, profile,
                    market_regime=getattr(ticker_regime_state, "market_regime", None),
                )
                if "signal below threshold" in str(gate_reason)
                else {}
            ),
        )
        return

    # 4. EV check
    ev_result = compute_expected_value(
        composite, size_eur, recent_trades, ticker_regime,
        setup_context=setup_context,
        profile=profile,
    )
    ev_blocked = ev_result["decision"] == "block"
    if ev_blocked:
        # Don't hard-block here — carry forward to grading pass.
        # A+/A grades will override with probe size; B/C will be dropped there.
        log_event("INFO", "ev_blocked_pending_grade", {"ticker": ticker, **ev_result})
    if ev_result.get("ev_decision") not in {None, "full_size", "exploration_full_size"}:
        log_event("INFO", "ev_sizing_adjusted", {
            "ticker": ticker,
            "composite": composite,
            "ev_decision": ev_result.get("ev_decision"),
            "size_multiplier": ev_result.get("size_multiplier"),
            "breakout_quality": ev_result.get("breakout_quality"),
            "reason": ev_result.get("reason"),
            "setup": setup_context,
        })

    return {
        "ticker": ticker,
        "ticker_regime": ticker_regime,
        "ticker_regime_state": ticker_regime_state,
        "signal_result": signal_result,
        "composite": composite,
        "signals_snap": signals_snap,
        "atr_data": atr_data,
        "regime_debug": regime_debug,
        "news_headline": news_headline,
        "setup_context": setup_context,
        "ev_result": ev_result,
        "ev_blocked": ev_blocked,
        "capital_base": capital_base,
        "action_hint": action_hint,
        "orb_score": signal_result.get("orb_score", 0.0),
        "signal_id": signal_id,
    }


def _execute_trade_candidate(candidate: dict, profile: dict, portfolio_state: dict):
    """LLM → order for an already-ranked candidate."""
    ticker = candidate["ticker"]
    ticker_regime = candidate["ticker_regime"]
    ticker_regime_state = candidate["ticker_regime_state"]
    signal_result = candidate["signal_result"]
    composite = candidate["composite"]
    signals_snap = candidate["signals_snap"]
    atr_data = candidate["atr_data"]
    regime_debug = candidate["regime_debug"]
    news_headline = candidate["news_headline"]
    setup_context = candidate["setup_context"]
    ev_result = candidate["ev_result"]
    capital_base = candidate["capital_base"]

    # ── A+ setup grade — override soft LLM blocks ────────────────────────────
    setup_grade: Optional[SetupGrade] = candidate.get("setup_grade")
    is_a_plus = setup_grade is not None and setup_grade.grade == "A+"
    hard_blocks = a_plus_hard_blocks()
    action = _deterministic_action(composite)

    # 5. LLM decision (gated by hourly limit)
    llm_result = None
    if not _can_call_llm():
        if is_a_plus:
            # A+ escalation: skip LLM, proceed with probe size
            log_event("INFO", "a_plus_llm_limit_override", {
                "ticker": ticker, "composite": composite,
                "grade": setup_grade.grade, "action": action,
            })
            llm_result = {
                "action": action,
                "conviction": max(abs(composite), 0.55),
                "hold_minutes": int(profile.get("max_hold_minutes", 45)),
                "stop_loss_pct": float(profile.get("stop_loss_pct", 2.0)),
                "rationale": "a_plus_llm_limit_override",
            }
            ev_result = ev_result.copy()
            ev_result["size_multiplier"] = min(ev_result.get("size_multiplier", 1.0), 0.35)
            ev_result["ev_decision"] = "a_plus_probe"
        else:
            log_event("WARN", "llm_limit_hit", {"ticker": ticker})
            _record_blocked_opportunity(
                ticker, candidate.get("action_hint"), composite, signals_snap, setup_context,
                ticker_regime, "llm", "llm_limit_hit", ev_result=ev_result,
            )
            return

    if llm_result is None:
        llm_result = llm_signal_decision(
            ticker, composite, ticker_regime, news_headline, profile,
            signal_scores   = signals_snap,
            atr_data        = atr_data,
            regime_context  = {
                "market_regime": getattr(ticker_regime_state, "market_regime", ""),
                "vix":           portfolio_state.get("vix", ""),
            },
        )
        _record_llm_call()
    suggested_action = str(llm_result.get("action", "HOLD")).upper()
    action = _deterministic_action(composite)
    raw_llm_conviction = llm_result.get("conviction", 0)
    llm_conviction = raw_llm_conviction if isinstance(raw_llm_conviction, (int, float)) else 0
    conviction = max(abs(composite), float(llm_conviction or 0))
    log_event("SIGNAL", "llm_decision", {
        "ticker": ticker,
        "composite": composite,
        "deterministic_action": action,
        "llm_action": suggested_action,
        "conviction": conviction,
        "rationale": llm_result.get("rationale", ""),
    })

    if suggested_action == "HOLD":
        allow_hold_override = bool(profile.get("allow_a_plus_llm_hold_override", False))
        if is_a_plus and allow_hold_override and not _llm_rationale_mentions_conflict(llm_result):
            # A+ escalation: override LLM HOLD — deterministic action with probe size
            log_event("INFO", "a_plus_llm_hold_override", {
                "ticker": ticker, "composite": composite,
                "grade": setup_grade.grade, "llm_rationale": llm_result.get("rationale", ""),
            })
            suggested_action = action
            # Force probe size — don't go full size on an LLM-overridden entry
            ev_result = ev_result.copy()
            ev_result["size_multiplier"] = min(ev_result.get("size_multiplier", 1.0), 0.35)
            ev_result["ev_decision"] = "a_plus_probe"
        else:
            log_event("INFO", "llm_hold_veto", {
                "ticker": ticker,
                "composite": composite,
                "rationale": llm_result.get("rationale", ""),
                "grade": setup_grade.grade if setup_grade else None,
                "a_plus_hold_override_enabled": allow_hold_override,
            })
            _record_blocked_opportunity(
                ticker, action, composite, signals_snap, setup_context,
                ticker_regime, "llm",
                llm_result.get("rationale", "llm_hold_veto"),
                ev_result=ev_result,
            )
            if action == "SELL":
                _log_short_candidate(
                    "short_candidate_llm_hold", ticker, composite,
                    llm_result.get("rationale", "llm_hold"), profile, ticker_regime_state,
                    {"llm_conviction": llm_conviction},
                )
            return
    if suggested_action in {"BUY", "SELL"} and suggested_action != action:
        log_event("INFO", "llm_direction_conflict", {
            "ticker": ticker,
            "composite": composite,
            "deterministic_action": action,
            "llm_action": suggested_action,
            "rationale": llm_result.get("rationale", ""),
        })
        _record_blocked_opportunity(
            ticker, action, composite, signals_snap, setup_context,
            ticker_regime, "llm",
            llm_result.get("rationale", "llm_direction_conflict"),
            ev_result=ev_result,
        )
        if action == "SELL":
            _log_short_candidate(
                "short_candidate_llm_conflict", ticker, composite,
                llm_result.get("rationale", "llm_direction_conflict"),
                profile, ticker_regime_state,
                {"llm_action": suggested_action, "llm_conviction": llm_conviction},
            )
        return
    if suggested_action in {"BUY", "SELL"} and _llm_rationale_mentions_conflict(llm_result):
        log_event("INFO", "llm_rationale_conflict_veto", {
            "ticker": ticker,
            "composite": composite,
            "llm_action": suggested_action,
            "rationale": llm_result.get("rationale", ""),
        })
        _record_blocked_opportunity(
            ticker, action, composite, signals_snap, setup_context,
            ticker_regime, "llm",
            llm_result.get("rationale", "llm_rationale_conflict_veto"),
            ev_result=ev_result,
        )
        return

    if conviction < profile["min_conviction"]:
        log_event("INFO", "conviction_below_threshold", {
            "ticker": ticker,
            "conviction": conviction,
            "min_conviction": profile["min_conviction"],
        })
        _record_blocked_opportunity(
            ticker, action, composite, signals_snap, setup_context,
            ticker_regime, "conviction", "conviction_below_threshold",
            ev_result=ev_result,
        )
        if action == "SELL":
            _log_short_candidate(
                "short_candidate_conviction_blocked", ticker, composite,
                "conviction_below_threshold", profile, ticker_regime_state,
                {"conviction": conviction, "min_conviction": profile["min_conviction"]},
            )
        return

    # 6. Size and submit order
    sizing = compute_position_size(ticker, capital_base, profile, conviction, atr_data, ticker_regime_state)
    base_stop_pct = float(sizing.get("stop_pct") or profile.get("stop_loss_pct", 2.0))
    stop_scalar = _leveraged_etf_stop_scalar(ticker, profile)
    if stop_scalar > 1.0:
        adjusted_stop_pct = min(12.0, max(base_stop_pct, base_stop_pct * stop_scalar))
        if adjusted_stop_pct > base_stop_pct:
            risk_scale = base_stop_pct / adjusted_stop_pct
            sizing["size_eur"] = round(float(sizing["size_eur"]) * risk_scale, 2)
            sizing["stop_pct"] = round(adjusted_stop_pct, 3)
            sizing["volatility_stop_adjustment"] = {
                "reason": "leveraged_etf_stop_scalar",
                "base_stop_pct": round(base_stop_pct, 3),
                "adjusted_stop_pct": round(adjusted_stop_pct, 3),
                "risk_scale": round(risk_scale, 4),
                "stop_scalar": round(stop_scalar, 3),
            }
            log_event("INFO", "volatility_stop_adjusted", {
                "ticker": ticker,
                **sizing["volatility_stop_adjustment"],
            })
    final_size = sizing["size_eur"]
    ev_size_multiplier = float(ev_result.get("size_multiplier") or 1.0)
    # Apply grade multiplier on top of EV multiplier (capped at 2.0× to prevent runaway sizing)
    if setup_grade is not None and setup_grade.size_multiplier > 0:
        combined_mult = effective_size_multiplier(setup_grade, ev_size_multiplier)
    else:
        combined_mult = ev_size_multiplier
    if setup_grade is not None and setup_grade.grade == "A+":
        atr_pct_for_quality = float(sizing.get("atr_pct") or 0)
        stop_pct_for_quality = float(sizing.get("stop_pct") or 0)
        max_atr = float(profile.get("a_plus_full_size_max_atr_pct", 2.5))
        max_stop = float(profile.get("a_plus_full_size_max_stop_pct", 5.0))
        if atr_pct_for_quality > max_atr or stop_pct_for_quality > max_stop:
            combined_mult = min(combined_mult, ev_size_multiplier)
            sizing["a_plus_size_capped"] = True
            sizing["a_plus_size_cap_reason"] = {
                "atr_pct": round(atr_pct_for_quality, 3),
                "max_atr_pct": max_atr,
                "stop_pct": round(stop_pct_for_quality, 3),
                "max_stop_pct": max_stop,
            }
            log_event("INFO", "a_plus_size_capped_by_volatility", {
                "ticker": ticker,
                **sizing["a_plus_size_cap_reason"],
            })
    if str(getattr(ticker_regime_state, "intraday_regime", "")).lower() == "ranging":
        ranging_scalar = max(0.0, min(1.0, float(profile.get("ranging_regime_size_multiplier", 0.35))))
        combined_mult *= ranging_scalar
        ranging_cap = float(profile.get("ranging_max_notional_eur", 0) or 0)
        if ranging_cap > 0:
            sizing["ranging_max_notional_eur"] = round(ranging_cap, 2)
        sizing["ranging_regime_size_scalar"] = round(ranging_scalar, 3)
        log_event("INFO", "ranging_regime_size_reduced", {
            "ticker": ticker,
            "scalar": round(ranging_scalar, 3),
            "combined_size_multiplier": round(combined_mult, 3),
        })
    final_size *= combined_mult
    if action == "SELL":
        final_size = _cap_short_notional(final_size, capital_base, profile)
    max_notional = _env_float(
        "MAX_NOTIONAL_PER_TRADE_EUR",
        profile.get("max_trade_notional_eur", final_size),
    )
    if str(getattr(ticker_regime_state, "intraday_regime", "")).lower() == "ranging":
        ranging_cap = _env_float("RANGING_MAX_NOTIONAL_EUR", float(profile.get("ranging_max_notional_eur", max_notional) or max_notional))
        if ranging_cap > 0:
            max_notional = min(max_notional, ranging_cap)
    final_size = min(final_size, max_notional)
    sizing["size_eur"] = round(final_size, 2)
    sizing["ev_decision"] = ev_result.get("ev_decision")
    sizing["ev_size_multiplier"] = round(ev_size_multiplier, 3)
    sizing["grade_size_multiplier"] = round(setup_grade.size_multiplier, 3) if setup_grade else 1.0
    sizing["combined_size_multiplier"] = round(combined_mult, 3)
    sizing["ev_result"] = ev_result
    sizing["setup_context"] = setup_context

    import yfinance as yf
    bar = yf.download(ticker, period="1d", interval="1m",
                      progress=False, auto_adjust=True)
    if bar.empty:
        log_event("WARN", "price_unavailable", {"ticker": ticker})
        _record_blocked_opportunity(
            ticker, action, composite, signals_snap, setup_context,
            ticker_regime, "price", "price_unavailable",
            ev_result=ev_result,
        )
        return
    current_price = float(bar["Close"].squeeze().iloc[-1])
    intended_size_eur = float(final_size or 0)
    use_bracket_orders = _env_value("USE_BRACKET_ORDERS", "true").lower() != "false"

    if setup_grade is not None and setup_grade.grade in {"A+", "A"} and current_price > 0:
        min_grade_shares = _env_float("GRADE_MIN_EXECUTABLE_SHARES", 2.0)
        min_buffer = max(0.0, _env_float("GRADE_MIN_NOTIONAL_BUFFER_PCT", 0.5)) / 100
        min_notional_eur = (current_price * min_grade_shares * (1 + min_buffer)) / _eurusd_rate()
        if final_size < min_notional_eur:
            capped_min_size = min(min_notional_eur, max_notional)
            if capped_min_size > final_size:
                final_size = capped_min_size
                sizing["grade_min_executable_shares"] = min_grade_shares
                sizing["grade_min_notional_eur"] = round(min_notional_eur, 2)
                sizing["grade_min_notional_buffer_pct"] = round(min_buffer * 100, 3)
                sizing["grade_min_notional_applied"] = True
                log_event("INFO", "grade_min_notional_applied", {
                    "ticker": ticker,
                    "grade": setup_grade.grade,
                    "previous_size_eur": round(intended_size_eur, 2),
                    "new_size_eur": round(final_size, 2),
                    "min_shares": min_grade_shares,
                    "buffer_pct": round(min_buffer * 100, 3),
                    "current_price": round(current_price, 4),
                })
            else:
                sizing["grade_min_notional_applied"] = False
                sizing["grade_min_notional_capped"] = True
                sizing["grade_min_notional_eur"] = round(min_notional_eur, 2)
                sizing["grade_min_notional_buffer_pct"] = round(min_buffer * 100, 3)

    inflation_block = _probe_floor_inflation_block(
        ev_result.get("ev_decision"),
        bool(sizing.get("grade_min_notional_applied")),
        intended_size_eur,
        final_size,
        profile,
    )
    if inflation_block:
        reason = "probe_floor_inflation_block"
        log_event("INFO", reason, {
            "ticker": ticker,
            "ev_decision": ev_result.get("ev_decision"),
            "intended_size_eur": round(intended_size_eur, 2),
            "floor_size_eur": round(final_size, 2),
            **inflation_block,
        })
        _record_blocked_opportunity(
            ticker, action, composite, signals_snap, setup_context,
            ticker_regime, "sizing", reason,
            ev_result=ev_result, reference_price=current_price,
        )
        return

    final_size_usd = _eur_to_usd(final_size)
    sizing["size_eur"] = round(final_size, 2)
    sizing["size_usd"] = round(final_size_usd, 2)
    qty = final_size_usd / current_price
    floor_qty = math.floor(qty) if use_bracket_orders else round(qty, 6)
    bracket_floor_qty_loss_pct = (
        round(max(0.0, (qty - floor_qty) / qty * 100), 4)
        if use_bracket_orders and qty > 0 else 0.0
    )
    sizing["intended_size_eur"] = round(intended_size_eur, 2)
    sizing["implied_qty"] = round(qty, 6)
    sizing["floor_qty"] = floor_qty
    sizing["bracket_floor_qty_loss_pct"] = bracket_floor_qty_loss_pct

    if use_bracket_orders and floor_qty < 1:
        reason = "bracket_floor_would_waste_trade"
        log_event("INFO", "bracket_floor_preflight_block", {
            "ticker": ticker,
            "grade": setup_grade.grade if setup_grade else None,
            "final_size_eur": round(final_size, 2),
            "size_usd": round(final_size_usd, 2),
            "current_price": round(current_price, 4),
            "implied_qty": round(qty, 6),
            "floor_qty": floor_qty,
            "ev_decision": ev_result.get("ev_decision"),
        })
        _record_blocked_opportunity(
            ticker, action, composite, signals_snap, setup_context,
            ticker_regime, "sizing", reason,
            ev_result=ev_result, reference_price=current_price,
        )
        return

    raw_hold_minutes = llm_result.get("hold_minutes", 30)
    try:
        hold_minutes = int(raw_hold_minutes)
    except (TypeError, ValueError):
        hold_minutes = 30
    mean_reversion_trade = bool(signal_result.get("mean_reversion_signal"))
    event_risk_probe = bool(setup_context.get("event_risk_intraday_probe"))
    hold_extension = None
    if mean_reversion_trade:
        hold_minutes = 2880
    else:
        hold_minutes = max(
            int(profile.get("min_hold_minutes", 1)),
            min(int(profile.get("max_hold_minutes", 60)), hold_minutes),
        )
        if event_risk_probe:
            hold_minutes = min(
                hold_minutes,
                int(profile.get("event_risk_max_hold_minutes", 30)),
            )
        else:
            hold_minutes, hold_extension = _apply_learned_hold_extension(
                ticker=ticker,
                hold_minutes=hold_minutes,
                conviction=conviction,
                composite=composite,
                profile=profile,
                portfolio_state=portfolio_state,
            )
            if hold_extension:
                log_event("INFO", "learned_hold_extended", hold_extension)

    stop_loss_pct = sizing.get("stop_pct") or float(llm_result.get("stop_loss_pct", profile["stop_loss_pct"]))
    if mean_reversion_trade:
        raw_atr_pct = atr_data.get("atr_pct")
        if raw_atr_pct:
            stop_loss_pct = max(stop_loss_pct, round(float(raw_atr_pct) * 2.0, 3))
    if event_risk_probe:
        stop_loss_pct = max(
            float(profile.get("event_risk_min_stop_pct", 0.25)),
            stop_loss_pct * float(profile.get("event_risk_stop_multiplier", 0.75)),
        )
        sizing["event_risk_intraday_only"] = True

    take_profit_pct = float(profile.get("take_profit_pct", profile["stop_loss_pct"] * 1.2))
    rr_block = _reward_risk_block(stop_loss_pct, take_profit_pct, ticker_regime, profile)
    if rr_block:
        log_event("INFO", "reward_risk_veto", {
            "ticker": ticker,
            "action": action,
            "composite": round(composite, 4),
            "regime": ticker_regime,
            **rr_block,
        })
        _record_blocked_opportunity(
            ticker, action, composite, signals_snap, setup_context,
            ticker_regime, "reward_risk", rr_block["reason"],
            ev_result=ev_result, reference_price=current_price,
        )
        return
    sizing["take_profit_pct"] = round(take_profit_pct, 4)
    sizing["reward_risk_ratio"] = round(take_profit_pct / max(float(stop_loss_pct or 0), 0.0001), 4)

    order = submit_market_order(
        ticker          = ticker,
        side            = action.lower(),
        qty             = round(qty, 6),
        stop_loss_pct   = stop_loss_pct,
        take_profit_pct = take_profit_pct,
        current_price   = current_price,
        signal_id       = candidate.get("signal_id"),
    )

    if "error" in order:
        log_event("ERROR", "order_failed", {"ticker": ticker, "error": order["error"],
                                             "client_order_id": order.get("client_order_id")})
        return

    submitted_qty = float(order.get("qty") or floor_qty or round(qty, 6))
    executed_size_usd = submitted_qty * current_price
    executed_size_eur = executed_size_usd / _eurusd_rate()
    sizing["submitted_qty"] = round(submitted_qty, 6)
    sizing["executed_size_usd"] = round(executed_size_usd, 2)
    sizing["executed_size_eur"] = round(executed_size_eur, 2)

    # Track open trade for exit monitoring
    if action == "BUY":
        stop_price = current_price * (1 - stop_loss_pct / 100)
        take_profit_price = current_price * (1 + take_profit_pct / 100)
    else:
        stop_price = current_price * (1 + stop_loss_pct / 100)
        take_profit_price = current_price * (1 - take_profit_pct / 100)

    exposure_direction = _exposure_direction(ticker, action)
    strategy_family = _strategy_family(
        ticker, action, ticker_regime, signal_result,
        horizon="short", mean_reversion_trade=mean_reversion_trade,
    )

    # Compute grade-differentiated partial exit target and runner ATR stop
    atr_raw = float(atr_data.get("atr_raw") or (current_price * float(atr_data.get("atr_pct", 2.5)) / 100))
    if setup_grade is not None and setup_grade.grade in {"A+", "A", "B"}:
        partial_atr_mult = 1.5 if setup_grade.grade == "A+" else 1.2
        if action == "BUY":
            partial_target_price = current_price + atr_raw * partial_atr_mult
        else:
            partial_target_price = current_price - atr_raw * partial_atr_mult
        partial_exit_pct = setup_grade.partial_exit_pct
        runner_atr_mult = setup_grade.runner_atr_multiplier
    else:
        partial_target_price = None
        partial_exit_pct = 0.5
        runner_atr_mult = 0.8

    _open_trades[ticker] = {
        "entry_time":    datetime.utcnow(),
        "entry_price":   current_price,
        "quantity":      submitted_qty,
        "submitted_qty": submitted_qty,
        "implied_qty":   round(qty, 6),
        "stop_price":    stop_price,
        "take_profit_price": take_profit_price,
        "hold_minutes":  hold_minutes,
        "hold_extension_count": 0,
        "hold_decision_json": hold_extension,
        "size_eur":      executed_size_eur,
        "size_usd":      executed_size_usd,
        "intended_size_eur": intended_size_eur,
        "executed_size_eur": executed_size_eur,
        "executed_size_usd": executed_size_usd,
        "bracket_floor_qty_loss_pct": bracket_floor_qty_loss_pct,
        "atr_pct":       sizing.get("atr_pct") or atr_data.get("atr_pct"),
        "atr_raw":       atr_data.get("atr_raw"),
        "stop_pct":      stop_loss_pct,
        "stop_multiplier": sizing.get("stop_multiplier"),
        "side":          action,
        "composite_score": composite,
        "signals_json":  {k: {"score": v["score"]} for k, v in signals_snap.items()},
        "regime":        ticker_regime,
        "exposure_direction": exposure_direction,
        "strategy_family": strategy_family,
        "playbook": setup_context.get("playbook"),
        "playbook_lifecycle": setup_context.get("playbook_lifecycle"),
        "session_window": setup_context.get("session_window"),
        "primary_factor": setup_context.get("primary_factor"),
        "factor_bucket": setup_context.get("factor_bucket"),
        "regime_key": setup_context.get("regime_key"),
        "data_quality_state": setup_context.get("data_quality_state"),
        "data_quality_json": setup_context.get("data_quality") or {},
        "cost_estimate_json": setup_context.get("cost_estimate") or {},
        "estimated_spread_pct": setup_context.get("estimated_spread_pct"),
        "estimated_total_cost_pct": setup_context.get("estimated_total_cost_pct"),
        "regime_debug_json": regime_debug,
        "macro_regime":  signal_result.get("macro_regime"),
        "macro_multiplier": signal_result.get("macro_multiplier", 1.0),
        "horizon":       "short",
        "sizing_json":   sizing,
        "mean_reversion_trade": mean_reversion_trade,
        "swing_trade":   False if event_risk_probe else mean_reversion_trade,
        "llm_conviction": conviction,
        "llm_rationale": llm_result.get("rationale", ""),
        "order_id":      order.get("order_id"),
        "client_order_id": order.get("client_order_id"),
        # Grade metadata
        "setup_grade":   setup_grade.grade if setup_grade else None,
        "sector_confirmation": setup_grade.sector_confirmation if setup_grade else None,
        "percentile_rank": setup_grade.percentile_rank if setup_grade else None,
        "grade_reasons": setup_grade.reasons if setup_grade else [],
        # Partial exit + runner tracking
        "partial_target_price": partial_target_price,
        "partial_exit_pct":    partial_exit_pct,
        "partial_exit_done":   False,
        "partial_exit_qty":    0.0,
        "runner_atr_mult":     runner_atr_mult,
        "runner_stop_price":   None,
        # Thesis invalidation strike counter
        "vwap_thesis_strike_count": 0,
        # Phase 1: runner trail + breakeven promotion
        "breakeven_stop_set":           False,
        "runner_trail_update_count":    0,
        "runner_trail_last_update_at":  None,
        # Phase 2: hold score
        "hold_score_latest":            None,
        "hold_score_min":               None,
        "hold_score_max":               None,
        "trim_done":                    False,
    }
    save_open_trade(ticker, _open_trades[ticker])

    log_event("TRADE", "order_submitted", {
        "ticker": ticker, "side": action,
        "size_eur": round(executed_size_eur, 2),
        "intended_size_eur": round(intended_size_eur, 2),
        "submitted_qty": round(submitted_qty, 6),
        "implied_qty": round(qty, 6),
        "bracket_floor_qty_loss_pct": bracket_floor_qty_loss_pct,
        "conviction": conviction,
        "composite": composite, "order_class": order.get("order_class"),
        "client_order_id": order.get("client_order_id"),
        "rationale": llm_result.get("rationale"),
        "sizing": sizing,
        "mean_reversion_trade": mean_reversion_trade,
        "event_risk_intraday_probe": event_risk_probe,
        "ev_decision": ev_result.get("ev_decision"),
        "exposure_direction": exposure_direction,
        "strategy_family": strategy_family,
        "playbook": setup_context.get("playbook"),
        "playbook_lifecycle": setup_context.get("playbook_lifecycle"),
        "session_window": setup_context.get("session_window"),
        "primary_factor": setup_context.get("primary_factor"),
        "data_quality_state": setup_context.get("data_quality_state"),
        "estimated_total_cost_pct": setup_context.get("estimated_total_cost_pct"),
    })


def _process_ticker(ticker, regime, weights, profile, portfolio_state, recent_trades,
                    regime_state, shock_result):
    """Compatibility wrapper for one-off ticker processing."""
    candidate = _evaluate_ticker_candidate(
        ticker, regime, weights, profile, portfolio_state, recent_trades,
        regime_state, shock_result,
    )
    if candidate:
        _execute_trade_candidate(candidate, profile, portfolio_state)


# _check_thesis_invalidation .. _check_partial_exit → moved to backend/execution/exit.py

# _check_exits → moved to backend/execution/exit.py

# _handle_hold_deadline → moved to backend/execution/exit.py

# _check_momentum_exit → moved to backend/execution/exit.py

# _recover_bracket_fill .. _cancel_bracket_orders_for_manual_exit → moved to backend/execution/exit.py

# _close_trade → moved to backend/execution/exit.py

# _current_daily_price, _stop_pct_from_atr, _submit_horizon_order
# → moved to backend/execution/orders.py


def _run_dip_buy_scan(tickers: list[str], portfolio_state: dict, macro_regime: str,
                      profile: dict, regime_state=None):
    opportunities = scan_for_extreme_dips(tickers, portfolio_state, macro_regime)
    log_event("SIGNAL", "extreme_dip_scan_complete", {
        "macro_regime": macro_regime,
        "tickers_scanned": len(tickers),
        "opportunities": len(opportunities),
    })

    open_tickers = _open_position_tickers(portfolio_state) | set(_open_trades.keys())
    for opp in opportunities:
        log_event("SIGNAL", "extreme_dip_detected", opp)
        ticker = opp["ticker"]
        if ticker in open_tickers:
            log_event("INFO", "extreme_dip_skipped_open_position", {
                "ticker": ticker,
                "dip_score": opp.get("dip_score"),
            })
            continue
        stop_pct, atr_data = _stop_pct_from_atr(
            ticker,
            multiplier=opp.get("stop_multiplier", 2.0),
            fallback=profile.get("stop_loss_pct", 2.0) * 2,
        )
        order = _submit_horizon_order(
            ticker=ticker,
            side="BUY",
            conviction=opp.get("conviction", 0.85),
            profile=profile,
            portfolio_state=portfolio_state,
            regime="news_driven" if macro_regime == "geopolitical_shock" else "ranging",
            horizon="swing",
            stop_loss_pct=stop_pct,
            hold_days=opp.get("hold_days", 3),
            size_multiplier=opp.get("size_multiplier", 1.5),
            composite_score=opp.get("dip_score", 0.0),
            signals_json={"extreme_dip": {"score": opp.get("dip_score", 0.0), "meta": opp}},
            rationale=f"{opp.get('type')} dip buy: {opp.get('pct_from_high')}% below 20d high, RSI {opp.get('rsi')}",
            macro_regime=macro_regime,
            macro_multiplier=1.0,
            dip_type=opp.get("type"),
            regime_state=regime_state,
            atr_data=atr_data,
            order_ref=_make_order_ref("dip", ticker, opp.get("type"), date.today().isoformat()),
        )
        if "error" not in order:
            open_tickers.add(ticker)


def re_evaluate_swing_positions():
    """
    Runs once per day at market open (09:35 EST / 14:35 UTC).
    Re-scores each promoted momentum swing position and decides:
    HOLD (extend), EXIT (close now), or TIGHTEN (trail stop on profit).
    """
    portfolio_state = _get_portfolio_state()
    _hydrate_open_trades(portfolio_state.get("positions", []))
    open_swings = [
        t for t, data in _open_trades.items()
        if data.get("promoted_to_swing") is True
    ]

    if not open_swings:
        log_event("INFO", "swing_reeval_no_positions", {})
        return

    log_event("INFO", "swing_reeval_start", {"positions": open_swings})

    profile = _apply_execution_overrides(
        get_effective_profile(PROFILE, portfolio_state)
    )
    weights = (_learning_engine.get_weights("trending")
               if _learning_engine else profile["signal_weights"])
    regime = detect_regime()

    for ticker in open_swings:
        try:
            pos = _open_trades[ticker]
            result = compute_all_signals(ticker, weights, regime_state=regime)
            composite = result["composite_score"]

            entry_price   = pos.get("entry_price", 0)
            current_price = _current_daily_price(ticker)
            if not current_price:
                continue
            pnl_pct = _trade_pnl_pct(pos, current_price)

            # Increment daily reeval counter
            _open_trades[ticker]["daily_reeval_count"] = int(pos.get("daily_reeval_count", 0)) + 1

            exit_reasons = []

            if regime.market_regime == "bear":
                exit_reasons.append("regime_turned_bear")

            if composite < -0.20:
                exit_reasons.append("momentum_reversed")

            earn = (result["signals"]
                    .get("earnings_proximity", {})
                    .get("meta", {}))
            days_to_earn = earn.get("days_to_earnings")
            if days_to_earn is not None and days_to_earn <= 1:
                exit_reasons.append("earnings_tomorrow")

            if result.get("shock_detected"):
                exit_reasons.append("macro_shock")

            if pnl_pct >= 8.0:
                # Hard-coded take-profit — prevents greed overriding discipline
                exit_reasons.append("take_profit_8pct")

            # Check max hold days
            entry_time = pos.get("entry_time") or datetime.utcnow()
            days_held  = (datetime.utcnow() - entry_time).days
            max_days   = pos.get("max_hold_minutes", 1950) // 390
            if days_held >= max_days:
                exit_reasons.append("time_exit")

            if exit_reasons:
                log_event("INFO", "swing_exit_triggered", {
                    "ticker":    ticker,
                    "reasons":   exit_reasons,
                    "pnl_pct":   round(pnl_pct, 3),
                    "composite": composite,
                })
                _close_trade(ticker, pos, current_price, exit_reasons[0])
                _send_discord_alert(
                    f"Swing exit: {ticker} "
                    f"P&L: {pnl_pct:+.1f}% "
                    f"Reason: {exit_reasons[0]}"
                )
                continue

            # Tighten stop if profitable — trail stop to lock in gains
            if pnl_pct > 3.0:
                old_stop_pct = float(pos.get("stop_pct", 2.5))
                new_stop_pct = old_stop_pct * 0.75
                _open_trades[ticker]["stop_pct"] = new_stop_pct
                log_event("INFO", "swing_stop_tightened", {
                    "ticker":       ticker,
                    "pnl_pct":      round(pnl_pct, 3),
                    "new_stop_pct": round(new_stop_pct, 3),
                })

            days_remaining = max(0, max_days - days_held)
            save_open_trade(ticker, _open_trades[ticker])
            log_event("INFO", "swing_hold_confirmed", {
                "ticker":          ticker,
                "composite":       composite,
                "pnl_pct":         round(pnl_pct, 3),
                "days_held":       days_held,
                "days_remaining":  days_remaining,
                "reeval_count":    _open_trades[ticker]["daily_reeval_count"],
            })

        except Exception as e:
            log_event("ERROR", "swing_reeval_error",
                      {"ticker": ticker, "error": str(e)[:80]})


def run_swing_cycle(portfolio_state: dict = None, profile: dict = None,
                    regime: str = None, macro_regime: str = None,
                    regime_state = None):
    """Daily swing re-evaluation and entry scan for SWING_TICKERS."""
    if not _allows_swing():
        return
    if not SWING_TICKERS:
        return

    portfolio_state = portfolio_state or _get_portfolio_state()
    if portfolio_state.get("broker_error"):
        log_event("ERROR", "swing_broker_account_unavailable", {
            "error": portfolio_state["broker_error"],
        })
        return

    profile = profile or _apply_execution_overrides(get_effective_profile(PROFILE, portfolio_state))
    regime_state = regime_state or detect_regime()
    regime = regime or regime_state.intraday_regime
    if macro_regime is None:
        macro_regime = detect_macro_regime()

    _hydrate_open_trades(portfolio_state.get("positions", []))
    open_tickers = _open_position_tickers(portfolio_state) | set(_open_trades.keys())
    log_event("INFO", "swing_cycle_start", {
        "tickers": SWING_TICKERS,
        "macro_regime": macro_regime,
        "open_tickers": sorted(open_tickers),
    })

    # Re-evaluate existing swing positions.
    for ticker, trade in list(_open_trades.items()):
        if trade.get("horizon") != "swing":
            continue
        current_price = _current_daily_price(ticker)
        if not current_price:
            continue
        score, meta = compute_swing_score(ticker)
        elapsed_days = max(0, (datetime.utcnow() - trade["entry_time"]).days)
        exit_reason = None
        if trade["side"] == "BUY":
            if current_price <= trade["stop_price"]:
                exit_reason = "stop_loss"
            elif trade.get("take_profit_price") and current_price >= trade["take_profit_price"]:
                exit_reason = "take_profit"
            elif score < -0.15:
                exit_reason = "signal_reversal"
        elif trade["side"] == "SELL" and score > 0.15:
            exit_reason = "signal_reversal"
        if elapsed_days >= int(trade.get("hold_days") or 3) and exit_reason is None:
            exit_reason = "time_exit"
        log_event("SIGNAL", "swing_recheck", {
            "ticker": ticker,
            "score": round(score, 4),
            "elapsed_days": elapsed_days,
            "hold_days": trade.get("hold_days"),
            "exit_reason": exit_reason,
            "meta": meta,
        })
        if exit_reason:
            _close_trade(ticker, trade, current_price, exit_reason)

    open_tickers = _open_position_tickers(portfolio_state) | set(_open_trades.keys())
    for ticker in SWING_TICKERS:
        if ticker in open_tickers:
            continue
        score, meta = compute_swing_score(ticker)
        action = _deterministic_action(score)
        if action == "SELL" and not profile.get("allow_short_selling", False):
            log_event("INFO", "swing_short_gated", {"ticker": ticker, "score": score})
            continue
        min_score = float(os.getenv("SWING_MIN_SCORE", profile.get("min_signal_score", 0.25)))
        if abs(score) < min_score:
            log_event("INFO", "swing_signal_below_threshold", {
                "ticker": ticker,
                "score": round(score, 4),
                "min_score": min_score,
            })
            continue
        stop_pct, atr_data = _stop_pct_from_atr(
            ticker,
            multiplier=2.0,
            fallback=profile.get("stop_loss_pct", 2.0) * 2,
        )
        conviction = max(0.65, min(0.90, abs(score)))
        _submit_horizon_order(
            ticker=ticker,
            side=action,
            conviction=conviction,
            profile=profile,
            portfolio_state=portfolio_state,
            regime=regime,
            horizon="swing",
            stop_loss_pct=stop_pct,
            hold_days=_env_int("SWING_HOLD_DAYS", 3),
            size_multiplier=1.0,
            composite_score=score,
            signals_json={"swing_score": {"score": score, "meta": meta}, "atr": {"score": 0, "meta": atr_data}},
            rationale="daily swing score entry",
            macro_regime=macro_regime,
            macro_multiplier=1.0,
            regime_state=regime_state,
            atr_data=atr_data,
            order_ref=_make_order_ref("swing", ticker, action, date.today().isoformat()),
        )


def _save_snapshot(portfolio_state, regime):
    from database.client import get_snapshots
    # Fetch up to 365 snapshots (ordered DESC) to derive both daily and
    # cumulative P&L from actual history rather than an arbitrary EUR→USD
    # conversion that produces nonsense numbers when paper equity >> start capital.
    snaps = get_snapshots(days=365)
    equity = portfolio_state["equity"]

    fx_rate = float(os.getenv("EURUSD_RATE", "1.08") or "1.08")
    unrealized_pnl_usd = float(portfolio_state.get("unrealized_pnl_usd") or 0)
    effective_total_eur = (equity + unrealized_pnl_usd) / fx_rate
    effective_cash_eur = float(portfolio_state.get("cash") or 0) / fx_rate

    # daily_pnl_pct: change since the previous snapshot
    prev_equity = snaps[0]["total_value_eur"] if snaps else None
    if prev_equity and prev_equity > 0:
        daily_pnl_pct = round((effective_total_eur - prev_equity) / prev_equity * 100, 3)
        daily_pnl_pct = max(-9999.0, min(9999.0, daily_pnl_pct))
    else:
        daily_pnl_pct = 0.0

    # cumulative_pnl_pct: change since the oldest available snapshot
    oldest_equity = snaps[-1]["total_value_eur"] if snaps else None
    if oldest_equity and oldest_equity > 0:
        cum_pnl = round((effective_total_eur - oldest_equity) / oldest_equity * 100, 3)
        cum_pnl = max(-9999.0, min(9999.0, cum_pnl))
    else:
        cum_pnl = 0.0

    save_snapshot({
        "total_value_eur":    round(effective_total_eur, 2),
        "cash_eur":           round(effective_cash_eur, 2),
        "daily_pnl_pct":      daily_pnl_pct,
        "cumulative_pnl_pct": cum_pnl,
        "drawdown_pct":       portfolio_state["drawdown_today"],
        "open_positions":     portfolio_state["positions"],
        "trades_today":       portfolio_state["trades_today"],
        "llm_calls_today":    _llm_calls_this_hour,
        "llm_cost_today":     round(_llm_calls_this_hour * 0.001, 4),
        "broker_equity_usd":  portfolio_state.get("broker_equity_usd"),
        "broker_cash_usd":    portfolio_state.get("broker_cash_usd"),
        "effective_equity_usd": portfolio_state.get("equity"),
        "effective_cash_usd": portfolio_state.get("cash"),
        "open_market_value_usd": portfolio_state.get("net_market_value_usd"),
        "gross_market_value_usd": portfolio_state.get("gross_market_value_usd"),
        "unrealized_pnl_usd": unrealized_pnl_usd,
        "unrealized_pnl_eur": round(unrealized_pnl_usd / fx_rate, 2),
        "fx_rate": fx_rate,
        "capital_ceiling_eur": portfolio_state.get("capital_ceiling_eur"),
        "capital_ceiling_usd": portfolio_state.get("capital_ceiling_usd"),
    })


# ── Nightly sweep (after US market close) ────────────────────────────────────

def run_nightly_sweep():
    """Runs after US market close every weekday. Simulation on alpaca_paper, live on ibkr_live."""
    try:
        try:
            from backend.learning.gate_controller import run_b_shadow_promotion_controller
            run_b_shadow_promotion_controller()
        except Exception as e:
            log_event("WARN", "b_shadow_promotion_controller_error", {"error": str(e)[:160]})

        if not _env_bool("SWEEP_ENABLED", False):
            log_event("INFO", "nightly_sweep_skipped", {"reason": "disabled"})
            return

        account = get_account()
        if "error" in account:
            log_event("ERROR", "nightly_sweep_account_error", {"error": account["error"]})
            return

        fx_rate = _env_float("EURUSD_RATE", 1.08)
        positions = get_positions()
        portfolio_state = {
            "equity_eur":      round(account.get("portfolio_value", 0) / fx_rate, 2),
            "cash_eur":        round(account.get("cash", 0) / fx_rate, 2),
            "open_positions":  len(positions),
            "pending_signals": 0,
        }

        plan   = compute_sweep_plan(portfolio_state)
        result = execute_sweep(plan)

        log_event("INFO", "nightly_sweep", result)

        if result.get("mode") == "simulation" and result.get("should_sweep"):
            _send_discord_alert(
                f"💰 Sweep simulation: Would park "
                f"€{plan['sweepable_eur']:.0f} in {plan['sweep_ticker']}. "
                f"Est. daily yield: €{plan['est_daily_yield']:.2f}"
            )
    except Exception as e:
        log_event("ERROR", "nightly_sweep_failed", {"error": str(e)[:100]})


# ── Post-market analytics (runs after market close, Mon–Fri) ─────────────────

def run_post_market_analytics():
    """
    Runs after US market close (21:05 UTC / 5:05 PM ET).
    Replays blocked opportunities and closed trade exits against post-event
    price action. Kept out of the signal cycle to avoid I/O overhead.
    """
    try:
        log_event("INFO", "post_market_analytics_start", {})
        _replay_blocked_opportunities()
        _replay_closed_trade_exits()
        log_event("INFO", "post_market_analytics_complete", {})
    except Exception as e:
        log_event("ERROR", "post_market_analytics_failed", {"error": str(e)[:160]})


def run_daily_eod_review():
    """Run read-only daily post-market synthesis and recommendations."""
    try:
        from backend.daily_review import run_daily_eod_review as _review
        return _review()
    except Exception as e:
        log_event("ERROR", "daily_eod_review_failed", {"error": str(e)[:160]})
        return {"error": str(e)}


# ── Weekly portfolio review (advisory, observation only) ─────────────────────

def run_portfolio_review():
    """
    Advisory portfolio review — observation and recommendation only.
    Scores every open position and writes hold/trim/add/exit recommendations
    to portfolio_reviews. No trades are placed.
    Called weekly (Sunday 17:00 UTC), one hour before the weekly digest.
    Execution authority is granted only after 8-10 weeks of validated recommendations.
    """
    from backend.portfolio.advisor import run_portfolio_review as _review
    try:
        result = _review()
        if result.get("skipped"):
            log_event("INFO", "portfolio_review_skipped", result)
        else:
            log_event("LEARNING", "portfolio_review_ok", {
                "positions": result.get("position_count", 0),
                "summary":   result.get("summary", {}),
                "alerts":    len(result.get("alerts", [])),
            })
        return result
    except Exception as e:
        log_event("ERROR", "portfolio_review_error", {"error": str(e)[:200]})
        return {}


# ── Weekly digest (called by scheduler) ──────────────────────────────────────

def run_weekly_digest():
    from database.client import get_recent_trades, get_daily_reviews, save_learning
    trades = get_recent_trades(days=7)
    daily_reviews = get_daily_reviews(limit=7)
    if not trades and not daily_reviews:
        return
    try:
        from backend.learning.gate_controller import run_gate_controller
        run_gate_controller(days=7, limit=500)
    except Exception as e:
        log_event("WARN", "gate_controller_error", {"error": str(e)[:160]})
    insights = generate_weekly_insights(trades, daily_reviews=daily_reviews)
    from datetime import date
    save_learning(
        week_start      = date.today(),
        insights        = insights,
        trades_analysed = len(trades)
    )
    log_event("LEARNING", "weekly_digest", {"insights": len(insights)})
    return insights


# ── Scheduler entry point ─────────────────────────────────────────────────────

def start_scheduler():
    from apscheduler.schedulers.blocking import BlockingScheduler

    scheduler = BlockingScheduler(timezone=NY_TZ or timezone.utc)

    # Main signal cycle: every 5 minutes during regular US market hours.
    # Scheduling in New York time keeps the window correct across DST changes.
    if NY_TZ is not None:
        scheduler.add_job(run_signal_cycle, "cron",
                          day_of_week="mon-fri",
                          hour=9,
                          minute="30-59/5")
        scheduler.add_job(run_signal_cycle, "cron",
                          day_of_week="mon-fri",
                          hour="10-15",
                          minute="*/5")
    else:
        scheduler.add_job(_run_signal_cycle_if_market_open, "cron",
                          day_of_week="mon-fri",
                          hour="13-21",
                          minute="*/5")

    # Weekly portfolio review: Sunday 17:00 UTC (one hour before digest)
    scheduler.add_job(run_portfolio_review, "cron",
                      day_of_week="sun", hour=17, minute=0,
                      timezone=timezone.utc)

    # Weekly digest: Sunday 18:00 UTC
    scheduler.add_job(run_weekly_digest, "cron",
                      day_of_week="sun", hour=18, minute=0,
                      timezone=timezone.utc)

    # Nightly cash sweep: after US market close Mon-Fri
    scheduler.add_job(run_nightly_sweep, "cron",
                      day_of_week="mon-fri", hour=16, minute=5)

    log_event("INFO", "scheduler_started", {"tickers": TICKERS, "profile": PROFILE.get("_name")})
    print(f"Agent started | Profile: {PROFILE['display_name']} | Tickers: {TICKERS}")
    scheduler.start()


if __name__ == "__main__":
    mode = os.getenv("AGENT_MODE", "scheduler")
    if mode == "sweep":
        run_nightly_sweep()
    elif mode == "digest":
        run_weekly_digest()
    elif mode == "portfolio_review":
        run_portfolio_review()
    elif mode == "signal":
        run_signal_cycle()
    elif mode == "swing_reeval":
        re_evaluate_swing_positions()
    else:
        start_scheduler()
