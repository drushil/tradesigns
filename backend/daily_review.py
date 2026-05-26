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
from pathlib import Path
from typing import Optional

import requests

from backend.broker.alpaca import get_account
from database.client import (
    get_recent_trades,
    get_blocked_opportunities,
    get_open_trade_records,
    get_recent_advisory_signals,
    get_recent_signals,
    get_logs,
    save_daily_review,
    insert_config_change_recommendations,
    log_event,
)


MODEL = os.getenv("DAILY_REVIEW_MODEL", "llama-3.3-70b-versatile")
_SNAPSHOT_DIR = Path(os.getenv("DAILY_REVIEW_SNAPSHOT_DIR", "artifacts/daily_reviews"))


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
        replay_json = item.get("replay_result_json") or {}
        block_detail = item.get("block_detail") or replay_json.get("block_detail") or {}
        severity = replay_json.get("runner_severity")
        if not severity and favorable >= 2.0 and close_after > 0:
            severity = "runner"
        elif not severity and favorable >= 0.75 and close_after > 0:
            severity = "minor"
        if severity:
            payload["runner_severity"] = severity
        if block_detail:
            payload["block_detail"] = block_detail
            if block_detail.get("threshold_gap") is not None:
                payload["threshold_gap"] = block_detail.get("threshold_gap")
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


def _median(values: list[float]) -> float:
    numeric = []
    for value in values:
        if value in {None, ""}:
            continue
        try:
            numeric.append(float(value))
        except (TypeError, ValueError):
            continue
    numeric = sorted(numeric)
    if not numeric:
        return 0.0
    mid = len(numeric) // 2
    if len(numeric) % 2:
        return round(numeric[mid], 4)
    return round((numeric[mid - 1] + numeric[mid]) / 2, 4)


def _near_miss_distribution(blocked: list[dict]) -> dict:
    items = []
    for row in blocked:
        detail = row.get("block_detail") or {}
        if not detail.get("near_threshold"):
            continue
        favorable = row.get("max_favorable_pct")
        adverse = row.get("max_adverse_pct")
        close_after = row.get("close_after_pct")
        checked = row.get("replay_checked_at") is not None
        item = {
            "ticker": row.get("ticker"),
            "stage": row.get("block_stage"),
            "reason": row.get("block_reason"),
            "score": detail.get("score"),
            "threshold": detail.get("threshold"),
            "threshold_gap": detail.get("threshold_gap"),
            "checked": checked,
            "max_favorable_pct": favorable,
            "max_adverse_pct": adverse,
            "close_after_pct": close_after,
        }
        items.append(item)
    checked_items = [i for i in items if i["checked"]]
    runners = [
        i for i in checked_items
        if float(i.get("max_favorable_pct") or 0) >= 2.0
        and float(i.get("close_after_pct") or 0) > 0
    ]
    return {
        "total": len(items),
        "checked": len(checked_items),
        "runner_count": len(runners),
        "median_max_favorable_pct": _median([i.get("max_favorable_pct") for i in checked_items]),
        "median_max_adverse_pct": _median([i.get("max_adverse_pct") for i in checked_items]),
        "median_close_after_pct": _median([i.get("close_after_pct") for i in checked_items]),
        "top_runners": sorted(
            runners,
            key=lambda i: float(i.get("max_favorable_pct") or 0),
            reverse=True,
        )[:10],
        "sample": sorted(
            checked_items,
            key=lambda i: float(i.get("max_favorable_pct") or 0),
            reverse=True,
        )[:10],
        "guardrail": (
            "Observability only: do not change trading behavior until at least 14 days "
            "or 20 near-threshold cases across multiple regimes, whichever comes later."
        ),
    }


def _signal_time(row: dict) -> Optional[datetime]:
    return _parse_dt(row.get("created_at") or row.get("logged_at"))


