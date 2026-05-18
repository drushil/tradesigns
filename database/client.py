"""
database/client.py
Supabase client wrapper. All DB operations go through here.
Reads credentials from os.environ (which app.py populates from st.secrets).
"""
import os
from datetime import date, datetime, timedelta
from typing import Optional
from supabase import create_client, Client


def _get_env(key: str) -> Optional[str]:
    """Read from os.environ (works locally via .env and on Streamlit Cloud via st.secrets bridge)."""
    return os.environ.get(key)


def get_client(write: bool = False) -> Client:
    """
    write=False → anon key  (dashboard reads — respects RLS)
    write=True  → service_role key (agent writes — bypasses RLS)
    """
    url = _get_env("SUPABASE_URL")
    if not url:
        raise ValueError("SUPABASE_URL not set — add it to Streamlit secrets or .env")
    if write:
        key = _get_env("SUPABASE_SERVICE_KEY")
        if not key:
            raise ValueError(
                "SUPABASE_SERVICE_KEY not set — add it to Streamlit secrets or .env. "
                "The service role key is required for write operations."
            )
    else:
        key = _get_env("SUPABASE_ANON_KEY")
        if not key:
            raise ValueError("SUPABASE_ANON_KEY not set — add it to Streamlit secrets or .env")
    return create_client(url, key)


# ── Trades ────────────────────────────────────────────────────────────────────

def insert_trade(trade: dict) -> dict:
    db = get_client(write=True)
    try:
        result = db.table("trades").insert(trade).execute()
        return result.data[0] if result.data else {}
    except Exception as e:
        base_columns = {
            "ticker", "side", "entry_price", "exit_price", "quantity",
            "stop_price", "take_profit_price", "order_id", "close_order_id",
            "close_error",
            "size_eur", "size_usd", "pnl_pct", "net_pnl_pct", "pnl_eur",
            "entry_time", "exit_time", "hold_minutes", "exit_reason",
            "regime", "macro_regime", "macro_multiplier", "dip_type",
            "sizing_json", "mean_reversion_trade", "swing_trade",
            "composite_score", "llm_conviction", "llm_rationale",
            "signals_json", "commission_eur", "slippage_eur", "llm_cost_eur",
            "risk_profile", "horizon",
            "atr_at_entry", "r_multiple", "stop_pct_used",
            "hold_decision_json", "hold_extension_count",
            "setup_grade", "partial_exit_done", "entry_tranche_count",
        }
        fallback = {k: v for k, v in trade.items() if k in base_columns}
        try:
            result = db.table("trades").insert(fallback).execute()
            return result.data[0] if result.data else {}
        except Exception:
            return {"error": str(e)}


