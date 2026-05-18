"""
Post-market daily review.

This module is intentionally read-only with respect to trading configuration:
it stores facts, synthesis, and recommendations, but never changes GitHub vars
or risk settings by itself.
"""
from __future__ import annotations

import json
import os
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import requests

from database.client import (
    get_recent_trades,
    get_blocked_opportunities,
    get_open_trade_records,
    get_recent_advisory_signals,
    get_logs,
    save_daily_review,
    insert_config_change_recommendations,
    log_event,
)


MODEL = os.getenv("DAILY_REVIEW_MODEL", "llama-3.3-70b-versatile")


def _parse_dt(value) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _review_day_bounds(review_date: date) -> tuple[datetime, datetime]:
    start = datetime(review_date.year, review_date.month, review_date.day, tzinfo=timezone.utc)
    return start, start + timedelta(days=1)


def _rows_for_date(rows: list[dict], review_date: date, *time_fields: str) -> list[dict]:
    start, end = _review_day_bounds(review_date)
    filtered = []
    for row in rows or []:
        dt = None
        for field in time_fields:
            dt = _parse_dt(row.get(field))
            if dt:
                break
        if dt and start <= dt < end:
            filtered.append(row)
    return filtered


def _avg(values: list[float]) -> float:
    values = [float(v or 0) for v in values]
    return sum(values) / len(values) if values else 0.0


def _sum_pnl(rows: list[dict]) -> float:
    return round(sum(float(r.get("pnl_eur") or 0) for r in rows), 2)


def _group_pnl(rows: list[dict], key: str) -> dict:
    grouped: dict[str, dict] = {}
    for row in rows:
        value = str(row.get(key) or "unknown")
        item = grouped.setdefault(value, {"count": 0, "pnl_eur": 0.0, "wins": 0, "losses": 0})
        pnl = float(row.get("pnl_eur") or 0)
        item["count"] += 1
        item["pnl_eur"] += pnl
        if pnl > 0:
            item["wins"] += 1
        else:
            item["losses"] += 1
    return {
        k: {**v, "pnl_eur": round(v["pnl_eur"], 2)}
        for k, v in sorted(grouped.items(), key=lambda kv: kv[1]["pnl_eur"])
    }


def _blocked_replay_summary(blocked: list[dict]) -> dict:
    checked = [b for b in blocked if b.get("replay_checked_at")]
    missed_winners = []
    bad_avoids = []
    neutral = []
    for item in checked:
        favorable = float(item.get("max_favorable_pct") or 0)
        adverse = float(item.get("max_adverse_pct") or 0)
        close_after = float(item.get("close_after_pct") or 0)
        payload = {
            "ticker": item.get("ticker"),
            "reason": item.get("block_reason"),
            "stage": item.get("block_stage"),
            "max_favorable_pct": round(favorable, 3),
            "max_adverse_pct": round(adverse, 3),
            "close_after_pct": round(close_after, 3),
            "setup_grade": item.get("setup_grade"),
        }
        if favorable >= 0.75 and close_after > 0:
            missed_winners.append(payload)
        elif adverse <= -0.50 or close_after <= 0:
            bad_avoids.append(payload)
        else:
            neutral.append(payload)
    return {
        "total": len(blocked),
        "checked": len(checked),
        "missed_winners": missed_winners[:10],
        "bad_avoids": bad_avoids[:10],
        "neutral_count": len(neutral),
    }


def _shadow_repeats_from_logs(review_date: date) -> list[dict]:
    rows = _rows_for_date(get_logs(level="INFO", limit=500), review_date, "logged_at", "created_at")
    counts: Counter[tuple[str, str]] = Counter()
    latest: dict[tuple[str, str], dict] = {}
    for row in rows:
        if row.get("event") != "dynamic_universe_shadow_recommendations":
            continue
        detail = row.get("detail") or {}
        for candidate in detail.get("shadow_candidates") or []:
            ticker = str(candidate.get("ticker") or "").upper()
            theme = str(candidate.get("theme") or "unknown")
            if not ticker:
                continue
            key = (ticker, theme)
            counts[key] += 1
            latest[key] = candidate
    output = []
    threshold = int(os.getenv("DYNAMIC_UNIVERSE_REPEAT_REVIEW_THRESHOLD", "3") or 3)
    for (ticker, theme), count in counts.most_common(12):
        candidate = latest.get((ticker, theme), {})
        output.append({
            "ticker": ticker,
            "theme": theme,
            "mentions": count,
            "threshold": threshold,
            "review_candidate": count >= threshold,
            "reason": candidate.get("reason"),
            "theme_relative_pct": candidate.get("theme_relative_pct"),
        })
    return output


