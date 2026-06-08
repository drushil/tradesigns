"""
backend/learning/advisory_learner.py
Nightly advisory-first learner.

Reads advisory_pick_scoreboard and advisory_execution_scoreboard, computes
per-(grade × stage × session_window) hit-rates and avg-R, then writes
advisory_policy_recommendations rows with status='proposed'.

Rules:
- Minimum MIN_SAMPLE_SIZE picks before any recommendation is made.
- Recommendations are NOT auto-applied. The config dashboard shows them.
- Each run expires previous 'proposed' rows for the same scope/field before
  writing new ones, so the dashboard always shows the freshest analysis.
- Runs at most once per calendar day (idempotent guard via agent_logs check).
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from database.client import (
    expire_old_advisory_policy_recommendations,
    insert_advisory_policy_recommendations,
    log_event,
)

MIN_SAMPLE_SIZE = int(os.getenv("ADVISORY_LEARNER_MIN_SAMPLE", "10"))
LOOKBACK_DAYS = int(os.getenv("ADVISORY_LEARNER_LOOKBACK_DAYS", "30"))

# Hit-rate below this for a grade/window slice → suggest raising the score floor.
HIT_RATE_LOW_THRESHOLD = float(os.getenv("ADVISORY_LEARNER_HIT_RATE_LOW", "0.35"))
# Hit-rate above this → could safely lower the score floor (capture more).
HIT_RATE_HIGH_THRESHOLD = float(os.getenv("ADVISORY_LEARNER_HIT_RATE_HIGH", "0.65"))
# avg_r below this for sim rows → execution policy may need tightening.
AVG_R_LOW_THRESHOLD = float(os.getenv("ADVISORY_LEARNER_AVG_R_LOW", "0.0"))


def _safe_div(num, den) -> Optional[float]:
    try:
        if den and den > 0:
            return round(float(num) / float(den), 4)
    except (TypeError, ValueError, ZeroDivisionError):
        pass
    return None


def _run_pick_level_analysis(db_client) -> list[dict]:
    """
    Query advisory_pick_scoreboard and generate per-(grade × session_window)
    threshold recommendations.

    Returns a list of recommendation dicts ready for bulk insert.
    """
    try:
        cutoff = (datetime.utcnow() - timedelta(days=LOOKBACK_DAYS)).isoformat() + "Z"
        result = (db_client.table("advisory_signals")
                  .select(
                      "grade,session_window,regime_at_pick,side,"
                      "forward_scored_at,target_hit_first,stop_hit_first,"
                      "direction_correct_60m,forward_return_60m,"
                      "composite_score,breakout_quality,ev_net_pct"
                  )
                  .not_.is_("forward_scored_at", "null")
                  .gte("created_at", cutoff)
                  .eq("side", "BUY")
                  .eq("market", "US")
                  .execute())
        rows = result.data or []
    except Exception as e:
        log_event("WARN", "advisory_learner_pick_read_failed", {"error": str(e)[:160]})
        return []

    if not rows:
        return []

    # Group by (grade, session_window).
    from collections import defaultdict
    groups: dict = defaultdict(list)
    for r in rows:
        grade = str(r.get("grade") or "-")
        window = str(r.get("session_window") or "-")
        groups[(grade, window)].append(r)

    recs = []
    now_iso = datetime.utcnow().isoformat() + "Z"

    for (grade, window), picks in groups.items():
        n = len(picks)
        if n < MIN_SAMPLE_SIZE:
            continue

        tp1_hits = sum(1 for p in picks if p.get("target_hit_first"))
        stops = sum(1 for p in picks if p.get("stop_hit_first"))
        dir_correct = sum(1 for p in picks if p.get("direction_correct_60m"))

        hit_rate = _safe_div(tp1_hits, n)
        dir_rate = _safe_div(dir_correct, n)

        # Average composite score of picks that hit T1 vs those that stopped.
        winning_composites = [
            float(p["composite_score"]) for p in picks
            if p.get("target_hit_first") and p.get("composite_score") is not None
        ]
        losing_composites = [
            float(p["composite_score"]) for p in picks
            if p.get("stop_hit_first") and p.get("composite_score") is not None
        ]
        all_composites = [
            float(p["composite_score"]) for p in picks
            if p.get("composite_score") is not None
        ]
        avg_win_comp = round(sum(winning_composites) / len(winning_composites), 4) if winning_composites else None
        avg_lose_comp = round(sum(losing_composites) / len(losing_composites), 4) if losing_composites else None
        avg_all_comp = round(sum(all_composites) / len(all_composites), 4) if all_composites else None

        # ── Recommendation 1: composite_score floor ───────────────────────────
        # If hit_rate is below the low threshold, suggest raising the floor.
        # If hit_rate is above the high threshold, suggest lowering it (capture more).
        if hit_rate is not None and avg_all_comp is not None:
            if hit_rate < HIT_RATE_LOW_THRESHOLD and avg_win_comp is not None:
                # Use the average composite of winners as the new floor.
                suggested = round(avg_win_comp * 0.95, 4)  # 5% below winner avg for breathing room
                confidence = min(0.9, round(n / 50, 3))
                expected_lift = round((HIT_RATE_LOW_THRESHOLD - hit_rate) * 100, 2)
                recs.append({
                    "computed_at": now_iso,
                    "scope": "grade",
                    "scope_value": f"{grade}|{window}",
                    "recommendation_type": "threshold",
                    "field_name": "min_composite_score",
                    "current_value": avg_all_comp,
                    "suggested_value": suggested,
                    "sample_size": n,
                    "hit_rate": hit_rate,
                    "expected_lift_pct": expected_lift,
                    "confidence": confidence,
                    "evidence_json": {
                        "tp1_hits": tp1_hits,
                        "stops": stops,
                        "avg_win_composite": avg_win_comp,
                        "avg_lose_composite": avg_lose_comp,
                        "direction_rate_60m": dir_rate,
                        "lookback_days": LOOKBACK_DAYS,
                        "grade": grade,
                        "session_window": window,
                    },
                })
            elif hit_rate > HIT_RATE_HIGH_THRESHOLD and avg_lose_comp is not None:
                # Signal is strong — consider widening the filter slightly.
                suggested = round(avg_all_comp * 0.90, 4)
                confidence = min(0.85, round(n / 50, 3))
                recs.append({
                    "computed_at": now_iso,
                    "scope": "grade",
                    "scope_value": f"{grade}|{window}",
                    "recommendation_type": "threshold",
                    "field_name": "min_composite_score",
                    "current_value": avg_all_comp,
                    "suggested_value": suggested,
                    "sample_size": n,
                    "hit_rate": hit_rate,
                    "expected_lift_pct": None,
                    "confidence": confidence,
                    "evidence_json": {
                        "reason": "high_hit_rate_widen",
                        "tp1_hits": tp1_hits,
                        "stops": stops,
                        "lookback_days": LOOKBACK_DAYS,
                        "grade": grade,
                        "session_window": window,
                    },
                })

        # ── Recommendation 2: breakout_quality floor ─────────────────────────
        winning_bqs = [
            float(p["breakout_quality"]) for p in picks
            if p.get("target_hit_first") and p.get("breakout_quality") is not None
        ]
        all_bqs = [
            float(p["breakout_quality"]) for p in picks
            if p.get("breakout_quality") is not None
        ]
        if winning_bqs and all_bqs and hit_rate is not None and hit_rate < HIT_RATE_LOW_THRESHOLD:
            avg_win_bq = round(sum(winning_bqs) / len(winning_bqs), 4)
            avg_all_bq = round(sum(all_bqs) / len(all_bqs), 4)
            if avg_win_bq > avg_all_bq:
                recs.append({
                    "computed_at": now_iso,
                    "scope": "grade",
                    "scope_value": f"{grade}|{window}",
                    "recommendation_type": "threshold",
                    "field_name": "breakout_quality_floor",
                    "current_value": avg_all_bq,
                    "suggested_value": round(avg_win_bq * 0.95, 4),
                    "sample_size": n,
                    "hit_rate": hit_rate,
                    "expected_lift_pct": None,
                    "confidence": min(0.75, round(n / 60, 3)),
                    "evidence_json": {
                        "avg_win_bq": avg_win_bq,
                        "avg_all_bq": avg_all_bq,
                        "lookback_days": LOOKBACK_DAYS,
                        "grade": grade,
                        "session_window": window,
                    },
                })

    return recs


def _run_execution_level_analysis(db_client) -> list[dict]:
    """
    Query advisory_auto_simulations and generate per-(entry_policy × grade) R
    and hit-rate recommendations. Returns list of recommendation dicts.
    """
    try:
        cutoff = (datetime.utcnow() - timedelta(days=LOOKBACK_DAYS)).isoformat() + "Z"
        result = (db_client.table("advisory_auto_simulations")
                  .select(
                      "entry_policy,grade,closure_reason,r_multiple,"
                      "mfe_pct,mae_pct,status,simulated_at"
                  )
                  .not_.is_("closure_reason", "null")
                  .gte("simulated_at", cutoff)
                  .eq("side", "BUY")
                  .eq("market", "US")
                  .execute())
        rows = result.data or []
    except Exception as e:
        log_event("WARN", "advisory_learner_exec_read_failed", {"error": str(e)[:160]})
        return []

    if not rows:
        return []

    from collections import defaultdict
    groups: dict = defaultdict(list)
    for r in rows:
        policy = str(r.get("entry_policy") or "unknown")
        grade = str(r.get("grade") or "-")
        groups[(policy, grade)].append(r)

    recs = []
    now_iso = datetime.utcnow().isoformat() + "Z"

    for (policy, grade), sims in groups.items():
        n = len(sims)
        if n < MIN_SAMPLE_SIZE:
            continue

        tp1_hits = sum(1 for s in sims if s.get("closure_reason") == "target_1")
        stops = sum(1 for s in sims if s.get("closure_reason") == "stop")
        eod_wins = sum(1 for s in sims if s.get("status") == "closed_eod_win")
        eod_losses = sum(1 for s in sims if s.get("status") == "closed_eod_loss")

        terminal = tp1_hits + stops
        hit_rate = _safe_div(tp1_hits, terminal) if terminal >= MIN_SAMPLE_SIZE else None

        r_multiples = [float(s["r_multiple"]) for s in sims if s.get("r_multiple") is not None]
        avg_r = round(sum(r_multiples) / len(r_multiples), 4) if r_multiples else None

        mfes = [float(s["mfe_pct"]) for s in sims if s.get("mfe_pct") is not None]
        maes = [float(s["mae_pct"]) for s in sims if s.get("mae_pct") is not None]
        avg_mfe = round(sum(mfes) / len(mfes), 4) if mfes else None
        avg_mae = round(sum(maes) / len(maes), 4) if maes else None

        if avg_r is not None and avg_r < AVG_R_LOW_THRESHOLD and terminal >= MIN_SAMPLE_SIZE:
            # Negative average R for this policy/grade — recommend scrutiny.
            recs.append({
                "computed_at": now_iso,
                "scope": "stage",
                "scope_value": f"{policy}|{grade}",
                "recommendation_type": "gate",
                "field_name": "entry_policy_avg_r",
                "current_value": avg_r,
                "suggested_value": 0.0,  # floor target
                "sample_size": n,
                "hit_rate": hit_rate,
                "expected_lift_pct": None,
                "confidence": min(0.8, round(n / 40, 3)),
                "evidence_json": {
                    "tp1_hits": tp1_hits,
                    "stops": stops,
                    "eod_wins": eod_wins,
                    "eod_losses": eod_losses,
                    "avg_mfe_pct": avg_mfe,
                    "avg_mae_pct": avg_mae,
                    "lookback_days": LOOKBACK_DAYS,
                    "policy": policy,
                    "grade": grade,
                    "action": "review_or_gate_this_entry_policy",
                },
            })

    return recs


def run_advisory_learner() -> dict:
    """
    Main entry point for the nightly advisory learner.

    Generates advisory_policy_recommendations rows for the config dashboard.
    Safe to call any time — idempotent via expire-then-insert pattern.
    """
    from database.client import get_client
    db = get_client()

    log_event("INFO", "advisory_learner_start", {
        "lookback_days": LOOKBACK_DAYS,
        "min_sample": MIN_SAMPLE_SIZE,
    })

    pick_recs = _run_pick_level_analysis(db)
    exec_recs = _run_execution_level_analysis(db)
    all_recs = pick_recs + exec_recs

    inserted = 0
    expired = 0
    for rec in all_recs:
        scope = rec.get("scope", "")
        scope_value = rec.get("scope_value", "")
        field_name = rec.get("field_name", "")
        expire_old_advisory_policy_recommendations(scope, scope_value, field_name)
        expired += 1

    if all_recs:
        inserted = insert_advisory_policy_recommendations(all_recs)

    summary = {
        "pick_recs": len(pick_recs),
        "exec_recs": len(exec_recs),
        "total_recs": len(all_recs),
        "inserted": inserted,
        "expired_old": expired,
        "lookback_days": LOOKBACK_DAYS,
    }
    log_event("INFO", "advisory_learner_complete", summary)
    return summary