def save_open_trade(ticker: str, trade: dict) -> dict:
    """Best-effort persistence for scheduled runs; bracket orders remain source of truth."""
    try:
        db = get_client(write=True)
        record = {
            "ticker": ticker,
            "side": trade.get("side"),
            "entry_time": trade.get("entry_time").isoformat() if trade.get("entry_time") else None,
            "entry_price": trade.get("entry_price"),
            "quantity": trade.get("quantity"),
            "submitted_qty": trade.get("submitted_qty"),
            "implied_qty": trade.get("implied_qty"),
            "stop_price": trade.get("stop_price"),
            "take_profit_price": trade.get("take_profit_price"),
            "hold_minutes": trade.get("hold_minutes"),
            "hold_days": trade.get("hold_days"),
            "horizon": trade.get("horizon"),
            "size_eur": trade.get("size_eur"),
            "size_usd": trade.get("size_usd"),
            "intended_size_eur": trade.get("intended_size_eur"),
            "executed_size_eur": trade.get("executed_size_eur"),
            "executed_size_usd": trade.get("executed_size_usd"),
            "bracket_floor_qty_loss_pct": trade.get("bracket_floor_qty_loss_pct"),
            "atr_pct": trade.get("atr_pct"),
            "atr_raw": trade.get("atr_raw"),
            "order_id": trade.get("order_id"),
            "regime": trade.get("regime"),
            "macro_regime": trade.get("macro_regime"),
            "macro_multiplier": trade.get("macro_multiplier"),
            "dip_type": trade.get("dip_type"),
            "sizing_json": trade.get("sizing_json"),
            "mean_reversion_trade": trade.get("mean_reversion_trade"),
            "swing_trade": trade.get("swing_trade"),
            "promoted_to_swing": trade.get("promoted_to_swing"),
            "promoted_at": trade.get("promoted_at"),
            "initial_horizon": trade.get("initial_horizon"),
            "swing_conviction": trade.get("swing_conviction"),
            "swing_reasons": trade.get("swing_reasons"),
            "highest_price_since_entry": trade.get("highest_price_since_entry"),
            "trailing_stop_price": trade.get("trailing_stop_price"),
            "stop_multiplier": trade.get("stop_multiplier"),
            "stop_pct": trade.get("stop_pct"),
            "max_hold_minutes": trade.get("max_hold_minutes"),
            "daily_reeval_count": trade.get("daily_reeval_count"),
            "hold_extension_count": trade.get("hold_extension_count"),
            "hold_decision_json": trade.get("hold_decision_json"),
            "peak_directional_score": trade.get("peak_directional_score"),
            "protective_stop_order_id": trade.get("protective_stop_order_id"),
            "composite_score": trade.get("composite_score"),
            "llm_conviction": trade.get("llm_conviction"),
            "llm_rationale": trade.get("llm_rationale"),
            "signals_json": trade.get("signals_json", {}),
            "exposure_direction": trade.get("exposure_direction"),
            "strategy_family": trade.get("strategy_family"),
            "regime_debug_json": trade.get("regime_debug_json"),
            "setup_grade": trade.get("setup_grade"),
            "sector_confirmation": trade.get("sector_confirmation"),
            "percentile_rank": trade.get("percentile_rank"),
            "grade_reasons": trade.get("grade_reasons"),
            "partial_target_price": trade.get("partial_target_price"),
            "partial_exit_pct": trade.get("partial_exit_pct"),
            "partial_exit_done": trade.get("partial_exit_done"),
            "partial_exit_qty": trade.get("partial_exit_qty"),
            "runner_atr_mult": trade.get("runner_atr_mult"),
            "runner_stop_price": trade.get("runner_stop_price"),
            "vwap_thesis_strike_count": trade.get("vwap_thesis_strike_count"),
            "status": "open",
            "closed_at": None,
            "close_reason": None,
        }
        try:
            result = db.table("open_trades").upsert(record, on_conflict="ticker").execute()
        except Exception:
            # Fallback strips only columns added in later migrations that may not
            # exist on older deployments. Critical state fields (partial_exit_done,
            # swing_trade, hold_extension_count, etc.) are intentionally kept —
            # losing them causes incorrect exit decisions on the next cold-start.
            fallback = {
                k: v for k, v in record.items()
                if k not in {"implied_qty", "bracket_floor_qty_loss_pct",
                             "intended_size_eur", "executed_size_eur", "executed_size_usd",
                             "sizing_json", "regime_debug_json", "percentile_rank",
                             "grade_reasons", "runner_atr_mult", "vwap_thesis_strike_count",
                             "atr_pct", "atr_raw"}
            }
            result = db.table("open_trades").upsert(fallback, on_conflict="ticker").execute()
        return result.data[0] if result.data else {}
    except Exception as e:
        print(f"[OPEN_TRADE_WRITE_FAILED] {str(e)[:200]}")
        return {"error": str(e)}


def get_open_trade_records() -> list:
    try:
        db = get_client()
        result = (db.table("open_trades")
                  .select("*")
                  .eq("status", "open")
                  .execute())
        return result.data or []
    except Exception as e:
        print(f"[OPEN_TRADE_READ_FAILED] {str(e)[:200]}")
        return []


def close_open_trade_record(ticker: str, reason: str = None):
    try:
        db = get_client(write=True)
        db.table("open_trades").update({
            "status": "closed",
            "closed_at": datetime.utcnow().isoformat(),
            "close_reason": reason,
        }).eq("ticker", ticker).eq("status", "open").execute()
    except Exception as e:
        print(f"[OPEN_TRADE_CLOSE_FAILED] {str(e)[:200]}")


def get_recent_trades(days: int = 30, ticker: str = None) -> list:
    db = get_client()
    q = db.table("trades").select("*").order("created_at", desc=True)
    if days:
        cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat() + "Z"
        q = q.gte("created_at", cutoff)
    if ticker:
        q = q.eq("ticker", ticker)
    result = q.limit(500).execute()
    return result.data or []