def _nearest_signal_snapshot(ticker: str, when: datetime, signals: list[dict]) -> dict:
    ticker = str(ticker or "").upper()
    candidates = []
    for row in signals:
        if str(row.get("ticker") or "").upper() != ticker:
            continue
        ts = _signal_time(row)
        if not ts or not when:
            continue
        delta = abs((ts - when).total_seconds())
        if delta <= 15 * 60:
            candidates.append((delta, row))
    if not candidates:
        return {}
    row = min(candidates, key=lambda item: item[0])[1]
    return {
        "created_at": row.get("created_at"),
        "composite_score": row.get("composite_score"),
        "rsi_divergence_score": row.get("rsi_divergence_score"),
        "vwap_deviation_score": row.get("vwap_deviation_score"),
        "news_sentiment_score": row.get("news_sentiment_score"),
        "tape_aggression_score": row.get("tape_aggression_score"),
        "order_book_score": row.get("order_book_score"),
        "macd_score": row.get("macd_score"),
        "rel_strength_score": row.get("rel_strength_score"),
        "orb_score": row.get("orb_score"),
        "action_hint": row.get("action_hint"),
        "regime": row.get("regime"),
    }


def _direction_error_candidates(blocked: list[dict], signals: list[dict]) -> list[dict]:
    output = []
    for row in blocked:
        if not row.get("replay_checked_at"):
            continue
        adverse = float(row.get("max_adverse_pct") or 0)
        close_after = float(row.get("close_after_pct") or 0)
        if adverse > -2.0 and close_after > -1.0:
            continue
        created_at = _parse_dt(row.get("created_at"))
        output.append({
            "ticker": row.get("ticker"),
            "action": row.get("action_hint"),
            "stage": row.get("block_stage"),
            "reason": row.get("block_reason"),
            "max_favorable_pct": round(float(row.get("max_favorable_pct") or 0), 3),
            "max_adverse_pct": round(adverse, 3),
            "close_after_pct": round(close_after, 3),
            "signal_snapshot": _nearest_signal_snapshot(row.get("ticker"), created_at, signals),
        })
    return sorted(
        output,
        key=lambda item: (item["close_after_pct"], item["max_adverse_pct"]),
    )[:10]


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
            "evidence_days": _shadow_evidence_days(ticker, theme, review_date),
            "reason": candidate.get("reason"),
            "theme_relative_pct": candidate.get("theme_relative_pct"),
        })
    return output


def _shadow_evidence_days(ticker: str, theme: str, review_date: date) -> int:
    ticker = str(ticker or "").upper()
    theme = str(theme or "")
    days = set()
    try:
        rows = get_logs(level="INFO", limit=1500)
    except Exception:
        rows = []
    for row in rows or []:
        if row.get("event") != "dynamic_universe_shadow_recommendations":
            continue
        logged_at = _parse_dt(row.get("logged_at") or row.get("created_at"))
        if not logged_at:
            continue
        if not (review_date - timedelta(days=6) <= logged_at.date() <= review_date):
            continue
        for candidate in (row.get("detail") or {}).get("shadow_candidates") or []:
            if str(candidate.get("ticker") or "").upper() == ticker and str(candidate.get("theme") or "") == theme:
                days.add(logged_at.date().isoformat())
                break
    return len(days) or 1


def _gate_activity_from_logs(review_date: date) -> dict:
    rows = _rows_for_date(get_logs(limit=1500), review_date, "logged_at", "created_at")
    gate_events = {
        "signal_consensus_veto",
        "reward_risk_veto",
        "theme_open_exposure_cap",
        "signal_alignment_veto",
        "ranging_regime_candidate_block",
        "trade_gated",
        "ev_blocked_pending_grade",
        "grade_ev_override_known_negative_block",
    }
    counts = Counter(row.get("event") for row in rows if row.get("event") in gate_events)
    veto_tickers = Counter()
    consensus_aligned = Counter()
    ranging_reasons = Counter()
    regime_observations = []
    for row in rows:
        event = row.get("event")
        detail = row.get("detail") or {}
        if event in gate_events and detail.get("ticker"):
            veto_tickers[str(detail.get("ticker")).upper()] += 1
        if event == "signal_consensus_veto":
            consensus_aligned[str(detail.get("aligned_count"))] += 1
        if event == "ranging_regime_candidate_block":
            ranging_reasons[str(detail.get("reason") or "unknown")] += 1
        if event == "cycle_regime_observability":
            regime_observations.append({
                "logged_at": row.get("logged_at"),
                "intraday_regimes": detail.get("intraday_regimes") or {},
                "spy_trend_score": detail.get("spy_trend_score"),
                "spy_trend_threshold": detail.get("spy_trend_threshold"),
                "spy_regime_reason": detail.get("spy_regime_reason"),
            })
    return {
        "event_counts": dict(counts),
        "top_veto_tickers": [{"ticker": k, "count": v} for k, v in veto_tickers.most_common(12)],
        "consensus_aligned_count_distribution": dict(consensus_aligned),
        "ranging_block_reasons": dict(ranging_reasons),
        "latest_regime_observations": regime_observations[-5:],
    }


