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
                                          get_recent_advisory_signals,
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
from backend.runtime.overrides   import _apply_execution_overrides
from backend.runtime.lifecycle   import (run_nightly_sweep, run_post_market_analytics,
                                          run_daily_eod_review, run_portfolio_review,
                                          run_weekly_digest)
from backend.runtime.helpers     import (_get_cached_signals, _init_learning_engine,
                                          _get_portfolio_state, _count_trades_today,
                                          _count_consecutive, _missing_runtime_config)
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
                                          _replay_advisory_signals,
                                          _replay_blocked_opportunities,
                                          _closed_trade_replay_exit_reasons,
                                          _replay_one_closed_trade_exit,
                                          _replay_closed_trade_exits)
from backend.execution.orders    import (_current_daily_price, _stop_pct_from_atr,
                                          _submit_horizon_order)
from backend.execution.swing     import (_try_promote_to_swing, _run_dip_buy_scan,
                                          re_evaluate_swing_positions, run_swing_cycle)
from backend.execution.entry     import (_evaluate_ticker_candidate, _execute_trade_candidate,
                                          _process_ticker)
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



# _get_cached_signals → moved to backend/runtime/helpers.py


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
_day_trade_log: list = []   # [(date, ticker)] same-day round trips (telemetry only)
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


# _init_learning_engine → moved to backend/runtime/helpers.py

# _get_portfolio_state → moved to backend/runtime/helpers.py

# _count_trades_today → moved to backend/runtime/helpers.py

# _count_consecutive → moved to backend/runtime/helpers.py


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



# _missing_runtime_config → moved to backend/runtime/helpers.py




# _apply_execution_overrides → moved to backend/runtime/overrides.py


# _apply_learned_hold_extension .. _count_day_trades_5d → moved to backend/execution/exit.py

# _try_promote_to_swing → moved to backend/execution/swing.py
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
    recent_live_advisories = get_recent_advisory_signals(days=1, mode="live", market="US")
    _hydrate_open_trades(portfolio_state.get("positions", []))
    try:
        _replay_advisory_signals()
    except Exception as e:
        log_event("WARN", "advisory_replay_cycle_failed", {"error": str(e)[:160]})
    if os.getenv("ADVISORY_AUTO_RUN", "").strip().lower() in {"1", "true", "yes", "on"}:
        try:
            from backend.advisory_auto.executor import run_advisory_auto_cycle
            run_advisory_auto_cycle()
        except Exception as e:
            log_event("WARN", "advisory_auto_cycle_failed", {"error": str(e)[:160]})

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
                    recent_live_advisories,
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



# _evaluate_ticker_candidate → moved to backend/execution/entry.py

# _execute_trade_candidate → moved to backend/execution/entry.py

# _process_ticker → moved to backend/execution/entry.py


# _check_thesis_invalidation .. _check_partial_exit → moved to backend/execution/exit.py

# _check_exits → moved to backend/execution/exit.py

# _handle_hold_deadline → moved to backend/execution/exit.py

# _check_momentum_exit → moved to backend/execution/exit.py

# _recover_bracket_fill .. _cancel_bracket_orders_for_manual_exit → moved to backend/execution/exit.py

# _close_trade → moved to backend/execution/exit.py

# _current_daily_price, _stop_pct_from_atr, _submit_horizon_order
# → moved to backend/execution/orders.py


# _run_dip_buy_scan, re_evaluate_swing_positions, run_swing_cycle → moved to backend/execution/swing.py
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


# run_nightly_sweep → moved to backend/runtime/lifecycle.py

# run_post_market_analytics → moved to backend/runtime/lifecycle.py

# run_daily_eod_review → moved to backend/runtime/lifecycle.py

# run_portfolio_review → moved to backend/runtime/lifecycle.py

# run_weekly_digest → moved to backend/runtime/lifecycle.py

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