def get_unchecked_closed_trades_for_replay(min_age_minutes: int = 20, limit: int = 50,
                                           exit_reasons: list[str] = None,
                                           max_age_days: int = 4) -> list:
    """Fetch closed trades eligible for post-exit replay.

    Only fetches trades within the yfinance 1m data window (max_age_days, default 4).
    Oldest-first ordering within that window ensures newest trades are never starved.
    """
    try:
        now = datetime.utcnow()
        recent_cutoff = (now - timedelta(minutes=min_age_minutes)).isoformat() + "Z"
        oldest_cutoff = (now - timedelta(days=max_age_days)).isoformat() + "Z"
        db = get_client()
        q = (db.table("trades")
             .select("id,ticker,side,exit_price,exit_reason,exit_time,created_at,net_pnl_pct,setup_grade")
             .is_("post_exit_checked_at", "null")
             .gte("created_at", oldest_cutoff)
             .lte("created_at", recent_cutoff)
             .order("created_at", desc=False)
             .limit(limit))
        if exit_reasons:
            q = q.in_("exit_reason", exit_reasons)
        result = q.execute()
        return result.data or []
    except Exception as e:
        print(f"[CLOSED_TRADE_REPLAY_READ_FAILED] {str(e)[:200]}")
        return []


def update_trade_post_exit_replay(trade_id: int, replay: dict) -> dict:
    """Persist post-exit replay stats for a closed trade."""
    try:
        db = get_client(write=True)
        record = {
            "post_exit_checked_at": datetime.utcnow().isoformat() + "Z",
            "post_exit_horizon_minutes": replay.get("post_exit_horizon_minutes"),
            "post_exit_max_favorable_pct": replay.get("post_exit_max_favorable_pct"),
            "post_exit_max_adverse_pct": replay.get("post_exit_max_adverse_pct"),
            "post_exit_close_after_pct": replay.get("post_exit_close_after_pct"),
            "post_exit_result_json": replay.get("post_exit_result_json") or {},
        }
        result = (db.table("trades")
                  .update(record)
                  .eq("id", trade_id)
                  .execute())
        return result.data[0] if result.data else {}
    except Exception as e:
        print(f"[CLOSED_TRADE_REPLAY_UPDATE_FAILED] {str(e)[:200]}")
        return {"error": str(e)}


def get_trade_stats(days: int = 30) -> dict:
    """Try the pre-computed view first, fall back to Python aggregation."""
    try:
        db = get_client()
        result = db.table("trade_stats_30d").select("*").execute()
        if result.data:
            r = result.data[0]
            return {
                "total":             r.get("total") or r.get("total_trades") or 0,
                "wins":              r.get("wins") or 0,
                "losses":            r.get("losses") or 0,
                "win_rate":          r.get("win_rate") or r.get("win_rate_pct") or 0,
                "avg_pnl":           r.get("avg_pnl") or r.get("avg_net_pnl_pct") or 0,
                "total_pnl_eur":     r.get("total_pnl_eur") or 0,
                "avg_hold_minutes":  r.get("avg_hold_minutes") or 0,
            }
    except Exception:
        pass
    trades = get_recent_trades(days)
    if not trades:
        return {"total": 0, "wins": 0, "losses": 0, "win_rate": 0,
                "avg_pnl": 0, "total_pnl_eur": 0}
    wins   = [t for t in trades if (t.get("net_pnl_pct") or 0) > 0]
    losses = [t for t in trades if (t.get("net_pnl_pct") or 0) <= 0]
    return {
        "total":          len(trades),
        "wins":           len(wins),
        "losses":         len(losses),
        "win_rate":       round(len(wins) / len(trades) * 100, 1),
        "avg_pnl":        round(sum(t.get("net_pnl_pct", 0) for t in trades) / len(trades), 3),
        "total_pnl_eur":  round(sum(t.get("pnl_eur", 0) for t in trades), 2),
        "avg_hold_minutes": round(sum(t.get("hold_minutes", 0) for t in trades) / len(trades), 1),
    }


# ── Signals ───────────────────────────────────────────────────────────────────

def insert_signal(signal: dict) -> dict:
    try:
        db = get_client(write=True)
        result = db.table("signals").insert(signal).execute()
        return result.data[0] if result.data else {}
    except Exception as e:
        base_columns = {
            "ticker", "composite_score", "order_book_score",
            "tape_aggression_score", "rsi_divergence_score",
            "news_sentiment_score", "vwap_deviation_score", "regime",
            "vix", "volume_vs_avg", "gated", "gate_reason",
            "llm_called", "llm_action", "llm_conviction",
            # Phase 1 additions
            "macd_score", "rel_strength_score", "earnings_days", "earnings_mult",
            "bollinger_score", "put_call_score", "atr_pct",
            "atr_stop_pct", "volatility_regime",
            "macro_regime", "macro_multiplier",
            "market_regime", "regime_bull_bear", "shock_detected", "shock_classification",
            "yield_curve", "yield_curve_state",
            "action_hint", "exposure_direction", "strategy_family", "regime_debug_json",
            "setup_grade", "sector_confirmation", "orb_score", "percentile_rank",
        }
        fallback = {k: v for k, v in signal.items() if k in base_columns}
        try:
            result = db.table("signals").insert(fallback).execute()
            return result.data[0] if result.data else {}
        except Exception:
            return {"error": str(e)}


