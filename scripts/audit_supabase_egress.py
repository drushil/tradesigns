"""Read-only Supabase DB/egress audit for the Tradesigns project.

The goal is to measure payload size for the app's hot read paths without
printing secrets or dumping raw rows.
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta

from dotenv import load_dotenv

load_dotenv()

from database.client import (  # noqa: E402
    get_advisory_auto_sim_signal_ids,
    get_advisory_scoreboard,
    get_active_advisory_auto_simulations,
    get_blocked_opportunities,
    get_client,
    get_daily_reviews,
    get_latest_advisory_scan_log,
    get_latest_advisory_scan_snapshots,
    get_latest_premarket_radar_snapshots,
    get_log_health,
    get_logs,
    get_open_trade_records,
    get_recent_advisory_signals,
    get_recent_signals,
    get_recent_trades,
)

# Same narrow column lists backend/advisory.py::run_advisory_cycle() passes for
# its prologue reads — kept here so this script measures what the hot path
# actually fetches, not the unbounded select("*") default.
_ADVISORY_PROLOGUE_SIGNAL_COLUMNS = (
    "id,market,data_symbol,side,status,created_at,grade,composite_score,"
    "breakout_quality,manual_pnl_eur,message_text,signal_json"
)
_ADVISORY_PROLOGUE_TRADE_COLUMNS = "regime,composite_score,net_pnl_pct,side"


def _json_bytes(value) -> int:
    return len(json.dumps(value, default=str, separators=(",", ":")).encode("utf-8"))


def _sample(name: str, fn, *args, **kwargs) -> dict:
    try:
        rows = fn(*args, **kwargs) or []
        if isinstance(rows, set):
            rows = list(rows)
        row_sizes = [_json_bytes(row) for row in rows]
        return {
            "name": name,
            "ok": True,
            "rows": len(rows),
            "bytes": sum(row_sizes),
            "max_row_bytes": max(row_sizes) if row_sizes else 0,
            "avg_row_bytes": round(sum(row_sizes) / len(row_sizes), 1) if row_sizes else 0,
        }
    except Exception as exc:
        return {"name": name, "ok": False, "error": str(exc)[:180]}


def _table_count(db, table: str, column: str = "id") -> dict:
    try:
        result = db.table(table).select(column, count="exact").limit(0).execute()
        return {"table": table, "ok": True, "count": result.count}
    except Exception as exc:
        return {"table": table, "ok": False, "error": str(exc)[:160]}


def _log_profile() -> dict:
    try:
        rows = get_logs(limit=1000)
        events = Counter(str(row.get("event") or "") for row in rows)
        levels = Counter(str(row.get("level") or "") for row in rows)
        return {
            "rows_sampled": len(rows),
            "bytes": _json_bytes(rows),
            "levels": levels.most_common(10),
            "events": events.most_common(15),
        }
    except Exception as exc:
        return {"error": str(exc)[:180]}


def _recent_table_profile(db, table: str, ts_column: str, days: int = 7) -> dict:
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat() + "Z"
    try:
        count_result = (
            db.table(table)
            .select("id", count="exact")
            .gte(ts_column, cutoff)
            .limit(0)
            .execute()
        )
        sample_result = (
            db.table(table)
            .select("*")
            .gte(ts_column, cutoff)
            .order(ts_column, desc=True)
            .limit(50)
            .execute()
        )
        rows = sample_result.data or []
        row_sizes = [_json_bytes(row) for row in rows]
        return {
            "table": table,
            "days": days,
            "count": count_result.count,
            "sample_rows": len(rows),
            "sample_bytes": sum(row_sizes),
            "avg_row_bytes": round(sum(row_sizes) / len(row_sizes), 1) if row_sizes else 0,
            "max_row_bytes": max(row_sizes) if row_sizes else 0,
        }
    except Exception as exc:
        return {"table": table, "error": str(exc)[:180]}


def main() -> None:
    db = get_client()
    samples = [
        _sample("sidebar:get_logs_100_full*", get_logs, limit=100),
        _sample("sidebar:get_log_health_100", get_log_health, limit=100),
        _sample("logs_page:get_logs_1000", get_logs, limit=1000),
        _sample("dashboard:get_recent_advisory_signals_live_1d_400", get_recent_advisory_signals, days=1, mode="live", limit=400),
        _sample("agent:get_recent_advisory_signals_shadow_1d_200", get_recent_advisory_signals, days=1, mode="shadow", limit=200),
        _sample("agent:get_recent_trades_90d_500_full*", get_recent_trades, days=90),
        _sample("advisory_cycle:get_recent_advisory_signals_live_1d_narrow", get_recent_advisory_signals,
                days=1, mode="live", columns=_ADVISORY_PROLOGUE_SIGNAL_COLUMNS),
        _sample("advisory_cycle:get_recent_advisory_signals_shadow_1d_narrow", get_recent_advisory_signals,
                days=1, mode="shadow", columns=_ADVISORY_PROLOGUE_SIGNAL_COLUMNS),
        _sample("advisory_cycle:get_recent_trades_90d_narrow", get_recent_trades,
                days=90, columns=_ADVISORY_PROLOGUE_TRADE_COLUMNS),
        _sample("dashboard:get_latest_advisory_scan_snapshots_100", get_latest_advisory_scan_snapshots, market="US", limit=100),
        _sample("dashboard:get_latest_advisory_scan_log_100", get_latest_advisory_scan_log, market="US", limit=100),
        _sample("dashboard:get_latest_premarket_radar_snapshots_100", get_latest_premarket_radar_snapshots, limit=100),
        _sample("learning:get_blocked_opportunities_7d_500", get_blocked_opportunities, days=7, limit=500),
        _sample("legacy:get_recent_signals_24h_500", get_recent_signals, hours=24, limit=500),
        _sample("ops:get_daily_reviews_20_full*", get_daily_reviews, limit=20),
        _sample("agent:get_open_trade_records", get_open_trade_records),
        _sample("sim:get_advisory_auto_sim_signal_ids_all", get_advisory_auto_sim_signal_ids),
        _sample("sim:get_active_advisory_auto_simulations_200", get_active_advisory_auto_simulations, limit=200),
        _sample("intel:get_advisory_scoreboard_30d_500", get_advisory_scoreboard, days_back=30, limit=500),
    ]
    tables = [
        _table_count(db, "advisory_signals"),
        _table_count(db, "advisory_scan_log"),
        _table_count(db, "advisory_scan_snapshots"),
        _table_count(db, "advisory_auto_simulations"),
        _table_count(db, "blocked_opportunities"),
        _table_count(db, "agent_logs"),
        _table_count(db, "signals"),
        _table_count(db, "trades"),
        _table_count(db, "portfolio_snapshots"),
        _table_count(db, "daily_reviews"),
    ]
    recent_profiles = [
        _recent_table_profile(db, "advisory_signals", "created_at", 7),
        _recent_table_profile(db, "advisory_scan_log", "scanned_at", 7),
        _recent_table_profile(db, "advisory_scan_snapshots", "cycle_started_at", 7),
        _recent_table_profile(db, "advisory_auto_simulations", "simulated_at", 7),
        _recent_table_profile(db, "blocked_opportunities", "created_at", 7),
        _recent_table_profile(db, "agent_logs", "logged_at", 7),
        _recent_table_profile(db, "signals", "created_at", 7),
        _recent_table_profile(db, "trades", "created_at", 90),
    ]
    result = {
        "generated_at_utc": datetime.utcnow().isoformat() + "Z",
        "hot_read_samples": sorted(samples, key=lambda row: row.get("bytes", 0), reverse=True),
        "table_counts": tables,
        "recent_table_profiles": sorted(recent_profiles, key=lambda row: row.get("sample_bytes", 0), reverse=True),
        "log_profile": _log_profile(),
    }
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