def _config_recommendations_from_metrics(metrics: dict) -> list[dict]:
    recs = []
    for item in metrics.get("shadow_universe", []):
        if not item.get("review_candidate"):
            continue
        ticker = item["ticker"]
        current = os.getenv("TICKER_UNIVERSE", "")
        tickers = [t.strip().upper() for t in current.split(",") if t.strip()]
        if ticker in tickers:
            continue
        suggested = ",".join(tickers + [ticker]) if tickers else ticker
        recs.append({
            "category": "universe",
            "variable": "TICKER_UNIVERSE",
            "current_value": current,
            "suggested_value": suggested,
            "command_text": f"gh variable set TICKER_UNIVERSE --body {suggested}",
            "reason": f"{ticker} appeared in shadow universe {item['mentions']} times for theme {item['theme']}.",
            "evidence": item,
            "confidence": min(0.9, 0.55 + 0.05 * int(item["mentions"])),
            "evidence_days": 1,
            "expected_effect": "Expose live ranking to a repeatedly leading sector candidate without bypassing normal gates.",
            "success_metric": "Candidate ranks above existing theme members and produces non-negative net P&L after 3-5 signals.",
            "rollback_condition": "Remove if it produces two same-direction losses or repeatedly fails EV/ranging gates.",
            "autonomy_level": "human_approval",
            "status": "pending",
        })
    return recs[:5]


def collect_daily_metrics(review_date: date = None) -> dict:
    review_date = review_date or datetime.now(timezone.utc).date()
    trades = _rows_for_date(get_recent_trades(days=3), review_date, "exit_time", "created_at")
    blocked = _rows_for_date(get_blocked_opportunities(days=3, limit=500), review_date, "created_at")
    advisory = _rows_for_date(get_recent_advisory_signals(days=3, limit=500), review_date, "created_at")
    open_trades = get_open_trade_records()

    wins = [t for t in trades if float(t.get("pnl_eur") or 0) > 0]
    losses = [t for t in trades if float(t.get("pnl_eur") or 0) <= 0]
    by_ticker = _group_pnl(trades, "ticker")
    by_exit = _group_pnl(trades, "exit_reason")
    by_regime = _group_pnl(trades, "regime")

    same_ticker_losses = [
        {"ticker": ticker, **data}
        for ticker, data in by_ticker.items()
        if data["losses"] >= 2 and data["pnl_eur"] < 0
    ]
    regime_counts = Counter(str(t.get("regime") or "unknown") for t in trades)

    metrics = {
        "review_date": review_date.isoformat(),
        "trade_summary": {
            "total_trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate_pct": round(len(wins) / len(trades) * 100, 1) if trades else 0.0,
            "total_pnl_eur": _sum_pnl(trades),
            "avg_net_pnl_pct": round(_avg([t.get("net_pnl_pct") for t in trades]), 4),
            "avg_hold_minutes": round(_avg([t.get("hold_minutes") for t in trades]), 1),
        },
        "pnl_by_ticker": by_ticker,
        "pnl_by_exit_reason": by_exit,
        "pnl_by_regime": by_regime,
        "regime_counts": dict(regime_counts),
        "same_ticker_loss_clusters": same_ticker_losses,
        "blocked_opportunities": _blocked_replay_summary(blocked),
        "shadow_universe": _shadow_repeats_from_logs(review_date),
        "advisory": {
            "total": len(advisory),
            "live": sum(1 for a in advisory if a.get("mode") == "live"),
            "shadow": sum(1 for a in advisory if a.get("mode") == "shadow"),
            "sent": sum(1 for a in advisory if a.get("status") == "sent"),
            "manual_pnl_eur": round(sum(float(a.get("manual_pnl_eur") or 0) for a in advisory), 2),
            "by_market": dict(Counter(str(a.get("market") or "unknown") for a in advisory)),
        },
        "open_positions": [
            {
                "ticker": r.get("ticker"),
                "side": r.get("side"),
                "entry_price": r.get("entry_price"),
                "size_eur": r.get("size_eur"),
                "horizon": r.get("horizon"),
                "setup_grade": r.get("setup_grade"),
                "stop_pct": r.get("stop_pct"),
                "close_reason": r.get("close_reason"),
            }
            for r in open_trades
        ],
    }
    metrics["deterministic_recommendations"] = _config_recommendations_from_metrics(metrics)
    return metrics