def update_signal(signal_id: int, updates: dict) -> dict:
    """Best-effort update for signal metadata computed after initial insert."""
    try:
        db = get_client(write=True)
        result = db.table("signals").update(updates).eq("id", signal_id).execute()
        return result.data[0] if result.data else {}
    except Exception as e:
        return {"error": str(e)}


def insert_blocked_opportunity(opportunity: dict) -> dict:
    """Best-effort record for replaying blocked/skipped trade opportunities."""
    try:
        db = get_client(write=True)
        record = {
            "ticker": opportunity.get("ticker"),
            "action_hint": opportunity.get("action_hint"),
            "composite_score": opportunity.get("composite_score"),
            "block_stage": opportunity.get("block_stage"),
            "block_reason": opportunity.get("block_reason"),
            "candidate_rank_score": opportunity.get("candidate_rank_score"),
            "breakout_quality": opportunity.get("breakout_quality"),
            "ev_decision": opportunity.get("ev_decision"),
            "ev_net_pct": opportunity.get("ev_net_pct"),
            "ev_result_json": opportunity.get("ev_result_json"),
            "signals_json": opportunity.get("signals_json"),
            "setup_context_json": opportunity.get("setup_context_json"),
            "regime": opportunity.get("regime"),
            "market_regime": opportunity.get("market_regime"),
            "strategy_family": opportunity.get("strategy_family"),
            "event_risk_active": opportunity.get("event_risk_active"),
            "reference_price": opportunity.get("reference_price"),
            "setup_grade": opportunity.get("setup_grade"),
            "a_plus_blocked": opportunity.get("a_plus_blocked"),
            "minutes_since_open": opportunity.get("minutes_since_open"),
            "atr_pct": opportunity.get("atr_pct"),
            "volatility_bucket": opportunity.get("volatility_bucket"),
            "is_leveraged_etf": opportunity.get("is_leveraged_etf"),
            "spread_pct": opportunity.get("spread_pct"),
            "opening_range_position": opportunity.get("opening_range_position"),
            "probe_eligible": opportunity.get("probe_eligible"),
            "reason_not_probed": opportunity.get("reason_not_probed"),
        }
        result = db.table("blocked_opportunities").insert(record).execute()
        return result.data[0] if result.data else {}
    except Exception as e:
        base_columns = {
            "ticker", "action_hint", "composite_score", "block_stage", "block_reason",
            "candidate_rank_score", "breakout_quality", "ev_decision", "ev_net_pct",
            "ev_result_json", "signals_json", "setup_context_json", "regime",
            "market_regime", "strategy_family", "event_risk_active", "reference_price",
            "setup_grade", "a_plus_blocked",
        }
        fallback = {k: v for k, v in record.items() if k in base_columns}
        try:
            result = db.table("blocked_opportunities").insert(fallback).execute()
            return result.data[0] if result.data else {}
        except Exception:
            return {"error": str(e)}


def get_unchecked_blocked_opportunities(min_age_minutes: int = 20, limit: int = 50,
                                        max_age_days: int = 4) -> list:
    """Fetch blocked opportunities eligible for replay against subsequent price action.

    Only fetches rows within the yfinance 1m data window (max_age_days, default 4).
    """
    try:
        now = datetime.utcnow()
        recent_cutoff = (now - timedelta(minutes=min_age_minutes)).isoformat() + "Z"
        oldest_cutoff = (now - timedelta(days=max_age_days)).isoformat() + "Z"
        db = get_client()
        result = (db.table("blocked_opportunities")
                  .select("*")
                  .is_("replay_checked_at", "null")
                  .gte("created_at", oldest_cutoff)
                  .lte("created_at", recent_cutoff)
                  .order("created_at", desc=False)
                  .limit(limit)
                  .execute())
        return result.data or []
    except Exception as e:
        print(f"[BLOCKED_OPPORTUNITY_READ_FAILED] {str(e)[:200]}")
        return []


def update_blocked_opportunity_replay(opportunity_id: int, replay: dict) -> dict:
    """Persist replay stats for a blocked opportunity."""
    try:
        db = get_client(write=True)
        record = {
            "replay_checked_at": datetime.utcnow().isoformat() + "Z",
            "max_favorable_pct": replay.get("max_favorable_pct"),
            "max_adverse_pct": replay.get("max_adverse_pct"),
            "close_after_pct": replay.get("close_after_pct"),
            "replay_result_json": replay.get("replay_result_json") or {},
        }
        result = (db.table("blocked_opportunities")
                  .update(record)
                  .eq("id", opportunity_id)
                  .execute())
        return result.data[0] if result.data else {}
    except Exception as e:
        print(f"[BLOCKED_OPPORTUNITY_UPDATE_FAILED] {str(e)[:200]}")
        return {"error": str(e)}


