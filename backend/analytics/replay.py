"""
backend/analytics/replay.py
Post-trade and blocked-opportunity replay engine.

Fetches historical price data for past signals and trade exits to
measure counterfactual outcomes — did a blocked signal miss a runner?
did an early exit leave money on the table?

Depends on:
  - stdlib + yfinance
  - backend.runtime.env  (env helpers)
  - database.client      (DB read/write)
"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from typing import Optional

from backend.runtime.env import _env_bool, _env_int, _env_float, _env_value
from database.client import (
    get_unchecked_blocked_opportunities,
    update_blocked_opportunity_replay,
    get_unchecked_closed_trades_for_replay,
    update_trade_post_exit_replay,
    log_event,
)


# ---------------------------------------------------------------------------
# Time parsing helper
# ---------------------------------------------------------------------------

def _parse_supabase_time(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Price-window fetcher (yfinance 1m bars)
# ---------------------------------------------------------------------------

def _replay_price_window(ticker: str, action: str, start_at: datetime,
                         reference_price: float, horizon_minutes: int,
                         period: str = "5d") -> dict:
    import yfinance as yf

    ticker = str(ticker or "").upper()
    action = str(action or "BUY").upper()
    if not ticker or not start_at:
        return {}
    if start_at.tzinfo is None:
        start_at = start_at.replace(tzinfo=timezone.utc)

    try:
        reference_price = float(reference_price)
    except (TypeError, ValueError):
        reference_price = 0

    now_utc = datetime.now(timezone.utc)
    end_at = min(now_utc, start_at + timedelta(minutes=horizon_minutes))
    if end_at <= start_at:
        return {}

    bars = yf.download(
        ticker,
        period=period,
        interval="1m",
        progress=False,
        auto_adjust=True,
    )
    if bars.empty:
        return {}

    if bars.index.tz is None:
        bars.index = bars.index.tz_localize("UTC")
    else:
        bars.index = bars.index.tz_convert("UTC")

    window = bars[(bars.index >= start_at) & (bars.index <= end_at)]
    if window.empty:
        return {}

    close_series = window["Close"].squeeze()
    high_series = window["High"].squeeze()
    low_series = window["Low"].squeeze()
    if reference_price <= 0:
        reference_price = float(close_series.iloc[0])
    if reference_price <= 0:
        return {}

    if action == "SELL":
        max_favorable = (reference_price - float(low_series.min())) / reference_price * 100
        max_adverse = (reference_price - float(high_series.max())) / reference_price * 100
        close_after = (reference_price - float(close_series.iloc[-1])) / reference_price * 100
    else:
        max_favorable = (float(high_series.max()) - reference_price) / reference_price * 100
        max_adverse = (float(low_series.min()) - reference_price) / reference_price * 100
        close_after = (float(close_series.iloc[-1]) - reference_price) / reference_price * 100

    return {
        "max_favorable_pct": round(max_favorable, 4),
        "max_adverse_pct": round(max_adverse, 4),
        "close_after_pct": round(close_after, 4),
        "bars_seen": int(len(window)),
        "reference_price": round(reference_price, 4),
        "start": start_at.isoformat(),
        "end": end_at.isoformat(),
        "action": action,
    }


# ---------------------------------------------------------------------------
# Blocked-opportunity replay
# ---------------------------------------------------------------------------

def _replay_one_blocked_opportunity(opp: dict, horizon_minutes: int) -> dict:
    ticker = str(opp.get("ticker") or "").upper()
    action = str(opp.get("action_hint") or "BUY").upper()
    created_at = _parse_supabase_time(opp.get("created_at"))
    if not ticker or not created_at:
        return {}

    reference_price = opp.get("reference_price")
    try:
        reference_price = float(reference_price)
    except (TypeError, ValueError):
        reference_price = 0

    replay = _replay_price_window(
        ticker, action, created_at, reference_price, horizon_minutes, period="5d"
    )
    if not replay:
        return {}

    favorable = float(replay.get("max_favorable_pct") or 0.0)
    close_after = float(replay.get("close_after_pct") or 0.0)
    runner_threshold = _env_float("MISSED_RUNNER_FAVORABLE_THRESHOLD_PCT", 2.0)
    minor_threshold = _env_float("MISSED_WINNER_FAVORABLE_THRESHOLD_PCT", 0.75)
    runner_severity = None
    if favorable >= runner_threshold and close_after > 0:
        runner_severity = "runner"
    elif favorable >= minor_threshold and close_after > 0:
        runner_severity = "minor"

    return {
        "max_favorable_pct": replay["max_favorable_pct"],
        "max_adverse_pct": replay["max_adverse_pct"],
        "close_after_pct": replay["close_after_pct"],
        "replay_result_json": {
            "horizon_minutes": horizon_minutes,
            "bars_seen": replay["bars_seen"],
            "reference_price": replay["reference_price"],
            "start": replay["start"],
            "end": replay["end"],
            "action": action,
            "block_stage": opp.get("block_stage"),
            "block_reason": opp.get("block_reason"),
            "block_detail": opp.get("block_detail") or {},
            "missed_runner": runner_severity == "runner",
            "runner_severity": runner_severity,
            "runner_threshold_pct": runner_threshold,
        },
    }


def _replay_blocked_opportunities():
    if not _env_bool("BLOCKED_OPPORTUNITY_REPLAY_ENABLED", True):
        return

    min_age = _env_int("BLOCKED_OPPORTUNITY_REPLAY_MIN_AGE_MINUTES", 20)
    horizon = _env_int("BLOCKED_OPPORTUNITY_REPLAY_HORIZON_MINUTES", 90)
    limit = _env_int("BLOCKED_OPPORTUNITY_REPLAY_LIMIT", 25)
    newest_first = _env_bool("BLOCKED_OPPORTUNITY_REPLAY_NEWEST_FIRST", True)
    opportunities = get_unchecked_blocked_opportunities(
        min_age_minutes=min_age,
        limit=limit,
        newest_first=newest_first,
    )
    if not opportunities:
        return

    checked = 0
    updated = 0
    skipped = 0
    for opp in opportunities:
        checked += 1
        try:
            replay = _replay_one_blocked_opportunity(opp, horizon)
            if not replay:
                update_blocked_opportunity_replay(opp.get("id"), {
                    "max_favorable_pct": None,
                    "max_adverse_pct": None,
                    "close_after_pct": None,
                    "replay_result_json": {"skipped": "no_bars", "horizon_minutes": horizon},
                })
                skipped += 1
                continue
            result = update_blocked_opportunity_replay(opp.get("id"), replay)
            if not result.get("error"):
                updated += 1
        except Exception as e:
            log_event("WARN", "blocked_opportunity_replay_failed", {
                "ticker": opp.get("ticker"),
                "id": opp.get("id"),
                "error": str(e)[:160],
            })
    if checked:
        created_dates = {}
        for opp in opportunities:
            created = str(opp.get("created_at") or "")[:10] or "unknown"
            created_dates[created] = created_dates.get(created, 0) + 1
        log_event("INFO", "blocked_opportunity_replay_complete", {
            "checked": checked,
            "updated": updated,
            "skipped_no_bars": skipped,
            "horizon_minutes": horizon,
            "newest_first": newest_first,
            "created_dates": created_dates,
            "oldest_created_at": min(
                (str(o.get("created_at")) for o in opportunities if o.get("created_at")),
                default=None,
            ),
            "newest_created_at": max(
                (str(o.get("created_at")) for o in opportunities if o.get("created_at")),
                default=None,
            ),
        })


# ---------------------------------------------------------------------------
# Closed-trade exit replay
# ---------------------------------------------------------------------------

def _closed_trade_replay_exit_reasons() -> list[str]:
    raw = _env_value(
        "CLOSED_TRADE_REPLAY_EXIT_REASONS",
        "time_exit,eod_cleanup,thesis_invalidated,momentum_peak_decay,"
        "take_profit,stop_loss,chandelier_stop,leveraged_etf_time_exit,"
        "partial_runner_stop,a_plus_override",
    )
    return [part.strip() for part in raw.split(",") if part.strip()]


def _replay_one_closed_trade_exit(trade: dict, horizon_minutes: int) -> dict:
    ticker = str(trade.get("ticker") or "").upper()
    side = str(trade.get("side") or "BUY").upper()
    exited_at = _parse_supabase_time(
        trade.get("exit_time") or trade.get("created_at")
    )
    if not ticker or not exited_at:
        return {}

    try:
        exit_price = float(trade.get("exit_price") or 0)
    except (TypeError, ValueError):
        exit_price = 0

    replay = _replay_price_window(
        ticker, side, exited_at, exit_price, horizon_minutes, period="5d"
    )
    if not replay:
        return {}

    result_json = {
        "horizon_minutes": horizon_minutes,
        "bars_seen": replay["bars_seen"],
        "reference_price": replay["reference_price"],
        "start": replay["start"],
        "end": replay["end"],
        "action": side,
        "exit_reason": trade.get("exit_reason"),
        "net_pnl_pct": trade.get("net_pnl_pct"),
        "setup_grade": trade.get("setup_grade"),
    }
    return {
        "post_exit_horizon_minutes": horizon_minutes,
        "post_exit_max_favorable_pct": replay["max_favorable_pct"],
        "post_exit_max_adverse_pct": replay["max_adverse_pct"],
        "post_exit_close_after_pct": replay["close_after_pct"],
        "post_exit_result_json": result_json,
    }


def _replay_closed_trade_exits():
    if not _env_bool("CLOSED_TRADE_REPLAY_ENABLED", True):
        return

    min_age = _env_int("CLOSED_TRADE_REPLAY_MIN_AGE_MINUTES", 20)
    horizon = _env_int("CLOSED_TRADE_REPLAY_HORIZON_MINUTES", 120)
    limit = _env_int("CLOSED_TRADE_REPLAY_LIMIT", 25)
    max_age_days = _env_int("CLOSED_TRADE_REPLAY_MAX_AGE_DAYS", 5)
    trades = get_unchecked_closed_trades_for_replay(
        min_age_minutes=min_age,
        limit=limit,
        exit_reasons=_closed_trade_replay_exit_reasons(),
        max_age_days=max_age_days,
    )
    if not trades:
        return

    checked = 0
    updated = 0
    skipped = 0
    for trade in trades:
        checked += 1
        try:
            replay = _replay_one_closed_trade_exit(trade, horizon)
            if not replay:
                result = update_trade_post_exit_replay(trade.get("id"), {
                    "post_exit_horizon_minutes": horizon,
                    "post_exit_result_json": {
                        "status": "skipped_no_bars",
                        "horizon_minutes": horizon,
                        "exit_reason": trade.get("exit_reason"),
                        "ticker": trade.get("ticker"),
                    },
                })
                if not result.get("error"):
                    skipped += 1
                continue
            result = update_trade_post_exit_replay(trade.get("id"), replay)
            if not result.get("error"):
                updated += 1
        except Exception as e:
            log_event("WARN", "closed_trade_exit_replay_failed", {
                "ticker": trade.get("ticker"),
                "id": trade.get("id"),
                "exit_reason": trade.get("exit_reason"),
                "error": str(e)[:160],
            })
    if checked:
        log_event("INFO", "closed_trade_exit_replay_complete", {
            "checked": checked,
            "updated": updated,
            "skipped_no_bars": skipped,
            "horizon_minutes": horizon,
        })
