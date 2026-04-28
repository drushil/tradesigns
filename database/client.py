"""
database/client.py
Supabase client wrapper. All DB operations go through here.
Reads credentials from os.environ (which app.py populates from st.secrets).
"""
import os
from datetime import date
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
        key = _get_env("SUPABASE_SERVICE_KEY") or _get_env("SUPABASE_ANON_KEY")
    else:
        key = _get_env("SUPABASE_ANON_KEY")
    if not key:
        raise ValueError("SUPABASE_ANON_KEY not set — add it to Streamlit secrets or .env")
    return create_client(url, key)


# ── Trades ────────────────────────────────────────────────────────────────────

def insert_trade(trade: dict) -> dict:
    db = get_client(write=True)
    result = db.table("trades").insert(trade).execute()
    return result.data[0] if result.data else {}


def get_recent_trades(days: int = 30, ticker: str = None) -> list:
    db = get_client()
    q = db.table("trades").select("*").order("created_at", desc=True)
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
            return result.data[0]
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
    db = get_client(write=True)
    result = db.table("signals").insert(signal).execute()
    return result.data[0] if result.data else {}


def get_recent_signals(hours: int = 24) -> list:
    db = get_client()
    result = (db.table("signals")
               .select("*")
               .order("created_at", desc=True)
               .limit(200)
               .execute())
    return result.data or []


# ── Signal weights ────────────────────────────────────────────────────────────

def save_signal_weights(regime: str, weights: dict, trade_count: int, trigger: str):
    db = get_client(write=True)
    record = {
        "regime":          regime,
        "order_book":      round(weights.get("order_book_imbalance", 0.25), 4),
        "tape_aggression": round(weights.get("tape_aggression", 0.25), 4),
        "rsi_divergence":  round(weights.get("rsi_divergence", 0.20), 4),
        "news_sentiment":  round(weights.get("news_sentiment", 0.20), 4),
        "vwap_deviation":  round(weights.get("vwap_deviation", 0.10), 4),
        "trade_count":     trade_count,
        "trigger":         trigger,
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
    db = get_client(write=True)
    db.table("portfolio_snapshots").insert(snapshot).execute()


def get_snapshots(days: int = 30) -> list:
    db = get_client()
    result = (db.table("portfolio_snapshots")
               .select("*")
               .order("snapshot_at", desc=True)
               .limit(days)
               .execute())
    return result.data or []


# ── Logs ──────────────────────────────────────────────────────────────────────

def log_event(level: str, event: str, detail: dict = None):
    try:
        db = get_client(write=True)
        db.table("agent_logs").insert({
            "level":  level,
            "event":  event,
            "detail": detail or {},
        }).execute()
    except Exception:
        pass  # never crash the agent due to a logging failure


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