def get_blocked_opportunities(days: int = 7, limit: int = 500) -> list:
    """Recent blocked/skipped opportunities, including replay metrics when available."""
    try:
        cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat() + "Z"
        db = get_client()
        result = (db.table("blocked_opportunities")
                  .select("*")
                  .gte("created_at", cutoff)
                  .order("created_at", desc=True)
                  .limit(limit)
                  .execute())
        return result.data or []
    except Exception as e:
        print(f"[BLOCKED_OPPORTUNITY_STATS_FAILED] {str(e)[:200]}")
        return []


# ── Advisory signals ─────────────────────────────────────────────────────────

def insert_advisory_signal(signal: dict) -> dict:
    """Persist a manual-trading advisory signal, separate from broker trades."""
    try:
        db = get_client(write=True)
        record = {
            "market": signal.get("market"),
            "mode": signal.get("mode"),
            "status": signal.get("status"),
            "data_symbol": signal.get("data_symbol"),
            "broker_display_name": signal.get("broker_display_name"),
            "exchange": signal.get("exchange"),
            "currency": signal.get("currency"),
            "side": signal.get("side"),
            "grade": signal.get("grade"),
            "composite_score": signal.get("composite_score"),
            "ev_net_pct": signal.get("ev_net_pct"),
            "breakout_quality": signal.get("breakout_quality"),
            "confidence": signal.get("confidence"),
            "entry_min": signal.get("entry_min"),
            "entry_max": signal.get("entry_max"),
            "do_not_chase_price": signal.get("do_not_chase_price"),
            "stop_price": signal.get("stop_price"),
            "target_1": signal.get("target_1"),
            "target_2": signal.get("target_2"),
            "suggested_size_eur": signal.get("suggested_size_eur"),
            "risk_eur": signal.get("risk_eur"),
            "risk_pct": signal.get("risk_pct"),
            "reward_risk": signal.get("reward_risk"),
            "valid_until": signal.get("valid_until"),
            "time_exit_at": signal.get("time_exit_at"),
            "rationale": signal.get("rationale"),
            "signal_json": signal.get("signal_json") or {},
            "market_context_json": signal.get("market_context_json") or {},
            "data_quality_json": signal.get("data_quality_json") or {},
            "message_text": signal.get("message_text"),
            "fx_rate": signal.get("fx_rate"),
        }
        result = db.table("advisory_signals").insert(record).execute()
        return result.data[0] if result.data else {}
    except Exception as e:
        print(f"[ADVISORY_SIGNAL_WRITE_FAILED] {str(e)[:200]}")
        return {"error": str(e)}


def get_recent_advisory_signals(days: int = 1, mode: str = None,
                                market: str = None, limit: int = 200) -> list:
    try:
        cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat() + "Z"
        db = get_client()
        q = (db.table("advisory_signals")
             .select("*")
             .gte("created_at", cutoff)
             .order("created_at", desc=True)
             .limit(limit))
        if mode:
            q = q.eq("mode", mode)
        if market:
            q = q.eq("market", market)
        result = q.execute()
        return result.data or []
    except Exception as e:
        print(f"[ADVISORY_SIGNAL_READ_FAILED] {str(e)[:200]}")
        return []


# ── Daily EOD reviews ────────────────────────────────────────────────────────

def save_daily_review(review: dict) -> dict:
    """Persist one post-market review per trading date."""
    try:
        db = get_client(write=True)
        record = {
            "review_date": review.get("review_date"),
            "status": review.get("status", "pending"),
            "summary": review.get("summary"),
            "confidence": review.get("confidence"),
            "metrics_json": review.get("metrics_json") or {},
            "review_json": review.get("review_json") or {},
            "recommendations_json": review.get("recommendations_json") or [],
            "discord_message": review.get("discord_message"),
            "model": review.get("model"),
            "error": review.get("error"),
        }
        result = db.table("daily_reviews").upsert(record, on_conflict="review_date").execute()
        return result.data[0] if result.data else {}
    except Exception as e:
        print(f"[DAILY_REVIEW_WRITE_FAILED] {str(e)[:200]}")
        return {"error": str(e)}