def _broker_rejections_from_logs(review_date: date) -> dict:
    rows = _rows_for_date(get_logs(limit=1500), review_date, "logged_at", "created_at")
    order_failed = [row for row in rows if row.get("event") == "order_failed"]
    grouped_by_error = Counter()
    grouped_by_ticker = Counter()
    examples = []
    for row in order_failed:
        detail = row.get("detail") or {}
        ticker = str(detail.get("ticker") or "unknown").upper()
        error = str(detail.get("error") or "unknown")
        grouped_by_error[error] += 1
        grouped_by_ticker[ticker] += 1
        if len(examples) < 10:
            examples.append({
                "logged_at": row.get("logged_at") or row.get("created_at"),
                "ticker": ticker,
                "error": error,
                "client_order_id": detail.get("client_order_id"),
            })
    return {
        "count": len(order_failed),
        "grouped_by_error": dict(grouped_by_error),
        "grouped_by_ticker": dict(grouped_by_ticker),
        "recent_examples": examples,
    }


def _broker_account_snapshot() -> dict:
    try:
        account = get_account() or {}
        return {
            "ok": "error" not in account,
            "equity": account.get("equity"),
            "cash": account.get("cash"),
            "buying_power": account.get("buying_power"),
            "daytrade_count": account.get("daytrade_count"),
            "pattern_day_trader": account.get("pattern_day_trader"),
            "trading_blocked": account.get("trading_blocked"),
            "account_blocked": account.get("account_blocked"),
            "status": account.get("status"),
            "error": account.get("error"),
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:160]}


def _snapshot_payload(review_date: date, metrics: dict, review: dict, saved: dict,
                      recommendations: list[dict], discord_message: str,
                      discord_sent: bool) -> dict:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "review_date": review_date.isoformat(),
        "daily_review": {
            "summary": review.get("summary"),
            "confidence": review.get("confidence"),
            "worked_well": review.get("worked_well") or [],
            "did_not_work": review.get("did_not_work") or [],
            "recommendations": recommendations,
            "do_not_change": review.get("do_not_change") or [],
        },
        "metrics": metrics,
        "broker_account_snapshot": metrics.get("broker_account_snapshot") or {},
        "broker_rejections": metrics.get("broker_rejections") or {},
        "data_source": {
            "supabase_saved": "error" not in (saved or {}),
            "local_snapshot_saved": False,
            "discord_sent": discord_sent,
        },
        "discord_message": discord_message,
        "saved": saved,
    }


def _write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    tmp_path.replace(path)


