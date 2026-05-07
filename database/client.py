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
            "size_eur", "pnl_pct", "net_pnl_pct", "pnl_eur",
            "entry_time", "exit_time", "hold_minutes", "exit_reason",
            "regime", "macro_regime", "macro_multiplier", "dip_type",
            "sizing_json", "mean_reversion_trade", "swing_trade",
            "composite_score", "llm_conviction", "llm_rationale",
            "signals_json", "commission_eur", "slippage_eur", "llm_cost_eur",
            "risk_profile", "horizon",
            "atr_at_entry", "r_multiple", "stop_pct_used",
            "hold_decision_json", "hold_extension_count",
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
            "stop_price": trade.get("stop_price"),
            "take_profit_price": trade.get("take_profit_price"),
            "hold_minutes": trade.get("hold_minutes"),
            "hold_days": trade.get("hold_days"),
            "horizon": trade.get("horizon"),
            "size_eur": trade.get("size_eur"),
            "size_usd": trade.get("size_usd"),
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
            "status": "open",
        }
        try:
            result = db.table("open_trades").upsert(record, on_conflict="ticker").execute()
        except Exception:
            fallback = {
                k: v for k, v in record.items()
                if k not in {"quantity", "hold_minutes", "hold_days", "horizon",
                             "size_usd",
                             "macro_regime", "macro_multiplier", "dip_type",
                             "sizing_json", "mean_reversion_trade", "swing_trade",
                             "promoted_to_swing", "promoted_at", "initial_horizon",
                             "swing_conviction", "swing_reasons",
                             "highest_price_since_entry", "trailing_stop_price",
                             "stop_multiplier", "stop_pct", "max_hold_minutes",
                             "daily_reeval_count", "hold_extension_count", "hold_decision_json",
                             "peak_directional_score",
                             "protective_stop_order_id",
                             "exposure_direction", "strategy_family", "regime_debug_json"}
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
        }
        fallback = {k: v for k, v in signal.items() if k in base_columns}
        try:
            result = db.table("signals").insert(fallback).execute()
            return result.data[0] if result.data else {}
        except Exception:
            return {"error": str(e)}


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
        db.table("portfolio_snapshots").insert(snapshot).execute()
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