def insert_config_change_recommendations(review_id: int, review_date: str,
                                         recommendations: list[dict]) -> list[dict]:
    """Persist human-approval config recommendations generated by the EOD review."""
    if not recommendations:
        return []
    try:
        db = get_client(write=True)
        records = []
        for rec in recommendations:
            records.append({
                "review_date": review_date,
                "daily_review_id": review_id,
                "category": rec.get("category", "parameter"),
                "variable": rec.get("variable"),
                "current_value": str(rec.get("current_value")) if rec.get("current_value") is not None else None,
                "suggested_value": str(rec.get("suggested_value")) if rec.get("suggested_value") is not None else None,
                "command_text": rec.get("command_text"),
                "reason": rec.get("reason"),
                "evidence": rec.get("evidence") or {},
                "confidence": rec.get("confidence"),
                "evidence_days": int(rec.get("evidence_days") or 1),
                "expected_effect": rec.get("expected_effect"),
                "success_metric": rec.get("success_metric"),
                "rollback_condition": rec.get("rollback_condition"),
                "autonomy_level": rec.get("autonomy_level", "human_approval"),
                "status": rec.get("status", "pending"),
            })
        result = db.table("config_change_recommendations").insert(records).execute()
        return result.data or []
    except Exception as e:
        print(f"[CONFIG_RECOMMENDATION_WRITE_FAILED] {str(e)[:200]}")
        return [{"error": str(e)}]


def get_daily_reviews(limit: int = 20) -> list:
    try:
        db = get_client()
        result = (db.table("daily_reviews")
                  .select("*")
                  .order("review_date", desc=True)
                  .limit(limit)
                  .execute())
        return result.data or []
    except Exception as e:
        print(f"[DAILY_REVIEW_READ_FAILED] {str(e)[:200]}")
        return []


def get_recent_signals(hours: int = 24) -> list:
    db = get_client()
    result = (db.table("signals")
               .select("*")
               .order("created_at", desc=True)
               .limit(200)
               .execute())
    return result.data or []


# ── News sentiment cache ──────────────────────────────────────────────────────

def get_news_cache(ticker: str, max_age_minutes: int = 15) -> Optional[dict]:
    """
    Persistent cache for news-derived sentiment.
    Scheduled GitHub Action runs do not share process memory, so this preserves
    the intended freshness window without repeatedly spending NewsAPI quota.
    """
    try:
        cutoff = (datetime.utcnow() - timedelta(minutes=max_age_minutes)).isoformat() + "Z"
        db = get_client()
        result = (db.table("news_cache")
                   .select("*")
                   .eq("ticker", ticker.upper())
                   .gte("fetched_at", cutoff)
                   .order("fetched_at", desc=True)
                   .limit(1)
                   .execute())
        return result.data[0] if result.data else None
    except Exception as e:
        print(f"[NEWS_CACHE_READ_FAILED] {str(e)[:200]}")
        return None


def upsert_news_cache(ticker: str, score: float, meta: dict, headlines: list) -> dict:
    try:
        db = get_client(write=True)
        record = {
            "ticker": ticker.upper(),
            "sentiment_score": round(float(score), 4),
            "meta_json": meta or {},
            "headlines_json": headlines or [],
            "fetched_at": datetime.utcnow().isoformat() + "Z",
        }
        result = db.table("news_cache").upsert(record, on_conflict="ticker").execute()
        return result.data[0] if result.data else {}
    except Exception as e:
        print(f"[NEWS_CACHE_WRITE_FAILED] {str(e)[:200]}")
        return {"error": str(e)}


def get_newsapi_daily_usage() -> int:
    """Return total NewsAPI calls made today across all tickers."""
    try:
        today = datetime.utcnow().date().isoformat()
        db = get_client()
        result = (db.table("newsapi_usage")
                   .select("calls")
                   .eq("usage_date", today)
                   .execute())
        return sum(int(r.get("calls") or 0) for r in (result.data or []))
    except Exception:
        return 0


def record_newsapi_usage(ticker: str, calls: int = 1):
    """Best-effort daily usage ledger for NewsAPI quota visibility."""
    if calls <= 0:
        return
    try:
        today = datetime.utcnow().date().isoformat()
        db = get_client(write=True)
        existing = (db.table("newsapi_usage")
                     .select("calls")
                     .eq("usage_date", today)
                     .eq("ticker", ticker.upper())
                     .limit(1)
                     .execute())
        total_calls = calls
        if existing.data:
            total_calls += int(existing.data[0].get("calls") or 0)
        db.table("newsapi_usage").upsert({
            "usage_date": today,
            "ticker": ticker.upper(),
            "calls": total_calls,
            "updated_at": datetime.utcnow().isoformat() + "Z",
        }, on_conflict="usage_date,ticker").execute()
    except Exception as e:
        print(f"[NEWSAPI_USAGE_WRITE_FAILED] {str(e)[:200]}")