def save_local_daily_review_snapshot(review_date: date, metrics: dict, review: dict, saved: dict,
                                     recommendations: list[dict], discord_message: str,
                                     discord_sent: bool) -> dict:
    payload = _snapshot_payload(
        review_date, metrics, review, saved, recommendations, discord_message, discord_sent
    )
    dated_path = _SNAPSHOT_DIR / f"{review_date.isoformat()}.json"
    latest_path = _SNAPSHOT_DIR / "latest.json"
    try:
        _write_json_atomic(dated_path, payload)
        _write_json_atomic(latest_path, payload)
        payload["data_source"]["local_snapshot_saved"] = True
        _write_json_atomic(dated_path, payload)
        _write_json_atomic(latest_path, payload)
        return {
            "ok": True,
            "dated_path": str(dated_path),
            "latest_path": str(latest_path),
        }
    except Exception as exc:
        log_event("ERROR", "daily_review_snapshot_failed", {
            "review_date": review_date.isoformat(),
            "error": str(exc)[:160],
        })
        return {
            "ok": False,
            "error": str(exc)[:160],
            "dated_path": str(dated_path),
            "latest_path": str(latest_path),
        }


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
        evidence_days = int(item.get("evidence_days") or 1)
        watch_only = evidence_days < 2
        suggested = ",".join(tickers + [ticker]) if tickers else ticker
        recs.append({
            "category": "universe_watch" if watch_only else "universe",
            "variable": "TICKER_UNIVERSE",
            "current_value": current,
            "suggested_value": suggested,
            "command_text": None if watch_only else f"gh variable set TICKER_UNIVERSE --body {suggested}",
            "reason": (
                f"Watch {ticker}: {item['mentions']} shadow mentions for theme {item['theme']} "
                f"over {evidence_days} evidence day(s). Require 2 days before promotion."
                if watch_only
                else f"{ticker} appeared in shadow universe {item['mentions']} times for theme {item['theme']} across {evidence_days} evidence days."
            ),
            "evidence": item,
            "confidence": min(0.9, 0.45 + 0.10 * evidence_days + 0.03 * int(item["mentions"])),
            "evidence_days": evidence_days,
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
    signals = _rows_for_date(get_recent_signals(hours=72, limit=1000), review_date, "created_at")
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
        "near_miss_distribution": _near_miss_distribution(blocked),
        "direction_error_candidates": _direction_error_candidates(blocked, signals),
        "gate_activity": _gate_activity_from_logs(review_date),
        "shadow_universe": _shadow_repeats_from_logs(review_date),
        "broker_rejections": _broker_rejections_from_logs(review_date),
        "broker_account_snapshot": _broker_account_snapshot(),
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
near_miss_distribution and direction_error_candidates are observability only:
do not recommend trading behavior changes from them until there are at least
14 days or 20 near-threshold cases across multiple regimes, whichever comes later.

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
            max_tokens=2000,
            response_format={"type": "json_object"},
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
    near_miss = metrics.get("near_miss_distribution") or {}
    if near_miss.get("total"):
        lines.append(
            "Near-miss watch: "
            f"{near_miss.get('checked', 0)}/{near_miss.get('total', 0)} checked, "
            f"{near_miss.get('runner_count', 0)} runners. Observability only."
        )
    direction_errors = metrics.get("direction_error_candidates") or []
    if direction_errors:
        parts = [
            f"{d.get('ticker')} {d.get('action')} ({float(d.get('close_after_pct') or 0):+.2f}%)"
            for d in direction_errors[:3]
        ]
        lines.append("Direction diagnostics: " + ", ".join(parts))
    gate_counts = (metrics.get("gate_activity") or {}).get("event_counts") or {}
    if gate_counts:
        important = [
            ("consensus", gate_counts.get("signal_consensus_veto", 0)),
            ("ranging", gate_counts.get("ranging_regime_candidate_block", 0)),
            ("alignment", gate_counts.get("signal_alignment_veto", 0)),
            ("R:R", gate_counts.get("reward_risk_veto", 0)),
        ]
        lines.append(
            "Gate activity: "
            + ", ".join(f"{name} {count}" for name, count in important if count)
        )

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
    if recommendations:
        review_id = saved.get("id") if isinstance(saved, dict) and "error" not in saved else None
        insert_config_change_recommendations(review_id, review_date.isoformat(), recommendations)
    discord_sent = _send_discord(discord_message) if send_discord else False
    snapshot = save_local_daily_review_snapshot(
        review_date, metrics, review, saved, recommendations, discord_message, discord_sent
    )
    log_event("INFO", "daily_eod_review_complete", {
        "review_date": review_date.isoformat(),
        "trade_count": metrics.get("trade_summary", {}).get("total_trades", 0),
        "recommendations": len(recommendations),
        "saved": "error" not in (saved or {}),
        "snapshot_saved": bool(snapshot.get("ok")),
    })
    return {
        "review_date": review_date.isoformat(),
        "metrics": metrics,
        "review": review,
        "saved": saved,
        "snapshot": snapshot,
        "discord_message": discord_message,
    }
