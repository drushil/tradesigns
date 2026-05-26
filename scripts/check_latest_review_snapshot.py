"""Read the latest local daily review snapshot and emit an automation-friendly packet."""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


def _parse_review_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _utc_now() -> datetime:
    override = os.getenv("REVIEW_NOW_timezone.utc", "").strip()
    if override:
        try:
            return datetime.fromisoformat(override.replace("Z", "+00:00")).astimezone(timezone.utc)
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def _previous_business_day(day: date) -> date:
    cursor = day - timedelta(days=1)
    while cursor.weekday() >= 5:
        cursor -= timedelta(days=1)
    return cursor


def _expected_latest_review_date(now: datetime) -> date:
    today = now.date()
    if today.weekday() >= 5:
        return _previous_business_day(today)
    review_cutoff = now.replace(hour=21, minute=25, second=0, microsecond=0)
    if now < review_cutoff:
        return _previous_business_day(today)
    return today


def _top_gate_reasons(snapshot: dict, limit: int = 4) -> list[dict]:
    event_counts = (((snapshot.get("metrics") or {}).get("gate_activity") or {}).get("event_counts") or {})
    ranked = sorted(event_counts.items(), key=lambda item: item[1], reverse=True)
    return [{"event": name, "count": count} for name, count in ranked[:limit]]


def _top_tickers(rows: list[dict], limit: int = 5) -> list[str]:
    counts: dict[str, int] = {}
    for row in rows or []:
        ticker = str(row.get("ticker") or "").upper()
        if not ticker:
            continue
        counts[ticker] = counts.get(ticker, 0) + 1
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return [ticker for ticker, _ in ranked[:limit]]


def _decision_packet(snapshot: dict) -> dict:
    review = snapshot.get("daily_review") or {}
    metrics = snapshot.get("metrics") or {}
    trade = metrics.get("trade_summary") or {}
    blocked = metrics.get("blocked_opportunities") or {}
    shadow = metrics.get("shadow_universe") or []
    near = metrics.get("near_miss_distribution") or {}
    broker = snapshot.get("broker_account_snapshot") or {}

    missed = blocked.get("missed_winners") or []
    bad_avoids = blocked.get("bad_avoids") or []
    review_candidates = [row for row in shadow if row.get("review_candidate")]

    return {
        "summary": review.get("summary"),
        "confidence": review.get("confidence"),
        "worked_well": review.get("worked_well") or [],
        "did_not_work": review.get("did_not_work") or [],
        "what_worked": {
            "bad_avoid_tickers": _top_tickers(bad_avoids),
            "trade_count": int(trade.get("total_trades") or 0),
        },
        "what_failed": {
            "missed_runner_tickers": _top_tickers(missed),
            "top_gate_reasons": _top_gate_reasons(snapshot),
        },
        "needs_more_data": {
            "shadow_review_candidates": [
                {
                    "ticker": row.get("ticker"),
                    "theme": row.get("theme"),
                    "mentions": row.get("mentions"),
                    "evidence_days": row.get("evidence_days"),
                }
                for row in review_candidates[:8]
            ],
            "near_miss_runner_count": int(near.get("runner_count") or 0),
        },
        "broker_context": {
            "daytrade_count": broker.get("daytrade_count"),
            "pattern_day_trader": broker.get("pattern_day_trader"),
            "trading_blocked": broker.get("trading_blocked"),
        },
        "recommendations": review.get("recommendations") or [],
    }


def main() -> None:
    path = Path("artifacts/daily_reviews/latest.json")
    if not path.exists():
        print(json.dumps({"ok": False, "error": "latest snapshot not found", "path": str(path)}, indent=2))
        return
    payload = json.loads(path.read_text(encoding="utf-8"))
    now = _utc_now()
    review_date = _parse_review_date(payload.get("review_date"))
    expected_date = _expected_latest_review_date(now)
    stale = review_date is None or review_date < expected_date
    result = {
        "ok": True,
        "source": "local_snapshot",
        "path": str(path),
        "generated_at": payload.get("generated_at"),
        "review_date": payload.get("review_date"),
        "expected_latest_review_date": expected_date.isoformat(),
        "stale": stale,
        "decision_packet": _decision_packet(payload),
        "snapshot": payload,
    }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