# ── Ticker profile cache ──────────────────────────────────────────────────────

def get_ticker_profile_cache(ticker: str) -> Optional[dict]:
    try:
        db = get_client()
        result = (db.table("ticker_profiles")
                   .select("profile_json")
                   .eq("ticker", ticker.upper())
                   .limit(1)
                   .execute())
        if result.data:
            return result.data[0].get("profile_json") or None
    except Exception as e:
        print(f"[TICKER_PROFILE_READ_FAILED] {str(e)[:200]}")
    return None


def upsert_ticker_profile_cache(ticker: str, profile: dict) -> dict:
    try:
        db = get_client(write=True)
        result = db.table("ticker_profiles").upsert({
            "ticker": ticker.upper(),
            "profile_json": profile or {},
            "updated_at": datetime.utcnow().isoformat() + "Z",
        }, on_conflict="ticker").execute()
        return result.data[0] if result.data else {}
    except Exception as e:
        print(f"[TICKER_PROFILE_WRITE_FAILED] {str(e)[:200]}")
        return {"error": str(e)}


# ── Signal weights ────────────────────────────────────────────────────────────

def save_signal_weights(regime: str, weights: dict, trade_count: int, trigger: str):
    db = get_client(write=True)
    record = {
        "regime":            regime,
        "order_book":        round(weights.get("order_book_imbalance", 0.25), 4),
        "tape_aggression":   round(weights.get("tape_aggression", 0.25), 4),
        "rsi_divergence":    round(weights.get("rsi_divergence", 0.20), 4),
        "news_sentiment":    round(weights.get("news_sentiment", 0.20), 4),
        "vwap_deviation":    round(weights.get("vwap_deviation", 0.10), 4),
        "macd_crossover":    round(weights.get("macd_crossover", 0.10), 4),
        "relative_strength": round(weights.get("relative_strength", 0.08), 4),
        "bollinger_squeeze": round(weights.get("bollinger_squeeze", 0.09), 4),
        "put_call_ratio":    round(weights.get("put_call_ratio", 0.05), 4),
        "trade_count":       trade_count,
        "trigger":           trigger,
    }
    db.table("signal_weights").insert(record).execute()


def get_latest_weights(regime: str = "global") -> Optional[dict]:
    try:
        db = get_client()
        result = (db.table("latest_signal_weights")
                   .select("*")
                   .eq("regime", regime)
                   .limit(1)
                   .execute())
        if result.data:
            r = result.data[0]
            return {
                "order_book_imbalance": r["order_book"],
                "tape_aggression":      r["tape_aggression"],
                "rsi_divergence":       r["rsi_divergence"],
                "news_sentiment":       r["news_sentiment"],
                "vwap_deviation":       r["vwap_deviation"],
                "macd_crossover":       r.get("macd_crossover", 0.10),
                "relative_strength":    r.get("relative_strength", 0.08),
                "bollinger_squeeze":    r.get("bollinger_squeeze", 0.09),
                "put_call_ratio":       r.get("put_call_ratio", 0.05),
            }
    except Exception:
        pass
    return None


def get_weight_history(regime: str = "global", limit: int = 50) -> list:
    db = get_client()
    result = (db.table("signal_weights")
               .select("*")
               .eq("regime", regime)
               .order("updated_at", desc=True)
               .limit(limit)
               .execute())
    return result.data or []


# ── Learnings ─────────────────────────────────────────────────────────────────

def save_learning(week_start: date, insights: list, trades_analysed: int):
    db = get_client(write=True)
    db.table("learnings").insert({
        "week_start":      week_start.isoformat(),
        "insights_json":   insights,
        "trades_analysed": trades_analysed,
        "applied":         False,
    }).execute()


def get_learnings(limit: int = 10) -> list:
    db = get_client()
    result = (db.table("learnings")
               .select("*")
               .order("created_at", desc=True)
               .limit(limit)
               .execute())
    return result.data or []


# ── Portfolio snapshots ───────────────────────────────────────────────────────

def save_snapshot(snapshot: dict):
    try:
        db = get_client(write=True)
        try:
            db.table("portfolio_snapshots").insert(snapshot).execute()
        except Exception:
            base_columns = {
                "total_value_eur", "cash_eur", "daily_pnl_pct",
                "cumulative_pnl_pct", "drawdown_pct", "open_positions",
                "trades_today", "llm_calls_today", "llm_cost_today",
            }
            fallback = {k: v for k, v in snapshot.items() if k in base_columns}
            db.table("portfolio_snapshots").insert(fallback).execute()
    except Exception as e:
        print(f"[SNAPSHOT_WRITE_FAILED] {str(e)[:200]}")


