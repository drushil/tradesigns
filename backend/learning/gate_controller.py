"""
Advisory controllers for gate calibration.

These controllers do not mutate runtime thresholds. They emit structured log
events so blocked-opportunity replay and B-grade outcomes can be reviewed before
changing GitHub variables or risk-profile defaults.
"""
from __future__ import annotations

import os
from collections import defaultdict

from database.client import get_blocked_opportunities, get_client, log_event


def _truthy(key: str, default: bool = True) -> bool:
    raw = os.getenv(key)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def run_gate_controller(days: int = 7, limit: int = 500) -> list[dict]:
    """
    Summarize replayed blocked opportunities by gate path and emit suggestions.
    A suggestion means "review this threshold", not "auto-change it".
    """
    if not _truthy("GATE_CONTROLLER_ENABLED", True):
        return []

    rows = get_blocked_opportunities(days=days, limit=limit)
    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in rows:
        if row.get("reference_price") is None:
            continue
        if row.get("close_after_pct") is None or row.get("max_favorable_pct") is None:
            continue
        key = (str(row.get("block_stage") or "unknown"), str(row.get("block_reason") or "unknown"))
        grouped[key].append(row)

    suggestions = []
    min_samples = int(os.getenv("GATE_CONTROLLER_MIN_SAMPLES", "8") or 8)
    min_win_rate = float(os.getenv("GATE_CONTROLLER_MIN_WIN_RATE", "0.55") or 0.55)
    min_avg_favorable = float(os.getenv("GATE_CONTROLLER_MIN_AVG_FAVORABLE_PCT", "0.75") or 0.75)

    for (stage, reason), items in grouped.items():
        if len(items) < min_samples:
            continue
        wins = [r for r in items if float(r.get("close_after_pct") or 0) > 0]
        avg_close = sum(float(r.get("close_after_pct") or 0) for r in items) / len(items)
        avg_fav = sum(float(r.get("max_favorable_pct") or 0) for r in items) / len(items)
        avg_adv = sum(float(r.get("max_adverse_pct") or 0) for r in items) / len(items)
        win_rate = len(wins) / len(items)
        if win_rate >= min_win_rate and avg_fav >= min_avg_favorable and avg_fav > abs(avg_adv):
            suggestion = {
                "block_stage": stage,
                "block_reason": reason,
                "sample_count": len(items),
                "win_rate": round(win_rate, 3),
                "avg_close_after_pct": round(avg_close, 4),
                "avg_max_favorable_pct": round(avg_fav, 4),
                "avg_max_adverse_pct": round(avg_adv, 4),
                "suggestion": "review_threshold_too_tight",
            }
            suggestions.append(suggestion)
            log_event("LEARNING", "gate_adaptation_suggestion", suggestion)

    if suggestions:
        log_event("LEARNING", "gate_controller_complete", {"suggestions": len(suggestions)})
    return suggestions


def run_b_shadow_promotion_controller(limit: int = 200) -> dict:
    """
    Review recent B-grade live outcomes and emit an advisory promotion event when
    the evidence floor is met. Does not update environment variables.
    """
    if not _truthy("B_SHADOW_PROMOTE_ENABLED", True):
        return {"skipped": "disabled"}

    min_samples = int(os.getenv("B_SHADOW_PROMOTE_MIN_SAMPLES", "30") or 30)
    min_win_rate = float(os.getenv("B_SHADOW_PROMOTE_MIN_WIN_RATE", "0.55") or 0.55)
    min_avg_pnl = float(os.getenv("B_SHADOW_PROMOTE_MIN_AVG_PNL_PCT", "0.003") or 0.003)

    try:
        db = get_client()
        result = (db.table("trades")
                  .select("ticker,setup_grade,net_pnl_pct,created_at")
                  .eq("setup_grade", "B")
                  .not_.is_("net_pnl_pct", "null")
                  .order("created_at", desc=True)
                  .limit(limit)
                  .execute())
        rows = result.data or []
    except Exception as e:
        log_event("WARN", "b_shadow_promotion_controller_failed", {"error": str(e)[:160]})
        return {"error": str(e)}

    by_ticker: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_ticker[str(row.get("ticker") or "").upper()].append(row)

    promoted = []
    for ticker, items in by_ticker.items():
        if not ticker or len(items) < min_samples:
            continue
        pnls = [float(r.get("net_pnl_pct") or 0) for r in items]
        win_rate = sum(1 for pnl in pnls if pnl > 0) / len(pnls)
        avg_pnl = sum(pnls) / len(pnls)
        if win_rate >= min_win_rate and avg_pnl >= min_avg_pnl:
            advisory = {
                "ticker": ticker,
                "sample_count": len(items),
                "win_rate": round(win_rate, 3),
                "avg_net_pnl_pct": round(avg_pnl, 4),
                "current_shadow_size_multiplier": float(os.getenv("RANGING_PROBE_GRADE_B_SHADOW_SIZE_MULTIPLIER", "0.10") or 0.10),
                "suggested_shadow_size_multiplier": 0.20,
                "suggestion": "promote_b_shadow_size_after_review",
            }
            promoted.append(advisory)
            log_event("LEARNING", "b_shadow_promotion_suggestion", advisory)

    log_event("LEARNING", "b_shadow_promotion_controller_complete", {"suggestions": len(promoted)})
    return {"suggestions": promoted, "rows": len(rows)}