def _fallback_review(metrics: dict, error: str = None) -> dict:
    trade = metrics.get("trade_summary", {})
    recs = metrics.get("deterministic_recommendations", [])
    return {
        "summary": (
            f"{trade.get('total_trades', 0)} trades, "
            f"€{trade.get('total_pnl_eur', 0):+.2f} P&L. "
            "Daily review used deterministic fallback."
        ),
        "confidence": 0.4 if error else 0.6,
        "worked_well": [],
        "did_not_work": [],
        "missed_opportunities": metrics.get("blocked_opportunities", {}).get("missed_winners", [])[:3],
        "bad_avoids": metrics.get("blocked_opportunities", {}).get("bad_avoids", [])[:3],
        "tomorrow_focus": [],
        "recommendations": recs,
        "do_not_change": ["No automatic risk/config change was made."],
        "error": error,
    }


def synthesize_daily_review(metrics: dict) -> dict:
    prompt = f"""You are reviewing one day of a paper-trading system.

Use ONLY the facts in METRICS. Do not invent trades or prices.
Return compact JSON only. Config changes must be recommendations only;
auto_apply must always be false for risk, sizing, universe, and threshold changes.
One noisy day is not enough to change thresholds unless it is an urgent safety fix.

METRICS:
{json.dumps(metrics, default=str)[:18000]}

Return this JSON object:
{{
  "summary": "one concise factual summary",
  "confidence": 0.0,
  "worked_well": ["fact"],
  "did_not_work": ["fact"],
  "missed_opportunities": ["fact"],
  "bad_avoids": ["fact"],
  "tomorrow_focus": ["focus"],
  "recommendations": [
    {{
      "category": "universe|risk|sizing|timing|advisory|instrumentation",
      "variable": "optional env/config variable",
      "current_value": "optional",
      "suggested_value": "optional",
      "command_text": "optional exact command for human to run",
      "reason": "evidence-based reason",
      "confidence": 0.0,
      "evidence_days": 1,
      "expected_effect": "measurable expected effect",
      "success_metric": "how to judge success",
      "rollback_condition": "when to revert",
      "autonomy_level": "human_approval",
      "auto_apply": false
    }}
  ],
  "do_not_change": ["things that worked and should remain untouched"]
}}"""
    try:
        from groq import Groq
        client = Groq(api_key=os.getenv("GROQ_API_KEY"))
        response = client.chat.completions.create(
            model=MODEL,
            max_tokens=1200,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.choices[0].message.content.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        review = json.loads(raw.strip())
        if not isinstance(review, dict):
            raise ValueError("review response was not a JSON object")
        review.setdefault("recommendations", [])
        review["recommendations"] = _normalize_recommendations(
            review.get("recommendations", []),
            metrics.get("deterministic_recommendations", []),
        )
        return review
    except Exception as e:
        return _fallback_review(metrics, str(e)[:180])


def _normalize_recommendations(llm_recs: list, deterministic_recs: list) -> list[dict]:
    merged = []
    for rec in (llm_recs or []) + (deterministic_recs or []):
        if not isinstance(rec, dict):
            continue
        item = {
            "category": rec.get("category", "parameter"),
            "variable": rec.get("variable"),
            "current_value": rec.get("current_value"),
            "suggested_value": rec.get("suggested_value"),
            "command_text": rec.get("command_text"),
            "reason": rec.get("reason"),
            "evidence": rec.get("evidence") or {},
            "confidence": float(rec.get("confidence") or 0),
            "evidence_days": int(rec.get("evidence_days") or 1),
            "expected_effect": rec.get("expected_effect"),
            "success_metric": rec.get("success_metric"),
            "rollback_condition": rec.get("rollback_condition"),
            "autonomy_level": rec.get("autonomy_level", "human_approval"),
            "status": "pending",
        }
        item["auto_apply"] = False
        if item["autonomy_level"] not in {"auto_log", "human_approval", "never_auto"}:
            item["autonomy_level"] = "human_approval"
        merged.append(item)
    return merged[:8]


def format_discord_review(review: dict, metrics: dict) -> str:
    trade = metrics.get("trade_summary", {})
    lines = [
        f"**EOD Review — {metrics.get('review_date')}**",
        (
            f"P&L: €{trade.get('total_pnl_eur', 0):+.2f} | "
            f"{trade.get('total_trades', 0)} trades | "
            f"{trade.get('wins', 0)}W/{trade.get('losses', 0)}L"
        ),
    ]
    if review.get("summary"):
        lines.append(str(review["summary"])[:300])

    worked = review.get("worked_well") or []
    failed = review.get("did_not_work") or []
    missed = metrics.get("shadow_universe") or []
    bad_avoids = metrics.get("blocked_opportunities", {}).get("bad_avoids", [])
    if worked:
        lines.append(f"Worked: {str(worked[0])[:220]}")
    if failed:
        lines.append(f"Watch: {str(failed[0])[:220]}")
    review_candidates = [m for m in missed if m.get("review_candidate")]
    if review_candidates:
        parts = [f"{m['ticker']}({m['mentions']}x)" for m in review_candidates[:4]]
        lines.append("Shadow review: " + ", ".join(parts))
    if bad_avoids:
        lines.append(f"Bad avoids tracked: {len(bad_avoids)} blocked signals likely avoided losses")

    recs = review.get("recommendations") or []
    if recs:
        lines.append("Suggested actions:")
        for idx, rec in enumerate(recs[:4], start=1):
            label = rec.get("variable") or rec.get("category") or "review"
            conf = float(rec.get("confidence") or 0)
            reason = str(rec.get("reason") or "")[:160]
            lines.append(f"{idx}. {label}: {reason} (conf {conf:.0%})")
    lines.append("_No config changes were auto-applied._")
    return "\n".join(lines)


def _send_discord(text: str) -> bool:
    webhook = os.getenv("DISCORD_WEBHOOK_URL", "")
    if not webhook:
        return False
    try:
        return requests.post(webhook, json={"content": text}, timeout=10).ok
    except Exception:
        return False


def run_daily_eod_review(review_date: date = None, send_discord: bool = True) -> dict:
    review_date = review_date or datetime.now(timezone.utc).date()
    metrics = collect_daily_metrics(review_date)
    review = synthesize_daily_review(metrics)
    discord_message = format_discord_review(review, metrics)
    recommendations = review.get("recommendations") or []
    saved = save_daily_review({
        "review_date": review_date.isoformat(),
        "status": "pending",
        "summary": review.get("summary"),
        "confidence": review.get("confidence"),
        "metrics_json": metrics,
        "review_json": review,
        "recommendations_json": recommendations,
        "discord_message": discord_message,
        "model": MODEL,
        "error": review.get("error"),
    })
    if saved and "error" not in saved:
        insert_config_change_recommendations(saved.get("id"), review_date.isoformat(), recommendations)
    if send_discord:
        _send_discord(discord_message)
    log_event("INFO", "daily_eod_review_complete", {
        "review_date": review_date.isoformat(),
        "trade_count": metrics.get("trade_summary", {}).get("total_trades", 0),
        "recommendations": len(recommendations),
        "saved": "error" not in (saved or {}),
    })
    return {
        "review_date": review_date.isoformat(),
        "metrics": metrics,
        "review": review,
        "saved": saved,
        "discord_message": discord_message,
    }