def get_snapshots(days: int = 30) -> list:
    db = get_client()
    result = (db.table("portfolio_snapshots")
               .select("*")
               .order("snapshot_at", desc=True)
               .limit(days)
               .execute())
    return result.data or []


# ── Portfolio reviews ─────────────────────────────────────────────────────────

def save_portfolio_review(review: dict):
    """Persist a weekly advisory review to portfolio_reviews."""
    try:
        import json
        db = get_client(write=True)
        db.table("portfolio_reviews").insert({
            "reviewed_at":    review.get("reviewed_at"),
            "equity_eur":     review.get("equity_eur"),
            "position_count": review.get("position_count"),
            "summary":        json.dumps(review.get("summary", {})),
            "alerts":         json.dumps(review.get("alerts", [])),
            "positions":      json.dumps(review.get("positions", [])),
            "exposure":       json.dumps(review.get("exposure", {})),
        }).execute()
    except Exception as e:
        print(f"[REVIEW_WRITE_FAILED] {str(e)[:200]}")


def get_portfolio_reviews(limit: int = 12) -> list:
    """Return the most recent weekly advisory reviews."""
    try:
        db = get_client()
        result = (db.table("portfolio_reviews")
                  .select("*")
                  .order("reviewed_at", desc=True)
                  .limit(limit)
                  .execute())
        return result.data or []
    except Exception as e:
        print(f"[REVIEW_READ_FAILED] {str(e)[:200]}")
        return []


# ── Logs ──────────────────────────────────────────────────────────────────────

def log_event(level: str, event: str, detail: dict = None):
    try:
        print(f"[{level}] {event}: {detail or {}}")
        db = get_client(write=True)
        db.table("agent_logs").insert({
            "level":  level,
            "event":  event,
            "detail": detail or {},
        }).execute()
    except Exception as e:
        print(f"[LOG_WRITE_FAILED] {level} {event}: {str(e)[:200]}")


def get_logs(level: str = None, limit: int = 100) -> list:
    db = get_client()
    q = db.table("agent_logs").select("*").order("logged_at", desc=True)
    if level:
        q = q.eq("level", level)
    result = q.limit(limit).execute()
    return result.data or []


# ── Signal percentile baselines ───────────────────────────────────────────────

def get_signal_percentiles(tickers: list) -> dict:
    """
    Returns {ticker: {sample_count, p50, p70, p85, p90, p95, window_composites}}
    for all requested tickers. Missing tickers return {}.
    """
    if not tickers:
        return {}
    try:
        db = get_client()
        upper_tickers = [t.upper() for t in tickers]
        result = (
            db.table("signal_percentiles")
            .select("ticker,sample_count,p50,p70,p85,p90,p95,window_composites")
            .in_("ticker", upper_tickers)
            .execute()
        )
        return {row["ticker"]: row for row in (result.data or [])}
    except Exception as e:
        print(f"[PERCENTILE_READ_FAILED] {str(e)[:200]}")
        return {}


def upsert_signal_percentiles(ticker: str, data: dict) -> dict:
    """
    Persist updated percentile thresholds and rolling window for a ticker.
    data: {sample_count, p50, p70, p85, p90, p95, window_composites (list)}
    """
    try:
        db = get_client(write=True)
        record = {
            "ticker":            ticker.upper(),
            "updated_at":        datetime.utcnow().isoformat() + "Z",
            "sample_count":      int(data.get("sample_count") or 0),
            "p50":               data.get("p50"),
            "p70":               data.get("p70"),
            "p85":               data.get("p85"),
            "p90":               data.get("p90"),
            "p95":               data.get("p95"),
            "window_composites": data.get("window_composites") or [],
        }
        result = db.table("signal_percentiles").upsert(record, on_conflict="ticker").execute()
        return result.data[0] if result.data else {}
    except Exception as e:
        print(f"[PERCENTILE_WRITE_FAILED] {str(e)[:200]}")
        return {"error": str(e)}


# ── Views ─────────────────────────────────────────────────────────────────────

def get_regime_performance_view() -> list:
    try:
        db = get_client()
        result = db.table("regime_performance").select("*").execute()
        return result.data or []
    except Exception:
        return []


def get_latest_weights_view() -> list:
    try:
        db = get_client()
        result = db.table("latest_signal_weights").select("*").execute()
        return result.data or []
    except Exception:
        return []
